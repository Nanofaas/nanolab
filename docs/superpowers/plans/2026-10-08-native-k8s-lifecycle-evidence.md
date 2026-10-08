# Native Kubernetes lifecycle — execution evidence, 2026-10-08

The installed native Kubernetes lifecycle passed, including normal resource
cleanup, on `fix/operational-validation`. Final independent review and the final
package coverage report are pending. No images were published.

## Actual installed run

- Installed NanoLab and tui-toolkit wheels into an isolated environment using
  public Sonata 0.6.14 dependencies; 51 packages have compatible dependencies.
  Both native presets are bundled and the installed CLI compiles their workflow.
- Frozen NanoFaaS revision: `fed1be9c28985ba3e0ee7f067b390e1dc8aac4b3`, clean.
  Build memory/parallelism controls: 12 GiB / 4. Actual cold native builds passed;
  later repeated validation reused verified frozen inputs and build outputs.
- Actual recipe: `recipe-0211510cf68a`, native variant `recipe-v2-k8s-native`.
  Control-plane image config:
  `sha256:697db99c38e4fb479201ef137224e5d336c96e55ec402eeb5b1dc6dd92bed6cb`.
  The fixed Java word-stats artifact remains JVM.
- Local target: an owned Docker/containerd Minikube profile on arm64, using its
  own MINIKUBE_HOME and KUBECONFIG. The operator's stopped Minikube profile was
  never started or modified.
- The installed command `nanolab run deployment-lifecycle-k8s-native.yaml`
  completed with exit 0. Ordinary queue validation ran before quota mutation.
  API checks exercised fresh POST, PATCH, GET/PUT replicas, invoke and DELETE.
  The native gate and every subsequent release passed.
- Quota proof: 30 separately retained responses, **2 HTTP 200 successes and
  28 HTTP 429 invocation_quota_exceeded responses**. The control-plane setting
  `NANOFAAS_INVOCATION_CAPACITY_EXECUTIONS_PER_FUNCTION=1` was applied using an
  atomic Deployment UID/resourceVersion guard.
- Deployment UID: `f814d462-47c4-45ae-9a35-2145397c525d`.
  Sampled pod UID: `cbb05354-f6ba-4a50-a540-1fb83eb9536e`.
  Container: `8256823026fb055c5321baa104231e6ad68a12c44ca9cebfbc774d7a030fe71c`.
  The before/after CRI PID was 20781, procfs start time 26281714.
  CPU limit was 1 and the actual command was
  `/app/application -Dreactor.netty.ioWorkerCount=1`.
  Exactly one Reactor worker was observed: thread 20797, `or-http-epoll-1`.
  GraalVM keeps the suffix of long thread names; selector and management threads
  were excluded by exact Reactor-derived matching.
- Logs before and after rollout were retained and had none of the unsupported,
  missing reflection or missing resource registration errors.
- Namespace, imported images, function, Helm release and API forward were
  released. Cluster inspection showed only system/default namespaces and no
  recipe images. The owned Minikube profile was subsequently deleted.

Raw receipts remain at `/tmp/nanolab-native53-z__1sylb/lifecycle-retry2`;
the successful command log is `/tmp/nanolab-native53-z__1sylb/lifecycle-runtime.log`.
These temporary receipts are evidence references, not required runtime paths.

## Verification and limits

- 172 affected tests passed, including real local HTTP, command-boundary CRI
  probes, actual Linux procfs parsing, native/JVM branching and failure cleanup.
- Runtime fixes were driven by real failures: dynamic Resource endpoints need
  semantic keys; GET replicas uses `name` while PUT uses `function`; ordinary
  success envelopes carry nullable `statusCode`; GraalVM thread truncation keeps
  suffixes; function cleanup must use the reopened owned API forward.
  Each corrected behavior has an observed RED-to-GREEN regression.
- Original full NanoLab checks before the final runtime fix: 3,623 functional
  tests passed, coverage 86.26%, exit 1 due to the unchanged 90% threshold.
  The final full run is still in progress. The previous operational baseline
  was 3,537 passes with 86.23% coverage; the coverage debt predates this lot.
- Toolkit: 51 tests passed, 93.71% coverage against its original 80% threshold.
- Workspace dependency check: 75 compatible packages. Quality hooks are being
  repeated after their import-format corrections.
- The remote VM path is covered at its existing transport/staging boundaries;
  live qualification in this delivery is local Minikube arm64. No live remote
  qualification, native CLI parity, function SDK matrix or watchdog contract
  qualification is claimed; the latter work remains #54's two separate lots.

## Execution decisions

The plan's execution ledger records every ruling and its cost if wrong. The
final record will be retained here before deleting this plan's scratch workspace.
