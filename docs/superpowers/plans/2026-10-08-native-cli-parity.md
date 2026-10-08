# Native CLI Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Qualify the shipped native and JVM CLI command contracts against the
same owned control plane, without accepting equal incorrect results.

**Architecture:** Reuse frozen recipe builds and local Kubernetes resources.
Build selected CLI artifacts in the same snapshot, then run the existing CLI
contract assembly twice with stricter proofs and state restoration. Keep legacy
CLI graphs unchanged unless strengthening a shared verifier requires correcting
a fixture that contradicts the actual DTO.

**Tech Stack:** Python 3.12, public Sonata 0.6.14, existing Gradle/GraalVM build
controls, Minikube/Helm/Kubernetes, PyYAML and the standard library.

**Spec:** [Approved native CLI parity design](../specs/2026-10-08-native-cli-parity-design.md).

## Global Constraints

- Work on `fix/operational-validation` in its existing isolated worktree.
- Reuse the existing `cli` workflow; no callback server, SDK matrix or watchdog.
- Build selected CLI artifacts only in the frozen NanoFaaS snapshot.
- JVM uses `:nanofaas-cli:installDist`; native uses `:nanofaas-cli:nativeCompile`.
- The fixed control plane and Java word-stats function are JVM artifacts.
- Recipe-backed CLI scenarios require backend `k8s` and the local environment.
- Reject unsupported backends, remote environments and external endpoints before
  provisioning/building. Preserve default legacy JVM scenarios.
- `cliRuntime` is `jvm | native | parity`, default `jvm`; non-default values
  are valid only for `workflow: cli`.
- CLI/API commands have a 60-second outer deadline; each stdout/stderr is limited
  to 1 MiB; receipts are bounded to 16 MiB per pass; each log is bounded to 8 MiB.
- Build tasks use the existing build policy, not the CLI-command deadline.
- Preserve source revision, patch fingerprint, recipe and artifact identities.
- Run JVM first in parity mode; restore function/runtime state before native.
- Remove only acquired resources; propagate contract, diagnostic and cleanup
  failures. No public publication or operator-checkout writes.
- Preserve and report the original 90% NanoLab coverage gate and its existing debt.
- Actual native/JVM builds and an installed local lifecycle are required; this
  lot alone does not close #54.

## Review Focus

- A library changed while the JVM launcher stayed unchanged must invalidate
  artifact continuity; hash the installed libraries too (Task 1).
- Ambient CLI configuration or environment must not redirect a pass or make
  `--config` pass without reading its file (Tasks 2/3).
- A baseline already equal to the default patch must not let an ignored native
  runtime-config write pass; choose a different legal value (Task 3).
- A replacement control plane with the same image must invalidate parity,
  including replacement during otherwise successful CLI requests (Task 3).
- Retry evidence and sliced executions must not qualify from an earlier or
  incomplete attempt; cleanup failure must leave the workflow failed (Tasks 3/4).

---

### Task 1: Frozen CLI artifacts and identity receipts

**Files:** Create `packages/nanolab/src/nanolab/tasks/cli_artifacts.py` and
`packages/nanolab/tests/tasks/validation/test_cli_artifacts.py`.

**Interfaces:** Define `CliMode = Literal["jvm", "native"]` and
`cli_modes(runtime: str) -> tuple[CliMode, ...]`, yielding `("jvm",)`,
`("native",)` or `("jvm", "native")`, rejecting other values.
Produce a frozen `CliArtifact(mode: CliMode, binary: Path, architecture: str, version: str,
files: tuple[tuple[str, str], ...])`; `files` contains source-relative paths
and SHA-256 values, including every installed JVM JAR.
Define `describe_cli_artifact(source: Path, mode: CliMode, *, architecture: str)
-> CliArtifact`, `verify_cli_artifact(source: Path, artifact: CliArtifact) -> None`,
and `cli_artifact_resource(run: Resource[RecipeRun], *, mode: CliMode,
executor: CommandTaskExecutor, evidence_dir: Path) -> Resource[CliArtifact]`.
The resource depends only on `run`, builds on `host`, and retains receipts;
its release does not remove build artifacts.

- [x] Write `test_cli_modes_selects_exact_artifacts` with the tuples above and
  rejection of an unknown runtime:

  ```python
  assert cli_modes("jvm") == ("jvm",)
  assert cli_modes("native") == ("native",)
  assert cli_modes("parity") == ("jvm", "native")
  with pytest.raises(ValueError):
      cli_modes("unknown")
  ```

  Add `test_artifact_identity_checks_kind_arch_and_libraries`:
  native requires an executable regular ELF64 for `arm64` or `amd64`; a launcher
  script, wrong machine, missing file, escaping symlink or nonexecutable fails.
  JVM requires the launcher and nonempty regular installed JARs. Mutating a JAR
  or binary after description must make `verify_cli_artifact` fail. Normalize
  host architecture to `arm64`/`amd64`, retain it in the artifact and check the
  native ELF machine against it; reject an incompatible execution host. Temporary ELF
  headers in these unit tests are parser fixtures, not executable qualification.
- [x] Run `.venv/bin/pytest packages/nanolab/tests/tasks/validation/test_cli_artifacts.py --no-cov`.
  Expected: RED for missing selection/identity/resource functions.
- [x] Implement the interfaces. Paths are
  `clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli` and
  `clients/cli/build/native/nativeCompile/nanofaas-cli`. Resolve every artifact
  inside `source`; read only a bounded ELF header. Use `read_project_version`
  for the expected version. Build through existing `GradleTask` and native
  controls; record bounded help/version output and reject unexpected version
  or native error markers. Write `<evidence_dir>/<mode>.json` exclusively with
  artifact/source identity. Verify frozen tracked inputs have not changed.
- [x] Add `test_artifact_build_depends_only_on_frozen_source`: inspect and run
  each resource with a command-boundary executor; assert the correct Gradle
  target, frozen cwd, artifact path and no Kubernetes/API commands. A failed
  build/help/version command must not produce a successful artifact receipt.
  Expected: GREEN for the whole module, including library mutation.
- [x] Commit `feat: build and verify isolated CLI artifacts`.

### Task 2: Shared CLI proofs and reusable contract assembly

**Files:** Modify `packages/nanolab/src/nanolab/tasks/cli_function.py`,
`packages/nanolab/src/nanolab/tasks/cli.py` and, for non-null invocation errors,
`packages/nanolab/src/nanolab/tasks/invocation.py`.
Test in `packages/nanolab/tests/tasks/validation/test_cli_contract.py`,
new `packages/nanolab/tests/tasks/functions/test_cli_function.py`,
and `packages/nanolab/tests/tasks/functions/test_invocation.py`.
Update only inaccurate canned replies in
`packages/nanolab/src/nanolab/tasks/testing.py` or local test fixtures if exposed.

**Interfaces:** Add `parse_cli_list(stdout: str) -> dict[str, str]`,
`CliFunctionGetTask(manifest: FunctionManifest, *, patch: dict[str, Any] | None = None,
cli_argv: tuple[str, ...], executor: CommandTaskExecutor, role: ExecutionRole,
cwd: Path | None = None)` and `CliFunctionListTask(expected: dict[str, str], *,
absent: tuple[str, ...] = (), cli_argv: tuple[str, ...], executor: CommandTaskExecutor,
role: ExecutionRole, cwd: Path | None = None)`.
Add `cli_config_file_task(manifest: FunctionManifest, *, binary: Path,
endpoint: str, executor: CommandTaskExecutor, role: ExecutionRole,
cwd: Path | None = None) -> CommandTask`.
Expose `runtime_config_patch_task(namespace: str, values: dict[str, Any], *,
expected_revision: int | None = None, cli_argv: tuple[str, ...],
executor: CommandTaskExecutor, role: ExecutionRole,
cwd: Path | None = None) -> CommandTask` at the existing runtime-config owner.
It verifies effective namespace values separately from the request envelope;
`runtime_config_tasks` delegates its patch command to it.
Extend `CliFunction` with `expected_output: object | None = None` and
`CliWorkflowRequest` with `config_file: Path | None = None`.
Extract `add_cli_contract(workflow: Workflow, request: CliWorkflowRequest, *,
executor: CommandTaskExecutor, cwd: Path | None = None,
requires: tuple[Resource[Any], ...] = (),
function_requires: tuple[Resource[Any], ...] = (),
readiness_timeout_seconds: int | None = None, strict: bool = False,
runtime_config_patch: dict[str, Any] | None = None) -> tuple[Resource[None], ...]` from the existing
contract half of `build_cli_workflow`. Legacy callers use default arguments.

- [x] Write `test_strict_cli_proofs_reject_wrong_identity_and_equal_bad_results`:
  invalid list rows, duplicate names, missing expected name/image, another
  function's details/replicas and boolean counts fail. Valid apply/get matches
  manifest fields, resources and requested/effective deployment mode; patches
  change the required values. A listed function after delete fails.
  Add `test_config_file_probe_does_not_use_endpoint_override`: its command uses
  an owned YAML file, no `--endpoint`, and child environment excludes
  `NANOFAAS_ENDPOINT` and `NANOFAAS_CONTEXT`; independent HTTP proves file selection.
- [x] Run the new tests RED. Expected: missing strict/config probes, or accepted
  invalid output. Also reproduce exit 0 with an expected negative diagnostic:
  unreplaced immutable apply and invalid runtime validation must both fail.
- [x] Implement the probes at `cli_function.py`, reusing `_script_with_file` and
  existing JSON parsing. Empty list output is valid only when expectations permit
  it; parse the actual tab-separated name/image format. Invoke uses the shared
  success check, rejects non-null error and, when provided, compares exact corpus
  output. Replica checks assert identity and `type(count) is int`. Negative
  verifiers require nonzero exit as well as their existing diagnostic.
  Add `test_runtime_restore_uses_revision_envelope_and_checks_effective_values`:
  with revision 7 and value 1000000, the file contains
  `{"expectedRevision": 7, "values": {"rateMaxPerSecond": 1000000}}`;
  matching effective values pass, stale revision or an ignored write fails.
- [x] Extract `add_cli_contract` without changing the default legacy topology.
  In strict mode, acquire through CLI apply followed by manifest get/readiness;
  check list, get after update/replace, explicit config-file get, and delete
  absence. Reuse all existing update/replica/replace/info/OpenAPI/runtime-config
  factories. Supply `runtime_config_patch` instead of the global default when
  passed; add an independent config get/readback after the patch in strict mode.
  Include the owned `config_file` in ordinary command prefixes.
  Readiness commands get a 45-second budget, inside the 60-second outer limit.
  In strict mode, place replica set/get and readiness before workload invocation;
  retain the existing legacy ordering when `strict=False`.
- [x] Run `.venv/bin/pytest packages/nanolab/tests/tasks/validation/test_cli_contract.py packages/nanolab/tests/tasks/functions --no-cov`.
  Expected: GREEN for existing lifecycle/slicing/compensation tests and new
  strict proofs. Canned responses must reflect the pinned DTOs, not weaken gates.
- [x] Commit `feat: prove CLI lifecycle and configuration contracts`.

### Task 3: Paired execution, baseline restoration and bounded evidence

**Files:** Create `packages/nanolab/src/nanolab/tasks/validation/cli_parity.py`
and `packages/nanolab/tests/tasks/validation/test_cli_parity.py`.

**Interfaces:** Consume Task 1's `CliArtifact`, Task 2's `add_cli_contract`,
the staged `Resource[RecipeRun]`, validated `Resource[RecipeDistribution]`,
the acquired `Endpoint` and existing selected `Resource[MinikubeTarget]`.
Produce `CliParityTask(run: Resource[RecipeRun], distribution:
Resource[RecipeDistribution], artifacts: tuple[Resource[CliArtifact], ...], *,
runtime: str, endpoint: Endpoint, target: Resource[MinikubeTarget], namespace: str,
function: str, executor: CommandTaskExecutor, evidence_dir: Path)`.
It is non-idempotent and resolves every resource only during `run(inputs)`.
Require artifact modes to equal `cli_modes(runtime)` in order, with no missing
or duplicate artifact. One requested mode qualifies that mode; requested
`parity` requires exactly `("jvm", "native")`. Record the distinction explicitly.

- [x] Write `test_parity_checks_both_passes_and_restores_baselines`: a stateful
  independent API/command-boundary fixture starts with no function and rate
  ceiling 1000000; both passes observe fresh registration and the same patch.
  After JVM deletion, deployment/service absence and the original runtime value
  must be verified before native starts. Reuse actual shared contract factories.
- [x] Write `test_parity_rejects_same_wrong_output_and_replaced_control_plane`:
  matching incorrect corpus output fails, and identical-image replacement after
  successful requests fails on Deployment/pod/container identity. Parameterize
  missing mode/case, registration error in stdout/stderr or post-request logs,
  oversize output/logs, native failure, and inner function cleanup failure. No successful parity
  receipt is produced for these cases. Run RED; expected missing orchestration.
- [x] Implement a small executor wrapper in this module that delegates to the
  same executor, applies GNU `timeout --kill-after=5s 60s` only to CLI/API/probe
  commands, bounds streams and records each command before verification. Reuse
  the established bounded-command approach without applying it to builds.
  Detect `UnsupportedFeatureError`, `MissingReflectionRegistrationError`,
  `MissingResourceRegistrationError`, `No serializer found`, traceback and panic
  markers. Keep each pass below 16 MiB and each owned log below 8 MiB.
- [x] Resolve the fixed Java JVM word-stats image from the validated distribution.
  Load the first corpus case with expected HTTP status 200 and no expected error;
  fail for absent/ambiguous input/expected output. Construct one inner `Workflow`
  per artifact and call Task 2's assembly, with strict proofs and no build phase.
  The owned config file avoids reading operator defaults. Snapshot the namespace's
  owned control plane through `owned_deployment_pods`; retain UID/container and
  image/metadata evidence before and after each pass, including post-request logs.
  Require the function absent through list and deployment/service probes before
  its first acquisition; unexpected existing resources fail without deleting them.
- [x] Read the original `control-plane` runtime-config `rateMaxPerSecond` and revision.
  Use patch 999999, or 999998
  if the baseline is already 999999, and invalid patch -1. Restore the original
  value with the current revision through Task 2's `runtime_config_patch_task` and
  verify readback before native; reject optimistic-lock conflict. Verify function
  deployment/service removal before reusing the name. Validate artifacts again
  after the passes, and compare semantic observations from the approved spec.
  Store raw dynamic fields; exclude only endpoint URLs, execution IDs, pod names
  and revision counters from cross-pass equality. Preserve meaningful array order.
  Build an explicit per-pass case ledger from the shared contract tasks: apply,
  get, list, update/get, replicas set/get/readiness, invoke, replacement refusal,
  replace/get, info, OpenAPI, config snapshot/valid/invalid/patch/readback,
  config-file get, delete/list and deployment/service absence. Attach verified
  help/version receipts from Task 1. Require the complete expected key set before
  comparison; missing observations cannot qualify. Keep raw task results and
  parsed observations keyed by case, independent of generated task identifiers.
  Restore and verify the controlled runtime baseline after the final successful
  pass too; on failure attempt owned compensation and retain every cleanup error.
- [x] Add `test_baseline_equal_to_default_patch_still_proves_native_write` and
  `test_retry_never_reuses_qualified_receipts`. Both must pass alongside the
  state/identity tests. Run `.venv/bin/pytest packages/nanolab/tests/tasks/validation/test_cli_artifacts.py packages/nanolab/tests/tasks/validation/test_cli_contract.py packages/nanolab/tests/tasks/validation/test_cli_parity.py --no-cov`.
  Expected: GREEN, with owned function compensation on either pass's failure.
- [x] Commit `feat: qualify native and JVM CLI parity`.

### Task 4: Recipe-backed local Kubernetes CLI integration

**Files:** Create `packages/nanolab/src/nanolab/plans/cli_recipe.py`;
modify `config/scenario.py`, `plans/cli.py`, `cli/product.py` and
`tasks/recipes/kubernetes.py` under `packages/nanolab/src/nanolab/`.
Create bundled recipe `assets/presets/recipes/cli-contract-k8s-jvm.yaml` and
scenario `assets/presets/scenarios/cli-contract-k8s-native-parity.yaml`.
Update the presets README. Test in `tests/plans/test_cli.py`,
`tests/config/test_scenario.py`, `tests/tasks/recipes/test_kubernetes.py` and
`tests/cli/test_command_surface.py` under `packages/nanolab/`.

**Interfaces:** Add optional `run_dir: Path | None = None` to `build_cli_plan`;
the existing product dispatcher supplies its operator run directory.
Define `build_recipe_cli_plan(config: ScenarioConfig, bindings: RoleBindings, *,
repo_root: Path, environment: EnvironmentConfig | None, run_dir: Path) -> Workflow`.
Change `minikube_images_resource`'s `probe_image` to `Resource[str] | None = None`,
excluding absent probe references/dependencies. All existing #53 calls keep their
probe argument. Use Task 1's resources and Task 3's gate without another engine.

- [x] Write `test_cli_runtime_and_recipe_guards_fail_before_commands` for invalid
  runtime, non-CLI native/parity, no recipe with native/parity, non-k8s recipe,
  remote environment or supplied endpoint. Construct direct builders too; assert
  the executor saw no calls. Default legacy CLI graphs remain unchanged.
- [x] Write `test_recipe_cli_compiles_frozen_artifacts_and_owned_platform`:
  the plan has the exact selected artifact resources, validated recipe, selected
  target/pinned context, image import, unique namespace, Helm and forward, then
  the gate. It has no HTTP function registration, queue-probe or registry/push
  task. A build-only slice acquires the source/artifact, never cluster resources.
  Run RED for missing configuration/integration.
- [x] Implement validation and the dedicated recipe with modules
  `k8s-deployment-provider`, `build-metadata`, `runtime-config`, JVM control-plane
  variant `recipe-v2-cli-k8s-jvm` and one Java JVM word-stats function.
  The bundled scenario selects `cliRuntime: parity`. Other runtime selections
  use the same path; validate the acquired distribution's modes/modules too.
  Pass `config.cli_runtime` explicitly to Task 3's gate, which independently
  checks its expected artifact modes.
- [x] At planning choose a fresh attempt token, without filesystem writes.
  Put frozen inputs and receipts under `run_dir/cli-attempts/<token>/`; use the
  token for unique image tags and namespace. Separate attempts therefore cannot
  share qualification receipts or import ownership. Build resources depend only
  on the staged source; API resources depend on the selected target/distribution.
  Reuse `PinnedKubeconfigExecutor`, recipe namespace/image resources and platform
  endpoint/Helm factories directly. Call only their platform half, leaving
  function acquisition to the CLI. Set admin runtime-config enabled and use the
  frozen chart. All API resources remain alive through both passes and cleanup.
- [x] Record contract/parity proof inside the current attempt only after all
  cases and continuity checks. Use the existing final run metadata to distinguish
  proof from overall success: cleanup failure leaves status failed. A slice that
  never ran the full gate has no qualification proof. Test duplicate/foreign
  namespace/image refusal, Resource-backed URL routing, normal cleanup and failure
  during acquisition/each pass/release. Preserve the optional-probe #53 path.
- [x] Run `.venv/bin/pytest packages/nanolab/tests/plans/test_cli.py packages/nanolab/tests/config/test_scenario.py packages/nanolab/tests/tasks/recipes/test_kubernetes.py packages/nanolab/tests/tasks/validation/test_cli_parity.py packages/nanolab/tests/cli/test_command_surface.py --no-cov`.
  Expected: GREEN; `build_cli_plan` compilation has no external side effects.
- [x] Commit `feat: integrate recipe-backed CLI parity scenario`.

### Task 5: Installed qualification and final whole-branch review

**Files:** Create `docs/superpowers/plans/2026-10-08-native-cli-parity-evidence.md`;
update this plan and its approved spec status with actual results.

- [ ] Build/install isolated NanoLab/toolkit wheels with unchanged public Sonata
  dependencies. Resolve both new bundled presets through the installed CLI.
  Expected: valid compilation and compatible installed dependencies.
- [ ] Run the installed parity scenario against an exclusively owned local
  arm64 Minikube profile, using an isolated MINIKUBE_HOME/KUBECONFIG and frozen
  source containing the native CLI fixes. Record actual Gradle builds, ELF/JAR
  hashes, version, every required command, corpus result, baseline restoration,
  control-plane continuity, logs, comparisons and all normal releases.
  Expected: exit 0, native and JVM independently pass and compare, cleanup passes.
  Inspect namespace/import removal, then remove only this verification profile.
  Failed/missing live evidence keeps this task explicitly incomplete.
- [ ] Run NanoLab and toolkit suites with their original package coverage
  configurations and the CI-pinned source for checkout-contract tests. Use the
  unrestricted test environment where local HTTP/async tests need it.
  Expected: functional tests pass; report the actual original coverage gate
  separately, preserving 90%. Run all quality hooks and both dependency checks.
  Expected: hooks/type checks/dependencies pass; no lock/version changes.
  Commands from the worktree are:
  `NANOFAAS_ROOT=/tmp/nanolab-61-pinned UV_CACHE_DIR=/tmp/nanolab-uv-cache uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml --cov-config=packages/nanolab/pyproject.toml packages/nanolab/tests`,
  `UV_CACHE_DIR=/tmp/nanolab-uv-cache uv run --frozen --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml --cov-config=packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests`,
  `UV_CACHE_DIR=/tmp/nanolab-uv-cache PRE_COMMIT_HOME=/tmp/nanolab-pre-commit uv run --frozen --all-packages --all-groups pre-commit run --all-files`,
  and `uv pip check --python .venv/bin/python` plus the same check using the
  isolated installed environment's Python. First verify the pinned checkout is
  still at `e7914be065e844776af57fe9e449bce7f12e03c5`.
- [ ] Obtain one fresh final whole-branch review according to the execution skill,
  including this plan's Review Focus and every ledger ruling. Fix material
  findings in the prescribed single RED-to-GREEN pass and rerun the whole suite;
  repeat affected live qualification if runtime behavior changed. Record exclusions
  and costs. Expected: no unaddressed material findings, no fabricated live proof.
- [ ] Commit the evidence, preserve decisions and actual coverage limitations,
  remove only this plan's scratch workspace, and keep the requested branch and
  worktree. Do not push, publish, merge or close #54 as part of this plan.

## Self-review and execution handoff

The spec maps to Tasks 1 (artifacts), 2 (shared command proofs), 3 (paired state,
identity, bounds and comparison), 4 (selection/platform/compatibility) and
5 (installed proof and final verification/review). Each Review Focus case has
an explicit test owner. The five tasks share the same artifact and lifecycle
interfaces; separate implementers would add dependent handoffs.

Self-review checked spec coverage, actionable steps, cross-task names/types,
the five failure-mode test owners and proportion. It corrected the required-mode
input, retained artifact architecture, the revision-guarded patch interface and
the complete case ledger; no uncovered spec requirement remains.

Recommended execution preserves the previous lot's method: I implement the tasks
in this session, with one fresh final reviewer on the most capable available
model. The user approved the written spec and this implementation plan. Inline execution
started on 2026-10-08; progress is recorded per task.
