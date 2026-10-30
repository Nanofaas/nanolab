# August GC/JIT/native settings, plus interpreter

This is the selected campaign after the user's 30 October correction. Reuse the
historical `comparison` workflow, not the separate experiment-01 QR/64 MiB
pilot. The only added execution configuration is `jvm-int` (Serial, `-Xint`).
Existing defaults remain unchanged; select the eight configurations explicitly.

| Key | Execution | GC |
| --- | --- | --- |
| jvm | JVM C1 only | Serial |
| jvm-g1 | JVM C1 only | G1 |
| jvm-c2 | JVM full tiering | Serial |
| jvm-g1-c2 | JVM full tiering | G1 |
| native-os | Native -Os | Serial |
| native-o3 | Native -O3 | Serial |
| native-o3-g1 | Native -O3 | G1 |
| jvm-int | JVM interpreter, -Xint | Serial |

## Resource and heap policy

| Existing scenario | Control-plane CPU limit | Container memory limit |
| --- | --- | --- |
| runtime-comparison-cpu1.yaml | 1 | 2048 MiB |
| runtime-comparison-cpu2.yaml | 2 | 2048 MiB |
| runtime-comparison-mem1024.yaml | 2 | 1024 MiB |
| runtime-comparison-mem512.yaml | 2 | 512 MiB |

JVM recipes add no Xmx or Xms. The NanoFaaS launcher sets
`-XX:MaxRAMPercentage=70`; actual maximum heap is selected by the JVM under the
container's memory limit, not a common 64 MiB. Native recipes add no maxHeap;
retain the selected native runtime's default heap sizing. Do not claim JVM and
native therefore have equal heaps or that either is unbounded. Container
limits and heap limits are different quantities.

This is grounded in NanoLab August tree `90ab774`, NanoFaaS August tree
`6fc6bd26` (Docker JVM launcher and native entrypoint), and NanoFaaS
`docs/experiments/baseline-2026-08/README.md`. The archived baseline records
1 CPU/2 GiB, 2 CPU/2 GiB, 2 CPU/1 GiB and 2 CPU/512 MiB conditions.

Native recipes now reuse the standard container builder: Community for Serial,
Oracle for G1. This replaces the host toolchain requirement without adding a
new installer. The source, plugin and toolchain versions are current pinned
versions, not the August binaries; record actual distributions and digests.
Oracle/G1 also changes distribution and instrumentation, so its contrast is
not a pure collector effect.

## Workload and execution

Retain the historical word-stats Java and JavaScript functions and the existing
comparison arrival calendar, concurrency and queue configuration. The JVM
publisher builds the shared function images once; `jvm-int` declares no
functions and reuses those digests. No QR workload, fixed 64 MiB heap, or
experiment-01 step calendar is introduced into this comparison.

```bash
export NANOFAAS_ROOT=/path/to/pinned/nanofaas
RUNTIMES=jvm,jvm-g1,jvm-c2,jvm-g1-c2,native-os,native-o3,native-o3-g1,jvm-int
./nanolab.sh compare runtime-comparison-cpu1.yaml \
  --environment /path/to/azure-comparison.yaml \
  --variants "$RUNTIMES" --repetitions 3 \
  --native-build-memory 8g --native-parallelism 4 \
  --run-dir /path/to/new-august-cpu1-run
```

Run each of the other three scenarios with its own new run directory. The
compiler memory option is independent of runtime heap sizing. Three
repetitions give 24 cells per resource condition and 96 across all four.
These commands can provision infrastructure; they are an operator recipe, not
a cloud run or a spending approval. Never resume a previous 64 MiB/QR run with
these settings; existing identities and receipts reject incompatible resumes.

Use the existing `compare-report /path/to/run` for the historical report.
Experiment-01's phase/cohort report remains specific to that separate protocol;
it is not evidence of the historical calendar. Keep failures and generator
shortfall visible, especially if the interpreter cannot sustain the offered
load. New results must not be pooled with the August results.

The plugin's generic JVM optimization metadata can say `c2` when no
TieredStopAtLevel flag is supplied, including with `-Xint`. The new variant is
classified as interpreted in NanoLab; verify actual launch arguments and
UseCompiler=false rather than treating that generic field as proof of C2.

## Verification boundary

Regression tests check publisher reuse, interpreted classification, absence of
explicit runtime heap bounds and the four existing resource conditions.
Validation/build/startup evidence is recorded in the follow-up ledger.
No Kubernetes workload or performance campaign has been run for this update.

Final checks: 381 targeted tests passed, type check clean, five import contracts
kept, Ruff clean, wheel/sdist and extracted-wheel smoke passed.
[Verification record](testing/2026-10-30-august-settings/verification.json) and
[interpreter smoke](testing/2026-10-30-august-settings/interpreter-smoke.json).
The follow-up did not rebuild the three native images or rerun the full Linux CI.
