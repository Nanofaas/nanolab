# Sonata extraction assessment — issue #61

Assessment of [issue #61](https://github.com/Nanofaas/nanolab/issues/61), after
merging #59 and #60. The issue is a candidate list, not an approved list of
modules to move. Decisions below concern individual capabilities. An example
outside NanoFaaS is a proposed acceptance test, not evidence of an existing
external consumer.

Inspected revisions:

- NanoLab: `38408bb7d54a2d311d307d6c67f65a1f19aa92f1`.
- Sonata: `07270a8d7dd603301808e043acc2693fc43b6a63`, version `0.6.5`.

At the time of this assessment, extraction and publication had not been
performed. Implementation and delivery evidence for the first slice is recorded
in [the owned-process plan](../plans/2026-10-06-owned-process-extraction.md).
NanoFaaS was not modified.

## Decision criteria

Extract a capability when it has a useful contract independent of NanoFaaS,
removes a meaningful implementation from NanoLab, and preserves observable
failure and cleanup behavior. Prefer an extension of an existing Sonata module.
Keep product policy and small compositions of existing tasks local. A function
using only the standard library does not by itself justify a new catalogue API.

Do not introduce a generic task for every helper. A process runner, parser or
storage primitive can be an ordinary API used by tasks. Sonata engine retains
zero runtime dependencies; these capabilities belong in `sonata-tasks`.

## Individual decisions

| Capability and current source | Decision | Reason and boundary |
| --- | --- | --- |
| `soak/processes.py`: `OwnedCommandRunner`, `OwnedCommandResult`, `run_owned_command` | Extract first | Used by builds, workload execution, diagnostic commands, helper builds and MAT. Local argv execution, deadlines, bounded output and descendant ownership are independent of the workload. Extend `sonata_tasks.process` without changing the existing managed-process API. |
| `soak/processes.py`: summary framing | Preserve with the runner initially | Markers and a separate output path are configurable, and both outputs share one byte budget. Moving only the basic runner would leave a second supervisor implementation for diagnostics/workloads. Keep interpretation of the resulting summary in NanoLab. |
| `soak/artifacts.py`: exclusive writer, atomic immutable publication, bounded append/raw writes | Extract after separating policy | Meaningful storage implementation used by soak and heap analysis. Ownership, partial-write accounting, quotas and immutable publication are reusable. `.soak-owner`, fixed terminal reservation and cumulative accounting exclusions need an explicit boundary. |
| `soak/artifacts.py`: `read_records` | Extract only the decoder | Complete malformed records must fail; a torn tail must remain distinguishable. The current decoder fabricates a `nanolab-soak-v1` observation-gap record. That record construction belongs in a NanoLab adapter. |
| `soak/artifacts.py`: `fingerprint` | Keep byte-compatible hashing local | Storage review demonstrated that JSON normalization followed by `sonata_tasks.core.fingerprint.fingerprint_digest` changes hashes for accepted nested numeric-key mappings: numeric sorting before JSON encoding differs from lexical sorting after normalization. Preserve the current bare-hex hash of the codec's exact canonical bytes. The small stdlib hash does not justify another shared API or an evidence-identity migration. |
| `soak/artifacts.py`: `describe_artifact` | Consolidate with artifact work | Streaming SHA-256 and byte count fit the storage capability. Do not create a separate hashing task or framework. |
| `soak/artifacts.py`: `retained`, `describe_tree`, `measure_tree`, `enforce_limit` | Keep current accounting policy local | They encode scratch-directory exclusions, source-tree handling and acceptance inventory rules. A generic writer must not silently exclude all `workspace-*` or dot directories. Consider shared traversal only if separating storage actually requires it. |
| `tasks/recipes/builder.py`: private Buildx builder and owner-node reconciliation | Extend existing Buildx resource; third slice trial implemented | Add opt-in strict ownership to the existing `Resource[str]`, preserving default reuse/replacement. Refuse existing names, use a unique owner node and remove only the unchanged single-node docker-container builder. Reconcile partial creation/cancellation; callers serialize client-store mutation during inspect/remove. The ordinary installed-wheel AMD64/ARM64 build passed with an isolated Docker client. NanoLab adoption awaits a published coherent pair. |
| `tasks/recipes/builder.py`: binfmt installation, lock and registration compensation | Defer generic API pending ownership validation | This affects a shared host facility. The current lock lives on the client and is keyed by Docker daemon ID. That does not demonstrate protection for remote clients or multiple daemons sharing a binfmt instance. Preserve inspection/compare-before-removal and builder-before-registration cleanup ordering. |
| `tasks/recipes/builder.py`: pinned installer/probe images, platform pair, registry config, evidence names | Keep local | These select the supported AMD64/ARM64 recipe setup and registry `127.0.0.1:5000`. They are inputs/policy for shared resources, not Sonata defaults. |
| `application/execution.py`: `_RemoteProjectExecutor` | Candidate for a later execution-adapter extension | Mapping a local project subtree to an explicit remote root is reusable. Preserve rejection of escaping cwd and simultaneous cwd/remote_dir, binding identity and dry-run. Keep default `/nanofaas`, environment/provider selection and role assembly local. |
| `application/execution.py`: `prometheus_over_ssh` and process cleanup | Reuse process lifecycle; defer a new tunnel API | Sonata's managed-process resource already has readiness, termination and failed-acquire compensation. `registry_tunnel_resource` is a remote systemd/socat forwarder and is not an SSH substitute. Keep provider selection, URL policy and identity discovery in NanoLab; extract a dedicated SSH forwarder only when a distinct reusable contract is demonstrated. |
| `tasks/vm/sync.py`: `repo_rsync_command` | Keep as a small policy adapter | The useful behavior here is the repository exclusion list and destructive mirror options. Ordinary rsync argv construction alone does not justify an abstraction. Sonata file transfer is not equivalent to a filtered mirror. |
| `tasks/infra/ansible.py`: `bundled_ansible_root` | Keep local | It locates NanoLab's packaged playbooks; there is no generic execution implementation to extract. |
| `tasks/components/bootstrap.py`: bootstrap/sync/retarget plans | Keep composition local | Uses shared `build_ansible_argv` and SSH helpers already. Playbooks, namespaces, remote layout, registry and operation IDs belong to NanoLab. Revisit only an independently demonstrated missing Ansible primitive. |
| `tasks/compose.py`: isolated Compose acquisition | Small extension to existing resource | Sonata already owns deploy/wait/compensation and teardown flags. NanoLab adds teardown before deployment. An opt-in pre-clean step can remove this duplicate composition; retain explicit role/env inputs and fresh-project policy locally. Do not enable destructive pre-clean by default. |
| `soak/retention.py`: `CleanupState`, generated Compose validation and release journal | Keep local | Cleanup records, release-only replay, dependent cleanup gates, ownership labels, allowed mounts and artifact retention are soak contracts. This is not interchangeable with a generic workflow journal. No generic journaling framework is justified by this module. |
| `soak/sources.py`: capture/verify/materialize snapshot and entry identity | Extract after artifact storage | Real generic contract: tracked plus nonignored working-tree inputs, deletions, modes, safe relative symlinks, bounded size and independently verified workspaces. Keep `nanolab-soak-v1` receipts and recipe/source selection locally. |
| `workspace/provenance.py`: provenance helpers and `source_fingerprint` | Keep for now | Git observation is generic, but current return shape and fallback behavior support NanoLab run metadata and image rollout tags. Its hash identifies commit/status/diff/untracked bytes, unlike the snapshot's file inventory. Do not unify those identities merely because both use SHA-256. |
| `release/build.py` and `release/resources.py`: immutable archive transfer | Extend `sonata_tasks.archive` in a later slice | Existing Sonata archive exports a commit anew for each acquisition. Release shares one frozen archive across VMs, checks it again and reports cleanup failures. Support those behaviors before adopting it; retain clean-source guards, remote-path policy and recipe staging locally. |
| `tasks/loadtest/prometheus.py`: HTTP transport | Already shared | It delegates HTTP/time/range acquisition to Sonata. Retain NanoLab retry defaults and representation adapters; do not move another HTTP client. |
| `tasks/loadtest/tasks.py`: `CapturePrometheusSnapshot` and window alignment | Later candidate, with explicit policies | Acquisition over an explicit query set can be reusable. Required-query decisions, leading/trailing margins, skew tolerance, diagnostic hints and JSON layout need separation. Preserve collecting all queries and retaining partial results before failing. |
| `tasks/loadtest/models.py`: `K6Config`, `TimeWindow`, `PrometheusQuery` | Keep until a consuming shared API needs them | Sonata already owns `K6Stage`, `K6RunResult` and generic `K6Config`. NanoLab adds target resources and payload selection. Avoid duplicating those shared models or moving isolated dataclasses with no shared consumer. |
| `metrics/interpretation.py`: k6 normalization, per-label counter delta, point statistics | Later extraction of pure calculations | Used by detailed reports and release evidence; not provided by Sonata's scrape checks. Preserve missing/invalid versus zero and resets per publisher. `is_counter` includes NanoFaaS's `function_dispatch` exception, which must remain a local rule. |
| `release/metrics.py`: aggregation, regression, baseline selection and record rendering | Keep local | Uses `statistics.median` already. Metrics, comparable-profile fields, autoscaling/k6 gates, thresholds and release records form a product contract. A generic benchmark framework would add API without removing that contract. Reuse the shared k6 interpretation when available. |
| `release/secrets.py`: private-file validation/copy and temporary remote staging | Later candidate | Private regular files, no-follow/identity-checked copying, temporary directories and cleanup are reusable. Use Sonata execution/transfer ports rather than copying the loosely typed provider access. Preserve sanitization of errors and no secret values in command/journal metadata. |
| `release/secrets.py`: GHCR login, cosign credentials and release error names | Keep adapters local | Registry/login/signing decisions and release-facing results belong to the release workflow; feed them shared private-file lifetimes when available. |
| `soak/collector.py`: bounded Linux procfs/Docker/HTTP collection | Optional later capability | Raw collection and before/after identity checks can support other container diagnostics. Preserve the killable helper boundary, byte/deadline bounds and local-container constraints. It must not become mandatory for portable Sonata tasks. |
| `soak/adapters.py`: `RoleBoundProbe`, sample/target/phase conversion | Keep local | Requires NanoLab domain models, required-metric units and source classification. A new role abstraction would duplicate existing execution bindings. |
| `heap_analysis/native.py`: procfs and JVM parsers | Consider pure parsers separately, later | Byte/unit normalization and collector-specific heap parsing are plausible reusable APIs. Keep the response-envelope interpretation and diagnostic criteria local. No current external consumer was established. |
| `heap_analysis/mat.py`: `MatAnalyzer` | Keep current workflow; defer generic runner | It fixes eight reports for a baseline/final pair, bundled locks/workers, helper image policy and evidence layout. Moving the entire module would export NanoLab's experiment. A future optional MAT task needs independently useful report/tool inputs first. |

Paths in the issue predate the previous refactors: `cli/execution.py` is now a
compatibility facade over `application/execution.py`; recipe builder lives under
`tasks/recipes`; shared measurement interpretation lives under `metrics`.
Diagnostic assets live under `assets/diagnostics`. Extraction must follow the
current owners, not recreate the old layout.

## First slice: Linux process ownership

Use `sonata_tasks.process` as the public entry point. Keep
`managed_process_resource` behavior and its portable use cases intact; it
currently stops only the process it spawned. The owned command runner is a
separate Linux-only capability requiring procfs, subreaper support and pidfds.
Replacing the owned runner with `killpg` or ordinary terminate/wait is not an
equivalent simplification: descendants may establish new sessions.

Carry the current constructor/run/stop/result semantics over first. Keep the
supervisor self-contained and preserve cancellation before launch, one-shot
execution, output quotas, summary framing, descendant adoption and confirmed
reaping. Interpret command failures and receipts in NanoLab's domain adapters.
Unsupported hosts must fail before launching the supervised command.

There is a packaging dependency beyond Python imports:
`assets/diagnostics/diagnostic-helper.Dockerfile` currently copies
`tasks/soak/processes.py` into the helper, where `diagnostic-worker.py` imports
`processes` directly. Simply replacing that file with a Sonata import breaks the
helper. The extraction must choose and verify one distribution route for the
published Sonata implementation inside the helper image, remove the old source
copy and update helper build provenance. The pinned helper Python is 3.12.14.

Acceptance for this slice:

1. Sonata tests run without NanoLab/NanoFaaS and include an ordinary Python child
   command as the independent use case. Existing synthetic descendant tests
   already show that the algorithm does not require NanoFaaS.
2. Transfer the runner tests from `tests/soak/test_process_regressions.py` and
   the shared-runner cases in `tests/soak/test_workload.py`. Exercise cwd/env/exit,
   timeout, quota, cancellation, caller exception, detached descendants and
   survival of an unrelated process. Keep workload-specific cases in NanoLab.
3. Keep existing Sonata process tests passing, including failed readiness and
   acquisition compensation.
4. Verify the installed published wheel and the helper's runtime import path;
   then run NanoLab workload, build executor, diagnostics, helper and MAT tests.
5. Remove the local supervisor implementation only after the released dependency
   is available. A small import/compatibility adapter may remain where needed.

## Subsequent slices and prerequisites

1. **Artifact storage:** neutral ownership and decoding; NanoLab keeps terminal
   reservation, accounting exclusions and gap-schema adapters. Transfer immutable
   write, concurrent-owner/append, corruption, torn-tail, quota, symlink and disk
   failure tests. Independent use case: a bounded command audit directory.
2. **Owned Buildx:** extend strict ownership and compensation in the existing
   resource while preserving existing reuse behavior. Independent use case: an
   ordinary multi-platform application build. Binfmt remains a separate decision
   gated on locking/host identity tests, including remote clients.
3. **Source snapshots:** depends on the storage decision. Independent use case:
   build a modified/untracked ordinary Git repository into separate workspaces.
   Preserve concurrent-change detection, executable bits, deletion entries,
   escaping/dangling/cyclic symlink rules and manifest verification. Sonata's
   `git archive` resource does not capture working-tree changes and cannot
   substitute for this contract.
4. **Small existing-API extensions:** opt-in Compose pre-clean and explicit
   remote cwd mapping can be separate PRs. Test deployment/readiness failures,
   compensation and untouched existing defaults; test relative/absolute cwd,
   escaping paths, explicit remote_dir conflicts and dry-run forwarding.
5. **Measurements and credentials:** independent later slices, each with an
   ordinary non-NanoFaaS example and retained failure evidence. Keep benchmark
   and release policy local. Diagnostics follow only if their separate contracts
   justify an optional API.

Each slice requires Sonata's tests/types/import contracts and an installed-wheel
check. Publish the coherent `sonata-engine`/`sonata-tasks` pair before changing
NanoLab's three exact pins or lockfile; engine publication precedes tasks.
Do not use Git dependencies or silently point NanoLab at the sibling checkout.
Do not weaken coverage thresholds to facilitate a move. Closing #61 requires
the implemented/adopted slices and explicit disposition of deferred candidates;
this assessment alone does not close it.

## Evidence collected for this assessment

Commands were run against the revisions above. These are selected baseline
checks with coverage disabled, not a full CI/coverage result:

- NanoLab: process regressions, artifacts, source snapshots, recipe builder,
  measurement interpretation, release secrets and provenance — **101 passed**.
- Sonata: process, Buildx, Compose, canonical fingerprints and archive —
  **41 passed**.
- Three canonical-JSON probes, including nested values and non-ASCII text,
  produced equal NanoLab and Sonata hashes after removing Sonata's `sha256:`
  prefix. Storage review additionally tested nested numeric-key mappings and
  found incompatible ordering after JSON normalization. That counterexample
  changes the individual decision above: keep byte-compatible hashing local
  while sharing the codec and artifact descriptors.

Builder tests use fake command execution; passing them does not establish
correctness across real Docker daemons, remote clients or binfmt namespaces.

## Third slice: owned Buildx trial

The [implementation plan](../plans/2026-10-06-owned-buildx-extraction.md) starts
from NanoLab `d49c228` and Sonata `ced86ed` (published pair 0.6.7). The next pair,
0.6.8, is prepared locally; NanoLab's declared pins and lock remain 0.6.7 until
publication. Trial verification explicitly installs the built pair in an
isolated environment.

Buildx ownership and binfmt remain separate decisions. The shared resource
handles strict name refusal, owner-node identity, bootstrap/validation and
compensation. NanoLab retains daemon architecture, pinned installer/probes,
registration comparison, registry configuration, platform pair and evidence.
If cleanup cannot confirm builder removal or absence, including interrupted
creation, NanoLab retains its registration, records the error and closes its
local lock. That lock still provides no cross-client or shared-kernel guarantee.

The installed base wheels performed an ordinary scratch application build on
the local Docker daemon with a fresh `DOCKER_CONFIG`, using cached BuildKit.
The OCI output contained AMD64 and ARM64 manifests; client selection remained
unchanged and release removed the builder and its container. This proves the
Buildx capability independently of NanoLab. No emulation was installed; the
experiment does not establish remote-client or binfmt locking correctness.
