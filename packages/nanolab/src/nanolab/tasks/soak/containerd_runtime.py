"""Run the shared soak lifecycle against a systemd CP and containerd functions."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.cli.execution import resolve_loadtest_urls
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.containerd import RootlessCollectionTransport
from nanolab.tasks.soak.preflight import effective_cpu_limit
from nanolab.tasks.soak.preparation import PreparationOptions, _payloads
from nanolab.tasks.soak.runtime import (
    create_soak_lifecycle,
    observe_local_configuration,
)
from nanolab.tasks.soak.sources import capture_source_snapshot
from nanolab.tasks.soak.workflow import write_policy_input, write_terminal_receipt


@dataclass(frozen=True)
class ContainerdBuildReceipt:
    """A remote build's actual running artifact and staged source revision."""

    role: str
    image_digest: str
    source_fingerprint: str
    source_revision: str
    platform: str
    artifact_kind: str
    artifact_path: str | None
    build_argv: tuple[str, ...]


@dataclass(frozen=True)
class ContainerdRecipe:
    """Declared build command and platform for one measured role."""

    role: str
    artifact_kind: str
    platform: str
    build_argv: tuple[str, ...]


@dataclass(frozen=True)
class PreparedContainerdSoak:
    """Frozen source, workload inputs and running artifact receipts."""

    run_id: str
    config: Any
    evidence_dir: Path
    writer: ArtifactWriter
    snapshot: Any
    recipes: tuple[ContainerdRecipe, ...]
    receipts: tuple[ContainerdBuildReceipt, ...]
    payloads: dict[str, list[dict[str, object]]]
    builds_finished_s: float
    frozen_at_s: float

    @property
    def images(self) -> dict[str, str]:
        """Return actual process and OCI identities used by common workload gates."""
        return {item.role: item.image_digest for item in self.receipts}


class ContainerdSoakRun(Task):
    """Measure actual rootless processes; Sonata resources own their cleanup."""

    title = "Run containerd soak measurement"
    idempotent = False

    def __init__(
        self,
        scenario: ScenarioConfig,
        run: RootlessRun,
        environment: EnvironmentConfig,
        executor: CommandTaskExecutor,
        *,
        run_dir: Path,
        repo_root: Path,
        functions: tuple[Any, ...] = (),
    ) -> None:
        """Bind scenario, owned runtime and output without running anything."""
        self.scenario = scenario
        self.rootless = run
        self.environment = environment
        self.executor = executor
        self.run_dir = run_dir
        self.repo_root = repo_root
        self.functions = functions

    def _remote(self, *argv: str) -> str:
        result = self.executor.run(
            CommandTaskSpec(
                task_id="",
                summary="Verify staged soak source",
                argv=argv,
                role="stack",
                options=CommandOptions(timeout_seconds=60),
            )
        )
        if result.status != "passed" or result.return_code != 0:
            raise RuntimeError(
                f"remote source verification failed: {result.stderr[:512]}"
            )
        return result.stdout.strip()

    def _verify_remote_source(self, snapshot: Any) -> dict[str, Any]:
        """Hash each staged source input after builds; rsync omits `.git`."""
        if not snapshot.revision or not snapshot.entries:
            raise ValueError("committed source entries are required")
        hashes = []
        for offset in range(0, len(snapshot.entries), 50):
            entries = [
                asdict(entry) for entry in snapshot.entries[offset : offset + 50]
            ]
            body = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
            encoded = base64.b64encode(zlib.compress(body)).decode()
            if len(encoded) > 65536:
                raise ValueError(
                    "remote source verification batch exceeds argument budget"
                )
            response = json.loads(
                self._remote(
                    "python3",
                    str(self.rootless.script.with_name("source_verify.py")),
                    str(self.rootless.repo_root),
                    encoded,
                )
            )
            expected = hashlib.sha256(body).hexdigest()
            if response != {"count": len(entries), "sha256": expected}:
                raise ValueError(
                    "staged VM source differs from the frozen feature checkout"
                )
            hashes.append(expected)
        return {
            "schema": "nanolab-containerd-source-v1",
            "revision": snapshot.revision,
            "clean": True,
            "source_fingerprint": snapshot.fingerprint,
            "entry_count": len(snapshot.entries),
            "batch_size": 50,
            "batches": hashes,
            "verification": "remote-content-after-build; rsync excludes .git",
        }

    def _prepare(
        self, transport: RootlessCollectionTransport, targets: tuple[Any, ...]
    ) -> PreparedContainerdSoak:
        policy = self.scenario.soak
        assert policy is not None
        evidence = self.run_dir / "evidence"
        writer = ArtifactWriter(evidence, policy.artifact_limit_bytes)
        try:
            writer.write_json("config.json", policy.model_dump(mode="json"))
            payloads = _payloads(policy, PreparationOptions())
            writer.write_json("payloads.json", payloads)
            snapshot = capture_source_snapshot(
                self.repo_root,
                evidence / "source",
                max_bytes=min(policy.artifact_limit_bytes // 2, 1024 * 1024 * 1024),
            )
            if snapshot.dirty:
                raise ValueError("containerd soak requires a committed source snapshot")
            writer.write_json(
                "remote-source.json", self._verify_remote_source(snapshot)
            )
            actual = {role: transport.inspect(role)[1] for role in policy.roles}
            recipes = []
            receipts = []
            commands = {
                function.name: function.image_build_argv or function.build_argv
                for function in self.functions
            }
            commands["control-plane"] = (
                "./gradlew",
                ":control-plane:bootJar",
                "-PcontrolPlaneModules="
                + ",".join(policy.images["control-plane"].modules),
            )
            builds = ArtifactWriter(
                evidence / "builds", policy.artifact_limit_bytes // 4
            )
            try:
                for index, (role, spec) in enumerate(policy.images.items()):
                    detail = actual[role]
                    if (
                        detail["artifact_kind"] != spec.artifact_kind
                        or detail.get("platform") != spec.platform
                    ):
                        raise ValueError(
                            f"{role} running artifact kind/platform differs from policy"
                        )
                    recipe = ContainerdRecipe(
                        role, spec.artifact_kind, spec.platform, tuple(commands[role])
                    )
                    receipt = ContainerdBuildReceipt(
                        role,
                        detail["image_digest"],
                        snapshot.fingerprint,
                        snapshot.revision,
                        detail["platform"],
                        spec.artifact_kind,
                        detail.get("artifact_path"),
                        tuple(commands[role]),
                    )
                    writer.write_json(f"recipe-{index}.json", asdict(recipe))
                    builds.write_json(
                        f"build-{index}.json",
                        {"schema": "nanolab-containerd-build-v1", **asdict(receipt)},
                    )
                    recipes.append(recipe)
                    receipts.append(receipt)
            finally:
                builds.close()
            return PreparedContainerdSoak(
                "soak-" + self.rootless.run_id,
                policy,
                evidence,
                writer,
                snapshot,
                tuple(recipes),
                tuple(receipts),
                payloads,
                time.monotonic(),
                time.monotonic(),
            )
        except BaseException:
            writer.close()
            raise

    def run(self, inputs: TaskInputs) -> TaskOutcome[Any]:
        """Measure the owned deployment and retain a terminal receipt on failure."""
        policy = self.scenario.soak
        if policy is None:
            raise ValueError("containerd soak policy unavailable")
        prepared = None
        holder = None
        try:
            write_policy_input(self.run_dir, self.scenario)
            transport = RootlessCollectionTransport(self.rootless, self.executor)
            targets = tuple(transport.inspect(role)[0] for role in policy.roles)
            for target in targets:
                if target.runtime != policy.roles[target.role].runtime:
                    raise ValueError(
                        f"{target.role} runtime differs from frozen policy"
                    )
            prepared = self._prepare(transport, targets)
            api, _prometheus = resolve_loadtest_urls(
                self.environment, backend="containerd"
            )
            management = api.rsplit(":", 1)[0] + ":8081"

            def discover():
                return tuple(transport.inspect(role)[0] for role in policy.roles)

            def observations(bound_targets):
                effective = observe_local_configuration(
                    prepared, management, api_url=api
                )
                observed: dict[str, Any] = {
                    **effective,
                    "roles": {},
                    "free_bytes": shutil.disk_usage(self.run_dir).free,
                    "remote_source": json.loads(
                        (prepared.evidence_dir / "remote-source.json").read_text()
                    ),
                }
                for target in bound_targets:
                    data = transport.collect(target, None, policy.scrape_timeout_s)
                    _identity, detail = transport.inspect(target.role)
                    config = data["configuration"]
                    cpu = config["cpu_max"]
                    quota = None if cpu[0] == "max" else int(cpu[0])
                    observed["roles"][target.role] = {
                        "image_digest": target.image_digest,
                        "cpu": effective_cpu_limit(
                            quota, int(cpu[1]), config["cpuset"]
                        ),
                        "memory_bytes": config["memory_bytes"],
                        "limit_sources": config["limit_sources"],
                        "runtime": config["runtime"],
                        "runtime_options": config["runtime_options"],
                        "capabilities": config["capabilities"],
                        "collection_sources": config["collection_sources"],
                        "diagnostics": [],
                        "artifact_path": detail.get("artifact_path"),
                        "platform": detail["platform"],
                    }
                    if target.role == "control-plane":
                        observed["roles"][target.role]["modules"] = effective.get(
                            "modules"
                        )
                return observed

            deployment = SimpleNamespace(
                api_endpoint=api,
                metrics_endpoints={
                    role: management + "/actuator/prometheus"
                    if role == "control-plane"
                    else "http://127.0.0.1:8080/metrics"
                    for role in policy.roles
                },
                discover=discover,
                observations=observations,
                diagnostic_inputs=None,
            )
            holder = create_soak_lifecycle(
                prepared,
                deployment=deployment,
                run_dir=self.run_dir,
                transport=transport,
                defer_terminal=True,
            )
            return holder.run(inputs)
        except BaseException as error:
            if not (self.run_dir / "terminal.json").exists():
                write_terminal_receipt(
                    self.run_dir,
                    "ABORTED" if not isinstance(error, Exception) else "INCONCLUSIVE",
                    report_path=getattr(getattr(holder, "state", None), "report", None),
                    reason=str(error),
                )
            raise
        finally:
            if prepared is not None:
                prepared.writer.close()
