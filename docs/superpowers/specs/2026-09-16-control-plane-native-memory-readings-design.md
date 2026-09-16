# Control-plane native memory readings

Date: 2026-09-16
Status: proposed

## Purpose

Add two optional memory readings to the `heap-analysis` checkpoints:
`GC.heap_info`, which reports committed and used heap, and the full
`/proc/<pid>/smaps`, which describes memory mappings and their residency.
These readings help investigate RSS growth; they do not identify its cause
on their own.

Two independent heap-analysis runs measured net decreases of 38,031 objects /
1.48 MB and 38,459 objects / 1.50 MB across 180,002 requests. The decrease was
dominated by a Caffeine cache evicting roughly 5,000 `Outcome` entries. This
shows a smaller aggregate Java live set at the final dump in those runs.
It does not exclude growth in individual object populations, transient heap
usage, or growth over a different workload or observation window.

Committed heap is not resident heap. Previously committed pages can become
resident when touched, so stable committed heap does not prove that an RSS
increase is outside the Java heap. Growing committed heap with stable used
heap is evidence about heap capacity, not proof that GC tuning is the answer.
The report presents these measurements together without a causal verdict.

## Goals

- Record committed and used heap at each heap-analysis checkpoint.
- Record full smaps and a summary that distinguishes virtual mapping size,
  RSS and PSS, with explicit definitions for anonymous, file and shared memory.
- Compare `natural-drain` with `after-final-gc`, collected immediately after
  the explicit full GC and before the final heap dump, to observe changes in
  committed heap and process residency across that GC.
- Preserve the default P24 soak request, response and collection behavior:
  no new reads, commands, fields or output limits when options are absent.

## Non-goals

- No Native Memory Tracking. It needs a JVM startup flag and a new diagnostic
  operation; it is a separate sub-project.
- No `MALLOC_ARENA_MAX` A/B spike. It needs an `env` field on `RolePolicy`;
  also a separate sub-project.
- No allocator profiling (jemalloc/tcmalloc via `LD_PRELOAD`). Excluded by the
  operator: the cost is high and it replaces the allocator under investigation,
  so it measures jemalloc's behaviour rather than glibc's.
- No memory-growth verdict, pass/fail threshold or acceptance criterion.
  Unavailable optional readings do not themselves change the run's status;
  existing target-identity and diagnostic completion requirements still apply.
- No change to `_OPERATIONS`, to `capture()`, or to the diagnostic evidence
  contract.
- No continuous sampling. The readings happen at three checkpoints, not on
  a timer.

## Known limitations, accepted

The readings are confined to `heap-analysis`, whose measured window is roughly
24 minutes. A slow RSS drift that only manifests over a long P24 soak may not
appear here. Extending collection to soak remains a follow-up.

The readings are sequential observations, not an atomic process snapshot.
`GC.heap_info` does not request a GC, but it acquires the JVM heap lock and is
documented as having medium impact. Reading full smaps also has a collection
cost. Record collection intervals and describe the observations as bounded
diagnostics, not as non-perturbing measurements.

## Design

### Why not a diagnostic operation

`capture()` requires a natural checkpoint, a budget reservation, an owned
artifact directory and completion evidence. Keep that contract intact for
the existing operations. Reuse the helper transport for optional memory
observations without adding an operation to `_OPERATIONS` or a second
transport implementation inside `tasks/heap_analysis/`.

This choice does not exempt the new command from ownership, serialization,
deadlines or completion handling. In particular, cancelling a procfs reader
and interrupting an attached JVM command are different cases; killing `jcmd`
does not prove that the command inside the JVM has completed.

### Capture and opt-in behavior

Extend `read_memory()` with explicit keyword options `include_smaps=False`
and `include_heap_info=False`, forwarding enabled options to the worker.
Omit disabled options from the request. With neither option enabled, retain
the existing worker loop, response shape, command sequence and transport
limit:

```python
for name, limit in (("status", 65536), ("smaps_rollup", 262144)):
```

Read full smaps only when requested, with an 8 MiB raw byte cap. This is a
bounded evidence budget, not a guarantee that every JVM's mappings fit.
An over-limit read is recorded explicitly and never treated as complete.

The worker never infers whether the target is a JVM. Only heap-analysis asks
for both optional readings, for its explicitly selected JVM control-plane
target. Existing soak and Node callers request neither option.

For `GC.heap_info`, reuse `/opt/java/openjdk/bin/jcmd` targeting pid 1 and the
existing owned command runner. Require the diagnostic-capable helper with
the target's shared attach directory, not a procfs-only helper. Verify target
identity and serialize the request with other helper operations. Pass the
remaining request budget to the worker, bound command output using the
existing 2 MiB limit, and use bounded scratch files that are cleaned up.
The worker deadline must leave time for acknowledgement before the host
deadline. All reads and the command share one overall collection budget.

Collect `status`, `smaps_rollup` and optional smaps before optional
`GC.heap_info`, so the procfs sample precedes this checkpoint's attach command.
Record start/end times and per-source completion or error information for
the opt-in response. Do not change timestamps or metadata in legacy responses.

### Transport bounds

The existing `_DockerCommands.run()` output limit is 1 MiB, and the current
`read_memory()` uses that default. An 8 MiB raw smaps cannot simply be added
to its JSON response under that limit.

For opt-in requests only, derive a finite response limit from every enabled
raw field cap, JSON escaping and bounded metadata/errors. With the current
caps, a 64 MiB serialized-response ceiling covers up to six encoded bytes per
raw byte for status (64 KiB), rollup (256 KiB), smaps (8 MiB) and heap-info
(2 MiB), plus at most 64 KiB of serialized metadata/errors. Enforce the field
and metadata bounds, rather than relying solely on the outer ceiling.
Check peak worker memory for raw strings and serialization against the
provisioned helper memory. Retain the 1 MiB default for legacy requests.

The bound applies to the complete serialized response, not just the smaps
file. Exercise it through the actual JSON encoding and host decoding path.
An optional field exceeding its cap becomes a bounded error with no complete
value; it must not overflow the transport or invalidate the other fields.

### Flow and checkpoint ordering

The heap-analysis session uses its bound diagnostic helper to request the
optional readings and retains the raw response for artifacts and aggregation.
`read_memory()` returns new keys only when requested. The shared
`parse_procfs_memory(status, smaps_rollup)` parser and ordinary soak collection
remain unchanged. Aggregation lives in `tasks/heap_analysis/`.

Preserve three observation checkpoints, with this order:

```text
warmup
observe(before-baseline)
full_gc(baseline)
heap_dump(baseline)
steady load
natural drain
observe(natural-drain)
full_gc(final)
observe(after-final-gc)
heap_dump(final)
```

This moves `after-final-gc` ahead of the final dump. The current dump command
does not pass `-all`, so it requests another full GC as well as producing an
HPROF; an observation after it cannot isolate the preceding explicit GC.
Keep existing natural-checkpoint and full-GC completion requirements intact.

Use `natural-drain` versus `after-final-gc` for the immediate before/after GC
comparison. `before-baseline` is before the baseline GC and dump, so it is
not a matched post-GC baseline for measuring live-set growth. Label those
different states in the report. The readings show observed changes, not a
guarantee that a particular collector returns memory immediately.

### What the summary contains

From `heap_info`: committed and used heap, and the same pair for metaspace.
Retain raw output, normalize parsed values to bytes, and mark unrecognized
or missing fields unavailable rather than zero.

For process residency, retain the kernel's anonymous/file/shared-memory
categories: `RssAnon`, `RssFile`, `RssShmem` from status and `Pss_Anon`,
`Pss_File`, `Pss_Shmem` from smaps_rollup. Missing fields stay unavailable.
Do not classify resident pages solely by a mapping's pathname: a file mapping
can contain anonymous copy-on-write pages. These sequential sources can
differ in timing and accounting; do not force their totals to agree.

From full smaps: per-mapping size, RSS, PSS, permissions and backing label,
plus `large_anonymous_mappings` count and separate sums of virtual size,
RSS and PSS. Classify private anonymous mappings explicitly, including
unnamed mappings and `[heap]`, `[stack]`, `[anon:...]`; retain shared-memory
and unknown/special mappings as separate categories. The backing label
describes the mapping, not the backing of every resident page in it.

"Large" means virtual mapping size at least 32 MiB, using a named constant.
Report qualifying mappings individually. This is a descriptive filter, not
an arena detector: JVM heap regions and other allocations can pass it.
Glibc heaps can contain separate writable and `PROT_NONE` mappings, and
mapping count is not arena count. A 64 MiB-looking region is only a clue;
neither size nor anonymity establishes allocator ownership or resident usage.
Do not report a glibc arena count or attribute these bytes to glibc.

### Artifacts

At each checkpoint — `before-baseline`, `natural-drain`, `after-final-gc`:

```text
evidence/runtime-<checkpoint>.json          gains a "native" block with the summary
evidence/native/<checkpoint>-smaps.txt      raw, when available
evidence/native/<checkpoint>-heap-info.txt  raw, when available
```

Retain the raw status and smaps_rollup used for the residency summary in the
opt-in checkpoint evidence. Include per-source intervals, completeness and
errors, so the summary can be checked against its inputs. The `native` block
contains process-wide evidence, including Java heap mappings; its name does
not imply that all reported bytes are outside the heap.

`report.json` gains a three-column comparison with checkpoint timing and
GC/dump ordering made explicit. Report measurements and missing evidence
without recommending a cause or tuning change automatically.

Smaps contains addresses and mapped file paths, not heap contents. The bundle
is already marked sensitive and stays so. All new evidence remains subject
to the existing artifact storage budget.

## Error handling

An unreadable smaps, unsupported heap-info output or completed command error
is recorded per source and the run continues. These optional evidence errors
alone do not change PASS/FAIL/INCONCLUSIVE, and must leave the helper usable
for the mandatory GC and dump operations. Preserve successful sibling reads.

Handle optional errors inside the worker's structured response where possible;
do not turn them into transport failures. The current host `read_memory()`
closes the helper on transport exceptions. If that happens, never retain a
closed handle for later mandatory operations: recover through the existing
provisioner, rebinding adapters while preserving the session's diagnostic
budget and target identity, only after reader/command completion is resolved.

For an opt-in JVM command, removing the helper is not proof that the JVM has
finished the command. On timeout or cancellation, retain available completion
evidence and use the existing ownership/cleanup rules. Do not issue a later
diagnostic while command completion is unresolved. Unrecoverable attachment,
identity or cleanup failures still follow the existing run outcome contract;
optional collection must not hide infrastructure failure or user cancellation.
Legacy procfs-only cancellation behavior stays unchanged.

Truncation or an over-limit smaps is marked explicitly as partial/unavailable.
Publish no totals derived from incomplete smaps. Independently complete
status, rollup and heap-info readings remain usable and visibly distinguished
from the incomplete source. A missing block is visible in `report.json`, never
interpreted as zero or "no growth".

## Testing

Synthetic tests, without Docker:

- Default and disabled options preserve legacy worker requests, response keys,
  read/command sequence and 1 MiB transport limit; existing soak and Node
  callers do not enable either reading.
- Each option independently enables only its requested source; both together
  preserve the specified procfs-before-jcmd order and identity checks.
- An encoded smaps response above 1 MiB and valid near-cap responses survive
  host transport and JSON decoding, including escaping. Over-limit fields
  produce bounded errors without overflowing the complete response budget.
- Smaps fixtures cover exact 32 MiB boundaries, 64 MiB regions, large JVM-like
  mappings, `PROT_NONE`, shared memory, named anonymous mappings and file
  mappings with copy-on-write pages. Assert separate virtual/RSS/PSS values
  and no inferred glibc ownership; missing kernel category fields stay absent.
- The heap-info parser uses real output from the supported JDK/collector and
  handles missing/unrecognized fields without inventing zero values.
- Partial smaps publishes no smaps-derived totals, while complete sibling
  sources remain available.
- Completed jcmd failures and unreadable smaps retain errors, permit later
  mandatory diagnostics and leave an otherwise successful run `PASS`.
- Transport failure does not leave a closed helper cached; resolved recovery
  retains target identity and the existing diagnostic budget. Unresolved JVM
  completion prevents further diagnostics and follows existing cleanup rules.
- Cancellation and deadlines distinguish procfs-only reads from JVM commands;
  stopping a jcmd process alone is not accepted as JVM completion evidence.
- Session ordering is `natural-drain`, completed final GC, `after-final-gc`,
  final dump. Existing natural-checkpoint and capture requirements still hold.
- Reports preserve source intervals, checkpoint ordering and visible missing
  evidence without producing causal verdicts.

The next real heap-analysis run validates the integration; no dedicated live
run is added. Synthetic tests do not establish the real collection overhead
or whether the observation window reproduces the RSS drift.

## Deferred work

Native Memory Tracking, the `MALLOC_ARENA_MAX` A/B spike, and extending these
readings to the soak path remain separate sub-projects. Choose follow-up work
from the combined evidence and its limitations, not a committed-versus-used
decision rule that claims to identify the source of RSS growth.

## References

- [Java MemoryUsage: used and committed](https://docs.oracle.com/en/java/javase/25/docs/api/java.management/java/lang/management/MemoryUsage.html)
- [JDK 25 jcmd: heap-info impact and heap-dump GC behavior](https://docs.oracle.com/en/java/javase/25/docs/specs/man/jcmd.html)
- [OpenJDK HeapInfoDCmd implementation](https://raw.githubusercontent.com/openjdk/jdk25u/master/src/hotspot/share/services/diagnosticCommand.cpp)
- [Linux procfs: residency, mapping categories and smaps](https://docs.kernel.org/filesystems/proc.html)
- [glibc heap allocation and mapping protection](https://raw.githubusercontent.com/bminor/glibc/master/malloc/arena.c)
