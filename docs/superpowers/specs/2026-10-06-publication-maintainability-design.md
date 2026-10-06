# Publication maintainability (#59)

The issue asks for maintainability improvements before publication while preserving
operational behavior. Apply independent, reviewable changes to the existing flows.

## Design

Move environment-to-VM translation, executor bindings and endpoint resolution to
`nanolab.application`. Existing CLI import paths remain compatibility aliases.
Move function resolution there too, and frozen soak composition next to the
runtime resources it composes. Forbid tasks/release/comparison/application imports
of CLI, app and TUI, and tasks/release imports of plan builders. Release request
data belongs to the release package, not its plan builder.

Share pure k6 interpretation and Prometheus statistics in `nanolab.metrics`.
Support summary-export and handleSummary shapes, prefer `rate` for rates, reject
invalid required values and render missing optional observations as unavailable.
Preserve counter labels through reset detection; gauges may decrease. Invalid
samples must not fabricate zero work.

Extract responsibilities from large modules where the existing functions already
form a cohesive unit: soak process observations, offline evidence validation,
load-test policy, and CLI configuration/catalogue rendering. Preserve public
entry points rather than impose arbitrary file-size targets.

Reuse Sonata's Ansible argv builder. Keep bootstrap operation wrappers only where
they carry execution-target and provider retargeting behavior. Remove unused
template and constants after checking imports and package exports.

Make source-independent tests run with no NanoFaaS checkout. Explicitly distinguish
checkout contracts from pure tests, keep frozen git archive behavior when a real
checkout is supplied, and exercise installed wheels outside the source directory.

Document the tested NanoFaaS revision and diagnose unsupported explicitly selected
function runtimes. Discovery of supported functions must tolerate other runtime
directories; implementing Rust remains #57. Add README metadata and a compatible
`tui-toolkit` range. Cloud extras are evaluated against unconditional imports;
do not create extras that still require the provider packages at import time.

## Verification

Use realistic k6 shapes, reset/gauge/missing/nonfinite cases, existing negative
Kubernetes contracts, installed wheel catalogue/preset/output checks, import
contracts, lint, types and both package suites. Verify against CI's pinned
NanoFaaS source exported under `/tmp`; never modify the operator checkout.
