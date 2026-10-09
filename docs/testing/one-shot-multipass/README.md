# One-shot Multipass workflow validation

This dossier qualifies local workflow plumbing, not Azure or algorithm performance.
NanoFaaS runtime source: `b1aa7f65c7ed0a70c2b94f37092fe62b7e877934`.
NanoLab base: `811fbddefca8949a823bbcc5c3cacb95d4125acb`.
The VM nodes share the physical ARM64 operator host with other workloads.

## B2: deployed preflight

`b2-deployed6/preflight.json` records the actual three independently provisioned
VMs, discovered peers, physical concurrency/release proof, CPU/RAM/image checks,
clock uncertainty and cross-VM transfer measurements. Clock-derived RTT is the
operator-to-node probe round trip; transfer bandwidth is cross-VM.
`b2-deployed6/clock.jsonl` records fresh periodic measured clock reports accepted
by the edge APIs. `sonata.jsonl` records acquisitions, tasks and reverse releases.
The operator clock reports NTP synchronized.

Build: Sonata `recipe_run_resource` + `assembled_recipe_distribution_resource`,
using bundled `one-shot-jvm.yaml`, frozen source above, tag `one-shot-b1aa7f65`.
Run: `NANOFAAS_ROOT=/tmp/nanofaas-one-shot-source UV_CACHE_DIR=/tmp/nanolab-uv-cache
uv run --frozen --all-packages --all-groups python /tmp/oneshot-b2-deployed-periodic.py`.
This temporary diagnostic composed `add_one_shot_platforms`,
`CollectTopologyTask` and `clock_health_resource`, waited through a refresh,
and used a Sonata journal. The final public CLI reproduction follows in B6.

Host Docker stores build configuration IDs; VM Docker 29 stores OCI manifest
IDs. Both identities are cryptographically bound through the Docker-save archive.
Target registrations use the actual immutable target image IDs.
Large image archives are retained locally and excluded from Git.

Earlier failures were preserved during diagnosis: an unacquired bootstrap host,
unnamed Docker-save images, config/manifest ID semantics, and an unscoped managed
container label. Regression tests cover each; all owned VMs were released.
`b2-import-diagnostic.json` records the image-store diagnosis.
Pre-existing `nanofaas-stack` and concurrent `lb-worker-*` VMs were preserved.

## B3: independent service calibration

`b3-calibration` retains 144 raw warm service samples, readiness/warmup records,
per-node seeded bootstrap statistics and six capacity configurations (one/two
replicas on each of three VMs). Mean physical occupancy is approximately 111ms.
Capacity-model error was below 2.8%; declared tolerance was 35%, confidence 95%,
relative mean-interval tolerance 15%. Profile v1 schema validation passed.
The profile aggregates homogeneous local nodes while retaining node statistics.
VM OS/architecture, host CPU and image/input/resource identities are fingerprinted.
The profile and its prerequisite reference are immutable bytes.

Run: `NANOFAAS_ROOT=/tmp/nanofaas-one-shot-source UV_CACHE_DIR=/tmp/nanolab-uv-cache
uv run --frozen --all-packages --all-groups python /tmp/oneshot-b3-calibration.py`.
Builder: `build_one_shot_calibration_plan`, bundled calibration preset and the
B2 immutable runtime distribution, Sonata journal in `/tmp/oneshot-b3-real2`.
The initial attempt's SDK response-envelope mismatch and complete VM cleanup
are retained in `prior-failure.json`; its regression reproduces the raw integer
HTTP response. No NanoFaaS runtime change or implicit synthetic sample was used.
