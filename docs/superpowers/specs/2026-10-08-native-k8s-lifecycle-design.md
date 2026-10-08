# Native Kubernetes lifecycle — first native delivery, issue #53

Status: the user approved splitting native validation into Kubernetes lifecycle,
CLI parity, and packaged function/watchdog contracts. This first delivery's
written design is approved; native implementation has not started.

## Outcome

A Kubernetes lifecycle run must prove it tested the selected native control
plane. It must reject wrong artifacts, missing response bodies, native reflection
failures, and the wrong event-loop configuration. The ordinary Java JVM function
workload stays fixed so failures can be attributed to the control plane.

## Scope and existing owners

Reuse `plans/validate.py`, recipe build/distribution and Kubernetes resources,
the existing function HTTP contracts, command transports and evidence directory.
Add a native counterpart to `validate-k8s-jvm.yaml` and its lifecycle scenario.
No new workflow name, callback server, SDK matrix or native CLI belongs here.

The existing recipe checks relate running image digests to build artifacts and
verify build metadata. The native recipe sets `controlPlane.build.mode: native`
with modules `k8s-deployment-provider`, `build-metadata`, and `sync-queue`, while
its word-stats function stays JVM. Set a distinct recipe variant/tag so native
and JVM evidence cannot be confused. Freeze a source snapshot outside the
operator's NanoFaaS checkout and retain its revision in the build receipts.

The legacy validation selector `controlPlaneRuntime: native` is currently ignored.
Reject that unsupported spelling before provisioning and point to the native
recipe scenario. Do not silently replace arbitrary function selections with the
bundled recipe. Preserve existing supported JVM and loadtest behavior.

## Lifecycle and API gates

Use the existing owned Kubernetes namespace, deployment and endpoint. First
prove the control-plane artifact is native through recipe metadata and runtime
image identity. Exercise register, patch, GET/PUT replicas, successful invocation
and delete. Validate the corresponding JSON bodies and identities/replica values,
not merely successful curl exit codes. Require successful invocation envelopes;
HTTP 200 with an error or timeout is a failed lifecycle.

Strengthen the existing PUT-replicas task at its current owner. Reuse GET replica
readiness checks and the existing response-envelope validator. Retain API bodies
and diagnostic output as run evidence. Delete responses follow the actual API
contract; do not require JSON where a successful deletion is legitimately empty.

For the quota regression, set `controlPlane.invocationCapacity.executionsPerFunction`
to 1 and send a bounded concurrent burst to an owned probe that holds requests
long enough to create overlapping live executions. Collect each response
separately. Require at least one HTTP 429 with JSON
`error: invocation_quota_exceeded`; a queue-limit 429, malformed body, or burst
without an observed invocation quota rejection does not qualify this gate.
Keep this burst distinct from existing sync-queue admission tests.

The lifecycle needs no admin runtime-config API. Any Spring property required by
these selected modules must be supplied before AOT/native compilation. Reuse
the source's native build memory option; do not change global build policy.

## Logs and actual runtime shape

Collect logs from the control-plane pods selected by the run's deployment owner,
not a cluster-wide label query. Treat `UnsupportedFeatureError`,
`MissingReflectionRegistrationError`, and `MissingResourceRegistrationError` as
failures, even if individual API calls succeeded. Missing diagnostic output also
fails qualification; retain the relevant pod UID and container ID.

Configure the preset with a 1-CPU limit and no explicit ioWorkerCount override;
the chart must derive one worker. Verify the running pod receives
`-Dreactor.netty.ioWorkerCount=1` as a native binary argument. Checking only
`JAVA_TOOL_OPTIONS` is insufficient.

Observe actual Linux threads through node runtime inspection using the existing
Minikube SSH or remote VM command transport. Resolve the owned pod's immutable
CRI container ID to its process; read bounded `/proc/<pid>/task/*/comm` data and
check the process identity/start time across the observation. Account for Linux
thread-name truncation when recognizing Reactor HTTP workers. Drive traffic
before counting and require the observed count to match the chart-derived count.
On arm64 require epoll, distinguishing its names from nio. An unavailable or
ambiguous process/thread observation fails this gate. Do not infer the actual
thread count from environment variables or requested CPU limits alone.

## Ownership and evidence

Use the existing recipe resources and command deadline/output limits. Build only
from the frozen source; remove only acquired namespace, runtime images and other
resources. Preserve nonzero failure for builds, API checks, diagnostics or cleanup.
Record source revision, artifact/metadata identity, pod UID/container ID, API
results, quota responses, logs, requested arguments and observed worker count.
No public image publication, unrelated cluster modification or comparison of
native JVM-metric families is part of this delivery.

## Verification and completion

Reproduce the ignored-selector and missing gates RED before implementing. Cover
bad native metadata/image identity, invalid/missing replica JSON, HTTP-200 error
envelopes, wrong/missing quota responses, reflection logs, mismatched worker
counts, arm64 nio, missing diagnostics, and cleanup failures. Use independent
HTTP fixtures and command-boundary transports rather than manufactured success
evidence. Verify bundled presets from an installed wheel and run the original
package checks without relaxing coverage thresholds.

Qualification additionally requires a real native build and owned Kubernetes
lifecycle run, including the API, quota, log and thread gates. If tools or build
capacity are unavailable, report that live gate explicitly as incomplete rather
than closing #53 based on mocked planning. #54 remains separate.
