# B5 Runtime Comparison Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution. The user authorized planning and corrections; cloud execution is not part of this change.

**Goal:** Compare Java, Go, JavaScript, Python and Rust word-statistics functions doing the same work under a fixed NanoFaaS deployment policy.

**Architecture:** Reuse the shared loadtest workflow, Kubernetes provisioning, function catalogue, payload corpora, k6 runner, event ledger and cAdvisor queries. Add one single-function load profile and five equivalent presets. Correct the actual handlers and qualify them against the same fixtures rather than compensating for semantic differences in the benchmark.

**Tech Stack:** Existing Python/Pydantic/Sonata, k6 JavaScript, language-native function handlers and tests.

**Spec:** User-approved B5 proposal and NanoFaaS `docs/word-stats-contract.md`.

## Protocol and constraints

- One `word-stats-{java,go,javascript,python,rust}` at a time; one replica, no governor or autoscaler.
- Fixed concurrency 2, queue capacity 20, invocation timeout 30000 ms, maximum retries 3; use async-queue's shared per-function strategy for synchronous invocations.
- Control plane: full-tiered JVM Serial (the default source image), 2 CPU / 2048 MiB. Function: 2 CPU / 512 MiB. Explicit requests and limits for every arm.
- Same medium corpus, deterministic case rotation and one common open-loop schedule. Warmup and staged offered load are descriptive; one pilot is not a maximum-capacity or significance claim.
- Verify output bodies against generated expected results, not merely HTTP200. Keep overload refusals separate from bad outputs and transport/server failures.
- Capture function CPU, working set and separately labelled RSS, CP heap where applicable, queue metrics and complete response accounting. Preserve k6 generator drops separately.
- VM Spot provisioning remains workflow-owned. Future execution uses isolated /data roots, frozen revisions and manifests, unrestricted build CPU/available memory, and complete provider teardown.
- No VM provisioning or changes to running A3/B1.1 in this implementation.

## Review focus

1. Unicode/tokenization and numeric topN semantics differ by language: common adversarial fixtures in every handler suite.
2. Expected corpus results must be derived reproducibly and qualified in all five languages, including every medium input.
3. Profile must not silently select a second function, an adaptive governor or wrong payload/CLI stages.
4. Function requests/limits, concurrency, timeout and retries must be identical at registration; collect function cAdvisor CPU/working-set queries explicitly.
5. HTTP200 with a wrong body is not useful throughput; ledger and counters must distinguish valid completions, refusals and failures.

## Tasks

- [x] Add the shared handler contract and generated correctness/benchmark qualification fixture. Observe failures on the current handlers.
- [x] Align the five implementations and run their native contract tests, retaining SDK transport behavior. Record any unavoidable Unicode-version scope limits.
- [x] Add a single-function runtime profile, fixed registration settings and five presets; reuse the default full-tiered Serial control-plane build and existing module/provisioning machinery.
- [x] Add a shared-corpus load generator with independent scheduled arrivals, exact output validation, preflight qualification and response ledger; reuse response parsing/accounting where compatible.
- [x] Test scenario equivalence, payload mapping, fixed registration, resource telemetry, body validation and invalid configurations; compile all five plans without external actions.
- [x] Run focused language and NanoLab regressions, lint, graph-change review and one final independent review. Update this ledger, commit and publish branches; do not merge or launch unless requested.

## Execution ledger

Planning: old concurrency-cycle presets are governor probes, with inconsistent payload/resource declarations. They will be retained for their original purpose; B5 gets explicit presets. The default source control-plane image is Serial with full tiering, so a new control-plane compiler recipe is unnecessary.

Completed implementation: all five actual handlers share 30 generated cases,
including all four unchanged medium payloads. Native Java, Go, TypeScript/Node,
Python and Rust tests pass; Python has one additional large-integer regression.
NanoLab: 130 focused regression tests pass, including compilation of all five
real workflow plans with a recording executor (zero external commands). Ruff
check/format and diff whitespace checks pass. No cloud run was performed.

Rulings: keep the old correctness fixture for Java-lite/Bash, outside B5. No SDK
transport changes. Rust needs unicode-general-category to exclude alphabetic
marks outside L/N. Unicode table versions remain a documented domain limit;
medium measured inputs are ASCII. Registration/resource resolution reuses the
existing helpers; no new launcher or compare-matrix implementation.

Independent final review found VU-local corpus rotation and Python integer
finiteness overflow; both fixed with focused tests. B5 uses the global scenario
iteration; an audit found the same rotation limitation in historical generators.
See docs/b5-function-runtime.md; do not rewrite frozen evidence or alter ongoing
runs. Generator tests cover invalid bodies, refusals, transport/server failures,
qualification isolation, global rotation and accounting mismatch retention.

GitNexus impact walks for Java/Go/Python/Rust handler entry points were LOW.
JavaScript symbol lookup resolved the handler, but impact-name disambiguation
selected Go; runtime registration and test callers were confirmed by text search
instead of treating an UNKNOWN graph result as proof. Refresh the index and run
detect_changes before committing.
