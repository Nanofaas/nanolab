# Control-plane heap analysis

The `heap-analysis` workflow is a diagnostic, not a P24 measurement. It deploys
one NanoFaaS source snapshot on the local container backend, drives a bounded
k6 workload against the two word-stat functions, and captures two post-full-GC
control-plane JVM heap dumps around it: one after warmup, one after the natural
drain. Once the deployment is released, it runs eight headless Eclipse MAT
report invocations over the dump pair. There is no acceptance policy, no
retention gate, and no prerequisite settlement — `soakPolicyFile` and every
P24 criteria/retention/prerequisites block are omitted by construction.

## Running it

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh run packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

`inspect` and `plan` resolve and compile the workflow without touching Docker,
building anything, or making a network call:

```bash
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

`run` requires a local container environment, `docker` and `k6` on the host, and
a `docker buildx` builder whose driver supports BuildKit attestations
(`docker-container`, not the plain `docker` driver) and which can reach
`localhost:5000` — the build step publishes `--provenance=mode=max`, which the
`docker` driver rejects outright, and a `docker-container` builder has a
`localhost` of its own, so it needs `network=host` or an equivalent bridge at
the moment it is created. The run acquires the registry itself, and removes it
again only if it was the run that started it; the builder stays yours, because
creating one with the right network mode needs a driver option nanolab does not
pass. It owns its own
endpoints: `--environment`, `--control-plane-url` and
`--prometheus-url` overrides are rejected, as is `--resume`/`--only`/`--from`/
`--until` partial selection. There is no `--teardown` mode and no `--keep`:
cleanup is automatic on every terminal path, and MAT cannot start until the
deployment is released, so there is nothing a kept run would let you inspect
that the artifacts below don't already give you.

Exit codes mirror soak's: `PASS` is 0, `FAIL` is 1, `INCONCLUSIVE` is 2,
`ABORTED` (Ctrl-C) is 130.

## Timeline

The shipped preset's application phases run for a fixed ~24 minutes:

| Phase | Duration |
| --- | --- |
| Warmup | 120 s (2 min) |
| Steady (workload + baseline dump before it) | 900 s (15 min) |
| Natural drain (before the final dump) | 420 s (7 min) |
| **Application total** | **1440 s (24 min)** |

MAT analysis runs afterward, once the deployment is released, and is bounded
separately by `mat_timeout_s` (300 s / 5 min in the shipped preset) — so a full
run is the 24-minute application timeline plus up to ~5 more minutes of MAT,
around 29 minutes end to end.

## Resource bounds

| Setting | Shipped value | Why |
| --- | --- | --- |
| `max_dumps` | 2 | Exactly baseline and final; the model rejects any other count. |
| `max_dump_bytes` | 1 GiB (1073741824) | The CAPTURE-side reservation only. Must stay at or below 1 GiB: the helper's tmpfs volume contract caps the derived per-capture quota at 1 GiB (`_diagnostic_resource_inputs` in `tasks/soak/runtime.py`), and a larger value fails deployment, not just analysis. It is **not** an analysis-side limit — see below. |
| `artifact_limit_bytes` | 8 GiB | Evidence budget shared across build receipts, dumps and workload receipts. |
| `mat_memory_mib` / `mat_cpus` | 2048 / 2.0 | The bounded, single-shot MAT container's memory and CPU limits. |
| `mat_timeout_s` | 300 s | Hard wall-clock bound on the eight MAT report invocations. |

**MAT applies no dump-size limit and no work-area quota.** By explicit
operator decision, a dump that was captured is always analysable:
`MatAnalysisRequest` does not take `max_dump_bytes` and never compares the
baseline+final size against a ceiling. Refusing a pair after capture throws
away a 24-minute run and protects nothing, because the bytes are already on
disk. For the same reason MAT's `/work` area is an **unbounded disk-backed
directory** (`<run-dir>/mat-work`, removed when MAT finishes), not a sized
tmpfs: tmpfs pages charge to the container's own memory cgroup, so a work area
large enough for MAT's on-disk indices would be OOM-killed against
`--memory` (`mat_memory_mib`) long before its quota could bind. With `/work`
on disk, `mat_memory_mib` governs only MAT's JVM heap, which is what it always
meant. `mat_memory_mib`, `mat_cpus` and `mat_timeout_s` remain in force.

The MAT container runs with an exact owned name
(`nanolab-mat-<run-dir-name>`) and `nanolab.run` / `nanolab.mat` labels, and
is removed with `docker rm --force` in a `finally`. Without that, a MAT still
running when the supervisor SIGKILLs the `docker` CLI — most likely on the
`mat_timeout_s` path — survives the run holding its CPU and memory
reservation with nothing able to find it.

### The helper image is built per run, not pinned in the scenario

The scenario names no helper image. Every run builds
`assets/soak/diagnostic-helper.Dockerfile`, publishes it to the same registry
preparation already pushes the application images to, and freezes the
`repository@sha256:<64 hex>` digest that build reported. The capture side and
MAT then use that one digest, so both ends of a run are provably the same
image.

A digest written into the scenario instead would name bytes that exist only in
whichever registry produced them: anyone else checking the repository out gets
a reference nothing can pull, and a `docker image prune` breaks it even for the
machine that built it. What must stay reproducible is the *input*, and it is —
the MAT archive in `assets/soak/mat.lock.json` and the base images in
`assets/soak/helper-bases.lock.json`, all digest-pinned. Bumping either lock
file changes the next run's helper with no scenario edit.

The cost is a build at the start of each run, which the layer cache absorbs
after the first. `HeapAnalysisOptions.helper_image` skips it when a digest is
already published, and `HeapAnalysisOptions.helper_builder` names the buildx
builder to use (default `nanolab-heap-analysis`).

## Sensitive artifacts

**Heap dumps and MAT reports can contain request payloads and other captured
runtime values.** `report.json` marks itself `"sensitive": true` for exactly
this reason. Treat the whole run directory as sensitive: do not attach it to a
public issue or share it outside the operator's own trust boundary without
first checking what the captured functions were processing.

## Artifact tree

```
<run-dir>/
├── report.json                  # terminal diagnostic receipt (see below)
├── terminal.json                # frozen CLI status contract (nanolab-soak-v1)
├── heap-analysis-journal.jsonl  # Sonata workflow journal
├── soak-compose.json            # the owned, frozen Compose project
├── ownership-intent.json        # pre-provisioning ownership record
├── evidence/
│   ├── config.json              # resolved deployment protocol
│   ├── payloads.json            # workload payload fixtures
│   ├── builds/                  # per-role build output
│   ├── source/                  # captured source snapshot
│   ├── runtime-<checkpoint>.json    # runtime-before-baseline / -natural-drain /
│   │                                # -after-final-gc
│   ├── native/                      # retained raw readings for the checkpoints
│   │                                # that have them (optional; see below)
│   ├── natural-<phase>.json         # natural-baseline.json, natural-drain.json
│   ├── warmup/, steady/, drain/     # one k6 receipt directory per phase
│   ├── baseline-gc/, final-gc/      # GC captures (diagnostic.json, events.jsonl)
│   └── baseline-heap_dump/, final-heap_dump/
│       └── artifacts/capture.hprof  # the two post-full-GC control-plane dumps
├── mat-work/                     # MAT's disk-backed work area; removed on exit
└── analysis/                     # written only after the deployment is released
    ├── manifest.json             # MAT run manifest: status, dump hashes, report list
    ├── mat-worker-receipt.json   # raw per-invocation MAT worker receipt
    ├── docker-run.log            # the MAT container's bounded output
    └── reports/                  # the eight MAT report outputs, flat (see below)
```

### Optional memory readings

Heap-analysis collects `GC.heap_info` and full procfs mappings at
`before-baseline`, `natural-drain`, and `after-final-gc`. The last observation
is immediately after the verified explicit full GC and before the final dump.
The dump requests a further full GC, so its effects are outside that reading.

Each `evidence/runtime-<checkpoint>.json` contains a `native` block with parsed
measurements, source intervals, completion states, errors and raw artifact
references. Available sources are retained as:

    evidence/native/<checkpoint>-status.txt
    evidence/native/<checkpoint>-smaps-rollup.txt
    evidence/native/<checkpoint>-smaps.txt
    evidence/native/<checkpoint>-heap-info.txt

`report.json` compares all three checkpoints and shows missing evidence
explicitly. The report carries that comparison only: per-mapping records and
the large-mapping list are not repeated there, so
`large_anonymous_mappings.mappings` appears as a pointer string naming the
checkpoint record, not as a list or a count — do not read that string as a
count, nor its absence as lost data. Partial or malformed smaps produces no
mapping totals; complete sibling sources remain usable. Exceptionally large
parsed summaries are trimmed to their totals in the checkpoint record, with
complete raw evidence retained; only a block still over budget after that trim
is marked unavailable.

Paths inside that comparison keep the base they were produced with. In
`report.json`, `native.<checkpoint>.sources.<key>.artifact.path` is relative to
the run's `evidence/` directory (`native/before-baseline-status.txt`), whereas
the trim pointer string (`see evidence/native/<checkpoint>-smaps.txt`) only
resolves from the run root. The report's other path fields
(`dumps.<name>`, `analysis_manifest`, `workload_receipt`) are run-root paths,
absolute for a CLI run.

Committed heap is not resident heap. Stable committed heap does not establish
that RSS growth is outside Java heap, and net live-set decline can mask growth
in individual object populations. Large anonymous mappings describe virtual
regions, not glibc arena counts or allocator ownership. Process residency uses
the kernel's anonymous, file and shared-memory categories; mapping labels do
not classify every resident page, including copy-on-write pages.

These are bounded diagnostics with collection overhead: heap-info acquires the
JVM heap lock. Completed optional reading errors do not themselves change the
verdict. Unresolved command completion stops further diagnostics and follows
owned-target cleanup; cancellation remains cancellation. All evidence shares
the run's artifact budget. The P24 soak and Node default observation paths
enable neither reading and retain their existing behavior.

The diagnostic helper uses the local Linux Docker backend and validates target
PIDs through the host's `/proc`. Its absolute monotonic deadline assumes this
supported shared clock domain and includes exec startup delay. A forwarded
Docker socket or a daemon in another kernel is not supported by this protocol.
The host deadline remains the outer bound; a late worker may return missing
readings or be interrupted, and unresolved JVM completion follows owned-target
cleanup. No timeout representation alone guarantees timely acknowledgement.

For procfs sources, `not_started` means the deadline prevented the attempt,
`failed` means the attempted read raised an error, and `completed` means the
read returned. Source availability and parsing errors are reported separately.
For JVM commands, completion acknowledges the command lifetime: an ordinary
positive error exit can be completed with an error; a missing or signal exit
does not establish completion.

## Interpreting the result

Three different things can each independently look like "something is wrong,"
and they are not the same claim:

- **Diagnostic `PASS` / `FAIL` / `INCONCLUSIVE`** (`report.json.status`,
  `terminal.json.status`) says only whether the *receipt* is complete: the
  workload ran without request failures, both post-full-GC dumps were
  captured, and MAT produced every required report. `PASS` is explicitly **not**
  a no-leak verdict and **not** P24 qualification — `report.json.meaning`
  states this in the receipt itself.
- **A MAT suspect** is a finding inside `analysis/reports/`: the
  `org.eclipse.mat.api:suspects`/`suspects2` reports name specific object
  groups MAT's heuristics flag as possible leaks, and `compare` shows what grew
  between the baseline and final dump. A suspect is MAT's opinion about one
  pair of dumps, not a confirmed defect — read the report before concluding
  anything.
- **A confirmed Java leak** is an operator conclusion reached by reading the
  MAT reports (dominator tree growth that tracks the workload rather than
  settling, a suspect that reproduces across runs, retained-size growth with
  no corresponding drop after drain) — this workflow does not compute that
  conclusion for you.
- **P24 qualification** is a separate, numerical `soak` workflow run with a
  reviewed `soakPolicyFile` criteria set and full retention-gated evidence
  (see [docs/soak.md](soak.md)). A heap-analysis `PASS`, and even a clean MAT
  report, says nothing about P24 status: `heap-analysis` never runs the soak
  lifecycle, carries no criteria, and never qualifies anything.

MAT's own batch-mode report generation (the `ParseHeapDump.sh` invocations
this workflow drives headlessly) is documented officially at
[Running the Leak Suspects report from the command line](https://help.eclipse.org/latest/topic/org.eclipse.mat.ui.help/tasks/runningleaksuspectreport.html),
which covers the `org.eclipse.mat.api:suspects2` report id used here; the same
page's report-id table covers `org.eclipse.mat.api:compare` for the
baseline/final comparison report.
