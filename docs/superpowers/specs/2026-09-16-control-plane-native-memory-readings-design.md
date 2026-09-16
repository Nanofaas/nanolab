# Control-plane native memory readings

Date: 2026-09-16
Status: proposed

## Purpose

Add two non-perturbing memory readings to the existing `heap-analysis`
checkpoints: `GC.heap_info`, which separates committed heap from used heap, and
the full `/proc/<pid>/smaps`, which separates anonymous from file-backed memory
and exposes the large-anonymous-mapping signature of glibc malloc arenas.

The heap-analysis workflow established that the control plane's Java live set
does not grow: two independent runs measured -38,031 objects / -1.48 MB and
-38,459 objects / -1.50 MB across 180,002 requests, with the decrease dominated
by a Caffeine cache evicting roughly 5,000 `Outcome` entries. Whatever RSS
growth the control plane shows is therefore not live Java objects.

These two readings decide the next fork. If committed heap grows while used
stays flat, the JVM is not returning memory to the operating system and the
answer is GC tuning. If committed heap is flat, the growth is outside the heap
and the investigation moves to native memory.

## Goals

- Record committed and used heap at each existing heap-analysis checkpoint.
- Record the full smaps at each checkpoint, plus an aggregate summary that
  answers "how much is anonymous" without further processing.
- Make the `after-final-gc` checkpoint answer directly whether a forced full GC
  returns committed memory to the operating system.
- Leave the P24 soak path byte-identical.

## Non-goals

- No Native Memory Tracking. It needs a JVM startup flag and a new diagnostic
  operation; it is a separate sub-project, to be decided by what these readings
  show.
- No `MALLOC_ARENA_MAX` A/B spike. It needs an `env` field on `RolePolicy`;
  also a separate sub-project.
- No allocator profiling (jemalloc/tcmalloc via `LD_PRELOAD`). Excluded by the
  operator: the cost is high and it replaces the allocator under investigation,
  so it measures jemalloc's behaviour rather than glibc's.
- No verdict, threshold or criterion. These readings are evidence, and they
  never change the run's PASS/FAIL/INCONCLUSIVE status.
- No change to `_OPERATIONS`, to `capture()`, or to the diagnostic evidence
  contract.
- No continuous sampling. The readings happen at the three existing
  checkpoints, not on a timer.

## Known limitation, accepted

The readings are confined to `heap-analysis`, whose measured window is roughly
24 minutes. A slow RSS drift that only manifests over a long P24 soak will not
appear here. The operator accepted this in exchange for leaving the shared soak
observation path untouched. If the 24-minute window proves too short, extending
the same readings to soak is a follow-up, not a redesign: the capture mechanism
is shared already.

## Design

### Why not a diagnostic operation

`capture()` imposes the full diagnostic ceremony on every operation: a
mandatory natural checkpoint, a budget reservation, an owned artifact
directory, and completion evidence. That ceremony is correct for `GC.run` and
`GC.heap_dump`, which perturb the JVM. `GC.heap_info` and reading smaps perturb
nothing; they belong with RSS and PSS, which already reach the host through the
observation path, not through `capture()`.

Two alternatives were rejected. Adding a "read-only" class of operation would
carve an exception into `capture()`'s invariants — shared code the P24 soak
depends on, and the exact surface this design is trying not to disturb. A
private reader inside `tasks/heap_analysis/` would duplicate the helper
transport that already exists, which the heap-analysis design forbids as a
second framework.

### Capture

The diagnostic worker already reads the target's procfs files in one loop with
a per-file byte cap, collecting per-file failures into `result["errors"]`
without interrupting the other reads:

```python
for name, limit in (("status", 65536), ("smaps_rollup", 262144)):
```

`smaps` becomes a third entry in that loop with its own cap of 8 MiB. A JVM has
a few thousand mappings, so this is generous; the cap exists to bound the read,
not to trim it.

The worker already has `jcmd` at `/opt/java/openjdk/bin/jcmd`, targeting pid 1,
wrapped in a deadline-aware helper. `GC.heap_info` is one call to that existing
function.

The worker never infers whether the target is a JVM. The host asks for the heap
reading explicitly through a flag in the request configuration, so Node roles
take exactly the path they take today.

### Flow

`read_memory()` returns the raw dictionary as it does now, with new keys added
alongside the existing ones. `parse_procfs_memory(status, smaps_rollup)` reads
the two keys it knows and ignores the rest, so the shared parser needs no
change. Aggregation lives in `tasks/heap_analysis/`, never in shared soak code.

### What the summary contains

From `heap_info`: committed against used, and the same pair for metaspace.

From `smaps`: RSS and PSS split between anonymous and file-backed, and
separately the count and total bytes of large anonymous mappings, which is the
signature of glibc malloc arenas.

"Large" means a mapping whose size is at least 32 MiB. The threshold is a named
constant, not a literal scattered through the aggregator. A 64-bit glibc arena
is 64 MiB, so 32 MiB catches arenas while staying clear of the JVM's own
smaller anonymous regions; the summary reports each qualifying mapping's size
alongside the count, so a wrong threshold is visible in the evidence rather
than hidden by it.

### Artifacts

At each of the three existing checkpoints — `before-baseline`,
`natural-drain`, `after-final-gc`:

```text
evidence/runtime-<checkpoint>.json          gains a "native" block with the summary
evidence/native/<checkpoint>-smaps.txt      raw
evidence/native/<checkpoint>-heap-info.txt  raw
```

`report.json` gains a compact three-column comparison across the checkpoints,
so "does committed grow while used stays flat" is readable without opening
anything.

smaps contains addresses and mapped file paths, not heap contents. It is far
less sensitive than an HPROF. The bundle is already marked sensitive and stays
so.

## Error handling

A failed reading is recorded and the run continues. Neither an unreadable
smaps nor a failing `jcmd` makes the run `INCONCLUSIVE`: that status is
reserved for build, deployment, attachment, full-GC proof, dump capture,
artifact budget and MAT failures, and a 24-minute run must not be discarded
over a secondary reading.

Truncation is the real trap. A summary computed over a truncated smaps would
report totals that are wrong rather than missing, which is worse than silence.
So truncation is marked explicitly, and a truncated read yields a summary
flagged partial that publishes no totals.

A missing block is recorded visibly in `report.json`, so absent evidence cannot
be misread as "no growth".

## Testing

All synthetic, no Docker:

- the aggregator against a sample smaps containing anonymous and file-backed
  mappings, including several around 64 MiB, asserting the totals and the
  large-anonymous detection;
- the `heap_info` parser against real `jcmd` output;
- a truncated read produces a summary marked partial and never publishes
  totals;
- a failing `jcmd` and an unreadable smaps leave their errors recorded and the
  run still `PASS`;
- the worker's read loop extended to the new key.

The real check arrives free with the next real heap-analysis run; no dedicated
live run is added.

## Deferred work

Native Memory Tracking, the `MALLOC_ARENA_MAX` A/B spike, and extending these
readings to the soak path. Each is its own sub-project, and which of them is
worth doing depends on what committed-versus-used shows first.
