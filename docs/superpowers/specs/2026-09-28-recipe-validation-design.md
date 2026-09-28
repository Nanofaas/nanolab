# Recipe-backed container validation

Date: 2026-09-28
Status: implemented on feat/recipe-v2-validate; local container E2E passed.

## Goal and scope

Migrate `packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml` to
build through NanoFaaS recipes v2, deploy exactly the resulting images, and run
the existing lifecycle checks. Profiles must be reusable files in the NanoLab
repository. The first profile is `packages/nanolab/recipes/validate-container-jvm.yaml`:
JVM control plane, container provider and build metadata, Java JVM word-stats.
Success requires a real local Docker run with identity and image checks, as well
as the existing invocation and resource assertions.

Native builders, services, bash, multiarch, other backends and release workflows
are later slices. The first profile verifies v2 build identity, not every v2 feature.

## Decision: two alternative NanoLab tasks

Introduce `AssembleRecipeTask` and `PublishRecipeTask` in `nanolab.tasks.recipe`.
Both are parametrized by a recipe and belong in NanoLab. Reuse Sonata's command
executor and Gradle task; no new engine task protocol or Sonata release is needed.
No NanoFaaS Gradle plugin change is required.

Each task accepts the profile, a writable staged NanoFaaS checkout, a unique run
tag and an output directory. `AssembleRecipeTask` executes `assembleRecipe`;
`PublishRecipeTask` executes `publishRecipe`, which includes assembly. They are
alternatives, not sequential stages. There is no public `publish` boolean and no
`PublishDistributionTask`. Share command construction and report parsing without
introducing a generic task framework.

Both commands receive `-Precipe`, `-PrecipeTag` and `-PrecipeOutput`. Do not forward
legacy module/native build properties that conflict with recipe ownership.
Each returns a distribution containing report path, recipe hash, source identity,
modules, control-plane image/build identity and function images keyed by
`(name, sdk)`. Keep the original report as evidence. Expose the value through an
ordinary Sonata resource/dependency so deploy/register tasks resolve it at
execution time. Do not hide a nested workflow inside one task.

For this initial single-platform implementation, assembly requires every image
to have status `built` and a local image ID. Publication requires every image to
have status `published`, a local image ID and a verified registry digest.
A failed command never returns a usable distribution, even if a report exists.
The first `validate/container` migration uses `PublishRecipeTask` with the local
registry, because the container provider may pull the function image.

## Profiles and scenario selection

Add optional `recipeProfile` to validate scenarios. Resolve it relative to the
scenario file, never the process working directory; carry its absolute resolved
path into planning. The migrated scenario selects
`../recipes/validate-container-jvm.yaml`. Custom profiles remain normal YAML files.
Recipe/schema semantics continue to be validated by NanoFaaS, not reimplemented
as a second recipe schema in NanoLab.

Initially accept this option only for `workflow: validate`, `backend: container`,
a local environment and Docker builds. Reject conflicting explicit image/runtime
or variant overrides, asynchronous/envelope checks, retry-burst checks and recovery
modes until their recipe contracts are supported. The scenario still owns function payloads, registration
names and resource assertions. Match catalog metadata `(name, sdk)` to the report;
do not guess names by stripping image suffixes. Require exactly the selected
functions, one control plane and no unconsumed services in this initial slice.

## Source isolation and run evidence

NanoLab's `NANOFAAS_ROOT` is read-only. Stage a writable copy beneath the run
directory before invoking Gradle; moving only `recipeOutput` is insufficient.
Preserve the selected revision and tracked working-tree changes in the staged
copy, without copying build products, credentials or caches. Capture original
source provenance separately and verify the staged tree matches the intended
inputs. Do not silently build a clean HEAD when the user selected dirty sources.
Fail with a useful error if the source cannot be reproduced.

The run directory contains the exact profile bytes, source provenance, staged
source, distribution output, command logs and runtime verification evidence.
Use a unique image tag per run, supplied through `recipeTag`, without mutating the
checked-in profile. Reuse the same run tag/output on retry; never deploy from a
report left by an earlier failed invocation. Keep build evidence after teardown.
Planning and dry-run must not build, create containers or write to NanoFaaS.

## Resource ordering and deployment

1. Prepare the run's source and profile copies.
2. Acquire the existing local registry resource.
3. Run `PublishRecipeTask`, targeting that local registry.
4. Validate the completed report before acquiring Compose or functions.
5. Acquire Compose with the control-plane image resolved from the report.
6. Register the selected functions using their report image references.
7. Run identity, invocation and resource checks.
8. Release functions, Compose and registry through existing compensation.

Keep registration references registry-qualified: the Docker provider may pull
images. Never reinterpret a local image ID as a registry digest. Compose uses
the produced image, with build disabled explicitly (`--no-build`, or a generated
compose definition without a build section). Merely omitting `--build` is not a
sufficient guarantee that Compose will never build. Existing shared Sonata
`build=False` only omits `--build`, so handle this within NanoLab's recipe path.
Preserve the existing network, callback URL, Docker socket mount and readiness
checks. Do not duplicate function artifact/image builds after recipe assembly.

## Report and runtime checks

Reject a missing/malformed report, unsupported report schema, wrong recipe hash,
wrong effective tag, missing/duplicate selected components, missing image IDs,
wrong module/build identity, or an image that violates the selected task's status
contract. The validation workflow requires published images and verified digests. This
slice uses ordinary single-platform Docker reports and rejects `platforms`.
An unsuccessful Gradle task fails the workflow even if a partial report exists.

Fetch `/modules/build-metadata` and require the profile's variant, JVM build type,
optimization `c2`, and exact module set. Compare source identity where supplied
and save the response. Locate Compose's running control plane with `compose ps`,
inspect its container `.Image` and compare it to the report's local `image.id`.
Do the same for the managed function instance. A tag match alone is insufficient.
Retain the existing HTTP correctness/resource checks and their cleanup behavior.

## Change surface and verification

Expected changes: scenario loader/config, `plans/validate.py`, new recipe task and
report reader, Compose acquisition for dynamic image/no-build, function resource
registration for dynamic report images, run-directory plumbing from CLI/TUI, and
the migrated scenario. Keep the recipe option additive for existing callers.

GitNexus reports HIGH upstream impact for `build_validate_plan`: direct caller
`cli.product._workflow`, then CLI plan/run and TUI paths. The NanoLab index is two
commits behind HEAD; this is a lower bound, not a complete regression inventory.
Refresh/recheck impact before implementation changes.

Tests must cover:

- profile resolution independent of current directory and rejection of conflicts;
- both task commands and their distinct built/published report contracts;
- report parsing, wrong hash/tag/components, partial publication and failed build;
- runtime consumers using report references even when defaults differ;
- Compose cannot rebuild, and no duplicate function build tasks are scheduled;
- source staging preserves intended inputs and leaves NanoFaaS unchanged;
- dry-run purity and compensation when identity/invocation checks fail;
- a local Docker lifecycle run whose saved report, metadata and inspected image
  IDs agree, followed by teardown and retained evidence.

Validate the checked-in profile with the actual NanoFaaS `validateRecipe` resolver
before implementation. Passing that preview is not an end-to-end result.
