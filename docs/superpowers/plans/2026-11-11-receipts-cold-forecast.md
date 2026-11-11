# Receipts and Cold Forecast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Native execution was already requested.

**Goal:** Preserve physical execution evidence across replica deletion and use local capacity when forecasts are unavailable.
**Architecture:** SDK JSON receipts → Docker journal → existing Fluent Bit → Loki → Nanolab correlation. Explicit missing forecasts trigger local admission with cloud overflow.
**Tech Stack:** Rust, Java, Python, Ansible, Docker, journald, Fluent Bit, Loki.
**Spec:** ../specs/2026-11-11-receipts-cold-forecast-design.md

## Global Constraints
No new application datastore, no auction version bump solely for telemetry, no ten-VM run. Preserve identity checks, source artifacts, user checkouts and bounded resources.

## Review Focus
Release before response classification; timeouts with live blocking handlers; deletion during collector outage; cross-run and duplicate identities; cold fallback exceeding physical capacity.

### Task 1: Emit released receipts
Files: NanoFaaS sdks/rust/src/{occupancy,invoke,limits}.rs and tests.
Interface: runtime.execution.released JSON attributes, receiptSchemaVersion=1, executionId, incarnation, dispatchAttempt, state, occupancySeconds, responseStatus, handlerStarted.
- [x] Test actual log emission after release, once per attempt, including late release after timeout.
- [x] Implement at physical release; retain response classification before final reservation drop.
- [x] Run Rust SDK suite.

### Task 2: Preserve logs independently of replica lifetime
Files: NanoFaaS deploy/observability; Workbench roles/log_collector; Nanolab provision-base.yml, one_shot_common.py, workbench/platform.py.
Interface: optional Workbench journal mounts; owned experiment VMs use journald; existing json-file source remains supported.
- [x] Reproduce lost logs after deletion with stopped collector.
- [x] Configure persistent bounded journal and journal input/cursor, expose through existing collector role.
- [x] Linux test: emit, remove producer, start/restart collector, query Loki and verify all receipts.

### Task 3: Consume receipt logs
Files: Nanolab tasks/one_shot/receipts.py (correlation), experiment.py, logging.py, tests/one_shot/test_receipts.py.
Interface: validated released receipts become existing physical.jsonl rows; no relaxed conservation gate.
- [x] Test terminal ID mapping, duplicate suppression, attempt/node/run mismatch and conflicts.
- [x] Use paged Loki export and receipt correlation; bypass per-request SDK polling for logging-enabled experiment runs.
- [x] Run one-shot and workbench tests.

### Task 4: Cold-forecast local fallback
Files: NanoFaaS forecasting WindowedForecastSource/EwmaForecastSource and offload input/coordination/routing consumers, their tests.
Interface: MISSING forecast remains explicit; local admission before reliable forecast, cloud on exhaustion.
- [x] Reproduce pre-traffic zero and missing-forecast routing.
- [x] Implement minimal policy through existing actuation/admission paths, preserving genuine zero and bounded capacity.
- [x] Test learning transition, local success and cloud overflow; run forecasting/offload suites.

### Task 5: Verify integrated behavior
- [x] Run focused Linux fault tests, relevant repository checks and graph impact/change review.
- [x] Review full diff, document evidence and remaining limits; do not claim five-edge acceptance without running it.
