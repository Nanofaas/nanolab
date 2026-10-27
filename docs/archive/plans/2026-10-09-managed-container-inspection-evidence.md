# Managed Docker replica inspection evidence — 2026-10-09

NanoFaaS main includes a function-name hash in managed Docker container names.
NanoLab's recipe image and resource checks previously constructed the legacy
`nanofaas-<function>-r1` name, so the standard validation workflow failed even
when the correct function was running.

Both checks now share label-based selection using `io.nanofaas.managed=true`,
`io.nanofaas.function=<registration name>` and `io.nanofaas.replica=<index>`.
They require exactly one running container and a full 64-character ID. Inspection
uses that ID and rechecks ID, labels and running state before comparing the image
or CPU/memory fields. No fallback to a guessed name or first matching container
is allowed. Compose control-plane inspection remains unchanged.

## Standard installed-wheel qualification

The unmodified `build_validate_plan` workflow ran from an installed NanoLab wheel
on the native ARM64 Docker host with clean NanoFaaS source
`b1aa7f65c7ed0a70c2b94f37092fe62b7e877934`. It selected four Rust functions
(`word-stats`, `json-transform`, `roman-numeral`, `qr-code`) and Go `word-stats`.
The recipe included `container-deployment-provider` and `build-metadata`.

All **33 tasks passed**, including recipe metadata, control-plane image,
registration, invocation, function images, resources and resource releases.
A verification-only executor delegated every command unchanged to the normal
host executor and recorded the actual Docker queries and inspections. It did
not replace, skip or weaken any workflow check.

Each function produced two full-ID inspections: one for its image and one for
its quotas. Independent assertions verified all five hashed names, the three
labels, running state, matching IDs, and images from `distribution.json`.
Every function had the declared values:

| Docker field | Observed value |
| --- | ---: |
| `CpuShares` | 256 |
| `NanoCpus` | 750,000,000 |
| `MemoryReservation` | 67,108,864 (64 MiB) |
| `Memory` | 134,217,728 (128 MiB) |

The workflow removed all five function containers, Compose and its registry.
The verification process subsequently removed only the six run-specific image
tags, after checking their IDs against the distribution report. The preexisting
`minikube` and `buildx_buildkit_nanolab-heap-analysis0` containers remained stopped.
The operator's NanoFaaS tracked files and all 19 preexisting untracked files were
verified unchanged.

Raw evidence is under `/tmp/nanolab-container-inspection-evidence`: `scenario.json`,
`recipe.yaml`, `provenance.json`, `result.json`, `docker-observations.json`,
`independent-verification.json`, `cleanup.json` and `validate-main/recipe/distribution/distribution.json`.
The qualified wheel SHA-256 is
`eea5e9bf8f78966363d869d2c7d596089d54d58efed0481d24deb7cfc7bb1cc1`.
The final Docker implementation matches the installed wheel; its only Python
file difference is restoration of an unrelated containerd type annotation to
the base revision, recorded in `wheel-comparison.json`.

The preexisting image-receipt filename uses component kind and family, so Rust
and Go `word-stats` share a receipt path. Both checks passed, and the ten recorded
Docker observations independently retain evidence for both runtimes. Receipt
filename redesign is outside this bounded inspection fix.

## Regression verification

The initial 18 tests reproduced the defect before implementation. The final
29 regression cases cover legacy and hashed names, missing and ambiguous
replicas, mismatched ID/labels/running state, truncated IDs, replica two,
inspection without resource declarations, and workflow fingerprint changes for
function, replica, quotas, role, executor binding and working directory.
Existing image-mismatch and all four quota-mismatch checks remain active.

The final targeted suite on the CI-pinned NanoFaaS source passed **132 tests**;
the final regression file passed **29 tests**. The full suite passed **3,920
tests with 3 skips**. Its command exited unsuccessfully solely because branch
coverage was **86.48%**, below the unchanged **90%** threshold. The preceding
Rust integration already reported 86.52%, so the coverage gate remains existing
debt; no threshold or CI setting was changed. All **15 pre-commit hooks passed**.
Logs are `targeted-final.log`, `green-final.log`, `full-tests-final.log` and
`pre-commit.log` in the evidence directory.

An independent read-only reviewer found no Critical, Important or Minor defect,
independently passed 45 inspection/resource tests, and recomputed all ten live
observations against the distribution images and requested quotas. Its final
review accepted the bounded correction with the existing coverage limitation
explicitly disclosed.

Kubernetes/containerd inspections, namespace expansion and broader Rust
operational qualification are outside this correction.
