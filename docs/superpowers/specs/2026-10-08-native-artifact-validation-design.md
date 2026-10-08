# Native artifact validation — issues #53 and #54

Status: the user approved the three-delivery subdivision below. The first
delivery (#53) is implemented, independently reviewed and qualified locally
through installed native artifacts; its existing coverage gate remains failing.
The CLI delivery is implemented and reviewed; its installed candidate passes,
but shipped-source qualification awaits NanoFaaS Context reflection integration.
The function/watchdog delivery is implemented and reviewed. Installed ARM64
WordStats smoke passes; full qualification is blocked by missing AWT libraries
in the NanoFaaS Java QR native image. Original NanoLab coverage also remains
below its unchanged gate. See the packaged-function execution evidence.

## Approved subdivision: three independent deliveries

The previous design combined three separate runtime contracts. The approved
revision preserves their acceptance criteria but delivers and reviews each
independently, in this order:

1. **#53 — Kubernetes native lifecycle.** Reuse recipe validation, add the
   native Kubernetes preset, reject the ignored legacy runtime selector, and
   verify native API response bodies, reflection logs and pod runtime shape.
   This delivery needs no callback server or new artifact-contract workflow.
2. **#54 — Native CLI parity.** Extend the existing CLI build/validation path
   to select the native executable or JVM launcher. Build from the same frozen
   source and compare the existing command contracts against the same owned
   control plane. No SDK matrix is required to qualify these CLI commands.
3. **#54 — Packaged functions and watchdog.** Introduce the local artifact
   contract workflow only here, with owned callback capture and the matrix
   derived from the currently supported catalog and independent corpus.

Each delivery must retain its own evidence and run live artifact checks before
qualification. The CLI delivery alone does not close #54. This subdivision is a
scope decision, not qualification or approval of an unwritten implementation.
The first delivery's design is [native Kubernetes lifecycle](2026-10-08-native-k8s-lifecycle-design.md).
The second delivery's approved design is [native CLI parity](2026-10-08-native-cli-parity-design.md);
its implementation is reviewed on `fix/operational-validation`;
[CLI execution evidence](../plans/2026-10-08-native-cli-parity-evidence.md) distinguishes
the successful patched candidate from the failed shipped source.
The third delivery's approved design is
[packaged functions and watchdog](2026-10-08-packaged-functions-watchdog-design.md).

## Outcome and scope

An operator must know which executable was tested. Native control-plane
validation must select an actual native image or reject the request before
provisioning. Artifact contracts must exercise packaged function runtimes,
their callbacks, the watchdog's one-shot mode, and the native CLI alongside
the JVM CLI. A successful build or HTTP 200 alone does not qualify a contract.

This is the third priority approved after soak teardown and Maven staging.
It introduces a callback-capture lifetime and an artifact contract workflow;
those interfaces require design review before implementation. The first two
fixes are independent and can be delivered without this workflow.

## Baseline behavior before the first delivery

- `build_validate_plan` currently ignores `controlPlaneRuntime`; a native
  Kubernetes request compiles a `:control-plane:bootJar` build.
- Recipe validation already verifies distribution metadata and running image
  identity. Container native recipes work; Kubernetes has a JVM recipe and a
  backend-neutral recipe distribution path that can be reused.
- `build_cli_workflow` always builds `:nanofaas-cli:installDist`.
- NanoFaaS `functions/contract-tests/run.sh` tests handlers in-process. Its
  existing native CLI smoke covers help/version/list, not the full CLI contract.
- `HttpFunctionSetReplicasTask` checks command success but not the returned
  JSON; `HttpFunctionReplicaStatusTask` verifies desired/ready replicas.
- The pinned Helm template already passes the derived event-loop count as a
  native `-D` argument as well as in `JAVA_TOOL_OPTIONS`.

## Proposed approach

Keep normal validation and artifact contracts separate in purpose, reusing
the existing recipe, image-plan, command, process and resource implementations.
Do not add a second build engine or move product contracts into Sonata.

### 1. Native control-plane lifecycle

Add a Kubernetes native recipe/scenario beside the existing JVM scenario.
Keep the ordinary function workload fixed while varying the control-plane
artifact. Reject the unsupported legacy `controlPlaneRuntime: native`
validation spelling with an actionable native-recipe message; do not silently
translate arbitrary selected functions into a fixed recipe.

Exercise registration, patch, deletion, GET/PUT replicas and a deliberately
forced invocation-capacity 429, requiring their JSON bodies. Reuse existing
HTTP tasks where they enforce the contract; strengthen response validation at
its existing owner rather than adding a parallel client. Native log checks
reject reflection/serialization failures. Verify the pod's requested native
arguments, actual event-loop count and Linux transport using a bounded probe
against the owned pod; an unavailable probe is a failed qualification gate.
Property-conditioned capabilities required by the scenario must be present in
the recipe's AOT configuration, not enabled only after native compilation.
Do not add an admin runtime-config exercise unless the lifecycle needs it.

### 2. Packaged function and watchdog contracts

Add an explicit local Linux/Docker `contract` workflow and bundled scenarios.
Build selected catalog artifacts through the existing image plan/recipe
machinery, preserving Java JVM/native and Java-lite native distinctions.
The default matrix covers the four shared-corpus families across the SDKs
currently supported by NanoLab. Rust remains the separately tracked #57;
the result lists its exclusion and never claims Rust qualification.

For every selected artifact, send its shipped payloads with distinct execution
IDs. Resolve expected body and status from the independent shared correctness
corpus; reject missing/ambiguous expectations. Binary QR responses additionally
validate envelope headers, encoding and PNG dimensions. Require exactly one
valid callback with matching execution ID, status and output within explicit
deadlines. Error envelopes inside HTTP 200 must fail.

Use an exclusively owned private Docker network and callback-capture container
to avoid exposing a host listener or needing SDK-specific host-port overrides.
Capture is bounded in bytes, records and time. Run the packaged watchdog with
`WARM=false`, `EXECUTION_ID` and `INVOCATION_PAYLOAD`, checking its single complete
callback; do not require a callback in warm watchdog mode.

### 3. Native CLI parity

Build the JVM launcher and native executable from the same frozen source.
Use the existing CLI task contracts for both against the same owned control
plane, resetting mutable function state between the two runs. Verify
list/get/apply/update/delete, replicas get/set, invocation, control-plane info,
and explicit configuration-file loading. Compare parsed observable responses;
retain raw stdout/stderr for diagnosis, without requiring unstable IDs, timing
or terminal formatting to be byte-identical. No runtime-config capability may
be silently skipped when the scenario requires it.

## Ownership, failure and evidence

Use an isolated frozen source; never build in or edit the operator's NanoFaaS
checkout. Use public Sonata dependencies unchanged. Reuse existing owned
command deadlines/output bounds and cleanup compensation. Retain immutable
JSON receipts per artifact/case and both CLI forms under the explicit operator
run directory. Any build, contract, diagnostic or cleanup failure makes the
workflow fail. Remove only the containers/network/builders acquired by the run.
No measurement resume, cloud provisioning, public image push or signing belongs
to this workflow. Native builds and representative live cases must be run before
claiming runtime qualification; incomplete live gates stay explicitly recorded.

## Verification and completion

First reproduce each missing gate RED, then implement the minimum change and
verify GREEN. Tests use real local HTTP/callback endpoints and command-boundary
doubles for Docker/VM operations, including wrong bodies/statuses, absent and
duplicate callbacks, reflection failures, startup failure, deadline and cleanup
failure. CLI parsing/configuration checks exercise actual executable responses.
Verify actual built native artifacts, installed wheel/presets, full NanoLab and
toolkit suites, original coverage configurations and existing quality hooks.
Keep the existing 90% coverage threshold and report the known debt separately.

A new live fixture must prove callback capture independently of the artifact
under test; manufactured callback data cannot qualify a shipped runtime.
No issue is considered complete solely because mocked commands or planning pass.
For shipped function, watchdog and CLI logs, also reject `No serializer found`,
Python tracebacks and Rust panics, as well as the reflection failures above.
