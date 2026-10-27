# Native control-plane heap fixed at build time

**Goal:** the three native builds of experiment 01 run with the same 64 MiB
maximum heap as the JVM builds, so `compare` stops refusing them.

**Approach:** GraalVM Native Image bakes a runtime option's default into the
binary with `-R:MaxHeapSize=<size>` at build time. Nothing is passed when the
container starts, so the Helm chart and the runtime arguments stay as they are.
The value becomes an option of the recipe's native build, next to
`optimization` and `gc`.

## Scope

In:

- one optional recipe field, `build.native.maxHeap`, carried by nanoFaaS to the
  native-image arguments and to the distribution report;
- nanolab: set it in the three native recipes, require it, drop the blocker.

Out (each one is a separate decision, not part of this work):

- runtime `-Xmx` or extra control-plane arguments in the Helm chart;
- reporting the heap through the `build-metadata` endpoint;
- `scripts/native-java-image.sh`: recipe builds pass `-P` properties to Gradle
  and do not go through it;
- any other native option (initial heap, young generation, monitoring);
- moving nanolab's CI pin for reasons other than this change.

The schema's native options are one definition shared by the control plane,
functions and services. The field is added there and works for all of them,
because restricting it to the control plane would take more schema work, not
less. Nothing besides the experiment recipes uses it.

## Part A: nanoFaaS

Work in a fresh clone or worktree on a branch. The operator's checkout
`~/Projects/nanofaas` has uncommitted changes and must not be touched.

**A0. Impact analysis (required by `AGENTS.md`).** Run GitNexus `impact`
(upstream) on `RecipeBuildProperties.putNative`, `effectiveNative`,
`NativeOptions`, `ownedFlags`, and on the method in `RecipeArtifacts` that
writes the distribution report's native options. Report callers and risk.
Stop on HIGH/CRITICAL, and confirm UNKNOWN with a text search.

**A1. Schema** (`tools/gradle-plugin/src/main/resources/recipes/recipe-v2.schema.json`):
in the native options add `maxHeap` as a string matching `^[1-9][0-9]*[mg]$`,
described as "Default maximum heap baked into the image (-R:MaxHeapSize)".
When omitted, Native Image keeps its own default.

**A2. Properties** (`RecipeBuildProperties`):

- `putNative`: `maxHeap` becomes `nativeMaxHeap`.
- `ownedFlags`: add `nativeMaxHeap`, so a `-P` passed by hand clashes with the
  recipe as the other native flags do.
- `NativeOptions` and `effectiveNative`: carry `maxHeap`, `null` when absent.

**A3. Build** (root `build.gradle`, next to `nativeGc`): if `nativeMaxHeap` is
present, add `-R:MaxHeapSize=${nativeMaxHeap}` to the native-image `buildArgs`.

**A4. Distribution report** (`RecipeArtifacts`): write `maxHeap` in the
component's `native` options only when it is set, so existing reports stay
byte-identical.

**A5. Tests**, following the existing `nativeGc` tests in
`RecipeBuildPropertiesTest`, `RecipeContainerBuildTest` and `RecipePluginTest`:

- recipe field to property;
- a hand-passed flag is rejected;
- valid and invalid values in the schema;
- the report carries the value only when it is set;
- the argument reaches `buildArgs`.

**A6.** Run `detect_changes` before committing, then a PR and a merge. Write
down the merged revision.

**Verification on the first real native build** (not possible locally): the
binary starts with no arguments, and the control plane's actuator shows
`jvm_memory_max_bytes{area="heap"}` equal to 64 MiB. That scrape is already
saved in every cell's `scheduler-metrics.txt`.

## Part B: nanolab

**B1.** Move the nanoFaaS pin in `.github/actions/setup-workspace/action.yml`
to the revision merged in A6, and rerun the suite against it.

**B2.** Add `maxHeap: 64m` to the `native` block of the three native recipes.
Check them against the new schema at the pin.

**B3.** In `comparison/profiles.py`:

- `experiment_01_options` requires `native.maxHeap` to equal
  `f"{CONTROL_PLANE_MAX_HEAP_MIB}m"`;
- `declared_options` keeps the value in the manifest's native options;
- remove `EXPERIMENT_01_BLOCKERS`, `experiment_01_blockers` and the refusal in
  `experiment_01_profiles`.

**B4.** Before load, compare the distribution report's `native.maxHeap` with
the recipe. Use the existing distribution check where it compares native
options; do not add a new task. Keep the running heap in `scheduler-metrics.txt`
as observed evidence, not as a requirement.

**B5.** Update the tests:

- native recipes are accepted, and refused without `maxHeap` or with another
  value;
- the CLI test of the refused native build becomes a drift test.

Update `docs/experiment-01.md` (remove the blocker, explain the build-time
heap) and the two READMEs.

**Done when:** the full `compare` selection, all seven keys, passes argument
validation and `experiment_01_profiles` without blockers; the suite and the
checks pass against the new pin; the docs no longer mention a blocker.
