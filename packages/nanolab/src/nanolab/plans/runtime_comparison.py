"""The run that puts control-plane BUILDS side by side.

Everything specific to that question lives here rather than in `loadtest`: which
k6 script drives the load, what the generator is told, and the fact that this is
the one profile whose result cannot be read without per-container CPU and memory.

It is a thin plan on purpose. The platform half — build, push, install the chart,
register the functions — and the load half are already assembled by
`build_loadtest_plan`, and duplicating that to avoid a few arguments would leave
two copies of the same wiring to keep in step. What this module does not share
with the load test is knowledge, not code: `loadtest` no longer contains a branch
that knows this experiment exists.

The variants themselves are not built here. A run compares builds that already
exist in the VM-local registry, produced by
`nanolab.images.control_plane_variants`, and each is passed in through
`prebuilt_control_plane_image` — which is also what makes the comparison honest,
since `platform.py` skips its own build entirely when an image is supplied.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from nanolab.application.functions import resolve_function
from nanolab.comparison.evidence import (
    verify_comparison_cell_registry,
    verify_comparison_scheduler,
)
from nanolab.comparison.profiles import (
    COMPARISON_FUNCTIONS,
    COMPARISON_RECIPE_MODULES,
    COMPARISON_SCHEDULER_STRATEGY,
    PreparedComparison,
)
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.control_plane_variants import resolve_variants
from nanolab.plans.loadtest import build_loadtest_plan
from nanolab.tasks.deployment import DEFAULT_NAMESPACE, LOCAL_REGISTRY
from nanolab.tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher
from nanolab.tasks.platform import Platform
from nanolab.tasks.recipes.kubernetes import RecipeKubernetesImageCheckTask
from nanolab.tasks.recipes.validation import RecipeMetadataCheckTask
from nanolab.tasks.recipes.workflow import RecipeDistribution

SCRIPT_NAME = "runtime-comparison.js"

# The mixed profile is the comparison profile with a different generator: same
# modules, same fixed pair of functions, same absence of a governor. What changes
# is that the load carries both doors and a small share of idempotency keys, which
# is the traffic the platform actually receives and has never been measured under.
MIXED_SCRIPT_NAME = "mixed-workload.js"


def script_for(config: ScenarioConfig) -> str:
    """Return the k6 script that drives this profile's load.

    The `mixed` profile reuses the comparison wiring with a different
    generator, so it is the one profile that does not run `SCRIPT_NAME`.
    """
    return MIXED_SCRIPT_NAME if config.load_profile == "mixed" else SCRIPT_NAME


# The module set every variant is compiled with, and therefore the set the run
# can be asked about. Written out rather than derived from `_additional_modules`:
# that function returns extras for autoscaling and concurrency runs, and this
# profile forbids both, so it would return nothing.
#
# Keep sync-queue outside this experiment: it adds another admission policy
# and selection strategy. The unified SchedulerEngine uses per-function here;
# async-queue is the retained module id for that strategy and async admission.
COMPARISON_MODULES: tuple[str, ...] = (
    "k8s-deployment-provider",
    "async-queue",
)

# The script carries its own k6 scenarios, so no --stage flags. An empty tuple
# rather than None: None means "use the profile's defaults", and those defaults
# are VU counts, which this script's arrival-rate executors would reject as a
# description of the load they are meant to schedule.
NO_STAGES: tuple[tuple[str, int], ...] = ()


def is_runtime_comparison(config: ScenarioConfig) -> bool:
    """Return whether the scenario's load profile is one this module plans."""
    return config.load_profile in ("comparison", "mixed")


def comparison_k6_environment(config: ScenarioConfig) -> Mapping[str, str]:
    """Return what the generator is told beyond the shared load-test environment.

    The pair is named explicitly because `k6_environment` only volunteers a
    neighbour for co-tenancy runs, and co-tenancy is defined by the presence of a
    governor. This profile drives two functions without one, so it would otherwise
    receive the script's own fallback rather than the functions the scenario asked
    for — and the run would silently load a function that does not exist.
    """
    environment = {"NANOFAAS_NEIGHBOUR": config.functions[1]}
    # Passate solo se dichiarate: lo script tiene i suoi valori predefiniti, e
    # scriverli qui vorrebbe dire tenere la definizione del mix in due posti.
    if config.async_share is not None:
        environment["K6_ASYNC_SHARE"] = str(config.async_share)
    if config.idem_share is not None:
        environment["K6_IDEM_SHARE"] = str(config.idem_share)
    return environment


def _variant_image(config: ScenarioConfig) -> str | None:
    """Return the image this run measures, taken from the VM-local registry.

    Passing it as `prebuilt_control_plane_image` is what makes the comparison
    honest rather than merely convenient: `platform.py` skips its own build
    entirely when an image is supplied, so a run measures the artefact the matrix
    compiled and cannot silently rebuild a different one under the same name.
    """
    # An explicit image wins: it is the only way to name a build of another revision,
    # which no variant key can describe.
    if config.control_plane_image is not None:
        return config.control_plane_image
    if config.control_plane_variant is None:
        return None
    variant = resolve_variants((config.control_plane_variant,))[0]
    return variant.image(LOCAL_REGISTRY)


def pinned_functions(
    config: ScenarioConfig,
    *,
    repo_root: Path | None,
    tool_root: Path | None,
) -> dict[str, str]:
    """Return the function images a cell uses, as the prepare phase named them.

    Resolved rather than invented: these are the same tags `platform.py` would
    have produced for itself. Declaring them as prebuilt changes nothing about
    which image runs and everything about when it is built — the platform half
    skips its build tasks, so no cell compiles anything.

    That matters more than the time it saves. `build_images` gates the functions
    and the control plane together, so a cell that was allowed to build its own
    control-plane variant would rebuild the functions too, twelve times over an
    hour, and base-image drift between the first cell and the last would arrive
    as a difference between variants.
    """
    return {
        function.key: function.image
        for function in (
            resolve_function(config, key, source_root=repo_root, tool_root=tool_root)
            for key in config.functions
        )
    }


def build_runtime_comparison_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    control_plane_url: str,
    prometheus_client: PrometheusClient,
    run_dir: Path,
    remote_run_dir: Path | None = None,
    remote_repo_root: Path | None = None,
    fetcher: RemoteFileFetcher | None = None,
    repo_root: Path | None = None,
    tool_root: Path | None = None,
    prebuilt_control_plane_image: str | None = None,
    prebuilt_function_images: Mapping[str, str] | None = None,
    prepared: PreparedComparison | None = None,
) -> Workflow:
    """Compile one variant's run of the comparison into a Sonata workflow."""
    if not is_runtime_comparison(config):
        raise ValueError(
            "runtime-comparison plan requires loadProfile: comparison or mixed"
        )
    if prepared is not None:
        if (
            config.backend != "k8s"
            or config.control_plane_variant is None
            or config.control_plane_variant not in prepared.distributions
        ):
            raise ValueError(
                "prepared comparison requires a selected Kubernetes variant"
            )
        selected = prepared.distributions[config.control_plane_variant]
        baseline = prepared.distributions["jvm"]
        prebuilt_control_plane_image = selected.control_plane().image.reference
        prebuilt_function_images = {
            key: baseline.function(*COMPARISON_FUNCTIONS[key]).image.reference
            for key in config.functions
        }
        remote_repo_root = Path(prepared.remote_source)
        before_load = _prepared_checks(
            config, selected, baseline, bindings, run_dir, repo_root, tool_root
        )
    else:
        before_load = None
    image = prebuilt_control_plane_image or _variant_image(config)
    if image is None:
        raise ValueError(
            "a comparison run needs a control-plane build to measure: set "
            "controlPlaneVariant, or pass a prebuilt image"
        )
    return build_loadtest_plan(
        config,
        environment,
        bindings,
        control_plane_url=control_plane_url,
        prometheus_client=prometheus_client,
        run_dir=run_dir,
        remote_run_dir=remote_run_dir,
        remote_repo_root=remote_repo_root,
        fetcher=fetcher,
        repo_root=repo_root,
        tool_root=tool_root,
        stages=NO_STAGES,
        prebuilt_control_plane_image=image,
        # Pinned for the Kubernetes matrix, where the cells share one VM registry that
        # the matrix populated beforehand and a per-cell rebuild would let base-image
        # drift arrive as a difference between variants. NOT pinned on the container
        # backend: there each run creates its own empty registry, and the control plane
        # validates a function image by pulling from it, so pinning without pushing
        # leaves that registry empty and every registration fails. Building them per run
        # from one checkout gives the same images across arms, which is what a
        # comparison needs.
        prebuilt_function_images=(
            prebuilt_function_images
            if prebuilt_function_images is not None
            # What the scenario named, if it named anything; otherwise built here.
            else (
                (dict(config.function_images) or None)
                if config.backend == "container"
                else pinned_functions(config, repo_root=repo_root, tool_root=tool_root)
            )
        ),
        script_name=script_for(config),
        k6_env_overrides=comparison_k6_environment(config),
        # The only profile that needs them, and it cannot be read without them:
        # a natively compiled control plane publishes no JVM memory gauges, so
        # cAdvisor is the one source that prices every build on the same terms.
        # Kubernetes only. There the kubelet already exports cAdvisor and it is the
        # right source. On the container backend it is not: this repository tried it
        # and rejected it on evidence — under Docker Desktop it enumerates only the
        # cgroup roots and reports nothing per container (see tasks/loadtest/
        # resources.py). That backend is priced from the Docker Engine API instead,
        # by the resource watcher, which is the same data `docker stats` reads.
        container_metrics=config.backend != "container",
        observed_modules=tuple(sorted(COMPARISON_RECIPE_MODULES))
        if prepared is not None
        else COMPARISON_MODULES,
        before_load=before_load,
        helm_values_overrides={
            "controlPlane.scheduler.strategy": COMPARISON_SCHEDULER_STRATEGY
        }
        if prepared is not None
        else None,
        # No reaching back before the load started: every cell redeploys the
        # control plane, so anything before k6 belongs to the build the previous
        # cell was measuring. That is not hypothetical — two cells reported the
        # same `jvm_gc_pause_seconds` for a JVM build and a native one, and the
        # native one does not publish the metric at all.
        snapshot_lead_seconds=0.0,
        # Not required here, and it is not a lowered standard: the G1 build
        # publishes no JVM gauges at all, because SubstrateVM registers no MXBeans
        # under that collector. The guard moves to the container memory series
        # below, which cAdvisor reports for every build alike — which is the whole
        # reason this profile collects it.
        heap_metrics_required=False,
        function_concurrency=2,
        function_queue_size=20,
    )


def _prepared_checks(
    config: ScenarioConfig,
    selected: RecipeDistribution,
    baseline: RecipeDistribution,
    bindings: RoleBindings,
    run_dir: Path,
    repo_root: Path | None,
    tool_root: Path | None,
) -> Callable[[Platform], Resource[None]]:
    def before_load(platform: Platform) -> Resource[None]:
        executor = RoleBoundCommandTaskExecutor(bindings)
        selected_resource = Resource(
            title="Prepared selected comparison distribution",
            acquire=lambda _inputs: selected,
            release=lambda _inputs, _value: None,
        )
        baseline_resource = Resource(
            title="Prepared shared comparison distribution",
            acquire=lambda _inputs: baseline,
            release=lambda _inputs, _value: None,
        )

        def acquire(inputs: TaskInputs) -> None:
            verify_comparison_cell_registry(
                distributions=(selected, baseline)
                if selected is not baseline
                else (selected,),
                executor=executor,
                inputs=inputs,
                run_dir=run_dir,
            )
            _ = RecipeMetadataCheckTask(
                selected_resource,
                executor=executor,
                run_dir=run_dir,
                endpoint=platform.endpoint,
                role="stack",
            ).run(inputs)
            checks = [
                (
                    selected_resource,
                    "nanofaas-control-plane",
                    ("control-plane", "control-plane", "java"),
                    run_dir / "images/control-plane",
                )
            ]
            for key in config.functions:
                function = resolve_function(
                    config, key, source_root=repo_root, tool_root=tool_root
                )
                name, sdk = COMPARISON_FUNCTIONS[key]
                checks.append(
                    (
                        baseline_resource,
                        f"fn-{function.name}",
                        ("function", name, sdk),
                        run_dir / "images" / sdk,
                    )
                )
            for distribution, deployment, component, evidence_dir in checks:
                _ = RecipeKubernetesImageCheckTask(
                    distribution,
                    namespace=DEFAULT_NAMESPACE,
                    deployment=deployment,
                    component=component,
                    executor=executor,
                    role="stack",
                    run_dir=evidence_dir,
                ).run(inputs)
            verify_comparison_scheduler(
                endpoint=platform.endpoint,
                executor=executor,
                inputs=inputs,
                run_dir=run_dir,
            )

        return Resource(
            title="Verify prepared comparison before load",
            acquire=acquire,
            release=lambda _inputs, _value: None,
            requires=(
                *platform.resources,
                *platform.functions,
                selected_resource,
                baseline_resource,
            ),
        )

    return before_load
