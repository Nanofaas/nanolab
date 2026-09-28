# NanoFaaS recipe profiles

These reusable NanoFaaS recipes describe builds; NanoLab scenarios describe how
to deploy and test them. Keep generated distributions and run evidence out of
this directory.

| Profile | Control plane | Function | Purpose |
| --- | --- | --- | --- |
| `validate-container-jvm.yaml` | JVM, container provider, build metadata | Java JVM word-stats | First recipes v2 container lifecycle validation |

The profile uses the local registry `127.0.0.1:5000` and single-platform Docker
images. It exercises the v2 build variant and its runtime metadata. It does not
cover native builds, services, bash functions, or multi-platform publication.

## Use directly

From a **disposable NanoFaaS working copy**, with the absolute path to this
NanoLab checkout in `NANOLAB_ROOT`:

```bash
./gradlew validateRecipe \
  -Precipe="$NANOLAB_ROOT/packages/nanolab/recipes/validate-container-jvm.yaml"

./gradlew assembleRecipe \
  -Precipe="$NANOLAB_ROOT/packages/nanolab/recipes/validate-container-jvm.yaml" \
  -PrecipeTag=recipe-v2-jvm-my-run \
  -PrecipeOutput="$PWD/build/recipes/my-run"
```

`validateRecipe` checks the profile and resolves its components; it does not
build or test the platform. `assembleRecipe` builds images and writes
`distribution.json`. To publish, use `publishRecipe` instead, after starting a
local registry. Publication performs assembly itself, so do not run both commands
as separate steps of the same workflow.

Use a unique `recipeTag` and output directory for each run. The checked-in tag
is only a convenient default for manual use. NanoLab must keep `NANOFAAS_ROOT`
read-only, including Gradle build/cache files, hence the disposable working copy.

## NanoLab integration

`deployment-lifecycle-container.yaml` selects this profile. Its validation
workflow runs NanoLab's `PublishRecipeTask`, which calls Gradle `publishRecipe`:
assembly and registry publication in one command. For a local build without
publication, `AssembleRecipeTask` calls Gradle `assembleRecipe`. The two tasks are
alternatives; do not run them in sequence for one distribution.

The recipe task stores the copied profile, staged source, Gradle log and
`distribution.json` under `runs/recipe-<id>/recipe/` by default. The runtime
metadata response and inspected image IDs are saved in `runs/recipe-<id>/`.
`--run-dir` chooses that run directory directly.
