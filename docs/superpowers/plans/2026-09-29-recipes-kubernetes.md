# Recipe-backed Kubernetes Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the current session (the user's preserved execution choice). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run one recipe-backed Kubernetes lifecycle scenario on host Minikube by default, or build and publish in the VM when Multipass is explicitly selected.

**Architecture:** Keep one scenario/profile and share report validation, Helm deployment, registration and lifecycle assertions. Local execution assembles in an isolated host checkout and loads images into Minikube; Multipass transfers that captured source and builds in an isolated VM directory. Kubernetes resources, endpoints and image evidence are explicit workflow dependencies.

**Tech Stack:** Python 3.12+, Pydantic, Sonata task/resource APIs already installed, Gradle recipes v2, Docker, Minikube, Helm, kubectl, k6, existing Multipass provider.

**Spec:** `docs/superpowers/specs/2026-09-28-recipes-kubernetes-design.md` (reviewed 2026-09-29).

## Global Constraints

- One scenario: `packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml`; one profile: `packages/nanolab/recipes/validate-k8s-jvm.yaml`.
- `local`: running Minikube with Docker driver; host `assembleRecipe`, then `minikube image load --daemon`. No VM provisioning or registry publication.
- Explicit `multipass`: VM `publishRecipe`, VM-local registry, no host registry tunnel. Reject other providers for this recipe path before provisioning.
- Profile modules: `k8s-deployment-provider`, `build-metadata`, `sync-queue`; Java JVM word-stats; registry repository `127.0.0.1:5000/nanofaas`; variant `recipe-v2-k8s-jvm`.
- Set profile `nanofaas.k8s.image-pull-policy` and Helm `controlPlane.image.pullPolicy` to `IfNotPresent`.
- Preserve existing container recipe and non-recipe behavior. No NanoFaaS plugin or Sonata dependency changes.
- Build recipe components and queue probe from captured staged sources, never the original NanoFaaS checkout. Keep host run evidence after cleanup.
- Preserve queue invocation and k6 burst checks; keep the auxiliary warm-echo probe outside the recipe, with a unique run tag.
- Planning/dry-run performs no commands, source staging or infrastructure mutations. Execution pins the selected Minikube profile/context.
- Own namespace `nanofaas-recipe-<run-token>` and run-tagged images; preserve unrelated cluster resources.
- Before implementation, isolate work following the worktree skill and carry required existing uncommitted NanoLab recipe/CI changes explicitly. Do not silently start from HEAD and lose them. Follow applicable repository impact-analysis rules before symbol edits and commit checks before committing.

## Review Focus

1. A tag resolves to different bytes: reconcile runtime config/manifest identities with the report (Tasks 1, 6).
2. Source paths contain spaces, tracked deletions or executable changes: identical host/VM inputs and no host-source writes (Tasks 2, 4).
3. Minikube context, driver or architecture differs: preflight fails before builds and cleanup never targets another cluster (Task 3).
4. Partial image loading, API forwarding or Helm acquisition fails: compensate owned resources in reverse order (Tasks 3, 5).
5. VM Gradle fails after writing a plausible report: fetch diagnostics, retain original failure, never deploy (Task 4).

## File Map

Source paths starting with `tasks/`, `workspace/`, `plans/`, `config/` or `cli/` are relative to `packages/nanolab/src/nanolab/`. Paths starting with `tests/`, `recipes/` or `scenarios-v2/` are relative to `packages/nanolab/`. Root documentation and CI paths are relative to the NanoLab repository. Run commands from that repository root.

- Extend `tasks/recipe.py`, `workspace/recipe.py`: stage resource, shared command/report contracts, assembly resource, backend-required modules, Compose-optional binding.
- Create `tasks/recipe_remote.py`: VM staging, publication, report/log fetching and success cleanup.
- Create `tasks/recipe_kubernetes.py`: Minikube preflight/image resources, owned namespace, API forwarding, Kubernetes image evidence.
- Extend `tasks/recipe_validation.py`: dynamic endpoint and role support for registration/metadata; preserve Docker image checks.
- Extend `tasks/platform.py`, `tasks/validate.py`, `plans/validate.py`, `tasks/components/helm.py`: dynamic Helm image, probe build/delivery, provider-specific graph and shared validation checks.
- Extend `config/scenario.py`, `cli/product.py`, `cli/provisioning.py` only as needed for early provider checks and remote finalization; preserve TUI entry paths.
- Tests: extend `tests/tasks/test_recipe.py`, `tests/workspace/test_recipe.py`, `tests/tasks/test_recipe_validation.py`, `tests/config/test_recipe.py`, `tests/plans/test_recipe_validate.py`; create `tests/tasks/test_recipe_remote.py`, `tests/tasks/test_recipe_kubernetes.py` and `tests/tasks/fixtures/recipe_kubernetes/`.
- Add profile; update existing scenario, recipe README, root README, roadmap and `.github/workflows/ci.yml`.

## Task 1: Establish the image identity contract

**Files:** create `tasks/recipe_kubernetes.py`, `tests/tasks/test_recipe_kubernetes.py`, `tests/tasks/fixtures/recipe_kubernetes/`.

**Interfaces:** `RuntimeImageIdentity(config_digest: str, manifest_digests: tuple[str, ...])`; `verify_runtime_image(expected: RecipeImage, actual: RuntimeImageIdentity) -> None`. Runtime inspection adapters return this record only after resolving the actual Pod image; configuration IDs and manifest digests remain distinct.

- [ ] Capture a minimal runtime sample during execution: inspect host Docker image ID, the image after loading into the explicitly selected Minikube profile, Pod `imageID`, node CRI inspection and relevant manifest/config descriptors. Use an isolated disposable image/Pod and remove only those resources. Record tool/runtime versions and sanitized JSON fixtures. Obtain equivalent k3s fixtures from an existing explicit Multipass environment or a disposable characterization run; record unavailable infrastructure as a blocked runtime verification, never as a passed check. Do not assume that removing `containerd://` yields the report's image ID.
- [ ] Write failing tests `test_config_and_manifest_digests_are_distinct`, `test_same_reference_different_config_fails`, `test_published_manifest_must_resolve_to_expected_config`, `test_unknown_runtime_identity_fails`. Assertions: config digest equals `RecipeImage.id`; published images additionally match `RecipeImage.digest`; missing/ambiguous relationships fail.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/test_recipe_kubernetes.py --no-cov`; expect the missing verification behavior to fail.
- [ ] Implement the pure identity verifier using captured fixture schemas. Resolve a runtime manifest ID to its configuration digest through node runtime inspection/content, not through the current mutable tag. Support the runtime observed in the target Minikube and k3s fixtures; fail clearly for an unrecognized representation.
- [ ] Rerun the same tests; require PASS before image-check integration. Keep fixtures and verifier in one commit when implementation commits are made.

## Task 2: Share staging and the two distribution contracts

**Files:** `tasks/recipe.py`, `workspace/recipe.py`, `tests/tasks/test_recipe.py`, `tests/workspace/test_recipe.py`.

**Interfaces:**
- `recipe_run_resource(*, source: Path, recipe: Path, run_dir: Path, tag: str, requires: tuple[Resource[Any], ...] = ()) -> Resource[RecipeRun]` stages only at acquisition.
- `assembled_recipe_distribution_resource(*, run: Resource[RecipeRun], executor: CommandTaskExecutor, functions: tuple[tuple[str, str], ...], required_modules: frozenset[str], requires: tuple[Resource[Any], ...] = ()) -> Resource[RecipeDistribution]` uses `AssembleRecipeTask`.
- Extend `require_validation_distribution(..., required_modules: frozenset[str] = frozenset({'build-metadata', 'container-deployment-provider'})) -> None`; preserve exact component/no-service/local-repository checks.
- Share `recipe_command(target: Literal['assembleRecipe', 'publishRecipe'], *, recipe: str, output: str, tag: str) -> tuple[str, ...]` between host tasks and remote execution. Keep command construction argv-based.
- `RecipeBinding` retains distribution, function mapping and run directory; its Compose project becomes optional, validated as present by the existing container path. Preserve existing keyword call sites.

- [ ] Add failing tests for host assembly returning `built` without publication digest, publication still requiring `published` plus digest, K8s required-module validation, missing/extra function rejection, lazy staging and argv with spaces.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/test_recipe.py packages/nanolab/tests/workspace/test_recipe.py --no-cov`; require new cases to fail for missing behavior.
- [ ] Implement the interfaces above using `prepare_recipe_run` and `read_distribution`. Preserve the existing published container resource as a compatibility wrapper. Remove stale output before a new build attempt and validate staged provenance against the report before exposing it.
- [ ] Rerun those tests plus `tests/plans/test_recipe_validate.py`; require PASS. Verify the current container plan still schedules one publication and no duplicate builds.

## Task 3: Own Minikube image delivery and the run namespace

**Files:** `tasks/recipe_kubernetes.py`, `tests/tasks/test_recipe_kubernetes.py`.

**Interfaces:**
- `MinikubeTarget(profile: str, context: str, nodes: tuple[str, ...])` is immutable.
- `minikube_target_resource(*, executor: CommandTaskExecutor) -> Resource[MinikubeTarget]` performs preflight at acquisition.
- `minikube_images_resource(*, target: Resource[MinikubeTarget], distribution: Resource[RecipeDistribution], probe_image: Resource[str], executor: CommandTaskExecutor, run_dir: Path) -> Resource[tuple[str, ...]]` loads and records exactly the recipe and probe references.
- `recipe_namespace_resource(*, namespace: str, executor: CommandTaskExecutor, role: ExecutionRole, target: Resource[MinikubeTarget] | None, requires: tuple[Resource[Any], ...]) -> Resource[str]` creates and deletes only its namespace.

- [ ] Add failing tests for a non-Minikube context (`test_non_minikube_context_fails_before_build`), stopped cluster, VM driver, missing k6 and incompatible host/node architecture. Assert all commands pin the verified profile/context, and planning performs no calls.
- [ ] Add `test_partial_load_compensates_only_acquired_images`, `test_preexisting_reference_is_not_claimed`, `test_namespace_collision_fails_without_deleting_it`. Record two loaded references, fail the next load and prove reverse cleanup; do not remove an unowned reference.
- [ ] Run the Task 1 test command and confirm the new cases fail.
- [ ] Implement these resources with existing compensation primitives. Before loading, verify host Docker IDs still match the report. Reject preexisting run references; track attempted loads so partial acquisition can clean up. At release, wait for the run's Pods to disappear before image removal. Make local namespace acquisition depend on image delivery and Helm depend on namespace acquisition, so reverse release uninstalls Helm, deletes the namespace and then removes imported images.
- [ ] Rerun tests and require PASS. Verify fake executors see neither `minikube start/delete` nor registry publication.

## Task 4: Stage and publish inside explicit Multipass

**Files:** create `tasks/recipe_remote.py`, `tests/tasks/test_recipe_remote.py`; extend `cli/product.py` for finalization within the existing provisioning context.

**Interfaces:**
- `RemoteRecipeRun(local: RecipeRun, root: PurePosixPath)` exposes remote source/profile/output paths under one owned root.
- `remote_recipe_run_resource(*, run: Resource[RecipeRun], provider: VmCommandProvider, request: VmRequest, run_dir: Path, tag: str, requires: tuple[Resource[Any], ...]) -> Resource[RemoteRecipeRun]` transfers verified inputs; its ordinary release retains diagnostic state.
- `remote_recipe_distribution_resource(*, run: Resource[RemoteRecipeRun], provider: VmCommandProvider, request: VmRequest, functions: tuple[tuple[str, str], ...], required_modules: frozenset[str]) -> Resource[RecipeDistribution]` runs the shared publish argv remotely, fetches outputs and parses them on the host.
- `cleanup_remote_recipe_run(run: RemoteRecipeRun, *, provider: VmCommandProvider, request: VmRequest) -> None` removes only the owned root after the workflow and evidence collection succeed, before VM teardown.

- [ ] Write failing tests for preserved tracked edits/deletions/executable bits and spaces, remote digest verification, absent credentials/alternates, VM-only Gradle execution, failed transfer, fetch failure, Gradle failure with a stale valid-looking report, and report source/hash mismatch.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/test_recipe_remote.py --no-cov`; expect new behavior to fail.
- [ ] Implement a source bundle from the already captured stage, including self-contained Git metadata needed for revision/dirty provenance. Use the existing provider `transfer_to`, `exec_argv`, and fetch interface; inspect their actual signatures before wiring. Verify the bundle and extracted tracked inputs remotely. Pass remote paths directly to VM commands, without the host-root cwd translator.
- [ ] Reuse `recipe_command('publishRecipe', ...)` and host `read_distribution(..., published=True)` after fetching the report. Always attempt log/partial-report fetch on failure; preserve the build failure as primary and never return a usable distribution for nonzero Gradle exit.
- [ ] Call success cleanup only after workflow success, within the VM provisioning lifetime. Failed runs retain host source/provenance/log evidence even if normal VM teardown deletes the VM; a retained VM keeps its stage. Test both successful cleanup and failure retention.
- [ ] Rerun tests and require PASS. Assert no host Gradle/Docker build was issued by the Multipass path.

## Task 5: Deploy the report images and keep the queue probe working

**Files:** `tasks/platform.py`, `tasks/validate.py`, `tasks/recipe_validation.py`, `tasks/recipe_kubernetes.py`, `tasks/components/helm.py`; tests in `tests/tasks/test_recipe_validation.py`, `tests/tasks/test_recipe_kubernetes.py`, `tests/tasks/migrated/components/test_helm.py`, `tests/tasks/migrated/test_validate_workflow.py`.

**Interfaces:**
- Extend `RecipeFunctionRegisterTask.endpoint` and `RecipeMetadataCheckTask.endpoint` to existing `Endpoint`; metadata gains `role: ExecutionRole = 'host'`. Resolve through `endpoint_argv`, preserving resource dependencies.
- Extend `control_plane_helm_values(..., image_pull_policy: str = 'Always') -> dict[str, str]` so only the recipe caller chooses `IfNotPresent`.
- `kubernetes_api_endpoint_resource(*, release: Resource[str], namespace: str, target: Resource[MinikubeTarget], executor: CommandTaskExecutor, run_dir: Path) -> Resource[str]` owns a host loopback API port-forward and returns its URL.
- Add `queue_probe_image_resource(*, run: Resource[RecipeRun], tag: str, executor: CommandTaskExecutor, remote_run: Resource[RemoteRecipeRun] | None = None, provider: VmCommandProvider | None = None, vm_request: VmRequest | None = None) -> Resource[str]`. Local builds in the stage; remote builds/pushes in the VM stage. Return `127.0.0.1:5000/nanofaas/java-warm-echo:<tag>`; no probe image belongs to the recipe report.

- [ ] Write failing tests where report images differ from defaults: Helm and recipe registration must consume report references; namespace/callback values match the run namespace; all chart/probe build paths use the captured stage. A recipe-bound `PlatformRequest(build_images=False)` must be valid without a static control-plane image, while non-recipe validation remains strict.
- [ ] Add tests proving the queue probe builds exactly once, uses ordinary registration and never indexes `binding.functions`; the local path loads it with zero Docker pushes, the VM path builds/pushes it remotely. Preserve the probe warmup invocation and k6 burst.
- [ ] Add endpoint tests: a resource-returned URL reaches register/metadata/invoke/k6; Multipass uses role `stack`; the local port-forward owns a loopback ephemeral port, waits for readiness, handles child exit/timeouts and terminates its process on partial acquisition or later failure.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/test_recipe_validation.py packages/nanolab/tests/tasks/test_recipe_kubernetes.py packages/nanolab/tests/tasks/migrated/components/test_helm.py packages/nanolab/tests/tasks/migrated/test_validate_workflow.py --no-cov`; confirm new cases fail.
- [ ] Resolve the recipe control-plane image inside Helm acquisition and build the spec there, preserving uninstall compensation. Add the explicit probe resource before deployment; disable duplicate legacy builds. Bind only functions present in the recipe mapping. Use the staged chart via resource-resolved cwd locally and remote paths in Multipass.
- [ ] Local endpoint acquisition runs `kubectl --context <context> -n <namespace> port-forward --address 127.0.0.1 service/control-plane 0:8080`, retains its log and extracts the assigned port after readiness. Reuse existing subprocess lifecycle helpers where their contracts fit. Multipass keeps its Service ClusterIP endpoint, executed inside the VM.
- [ ] Rerun tests; require PASS. Assert reverse teardown: functions/Pods, API forwarding, Helm, namespace, imported images, retained source/evidence.

## Task 6: Wire one scenario to both providers and verify runtime identity

**Files:** `plans/validate.py`, `config/scenario.py`, `cli/product.py`, `cli/provisioning.py`, `tasks/validate.py`, `tasks/recipe_kubernetes.py`; `tests/plans/test_recipe_validate.py`, `tests/config/test_recipe.py`, `tests/tasks/test_recipe_kubernetes.py`, relevant CLI/TUI tests.

**Interfaces:** `RecipeKubernetesImageCheckTask(distribution: Resource[RecipeDistribution], *, namespace: str, deployment: str, component: tuple[str, str, str], executor: CommandTaskExecutor, role: ExecutionRole, run_dir: Path, target: Resource[MinikubeTarget] | None = None)` uses Task 1 identity verification. `require_recipe_environment(config: ScenarioConfig, environment: EnvironmentConfig) -> None` enforces supported combinations without I/O, before provisioning and in plan/TUI entry paths.

- [ ] Add paired plan tests loading the identical scenario/profile under local and Multipass environments. Assert host assembly plus Minikube loading versus VM publication, dynamic Helm/endpoint dependencies, one probe build and no duplicate recipe component builds.
- [ ] Add failing tests for unsupported providers before provisioning, preserved container restrictions, conflicting runtime/image overrides, relative profile resolution, pure CLI/TUI preview and serialization, plus per-run namespace and image-tag consistency.
- [ ] Add image-check tests for every ready target container/replica, selection through Deployment/ReplicaSet ownership, unrelated same-label Pods, missing/changed images, Pod node selection, and raw evidence retained on mismatch. Inspect CRI on the actual node, not always the first Minikube node.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_recipe_validate.py packages/nanolab/tests/config/test_recipe.py packages/nanolab/tests/tasks/test_recipe_kubernetes.py --no-cov`; confirm missing behavior fails.
- [ ] Wire the resource graph. Pin local Kubernetes commands to the acquired context; role `stack` on local still executes on host. Metadata uses `platform.endpoint` and the correct role. Attach image identity checks after platform/function readiness, and a final successful-verification marker after all lifecycle checks. Expose enough run state to perform remote success cleanup only after the workflow returns successfully.
- [ ] Rerun those tests plus `tests/plans/test_validate.py`, `tests/cli/test_command_surface.py`, `tests/cli/test_execution_bindings.py` and `tests/test_tui_app.py`. Require PASS or record and isolate unchanged baseline failures; do not silently deselect new failures.

## Task 7: Migrate the profile consumer, document and execute

**Files:** create `recipes/validate-k8s-jvm.yaml`; modify existing K8s scenario, `recipes/README.md`, root `README.md`, `docs/recipes-roadmap.md`, `.github/workflows/ci.yml`.

- [ ] Create the profile with the exact modules/variant/pull-policy in Global Constraints; JVM control plane and Java JVM `word-stats`, default backend `k8s`, advanced metrics. Set `recipeProfile: ../recipes/validate-k8s-jvm.yaml` on the existing scenario.
- [ ] In a disposable NanoFaaS checkout run `./gradlew validateRecipe -Precipe=/absolute/nanolab/packages/nanolab/recipes/validate-k8s-jvm.yaml`; require success and exact component/module selection. Confirm the CI pin contains Kubernetes validation-pod support for configured `imagePullPolicy`; the previously used `cf46b94...` contains it, but inspect the current pin again.
- [ ] Extend the existing CI profile-validation step with this profile; add no scheduled triggers. Document default local Minikube and explicit `--environment packages/nanolab/environments/multipass.yaml` using the same scenario, host tools, preserved reports and failure diagnostics.
- [ ] Run `uv run --locked --no-sync --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --environment packages/nanolab/environments/local.yaml`, then the same command with `multipass.yaml`. Require correct graphs and no side effects.
- [ ] Run `uv run --frozen ruff check packages`, `uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab`, and `uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache`.
- [ ] Run `NANOFAAS_ROOT=<selected-checkout> uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests`; distinguish reproducible baseline failures from regressions and record commands/results.
- [ ] With Docker/Minikube available, run `NANOFAAS_ROOT=<selected-checkout> uv run --locked --no-sync --package nanolab nanolab run packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --run-dir /tmp/nanolab-recipe-k8s-local-<unique-id>`. Prove metadata, runtime identities, invocation, resource and queue assertions, complete cleanup, original-source status unchanged and no VM/registry publication.
- [ ] Execute the same scenario with explicit Multipass environment and a fresh run directory when Multipass is available. Verify the retained VM build log/report and provenance prove VM build/publication; test fixtures and plan checks alone do not count as E2E. If unavailable, leave this verification incomplete and report the exact blocker.
- [ ] Run `git diff --check`, self-review against the revised spec and record evidence paths/results here. Update roadmap only for verified outcomes; leave containerd as the next slice. Commit only the implementation's own reviewed files under applicable repository rules.

## Plan Review Record

- Spec corrections are covered: connectivity/namespace/probe (Tasks 3 and 5), image identity (Tasks 1 and 6), remote path/provenance/failure semantics (Task 4).
- Provider selection, single scenario/profile, CLI/TUI purity and regression checks are covered in Tasks 6 and 7.
- Task interfaces distinguish `Endpoint` resources, host `Path`, VM `PurePosixPath`, built IDs and published manifest digests.
- The checkboxes record the original execution plan; the verified implementation outcomes and remaining VM check are recorded below.

## Implementation evidence (2026-09-29)

- `./gradlew validateRecipe -Precipe=<NanoLab>/packages/nanolab/recipes/validate-k8s-jvm.yaml` passed against the current NanoFaaS checkout.
- Both `nanolab plan` variants passed: local schedules host assembly, image loading and port forwarding; explicit Multipass schedules VM staging and publication.
- Local Minikube run `/tmp/nanolab-recipe-k8s-final` passed all 29 acquire, check and release steps. Retained evidence includes `recipe/gradle.log`, `recipe/distribution/distribution.json`, Pod image JSON, build metadata and `k8s-queue-burst.json`. The selected kubeconfig and owned namespace were removed. No k6 summary was written to the NanoFaaS checkout.
- Ruff, Ruff format, basedpyright, import-linter and `git diff --check` passed. The full NanoLab suite: 2807 passed, 2 failed for pre-existing checkout assumptions (`test_build_release_request_requires_credentials_for_execution` needs clean NanoFaaS Git; `test_retention_reads_the_bean_the_control_plane_actually_publishes` looks for an old Java package/class in this checkout).
- Explicit Multipass run `/tmp/nanolab-recipe-k8s-multipass-4` passed all 25 workflow steps plus VM provisioning and destruction. The run created `nanofaas-stack`, published the recipe and queue probe in that VM, validated Pod/runtime image identities on k3s, invoked the function and queue, ran k6, then removed its namespace and VM. Retained host evidence includes the distribution, Gradle and probe logs, build metadata, image JSON and k6 summary. `multipass list --format json` returned no remaining VMs.
- Earlier Multipass attempts exposed and prompted fixes for a missing local fetch directory and for OCI index versus platform config digests and k3s Pod `imageID` syntax. The final run exercises those fixes end to end.
