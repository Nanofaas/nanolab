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
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

import typer
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.cli.progress import ConsoleProgressSink
from nanolab.cli.vm_provider import provider_for_environment, vm_request_for_role
from nanolab.comparison.evidence import require_recorded_publications
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
    PreparedComparison,
    comparison_profiles,
)
from nanolab.comparison.target import read_comparison_target, require_comparison_target
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.control_plane_variants import VARIANTS_BY_KEY, resolve_variants
from nanolab.plans.functions import resolve_function
from nanolab.tasks.loadtest.comparison_report import WriteComparisonReport
from nanolab.tasks.recipe_remote import remote_recipe_root
from nanolab.tasks.vm.models import VmRequest

DEFAULT_VARIANTS = tuple(VARIANTS_BY_KEY)


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
    from nanolab.cli.product import _execute_workflow, _prepare_run

    cell_dir = cell.run_dir(root)
    for attempt in range(1, CELL_ATTEMPTS + 1):
        if before_attempt is not None:
            before_attempt()
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
            return
        except Exception as error:
            if attempt == CELL_ATTEMPTS:
                raise
            typer.echo(f"  {cell.label} failed ({error}); retrying once")
        finally:
            lifetime.close()


def cell_scenario(config: ScenarioConfig, cell: ComparisonCell) -> ScenarioConfig:
    """Return one cell's scenario: the base with the build it measures named.

    A copy rather than a mutation, because the base is read once and every cell
    must differ from it in exactly one field.
    """
    return config.model_copy(update={"control_plane_variant": cell.variant.key})


def register(app: typer.Typer) -> None:
    """Register the `compare` command on `app`."""

    @app.command("compare")
    # Typer's documented API takes the parameter spec as the default, and each
    # call builds a fresh object that is not shared between invocations, so
    # B008's mutable-default concern does not apply.
    def compare_command(
        scenario: Path = typer.Argument(..., exists=True),  # noqa: B008
        environment: Path = typer.Option(..., "--environment", exists=True),  # noqa: B008
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
        if (
            scenario_config.workflow != "loadtest"
            or scenario_config.backend != "k8s"
            or scenario_config.load_profile not in ("comparison", "mixed")
        ):
            raise typer.BadParameter(
                "compare requires a Kubernetes comparison or mixed loadtest"
            )
        if environment_config.provider == "local":
            raise typer.BadParameter("compare requires a VM environment")
        if (
            set(scenario_config.functions) != set(COMPARISON_FUNCTIONS)
            or scenario_config.control_plane_image
            or scenario_config.function_images
            or scenario_config.recipe_profile
        ):
            raise typer.BadParameter(
                "compare requires the Java/JavaScript function pair "
                "without prebuilt image overrides"
            )
        if repetitions < 1 or (
            native_parallelism is not None and native_parallelism < 1
        ):
            raise typer.BadParameter(
                "repetitions and native parallelism must be positive"
            )
        selected = tuple(part.strip() for part in variants.split(",") if part.strip())
        paths = default_tool_paths()
        try:
            profiles = comparison_profiles(paths.tool_root, selected)
            cells = build_matrix(resolve_variants(selected), repetitions)
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
        with provisioning:
            target = read_comparison_target(provider, requests)
            if new_run:
                manifest.target = target
                (root / "prepare").mkdir(parents=True, exist_ok=True)
                (root / "prepare/target.json").write_text(
                    json.dumps(target, indent=2) + "\n"
                )
                write_comparison_manifest(root, manifest)
            else:
                assert manifest.target is not None
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
            todo = cells if fresh else pending(cells, root)
            if len(todo) < len(cells):
                typer.echo(
                    f"resuming: {len(cells) - len(todo)} of {len(cells)} cells "
                    "already have results"
                )

            def before_attempt() -> None:
                require_matching_inputs(manifest, capture())
                require_comparison_target(
                    target, read_comparison_target(provider, requests)
                )
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
        report = WriteComparisonReport(
            task_id="", title="Control-plane build comparison", root=root
        ).run()
        typer.echo(f"matrix complete: {root}")
        typer.echo(f"report: {report}")
