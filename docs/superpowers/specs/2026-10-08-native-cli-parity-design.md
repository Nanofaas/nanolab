# Native CLI parity — second native delivery, issue #54

Status: the user approved this written design. The implementation plan is
`../plans/2026-10-08-native-cli-parity.md`, approved and executing
on `fix/operational-validation`. This document is not live qualification.
Kubernetes native lifecycle (#53) is already qualified separately; packaged
function/watchdog contracts remain the third delivery.

## Outcome and source of requirements

An operator can explicitly select the JVM launcher, native executable, or their
parity check through the existing `cli` workflow. Parity means both artifacts
exercise the same command contracts against the same owned control plane, from
the same frozen NanoFaaS source, with independently checked expected behavior.
Successful compilation or equal incorrect responses cannot qualify the run.

The [issue #54](https://github.com/Nanofaas/nanolab/issues/54) reports native CLI
reflection failures in replicas get, control-plane info, YAML function updates
and configuration-file loading. The source confirms that these use separate
deserialization paths. The current NanoLab workflow builds only `installDist`,
and its list/apply/delete checks are weaker than its update/replica checks.
These findings, rather than the proposed issue text alone, define this delivery.

## Alternatives and selected approach

1. **Selected: recipe-backed local Kubernetes CLI qualification.** Reuse the
   frozen recipe, selected Minikube target, verified image import, owned namespace,
   Helm release and API forward introduced or exercised by #53. Add the CLI
   command phase at its existing owner. This requires launcher selection,
   paired execution and evidence, without another deployment implementation.
2. Extend all legacy container/containerd/VM CLI graphs together. This also
   requires native binary transfer/architecture handling on VMs and isolated
   replacements for the local graph's fixed ports, registry and Docker network.
   Those additional environments are not needed for this local acceptance gate.
3. Extend NanoFaaS's standalone native smoke script. It uses a stubbed function
   list and cannot qualify state changes or packaged artifact behavior against
   an owned deployed control plane.

The fixed control plane and Java word-stats function are JVM artifacts. This
isolates the CLI runtime as the changed variable; native control-plane behavior
is already #53's separate evidence. No callback server, SDK matrix, watchdog,
cloud provisioning or new workflow name belongs here.

## Configuration and compatibility

Add `cliRuntime: jvm | native | parity`, defaulting to `jvm`, to scenario
configuration. Non-default values are valid only for `workflow: cli`. Add
`cli-contract-k8s-native-parity.yaml`, selecting `parity`, and a dedicated
`cli-contract-k8s-jvm.yaml` recipe. Recipe-backed CLI scenarios initially require
backend `k8s` and the local environment; unsupported backends, remote environments
and externally supplied endpoints fail before provisioning or builds.

The recipe selects the JVM control plane with `k8s-deployment-provider`,
`build-metadata` and `runtime-config`, plus Java JVM word-stats. The Helm release
enables the runtime-config admin API inside the owned platform. The recipe and
validated distribution must actually provide these modules and modes; the gate
cannot silently skip a required command. Reject incompatible recipe selections.

Existing scenarios without `recipeProfile` retain their JVM behavior. They do
not silently accept a native/parity request. Programmatic builders enforce the
same restrictions as YAML validation. Forward the existing operator run directory
to the CLI planner; planning itself does not build, provision or freeze sources.

## Build and artifact identity

Reuse `recipe_run_resource` and `prepare_recipe_run` to capture tracked inputs
outside the operator checkout. Build the recipe and selected CLI artifacts only
in that snapshot. JVM uses `:nanofaas-cli:installDist`; native uses
`:nanofaas-cli:nativeCompile`. Parity builds both before exercising either.
Use existing native memory/parallelism controls, without changing global policy.

Record source revision, tracked patch fingerprint, recipe identity, host
architecture, artifact paths, artifact hashes and help/version output. Verify
that the native path contains an executable ELF for the host architecture,
not a JVM script. Record the JVM launcher and installed library hashes. Paths
must remain inside the frozen source. Missing, incompatible or changed artifacts
fail; there is no fallback from a requested native executable to the JVM launcher.

Build-only task selection stays offline. A sliced run that omits either parity
side or a required case cannot write a successful parity qualification marker.

## Owned platform and paired lifecycle

Acquire the selected Minikube context through existing pinned bindings. Load
only the recipe's images, using the existing image resource with its queue-probe
input made optional; this CLI delivery needs no sync-queue probe. Preserve the
existing #53 caller and its queue behavior.

Use a dedicated namespace, Helm release and API forward owned by this run.
Verify control-plane metadata and actual image identity before CLI commands.
Both CLI passes use this same control-plane instance. Record and check its
Deployment/pod/container identity across the passes; replacement invalidates the
comparison. Reuse the existing immutable image and metadata checks.

Run the JVM pass, release its function, restore the runtime-config baseline,
then run the native pass with the same manifest, image, payload and settings.
Selected functions start absent, get registered through CLI apply and are deleted
through CLI cleanup. Do not preregister them through HTTP. Restore the controlled
runtime setting through revision-guarded patches and verify its effective value.
The native pass must start from a baseline that makes ignored writes observable.

Capture the acquired API endpoint at execution time, including Resource-backed
forwards. Extend existing CLI command factories to accept the resolved invocation
prefix; preserve ordinary string-prefix callers. Build artifact selection must
be reflected in every command and cleanup, not just the first invocation.

## Command contracts and comparison

Reuse and strengthen the checks in `tasks/cli_function.py`; do not create another
HTTP client or copy the existing update/replace/runtime-config command logic.
Each artifact exercises:

- Help and version, with the version expected from the frozen source.
- Fresh manifest apply, explicit get and list containing the correct name/image.
- YAML update of the existing mutable fields, verified by subsequent get.
- Replica set/get, checking function identity and integer counts, with bounded
  readiness before invoking the workload.
- Invocation using the first deterministic successful case from the frozen
  `functions/test-data/word-stats/correctness.json`. Check success, no non-null
  error and exact expected output, not merely the presence of an output field.
- Changed immutable manifest refused without `--replace`, then correctly applied
  with `--replace`; check the requested change and function identity afterward.
- Control-plane info/capabilities and the existing OpenAPI contract command.
- Runtime-config get, valid/invalid validation and patch/readback. Negative cases
  must exit nonzero with the expected diagnostic; accepting an invalid patch or
  an implicit replacement is a failure even if both artifacts do it.
- An explicit YAML `--config` file pointing at the owned API. Invoke without
  `--endpoint`, with `NANOFAAS_ENDPOINT`/`NANOFAAS_CONTEXT` removed from the child
  environment. Verify actual server access and the expected returned function.
- Delete, verified by absence from a subsequent list; retain the existing cleanup
  compensation and propagate cleanup failures.

Compare parsed observations, not whole terminal output. List is tabular in the
actual CLI: parse and compare the name/image rows rather than demanding JSON.
Function details compare manifest fields, resources, requested/effective modes,
backend and controlled changes. Replicas compare identity and desired/ready
counts. Invocation compares status and the corpus output. Info compares the
reported build and capabilities. Runtime configuration compares controlled
effective values and validation results.

Retain generated endpoint URLs, execution IDs, pod names and configuration
revision counters in raw evidence, but exclude them from cross-pass equality.
Still check their type, ownership or continuity wherever the individual contract
requires it. Do not ignore application output fields or meaningful array order.

## Bounds, evidence and cleanup

Use the existing host command executor and bounded-command pattern: CLI/API
commands have a 60-second outer deadline and each stdout/stderr is limited to
1 MiB. Build tasks use the existing build policy, not the CLI-command deadline.
Keep separate stdout/stderr/exit-code receipts per artifact and case, bounded
to 16 MiB per pass. Retain both failed and successful evidence; a retry allocates
a fresh attempt directory and cannot reuse a previous qualification marker.

Check every retained command output and owned control-plane log for native
registration/unsupported-feature errors, `No serializer found`, Python traceback
and Rust panic markers. Read owned logs before and after each pass, before any
resource removal; each log is bounded to 8 MiB. Reject missing or truncated
required evidence. Expected CLI error diagnostics are not native failures.

Write the final parity receipt only after all required cases, comparisons,
artifact continuity and platform checks pass. Successful workflow completion
also requires every acquired function/forward/Helm/namespace/imported-image
release to succeed. No unrelated resources are removed, no public images are
published, and the operator NanoFaaS checkout remains read-only.

## Verification and completion

Reproduce missing native selection, untested config loading and false-positive
contracts RED before implementation. Cover mismatched artifacts/architectures,
different outputs, equally wrong outputs, ignored updates, another function's
replica response, successful exit on negative cases, contaminated config
environment, registration errors during successful requests, stale attempts,
replaced control-plane identity and failures during either pass or cleanup.

Use independent HTTP fixtures and command-boundary tests for orchestration.
Qualification additionally requires actual JVM/native builds and an installed
NanoLab wheel running the bundled parity scenario against an owned local
Minikube platform. Report missing prerequisites or failed live gates as
incomplete. Run the original NanoLab/toolkit checks, quality hooks and dependency
checks, preserving and reporting the existing 90% NanoLab coverage debt.

This delivery qualifies CLI parity only. Issue #54 remains open until the separate
packaged function/watchdog delivery also has its own live evidence.
