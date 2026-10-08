# Packaged Function and Watchdog Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Qualify the real packaged function matrix and bash watchdog modes against independent body/status/callback oracles, with verified cleanup.

**Architecture:** Add a local container `contract` workflow, using the existing frozen-source resource, function catalog, image planner and Bake renderer. A bundled stdlib Python container captures callbacks and probes artifacts on an owned private network. Sonata owns builds and runtime resources; the CLI writes final qualification only after the workflow has released resources and absence checks pass.

**Tech Stack:** Python >=3.12, Pydantic, existing Sonata 0.6.14, Docker/Buildx, pytest; no dependency or lockfile changes.

**Spec:** [Approved packaged functions/watchdog design](../specs/2026-10-08-packaged-functions-watchdog-design.md).

Status: written plan approved on 2026-10-08 with Native execution; implementation is in progress. Worktree `/tmp/nanolab-operational-validation`, branch `fix/operational-validation`, existing NanoLab PR #76.

## Global Constraints

- `workflow: contract`, `backend: container`, local environment; Linux native host architecture only (`amd64` or `arm64`).
- 60 seconds for readiness, 30 seconds per HTTP request, 15 seconds for callback arrival, and a 2-second observation period after runtime shutdown before the final callback audit.
- Bound each request/response/callback to 2 MiB, logs to 8 MiB per container and capture to 1,000 records / 32 MiB total. Reaching a bound fails qualification.
- Use a 45-minute build deadline per image. The private builder has a 16 GiB memory limit and four CPU equivalents; record and verify those Docker resource limits.
- No emulation, privileged binfmt setup, operator builder/source mutation, image publication, external endpoints, remote providers, Kubernetes/containerd, `--keep`, teardown, resume or partial task selections for qualification.
- Every selected runtime flavor and every independent corpus case must run. Current observations are 23 implementations, 27 cells, 119 HTTP cases and 18 one-shot cases; derive acceptance counts from frozen inputs.
- Warm bash has zero callbacks; ordinary SDK and one-shot bash have exactly one successful callback per case. Missing callback `statusCode` means 200.
- Preserve public dependencies and original package coverage configurations and thresholds (NanoLab 90%, toolkit 80%). Keep CLI shipped-source qualification separate; this delivery does not close #54.

## Review Focus

1. Equivalent-looking JSON (`true` versus `1`, duplicate keys, reordered arrays) must not silently match an oracle: Task 1 parser and Task 4 comparison tests.
2. Slow/oversized HTTP bodies and capture saturation must fail within budgets and preserve failure evidence: Task 2 real socket tests.
3. An SDK retries or delivers after its container stops; an early successful callback must not hide a duplicate: Tasks 2 and 4 post-shutdown audit tests.
4. An operator resource adopts a reused name/tag; cleanup must preserve the replacement and fail qualification: Task 3 ownership-conflict tests.
5. Release fails after all cases passed, or receipts are missing/replaced; no final success marker may be written: Task 5 finalization tests.

---

## File ownership and shared types

All paths below are relative to the worktree. `src` abbreviates `packages/nanolab/src/nanolab`; `tests` abbreviates `packages/nanolab/tests`.

| Owner | Files | Responsibility |
| --- | --- | --- |
| Configuration/oracles | `src/config/contract.py`, `src/config/scenario.py`, `src/functions/contracts.py` | Validated budgets, selector expansion, corpus and payload validation; no platform acquisition. |
| Capture/probe | `src/assets/diagnostics/artifact_contract.py`, `src/assets/diagnostics/artifact_contract.Dockerfile` | Bounded stdlib HTTP server, actual HTTP probe, registration and retained callback records. Existing package-data glob ships both. |
| Build/resource ownership | `src/tasks/validation/contract_resources.py` | Private Buildx/network/helper/images/containers, identity, compensated acquisition and absence receipts. |
| Runtime semantics | `src/tasks/validation/function_contracts.py` | Corpus execution, response/callback/PNG/log checks, per-case immutable evidence. |
| Product integration | `src/plans/contract.py`, `src/cli/product.py`, `src/cli/catalogue.py`, two new `src/assets/presets/scenarios/artifact-contract*-container.yaml` | Workflow graph, CLI validation, installed presets and post-release finalization. |
| Documentation/evidence | `docs/artifact-contracts.md`, `docs/superpowers/plans/2026-10-08-packaged-functions-watchdog-evidence.md`, product documentation links | Operator use, restrictions, real run evidence and exclusions. |

Task 1 owns `JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]`, frozen `ContractCase(name: str, input: JsonValue, expected: JsonValue, status: int, png_size: int | None)` and `ContractMatrix(images: ImagePlan, cases: dict[str, tuple[ContractCase, ...]], source_state: dict[str, object], corpus_hashes: dict[str, str], subset: tuple[str, ...])`. `png_size is not None` identifies the PNG oracle; JSON null remains a legitimate exact expectation.

Task 3 owns frozen `ContractRuntime(network: str, capture_container: str, probe_image: str, owner: str)` and `ContractImage(cell: ImageCell, image_id: str, architecture: str, entrypoint: tuple[str, ...], native_sha256: str | None)`. Task 4 owns frozen `ContractEvidence(case_paths: tuple[Path, ...], expected_http: int, expected_one_shot: int, expected_callbacks: int)`. Existing `Resource`, `TaskInputs`, `Task`, `TaskOutcome`, `Workflow`, `RecipeRun`, `RoleBindings` and `CommandTaskExecutor` retain their published types.

## Task 1: Reject invalid matrices and resolve independent oracles

**Files:** Create `src/config/contract.py`, `src/functions/contracts.py`, `tests/config/test_contract.py`, `tests/functions/test_contracts.py`; modify `src/config/scenario.py`.

**Interfaces:** Produce `ContractConfig` with validated positive finite budget fields named `readiness_seconds`, `request_seconds`, `callback_seconds`, `quiet_seconds`, `message_bytes`, `log_bytes`, `capture_records`, `capture_bytes`, `build_seconds`, `builder_memory_bytes`, `builder_cpu_quota`. Defaults respectively `60, 30, 15, 2, 2097152, 8388608, 1000, 33554432, 2700, 17179869184, 400000` (CPU period 100000). Produce `resolve_contract_matrix(source: Path, selectors: tuple[str, ...], *, architecture: ImageArchitecture, tag: str) -> ContractMatrix`; consume existing `list_functions(root)` and `build_image_plan(...) -> ImagePlan`.

- [ ] Write parser/selector RED tests, including representative assertions:
  ```python
  def test_defaults():
      assert ContractConfig().capture_bytes == 33554432
      assert ContractConfig().build_seconds == 2700


  def test_empty_selection(frozen_fixture):
      with pytest.raises(ValueError, match="empty"):
          resolve_contract_matrix(frozen_fixture, (), architecture="arm64", tag="attempt")
  ```
  Add named tests `test_duplicate_json_keys_rejected`, `test_boolean_status_rejected`, `test_nonfinite_json_rejected`, `test_missing_and_ambiguous_oracles_rejected`, `test_unknown_and_rust_selectors_rejected`, `test_payload_expected_cannot_override_oracle`, `test_catalog_addition_changes_matrix`, and `test_escaped_corpus_or_payload_rejected`. Assert `ValueError` and zero platform commands. Checkout-marked `test_current_catalog_matrix` asserts the observed 27/119/18 counts at recorded `a234ea17`, never production constants.
- [ ] Run `uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/config/test_contract.py packages/nanolab/tests/functions/test_contracts.py --no-cov`. Expected RED: missing contract interface/validation; then use the same command for GREEN.
- [ ] Implement the declared interfaces. `ScenarioConfig.contract: ContractConfig | None` defaults to `None`; require it only for `contract`, add the workflow literal and reject irrelevant contract-only settings elsewhere. Preserve existing nonempty `functions` validation. Contract selectors accept a catalog key or one of the four supported family names; family expands to all its current supported entries. Full preset lists the four families, smoke lists `word-stats`.
- [ ] Read `functions/test-data/{family}/correctness.json` strictly, validate unique names and inputs, explicit/default status (100–599, not boolean), and JSON expectation or positive QR PNG oracle (default size 256). Validate every selected implementation's `payloads/*.json` wrapped `input` and `expected` against exactly one corpus case. Reject symlink escapes; retain hashes and `captured_source_state(source)`. Map catalog keys to existing image target names, select host architecture and all target flavors; exclude other families/fixtures/service targets with reported reasons.
- [ ] Run GREEN plus `tests/config/test_scenario.py`, `tests/images/test_plan.py`, `tests/images/test_bake.py` with expanded paths and `--no-cov`. Commit `feat: resolve packaged function contract matrices` including only this task's files.

## Task 2: Bounded callback capture and actual HTTP probing

**Files:** Create the two helper assets, `tests/tasks/validation/test_artifact_contract_capture.py`, `tests/tasks/validation/test_artifact_contract_probe.py`.

**Interfaces:** Helper accepts `serve` or `probe` mode with validated settings. Probe reads a bounded JSON instruction from stdin and writes one bounded JSON observation with HTTP `status`, `headers`, `bodyBase64` or a failure. Capture uses port 8081, POST `/v1/executions/{id}:complete`, POST `/_nanolab/register` with `{executionId, expectedCallbacks}`, and GET `/_nanolab/records`; inspection returns retained raw-body base64, method/path, monotonic sequence and violations. Unknown routes are retained violations. Readiness is GET `/health`. Each expected execution is registered before invocation; startup probe is a separate registered ID excluded from artifact counts.

- [ ] Write real local HTTP RED tests (unrestricted local networking when required), not fabricated transport records:
  ```python
  def test_callback_id_comes_from_path(capture):
      response = capture.post(
          "/v1/executions/one:complete", {"success": True, "output": {}}
      )
      assert response.status == 200
      assert capture.records()[0]["executionId"] == "one"
  ```
  Fixture registers `one` first. Add `test_ack_does_not_wait_for_inspection`, `test_duplicate_and_unknown_ids_retained`, `test_duplicate_keys_and_unknown_routes_fail`, `test_warm_registration_rejects_callback`, `test_slow_body_hits_request_deadline`, `test_message_and_capture_bounds_fail`, `test_probe_preserves_non_2xx_body`, and `test_late_delivery_is_in_final_snapshot`. Assert raw evidence retained, violation nonempty and deadline exit; exact-bound inputs fail rather than truncate into success.
- [ ] Run `uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/validation/test_artifact_contract_capture.py packages/nanolab/tests/tasks/validation/test_artifact_contract_probe.py --no-cov`. Expected RED: missing helper/protocol; GREEN uses the same command.
- [ ] Implement bounded stdlib `http.server`, `urllib.request`, `json` and monotonic deadlines. Retain malformed records and fail flags without exceeding total evidence bounds; no silent record eviction. Probe performs actual requests and treats HTTP errors as observations. Configure server request deadlines so stalled clients cannot block health/inspection indefinitely. No invocation in readiness. Dockerfile uses Python 3.12 stdlib, unprivileged user and no host port; record the resolved base/image identity at build time.
- [ ] Run GREEN. Commit `feat: capture and probe artifact callbacks on an owned network` including only the helper assets and tests.

## Task 3: Freeze, build and own exact artifacts

**Files:** Create `src/tasks/validation/contract_resources.py`, `tests/tasks/validation/test_contract_resources.py`; reuse `src/tasks/recipes/workflow.py`, `src/workspace/recipe.py`, `src/images/plan.py` and `src/images/bake.py` without introducing another source/build engine.

**Interfaces:** Consume Task 1 matrix/settings and Task 2 helper. Produce `contract_images_resource(source: Resource[RecipeRun], *, selectors: tuple[str, ...], settings: ContractConfig, executor: CommandTaskExecutor, run_dir: Path, tag: str) -> Resource[tuple[ContractImage, ...]]`, `contract_runtime_resource(*, settings: ContractConfig, executor: CommandTaskExecutor, run_dir: Path, tag: str) -> Resource[ContractRuntime]`, `artifact_container_resource(image: ContractImage, runtime: ContractRuntime, *, mode: Literal["sdk", "warm", "one-shot"], execution_id: str | None, payload: JsonValue | None, executor: CommandTaskExecutor, run_dir: Path) -> Resource[str]` (value is inspected container ID), and `verify_contract_cleanup(run_dir: Path, executor: CommandTaskExecutor) -> None`.

- [ ] Write RED ownership/build tests:
  ```python
  def test_replacement_container_is_preserved(owned_container, executor):
      executor.replace_container_owner()
      with pytest.raises(RuntimeError, match="ownership"):
          owned_container.release()
      assert executor.replacement_exists()
  ```
  Use the existing resource test harness to supply `TaskInputs` for release. Add `test_partial_acquisition_compensates`, `test_private_builder_limits_verified`, `test_bake_uses_frozen_source_and_load`, `test_each_image_build_has_deadline`, `test_jvm_prerequisites_run_once`, `test_java_lite_does_not_claim_unused_native_flags`, `test_wrong_elf_or_changed_image_fails`, `test_changed_source_fails`, `test_tag_collision_preserved`, `test_absence_check_does_not_trust_remove_exit`, and `test_runtime_network_has_no_published_ports`. Assert no binfmt/privileged/selected-builder command and only owned resources removed.
- [ ] Run `uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/validation/test_contract_resources.py --no-cov`. Expected RED: missing resource owner/identity rules; same command GREEN.
- [ ] Implement interfaces using `recipe_run_resource(source=repo_root, recipe=scenario_path, run_dir=attempt/"inputs", tag=tag)` for tracked-source/config freezing; its generic acquisition does not parse a recipe or assemble a distribution. Require a fresh attempt and recompute Task 1 matrix from that snapshot before any Docker acquisition. Compare tracked source state before/after prerequisites and image builds; operator source is never the build context.
- [ ] Wrap published `buildx_builder_resource` with `exclusive=True`, `use=False`, unique owner node, runc config and driver options `memory=16g`, `cpu-period=100000`, `cpu-quota=400000` from settings. Inspect actual builder container limits. Run selected JVM prerequisites then render existing Bake targets, building one cell at a time with `--load` and its 2700-second deadline. Use existing native flags where accepted; Java-lite relies on verified builder cgroup limits. Track helper and cell image IDs; no registry push. Persist `matrix.json` (source state, corpus hashes, selected cells, cases and derived totals) and `owned-resources.json` (kind, ID/tag, owner and expected identity). Write separate immutable `cleanup/{kind}-{id}.json` observations after every release, including conflicts/failures; the final absence check uses the retained ownership inventory rather than command exit alone.
- [ ] Create labeled network/helper/artifact resources with compensation and identity-aware release. Inspect actual image/container architecture, entrypoint and IDs; preserve existing artifact command. Extract/hash Java/native executable and validate ELF64 machine 62/183 as in existing CLI identity checks (do not invoke a JVM launcher to prove native). Record owned image tags/IDs and independently inspect absence after removal; preserve preexisting/replaced identities and propagate cleanup failure.
- [ ] Run GREEN and existing recipe builder/source tests with `--no-cov`. Commit `feat: own frozen contract builds and container resources` including only this task's changes.

## Task 4: Execute corpus contracts, watchdog modes and final callback audit

**Files:** Create `src/tasks/validation/function_contracts.py`, `tests/tasks/validation/test_function_contracts.py`.

**Interfaces:** Consume Tasks 1–3. Produce `FunctionContractsTask(source: Resource[RecipeRun], images: Resource[tuple[ContractImage, ...]], runtime: Resource[ContractRuntime], *, selectors: tuple[str, ...], settings: ContractConfig, executor: CommandTaskExecutor, run_dir: Path)` as `Task[ContractEvidence]`; `run(inputs: TaskInputs) -> TaskOutcome[ContractEvidence]`. Produce `validate_case_observation(case: ContractCase, *, mode: Literal["sdk", "warm", "one-shot"], http: dict[str, JsonValue] | None, callbacks: tuple[dict[str, JsonValue], ...], exit_code: int | None) -> None` for semantic validation.

- [ ] Write RED contract tests:
  ```python
  def test_boolean_does_not_match_integer(case_with_integer, observation_with_bool):
      with pytest.raises(ValueError):
          validate_case_observation(
              case_with_integer, mode="sdk", **observation_with_bool
          )


  def test_missing_status_cannot_qualify_422(qr_error, callback_without_status):
      with pytest.raises(ValueError):
          validate_case_observation(
              qr_error,
              mode="one-shot",
              http=None,
              callbacks=(callback_without_status,),
              exit_code=0,
          )
  ```
  Add `test_missing_status_means_200`, `test_wrong_status_body_and_array_order_fail`, `test_business_error_200_is_valid`, `test_runtime_error_envelope_fails`, `test_missing_duplicate_wrong_id_callbacks_fail`, `test_duplicate_after_shutdown_invalidates_pass`, `test_warm_has_zero_callbacks`, `test_one_shot_uses_real_entrypoint_and_wrapped_input`, `test_nonzero_one_shot_exit_fails`, `test_png_crc_dimensions_and_base64`, `test_qr_http_and_callback_bytes_must_match`, `test_logs_fail_on_native_serialization_traceback_panic`, and `test_failed_case_keeps_raw_receipts`. Assert no success aggregate is written by the task.
- [ ] Run `uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/validation/test_function_contracts.py --no-cov`. Expected RED: missing semantic checks/execution; same command GREEN.
- [ ] Implement the interfaces. SDK/warm: one artifact at a time, readiness health only, then POST `/invoke` via helper with unique `X-Execution-Id` and `{"input": case.input}`. Register each ID before sending; wait within callback budget for SDK, require zero for warm. Validate exact semantic JSON with type-aware numbers/booleans and meaningful array order; effective callback status defaults to 200 and `success` must be boolean true. Infrastructure envelopes cannot pass as expected application errors.
- [ ] One-shot bash: fresh packaged container per case, existing handler command, `WARM=false`, `EXECUTION_ID`, owned `CALLBACK_URL`, and `INVOCATION_PAYLOAD` equal to the wrapped request. Require exit 0 and one valid callback; do not assert HTTP or substitute a scratch-image handler. Probe, capture and artifact diagnostics use the existing native error markers plus serialization/traceback/panic checks, bounded at 8 MiB.
- [ ] QR: exact JSON/status for errors; otherwise honor shipped PNG envelope/content type/encoding, strict base64, PNG signature/chunk lengths/CRC/IHDR/IEND and bounded decompression/image structure, requested/default dimensions. Compare decoded HTTP/callback bytes within an invocation only; do not claim QR text decoding or cross-runtime byte equality.
- [ ] Stop/release the artifact before the 2-second quiet observation; keep capture alive, then audit its full retained snapshot for all IDs, unexpected/duplicate deliveries and violations. Preserve immutable raw bodies, callback records, image/container identity and bounded logs before validating, including failures; recheck artifact identity. Create `cases/{cell}/{mode}/{case-index}/receipt.json` and bounded sibling raw files without replacement, and `case-index.json` containing their hashes, required case identities and final audit result. Derive `ContractEvidence` expected totals from the matrix, never hard-coded counts.
- [ ] Run GREEN plus actual Task 2 HTTP tests. Commit `feat: qualify packaged function and watchdog contracts` including only this task's changes.

## Task 5: Product workflow and qualification after successful cleanup

**Files:** Create `src/plans/contract.py`, both declared presets, `tests/plans/test_contract.py`, `tests/cli/test_contract.py`; modify `src/cli/product.py`, `src/cli/catalogue.py`, `tests/test_installed_presets.py`; create `docs/artifact-contracts.md` and link it from existing product docs.

**Interfaces:** Produce `build_contract_plan(config: ScenarioConfig, bindings: RoleBindings, *, repo_root: Path, environment: EnvironmentConfig, run_dir: Path, scenario_path: Path) -> Workflow` and `finalize_contract_run(run_dir: Path, *, executor: CommandTaskExecutor) -> Path` returning a newly created `qualification.json`. Consume Task 3 cleanup validator and Task 4 receipts. Thread optional `scenario_path: Path | None = None` through existing `_workflow` and `_build_run_workflow`; supply the actual resolved scenario for contract plan and run callers. Other workflows retain defaults.

- [ ] Write RED product tests:
  ```python
  def test_release_failure_prevents_qualification(cli, failed_release_run):
      assert cli.run_contract(failed_release_run).exit_code != 0
      assert not (failed_release_run / "qualification.json").exists()
  ```
  Add `test_plan_has_no_platform_side_effects_or_marker`, `test_forbidden_options_rejected_before_acquisition` (keep/resume/teardown/only/start/until/endpoints/push/providers/backends), `test_missing_or_changed_receipt_prevents_marker`, `test_fresh_attempt_cannot_overwrite_marker`, `test_subset_reports_exact_matrix`, `test_contract_skips_control_plane_provisioning`, `test_final_marker_follows_all_resource_releases`, and installed-preset resolution tests. Assert marker contains source/corpus identities, selected/excluded cells, derived counts, receipt hashes and verified cleanup.
- [ ] Run `uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_contract.py packages/nanolab/tests/cli/test_contract.py packages/nanolab/tests/test_installed_presets.py --no-cov`. Expected RED: missing workflow/presets/finalizer; same command GREEN.
- [ ] Build the graph from existing source resource and Tasks 3–4. Planning only reads inputs/constructs the graph. Reject unsupported settings/options before provisioning; contract must bypass existing control-plane provisioning and forwarders. Source acquisition precedes Docker resources; capture must outlive runtime shutdown/audit. Do not make qualification a task whose surrounding resources are still acquired.
- [ ] Call `finalize_contract_run` in the contract execution branch only after `Workflow.run` returns and all owned resource releases finish. Read `matrix.json`, `owned-resources.json`, `case-index.json`, case receipts and release observations; independently check absence, complete expected matrix and immutable receipt hashes. Atomically create the marker without replacement. Any run/finalization failure gets existing failed run metadata and nonzero exit. Pure tests remain pure; actual checkout tests have `@pytest.mark.nanofaas`.
- [ ] Bundle full four-family and word-stats smoke presets with explicit contract defaults. Document local-only use, restrictions, actual result meaning, subset qualification, build/callback bounds, evidence paths and exclusions. Run GREEN and existing CLI/source-compatibility/product-doc tests. Commit `feat: expose local artifact contract qualification` including only this task's files.

## Task 6: Installed live qualification and final review

**Files:** Create `docs/superpowers/plans/2026-10-08-packaged-functions-watchdog-evidence.md`; update task checkboxes only when their evidence exists.

**Interfaces:** Consume installed product CLI and all task receipts; produce evidence naming exact NanoLab wheel hash, NanoFaaS revision/patch, architecture, qualified matrix/counts, logs and cleanup proof. No new product API.

- [ ] Build toolkit and NanoLab wheels (`uv build --package tui-toolkit --out-dir <evidence>/wheels`, then `uv build --package nanolab --out-dir <evidence>/wheels`), create isolated venv with `uv venv <evidence>/installed`, install those wheels with `uv pip install --python <evidence>/installed/bin/python <toolkit-wheel> <nanolab-wheel>`, and run `uv pip check` there. Resolve both presets using installed `nanolab plan`; expected valid plans and no acquired Docker resources.
- [ ] Run installed smoke with recorded shipped NanoFaaS source and a fresh owned Docker attempt. Then run installed full preset on the same native host architecture using a new attempt. Use installed `nanolab run --help` for exact existing scenario/environment/run-directory flags; record exact commands. Expected current matrix: 27 cells, 119 HTTP cases, 18 real bash one-shot cases, 119 artifact callbacks (startup probe separate), zero warm callbacks, exit 0 and cleanup-backed marker. If frozen catalog differs, explain derived counts; if blocked/partial, retain failures and do not claim full qualification or alter NanoFaaS to manufacture a pass.
- [ ] Inspect independent absence of owned containers/network/image tags/builder and unchanged operator source/selected builder. Resolve both installed presets again from outside the checkout. Receipt bounds, native identities and final post-shutdown duplicate audit must be present. Any omitted case or failed cleanup leaves this live gate incomplete.
- [ ] Run original package gates with exact configurations: `NANOFAAS_ROOT=/tmp/nanolab-61-pinned uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml --cov-config=packages/nanolab/pyproject.toml packages/nanolab/tests` and `uv run --frozen --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml --cov-config=packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests`; verify pinned checkout remains `e7914be065e844776af57fe9e449bce7f12e03c5` first. Expected functional tests pass; report actual original coverage gates separately. Existing NanoLab branch coverage is 86.62%, below unchanged 90%; statement-only CI success does not replace this gate.
- [ ] Run `uv run --frozen --all-packages --all-groups pre-commit run --all-files`, `uv pip check --python .venv/bin/python` and installed dependency check. Expected hooks/types/dependencies pass, unchanged public dependency versions. Obtain final whole-branch review using the selected execution skill, including all five Review Focus conditions; fix material findings through RED/GREEN and repeat affected live verification only when changes warrant it.
- [ ] Commit verified evidence and update the existing NanoLab PR #76 on `fix/operational-validation` under the user's existing push/PR authorization. Preserve incomplete gates and upstream findings explicitly. Do not merge, publish images or close #54; CLI shipped-source qualification and Rust #57 remain separate.

## Plan self-review

Checked inline against the approved spec: selection/corpus/payloads → Task 1; actual bounded capture/probe → Task 2; frozen builds, native identity, ownership/compensation → Task 3; HTTP/callback/PNG/watchdog/log semantics and late audit → Task 4; product restrictions, immutable receipts and post-cleanup marker → Task 5; installed full matrix, original gates/dependencies, final review → Task 6. The five Review Focus conditions have named test owners. Shared types and producer/consumer signatures agree; no separate build engine, dependency change or upstream patch is planned. Remaining approval is written-plan review and execution-method selection.
