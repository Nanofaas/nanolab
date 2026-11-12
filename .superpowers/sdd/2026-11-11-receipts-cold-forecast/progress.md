# SDD ledger — plan: docs/superpowers/plans/2026-11-11-receipts-cold-forecast.md
Native work authorized. Original checkouts preserved; working in /private/tmp/nl-nanolab-receipts, /private/tmp/nl-nanofaas-20261111, /private/tmp/nl-review-workbench.
Ruling: proceed without another design permission loop: user explicitly delegated design and approved log receipts plus cold fallback.
Pre-flight: receipt schema connects Tasks 1 and 3; journal source connects Tasks 2 and 3. Full retention still bounded by disk budget.
GitNexus does not index Rust occupancy; invoke resolved unrelated JS. Graph cannot establish safety; inspected Rust callers directly. Java index also stale, augment with source/tests.

## Implementation and verification

- SDK: one structured release receipt per attempt, final response classification before reservation release. Rust suite: 111 passed (100 unit + 11 integration). Error-output regression exercised.
- Workbench: optional journal mounts in the existing collector role; no new service. Linux stopped-collector/container-removal test: 50/50 receipts, passed in 27.25 s. Production NanoFaaS config plus Nanolab correlation: 50/50 after producer deletion, passed in 22.76 s.
- Nanolab: isolated receipt correlation module, reused paged Loki exporter, raw events retained; physical evidence still checked against terminal identity, node, attempt and schema. Timeout ingestion regression RED then GREEN. Strict numeric schema version test RED then GREEN (11 receipt tests).
- Forecasting: pre-traffic windows stay MISSING; genuine idle windows after traffic still decay. Cold nodes reserve bounded local capacity, close peer phases without trading, then route local-first/cloud-overflow. No protocol version bump.
- Java suites before the heterogeneous-memory follow-up: execution-runtime 362 tests, forecasting 42, offload 171, control-plane 885 (3 skipped); no failures/errors. Full affected suite rerun after callback attribution correction passed.
- Nanolab broad Linux checks: 2050 passed, 38 skipped, 20 subtests. Latest receipt/install/platform/queue focused run: 73 passed before one additional schema validation regression (11 receipt tests now pass).
- Workbench collection packaging/install: 4 passed. Collector ansible-lint: 0 failures/warnings. Nanolab types: 0 errors/warnings; six import contracts kept; modified Python lint passed.

## Review rulings

Read-only reviewer found three concrete issues: timeout receipts collected too early, receipt outcome recorded before output validation, callback-first LOCAL_THEN_CLOUD success missing node attribution. All reproduced with regressions and corrected. Cloud identity is not synthesized as local.
Final source review also exposed unused capacity for heterogeneous function memory. Regression expects 32 small replicas/0 oversized replicas within a 512 MiB node; original returned 1/0. Allocation now repeats bounded batches over functions that still fit.
Fresh GitNexus index at the implementation checkout includes Rust and Java. detect_changes reports critical aggregate impact across admission, completion, forecasting and epoch preparation (24 flows); reviewed the completion callers and ran their owning module suites. This is not a claim that the graph proves safety.

## Delivery limits

Workbench 0.4.0 metadata and Nanolab pin are prepared together. Public tag/image not released; local Ansible package used for integration verification. Publish Workbench before enabling the new Nanolab dependency in a fresh environment. NanoFaaS must be built from this branch to supply journal config and receipt events.
No new five-edge campaign, native-image build or scientific acceptance is claimed. The journal/collector have bounded retention; abrupt process/host loss and disk exhaustion remain detectable incomplete evidence, not guaranteed exactly-once delivery.

Final follow-up: full offload suite 172/172 passed after heterogeneous-memory regression; reviewer confirmed all three original findings resolved and no new concrete issue in the allocation loop. Final graph check: 77 changed symbols / 24 affected flows, no partial/truncated flag.
