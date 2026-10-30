# August settings follow-up

User decision: use the August experiment settings, adding only the no-JIT case.
Reuse the historical comparison workflow, recipes, workload and resource presets.
Do not convert the separate QR/64 MiB experiment into an August replication.

- [x] Add an optional `jvm-int` historical variant and recipe: Serial + -Xint,
      no explicit Xmx. Keep the previous CLI defaults for existing users.
- [x] Verify interpreter classification, publisher reuse, unchanged existing
      recipe options and four August resource conditions with regression tests.
- [x] Validate the recipe with the pinned NanoFaaS plugin and run targeted tests,
      static checks and wheel asset checks.
- [x] Document explicit eight-variant commands and source evidence. No cloud run.

Sources: NanoLab August tree 90ab774; NanoFaaS August tree 6fc6bd26 and
`docs/experiments/baseline-2026-08/README.md`. August JVM used
MaxRAMPercentage=70; native Docker entrypoint was /app/application without heap
arguments and no explicit native maximum heap was supplied. Resource conditions:
1 CPU/2048 MiB, 2 CPU/2048 MiB, 2 CPU/1024 MiB, 2 CPU/512 MiB.
Current historical JVM image verification checks MaxRAMPercentage=70. Native
recipes retain their existing defaults, not an asserted equal JVM/native heap.
Modern code/plugin/toolchains still differ from August: this reruns the settings,
not the old binaries. Standard publication evidence captures the new identities.


## Verification and deviations

The recipe-resolution regression first failed on unknown jvm-int, then passed
with the implementation. The new CLI regression exercises interpreter selection,
shared publisher and resume with fake infrastructure. Source-backed targeted
suite and final results are preserved under docs/testing/2026-10-30-august-settings.
The original variant selection stays the CLI default; the eight-variant command
explicitly opts into the interpreter and omits event-loop diagnostic variants.

Native historical recipes now use the already-supported container builder,
without changing GC, optimization or supplying maxHeap. This avoids a separate
host Oracle installer. Existing publication identities reject incompatible
resumes. No new native image was built for this follow-up: the prior native
smokes used 64 MiB and do not establish the new default-heap runtime behavior.
All three changed native recipes pass the official pinned validator.

New interpreter: official validation, local image assembly, distribution parsing,
metadata verification and HTTP health passed at 1 CPU / 2 GiB. Actual JVM flags
show UseCompiler=false, UseSerialGC=true, MaxRAMPercentage=70 and MaxHeapSize
1503657984 bytes. This is a startup check, not workload performance. The local
source archive has the same synthetic identity/limitations documented in the
previous smoke evidence; campaign builds must use real pinned checkouts.
The plugin generic optimization field says c2 for Xint; NanoLab classification
and actual execution flags correctly distinguish the interpreter. No plugin
metadata contract was changed in this task.

Wheel/sdist build and extracted-wheel smoke outside the checkout passed; type
check reports 0 errors/0 warnings, Ruff and all five import contracts pass.
Temporary smoke container removed. No cloud provisioning, push or merge.

Final targeted rerun: 381 passed in 26.70s. Full Linux CI was not rerun for this follow-up.
