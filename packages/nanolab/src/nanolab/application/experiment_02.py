"""Experiment 02 orchestration, independent of command-line presentation."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from traceback import format_exception_only
from typing import Protocol
from uuid import uuid4

from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.application.inputs import load_environment, load_scenario
from nanolab.application.provisioning import provisioning_context
from nanolab.application.vm_provider import (
    provider_for_environment,
    vm_request_for_role,
)
from nanolab.comparison.cells import (
    DIAGNOSTICS,
    LOAD_STARTED,
    Outcome,
    archive_cell_result,
    attempt_dir,
    experiment_completed,
    next_attempt,
    write_cell_result,
)
from nanolab.comparison.evidence import require_recorded_publications
from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    EXPERIMENT_02_PUBLISHER,
    Experiment02Cell,
    build_experiment_02_matrix,
    experiment_02_identity,
    required_evidence,
    write_experiment_02_report,
)
from nanolab.comparison.manifest import (
    ComparisonManifest,
    capture_comparison_inputs,
    new_comparison_manifest,
    read_comparison_manifest,
    require_matching_inputs,
    write_comparison_manifest,
)
from nanolab.comparison.matrix import pending
from nanolab.comparison.prepare import comparison_remote_command
from nanolab.comparison.profiles import (
    EXPERIMENT_02_CONTRACT,
    EXPERIMENT_02_FUNCTIONS,
    EXPERIMENT_02_MODULES,
    PreparedComparison,
    experiment_02_profiles,
)
from nanolab.comparison.target import (
    read_comparison_target,
    read_host_facts,
    require_comparison_target,
)
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.experiment_01 import experiment_01_payload
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.experiment_02.baselines import (
    Fn,
    FunLess,
    Serverledge,
    Spin,
    TinyFaas,
    ensure_baselines,
)
from nanolab.tasks.experiment_02.budget import slice_setup_operation
from nanolab.tasks.experiment_02.cell import CLEANUP_ERRORS, run_cell
from nanolab.tasks.experiment_02.platforms import (
    DirectReference,
    NanofaasPlatform,
    Platform,
)
from nanolab.tasks.experiment_02.refusal import CellRefusedError
from nanolab.tasks.experiment_02.vm import Vm
from nanolab.tasks.experiment_02.workload import (
    WORKLOAD,
    WORKLOADS_ENV,
    capture_workloads,
    ensure_workloads,
)
from nanolab.tasks.recipes.remote import remote_recipe_root
from nanolab.tasks.vm.models import VmRequest, vm_remote_home
from nanolab.workspace.paths import bundled_assets_root, default_tool_paths

EXPERIMENT_02_SCRIPT_NAME = "experiment-01.js"
_EXPERIMENT_02_REASONS: dict[str, str] = {
    "measured": "completed",
    "oom": "OOM kill in the measured slice",
    "startup_failed": "no correct response before the readiness timeout",
}


class ComparisonPreparer(Protocol):
    """Prepare published recipes while the entry point owns progress rendering."""

    def __call__(
        self,
        *,
        source: Path,
        profiles: Mapping[str, Path],
        root: Path,
        manifest: ComparisonManifest,
        provider: VmCommandProvider,
        request: VmRequest,
    ) -> PreparedComparison:
        """Stage source and return the verified published distributions."""
        ...


def run_experiment_02_cell(
    cell: Experiment02Cell,
    *,
    root: Path,
    identity: Mapping[str, object],
    before_attempt: Callable[[], None],
    run_attempt: Callable[[Path], Outcome],
    notify: Callable[[str], None] = print,
) -> Outcome:
    """Run one experiment 02 cell to a definitive outcome, or stop the matrix.

    Each attempt has its own directory and an outcome commits a receipt. A
    refusal (`CellRefusedError`: a budget not enforced, a process outside it)
    stops at once. Any other failure before the load is taken as transport and
    retried once in a new attempt, provided the attempt cleaned up after
    itself; after the load started, it stops. A cleanup that failed leaves
    the remote state unknown: a documented `oom` or `startup_failed` keeps its
    receipt, a `measured` cell does not, and the matrix stops either way.
    """
    transport_retried = False
    while True:
        cell_dir = cell.run_dir(root)
        archive_cell_result(cell_dir)
        attempt = next_attempt(cell_dir)
        run_dir = attempt_dir(cell_dir, attempt)
        run_dir.mkdir(parents=True)
        started = datetime.now(UTC)
        before_attempt()
        cleanup_errors = run_dir / DIAGNOSTICS / CLEANUP_ERRORS
        try:
            outcome = run_attempt(run_dir)
        except Exception as error:
            diagnostics = run_dir / DIAGNOSTICS
            diagnostics.mkdir(exist_ok=True)
            (diagnostics / "error.txt").write_text(
                "".join(format_exception_only(error))
            )
            if (
                isinstance(error, CellRefusedError)
                or (run_dir / LOAD_STARTED).is_file()
                or transport_retried
                or cleanup_errors.is_file()
            ):
                raise
            transport_retried = True
            notify(f"  {cell.label} failed before load ({error}); retrying once")
            continue
        if cleanup_errors.is_file() and outcome == "measured":
            raise RuntimeError(
                f"{cell.label}: cleanup failed, see {cleanup_errors}; "
                "the remote state is unknown, so the measurement is not kept"
            )
        _ = write_cell_result(
            cell,
            root,
            identity=identity,
            attempt=attempt,
            outcome=outcome,
            reason=_EXPERIMENT_02_REASONS[outcome],
            started_at=started,
            required=required_evidence(cell.configuration),
        )
        notify(f"  {cell.label}: {outcome} (attempt {attempt})")
        if cleanup_errors.is_file():
            raise RuntimeError(
                f"{cell.label}: cleanup failed, see {cleanup_errors}; "
                "stopping before the next cell"
            )
        return outcome


def experiment_02_platform(
    cell: Experiment02Cell,
    prepared: PreparedComparison,
    *,
    function_image: str,
    protocol: Experiment02Protocol,
    installed: Mapping[str, Mapping[str, str]],
) -> Platform:
    """Return the platform a cell deploys, from the prepared images and installs."""
    configuration = cell.configuration
    if configuration.platform == "baseline":
        key = configuration.key
        root = PurePosixPath(installed[key]["root"])
        image = installed[key]["image"]
        baselines: dict[str, Callable[[], Platform]] = {
            "funless": lambda: FunLess(root=root),
            "spin": lambda: Spin(root=root),
            "tinyfaas": lambda: TinyFaas(root=root),
            "serverledge": lambda: Serverledge(
                budget=cell.budget, root=root, image=image
            ),
            "fn": lambda: Fn(root=root, image=image),
        }
        return baselines[key]()
    if configuration.platform == "direct" or configuration.profile is None:
        return DirectReference(function_image)
    distribution = prepared.distributions[configuration.key]
    return NanofaasPlatform(
        profile=configuration.profile,
        control_plane_image=distribution.control_plane().image.reference,
        function_image=function_image,
        modules=EXPERIMENT_02_MODULES[configuration.profile],
        concurrency=protocol.fixed_concurrency,
        queue_capacity=protocol.queue_capacity,
        timeout_ms=protocol.http_timeout_seconds * 1000,
        runtime=configuration.runtime,
    )


def compare_experiment_02(  # NOSONAR (S3776): one sequential matrix run
    *,
    scenario: Path,
    scenario_config: ScenarioConfig,
    environment: Path,
    environment_config: EnvironmentConfig,
    selected: tuple[str, ...],
    repetitions: int,
    run_dir: Path | None,
    fresh: bool,
    native_build_memory: str | None,
    native_parallelism: int | None,
    prepare: ComparisonPreparer,
    notify: Callable[[str], None],
) -> None:
    """Run experiment 02: complete platforms on Docker under budgeted cgroups.

    The shape is experiment 01's: one manifest, one provisioning, one prepare,
    then the cells in the protocol's seeded order, each with its receipt. What
    differs is the stack (Docker and cgroup slices, no k3s) and the cell, which
    `tasks.experiment_02.cell.run_cell` drives directly on the VMs.
    """
    protocol = scenario_config.experiment_02
    if protocol is None:
        raise ValueError("experiment-02 requires its experiment02 block")
    if environment_config.provider != "azure" or set(environment_config.roles) != {
        "stack",
        "loadgen",
    }:
        raise ValueError(
            "experiment-02 runs on Azure with exactly the stack and loadgen roles"
        )
    if (
        scenario_config.control_plane_image
        or scenario_config.function_images
        or scenario_config.recipe_profile
    ):
        raise ValueError(
            "compare requires the profile's own functions "
            "without prebuilt image overrides"
        )
    paths = default_tool_paths()
    script = bundled_assets_root() / "k6" / EXPERIMENT_02_SCRIPT_NAME
    try:
        profiles = experiment_02_profiles(paths.tool_root, selected)
        configurations = tuple(CONFIGURATIONS_BY_KEY[key] for key in selected)
        payload = experiment_01_payload(
            protocol, paths.tool_root / "scenarios" / "payloads"
        )
        cells = build_experiment_02_matrix(
            configurations,
            tuple(protocol.budgets),
            repetitions,
            seed=protocol.order_seed,
        )
        baselines = tuple(
            key for key in selected if CONFIGURATIONS_BY_KEY[key].platform == "baseline"
        )
        workloads_root = None
        if baselines:
            location = os.environ.get(WORKLOADS_ENV)
            if not location:
                raise ValueError(
                    f"{WORKLOADS_ENV} must name nanofaas-paper/functions "
                    "to measure a baseline"
                )
            workloads_root = Path(location)
        workloads = (
            None if workloads_root is None else capture_workloads(workloads_root)
        )
        experiment = experiment_02_identity(
            protocol,
            cells,
            payload=payload,
            script=script,
            selected=selected,
            seed=protocol.order_seed,
            workloads=workloads,
        )
        root = (run_dir or paths.runs_dir / "experiment-02").resolve()
        manifest = read_comparison_manifest(root)
        new_run = manifest is None
        tag = (
            f"recipe-{uuid4().hex}"
            if manifest is None
            else str(manifest.identity["tag"])
        )
        nanolab_root = Path(
            subprocess.check_output(
                ("git", "rev-parse", "--show-toplevel"),
                cwd=paths.tool_root,
                text=True,
                timeout=120,
            ).strip()
        )

        def capture() -> dict[str, object]:
            return capture_comparison_inputs(
                scenario=load_scenario(scenario),
                environment=load_environment(environment),
                nanofaas_root=paths.nanofaas_root,
                nanolab_root=nanolab_root,
                profiles=profiles,
                variants=selected,
                repetitions=repetitions,
                tag=tag,
                build_memory=native_build_memory,
                parallelism=native_parallelism,
                experiment=experiment,
                profile_options=EXPERIMENT_02_CONTRACT.options,
            )

        inputs = capture()
        if manifest is not None:
            require_matching_inputs(manifest, inputs)
            if manifest.target is None:
                raise ValueError(
                    "Comparison manifest has no original target identity; "
                    "use a new run directory"
                )
        else:
            manifest = new_comparison_manifest(
                inputs,
                cells,
                variants=[
                    {
                        "key": configuration.key,
                        "label": configuration.label,
                        "platform": configuration.platform,
                        "profile": configuration.profile,
                        "runtime": configuration.runtime,
                    }
                    for configuration in configurations
                ],
            )
            write_comparison_manifest(root, manifest)
    except ValueError as error:
        raise ValueError(str(error)) from error
    provider = provider_for_environment(environment_config, paths.nanofaas_root)
    requests = {
        role: vm_request_for_role(environment_config, role, loadtest=True)
        for role in ("stack", "loadgen")
    }
    request = requests["stack"]
    provisioning = (
        provisioning_context(scenario_config, environment_config, paths, True)
        if new_run
        else nullcontext()
    )

    def read_target() -> dict[str, object]:
        target = read_comparison_target(provider, requests, kubernetes=False)
        target["facts"] = read_host_facts(provider, requests)
        return target

    with provisioning:
        if new_run:
            # Once, on the fresh stack: it recreates the empty registry outside
            # the budget, which on a resume would discard published images.
            _ = comparison_remote_command(
                provider, request, slice_setup_operation().argv
            )
        target = read_target()
        if new_run:
            manifest.target = target
            (root / "prepare").mkdir(parents=True, exist_ok=True)
            (root / "prepare/target.json").write_text(
                json.dumps(target, indent=2) + "\n"
            )
            write_comparison_manifest(root, manifest)
        else:
            if manifest.target is None:
                raise ValueError("comparison resume requires a recorded target")
            require_comparison_target(manifest.target, target)
            require_recorded_publications(
                manifest=manifest, provider=provider, request=request, root=root
            )
        prepared = prepare(
            source=paths.nanofaas_root,
            profiles=profiles,
            root=root,
            manifest=manifest,
            provider=provider,
            request=request,
        )
        done = experiment_completed(manifest.identity)
        todo = cells if fresh else pending(cells, root, done)
        if len(todo) < len(cells):
            notify(
                f"resuming: {len(cells) - len(todo)} of {len(cells)} cells "
                "already have results"
            )
        function_image = (
            prepared.distributions[EXPERIMENT_02_PUBLISHER]
            .function(*EXPERIMENT_02_FUNCTIONS["qr-code-rust"])
            .image.reference
        )
        stack = Vm(provider, request)
        loadgen = Vm(provider, requests["loadgen"])
        installed: dict[str, dict[str, str]] = {}
        if workloads_root is not None and workloads is not None:
            # Outside the run's owned staging, which a finished run removes: a
            # resume verifies these artifacts instead of rebuilding them.
            workload_dir = PurePosixPath(vm_remote_home(request)) / f"nanolab-e02-{tag}"
            artifacts = ensure_workloads(
                stack,
                workloads=workloads_root,
                remote_root=workload_dir,
                payload=payload,
                png_sha256=protocol.png_sha256,
                record=root / "prepare" / "workloads.json",
            )
            installed = ensure_baselines(
                stack,
                baselines,
                source=workload_dir / WORKLOAD,
                artifacts=artifacts,
                workload_tree=workloads["tree"],
                record=root / "prepare" / "baselines.json",
            )
        # The address the loadgen reaches the stack at, through the ports
        # `secure_release_endpoints` opened to it.
        host = str(provider.connection_host(request))  # type: ignore[attr-defined]
        owned = remote_recipe_root(request, tag)

        def before_attempt() -> None:
            require_matching_inputs(manifest, capture())
            require_comparison_target(target, read_target())

        for index, cell in enumerate(todo, start=1):
            notify(f"[{index}/{len(todo)}] {cell.label}")
            platform = experiment_02_platform(
                cell,
                prepared,
                function_image=function_image,
                installed=installed,
                protocol=protocol,
            )

            def run_attempt(
                attempt_run_dir: Path,
                cell: Experiment02Cell = cell,
                platform: Platform = platform,
            ) -> Outcome:
                return run_cell(
                    cell,
                    stack=stack,
                    loadgen=loadgen,
                    platform=platform,
                    protocol=protocol,
                    payload=payload,
                    script=script,
                    run_dir=attempt_run_dir,
                    remote_root=owned
                    / "cells"
                    / attempt_run_dir.relative_to(root).as_posix(),
                    host=host,
                )

            _ = run_experiment_02_cell(
                cell,
                root=root,
                identity=manifest.identity,
                before_attempt=before_attempt,
                run_attempt=run_attempt,
                notify=notify,
            )
        if prepared.remote_source != owned / "source":
            raise ValueError("Comparison cleanup source is outside its owned staging")
        _ = comparison_remote_command(
            provider, request, ("rm", "-rf", "--", str(owned))
        )
    report = write_experiment_02_report(root, cells, protocol)
    notify(f"matrix complete: {root}")
    notify(f"report: {report}")
