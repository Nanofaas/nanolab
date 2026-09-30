# Runtime comparison prepared with recipes v2

## Purpose and scope

Migrate the preparation phase of `nanolab compare` from handwritten Gradle,
Docker and push commands to NanoFaaS recipe v2. Keep the comparison's existing
question intact: compare control-plane builds while the Java and JavaScript
function images, cluster, workload, resource limits and measurements are fixed.
The first end-to-end proof is one `jvm` cell with one repetition on Multipass.
The implementation must prepare all nine currently supported variants, including
native builds, but the full matrix is not a gate for this slice.

`compare` remains the owner of VM provisioning, selected variants, cell order,
retry, resume, k6, Prometheus, report generation and cleanup. A recipe describes
only what is built and published. Cells do not run recipe tasks or build images.

## Approaches considered

1. **One checked-in profile per variant, with functions in the `jvm` profile
   (chosen).** Publish `jvm` first and the other selected variants afterward.
   The profiles are directly reusable with `validateRecipe` or `publishRecipe`,
   and the two functions are built exactly once. A selection without `jvm`
   still prepares its JVM distribution to obtain those functions; this costs
   one extra JVM build before the measurement.
2. **Generate a different profile for each run.** This avoids the extra JVM
   build for non-JVM selections, but the effective recipe is hidden in generator
   code and the checked-in profiles cannot be used as-is. It adds branching to
   a preparation phase whose main value is an auditable input.
3. **Add multiple control planes to one recipe.** This would remove repeated
   Gradle calls, but changes the NanoFaaS recipe contract and report schema for
   one NanoLab workflow. The existing recipe contract intentionally has one
   control plane per distribution.

## Profiles and build identity

Add nine profiles under `packages/nanolab/recipes/`, named
`comparison-<variant>.yaml`, for the keys in `VARIANTS_BY_KEY`. Each uses the
VM registry `127.0.0.1:5000/nanofaas`, a distinct control-plane image name
`control-plane-<variant>`, and the exact existing comparison modules:
`k8s-deployment-provider` and `async-queue`. Do not add `build-metadata`: it
was not in the measured module set. Consequently omit `build.variant`, which
requires that module. The profile name, hash, recorded options and manifest
identify the variant; no claim is made that a runtime build-metadata endpoint
contains the variant.

The `jvm` profile includes both scenario functions: recipe component `word-stats`
with Java JVM build and component `word-stats` with JavaScript SDK, with distinct
image names. Map them to scenario keys `word-stats-java` and
`word-stats-javascript` and verify names and SDKs before provisioning. The other
eight profiles contain no functions.
All profiles declare their JVM arguments or native options explicitly, including
`-XX:+UseSerialGC -XX:TieredStopAtLevel=1` for `jvm`. This corrects NanoLab's
stale assumption that the Dockerfile defaults to C1; NanoFaaS now defaults to
SerialGC with full tiering. The new `jvm` recipe deliberately builds C1 so it
matches the comparison's documented baseline. Historical `jvm` samples made
without an explicit tier flag may have measured C2 and must not be treated as
equivalent to the new samples. Every variant's effective
collector, tier, event-loop setting and native optimization must match its
current label and rationale. Native G1 still requires Oracle GraalVM in the VM.
Preserve `--native-build-memory` and `--native-parallelism` by forwarding their
existing Gradle properties to recipe publication.

Use one unique run tag shared by the nine profiles. Their image names are
distinct, so references cannot collide. A run directory may be resumed only
when its captured NanoFaaS source revision and patch, recipe hashes, tag and
variant selection still match. Changed inputs require a new run directory.

## Preparation and handoff

Capture the NanoFaaS checkout once, including tracked edits, and stage it on
the stack VM. Copy the checked-in profiles into that captured run. Publish the
`jvm` recipe first, then each selected variant other than `jvm`, in one VM
registry and on one VM source checkout. `publishRecipe` performs assembly and
publication in one call. Each invocation has its own output directory; fetch
`distribution.json` and Gradle log to `RUN_DIR/prepare/<variant>/`. Retain the
captured profiles and source identity in the run directory. Cleanup removes
only run-owned VM staging after successful verification; diagnostics remain on
the host after failure. Leave the VM alive through all cells.

Validate each distribution before starting any cell: recipe SHA-256, source
identity, run tag, exact module set, expected control-plane mode and options,
published status, nonempty digest and exact function set (two for `jvm`, none
otherwise). Reject duplicate image references or digests that do not match
the selected artifact. A failed publication or invalid report stops the matrix
before deployment or k6. The shared function images and each control-plane
image come from the validated distributions, never from a constructed variant
tag or a fresh build in a cell.

Pass the selected control-plane reference and the two shared function references
to every cell through the existing prebuilt-image inputs. Use digest-qualified
references when the Kubernetes/Helm path accepts them; otherwise verify the
registry's manifest digest against the report immediately before each cell and
verify the running Pod image identity after deployment. A mutable tag by itself
does not prove a fixed artifact. Keep the existing load-test and measurement
task graph unchanged. The scenario's `controlPlaneVariant` remains the human
matrix key, while recipe selection belongs to `compare`, not `recipeProfile` on
each cell.

Write the matrix manifest before preparation so an interrupted run remains
legible. After each successful publication, update it atomically with the
profile path/hash, distribution path, effective build options, image reference
and digest for that variant; record the shared function references and digests
once. The report reader continues to consume existing k6 summaries and metrics
snapshots. A resumed cell must use the same prepared image identities as its
completed peers. Since `compare` may provision a fresh VM on resume, it may
republish the same captured inputs to restore the registry; compare every new
digest to the previous manifest before running a cell. Stop if a digest differs.
`--fresh` reruns cells with the same prepared identities; a new build needs a
new run.

## Verification and documentation

- Validate all nine checked-in profiles with `validateRecipe` against the pinned
  NanoFaaS checkout. Assert exact modules, functions, build flags, image names
  and unique references in focused tests.
- Test preparation with a fake VM publisher: `jvm` first, then each selected
  non-JVM variant once, functions exactly once, one source snapshot, per-variant
  reports, and no legacy build operations. Cover invalid reports and an
  interrupted/resumed run with changed source or digest.
- Test that each cell receives report-derived prebuilt references and that no
  Gradle, Docker build or push task appears in its plan. Preserve the k6 script,
  Prometheus queries, resource settings, interleaving and retry assertions.
- Run a Multipass `compare` with `--variants jvm --repetitions 1`; inspect the
  published report, registry/Pod image identities, k6 summary, metrics snapshot
  and generated comparison report. Numerical equality with an earlier run is
  not expected.
- Run Ruff, type checking, relevant NanoLab tests and `git diff --check`.
  Update `packages/nanolab/recipes/README.md`, comparison usage docs and
  `docs/recipes-roadmap.md` after the end-to-end evidence exists.
