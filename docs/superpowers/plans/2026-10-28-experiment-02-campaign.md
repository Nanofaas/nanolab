# Experiment 02 campaign preparation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** prepare correctness-only pilot/campaign reporting without unnecessary thresholds or new orchestration.
**Architecture:** reuse compare, calendar, receipts and summaries; add one offline aggregation/report path and the pilot inputs. Keep experiment 01 behavior.
**Tech Stack:** Python, existing Pydantic/Typer and standard-library statistics/random; existing quantile helper.
**Spec:** docs/superpowers/specs/2026-10-29-experiment-02-campaign-design.md.

## Global constraints

- Author approved plan 4 and correctness-only service on 29 October; no latency bound L.
- p50/p95/p99 use all correct completed responses, including slow responses.
- No new dependencies, orchestration framework or adjustable quality gates.
- Preserve operational limits, correctness, provenance and receipt integrity.
- No Azure creation/start/deletion without a separately approved resource proposal.
- No campaign before frozen-scenario approval; no push/merge; nanoFaaS is read-only at CI pin.
- Preserve historical scenario loading and experiment 01 results; do not rewrite archived run evidence or operator paper sections.

## Review focus

- Wrong but fast HTTP 200 responses must not lower reported correct-response latency.
- Missing/OOM/startup-failed runs must remain visible, never become zero resource costs.
- A single successful repetition must not imply a precise confidence interval.
- Offline reports must reject changed receipt evidence without touching VMs or raw input files.
- Reports must use the saved configuration labels, order and identities, not today's platform versions.

### Task 1: correctness-only experiment 02

Files: config/experiment_01.py, config/experiment_02.py, comparison/experiment_01.py, comparison/experiment_02.py, assets/presets/scenarios/experiment-02-pilot.yaml and corresponding config/summary tests (all under packages/nanolab).

Interface: shared ExperimentCalendar; Experiment01Protocol retains required criteria/frozen generator; Experiment02Protocol has optional legacy criteria. Shared summarize_phases gains a correctness-only service mode, used by experiment 02; experiment 01 keeps its default behavior.

- [ ] Add test_frozen_experiment02_needs_no_service_or_generator_thresholds; assert frozen accepts omitted criteria/generator with the existing PNG hash, while frozen experiment 01 still rejects absent generator.
- [ ] Add test_experiment02_uses_all_correct_responses_for_latency_and_efficiency: mix a slow correct response, fast correct response, fast wrong output, timeout and pending; correct throughput/fraction and latency use the correct population, errors stay in sent denominator; zero useful cost remains null.
- [ ] Run tests and observe failures, then minimally implement shared calendar and correctness-only summaries. No experiment-01 output change.
- [ ] Remove objective fields from pilot preset, fix PNG hash, retain common operational calendar. Update existing experiment-02 summary expectations for the new service definition.
- [ ] Run config/comparison/generator tests, types and lint; commit.

### Task 2: run-level aggregation

Files: comparison/experiment_02_campaign.py (new), tests/comparison/test_experiment_02_campaign.py (new).

Interface: aggregate_experiment_02(summaries, cells, protocol, *, repetitions) -> dict; report describes reference budget and observed budget grid, not threshold-qualified feasibility. One metric contains mean, confidence95, sampleCount and missingRuns. Bootstrap resamples run values with the protocol seed; reuse tasks.one_shot.statistics.quantile.

- [ ] Tests: two run values 10/30 give mean20 and bootstrap interval10/30, regardless of request counts; one value gives no interval; nulls stay null; OOM/startup/incomplete counts remain visible; duplicate repetitions are rejected; input order does not change seeded results.
- [ ] Observe RED, implement minimal grouping and bootstrap, then GREEN. Include idle/readiness, correct throughput/fraction/latency, errors and complete-deployment/engine/observer resource metrics.
- [ ] Commit after targeted tests and static checks.

### Task 3: existing-run report and compare integration

Files: comparison/experiment_02_campaign.py, application/experiment_02.py, cli/comparison.py; new CLI report test plus existing comparison tests.

Interface: write_experiment_02_campaign_report(root: Path) -> Path. Read the real saved manifest, reconstruct its matrix using recorded variants/configurations, validate receipt digests, summarize available cells and write analysis/{experiment-02-campaign.json,experiment-02-reference.csv,experiment-02-budget-grid.csv}. New command compare-report RUN_DIR invokes only that function. Existing compare invokes it after its existing report.

- [ ] Tests: offline command works with no NANOFAAS_ROOT/VM provider; source labels/publication identities preserved; missing cells reported; tampered evidence rejected; input evidence hashes and mtimes unchanged.
- [ ] Observe RED, implement the narrow CLI/report integration, GREEN; keep existing CLI behavior and experiment-01 tests passing.
- [ ] Commit.

### Task 4: pilot preparation and verification

Files: docs/experiment-02.md; operator pilot scenario/resource proposal in paper notes, not archived runs.

- [ ] Record the 11-configuration, one-budget, one-repetition pilot and deterministic cell-time estimate. Present a separate bounded Spot resource/cost proposal before any allocation; collect Fn/tinyFaaS residual evidence during that pilot.
- [ ] Document metric changes, descriptive grid, offline reporting, pending paper definition changes and campaign approval boundary.
- [ ] Run uv sync, pre-commit, appropriate focused tests, complete Linux pure tests and real CI-pin contracts, combined coverage above86%; run TUI checks. Use system Python/Node/util-linux and native Linux temporary files.
- [ ] One fresh whole-branch review; fix important findings with RED/GREEN and required checks. Commit documentation; retain branch locally.
- [ ] Request only the concrete Azure resource approval still needed. Pilot/campaign results and paper numbers cannot be fabricated or inferred from unit tests.
