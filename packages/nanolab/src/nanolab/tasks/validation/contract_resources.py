"""Owned host-only artifact builds and private Docker execution resources."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sonata_engine import Resource, TaskInputs
from sonata_tasks.buildx import buildx_builder_resource
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.comparison.prepare import captured_source_state
from nanolab.config.contract import ContractConfig
from nanolab.functions.contracts import JsonValue, resolve_contract_matrix
from nanolab.images.bake import render_bake
from nanolab.images.plan import ImageArchitecture, ImageCell, ImagePlan
from nanolab.workspace.recipe import RecipeRun

OWNER_LABEL = "nanolab.contract.owner"
_OWNED_COMMAND = """import json, os, sys
from dataclasses import asdict
from pathlib import Path
from threading import Event
from sonata_tasks.process import run_owned_command
cfg = json.loads(sys.argv[1])
cfg['log_path'] = Path(cfg['log_path'])
cfg['env'] = dict(os.environ, **cfg['env'])
result = run_owned_command(**cfg, cancelled=Event())
print(json.dumps(asdict(result)))
"""


def host_architecture() -> ImageArchitecture:
    """Reject emulation rather than silently selecting a foreign architecture."""
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if sys.platform != "linux" or architecture is None:
        raise ValueError("contract requires a Linux amd64/arm64 host")
    return cast(ImageArchitecture, architecture)


def write_receipt(path: Path, value: object) -> None:
    """Never replace evidence from an earlier observation or attempt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, default=str)
        stream.write("\n")


class ContractExecutor:
    """Reuse Sonata's bounded descendant owner through the existing host binding."""

    def __init__(
        self, delegate: CommandTaskExecutor, run_dir: Path, settings: ContractConfig
    ):
        """Bind one local attempt's command limits and immutable command receipts."""
        self.delegate, self.run_dir, self.settings = delegate, run_dir, settings

    def binding_key(self, role: str) -> str:
        """Preserve the selected execution binding in task fingerprints."""
        return self.delegate.binding_key(role)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        """Run through the published owner and reject unconfirmed cleanup."""
        if dry_run:
            raise ValueError("contract commands cannot produce dry-run evidence")
        log = self.run_dir / "commands" / f"{uuid4().hex}.log"
        config = {
            "argv": task.argv,
            "cwd": str(task.options.cwd or self.run_dir),
            "env": dict(task.options.env),
            "log_path": str(log),
            "timeout_s": task.options.timeout_seconds or self.settings.request_seconds,
            "output_limit_bytes": self.settings.log_bytes,
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        wrapped = CommandTaskSpec(
            task.task_id,
            task.summary,
            (sys.executable, "-c", _OWNED_COMMAND, json.dumps(config)),
            task.role,
        )
        result = self.delegate.run(wrapped)
        if not result.ok:
            raise RuntimeError(f"contract command supervisor failed: {task.summary}")
        observed = json.loads(result.stdout)
        write_receipt(log.with_suffix(".json"), {"command": config, "result": observed})
        if not observed.get("reaped") or any(
            observed.get(key)
            for key in (
                "forced_stop",
                "cancelled",
                "timed_out",
                "quota_exceeded",
                "errors",
            )
        ):
            raise RuntimeError(
                f"contract command deadline/output/cleanup failed: {task.summary}"
            )
        code = observed.get("returncode")
        if type(code) is not int:
            raise RuntimeError("contract command has no exit status")
        return TaskResult(
            task_id=task.task_id,
            status="passed" if code in task.options.expected_exit_codes else "failed",
            return_code=code,
            expected_exit_codes=task.options.expected_exit_codes,
            stdout=log.read_bytes().decode(errors="replace"),
        )


def contract_command(
    executor: CommandTaskExecutor,
    inputs: TaskInputs,
    argv: tuple[str, ...],
    *,
    cwd: Path | None = None,
    deadline: float | None = None,
    missing: bool = False,
) -> TaskResult:
    """Keep all infrastructure commands inside the bounded resource executor."""
    result = (
        CommandTask(
            title=" ".join(argv[:3]),
            argv=argv,
            executor=executor,
            options=CommandOptions(
                cwd=cwd,
                timeout_seconds=deadline,
                expected_exit_codes=frozenset({0, 1}) if missing else frozenset({0}),
            ),
        )
        .run(inputs)
        .value
    )
    if result is None:
        raise RuntimeError("contract command returned no observation")
    return result


def inspect_contract_object(
    executor: CommandTaskExecutor, inputs: TaskInputs, kind: str, reference: str
) -> dict[str, Any] | None:
    """Absence requires a successful daemon response naming the missing object."""
    prefix = (
        ("docker", "inspect") if kind == "container" else ("docker", kind, "inspect")
    )
    result = contract_command(executor, inputs, (*prefix, reference), missing=True)
    if result.return_code == 1:
        if not any(
            marker in result.stdout.lower() for marker in ("no such", "not found")
        ):
            raise RuntimeError("Docker observation failed without proving absence")
        return None
    value = json.loads(result.stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError("invalid Docker identity observation")
    return value[0]


def _owned_labels(value: dict[str, Any], kind: str) -> dict[str, str]:
    return (
        value.get("Labels", {})
        if kind == "network"
        else value.get("Config", {}).get("Labels", {}) or {}
    )


def _register(
    run_dir: Path, kind: str, reference: str, owner: str, identity: str | None
) -> Path:
    key = hashlib.sha256(f"{kind}:{reference}".encode()).hexdigest()[:24]
    path = run_dir / "ownership" / f"{kind}-{key}.json"
    write_receipt(
        path,
        {"kind": kind, "reference": reference, "owner": owner, "identity": identity},
    )
    return path


def _release_owned(
    executor: CommandTaskExecutor, inputs: TaskInputs, receipt: Path, run_dir: Path
) -> None:
    owned = json.loads(receipt.read_text())
    kind, reference, owner, identity = (
        owned[key] for key in ("kind", "reference", "owner", "identity")
    )
    binding = run_dir / "identity-bindings" / receipt.name
    if identity is None and binding.exists():
        identity = json.loads(binding.read_text())["identity"]
    cleanup = run_dir / "cleanup" / receipt.name
    if cleanup.exists():
        if not json.loads(cleanup.read_text()).get("absent"):
            raise RuntimeError("previous ownership cleanup failed")
        return
    try:
        current = inspect_contract_object(executor, inputs, kind, identity or reference)
        if current is not None:
            if _owned_labels(current, kind).get(OWNER_LABEL) != owner or (
                identity is not None and current.get("Id") != identity
            ):
                raise RuntimeError("contract resource ownership conflict")
            verified_id = current["Id"]
            if kind == "container":
                contract_command(
                    executor, inputs, ("docker", "rm", "--force", verified_id)
                )
            else:
                contract_command(executor, inputs, ("docker", kind, "rm", verified_id))
        if inspect_contract_object(executor, inputs, kind, reference) is not None or (
            identity is not None
            and inspect_contract_object(executor, inputs, kind, identity) is not None
        ):
            raise RuntimeError("contract resource absence unconfirmed")
        write_receipt(cleanup, {"absent": True, "owned": owned})
    except BaseException as error:
        write_receipt(cleanup, {"absent": False, "owned": owned, "error": str(error)})
        raise


def _release_all(
    executor: CommandTaskExecutor,
    inputs: TaskInputs,
    receipts: list[Path],
    run_dir: Path,
) -> None:
    errors = []
    for receipt in reversed(receipts):
        try:
            _release_owned(executor, inputs, receipt, run_dir)
        except BaseException as error:
            errors.append(error)
    if errors:
        raise RuntimeError(
            "contract cleanup failed: " + "; ".join(str(error) for error in errors)
        ) from errors[0]


@dataclass(frozen=True, slots=True)
class ContractRuntime:
    """Only this attempt's private execution/capture network."""

    network: str
    capture_container: str
    probe_image: str
    owner: str


@dataclass(frozen=True, slots=True)
class ContractImage:
    """Identity of the actual built cell, including native executable content."""

    cell: ImageCell
    image_id: str
    architecture: str
    entrypoint: tuple[str, ...]
    native_sha256: str | None


def validate_native_binary(raw: bytes, architecture: str) -> str:
    """Reject a JVM wrapper or foreign ELF64 executable in a native cell."""
    machine = {"amd64": 62, "arm64": 183}.get(architecture)
    if (
        machine is None
        or len(raw) < 64
        or raw[:6] != b"\x7fELF\x02\x01"
        or int.from_bytes(raw[18:20], "little") != machine
    ):
        raise ValueError("native artifact must be a matching ELF64 executable")
    return hashlib.sha256(raw).hexdigest()


def contract_images_resource(
    source: Resource[RecipeRun],
    *,
    selectors: tuple[str, ...],
    settings: ContractConfig,
    executor: CommandTaskExecutor,
    run_dir: Path,
    tag: str,
) -> Resource[tuple[ContractImage, ...]]:
    """Build the complete selected matrix under an exclusively owned builder."""
    bounded = ContractExecutor(executor, run_dir, settings)
    node = f"contract-{tag}-{uuid4().hex[:12]}"
    builder_name = f"nanolab-contract-{tag}"
    config = run_dir / "buildkitd.toml"
    builder = buildx_builder_resource(
        name=builder_name,
        executor=bounded,
        exclusive=True,
        owner_node=node,
        use=False,
        buildkitd_config=str(config),
        driver_options=(
            f"memory={settings.builder_memory_bytes}",
            "cpu-period=100000",
            f"cpu-quota={settings.builder_cpu_quota}",
        ),
    )
    builder_value: str | None = None
    receipts: list[Path] = []

    def cleanup(inputs: TaskInputs) -> None:
        nonlocal builder_value
        try:
            _release_all(bounded, inputs, receipts, run_dir)
        finally:
            if builder_value is not None:
                builder.release(inputs, builder_value)
                builder_value = None
                result = contract_command(
                    bounded,
                    inputs,
                    ("docker", "buildx", "inspect", builder_name),
                    missing=True,
                )
                if result.return_code != 1 or not any(
                    marker in result.stdout.lower()
                    for marker in ("not found", "no builder")
                ):
                    raise RuntimeError("builder absence unconfirmed")
                write_receipt(
                    run_dir / "builder-cleanup.json",
                    {"absent": True, "name": builder_name, "node": node},
                )

    def acquire(inputs: TaskInputs) -> tuple[ContractImage, ...]:
        nonlocal builder_value
        staged = inputs.resource(source)
        matrix = resolve_contract_matrix(
            staged.source_dir, selectors, architecture=host_architecture(), tag=tag
        )
        write_receipt(run_dir / "contract-settings.json", settings.model_dump())
        write_receipt(
            run_dir / "matrix.json",
            {
                "source": matrix.source_state,
                "corpusHashes": matrix.corpus_hashes,
                "subset": matrix.subset,
                "cases": {
                    family: [asdict(case) for case in cases]
                    for family, cases in matrix.cases.items()
                },
                "images": [asdict(cell) for cell in matrix.images.cells],
            },
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[worker.oci]\n  runtime = "runc"\n')
        builder_value = builder.acquire(inputs)
        limits = inspect_contract_object(
            bounded, inputs, "container", f"buildx_buildkit_{node}"
        )
        expected = {
            "Memory": settings.builder_memory_bytes,
            "CpuQuota": settings.builder_cpu_quota,
            "CpuPeriod": 100000,
        }
        if limits is None or any(
            limits.get("HostConfig", {}).get(key) != value
            for key, value in expected.items()
        ):
            raise RuntimeError("private builder resource limits disagree")
        write_receipt(
            run_dir / "builder.json",
            {"name": builder_name, "node": node, "limits": limits},
        )
        prerequisites = dict.fromkeys(
            cell.prerequisite_command
            for cell in matrix.images.cells
            if cell.prerequisite_command is not None
        )
        for command in prerequisites:
            contract_command(
                bounded,
                inputs,
                command,
                cwd=staged.source_dir,
                deadline=settings.build_seconds,
            )
        images = []
        for index, cell in enumerate(matrix.images.cells):
            if (
                inspect_contract_object(bounded, inputs, "image", cell.image)
                is not None
            ):
                raise RuntimeError("contract image tag already exists")
            receipt = _register(run_dir, "image", cell.image, tag, None)
            receipts.append(receipt)
            single = ImagePlan(
                matrix.images.version, matrix.images.registry, (cell.target,), (cell,)
            )
            bake = render_bake(single)
            target = next(iter(bake["target"].values()))
            target["labels"] = {OWNER_LABEL: tag}
            if cell.native_build is not None:
                empty_maven = run_dir / "empty-maven-context"
                empty_maven.mkdir(exist_ok=True)
                target["contexts"] = {"containerd_maven_repo": str(empty_maven)}
                args = target["args"]
                parallelism = max(1, settings.builder_cpu_quota // 100000)
                args["GRADLE_ARGS"] += (
                    f" -PnativeBuildMemory={settings.builder_memory_bytes // 2}"
                    f" -PnativeParallelism={parallelism}"
                )
            bake_path = run_dir / "builds" / f"{index:03d}.json"
            write_receipt(bake_path, bake)
            contract_command(
                bounded,
                inputs,
                (
                    "docker",
                    "buildx",
                    "bake",
                    "--builder",
                    builder_name,
                    "--file",
                    str(bake_path),
                    "--load",
                ),
                cwd=staged.source_dir,
                deadline=settings.build_seconds,
            )
            if captured_source_state(staged.source_dir) != matrix.source_state:
                raise RuntimeError("contract source changed during build")
            observed = inspect_contract_object(bounded, inputs, "image", cell.image)
            if (
                observed is None
                or observed.get("Architecture") != cell.architecture
                or _owned_labels(observed, "image").get(OWNER_LABEL) != tag
            ):
                raise RuntimeError("built contract image identity disagrees")
            write_receipt(
                run_dir / "identity-bindings" / receipt.name,
                {"identity": observed["Id"]},
            )
            entrypoint = tuple(observed.get("Config", {}).get("Entrypoint") or ())
            if not entrypoint:
                raise RuntimeError("contract image has no entrypoint")
            native_hash = None
            if cell.flavor == "native":
                name = f"contract-extract-{uuid4().hex}"
                extract_receipt = _register(run_dir, "container", name, tag, None)
                receipts.append(extract_receipt)
                identity = contract_command(
                    bounded,
                    inputs,
                    (
                        "docker",
                        "create",
                        "--name",
                        name,
                        "--label",
                        f"{OWNER_LABEL}={tag}",
                        observed["Id"],
                    ),
                ).stdout.strip()
                write_receipt(
                    run_dir / "identity-bindings" / extract_receipt.name,
                    {"identity": identity},
                )
                binary = run_dir / "native" / f"{index:03d}.elf"
                binary.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if not entrypoint[0].startswith("/"):
                        raise RuntimeError("native entrypoint must name its executable")
                    contract_command(
                        bounded,
                        inputs,
                        ("docker", "cp", f"{identity}:{entrypoint[0]}", str(binary)),
                    )
                    native_hash = validate_native_binary(
                        binary.read_bytes(), cell.architecture
                    )
                finally:
                    _release_owned(bounded, inputs, extract_receipt, run_dir)
            image = ContractImage(
                cell, observed["Id"], cell.architecture, entrypoint, native_hash
            )
            images.append(image)
            write_receipt(
                run_dir / "images" / f"{index:03d}.json",
                {"cell": asdict(cell), "image": observed, "nativeSha256": native_hash},
            )
        return tuple(images)

    return compensated_resource(
        title="Build frozen contract artifacts",
        acquire=acquire,
        compensate=cleanup,
        release=lambda inputs, _: cleanup(inputs),
        requires=(source,),
    )


def artifact_container_resource(
    image: ContractImage,
    runtime: ContractRuntime,
    *,
    mode: Literal["sdk", "warm", "one-shot"],
    execution_id: str | None,
    payload: JsonValue | None,
    executor: CommandTaskExecutor,
    run_dir: Path,
) -> Resource[str]:
    """Own a real artifact container without overriding its entrypoint or command."""
    bounded = (
        executor
        if isinstance(executor, ContractExecutor)
        else ContractExecutor(executor, run_dir, ContractConfig())
    )
    receipt: Path | None = None

    def cleanup(inputs: TaskInputs) -> None:
        if receipt is not None:
            _release_owned(bounded, inputs, receipt, run_dir)

    def acquire(inputs: TaskInputs) -> str:
        nonlocal receipt
        name = f"contract-artifact-{uuid4().hex}"
        receipt = _register(run_dir, "container", name, runtime.owner, None)
        env = ["CALLBACK_URL=http://capture:8081/v1/executions"]
        if mode == "one-shot":
            if execution_id is None:
                raise ValueError("one-shot requires execution ID")
            env += [
                "WARM=false",
                f"EXECUTION_ID={execution_id}",
                f"INVOCATION_PAYLOAD={json.dumps(payload)}",
            ]
        else:
            env += ["WARM=true", "PORT=8080", "SERVER_PORT=8080"]
        argv = (
            "docker",
            "create",
            "--name",
            name,
            "--label",
            f"{OWNER_LABEL}={runtime.owner}",
            "--network",
            runtime.network,
            "--network-alias",
            "artifact",
            *(part for value in env for part in ("--env", value)),
            image.image_id,
        )
        identity = contract_command(bounded, inputs, argv).stdout.strip()
        write_receipt(
            run_dir / "identity-bindings" / receipt.name, {"identity": identity}
        )
        observed = inspect_contract_object(bounded, inputs, "container", identity)
        if (
            observed is None
            or observed.get("Image") != image.image_id
            or observed.get("HostConfig", {}).get("PortBindings")
        ):
            raise RuntimeError("started artifact identity or ports disagree")
        write_receipt(run_dir / "containers" / f"{identity}.json", observed)
        contract_command(bounded, inputs, ("docker", "start", identity))
        return identity

    return compensated_resource(
        title=f"Owned {mode} artifact",
        acquire=acquire,
        compensate=cleanup,
        release=lambda inputs, _: cleanup(inputs),
    )


def contract_runtime_resource(
    *,
    settings: ContractConfig,
    executor: CommandTaskExecutor,
    run_dir: Path,
    tag: str,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[ContractRuntime]:
    """Own the helper image, capture container and private Docker network."""
    bounded = ContractExecutor(executor, run_dir, settings)
    receipts: list[Path] = []

    def acquire(inputs: TaskInputs) -> ContractRuntime:
        network = f"contract-{tag}"
        if inspect_contract_object(bounded, inputs, "network", network) is not None:
            raise RuntimeError("contract network already exists")
        network_receipt = _register(run_dir, "network", network, tag, None)
        receipts.append(network_receipt)
        identity = contract_command(
            bounded,
            inputs,
            ("docker", "network", "create", "--label", f"{OWNER_LABEL}={tag}", network),
        ).stdout.strip()
        write_receipt(
            run_dir / "identity-bindings" / network_receipt.name, {"identity": identity}
        )
        helper = f"nanolab-contract/{tag}/capture:probe"
        if inspect_contract_object(bounded, inputs, "image", helper) is not None:
            raise RuntimeError("contract helper tag already exists")
        helper_receipt = _register(run_dir, "image", helper, tag, None)
        receipts.append(helper_receipt)
        assets = run_dir / "helper"
        assets.mkdir(parents=True, exist_ok=True)
        helper_hashes = {}
        for filename in ("artifact_contract.py", "artifact_contract.Dockerfile"):
            raw = (
                files("nanolab")
                .joinpath("assets", "diagnostics", filename)
                .read_bytes()
            )
            with (assets / filename).open("xb") as stream:
                stream.write(raw)
            helper_hashes[filename] = hashlib.sha256(raw).hexdigest()
        write_receipt(run_dir / "helper-inputs.json", helper_hashes)
        contract_command(
            bounded,
            inputs,
            (
                "docker",
                "buildx",
                "build",
                "--builder",
                f"nanolab-contract-{tag}",
                "--load",
                "--label",
                f"{OWNER_LABEL}={tag}",
                "--tag",
                helper,
                "--file",
                str(assets / "artifact_contract.Dockerfile"),
                str(assets),
            ),
            deadline=settings.build_seconds,
        )
        helper_identity = inspect_contract_object(bounded, inputs, "image", helper)
        if (
            helper_identity is None
            or _owned_labels(helper_identity, "image").get(OWNER_LABEL) != tag
        ):
            raise RuntimeError("contract helper image identity disagrees")
        write_receipt(
            run_dir / "identity-bindings" / helper_receipt.name,
            {"identity": helper_identity["Id"]},
        )
        name = f"contract-capture-{tag}"
        capture_receipt = _register(run_dir, "container", name, tag, None)
        receipts.append(capture_receipt)
        instruction = json.dumps({"settings": settings.model_dump()})
        capture = contract_command(
            bounded,
            inputs,
            (
                "docker",
                "create",
                "--name",
                name,
                "--label",
                f"{OWNER_LABEL}={tag}",
                "--network",
                network,
                "--network-alias",
                "capture",
                helper,
                "serve",
                instruction,
            ),
        ).stdout.strip()
        write_receipt(
            run_dir / "identity-bindings" / capture_receipt.name, {"identity": capture}
        )
        contract_command(bounded, inputs, ("docker", "start", capture))
        helper_identity = inspect_contract_object(bounded, inputs, "image", helper)
        write_receipt(run_dir / "capture-image.json", helper_identity)
        return ContractRuntime(network, capture, helper, tag)

    return compensated_resource(
        title="Owned contract capture network",
        acquire=acquire,
        compensate=lambda inputs: _release_all(bounded, inputs, receipts, run_dir),
        release=lambda inputs, _: _release_all(bounded, inputs, receipts, run_dir),
        requires=requires,
    )


def verify_contract_cleanup(run_dir: Path, executor: CommandTaskExecutor) -> None:
    """Observe absence independently after all releases, preserving failed proof."""
    bounded = ContractExecutor(executor, run_dir, ContractConfig())
    inputs = TaskInputs.empty()
    inventory = []
    for receipt in sorted((run_dir / "ownership").glob("*.json")):
        owned = json.loads(receipt.read_text())
        binding = run_dir / "identity-bindings" / receipt.name
        if owned["identity"] is None and binding.exists():
            owned["identity"] = json.loads(binding.read_text())["identity"]
        cleanup = run_dir / "cleanup" / receipt.name
        if (
            not cleanup.exists()
            or not json.loads(cleanup.read_text()).get("absent")
            or inspect_contract_object(
                bounded, inputs, owned["kind"], owned["reference"]
            )
            is not None
            or (
                owned["identity"] is not None
                and inspect_contract_object(
                    bounded, inputs, owned["kind"], owned["identity"]
                )
                is not None
            )
        ):
            raise RuntimeError("contract cleanup absence not proven")
        inventory.append(owned)
    builder = json.loads((run_dir / "builder-cleanup.json").read_text())
    observed = contract_command(
        bounded, inputs, ("docker", "buildx", "inspect", builder["name"]), missing=True
    )
    if (
        not builder["absent"]
        or observed.return_code != 1
        or not any(
            marker in observed.stdout.lower() for marker in ("not found", "no builder")
        )
    ):
        raise RuntimeError("contract builder absence not proven")
    write_receipt(run_dir / "owned-resources.json", inventory)
