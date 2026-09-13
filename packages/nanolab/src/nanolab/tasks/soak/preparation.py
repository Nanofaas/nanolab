"""Deferred single-snapshot preparation, with injectable build observations."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.build_executor import OwnedBuildCommandExecutor
from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector
from nanolab.tasks.soak.builds import BuildImagesTask
from nanolab.tasks.soak.images import BuildReceipt, BuildRecipe, plan_images
from nanolab.tasks.soak.sources import SourceSnapshot, capture_source_snapshot
from nanolab.tasks.soak.workload import allocate_vus, constant_arrival_options


@dataclass(frozen=True)
class PreparationOptions:
    """Operational inputs, separate from the frozen numerical acceptance policy."""

    registry: str = "localhost:5000/nanofaas"
    build_timeout_s: float = 3600
    source_limit_bytes: int = 1024 * 1024 * 1024
    build_artifact_limit_bytes: int = 256 * 1024 * 1024
    generator_command: tuple[str, ...] = ("k6",)
    build_observer: Any = None
    prerequisite_runner: Any = None
    prerequisite_provider_available: bool = False
    diagnostic_adapter: Any = None
    # Provider wiring exists; this does NOT assert attachment or target capabilities.
    diagnostic_provider_available: bool = False
    payloads: dict[str, list[dict[str, object]]] | None = None
    support_check: Callable[[SoakConfig], None] | None = None


@dataclass(frozen=True)
class PreparedSoak:
    """Actual immutable build results shared by every deployed application role."""

    run_id: str
    config: SoakConfig
    evidence_dir: Path
    writer: ArtifactWriter
    snapshot: SourceSnapshot
    recipes: tuple[BuildRecipe, ...]
    receipts: tuple[BuildReceipt, ...]
    payloads: dict[str, list[dict[str, object]]]
    builds_finished_s: float
    frozen_at_s: float

    @property
    def images(self) -> dict[str, str]:
        """Return only the immutable references established by build receipts."""
        return {receipt.role: receipt.image_digest for receipt in self.receipts}


def check_preparation_support(config: SoakConfig, options: PreparationOptions) -> None:
    """Reject unsupported diagnostic/profile/build policies before expensive work."""
    if (
        any(config.diagnostics.operations.values())
        and options.diagnostic_adapter is None
        and options.diagnostic_provider_available is not True
    ):
        raise ValueError("diagnostic adapter unavailable; provision it before building")
    if (
        config.prerequisites.required_coverage
        and config.prerequisites.mode == "run"
        and options.prerequisite_runner is None
        and options.prerequisite_provider_available is not True
    ):
        raise ValueError("isolated prerequisite runner unavailable before build")
    if any(image.mode != "build" for image in config.images.values()):
        raise ValueError(
            "prebuilt provenance import is unsupported by this preparation"
        )
    allocation = allocate_vus(
        config.workload.rates, config.workload.preallocated_vus, config.workload.max_vus
    )
    for name, rate in config.workload.rates.items():
        own = allocation[name]
        constant_arrival_options(
            rate, config.phases.steady_s, own["preAllocatedVUs"], own["maxVUs"]
        )
    if (
        not options.generator_command
        or shutil.which(options.generator_command[0]) is None
    ):
        raise ValueError("generator executable unavailable before build")
    if options.support_check is not None:
        options.support_check(config)
    else:
        check_local_process_support(config)
        from nanolab.tasks.soak.collector import collect_http

        registry_host = options.registry.split("/", 1)[0]
        if registry_host.split(":", 1)[0] in {"localhost", "127.0.0.1"}:
            collect_http(f"http://{registry_host}/v2/", 5)


def check_local_process_support(config: SoakConfig) -> None:
    """Check the default host-UID collector before allocating a source/build tree."""
    from nanolab.tasks.soak.collector import _docker_get

    if any(role.runtime not in {"jvm", "node"} for role in config.roles.values()):
        raise ValueError(
            "native executable identity requires an explicit observer before build"
        )
    if any(
        "$" in option
        for role in config.roles.values()
        for option in role.runtime_options
    ):
        raise ValueError(
            "Compose runtime options cannot contain environment interpolation"
        )
    info = _docker_get("/info", "/var/run/docker.sock", 5)
    reported = str(info["Architecture"])
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(reported, reported)
    if any(
        image.platform.split("/")[1] != architecture for image in config.images.values()
    ):
        raise ValueError(
            "default builder requires the observed daemon architecture before build"
        )
    security = info.get("SecurityOptions")
    if not isinstance(security, list) or any(
        "userns" in item or "rootless" in item for item in security
    ):
        raise ValueError(
            "default host-UID procfs collector cannot support daemon UID remapping"
        )
    if any(
        "process_pss_bytes" in role.required_metrics for role in config.roles.values()
    ):
        with Path("/proc/self/smaps_rollup").open("rb") as stream:
            if not stream.read(4096):
                raise ValueError("PSS source unavailable before build")


def _payloads(config: SoakConfig, options: PreparationOptions) -> dict:
    """Freeze explicit correctness cases; built-in word-stats uses known semantics."""
    if options.payloads is not None:
        values = json.loads(json.dumps(options.payloads, allow_nan=False))
    else:
        values = {}
        for role in config.workload.rates:
            if not role.startswith("word-stats-"):
                raise ValueError(
                    "explicit input/expected payload cases required before build: "
                    f"{role}"
                )
            values[role] = [
                {
                    "input": {"text": "hello world hello"},
                    "expected": {
                        "wordCount": 3,
                        "uniqueWords": 2,
                        "topWords": [
                            {"word": "hello", "count": 2},
                            {"word": "world", "count": 1},
                        ],
                        "averageWordLength": 5.0,
                    },
                }
            ]
    if set(values) != set(config.workload.rates):
        raise ValueError("payloads must cover exactly the workload functions")
    for cases in values.values():
        if (
            not isinstance(cases, list)
            or not cases
            or any(
                not isinstance(case, dict) or set(case) != {"input", "expected"}
                for case in cases
            )
        ):
            raise ValueError("payload cases require explicit input and expected output")
    return values


def prepare_soak(
    config: SoakConfig,
    *,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path,
    options: PreparationOptions | None = None,
    build_observer: Any = None,
    cancelled: Event | None = None,
) -> PreparedSoak:
    """Execute preparation only when called by the deferred Sonata runtime task.

    ``build_observer`` supplies the observed provenance collector to BuildImagesTask.
    Its build-stage command capture remains owned by the existing build task.
    There is exactly one source acquisition, regardless of role count.
    """
    options = options or PreparationOptions()
    config = SoakConfig.model_validate(config.model_dump(mode="json"))
    cancelled = cancelled if cancelled is not None else Event()
    check_preparation_support(config, options)
    payloads = _payloads(config, options)
    if cancelled.is_set():
        raise KeyboardInterrupt("soak preparation cancelled")
    executor = OwnedBuildCommandExecutor(
        cwd=repo_root.absolute(),
        log_dir=(run_dir / "build-command-logs").absolute(),
        cancelled=cancelled,
        timeout_cap_s=options.build_timeout_s,
        artifact_limit_bytes=options.build_artifact_limit_bytes,
    )
    observer = build_observer if build_observer is not None else options.build_observer
    if observer is None:
        observer = BuildProvenanceCollector(executor.observe).collect
    # Resolve catalogue support and payload semantics before source capture/build.
    run_id = "soak-" + uuid4().hex
    runtimes = {role: policy.runtime for role, policy in config.roles.items()}
    plan_images(
        repo_root, config.images, runtimes, registry=options.registry, run_id=run_id
    )
    evidence = run_dir / "evidence"
    writer = ArtifactWriter(evidence, config.artifact_limit_bytes)
    resolved = config.model_dump(mode="json")
    writer.write_json("config.json", resolved)
    writer.write_json("payloads.json", payloads)
    result: list[PreparedSoak] = []
    snapshot = Resource(
        title="Capture one source snapshot for all soak roles",
        acquire=lambda inputs: capture_source_snapshot(
            repo_root, evidence / "source", max_bytes=options.source_limit_bytes
        ),
        release=lambda inputs, value: None,
        always_release=True,
    )

    class BuildPrepared(Task):
        title = "Build all images before deploying or measuring"

        def run(self, inputs: TaskInputs) -> TaskOutcome:
            source = inputs.resource(snapshot)
            recipes = plan_images(
                source.root,
                config.images,
                runtimes,
                registry=options.registry,
                run_id=run_id,
            )
            for index, recipe in enumerate(recipes):
                writer.write_json(f"recipe-{index}.json", asdict(recipe))
            task = BuildImagesTask(
                recipes,
                snapshot=snapshot,
                executor=executor,
                output_dir=evidence / "builds",
                collect=observer,
                artifact_limit_bytes=options.build_artifact_limit_bytes,
            )
            receipts = task.run(inputs).value
            finished = time.monotonic()
            if receipts is None:
                raise ValueError("build task produced no receipts")
            if {item.role for item in receipts} != set(config.roles):
                raise ValueError("build results do not cover all configured roles")
            if any(item.source_fingerprint != source.fingerprint for item in receipts):
                raise ValueError(
                    "build receipts do not share the single source snapshot"
                )
            prepared = PreparedSoak(
                run_id,
                config,
                evidence,
                writer,
                source,
                recipes,
                receipts,
                payloads,
                finished,
                time.monotonic(),
            )
            result.append(prepared)
            return TaskOutcome(value=prepared)

    workflow = Workflow(workflow_id="soak-preparation")
    workflow.add(BuildPrepared(), requires=(snapshot,))
    try:
        workflow.run()
        return result[0]
    except BaseException:
        writer.close()
        raise
