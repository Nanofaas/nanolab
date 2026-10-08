# Soak teardown and Maven release staging — implementation evidence

Two bounded changes approved as the first priorities after the remaining-work
assessment. Worktree: `/tmp/nanolab-operational-validation`, branch
`fix/operational-validation`, base `55668dd55423d21efb3eb318135e89c8f47b164b`.
The base includes the operator's local NanoFaaS build-layout compatibility
commits. The normal NanoLab and NanoFaaS checkouts are not modified.

## Soak CLI teardown

The CLI discarded `teardown_soak_run()`'s returned Sonata workflow and exited
successfully without executing its tasks. It now executes that workflow.
The existing bounded journal validation, ownership checks and release ordering
remain the cleanup implementation; no second cleanup path is introduced.

The regression starts from actual retained Sonata resources and invokes the
real CLI/plan/workflow/task. Only Docker/HTTP transports are replaced. Successful
cleanup releases the function and Compose project, empties outstanding ownership
records, and repeated CLI replay does no more Docker work. A foreign live image
returns nonzero, preserves the ownership journal and performs no destructive
cleanup. Both new cases failed RED against the original discarded workflow.

Review reproduced two partial-cleanup problems, also fixed here. Platform
identities are recovered from the entire retained history, while only outstanding
functions are released. Before Compose removal the platform is verified again.
Functions still outstanding require the continuously running original catalog;
Compose-only cleanup accepts the owned container stopped, restarted or already
removed. When present, its ID, image, labels and configured endpoint must match.
This preserves recovery after `down` partially succeeds without accepting a
foreign replacement or deleting an already released function again.

Regression evidence: ten foreign-replacement cases failed RED against the first
CLI fix; six owned stopped/absent/restarted cases failed RED against the first
history guard. The final suite covers both cleanup and legacy journals, stopped
foreign replacements, and replacement during function deletion.

This does not change P24 thresholds, builder selection or resource ownership
policies. The other independently assessed parts of issue #52 remain separate.

## Maven coordinates

The staging allowlist now selects `io.github.nanofaas:containerd-java:0.25.0`,
`io.github.nanofaas:containerd-java-cni:0.25.0` and
`io.github.nanofaas:libcni-java:0.24.0`. The independent staging/provisioning
fixtures use only those coordinates. The receipt/filtering test and real
provisioning composition failed RED with the old allowlist and now pass.

The implementation changes only the three coordinates. File filtering,
required JAR/POM completeness, regular-file checks, 64 MiB bound, fresh
destination requirement and SHA-256 receipts retain their existing behavior.

All six real released JAR/POM files were downloaded from Maven Central into an
isolated temporary repository and staged with the normal implementation:
1,935,063 bytes total, all six independent source hashes matching the receipt.
Evidence: `/tmp/nanolab-operational-maven-proof.json`.

## Verification

- Original focused baseline: 79 passed.
- RED: four expected failures, 35 passed (two CLI teardown cases, new-coordinate
  staging and provisioning).
- Initial GREEN focused suite: 81 passed; final focused suite: 114 passed.
- Full NanoLab suite: 3,537 functional cases passed in 303.84s with isolated
  NanoFaaS pin `e7914be065e844776af57fe9e449bce7f12e03c5`.
- Explicit original package branch coverage: 86.23% versus 90%; command exit 1
  solely for the already recorded coverage debt. The threshold is unchanged.
- Toolkit: 51 passed, 93.71% versus its original 80% gate.
- All 15 existing quality hooks pass; dependency versions/lock are unchanged.
- No live VM/cloud provisioning or destructive operator Docker cleanup was run.
  Docker ownership/error behavior is exercised through the command boundary.

The sandboxed rerun blocked in an existing asyncio prerequisite-platform test.
An isolated no-coverage diagnostic reproduced the sandbox block; the identical
module outside the sandbox passed all 19 tests in 0.66s. The complete final suite
ran outside the sandbox with the same source pin and coverage configuration.
Only the task's blocked pytest processes were terminated; no product workaround
or weakened test/coverage configuration was introduced.

## Independent review

The reviewer reproduced both Important partial-cleanup defects above; after the
fixes, the final verdict has no Critical, Important or Minor findings. The
reviewer independently ran 74 affected tests and six additional CLI fault checks
for outstanding functions against stopped, absent and restarted control planes.

Explicit review exclusions and their disposition:

- Native documents and implementation: remain pending user design review and
  subsequent implementation, not qualified by this review.
- Real Docker, cloud and VM resources: not exercised. Live destructive operator
  cleanup is unnecessary for the bounded CLI fix; transport tests verify the
  failure and ownership behavior.
- New-coordinate Gradle build compatibility: not claimed. Issue #72 requests
  repository staging/provisioning acceptance, verified by focused tests and real
  Maven Central files; no consumer build was run.
- Full-suite coverage: independently verified by the coordinator rather than
  repeated by the reviewer. The existing coverage debt remains explicit below.

## Native validation

The separate [native artifact specification](../specs/2026-10-08-native-artifact-validation-design.md)
was presented for user review; the user requested revision and approved a split
into Kubernetes lifecycle, CLI parity and packaged function/watchdog deliveries
now recorded in that document. The [first delivery's Kubernetes design](../specs/2026-10-08-native-k8s-lifecycle-design.md)
is approved; its implementation plan is being prepared. The callback-capture and
contract workflow are not
implemented or qualified by the first two fixes or their tests. Issues #53/#54
remain pending that design and subsequent implementation/live evidence.
