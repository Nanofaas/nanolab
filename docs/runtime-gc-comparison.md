# GC, JIT and native comparison

Use the existing `compare` command with the experiment-01 arrival calendar.
Select the runtime group explicitly (the original default remains unchanged):

```bash
export NANOFAAS_ROOT=/path/to/pinned/nanofaas
RUNTIMES=jvm,jvm-g1,jvm-c2,jvm-g1-c2,native-os,native-o3,native-o3-g1
./nanolab.sh compare experiment-01-runtime-gc-pilot-cpu1.yaml \
  --environment /path/to/azure-comparison.yaml \
  --variants "$RUNTIMES" --repetitions 1 \
  --native-build-memory 8g --native-parallelism 4 \
  --run-dir /path/to/new-run
```

This command provisions infrastructure; review the resource/lifecycle proposal
before running it. CPU2 and CPU4 presets change only the control-plane CPU
allocation. Pilot values are provisional: 512 MiB container memory, 64 MiB heap,
fixed concurrency 2, queue 200. OOM/startup failures remain results; do not
increase one variant's heap during a run. Use a new protocol and run identity
if calibration requires a different common heap.

The seven recipe profiles reuse the existing runtime definitions. Only the JVM
publisher builds the shared Rust QR function. All profiles include exactly
k8s-deployment-provider, async-queue and build-metadata. Native recipes use the
NanoFaaS container builder, Community for Serial and Oracle for G1, without an
additional NanoLab installer. Oracle/G1 also enables JFR: that contrast changes
both distribution and instrumentation.

The existing compare-report command regenerates the experiment-01 JSON/CSV from
receipts and raw evidence. Per-phase JSON now includes `gc`: observed counter
increments, or null when unavailable. Counter samples retain publisher labels
for reset handling. The increment covers the observed samples within a phase,
not unobserved boundaries; it cannot recover individual GC pauses. MXBean and
JFR values are distinct sources and must not be summed.

Readiness, flags and build metadata do not prove C2 compiled application methods.
Confirm warmup and GC/JFR availability in the pilot. Keep function/generator
resource traces to distinguish their saturation from control-plane saturation.

The native compiler heap (`--native-build-memory`) is separate from the 64 MiB
runtime heap. Local G1 compilation completed with an 8 GiB compiler heap and
four threads, reaching 10.21 GiB peak RSS. Reserve additional builder memory;
an earlier 6 GiB attempt was cancelled during heavy compiler GC. This does not
change the runtime limits or prove that 6 GiB cannot complete.

Local verification on 30 October 2026: seven recipes accepted by NanoFaaS
validateRecipe with plugin 0.1.2; wheel/sdist build and extracted-wheel asset
smoke pass. The runtime group also emits experiment-01-report.html with existing
comparison charts; phase summaries retain cohort latency, errors and failures.

All seven recipes were also built and started on Linux ARM64, with the same
Rust QR function image. Existing metadata verification and QR readiness checks
passed for every runtime with a 64 MiB runtime heap and 512 MiB container limit.
These checks used an EXTERNAL function endpoint through the control plane;
they did not exercise managed Kubernetes deployment or establish performance.
The smoke source was an archive of the pinned code with a local synthetic Git
identity, not a campaign publication. Three tracked files absent from that
synthetic commit are listed in the evidence; no tracked build source changed.
Use the real pinned checkout and standard publication receipts for the pilot.

The full Linux CI run passed 5,414 pure, 645 source-contract and 53 TUI tests,
with 87.67% combined core coverage and all static/package gates passing.
A subsequent missing-origin chart correction passed 317 Linux tests plus type,
package and installed-wheel checks. The final optional unfiltered JFR query
and report caption passed 365 targeted tests against the pinned source.
The full CI suite preceded these last two small changes.

Raw JFR operation time now retains operation labels even when the existing GC
name filter does not recognize them. Wrapper, remark and cleanup operations
must not be summed into an asserted total GC pause. Missing generator origin
suppresses resource curves rather than inventing an alignment.

[Verification evidence](testing/2026-10-30-gc-native-refresh/verification.json),
[HTTP results](testing/2026-10-30-gc-native-refresh/http-results.json) and
[logs](testing/2026-10-30-gc-native-refresh/verification-logs.tar.gz) are local
engineering evidence, not paper results. No cloud resources were provisioned;
no registry push, merge or performance campaign was performed.
