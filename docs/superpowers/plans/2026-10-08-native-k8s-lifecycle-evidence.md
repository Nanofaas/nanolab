# Native Kubernetes lifecycle — execution evidence, 2026-10-08

The installed native Kubernetes lifecycle passed, including normal resource
cleanup, on `fix/operational-validation`. The final independent review identified
two Important defects; both now have observed RED-to-GREEN regressions and the
updated installed lifecycle passed again. All 3,629 functional tests passed;
coverage remains below the unchanged 90% gate. No images were published.

## Actual installed run

- Installed NanoLab and tui-toolkit wheels into an isolated environment using
  public Sonata 0.6.14 dependencies; 51 packages have compatible dependencies.
  Both native presets are bundled and the installed CLI compiles their workflow.
  The final wheel SHA-256 is
  `9fb43dcc0fcfa0f186d3c082ac079515200c0d01c054552a23bb8d9bf989dbee`.
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
- Quota proof: 30 separately retained responses, **4 HTTP 200 successes and
  26 HTTP 429 invocation_quota_exceeded responses**. The control-plane setting
  `NANOFAAS_INVOCATION_CAPACITY_EXECUTIONS_PER_FUNCTION=1` was applied using an
  atomic Deployment UID/resourceVersion guard.
- Deployment UID: `0e35f435-dfef-46db-98b4-56ab7507264d`.
  Sampled pod UID: `01ac7e5d-2675-45e8-b022-d9f3ea9806f6`.
  Container: `76a4bb45aafb0492d00fe26c324cfc055f3b84c84fed8a30b132ea5227aa3771`.
  The before/after CRI PID was 5981, procfs start time 26372849.
  CPU limit was 1 and the actual command was
  `/app/application -Dreactor.netty.ioWorkerCount=1`.
  Exactly one Reactor worker was observed: thread 5997, `or-http-epoll-1`.
  GraalVM keeps the suffix of long thread names; selector and management threads
  were excluded by exact Reactor-derived matching.
- Logs before API requests, after those requests before rollout, and after quota
  traffic were retained. They had none of the unsupported, missing reflection
  or missing resource registration errors.
- Namespace, imported images, function, Helm release and API forward were
  released. Cluster inspection showed only system/default namespaces and no
  recipe images. The owned Minikube profile was subsequently deleted.

Raw receipts remain at `/tmp/nanolab-native53-z__1sylb/lifecycle-retry2`;
the successful command log is `/tmp/nanolab-native53-z__1sylb/lifecycle-review.log`.
These temporary receipts are evidence references, not required runtime paths.

## Verification and limits

- 165 affected tests passed after the review fixes, including real local HTTP,
  remote receipt retry, command-boundary CRI
  probes, actual Linux procfs parsing, native/JVM branching and failure cleanup.
- Runtime fixes were driven by real failures: dynamic Resource endpoints need
  semantic keys; GET replicas uses `name` while PUT uses `function`; ordinary
  success envelopes carry nullable `statusCode`; GraalVM thread truncation keeps
  suffixes; function cleanup must use the reopened owned API forward.
  Each corrected behavior has an observed RED-to-GREEN regression.
- Final original full NanoLab checks after the review fixes: **3,629 functional
  tests passed in 354.16s**, coverage **86.31%**, exit 1 solely for the unchanged
  90% threshold. Log: `/tmp/nanolab-native53-z__1sylb/full-nanolab-review.log`.
  The previous operational baseline was 3,537 passes with 86.23% coverage;
  the coverage debt predates this lot. The full CI coverage gate remains failing.
- Toolkit: 51 tests passed, 93.71% coverage against its original 80% threshold.
- Workspace dependency check: 75 compatible packages; installed check: 51.
  All 15 quality hooks passed after the fixes, including both type checks.
- The remote VM path is covered at its existing transport/staging boundaries;
  live qualification in this delivery is local Minikube arm64. No live remote
  qualification, native CLI parity, function SDK matrix or watchdog contract
  qualification is claimed; the latter work remains #54's two separate lots.

## Independent review and correction

One fresh-context whole-branch review covered `55668dd..df8befa`; its reviewer
independently ran 126 targeted tests. No Critical or Minor findings were reported.
Both Important findings were accepted based on their effect on qualification:

1. Native errors emitted during otherwise successful API requests could disappear
   when quota configuration rolled the pod. The gate now retains and checks the
   original pod's logs after those requests, before mutation. The regression
   verifies rejection, preserved error evidence and release of owned resources.
2. Remote retries reused an exclusive receipt path, failing with FileExistsError
   and copying the previous burst's evidence. Each burst now gets a unique path
   inside the owned remote root. The regression runs the packaged diagnostic
   against real HTTP twice, archives the first failed attempt and verifies that
   current evidence contains the second burst's quota responses.

Both regressions failed before the correction and passed afterward. Logs are
retained at `/tmp/nanolab-native53-z__1sylb/review-fixes-{red,green}.log`.
The final package functional suite passed after the single fix pass; no second
review was dispatched. The review's four explicit exclusions and their costs
are recorded as the final four decisions below.

## Execution decisions

These are every ruling from the plan's execution ledger, in decision order,
including costs if wrong. Decision 13 supersedes decision 8's temporary forward
implementation. There are no deferred Minor findings.

1. existing host/VM adapters reject CommandOptions timeouts — bound native curl with its native time/byte flags and GNU timeout, preserving the same executor and explicit 60s deadline — cost if wrong: qualification fails when the Linux timeout utility is unavailable, rather than hanging or passing a gate.
2. add an explicit optional evidence_file argument to the API helper instead of treating cwd as the output path — remote cwd and local receipt paths are distinct — cost if wrong: one internal caller signature adjustment.
3. four recovery fixtures supplied {} for a successful PUT — update only those responses to the independently verified ReplicaResponse record, and bump the PUT semantic contract to v3 — cost if wrong: affected recovery tests fail; existing journals may need a new run rather than reusing the weaker old contract.
4. start the isolated native build while API implementation proceeds because it depends only on completed Task 1 — cost if wrong: an early build must be repeated if the recipe changes before final qualification.
5. the first build harness passed RecipeBuilder to a single-platform recipe, which NanoFaaS correctly rejects; the owned builder was released — rerun using the existing single-platform recipe build path without that multiarch-only argument — cost if wrong: build fails explicitly, without modifying operator resources.
6. test and attach queue-before-quota ordering in Task 4, which creates the lifecycle task and workflow attachment — Task 3 provides the independently tested diagnostic and guarded quota mutation — cost if wrong: integration ordering must fail Task 4's test before shipping.
7. use an atomic JSON patch testing Deployment UID and resourceVersion instead of an unguarded set-env command — prevents mutating a replacement between inspection and write — cost if wrong: concurrent changes fail qualification and require a fresh run.
8. reopen a compensated run-owned API forward after the native quota rollout — kubectl port-forward is tied to the original pod and cannot qualify the restarted process — cost if wrong: the fresh forward fails explicitly, and its release failure fails the run.
9. add optional assets and remote_root constructor arguments and pass the existing staged diagnostics directory — remote stdout receipts and local evidence paths must be separate — cost if wrong: remote qualification fails rather than silently omitting its evidence.
10. collect procfs via a bounded shell script on the node and parse it with the packaged standard-library Python diagnostic on the host — Minikube nodes need no Python installation — cost if wrong: unsupported node utilities make qualification fail.
11. preserve failed native receipts under native-attempts/<UUID> before repeating validate with the same run directory, and reuse the existing verified frozen recipe inputs — avoids overwriting evidence and an unnecessary cold rebuild — cost if wrong: resumption fails explicitly on incompatible staged inputs; attempt archives consume additional bounded evidence space.
12. recognize both prefix and GraalVM suffix truncation for Reactor workers, excluding selector and management threads — measured procfs names require it — cost if wrong: worker counting could misidentify a colliding name; exact derived suffix matching limits that risk.
13. replace the short-lived extra forward with reopening the existing workflow-owned forward and share its new URL with function cleanup — the failed live run proved early forward release leaves DELETE using a stale tunnel — cost if wrong: cleanup or refresh fails the run; the original resource still owns and releases the actual latest process.
14. native CLI parity and packaged function/watchdog contracts remain the two approved #54 lots — keep this delivery focused on #53 — cost if wrong: native qualification here does not establish those remaining contracts.
15. live remote VM/cloud and destructive Docker soak behavior are not claimed from transport tests — qualify the installed local Minikube path and retain the remote retry transport regression — cost if wrong: environment-specific remote or Docker failures can remain despite these local checks.
16. downstream Gradle compatibility of the staged Maven releases is outside the approved coordinate-staging change — keep the narrower build/export checks and make no consumer qualification claim — cost if wrong: a downstream consumer may still require adjustments.
17. preserve the original 90% coverage gate and report the existing repository-wide coverage debt — unrelated coverage remediation is outside this branch's approved scope — cost if wrong: CI stays blocked by the coverage gate despite the passing functional suite.
