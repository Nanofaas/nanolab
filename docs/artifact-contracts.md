# Packaged function and watchdog contracts

Run the distributed function images directly on a local Linux Docker host:

```sh
export NANOFAAS_ROOT=/path/to/nanofaas
nanolab plan artifact-contract-smoke-container.yaml
nanolab run artifact-contract-smoke-container.yaml --run-dir /tmp/contracts-smoke
nanolab run artifact-contract-container.yaml --run-dir /tmp/contracts-full
```

Every run requires a fresh directory and a native amd64 or arm64 host. Planning
reads the catalog and independent correctness corpus, and acquires no Docker
resources. The smoke preset selects word-stats; the full preset selects
word-stats, json-transform, roman-numeral and qr-code. To narrow a run, copy the
preset and select a family or catalog key in `functions`. Every selected
implementation retains all its image flavors and all its corpus cases. The
qualification marker identifies the exact subset; a subset does not qualify the
full catalog.

The current catalog produces 27 full-preset cells, 119 HTTP invocations and 18
fresh bash one-shot containers. SDK invocations require exactly one actual
callback. Bash warm invocations require zero callbacks; one-shot invocations
require exit 0 and exactly one callback. The expected totals are derived from
frozen source, rather than these illustrative numbers. Outputs and status codes
are checked against the independent correctness corpus; example payload
expectations are checked for consistency and never become the oracle. QR checks
cover PNG structure, dimensions, content type, encoding and identical HTTP and
callback bytes within an invocation. They do not decode the QR text.

The workflow freezes tracked source and the selected scenario, builds one image
at a time with the existing Bake planner, and uses a private Buildx builder,
network and callback helper. It preserves each packaged entrypoint and command, verifies
actual image/container IDs, and verifies ELF architecture and SHA-256 for Java
native executables. No ports are published and no images are pushed. Its
16 GiB/four-CPU builder limits are inspected on the actual builder container.

Default budgets are 2700 seconds per build, 60 seconds for readiness, 30 seconds
per HTTP exchange, 15 seconds for callbacks and two seconds of quiet after
runtime shutdown. A message must be smaller than 2 MiB; diagnostics must be
smaller than 8 MiB. Capture retains fewer than 1000 records and less than 32 MiB
of bodies. Reaching a bound fails qualification. These budgets can be changed in
the explicit `contract` block of a copied preset.

`--keep`, `--resume`, `--teardown`, task selection, external endpoints, release
settings, remote providers, Kubernetes and containerd are unsupported. A failed
run keeps its evidence and exits nonzero. Resource cleanup checks owner labels
and recorded identities, preserves replacements, and independently checks
absence after removal.

The run directory contains frozen `inputs/`, `matrix.json`, exact `images/` and
`containers/` observations, bounded `commands/` and runtime logs, `capture/` raw
callback records, per-case `cases/` receipts and `case-index.json`, `ownership/`,
`identity-bindings/`, and `cleanup/`. `qualification.json` is published atomically
without replacement only after the complete case matrix passes, the final
post-shutdown callback audit passes, every resource release finishes, and an
independent cleanup check succeeds. Missing, changed or failed case evidence
cannot produce a marker. The marker retains source/corpus identities, selected
cells, case hashes, derived counts and cleanup evidence. It is an operational
qualification receipt, not a tamper-proof signature.

Other families, fixtures, service images, Rust, cross-architecture execution and
watchdog FILE/HTTP supervision remain outside this workflow. Control-plane,
Kubernetes lifecycle and CLI parity are separate validation gates.

The installed ARM64 full preset qualifies on NanoFaaS candidate `84a8f0ce`:
all 27 cells and 137 cases pass, with 119 real artifact callbacks and verified
cleanup. This candidate fixes the missing AWT libraries and JNI metadata that
blocked Java QR native on `a234ea1`; its upstream fix is
[NanoFaaS PR #255](https://github.com/Nanofaas/nanofaas/pull/255). The native CLI
fix in PR #253 and the existing NanoLab coverage deficit remain separate; see the
[installed execution evidence](superpowers/plans/2026-10-08-packaged-functions-watchdog-evidence.md).
