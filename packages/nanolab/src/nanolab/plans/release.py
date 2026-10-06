"""Compile a release scenario into a Sonata workflow.

The release pipeline is a linear DAG built from the Task and Resource
primitives defined in sonata-tasks. Each phase that iterates over image cells
is a composite Steps node so the workflow surface stays coarse-grained and
selectable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import yaml
from sonata_engine import JournalConfig, Verifier, Workflow
from sonata_tasks.buildx import buildx_builder_resource
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor
from sonata_tasks.registry_tunnel import registry_tunnel_resource

from nanolab.cli.execution import build_role_bindings
from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.plan import DEFAULT_REGISTRY, ImagePlan, build_image_plan
from nanolab.plans.release_phases import (
    build_arm64_phase,
    build_attestation_phase,
    build_benchmark_phase,
    build_publication_phase,
    build_recipe_assembly_phase,
    build_registry_push_phase,
    build_regression_phase,
    build_source_test_phase,
)
from nanolab.release import arm as release_arm
from nanolab.release import publish as release_publish
from nanolab.release.build import extract_commit_tree
from nanolab.release.environment import validate_release_environment
from nanolab.release.evidence import release_evidence_verifiers
from nanolab.release.model import (
    CredentialFiles,
    ReleaseIdentity,
    ReleaseSettings,
    digest_path,
    git_state,
)
from nanolab.release.recipe import ReleaseRecipeGroup, prepare_release_recipe_groups
from nanolab.release.recipe_execution import capture_release_inventory
from nanolab.release.resources import (
    build_release_resources,
    preserve_release_builder_selection,
    release_execution_guard,
    release_recipe_inputs_resource,
)
from nanolab.release.tasks import (
    ReleasePhaseTask,
    versioned_release_run_dir,
)
from nanolab.release.versioning import normalize_version, verify_version_consistency
from nanolab.tasks.provisioning.providers import provider_for


def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return value


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    """Everything needed to compile a release workflow."""

    repo_root: Path
    version: str
    environment: EnvironmentConfig
    scenario: ScenarioConfig
    image_plan: ImagePlan
    settings: ReleaseSettings
    run_dir: Path
    performance_root: Path
    source_tree: Path
    credentials: CredentialFiles | None = None
    nanofaas_root: Path | None = None  # defaults to repo_root
    identity: ReleaseIdentity | None = None
    recipe_groups: tuple[ReleaseRecipeGroup, ...] = ()
    arm_image_plan: ImagePlan | None = None
    arm_recipe_groups: tuple[ReleaseRecipeGroup, ...] = ()
    source_archive: Path | None = None
    archive_digest: str = ""
    inventory_file: Path | None = None


def release_verifiers(request: ReleaseRequest, provider: Any) -> dict[str, Verifier]:
    """Route local image proof by frozen matrices and registry proof via stack."""
    stack = vm_request_for_role(request.environment, "stack", loadtest=True)
    arm = vm_request_for_role(request.environment, "arm-builder")
    image_requests: dict[str, object] = {}
    for plan, architecture, owner in (
        (request.image_plan, "amd64", stack),
        (request.arm_image_plan, "arm64", arm),
    ):
        if plan is None or not plan.cells:
            raise ValueError("Release verifiers require both frozen image matrices")
        for cell in plan.cells:
            if cell.architecture != architecture:
                raise ValueError("Release verifier matrix architecture differs")
            reference = "docker-daemon:" + cell.image
            if reference in image_requests:
                raise ValueError(
                    "Ambiguous or duplicate release verifier matrix reference"
                )
            image_requests[reference] = owner
    return release_evidence_verifiers(
        provider, stack, local_image_requests=image_requests,
        credentials=request.credentials,
    )


def release_journal_config(request: ReleaseRequest) -> JournalConfig:
    """Use one Sonata journal per prepared release version."""
    return JournalConfig(
        versioned_release_run_dir(
            request.run_dir, normalize_version(request.version)[0]
        )
        / "sonata.jsonl"
    )


def build_release_request(
    *,
    repo_root: Path,
    nanofaas_root: Path,
    scenario_path: Path,
    environment_path: Path,
    release_config_path: Path | None,
    run_dir: Path,
    performance_root: Path,
    source_tree: Path,
    executable: bool = False,
) -> ReleaseRequest:
    """Validate local release inputs without constructing cloud infrastructure."""
    tool_root = Path(repo_root).expanduser().resolve()
    source_root = Path(nanofaas_root).expanduser().resolve()
    scenario_file = Path(scenario_path).expanduser().resolve()
    environment_file = Path(environment_path).expanduser().resolve()
    scenario = ScenarioConfig.model_validate(_read_yaml(scenario_file))
    if scenario.workflow != "release" or scenario.release is None:
        raise ValueError("release preflight requires a release scenario")

    release = scenario.release
    policy = (
        release.profile,
        release.benchmark_scenario,
        release.throughput_max_loss_percent,
        release.p95_max_increase_percent,
        release.error_rate_max,
    )
    if policy != (
        "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1",
        "autoscaling-cycle-k8s.yaml",
        10.0,
        15.0,
        0.30,
    ):
        raise ValueError(
            "release scenario does not match the canonical Azure performance policy"
        )
    benchmark_scenario = scenario_file.parent / release.benchmark_scenario
    if not benchmark_scenario.is_file():
        raise ValueError("release benchmark scenario must be a file")

    environment = EnvironmentConfig.model_validate(_read_yaml(environment_file))
    plain_version, version_tag = normalize_version(release.version)
    source_commit = _release_source_commit(source_root, plain_version)
    # Plan from the commit, never from the checkout: ignored build output and
    # untracked files must not be able to add cells the archive cannot build.
    source_archive = Path(source_tree) / "source.tar"
    planning_root = extract_commit_tree(
        source_root,
        source_commit,
        Path(source_tree) / "source",
        archive_destination=source_archive,
    )
    validate_release_environment(environment, source_root, plain_version)

    credentials = None
    if release_config_path is not None:
        credential_config = _read_yaml(Path(release_config_path).expanduser().resolve())
        try:
            credentials = CredentialFiles(
                ghcr_token=Path(str(credential_config["ghcr_token_file"]))
                .expanduser()
                .absolute(),
                cosign_key=Path(str(credential_config["cosign_key_file"]))
                .expanduser()
                .absolute(),
                cosign_password=Path(str(credential_config["cosign_password_file"]))
                .expanduser()
                .absolute(),
            )
        except KeyError as error:
            raise ValueError(
                "release credential configuration is incomplete"
            ) from error
        credentials.validate(repo_root=tool_root).validate(repo_root=source_root)
    elif executable:
        raise ValueError("release credential config is required for execution")

    image_plan = build_image_plan(
        planning_root,
        version_tag,
        registry=DEFAULT_REGISTRY,
        architectures=("amd64",),
    )
    if not image_plan.cells:
        raise ValueError("release image matrix must not be empty")
    recipe_groups = prepare_release_recipe_groups(
        planning_root, image_plan, profiles_root=tool_root / "recipes"
    )
    arm_image_plan = build_image_plan(
        planning_root,
        version_tag,
        registry=image_plan.registry,
        architectures=("arm64",),
    )
    arm_recipe_groups = prepare_release_recipe_groups(
        planning_root,
        arm_image_plan,
        profiles_root=tool_root / "recipes",
        architecture="arm64",
    )

    after = git_state(source_root)
    if not after.clean or after.commit != source_commit:
        raise ValueError("Release source changed during preflight archive capture")
    archive_digest = digest_path(source_archive)
    inventory_file = capture_release_inventory(
        planning_root, Path(source_tree) / "source-inventory.json"
    )

    settings = ReleaseSettings(
        max_parallelism=release.max_parallelism,
        scenario=benchmark_scenario,
        scenario_name=release.benchmark_scenario,
        benchmark_runs=release.benchmark_runs,
        profile=release.profile,
        throughput_max_loss_percent=release.throughput_max_loss_percent,
        p95_max_increase_percent=release.p95_max_increase_percent,
        error_rate_max=release.error_rate_max,
    )
    return ReleaseRequest(
        repo_root=tool_root,
        nanofaas_root=source_root,
        version=release.version,
        environment=environment,
        scenario=scenario,
        image_plan=image_plan,
        settings=settings,
        run_dir=Path(run_dir).expanduser().resolve(),
        performance_root=Path(performance_root).expanduser().resolve(),
        source_tree=planning_root,
        credentials=credentials,
        recipe_groups=recipe_groups,
        arm_image_plan=arm_image_plan,
        arm_recipe_groups=arm_recipe_groups,
        source_archive=source_archive,
        archive_digest=archive_digest,
        inventory_file=inventory_file,
        identity=ReleaseIdentity(
            source_commit=source_commit,
            prepared_version=plain_version,
            release_config_digest=digest_path(scenario_file),
            environment_digest=digest_path(environment_file),
        ),
    )


def build_release_workflow(
    request: ReleaseRequest,
    *,
    provider: Any = None,
) -> Workflow:
    """Compile a ReleaseRequest into a linear Sonata release workflow.

    All top-level nodes are returned so the caller can add them to any
    workflow, slice them with ``select``, or embed them in a larger plan.
    """
    env = request.environment
    if env.provider != "azure":
        raise ValueError("release workflow requires an Azure environment")

    nanofaas = request.nanofaas_root or request.repo_root
    execution_guard = release_execution_guard(
        request.credentials, repo_roots=(request.repo_root, nanofaas)
    )
    source_commit = (
        request.identity.source_commit
        if request.identity is not None
        else _release_source_commit(nanofaas, request.version)
    )
    if request.identity is None:
        raise ValueError("release workflow requires a validated release identity")
    identity = request.identity
    _, expected_image_tag = normalize_version(request.version)
    if request.image_plan.version != expected_image_tag:
        raise ValueError(
            "release image plan version does not match the requested project version"
        )

    if (
        not request.recipe_groups
        or request.arm_image_plan is None
        or not request.arm_recipe_groups
        or request.source_archive is None
        or request.inventory_file is None
    ):
        raise ValueError(
            "Release requires frozen recipes, source archive and inventory"
        )
    if digest_path(request.source_archive) != request.archive_digest:
        raise ValueError("Frozen release source archive changed")
    if tuple(g.flavor for g in request.recipe_groups) != ("jvm", "native", "default"):
        raise ValueError("Release requires all three recipe groups")
    grouped_cells = tuple(c for g in request.recipe_groups for c in g.cells)
    if sorted(grouped_cells, key=lambda c: c.image) != sorted(
        request.image_plan.cells, key=lambda c: c.image
    ):
        raise ValueError("Frozen recipe image matrix changed")
    if any(
        g.profile_digest != "sha256:" + hashlib.sha256(g.profile_bytes).hexdigest()
        for g in request.recipe_groups
    ):
        raise ValueError("Frozen recipe bytes changed")

    arm_plan = request.arm_image_plan
    if (
        arm_plan.version != expected_image_tag
        or arm_plan.registry != request.image_plan.registry
        or not arm_plan.cells
        or any(c.architecture != "arm64" for c in arm_plan.cells)
    ):
        raise ValueError("Frozen ARM recipe matrix does not match release")
    arm_groups = request.arm_recipe_groups
    if tuple(g.flavor for g in arm_groups) != ("jvm", "native", "default"):
        raise ValueError("Release requires all three frozen ARM recipe groups")
    if sorted(
        (c for g in arm_groups for c in g.cells), key=lambda c: c.image
    ) != sorted(arm_plan.cells, key=lambda c: c.image):
        raise ValueError("Frozen ARM recipe image matrix changed")
    if any(
        g.profile_digest != "sha256:" + hashlib.sha256(g.profile_bytes).hexdigest()
        for g in arm_groups
    ):
        raise ValueError("Frozen ARM recipe bytes changed")

    if provider is None:
        provider = provider_for(vm_request_for_role(env, "stack"), request.repo_root)
    infrastructure = build_release_resources(
        env, nanofaas, provider, requires=(execution_guard,)
    )
    stack_req = vm_request_for_role(env, "stack", loadtest=True)
    _ = vm_request_for_role(
        env, "loadgen", loadtest=True
    )  # captured by build_role_bindings
    arm_req = vm_request_for_role(env, "arm-builder")
    remote_root = f"/home/azureuser/nanofaas-release/{request.version}"
    source_dir = f"{remote_root}/source"
    # A release does not sync the checkout to the project directory the way
    # every other workflow does: it stages one immutable tree per version, and
    # the benchmark's platform commands run inside that tree. Without this the
    # translation sends them to the project directory, which a release VM never
    # populates -- the load test then dies on its first `cd`.
    bindings, fetcher = build_role_bindings(
        env,
        vm_provider=provider,
        repo_root=nanofaas,
        remote_project_root=source_dir,
    )
    executor = RoleBoundCommandTaskExecutor(bindings)
    release_dir = versioned_release_run_dir(request.run_dir, identity.prepared_version)

    wf = Workflow(workflow_id=f"release-{request.version}")

    # --- Phase 1: Source Tests ---
    sources, source_tests = build_source_test_phase(
        identity=identity,
        run_dir=request.run_dir,
        release_dir=release_dir,
        nanofaas=nanofaas,
        source_commit=source_commit,
        remote_root=remote_root,
        source_dir=source_dir,
        provider=provider,
        stack_request=stack_req,
        arm_request=arm_req,
        infrastructure=infrastructure,
        executor=executor,
        source_archive=request.source_archive,
        archive_digest=request.archive_digest,
        inventory_file=request.inventory_file,
    )

    # --- Phase 2: AMD64 Build ---
    amd64_inputs = release_recipe_inputs_resource(
        groups=request.recipe_groups,
        max_parallelism=request.settings.max_parallelism,
        run_dir=release_dir / "recipe-inputs/amd64",
        remote_root=remote_root,
        provider=provider,
        request=stack_req,
        requires=(infrastructure.stack,),
    )
    amd64_builder_name = f"release-amd64-{request.version}"
    amd64_builder = buildx_builder_resource(
        name=amd64_builder_name,
        executor=executor,
        role="stack",
        requires=(infrastructure.stack, amd64_inputs),
        buildkitd_config=f"{remote_root}/recipe-inputs/amd64/buildkitd-amd64.toml",
        driver_options=("default-load=true",),
        replace_existing=True,
    )
    amd64_builder = preserve_release_builder_selection(
        amd64_builder, executor=executor, name=amd64_builder_name
    )
    release_images, amd64_build = build_recipe_assembly_phase(
        architecture="amd64",
        role="stack",
        identity=identity,
        run_dir=request.run_dir,
        image_plan=request.image_plan,
        max_parallelism=request.settings.max_parallelism,
        builder_name=amd64_builder_name,
        remote_root=remote_root,
        source_dir=source_dir,
        executor=executor,
        prerequisite_phases=(source_tests,),
        recipe_groups=request.recipe_groups,
        provider=provider,
        request=stack_req,
        inventory_file=request.inventory_file,
        archive_digest=request.archive_digest,
    )

    # --- Phase 3: Registry Push ---
    registry_push = build_registry_push_phase(
        architecture="amd64",
        role="stack",
        identity=identity,
        run_dir=request.run_dir,
        image_plan=request.image_plan,
        release_images=release_images,
        executor=executor,
        prerequisite_phases=(source_tests,),
        assembly=amd64_build,
    )

    # --- Phases 4-6: Benchmarks ---
    benchmark_scenario = _read_yaml(request.settings.scenario)
    benchmark_config = ScenarioConfig.model_validate(benchmark_scenario)
    benchmark_plan = cast(
        ReleaseRequest,
        replace(
            request,
            repo_root=Path(source_dir),
            scenario=benchmark_config,
            run_dir=versioned_release_run_dir(
                request.run_dir, identity.prepared_version
            ),
        ),
    )
    benchmark_runs: tuple[ReleasePhaseTask, ...] = build_benchmark_phase(
        identity=identity,
        run_dir=request.run_dir,
        benchmark_plan=benchmark_plan,
        runs=request.settings.benchmark_runs,
        scenario=request.settings.scenario,
        release_images=release_images,
        registry_push=registry_push,
        bindings=bindings,
        fetcher=fetcher,
        endpoints=infrastructure.endpoints,
    )

    # --- Phases 7-8: Aggregate and Regression Gate ---
    regression_tasks: tuple[ReleasePhaseTask, ReleasePhaseTask] = (
        build_regression_phase(
            identity=identity,
            run_dir=request.run_dir,
            benchmark_plan=benchmark_plan,
            runs=request.settings.benchmark_runs,
            benchmark_runs=benchmark_runs,
        )
    )
    aggregate, reg_gate = regression_tasks

    # --- Phase 9: ARM64 Build ---
    arm_plan = request.arm_image_plan
    arm_inputs = release_recipe_inputs_resource(
        groups=request.arm_recipe_groups,
        max_parallelism=request.settings.max_parallelism,
        run_dir=release_dir / "recipe-inputs/arm64",
        remote_root=remote_root,
        provider=provider,
        request=arm_req,
        architecture="arm64",
        requires=(infrastructure.arm_builder,),
    )
    tunnel = registry_tunnel_resource(
        registry_upstream=lambda: provider.connection_host(stack_req),
        provider=provider,
        request=arm_req,
        unit_name="nanofaas-registry-tunnel",
        listen_port=5000,
        upstream_port=5000,
        title="Acquire registry tunnel to <release-stack>:5000",
        requires=(infrastructure.stack, infrastructure.arm_builder),
    )
    arm64_builder = buildx_builder_resource(
        name=f"release-arm64-{request.version}",
        executor=executor,
        role="arm-builder",
        requires=(infrastructure.arm_builder, arm_inputs),
        buildkitd_config=f"{remote_root}/recipe-inputs/arm64/buildkitd-arm64.toml",
        driver_options=("default-load=true",),
        validate=release_arm.require_arm64_builder,
        validation_key="nanolab.release.arm64-builder:v1",
        replace_existing=True,
    )
    arm64_builder = preserve_release_builder_selection(
        arm64_builder,
        executor=executor,
        name=f"release-arm64-{request.version}",
        role="arm-builder",
    )
    arm_runtime_plan, arm_images, arm64_build, arm64_push, arm64_smoke = (
        build_arm64_phase(
            request=request,
            executor=executor,
            identity=identity,
            release_dir=release_dir,
            nanofaas=nanofaas,
            arm_plan=arm_plan,
            remote_root=remote_root,
            source_dir=source_dir,
            provider=provider,
            arm_request=arm_req,
            reg_gate=reg_gate,
            source_tests=source_tests,
        )
    )

    # --- Publish, attest, and finalize ---
    pub_plan = release_publish.build_publish_plan(
        request.source_tree,
        request.version,
        local_registry=request.image_plan.registry,
    )
    publication = build_publication_phase(
        request=request,
        identity=identity,
        release_dir=release_dir,
        provider=provider,
        stack_request=stack_req,
        infrastructure=infrastructure,
        release_images=release_images,
        registry_push=registry_push,
        reg_gate=reg_gate,
        arm64_push=arm64_push,
        arm64_smoke=arm64_smoke,
        arm_images=arm_images,
        arm_runtime_plan=arm_runtime_plan,
        pub_plan=pub_plan,
    )
    ghcr = publication.ghcr
    cosign = publication.cosign
    publish_architectures = publication.publish_architectures
    publish_manifests = publication.publish_manifests
    publish_aliases = publication.publish_aliases
    publication_receipts = publication.publication_receipts
    all_published = publication.all_published
    docker_credentials = publication.docker_credentials

    attestation_tasks: tuple[ReleasePhaseTask, ReleasePhaseTask] = (
        build_attestation_phase(
            request=request,
            identity=identity,
            release_dir=release_dir,
            remote_root=remote_root,
            provider=provider,
            stack_request=stack_req,
            executor=executor,
            benchmark_plan=benchmark_plan,
            aggregate=aggregate,
            cosign=cosign,
            pub_plan=pub_plan,
            publication_receipts=publication_receipts,
            all_published=all_published,
            docker_credentials=docker_credentials,
        )
    )
    attest, finalize = attestation_tasks

    # --- Wire the DAG ---
    # Order of wf.add() defines execution order. requires only lists Resource
    # objects that need acquire/release spliced around their consumers.
    wf.add(  # pyright: ignore[reportArgumentType]
        source_tests,
        requires=(infrastructure.stack, sources.stack),
    )
    wf.add(  # pyright: ignore[reportArgumentType]
        amd64_build,
        requires=(
            infrastructure.stack,
            sources.stack,
            amd64_inputs,
            amd64_builder,
        ),
    )
    wf.add(registry_push, requires=(infrastructure.stack,))  # pyright: ignore[reportArgumentType]
    for benchmark in benchmark_runs:
        wf.add(  # pyright: ignore[reportArgumentType]
            benchmark,
            requires=(
                infrastructure.stack,
                infrastructure.loadgen,
                infrastructure.endpoints,
                # benchmark_plan sets repo_root to the staged source on the stack
                # VM, so the benchmarks are consumers of it. Without this, Sonata
                # splices the source's release after the AMD64 build and every
                # benchmark dies on `cd: .../source: No such file or directory`.
                sources.stack,
            ),
        )
    wf.add(aggregate)  # pyright: ignore[reportArgumentType]
    wf.add(reg_gate)  # pyright: ignore[reportArgumentType]
    wf.add(  # pyright: ignore[reportArgumentType]
        arm64_build,
        requires=(
            infrastructure.stack,
            infrastructure.arm_builder,
            arm64_builder,
            sources.arm,
            arm_inputs,
        ),
    )
    wf.add(  # pyright: ignore[reportArgumentType]
        arm64_push,
        requires=(infrastructure.stack, infrastructure.arm_builder, tunnel),
    )
    # ARM64 gets a smoke phase and AMD64 does not, on purpose: the three
    # benchmark runs exercise every AMD64 image from the local registry and
    # fail if one does not start, so AMD64 is smoke-tested by the loadtest.
    # Nothing runs the ARM64 images otherwise, so they need their own.
    wf.add(  # pyright: ignore[reportArgumentType]
        arm64_smoke,
        requires=(infrastructure.stack, infrastructure.arm_builder, tunnel),
    )
    publish_requires = (infrastructure.stack,) + ((ghcr,) if ghcr is not None else ())
    wf.add(publish_architectures, requires=publish_requires)  # pyright: ignore[reportArgumentType]
    wf.add(publish_manifests, requires=publish_requires)  # pyright: ignore[reportArgumentType]
    wf.add(publish_aliases, requires=publish_requires)  # pyright: ignore[reportArgumentType]
    attest_requires = publish_requires + ((cosign,) if cosign is not None else ())
    wf.add(attest, requires=attest_requires)  # pyright: ignore[reportArgumentType]
    wf.add(finalize)  # pyright: ignore[reportArgumentType]

    return wf


def _release_source_commit(repo_root: Path, requested_version: str) -> str:
    """Return the immutable source commit for an already prepared release."""
    source = git_state(repo_root)
    if not source.clean:
        raise ValueError("release requires a clean nanoFaaS Git tree")
    requested_plain, _ = normalize_version(requested_version)
    prepared = verify_version_consistency(repo_root)
    if prepared != requested_plain:
        raise ValueError(
            f"requested release {requested_plain} does not match "
            f"the committed nanoFaaS project version {prepared}"
        )
    return source.commit
