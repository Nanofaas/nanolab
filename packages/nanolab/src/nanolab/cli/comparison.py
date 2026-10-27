"""The `compare` command: build every variant once, then run the matrix.

Two phases, and the split is the point. The prepare phase compiles the function
images and every control-plane build into the VM-local registry; the matrix phase
runs cells that build nothing at all. A cell that could build would rebuild its
functions too — `platform.py` gates both behind one flag — and twelve rebuilds
spread over an hour would let base-image drift arrive as a difference between
variants.

Provisioning is held for the whole matrix (`keep=True`), so twelve cells share
one cluster and one registry. Tearing down between cells would make every cell
pay for a fresh k3s and would also destroy the images the prepare phase built.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from traceback import format_exception_only
from uuid import uuid4

import typer
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.application.functions import resolve_function
from nanolab.application.vm_provider import (
    provider_for_environment,
    vm_request_for_role,
)
from nanolab.cli.catalogue import environment_input, scenario_input
from nanolab.cli.progress import ConsoleProgressSink
from nanolab.comparison.evidence import require_recorded_publications
from nanolab.comparison.experiment_01 import (
    DIAGNOSTICS,
    LOAD_STARTED,
    Outcome,
    archive_cell_result,
    attempt_dir,
    classify_failure,
    experiment_01_identity,
    experiment_completed,
    next_attempt,
    write_cell_result,
    write_experiment_01_report,
)
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
from nanolab.comparison.matrix import (
    ComparisonCell,
    build_experiment_01_matrix,
    build_matrix,
    pending,
)
from nanolab.comparison.prepare import (
    comparison_remote_command,
    leftover_cleanup_operations,
    prepare_comparison,
    stage_comparison,
)
from nanolab.comparison.profiles import (
    COMPARISON_FUNCTIONS,
    EXPERIMENT_01_CONTRACT,
    EXPERIMENT_01_FUNCTIONS,
    EXPERIMENT_02_CONTRACT,
    EXPERIMENT_02_FUNCTIONS,
    EXPERIMENT_02_MODULES,
    PreparedComparison,
    comparison_profiles,
    experiment_01_profiles,
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
from nanolab.images.control_plane_variants import (
    EXPERIMENT_01_VARIANTS_BY_KEY,
    VARIANTS_BY_KEY,
    resolve_variants,
)
from nanolab.plans.runtime_comparison import EXPERIMENT_01_SCRIPT_NAME
from nanolab.tasks.deployment import DEFAULT_NAMESPACE
from nanolab.tasks.experiment_02.baselines import (
    Fn,
    FunLess,
    Serverledge,
    Spin,
    TinyFaas,
    install_baselines,
)
from nanolab.tasks.experiment_02.budget import slice_setup_operation
from nanolab.tasks.experiment_02.cell import CLEANUP_ERRORS, run_cell
from nanolab.tasks.experiment_02.platforms import (
    DirectReference,
    NanofaasPlatform,
    Platform,
)
from nanolab.tasks.experiment_02.vm import Vm
from nanolab.tasks.experiment_02.workload import (
    WORKLOAD,
    WORKLOADS_ENV,
    capture_workloads,
    ensure_workloads,
)
from nanolab.tasks.loadtest.comparison_report import WriteComparisonReport
from nanolab.tasks.recipes.remote import remote_recipe_root
from nanolab.tasks.vm.models import VmRequest, vm_remote_home
from nanolab.workspace.paths import bundled_assets_root

DEFAULT_VARIANTS = tuple(VARIANTS_BY_KEY)
EXPERIMENT_01_DEFAULT_VARIANTS = tuple(EXPERIMENT_01_VARIANTS_BY_KEY)
EXPERIMENT_02_DEFAULT_CONFIGURATIONS = tuple(CONFIGURATIONS_BY_KEY)
# Experiment 02 runs experiment 01's calendar, so it runs experiment 01's script.
EXPERIMENT_02_SCRIPT_NAME = "experiment-01.js"
_EXPERIMENT_02_REASONS: dict[str, str] = {
    "measured": "completed",
    "oom": "OOM kill in the measured slice",
    "startup_failed": "no correct response before the readiness timeout",
}


HEARTBEAT_SECONDS = 60.0


@contextmanager
def _heartbeat(summary: str, interval: float = HEARTBEAT_SECONDS) -> Iterator[None]:
    """Say the build is still alive, because nothing else will.

    A native compile takes twenty minutes and prints nothing while it runs. The
    obvious fix — binding a workflow sink so `SubprocessShell._emit_output`
    forwards the command's output — does not work: `ConsoleProgressSink.emit`
    returns early for any event without a `task_id`, so log lines are routed into
    a sink that discards them. Command output is never shown by this CLI, for
    cells or for prepare.

    So the elapsed time is the signal. It cannot distinguish "compiling" from
    "wedged", but it does distinguish both from "the process died", which is the
    question that had to be answered by reading load average off the VM.
    """
    done = threading.Event()

    def tick() -> None:
        waited = 0.0
        while not done.wait(interval):
            waited += interval
            typer.echo(f"prepare: {summary} — still running, {waited / 60:.0f}m")

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=1.0)


def _run_prepare(
    *,
    source: Path,
    profiles: Mapping[str, Path],
    root: Path,
    manifest: ComparisonManifest,
    provider: VmCommandProvider,
    request: VmRequest,
) -> PreparedComparison:
    """Stage one source and publish verified recipes with progress and heartbeat."""
    with bind_workflow_sink(ConsoleProgressSink(log_lines=True)), _heartbeat("prepare"):
        stage = stage_comparison(
            source=source,
            profiles=profiles,
            root=root,
            tag=str(manifest.identity["tag"]),
            provider=provider,
            request=request,
            shared=next(iter(profiles)),
        )
        return prepare_comparison(
            stage=stage,
            manifest=manifest,
            root=root,
            provider=provider,
            request=request,
        )


# One retry, not more. A cell is safe to re-run — it has produced no summary
# yet, so a second attempt yields a whole valid cell rather than a mixture — and
# the failure it exists for is a dropped connection, which recurs at a rate a
# single retry covers: two k6 runs out of eight died with paramiko's "channel
# closed without an exit status" over an eight-minute command.
#
# A second failure is not more of the same. The SDK already keepalives the
# transport, so a connection that dies twice in a row is not a blip and the
# matrix should stop rather than grind through ten more cells producing nothing.
CELL_ATTEMPTS = 2


def _run_cell(
    cell: ComparisonCell,
    *,
    scenario: Path,
    scenario_config: ScenarioConfig,
    environment: Path,
    environment_config: EnvironmentConfig,
    paths: object,
    root: Path,
    prepared: PreparedComparison,
    before_attempt: Callable[[], None] | None = None,
) -> None:
    """Run one cell, retrying once if the attempt dies rather than fails."""
    for attempt in range(1, CELL_ATTEMPTS + 1):
        if before_attempt is not None:
            before_attempt()
        try:
            _run_attempt(
                cell,
                run_dir=cell.run_dir(root),
                scenario=scenario,
                scenario_config=scenario_config,
                environment=environment,
                environment_config=environment_config,
                paths=paths,
                prepared=prepared,
            )
            return
        except Exception as error:
            if attempt == CELL_ATTEMPTS:
                raise
            typer.echo(f"  {cell.label} failed ({error}); retrying once")


def _run_attempt(
    cell: ComparisonCell,
    *,
    run_dir: Path,
    scenario: Path,
    scenario_config: ScenarioConfig,
    environment: Path,
    environment_config: EnvironmentConfig,
    paths: object,
    prepared: PreparedComparison,
) -> None:
    """Execute one cell's workflow into `run_dir`, building nothing."""
    from nanolab.cli.product import _execute_workflow, _prepare_run

    cell_dir = run_dir
    lifetime = ExitStack()
    try:
        sink, *_rest = _prepare_run(
            lifetime,
            release=False,
            scenario=scenario,
            environment=environment,
            release_config=None,
            run_dir=cell_dir,
            resume=False,
            environment_config=environment_config,
            paths=paths,  # type: ignore[arg-type]
            effective_run_dir=cell_dir,
        )
        _execute_workflow(
            sink=sink,
            scenario_config=cell_scenario(scenario_config, cell),
            environment_config=environment_config,
            paths=paths,  # type: ignore[arg-type]
            keep=True,
            control_plane_url=None,
            prometheus_url=None,
            effective_run_dir=cell_dir,
            only=None,
            start=None,
            until=None,
            scenario=scenario,
            release_request=None,
            release_provider=None,
            release_journal=None,
            resume=False,
            prepared_comparison=prepared,
            provision=False,
        )
    finally:
        lifetime.close()


def _pods(provider: VmCommandProvider, request: VmRequest) -> str:
    return comparison_remote_command(
        provider,
        request,
        (
            "sudo",
            "k3s",
            "kubectl",
            "get",
            "pods",
            "-n",
            DEFAULT_NAMESPACE,
            "-o",
            "json",
        ),
    )


def _run_experiment_cell(
    cell: ComparisonCell,
    *,
    root: Path,
    identity: Mapping[str, object],
    provider: VmCommandProvider,
    request: VmRequest,
    before_attempt: Callable[[], None],
    run_attempt: Callable[[Path], None],
) -> str:
    """Run one experiment 01 cell to a definitive outcome, or stop the matrix.

    Every attempt has its own directory. OOM and failed startup are results:
    they are committed with their diagnostics and never retried. One retry is
    allowed only for a transport failure before load, with the control plane
    verified running and ready. Anything else stops with its partial evidence;
    the next resume starts a new attempt on a clean deployment.
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
        try:
            run_attempt(run_dir)
        except Exception as error:
            diagnostics = run_dir / DIAGNOSTICS
            diagnostics.mkdir(exist_ok=True)
            (diagnostics / "error.txt").write_text(
                "".join(format_exception_only(error))
            )
            try:
                listing = _pods(provider, request)
            except Exception as probe:  # the probe failing is itself evidence
                (diagnostics / "pods-error.txt").write_text(f"{probe}\n")
                raise error from None
            (diagnostics / "pods.json").write_text(listing)
            failure = classify_failure(
                json.loads(listing), load_started=(run_dir / LOAD_STARTED).is_file()
            )
            if failure in ("oom", "startup_failed"):
                write_cell_result(
                    cell,
                    root,
                    identity=identity,
                    attempt=attempt,
                    outcome=failure,
                    reason=str(error),
                    started_at=started,
                )
                typer.echo(f"  {cell.label}: {failure} (attempt {attempt})")
                return failure
            if failure == "transport" and not transport_retried:
                transport_retried = True
                typer.echo(f"  {cell.label} transport failure before load; retrying")
                continue
            raise
        # A run that finished can still have lost its control plane to the OOM
        # killer and restarted it mid-load: that is an OOM, measured or not.
        listing = _pods(provider, request)
        (run_dir / DIAGNOSTICS).mkdir(exist_ok=True)
        (run_dir / DIAGNOSTICS / "pods-after-load.json").write_text(listing)
        outcome = (
            "oom"
            if classify_failure(json.loads(listing), load_started=True) == "oom"
            else "measured"
        )
        write_cell_result(
            cell,
            root,
            identity=identity,
            attempt=attempt,
            outcome=outcome,
            reason="completed" if outcome == "measured" else "OOMKilled during load",
            started_at=started,
        )
        return outcome


def cell_scenario(config: ScenarioConfig, cell: ComparisonCell) -> ScenarioConfig:
    """Return one cell's scenario: the base with the build it measures named.

    A copy rather than a mutation, because the base is read once and every cell
    must differ from it in exactly one field.
    """
    return config.model_copy(update={"control_plane_variant": cell.variant.key})


def _run_experiment_02_cell(
    cell: Experiment02Cell,
    *,
    root: Path,
    identity: Mapping[str, object],
    before_attempt: Callable[[], None],
    run_attempt: Callable[[Path], Outcome],
) -> Outcome:
    """Run one experiment 02 cell to a definitive outcome, or stop the matrix.

    Each attempt has its own directory and an outcome commits a receipt. A
    refusal (`ValueError`: a budget not enforced, a process outside it) stops
    at once. Any other failure before the load is taken as transport and
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
        except ValueError:
            raise
        except Exception as error:
            diagnostics = run_dir / DIAGNOSTICS
            diagnostics.mkdir(exist_ok=True)
            (diagnostics / "error.txt").write_text(
                "".join(format_exception_only(error))
            )
            if (
                (run_dir / LOAD_STARTED).is_file()
                or transport_retried
                or cleanup_errors.is_file()
            ):
                raise
            transport_retried = True
            typer.echo(f"  {cell.label} failed before load ({error}); retrying once")
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
        typer.echo(f"  {cell.label}: {outcome} (attempt {attempt})")
        if cleanup_errors.is_file():
            raise RuntimeError(
                f"{cell.label}: cleanup failed, see {cleanup_errors}; "
                "stopping before the next cell"
            )
        return outcome


def _experiment_02_platform(
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


def _compare_experiment_02(  # NOSONAR (S3776): one sequential matrix run
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
) -> None:
    """Run experiment 02: complete platforms on Docker under budgeted cgroups.

    The shape is experiment 01's: one manifest, one provisioning, one prepare,
    then the cells in the protocol's seeded order, each with its receipt. What
    differs is the stack (Docker and cgroup slices, no k3s) and the cell, which
    `tasks.experiment_02.cell.run_cell` drives directly on the VMs.
    """
    from nanolab.cli.product import (  # local: the router imports this module
        _environment,
        _provisioning_context,
        _scenario,
        default_tool_paths,
    )

    protocol = scenario_config.experiment_02
    if protocol is None:
        raise typer.BadParameter("experiment-02 requires its experiment02 block")
    if environment_config.provider != "azure" or set(environment_config.roles) != {
        "stack",
        "loadgen",
    }:
        raise typer.BadParameter(
            "experiment-02 runs on Azure with exactly the stack and loadgen roles"
        )
    if (
        scenario_config.control_plane_image
        or scenario_config.function_images
        or scenario_config.recipe_profile
    ):
        raise typer.BadParameter(
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
        experiment = experiment_02_identity(
            protocol,
            cells,
            payload=payload,
            script=script,
            selected=selected,
            seed=protocol.order_seed,
            workloads=None
            if workloads_root is None
            else capture_workloads(workloads_root),
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
            ).strip()
        )

        def capture() -> dict[str, object]:
            return capture_comparison_inputs(
                scenario=_scenario(scenario),
                environment=_environment(environment),
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
        raise typer.BadParameter(str(error)) from error
    provider = provider_for_environment(environment_config, paths.nanofaas_root)
    requests = {
        role: vm_request_for_role(environment_config, role, loadtest=True)
        for role in ("stack", "loadgen")
    }
    request = requests["stack"]
    provisioning = (
        _provisioning_context(scenario_config, environment_config, paths, True)
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
        prepared = _run_prepare(
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
            typer.echo(
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
        if workloads_root is not None:
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
            installed = install_baselines(
                stack, baselines, source=workload_dir / WORKLOAD, artifacts=artifacts
            )
            record = root / "prepare" / "baselines.json"
            if record.is_file() and json.loads(record.read_text()) != installed:
                raise RuntimeError(
                    f"the baselines' installation changed since {record}; "
                    "start a new run directory"
                )
            record.write_text(json.dumps(installed, indent=2) + "\n")
        # The address the loadgen reaches the stack at, through the ports
        # `secure_release_endpoints` opened to it.
        host = str(provider.connection_host(request))  # type: ignore[attr-defined]
        owned = remote_recipe_root(request, tag)

        def before_attempt() -> None:
            require_matching_inputs(manifest, capture())
            require_comparison_target(target, read_target())

        for index, cell in enumerate(todo, start=1):
            typer.echo(f"[{index}/{len(todo)}] {cell.label}")
            platform = _experiment_02_platform(
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

            _ = _run_experiment_02_cell(
                cell,
                root=root,
                identity=manifest.identity,
                before_attempt=before_attempt,
                run_attempt=run_attempt,
            )
        if prepared.remote_source != owned / "source":
            raise ValueError("Comparison cleanup source is outside its owned staging")
        _ = comparison_remote_command(
            provider, request, ("rm", "-rf", "--", str(owned))
        )
    report = write_experiment_02_report(root, cells, protocol)
    typer.echo(f"matrix complete: {root}")
    typer.echo(f"report: {report}")


def register(app: typer.Typer) -> None:
    """Register the `compare` command on `app`."""

    @app.command("compare")
    # Typer's documented API takes the parameter spec as the default, and each
    # call builds a fresh object that is not shared between invocations, so
    # B008's mutable-default concern does not apply.
    def compare_command(
        scenario: Path = typer.Argument(..., callback=scenario_input),  # noqa: B008
        environment: Path = typer.Option(  # noqa: B008
            ..., "--environment", callback=environment_input
        ),
        repetitions: int = typer.Option(
            3,
            "--repetitions",
            help="Runs per variant. Three is the smallest number that shows spread.",
        ),
        variants: str = typer.Option(
            ",".join(DEFAULT_VARIANTS),
            "--variants",
            help="Comma-separated control-plane builds to compare.",
        ),
        run_dir: Path | None = typer.Option(None, "--run-dir"),  # noqa: B008
        fresh: bool = typer.Option(
            False,
            "--fresh",
            help=(
                "Re-run cells that already have results. By default a matrix "
                "resumes: an interruption should not cost the hours of correct "
                "cells already on disk."
            ),
        ),
        native_build_memory: str | None = typer.Option(
            None,
            "--native-build-memory",
            help=(
                "Heap for the native-image BUILDER, e.g. 6g. It sizes its own heap "
                "from the machine's total memory and cannot see what else is "
                "running, so on a shared VM it gets OOM-killed. Unset means unbounded."
            ),
        ),
        native_parallelism: int | None = typer.Option(
            None,
            "--native-parallelism",
            help="native-image workers. Fewer cost wall-clock and save peak memory.",
        ),
    ) -> None:
        """Compare control-plane builds under one varying load."""
        from nanolab.cli.product import (  # local: the router imports this module
            _environment,
            _provisioning_context,
            _scenario,
            default_tool_paths,
        )

        scenario_config = _scenario(scenario)
        environment_config = _environment(environment)
        if scenario_config.load_profile == "experiment-02":
            if native_parallelism is not None and native_parallelism < 1:
                raise typer.BadParameter("native parallelism must be positive")
            chosen = tuple(part.strip() for part in variants.split(",") if part.strip())
            _compare_experiment_02(
                scenario=scenario,
                scenario_config=scenario_config,
                environment=environment,
                environment_config=environment_config,
                selected=(
                    EXPERIMENT_02_DEFAULT_CONFIGURATIONS
                    if chosen == DEFAULT_VARIANTS
                    else chosen
                ),
                repetitions=repetitions,
                run_dir=run_dir,
                fresh=fresh,
                native_build_memory=native_build_memory,
                native_parallelism=native_parallelism,
            )
            return
        experiment = scenario_config.load_profile == "experiment-01"
        if (
            scenario_config.workflow != "loadtest"
            or scenario_config.backend != "k8s"
            or scenario_config.load_profile
            not in ("comparison", "mixed", "experiment-01")
        ):
            raise typer.BadParameter(
                "compare requires a Kubernetes comparison, mixed or "
                "experiment-01 loadtest"
            )
        if environment_config.provider == "local":
            raise typer.BadParameter("compare requires a VM environment")
        if experiment and (
            environment_config.provider != "azure"
            or set(environment_config.roles) != {"stack", "loadgen"}
        ):
            raise typer.BadParameter(
                "experiment-01 runs on Azure with exactly the stack and loadgen roles"
            )
        expected_functions = (
            EXPERIMENT_01_FUNCTIONS if experiment else COMPARISON_FUNCTIONS
        )
        if (
            set(scenario_config.functions) != set(expected_functions)
            or scenario_config.control_plane_image
            or scenario_config.function_images
            or scenario_config.recipe_profile
        ):
            raise typer.BadParameter(
                "compare requires the profile's own functions "
                "without prebuilt image overrides"
            )
        if repetitions < 1 or (
            native_parallelism is not None and native_parallelism < 1
        ):
            raise typer.BadParameter(
                "repetitions and native parallelism must be positive"
            )
        selected = tuple(part.strip() for part in variants.split(",") if part.strip())
        if experiment and selected == DEFAULT_VARIANTS:
            selected = EXPERIMENT_01_DEFAULT_VARIANTS
        paths = default_tool_paths()
        protocol = scenario_config.experiment_01
        try:
            if protocol is not None:
                profiles = experiment_01_profiles(paths.tool_root, selected)
                cells = build_experiment_01_matrix(
                    resolve_variants(selected), repetitions
                )
                experiment_identity: dict[str, object] | None = experiment_01_identity(
                    protocol,
                    cells,
                    payload=experiment_01_payload(
                        protocol, paths.tool_root / "scenarios" / "payloads"
                    ),
                    script=bundled_assets_root() / "k6" / EXPERIMENT_01_SCRIPT_NAME,
                    selected=selected,
                )
            else:
                profiles = comparison_profiles(paths.tool_root, selected)
                cells = build_matrix(resolve_variants(selected), repetitions)
                experiment_identity = None
            root = (run_dir or paths.runs_dir / "comparison").resolve()
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
                ).strip()
            )

            def capture() -> dict[str, object]:
                return capture_comparison_inputs(
                    scenario=_scenario(scenario),
                    environment=_environment(environment),
                    nanofaas_root=paths.nanofaas_root,
                    nanolab_root=nanolab_root,
                    profiles=profiles,
                    variants=selected,
                    repetitions=repetitions,
                    tag=tag,
                    build_memory=native_build_memory,
                    parallelism=native_parallelism,
                    experiment=experiment_identity,
                    **(
                        {"profile_options": EXPERIMENT_01_CONTRACT.options}
                        if experiment
                        else {}
                    ),
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
                manifest = new_comparison_manifest(inputs, cells)
                write_comparison_manifest(root, manifest)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        provider = provider_for_environment(environment_config, paths.nanofaas_root)
        requests = {
            role: vm_request_for_role(environment_config, role, loadtest=True)
            for role in ("stack", "loadgen")
        }
        request = requests["stack"]
        provisioning = (
            _provisioning_context(scenario_config, environment_config, paths, True)
            if new_run
            else nullcontext()
        )

        def read_target() -> dict[str, object]:
            target = read_comparison_target(provider, requests)
            if experiment:
                target["facts"] = read_host_facts(provider, requests)
            return target

        with provisioning:
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
            prepared = _run_prepare(
                source=paths.nanofaas_root,
                profiles=profiles,
                root=root,
                manifest=manifest,
                provider=provider,
                request=request,
            )
            done = experiment_completed(manifest.identity) if experiment else None
            todo = (
                cells
                if fresh
                else pending(cells, root, done)
                if done is not None
                else pending(cells, root)
            )
            if len(todo) < len(cells):
                typer.echo(
                    f"resuming: {len(cells) - len(todo)} of {len(cells)} cells "
                    "already have results"
                )

            def before_attempt() -> None:
                require_matching_inputs(manifest, capture())
                require_comparison_target(target, read_target())
                names = [
                    resolve_function(
                        scenario_config,
                        key,
                        source_root=paths.nanofaas_root,
                        tool_root=paths.tool_root,
                    ).name
                    for key in scenario_config.functions
                ]
                for operation in leftover_cleanup_operations(names):
                    _ = comparison_remote_command(provider, request, operation.argv)

            for index, cell in enumerate(todo, start=1):
                typer.echo(f"[{index}/{len(todo)}] {cell.label}")
                if experiment:

                    def run_attempt(run_dir: Path, cell: ComparisonCell = cell) -> None:
                        _run_attempt(
                            cell,
                            run_dir=run_dir,
                            scenario=scenario,
                            scenario_config=scenario_config,
                            environment=environment,
                            environment_config=environment_config,
                            paths=paths,
                            prepared=prepared,
                        )

                    _ = _run_experiment_cell(
                        cell,
                        root=root,
                        identity=manifest.identity,
                        provider=provider,
                        request=request,
                        before_attempt=before_attempt,
                        run_attempt=run_attempt,
                    )
                    continue
                _run_cell(
                    cell,
                    scenario=scenario,
                    scenario_config=scenario_config,
                    environment=environment,
                    environment_config=environment_config,
                    paths=paths,
                    root=root,
                    prepared=prepared,
                    before_attempt=before_attempt,
                )
            owned = remote_recipe_root(request, tag)
            if prepared.remote_source != owned / "source":
                raise ValueError(
                    "Comparison cleanup source is outside its owned staging"
                )
            _ = comparison_remote_command(
                provider, request, ("rm", "-rf", "--", str(owned))
            )
        if protocol is not None:
            report = write_experiment_01_report(root, cells, protocol)
        else:
            report = WriteComparisonReport(
                task_id="", title="Control-plane build comparison", root=root
            ).run()
        typer.echo(f"matrix complete: {root}")
        typer.echo(f"report: {report}")
