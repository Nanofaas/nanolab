# Packaged functions and watchdog — execution evidence

Execution began 2026-10-08 and continued 2026-10-09 on `fix/operational-validation`
in `/tmp/nanolab-operational-validation`. Operator main was not used for edits.

## Implementation and review

Tasks 1–5 implement strict independent-corpus selection, frozen image builds,
owned capture/probe networking, HTTP/body/status/callback validation, packaged
bash warm and fresh one-shot modes, structural PNG validation, CLI presets, and
qualification after verified resource release. Task-specific verification:
97, 13, 63, 46 and 91 tests respectively; original configurations and public
dependency versions remain unchanged.

A fresh read-only whole-branch review by `gpt-6-astra` found five material gaps:
mutable-reference destruction races; unsupported methods/pending headers omitted
from audit; incomplete retained evidence/inventory checks; redirects masking
invocation status; and lost partial HTTP/callback observations. All received
concrete failing regressions and fixes in `d6b492e`. The final dedicated contract
group passed 91 tests; the broader validation group passed 319 with three source
fixture skips. All repository hooks, typing, import boundaries and security
checks passed. No minor findings were deferred and no second review was used.

## Installed ARM64 qualification

Evidence root: `/tmp/nanolab-functions54-evidence`.
NanoFaaS source: `/tmp/nanofaas-native-cli-context`, revision
`a234ea17ea2fed679c6b7c99243cceaf4ded0b5b` (the separate native CLI context
candidate); tracked patch was empty. Host and Docker report native ARM64.
Installed wheels use Python 3.12.3 and an isolated 51-package environment.
Both presets compile from `/tmp` without acquiring Docker resources.

Current NanoLab wheel, built at `15fb13c`, SHA-256:
`f4921aa750eac836c17dd70c05bbb2104c0dfe082248817818dbb8e288399f3d`.
The prior Bake-only wheel at `ffe599f` was
`6bb812d2633de90abd872587a4f9e23ea5c10c57a80ed1c47c04b850c9150424`.
The prior post-review wheel at `d6b492e` was
`b4fb529541b852b528fef8b0f3b5a7bb4fea31fd566b0ba8cfdbb1a30082a954`.
Toolkit wheel SHA-256:
`4de5f4cc2341805d9b538b5f3a4e4eec4692a7d301466371aa3e011b94809129`.
Workspace and installed `uv pip check` passed. The corrected wheel also resolves
both plans from outside the checkout. CI-shaped pure tests passed 3242 with
17 skips and 391 checkout cases deselected. The checkout phase passed 636 tests
with 3245 deselected on the approved pinned revision. NanoFaaS PR #253 remained
open at the final integration-status check.

The independently resolved full matrix contains 27 cells, 119 HTTP cases,
18 bash one-shot cases and 119 artifact callbacks (startup probe separate).
The first installed smoke failed during Java native image build: the shipped
Dockerfile's `COPY --from=containerd_maven_repo` requires an explicit empty named
context for ordinary builds. NanoLab now supplies an owned empty directory;
the missing-context regression failed before the fix and passed afterward.
That failed attempt is retained at `smoke/`; no qualification marker was issued.
Source tracked patch and selected operator builder remained unchanged.

The second smoke (`smoke-fixed/`) stopped at Bake's filesystem entitlement:
its named context was outside the frozen working directory. The exact owned
empty context now receives `--allow=fs.read=<attempt>/empty-maven-context`;
no global check is disabled. This regression passed after failing on the missing
allow argument, and 41 resource/finalizer regressions plus quality hooks passed.
All three failed attempts independently proved owned-resource absence and
issued no marker. The third smoke (`smoke-bake/`) successfully built the Java
native artifacts, then exposed NanoLab's invalid requirement that every image
have an `ENTRYPOINT`. JavaScript and Python ship valid `CMD`-only startup.
Commit `15fb13c` accepts them and retains and checks both startup fields.
The two regression tests failed before correction (valid CMD-only rejected;
changed container CMD accepted), and 49 resource/execution/finalizer tests
passed afterward, along with the affected quality hooks. The corrected fourth
smoke is retained at `smoke-cmd/`.

The corrected smoke passed with exit 0: all seven cells, 21 HTTP invocations,
three real one-shot invocations and 21 artifact callbacks (24 case receipts).
The final audit retained 22 callbacks including the startup self-check; warm
watchdog calls emitted zero callbacks. `qualification.json` was published only
after resource release and independent absence checks (`cleanupVerified: true`).
Its SHA-256 is
`4d7b6890ffc18461abcf3956620f97f1c5744f375fefafd4a773a8f1beafd30e`.

The full preset (`full-cmd/`) built all 27 cells successfully in 1771.1 seconds.
It then retained 79 case receipts: 78 passed, and
`java-qr-code-native/sdk/000` failed. All 18 watchdog warm cases and all 18 real
one-shot cases passed; Go, Java JSON JVM/native, Java lite and Java QR JVM cases
also passed before the failure. The remaining 58 required cases were not
invoked after the workflow stopped. The full attempt exited 1 and published no
qualification marker. A passing smoke does not qualify this full matrix.

The failing independent case is `encodes a URL as PNG`, input
`{"text":"https://example.org/invite/abc","size":256}`. Instead of expected
HTTP 200/PNG, the artifact returned HTTP 500 and a real `success: false`
callback with `HANDLER_ERROR`:

```text
java.lang.UnsatisfiedLinkError: Can't load library: awt
java.library.path = [/usr/lib64, /lib64, /lib, /usr/lib]
```

The retained runtime stack points from `ColorModel`/`BufferedImage` to
`MatrixToImageWriter.writeToStream` and `QrCodeHandler.handle:37`.
Build log `full-cmd/commands/0cf6432597cd4e039b5196941026302f.log` shows GraalVM
emitting `libawt.so`, `libawt_headless.so`, `libawt_xawt.so`, `libjavajpeg.so`,
`liblcms.so` and library shims beside the executable. The immutable upstream
[Dockerfile](https://github.com/Nanofaas/nanofaas/blob/a234ea17ea2fed679c6b7c99243cceaf4ded0b5b/tools/native-java/Dockerfile)
copies only the executable to `/tmp/application` and then `/app/application`.
This establishes a NanoFaaS native-image packaging defect; the required library
closure and lookup path need their own fix and runtime regression. No NanoFaaS
source, expected output or selected case was changed to manufacture a pass.

The failed receipt, raw HTTP body and callback are retained under
`full-cmd/cases/java-qr-code-native/sdk/000/`; the native runtime diagnostic is
`full-cmd/container-logs/b7c111f209b49469a7e0e3edeae2feb1d1d6a5881f8ac87844602554d7920d15.log`.
All five attempts have independently verified owned-resource absence; failed
attempts have no qualification marker. Final operator source status/patch and
selected-builder inventory compare byte-for-byte with the initial snapshots.
Operator NanoLab main remains clean. `attempts-summary.json` records exact
attempt outcomes. Full qualification was pending after these original attempts;
the corrected native qualification below resolves that gate. Overall #54
completion still requires its separate shipped-source CLI qualification.

## Corrected native full qualification — 2026-10-09

The installed full preset now qualifies on NanoFaaS revision
`84a8f0ce7200f3f0b0bd24979e55d0d89168c3ce`, with an empty tracked patch.
[NanoFaaS PR #255](https://github.com/Nanofaas/nanofaas/pull/255) preserves the
emitted native `.so` libraries in release, export and recipe packaging, and
registers the QR module's required AWT/ImageIO JNI members, including
`System.load(String)`. The metadata comes from a real GraalVM agent trace of
headless PNG generation at 128, 256 and 1024 pixels.

Evidence root: `/tmp/nanofaas-awt-evidence`; complete installed attempt: `full/`.
The same NanoLab and toolkit wheels identified above ran from `/tmp`, outside
the checkout, under Python 3.12.3. NanoLab production source matches merged
`2de1f1a`; its differences from wheel source `15fb13c` are documentation only.
The independent corpus hashes match the failed `full-cmd/` attempt exactly.
No case, expected value or catalog flavor was removed or changed.

All 27 image cells built successfully in 1669.4 seconds. The actual private
builder inspection confirms 16 GiB, CPU quota 400000/100000 and `runc`.
All 137 case receipts passed: 119 HTTP invocations and 18 fresh bash one-shot
invocations. All 119 artifact callbacks passed; the final audit retains 120
records including its startup self-check, with zero violations and zero active
requests or callbacks. The 18 watchdog warm cases emitted zero callbacks;
the 18 one-shot cases each emitted exactly one. All seven Java native image
identities and live ELF architecture/hash checks passed. Every Java QR native
corpus case passed, including `sdk/000`, which stopped the original full run.

The contract task passed in 343.1 seconds. All release tasks and independent
owned-resource absence checks passed; the installed command exited 0.
`full/qualification.json` was issued after cleanup with `cleanupVerified: true`.
Its SHA-256 is
`bbf2d21513cd29e68d6113fd2f48bcb593bac1ee23485209146ecfabdef053a1`.
An additional read-only check confirmed all 137 receipt hashes, the final audit
hash, source revision, empty patch, corpus identities and derived counts against
the retained marker and matrix. The selected operator builder remained
`nanolab-heap-analysis`; the isolated regression builder and images were removed.

Before the full run, the native packaging regressions demonstrated the missing
libraries and JNI failures, then passed after correction. Real QR invocations
returned HTTP 200 and a valid 256×256 PNG through all three container paths:
the release image, exported-artifact recipe image and `recipe-native` image,
using Oracle GraalVM with O3 and G1 on ARM64. NanoFaaS's full Java suite passed
2800 tests with nine skips; the complete scripts, experiments and SDK runtime
contract suite passed 275 tests with the real native QR regression enabled.
The host recipe staging test also checks emitted libraries and unrelated-file
exclusion. No important code-review findings remained.

GitNexus change analysis returned `critical` because a Markdown Section's empty
ID was shared by 7188 indexed nodes: all 858 reported processes were attributed
to that Section, which has zero actual `STEP_IN_PROCESS` edges. The raw result,
graph diagnosis and independent review are retained in the evidence root.
This is a demonstrated tooling limitation, not a clean whole-index result;
the concrete code impact and runtime checks were assessed separately.

This qualifies the selected function/watchdog matrix on that candidate.
NanoFaaS PR #253 was still open at the integration check, so shipped-source CLI
qualification remains separate. The unchanged 90% NanoLab coverage gate's
previous 86.49% result is also unresolved by this documentation-only follow-up.

Executed from `/tmp` with a fresh evidence directory:

```sh
NANOFAAS_ROOT=/tmp/nanofaas-native-awt \
NANOLAB_WORKSPACE=/tmp/nanofaas-awt-evidence/operator \
  /tmp/nanolab-functions54-evidence/installed/bin/nanolab \
  run artifact-contract-container.yaml --run-dir /tmp/nanofaas-awt-evidence/full
```


## Reproducible installed commands

Executed from `/tmp`, outside the NanoLab checkout:

```sh
export NANOFAAS_ROOT=/tmp/nanofaas-native-cli-context
export NANOLAB_WORKSPACE=/tmp/nanolab-functions54-evidence/operator
CLI=/tmp/nanolab-functions54-evidence/installed/bin/nanolab
"$CLI" plan artifact-contract-smoke-container.yaml
"$CLI" plan artifact-contract-container.yaml
"$CLI" run artifact-contract-smoke-container.yaml \
  --run-dir /tmp/nanolab-functions54-evidence/smoke-cmd
"$CLI" run artifact-contract-container.yaml \
  --run-dir /tmp/nanolab-functions54-evidence/full-cmd
```

Use fresh directories to repeat a run; existing evidence is never overwritten.
The earlier failed attempts used the same smoke command with the distinct
`smoke`, `smoke-fixed` and `smoke-bake` run directories and their recorded wheels.


## Original package gates

Pinned test source remains `e7914be065e844776af57fe9e449bce7f12e03c5`.
The first full functional run passed 3861 tests with three skips, but parallel
NanoLab/toolkit coverage collection collided in their shared `.coverage` file.
The final complete run on `15fb13c` passed 3880 tests with three skips in
362.96 seconds. Its original branch-inclusive coverage gate failed at 86.49%
against 90% (exit 1 for coverage only); the earlier baseline was 86.62%.
No threshold or omission rule was changed. All 15 quality hooks and both
dependency checks passed on this production commit. The original toolkit gate
passed 51 tests with 93.71% coverage, against its unchanged 80% requirement.
The existing NanoLab branch coverage deficit is assessed separately against the
unchanged 90% gate; statement-only CI success does not replace it.

CLI shipped-source qualification and Rust #57 remain separate. QR text decoding,
cross-runtime PNG byte equality, unsupported families, cross-architecture
execution, and watchdog FILE/HTTP supervision are excluded. Callbacks starting
beyond the configured quiet observation window are outside the claim. Receipts
are operational evidence and are not tamper-proof signatures.

## Execution decisions and costs

- capture inspection returns metadata/index and GET /_nanolab/body/{sequence} returns bounded raw bytes, rather than embedding every raw body as base64 in one response — base64/all-record aggregation can exceed the spec's response bound — cost if wrong: protocol consumers must perform an extra bounded request per retained body. The probe still reports raw response bytes as base64 over its process stdout.
- assert new contract field is None and exclude it from the existing P24 experiment snapshot comparison — the frozen experiment did not have this optional new workflow field; all original experiment fields remain compared — cost if wrong: that snapshot does not detect future default changes except the explicit None assertion.
- execute commands through a local ContractExecutor wrapping the existing host binding and Sonata run_owned_command — the existing host adapter discards command timeout options and ordinary capture reads unbounded output; the published owner already enforces deadlines, byte limits and descendant cleanup — cost if wrong: an extra short supervisor process per command. Ownership is immutable per-resource files, aggregated into owned-resources.json after release because per-case containers are acquired dynamically.
- helper accepts a bounded JSON argv instruction in addition to stdin — Sonata's owned runner deliberately supplies DEVNULL to children; no new runner or shell pipeline is needed for the small shipped correctness corpus — cost if wrong: larger future corpus instructions may require file transport. Verified the added mode with actual subprocess/HTTP RED before implementation.
- runtime resource accepts optional requires=(images,) — helper build shares the same private builder and must acquire after frozen artifact builds and release before that builder — cost if wrong: stricter resource dependency ordering. Bundled helper assets are read through files('nanolab') and copied/hash-recorded into an owned context; namespace MultiplexedPath is not a filesystem directory.
- actual TCP execution regressions live in a separate test_function_contract_execution.py; parametrized wrong-body and late-duplicate cases cover the plan's named receipt/late-delivery tests — this keeps pure semantic cases separate from socket lifecycles — cost if wrong: one additional test module to discover. PNG chunk-name and consecutive-data RED 3 then GREEN; task hooks all pass.
- catalogue and installed-preset discovery already glob bundled scenarios; reuse their existing behavior without adding dispatch tables — avoids two catalog sources — cost if wrong: installed discovery regressions must catch a missing preset. Product/engine lifecycle uses real Sonata resources with a test finalization boundary; independent finalizer filesystem and Docker-boundary tests exercise integrity separately — cost if wrong: installed live qualification must catch the remaining combined integration defects.
- installed ARM64 qualification remains an independent actual-run gate — reviewer acquired no Docker resources and parent executes it — cost if wrong: unit/review success cannot substitute for full qualification.
- original coverage/dependency gates use their unchanged configurations and actual outputs — reviewer did not independently run them — cost if wrong: coverage debt remains visible and may prevent merge.
- callbacks starting beyond the configured quiet window remain outside the claim — approved finite observation window — cost if wrong: such later deliveries are not detected.
- no coordinated-manifest rewrite protection is added — operational receipts are not signed and spec does not promise tamper resistance — cost if wrong: evidence must be trusted operationally.
- QR text decoding and cross-runtime PNG byte equality remain excluded — approved structural/dimension and within-invocation checks — cost if wrong: valid PNGs containing wrong QR text can meet this gate.
- Rust/other unsupported families/cross-architecture/watchdog FILE-HTTP supervision remain excluded — approved scope — cost if wrong: those paths remain unqualified.
- shipped-source CLI qualification remains separate — this delivery validates function artifacts, not CLI — cost if wrong: #54 cannot be closed from this marker alone.
- native Bake supplies an owned empty containerd_maven_repo named context for ordinary function builds — shipped tools/native-java/Dockerfile unconditionally COPYs it; actual installed smoke failed resolving it as a registry image without that context — cost if wrong: optional provider builds must still use their separate pinned Maven stage. RED missing context then corrected.
- grant Bake fs.read only for this attempt's owned empty Maven context — installed Buildx refuses the additional context outside its working directory without that explicit entitlement — cost if wrong: the exact owned context path is readable to that local build; no global filesystem check is disabled. RED missing allow flag then GREEN41. Second failed attempt also independently cleaned, no marker.
- accept Docker CMD-only non-native artifacts and preserve both Entrypoint and Cmd for live identity checks — installed JavaScript image has no Entrypoint and a valid Cmd, while Python also uses Cmd — cost if wrong: native extraction still requires the approved absolute executable entrypoint; future native CMD-only layouts remain unsupported. Two proper RED regressions (valid CMD-only rejected, changed CMD accepted), GREEN49; quality hooks rerun after fixture typing correction. Third smoke cleaned independently and no marker.
- retain the full native-QR packaging failure and do not patch NanoFaaS, skip its case or rewrite the oracle in this NanoLab delivery — approved plan explicitly preserves blocked/partial qualification — cost if wrong: full qualification requires an upstream packaging correction and a fresh full run;58 remaining cases were not invoked after fail-fast.

Deferred minor findings: none.
