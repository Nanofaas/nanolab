# B4: pressure offload versus local-only execution

Research question: at a fixed edge resource budget and identical offered work,
when does forwarding synchronous requests to a terminal cloud improve valid
completion probability and goodput, and what full-response latency does it cost?

Scope: the controller runs on a DGX Spark, with one workflow-created Multipass
edge VM (4 vCPUs, 12 GiB) and one separate Multipass generator (2 vCPUs, 4 GiB).
The terminal cloud is a workflow-created Azure Spot Standard_D8s_v5 VM in
maurinoRicerca-rg, westeurope. Controller results live under /data. Cloud ingress
is restricted to the DGX public NAT address discovered over SSH; the edge and
generator must share that egress address. No inbound WAN access to the DGX is
needed. No VMs are launched by this preparation change. Reuse offload-loadtest
and Sonata platform/provisioning; never introduce a second infrastructure runner.
Keep the historical smoke preset. Operational steps: [pilot runbook](../../b4-offload-pilot.md).

Chosen approach: opt-in offloadExperiment configuration on the existing workflow,
one measured arm per fresh invocation. Matched disabled/pressure arms use the same
word-stats-java medium corpus and exact oracle, fixed resources, fixed concurrency,
explicit SYNC_QUEUE and zero application/client retries. A cloud-direct arm is a
capacity diagnostic, not a main treatment. A Python preparation tool writes unique
scenario/environment paths and a nine-arm provisional pilot manifest, alternating
paired treatment order across rates. Calibrated replication remains a separate
protocol decision. No silently ignored options.
Alternatives rejected: repeating the old 60s two-function smoke cannot answer the
question; replacing one-shot would measure a different mechanism.

Qualification/warmup and measurement are separated. Preserve a per-request ledger
including full response body, status, offload marker, input identity, globally
unique iteration within phase, client elapsed duration and HTTP duration. Preserve
k6 summary/exit, all generator omissions, raw before/after exposition and labelled
Prometheus history for both nodes, deployment/image/resource inventories and logs.
Collect from the corresponding VM over the existing role executor. Always retain
available evidence before failing an arm. Missing samples or absent files are not
zero. HTTP 200 must satisfy the exact body oracle. Offload errors stay outcomes;
scientific success is never a prerequisite for keeping data.

Primary denominators: scheduled requests and emitted requests, both reported;
valid completions/s over scheduled measurement duration. Report refused, transport,
HTTP and invalid-body outcomes separately, and valid full-response p50/p95/p99 overall and by
local versus offload marker (an observed proxy path, not physical exactly-once proof).
Edge success includes cloud success: do not add them. Cloud-direct requests and
setup work must never enter the primary edge comparison.

Pilot rates are provisional (20, 100, 300 rps). Use measured baseline capacity to
freeze the final matched rate grid; do not infer capacity from old hardware. Main
campaign uses three paired repetitions, alternating order, same input rotation.
Keep original failed/interrupted arms, preserve VM eviction failures and assign a
new attempt directory rather than overwriting. Fixed Azure region/VM class and
actual image identity are part of provenance. WAN effects are included in
full-response latency, not separately estimated or controlled. No inference about WAN sensitivity or optimal routing.

Review defects to fix: ignored k6 verdict; zero-valued failure meters rejected;
forbidden retries/offloads accepted within tolerance; absent required meters;
Azure cloud ingress; unsupported remote topology; inaccessible/discarded raw
metrics; experimental parameters/resources silently ignored.

Generator boundary rule: retain nominal rate × duration and actual scheduled
requests separately. A phase may have at most one fully accounted extra boundary
iteration. Exact ledger versus k6 iteration counts and tagged drop reconciliation
remain mandatory; completion probability uses the actual scheduled denominator.
