# Control-plane heap analysis workflow

Date: 2026-09-15
Status: implemented

Amended 2026-09-15 after the first live runs: the analysis-side dump and
work-area budgets this spec originally mandated were removed by operator
decision. See "Resource and safety bounds".

## Purpose

Add a small, repeatable NanoLab workflow that captures comparable JVM heap
dumps before and after a bounded workload and runs Eclipse Memory Analyzer
(MAT) reports over both dumps. The workflow is a diagnostic tool. It does not
qualify P24, replace the single-version soak workflow, or decide whether an
RSS increase is acceptable.

The first version analyzes only the NanoFaaS control plane. The workload may
still invoke both Java and JavaScript functions so that it exercises the same
control-plane paths as the existing memory soak.

## Goals

- Capture a post-warm-up, post-full-GC control-plane heap dump as the baseline.
- Capture a post-load, post-drain, post-full-GC control-plane heap dump.
- Run standard headless MAT reports on both dumps after releasing the NanoFaaS
  deployment.
- Preserve enough runtime and memory evidence to interpret the reports.
- Reuse existing NanoLab build, deployment, k6 workload, JVM attachment, full-GC
  evidence, artifact hashing, and cleanup behavior where their contracts fit.
- Keep the public command identical to other NanoLab workflows.

## Non-goals

- No P24 PASS or FAIL decision.
- No RSS return-to-baseline criterion.
- No retention-policy, ownership-metric, or soak-profile validation.
- No SDK or Node.js heap analysis in the first version.
- No custom leak detector, OQL library, report server, or MAT UI automation.
- No comparison with another NanoFaaS revision.
- No permanent diagnostic endpoint or new NanoFaaS instrumentation.

## Public shape

The workflow name is `heap-analysis`, not `soak-mat`, to keep diagnostics
separate from soak qualification. The initial checked-in scenario is:

```text
packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

It runs through the normal NanoLab entry point:

```bash
NANOFAAS_ROOT=/path/to/nanofaas \
  ./nanolab.sh run \
  packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

The scenario fixes the first useful diagnostic recipe rather than exposing
speculative flexibility:

- one NanoFaaS version;
- container backend;
- JVM control plane as the only dump target;
- Java and JavaScript word-stat functions as workload destinations;
- 120 seconds of warm-up;
- 900 seconds of measured load at 200 requests per second, split equally;
- 420 seconds of natural drain;
- two control-plane heap dumps;
- an explicit total artifact limit and a capture-side per-dump reservation
  (there is no analysis-side dump limit; see "Resource and safety bounds");
- a digest-pinned diagnostic/MAT helper image.

The timing and rate fields use the existing workload configuration types where
possible. The workflow does not inherit the soak policy file or P24 criteria.

## Lifecycle

The Sonata workflow executes these owned phases in order:

1. Capture source provenance, build all application images, freeze their
   digests, and deploy one NanoFaaS version using existing primitives.
2. Register the two workload functions and run the 120-second warm-up.
3. Record control-plane RSS, PSS, cgroup memory, `GC.heap_info`, effective JVM
   flags, and Native Memory Tracking output when NMT is enabled.
4. Request a control-plane full GC and verify its completion using the existing
   JVM diagnostic evidence contract.
5. Capture `baseline/control-plane.hprof`, validate its size, and hash it.
6. Run the existing k6 workload for 900 seconds at 200 requests per second,
   retaining the normal k6 summary.
7. Stop the generator, wait for the 420-second natural drain, and capture the
   unperturbed RSS/cgroup checkpoint before any diagnostic GC.
8. Request and verify the final full GC, then capture
   `final/control-plane.hprof` with the same limits and mechanism as baseline.
9. Capture the same JVM and operating-system measurements used at baseline.
10. Release the NanoFaaS deployment and its application resource reservations.
11. Run headless MAT analysis over each dump in a bounded helper container.
12. Write the analysis manifest and terminal report, then remove temporary
    helper resources.

The workflow must attempt owned cleanup after interruption or failure. MAT runs
only after the application deployment is released so its memory allowance does
not compete with the measured services.

## Reuse boundary

The new workflow may import existing low-level facilities from the soak package:

- image preparation and digest freezing;
- deployment acquisition and function registration;
- workload configuration and k6 runner;
- `JvmDiagnosticAdapter` and verified `GC.run`/`GC.heap_dump` capture;
- artifact writer, hashing, quotas, cancellation, and cleanup helpers.

It must not call the complete `create_soak_lifecycle` orchestration or satisfy
its P24 policy by inventing empty criteria. If a reusable facility is trapped
inside soak orchestration, extract only that facility into a neutral module;
do not create a second framework or broadly refactor soak.

## MAT execution

The existing digest-pinned diagnostic helper image is extended with the
headless MAT distribution and its launcher. This avoids another image lifecycle.
Including MAT in the image increases disk size but consumes no additional
runtime memory during dump capture because MAT is not started then.

For each dump, the helper runs only standard MAT reports:

- overview;
- leak suspects;
- top components.

It also runs the standard two-snapshot reports with the final dump as the
subject and the baseline dump as its reference:

- `org.eclipse.mat.api:compare`;
- `org.eclipse.mat.api:suspects2`.

MAT receives an explicit CPU, memory and wall-clock limit, and runs under an
exact owned container name and owner label so it can always be found and
removed. Its output directory and its work area are owned directories mounted
read-write, while the HPROF inputs are mounted read-only. The work area is
disk-backed and uncapped: see "Resource and safety bounds". The workflow records the exact helper digest, MAT version, command,
exit status, duration, report hashes, and report sizes.

The first version does not parse MAT HTML into a new semantic model and does
not claim that a Leak Suspects report proves a leak. Individual and comparison
reports are retained for human or agent review.

## Artifacts

The stable artifact set is (as produced; this replaces the six speculative
paths the first draft named, which the workflow never wrote):

```text
evidence/runtime-before-baseline.json
evidence/runtime-natural-drain.json
evidence/runtime-after-final-gc.json
evidence/natural-baseline.json
evidence/natural-drain.json
evidence/steady/k6-summary.json
evidence/steady/workload-receipt.json
evidence/baseline-gc/diagnostic.json
evidence/baseline-heap_dump/artifacts/capture.hprof
evidence/final-gc/diagnostic.json
evidence/final-heap_dump/artifacts/capture.hprof
analysis/reports/
analysis/manifest.json
analysis/mat-worker-receipt.json
report.json
```

`analysis/reports/` is flat: every MAT report output from both dumps lands in
one directory, named by the launcher, and `analysis/manifest.json` is what
binds each file to its `(dump, report_id)` pair. There are no
`analysis/baseline/` and `analysis/final/` subdirectories.

`analysis/manifest.json` links every report to its input dump SHA-256 and records
all commands and tool versions. HPROF files may contain request payloads and
other sensitive values, so the report marks the bundle as sensitive and the
workflow never prints heap contents to the terminal.

## Outcomes

- `PASS`: workload completed without request failures, both verified post-GC
  dumps were captured, and all required MAT reports completed.
- `FAIL`: the application workload violated its explicit success contract.
- `INCONCLUSIVE`: build, deployment, attachment, full-GC proof, dump capture,
  artifact budget, or MAT execution was unavailable or incomplete.

`PASS` means the diagnostic receipt is complete. It is not a no-leak verdict.

## Resource and safety bounds

- Exactly two dumps are permitted in the initial scenario.
- A capture-side per-dump reservation and the total artifact limit are checked
  before capture and after copy.
- **There is no analysis-side dump-size limit and no MAT work-area quota.**
  This spec originally mandated finite limits on both. The operator removed
  them: a dump that was captured successfully is always analysable, because
  refusing it afterwards throws away a 24-minute run and protects nothing —
  the bytes are already on disk. `MatAnalysisRequest` therefore takes no
  `max_dump_bytes` and performs no size comparison. The consequence is
  accepted: a pathologically large dump pair costs MAT wall-clock time until
  `mat_timeout_s` expires, and costs disk under `<run-dir>/mat-work`, which is
  removed when MAT finishes.
- MAT's `/work` area is a disk-backed directory owned by the run, not a sized
  tmpfs. A tmpfs charges its pages to the container's own memory cgroup, so a
  work area sized for MAT's on-disk indices is OOM-killed against `--memory`
  long before any quota could bind. On disk, `mat_memory_mib` correctly
  governs only MAT's JVM heap.
- Dump and report files are hashed and must remain stable while copied.
- JVM diagnostic actions require an owned target and explicit permission to
  perturb it.
- MAT starts only after application teardown and receives finite CPU, memory
  and wall-clock limits, plus a bounded captured output log. It runs under an
  exact owned name and owner label and is removed with `docker rm --force` in
  a `finally`, because SIGKILLing the `docker` CLI never reaches the
  container.
- Partial reports remain evidence, but produce `INCONCLUSIVE`.
- Helper containers and temporary volumes are removed on every terminal path.

## Testing

Keep tests narrow and mostly synthetic:

- configuration accepts the checked-in scenario and rejects multiple targets,
  missing limits, unpinned helpers, and non-JVM targets;
- lifecycle ordering proves natural drain evidence precedes final GC and dump;
- baseline and final dump receipts are bound to the correct checkpoint;
- MAT commands use read-only HPROF mounts and bounded resources;
- MAT failure produces `INCONCLUSIVE` and still performs cleanup;
- manifest generation binds report hashes to dump hashes;
- one local smoke test uses small synthetic dumps or a tiny JVM fixture.

The unit suite does not run the 24-minute workload or require Docker. A real
container smoke is an explicit operator action.

## Deferred work

Add SDK JVM targets, Node.js snapshots, configurable target lists, automatic
retained-size deltas, or CI scheduling only after the control-plane workflow
proves those additions useful. None are part of this design.
