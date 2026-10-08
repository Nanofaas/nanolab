# Native CLI parity — execution evidence, 2026-10-08

The NanoLab implementation is on `fix/operational-validation`. The installed
ARM64 JVM/native parity cycle passed against an explicitly patched NanoFaaS
candidate, including every release. The unmodified NanoFaaS native CLI fails
its required YAML configuration contract; it is not qualified by this evidence.
Task 5 remains incomplete for the shipped source until that source fix is
integrated and qualified. Issue #54 also retains the separate function/watchdog lot.

## Actual installed candidate

- Public Sonata 0.6.14; isolated NanoLab/toolkit wheels; 51 compatible installed
  packages and 75 compatible workspace packages. Both bundled presets resolve
  through the installed CLI. NanoLab wheel SHA-256:
  `48d83f4e84513eab327579756e630188f60b1f855c5727974c1310211e916c24`.
- NanoFaaS base revision `fed1be9c28985ba3e0ee7f067b390e1dc8aac4b3`, plus only
  a tracked reflection registration for `it.unimib.datai.nanofaas.cli.config.Context`.
  Captured source patch fingerprint:
  `e18dc182eb997f2e8eda82811ba554cde27654a17f3b9d5f583e62ffaf849203`.
  The source is a temporary clone; neither operator checkout was edited.
- Real Gradle `:nanofaas-cli:installDist` and `:nanofaas-cli:nativeCompile`
  completed in the same frozen attempt. GraalVM Community 25.2.4 on ARM64;
  existing native build controls 12 GiB / 4 workers. Both report `nanofaas 0.22.0`.
  The native ELF64 machine is ARM64; its SHA-256 is
  `1f9073ec72872cd57deb615e90708bd3be80a6d579d7aee14baa1d0a99168107`.
  The JVM receipt hashes the executable launcher and all ten installed JARs.
- Recipe `cli-e0f99aef7fb5`: JVM control plane, Java JVM word-stats,
  exactly build-metadata/k8s-deployment-provider/runtime-config. No image publication.
- Installed command `nanolab run cli-contract-k8s-native-parity.yaml` exited 0.
  Its owned Docker/containerd Minikube profile uses isolated MINIKUBE_HOME and
  KUBECONFIG; the operator Minikube profile remained stopped.
- Each artifact passed 27 parsed observations and retained 54 raw command
  receipts. Fresh apply/get/list, YAML update/get, replicas/readiness, exact
  frozen word-stats corpus output, immutable refusal/replace/get, info/contract,
  valid/invalid runtime config, patch/readback, actual YAML --config without
  endpoint/environment shortcuts, delete/list and Kubernetes absence passed.
  Native/JVM observations compare equal after checking generated metadata.
- Each pass patched the controlled rate from 1000000 to 999999 and restored
  1000000 with revision guards (final revisions 2 and 4). The control-plane
  Deployment/pod/container stayed identical, including final restoration.
  Deployment UID `14699c9f-7630-4ccc-b454-5cb7d57b6986`,
  pod UID `6309e5b5-b807-4366-b381-23ddd7d6d5bf`, container
  `1031b88e43f842bb9a33e46f1237597c0cbd6dd55beed8e7b3ab9b14164c2476`.
- Logs before requests, after requests before function deletion, after release
  and after baseline restore contain no prohibited diagnostic markers.
  Function, forward, Helm, namespace, imported images and pinned credentials
  all released. Independent inspection found only the four system/default
  namespaces, no recipe images and no Helm releases. The dedicated Minikube
  profile was subsequently deleted; the operator profile stayed stopped.
- The installed JVM build-only slice also exited 0 with a nonexistent kubeconfig,
  acquired no platform resources and wrote no qualification marker.

Raw candidate evidence: `/tmp/nanolab-cli54-u5t2pk5h/parity-metadata`, attempt
`cli-attempts/e0f99aef7fb5`. Command log: `parity-metadata.log` in that temporary
root. Receipts are temporary evidence references, not required runtime paths.

## Unmodified-source failure and required NanoFaaS fix

Clean `fed1be9` native compilation succeeds, but a nonempty `contexts` mapping
fails with `Failed to read config`. Empty contexts load successfully. The missing
reflection entry is `Context`; the existing registration covers `Config` only.
The current operator revision `a3722a47` has identical CLI configuration sources
and reflection metadata. The failed installed attempt retained raw native
receipts, failure logs, baseline restoration and successful outer cleanup;
no parity marker exists. See `/tmp/nanolab-cli54-u5t2pk5h/parity-deadline`.

A six-line candidate patch is reviewable at
`/tmp/nanolab-cli54-u5t2pk5h/nanofaas-context-reflection.patch`. Applying it only
to a temporary clone enabled the native pass and the successful candidate cycle.
That initial prototype was not committed or integrated. The subsequently
authorized upstream correction is recorded below. This document does not equate
candidate success with shipped-source qualification.

## Upstream correction and publication

The user subsequently authorized the NanoFaaS correction on a dedicated branch,
then push and pull requests for both repositories. NanoFaaS
[PR #253](https://github.com/Nanofaas/nanofaas/pull/253) contains commit `a234ea17`
on `fix/native-cli-context-reflection`, based on `a3722a47`. It adds the six-line
`Context` reflection entry and extends the existing native smoke test with an
actual YAML context lookup against a temporary localhost API, without an
endpoint flag or inherited endpoint/context environment overrides.

The regression fails on the original native binary with `Failed to read config`
and passes on the newly compiled ARM64 GraalVM binary and JVM launcher, including
with foreign endpoint/context environment variables. The rebuilt native binary
SHA-256 is `0845a81fd86e1c48c677c3c46b02a37f98cd8af98c94e36ca7737ca562946937`.
The full NanoFaaS Gradle suite passed: 2795 tests, 9 skips, no failures; all 196
JVM CLI tests passed. It used the default Docker builder and checksummed Maven
artifacts from the project's pinned containerd source revisions. GitNexus
reported low impact for the two changed files. Verification logs and receipts
remain under `/tmp/nanofaas-native-cli-context-evidence` and
`/tmp/nanofaas-native-cli-context-*.log`.

The installed paired Kubernetes evidence above remains the earlier candidate
cycle. The upstream PR is open; integration and shipped-source qualification
remain pending, as does the separate function/watchdog delivery under #54.

## Verification

- Original CI-pinned NanoLab suite: 3748 passed, 3 intentional artifact-parser
  skips in 320.22s. Coverage 86.62%; exit 1 solely for the unchanged 90% gate.
  Coverage debt predates this lot (previous #53 result: 86.31%). The full CI
  coverage gate remains failing. Log: `full-nanolab-metadata.log` in the evidence root.
- Toolkit: 51 passed, original 80% coverage gate passed at 93.71%.
- All 15 quality hooks passed, including both type checks; dependency checks passed.
- Observed RED-to-GREEN fixes cover the product endpoint guard, the real host
  adapter deadline, the unchanged P24 baseline with the new default CLI selector,
  and generated runtime patch UUID/timestamp normalization. Invalid IDs/times
  remain failures; application values and meaningful array order are preserved.
- One fresh gpt-6-astra review covered the whole branch `55668dd..0dd322f`.
  It independently ran 124 tests with 3 intentional skips, found no Critical or
  Important NanoLab defects, and judged the implementation ready to merge.
  Shipped-source qualification remains incomplete. No fix pass was required.

## Execution decisions

1. advance the HTTP invocation semantic key alongside CLI when strengthening the shared error validator — resumed journals must not reuse the weaker old HTTP proof — cost if wrong: an older interrupted run needs a fresh execution.
2. add_cli_contract returns its acquired function resources rather than None — the paired gate must keep them alive for post-request logs before normal deletion — cost if wrong: a programmatic consumer relying on a None return must adapt; legacy callers ignore the return.
3. pass 45 seconds to the shared Kubernetes readiness factory as well as CommandOptions — the factory otherwise emits its default 120-second polling budget — cost if wrong: a slow function fails this intentionally bounded qualification earlier.
4. expose one artifact-check consumer per selected mode — public Sonata selects tasks, not resource acquisitions, so this makes the required offline build slice selectable — cost if wrong: two extra consumer entries appear in parity plans.
5. carry scenario resource overrides into Helm and CLI manifests, checking the requested nested fields against complete server DTOs — existing resource settings must not be silently ignored; semantic comparison still retains all returned fields — cost if wrong: unspecified server resource defaults are accepted by the individual get proof.
6. translate timeout_seconds to GNU timeout in the evidence executor and clear the adapter option — the product HostCommandTaskExecutor explicitly rejects this option; retain the stricter of the requested and 60-second deadlines — cost if wrong: deadline/process-group behavior follows GNU timeout rather than the local executor.
7. exclude validated UUID changeId and zoned appliedAt only at the runtime-patch response root — actual source generates them for each accepted write, so comparing them rejects correct independent passes; application fields and raw receipts stay intact — cost if wrong: differences confined to valid change identifiers/timestamps do not invalidate semantic parity.
8. retain the shipped-source native configuration failure and verify a Context reflection candidate only in a temporary clone — the missing registration is outside NanoLab; no operator checkout edits or unmerged-source qualification claims — cost if wrong: the installed success applies only to the explicitly patched candidate until the NanoFaaS fix is integrated.

## Deferred minors

1. Unify the version formats accepted during artifact acquisition and qualification.
2. Retain bounded raw failure receipts when help/version exits successfully but fails validation.

## Final review decisions

1. NanoFaaS Context integration/release remains external — retain the failed shipped-source proof and temporary candidate success; no source checkout or publication authorization is inferred — cost if wrong: shipped-source qualification remains incomplete until integration.
2. function/watchdog contracts remain separate — the user approved three deliveries, and CLI evidence cannot close issue #54 — cost if wrong: their runtime defects remain independently unqualified.
3. remote and other-backend CLI parity remain rejected — the approved scope is local Kubernetes and the guards run before side effects — cost if wrong: those environments have no native parity qualification.
4. live qualification is ARM64 only — ELF architecture checks do not substitute for an amd64 run — cost if wrong: amd64-specific runtime defects remain unqualified.
5. the original 90% coverage gate remains failing — report the preexisting debt and 86.62% result without changing threshold or lock files — cost if wrong: full CI remains red for coverage.
6. stream bounds are acceptance/retained-evidence bounds after capture — reuse the existing host executor and GNU deadlines, and make no claim of a hard streaming memory ceiling — cost if wrong: a noisy command can consume memory during the bounded capture.
7. generated patch IDs/times and unspecified resource defaults use the existing rulings — individual contracts validate requested fields and generated metadata; complete meaningful semantic responses still compare — cost if wrong: valid ID/time differences or unspecified defaults are not rejected individually.
