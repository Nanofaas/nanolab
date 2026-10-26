# Weekly review fixes implementation plan

> **For agentic workers:** Use test-driven-development and verification-before-completion; independent domains may use dispatching-parallel-agents.

**Goal:** Resolve all six reproduced defects, fresh-attempt resume semantics, and four simplifications from the review of `5845dba..b6191fe`, as authorized in this conversation.

**Architecture:** Keep the existing workflows and dependencies. Preserve generator evidence independently of successful load completion; use attempt-specific remote storage so failed transfers cannot destroy earlier evidence. A new experiment attempt invalidates the selected receipt while preserving the previous receipt with its attempt. Share only existing concrete verification operations.

**Tech stack:** Python 3.12, Sonata 0.6.16, pytest, existing Node harness for k6 JavaScript.

**Spec:** The preceding review and user's instruction to address every finding. Implementation decisions: positive integral arrival rates; no-arrival replay produces `no-window`; duplicate forecaster labels are rejected before output publication; a failed fresh replacement remains pending.

## Constraints and review focus

- No new dependencies, live infrastructure operations, or changes to the NanoFaaS checkout.
- Preserve the original execution error if evidence recovery also fails.
- Retrieve the ledger even if the generator or summary retrieval fails; retain inaccessible remote attempts.
- Preserve old evidence and receipts when an explicit replacement fails.
- Malformed successful HTTP responses must still emit a terminal event.
- Model-specific validation must agree between observation and replay; empty and duplicate model inputs must not fabricate measurements.
- Run focused regressions first and the full pure suite once after integration, with the existing Node binary on PATH. CI-pinned NanoFaaS contracts are unavailable locally.

## Tasks

- [x] Evidence recovery: `plans/loadtest.py`, `tasks/loadtest/__init__.py`, workflow/plan tests. First reproduce killed k6 and failed summary fetch; then ensure recovery on failure and remote attempt isolation without replacing the original error.
- [x] Forecast replay: `tasks/one_shot/forecast_replay.py`, `tests/one_shot/test_forecast_replay.py`. First test empty arrivals, repeated labels, and Holt history=4; then add minimal guards and provider-specific validation.
- [x] Experiment contracts: `config/experiment_01.py`, k6 asset, `cli/comparison.py`, `comparison/experiment_01.py`, related tests. First test fractional rates, malformed responses and failed fresh replacement; then fix, replace manual logfmt scanning with JSONDecoder, and delete unused VU-budget code.
- [x] Verification simplification: `plans/runtime_comparison.py`, `tasks/recipes/kubernetes.py`, related tests. Reuse common image/registry/metadata checks; first test repeated immutable-image config resolution, then cache within the operation/run scope.
- [x] Integrate, run Ruff/types/import checks and full pure tests, review the complete diff independently, and document final evidence.

## Progress

- Baseline: clean checkout at b6191fe; prior review ran 4,749 pure tests successfully (22 rerun with installed Node on PATH). Dedicated branch created in the shared checkout.

- Recovery regressions: six RED cases, then task/plan checks passed; full-plan tests cover killed k6 and missing summary. Real prepare commands preserve the previous ledger for both default and explicit remote roots.
- Forecast regressions: 12 RED cases; 56 focused tests passed. Empty replay at a 2026 anchor completes in milliseconds.
- Experiment regressions: fractional rates, malformed response terminals, and failed fresh replacements observed RED; focused suite passed 101 tests (16 unavailable checkout contracts skipped).
- Image verification: repeated export test observed RED; 84 focused tests passed (4 checkout skips), including wrong identity on a second node after cached config extraction.
- Independent whole-diff review: no important issues; added the suggested real cleanup-retention test.
- User steering: new subprocess calls in tests use Sonata's SubprocessShell (subprocess-toolkit), matching the existing execution layer.
- Full pure suite: 4,840 passed, 35 skipped, 393 deselected, 20 subtests passed; one pre-existing simulated-VM test failed because it read real free host RAM. Reproduced with host_capacity=(128, 1), reused simulated_one_shot_host, and verified RED-to-GREEN under that constrained capacity.
- Final regression sweep of all changed areas: 550 passed, 51 checkout-dependent skips. Includes the stabilized CPU test and SubprocessShell-backed JS/cleanup checks. No production changes followed the full suite except the Endpoint type annotation correction.
- Ruff, formatting, import contracts (5/5), Bandit and git diff --check pass. Independent review found no important correctness issues. Native/Azure/Kubernetes execution and unavailable CI-pinned NanoFaaS contracts remain outside local verification.
