# Recipe preparation for the AMD64 release

## Intent and scope

Migrate the AMD64 image build of the guarded NanoLab release to reusable
NanoFaaS recipes v2. Preserve the current release matrix, versioned image names,
source guard, VM placement, receipts, benchmark policy and publication barriers.
The desired simplification is to let NanoFaaS own artifact preparation and image
assembly instead of restating those commands in NanoLab's AMD64 Bake plan.

The approved first slice is AMD64. ARM64 build migration follows separately.
The existing ARM64 phase and shared infrastructure/source-test prerequisites
remain part of the release DAG. P24, other soak backends and smoke memory-policy
review are separate roadmap items. No memory or performance threshold changes
belong to this migration.

This spec authorizes a build-path design. Implementation requires review of
this written spec and a subsequent implementation plan. Verification of this
slice does not publish a public release or authorize changes to signing policy.

## Current contract

NanoLab main at `0ddf589` uses NanoFaaS
`e7914be065e844776af57fe9e449bce7f12e03c5`, the CI pin, for this design.
`build_release_request` archives the guarded commit for planning and derives an
`ImagePlan` from that tree. It requires a prepared version and a clean source
checkout. Source tests and staging bind execution to the same archive.

`build_amd64_phase` prepares JVM artifacts, then runs an AMD64 Bake graph with
`--load` on the Azure stack VM. Its receipt covers local image IDs, with explicit
AMD64 architecture checks. `build_registry_push_phase` pushes these images to
the stack registry and records registry digests. Benchmarks, ARM64 build/smoke,
GHCR publication, manifest creation, aliases and Cosign consume guarded receipts
and digests downstream.

The pinned source currently expands to 35 targets and 44 AMD64 cells:

| Group | Images | Existing tag suffix | Recipe selection |
| --- | ---: | --- | --- |
| JVM | 9 | `-amd64-jvm` | Control plane, 7 Java functions, warm-echo |
| Native | 12 | `-amd64-native` | Control plane, 7 Java functions, warm-echo, 3 Java-lite functions |
| Default | 23 | `-amd64` | 6 Bash, 5 Go, 5 JavaScript, 6 Python functions and watchdog |

These counts describe this pin, not a hard-coded future catalog. Planning must
compare profiles with the matrix derived from the guarded commit and reject
missing, additional or duplicated images before provisioning.

## Chosen approach

Use three reusable profiles and three root `assembleRecipe` invocations inside
the existing AMD64 build phase. Each profile owns one tag group. Keep registry
push as the next phase, consuming the completed local-image receipt.

Two alternatives were considered:

- `publishRecipe` with `registry.platforms` would avoid local image loading, but
  would move staging publication into the build phase and change its digest
  contract. That is a wider migration than this first slice.
- One recipe with subsequent per-image retagging would fit mixed tag suffixes,
  but introduce a second name mapping outside the recipe reports.

The chosen path preserves the local-image/registry-digest distinction and phase
ordering. It does not require a new public CLI command, a new generic task or a
Sonata/NanoFaaS plugin change. Reuse command construction, VM execution, logged
transport and existing builder/resource primitives where their contracts fit.

## Reusable profiles and coverage

Store these profiles under `packages/nanolab/recipes/`:

- `release-amd64-jvm.yaml`
- `release-amd64-native.yaml`
- `release-amd64-default.yaml`

Use schema version 2 and the canonical local release repository. Omit
`registry.platforms` and `registry.provenance`: the recipe's ordinary assembly
path produces local images with `image.id`. `-PrecipeTag` supplies respectively
`<version>-amd64-jvm`, `<version>-amd64-native` and `<version>-amd64`, where
`<version>` is the normalized `v`-prefixed version already used by `ImagePlan`.
Preserve every `container.image` as the current target name, including
`java-warm-echo`, `java-lite-*` and `watchdog`.

All three profiles select the explicit module IDs equivalent to the current
`controlPlaneModules=all`; recipes do not accept the sentinel `all`. The
resolved IDs are async-queue, autoscaler, build-metadata, concurrency-control,
k8s-deployment-provider, offload, runtime-config and sync-queue. The catalog has
ten modules, but `all` gives priority to the default-enabled Kubernetes provider
and excludes the conflicting container and containerd providers. Preserve this
eight-module selection. Validate the resolved `all` selection against the guarded
source; a changed selection requires a
profile update rather than silent expansion.

Preserve the effective JVM G1/C2 settings for all JVM images and the control
plane label `jvm-g1-c2`. Spring native components use the container builder,
Oracle GraalVM, optimization 3 and G1, with the existing effective JFR monitoring
and control plane label `native-o3-g1`. Java-lite keeps its existing Community
GraalVM, optimization 3 and serial-GC contract; its native tag must not silently
select the Spring components' G1 policy. Preserve source-owned runtime defaults
and AOT configuration. Recipe packaging may change image bytes, but must retain
runtime behavior, ports, user permissions and effective options.

The schema always requires a control plane. The default profile therefore
selects a JVM control-plane artifact with no `container`; its report must contain
exactly that declared artifact-only component with `image: null`. It is not a
45th release image. Accept this case explicitly in the release adapter; never
ignore arbitrary null/missing images. Extra artifact preparation is a known
schema constraint, not a reason to duplicate an image or alter its tag.

## Source identity and staging

The existing clean-tree, prepared-version and commit guards remain authoritative.
Freeze the raw profiles and their SHA-256 hashes with the guarded archive,
resolved tag groups and complete expected image mapping before provisioning.
Recheck the source and input identities at the existing build boundaries.

Execute from the same source archive staged for source tests. Keep the original
commit and archive digest in release evidence. An extracted `git archive` has
no `.git`; its recipe report can legitimately contain `source: null`. This is
not proof of the guarded commit. Prove identity through the existing verified
archive and a retained input inventory captured before source tests, checking original source paths and
rejecting unexpected source additions across recipe execution. Allow only
identified build outputs/cache directories of source-identified projects.
Do not synthesize or mislabel a staging Git commit as the release commit, sync
a dirty checkout over the archive, or exclude ignored-but-committed inputs.

Put each frozen profile and output under the owned versioned remote release
root, outside the Docker source context. Each group gets a distinct output
path; one assembly cannot erase another group's distribution. Fetch complete
reports and command logs using existing bounded VM transfer/transport helpers.
Retain and hash these inputs and reports locally before creating the build
receipt. Transfer truncation, changed inputs or unavailable evidence fail the
phase. Cleanup follows existing release ownership and compensation rules.

## Builder and mandatory capability gate

Build on the native Linux AMD64 Azure stack VM used by the release benchmark.
Reject a non-AMD64 host/daemon or mismatched builder before image work. ARM64
host emulation does not qualify as verification of this release build.

Retain the owned `docker-container` builder and BuildKit configuration with the
release's `max_parallelism` ceiling. Add its supported
`driver_options=("default-load=true",)` setting and bind root Gradle execution
to it through `BUILDX_BUILDER=<owned builder>`, with BuildKit enabled. Native
container exports explicitly request local binary output; default loading must
not replace that exporter. Record actual Docker/Buildx/BuildKit capabilities,
resolved builder identity, driver options, environment and configuration hashes.
Do not rely on the client-selected builder or a platform-less recipeBuilder
flag to select the ordinary `docker build` path.

Docker documents both [explicit builder selection](https://docs.docker.com/build/builders/)
and [automatic loading for the container driver](https://docs.docker.com/build/builders/drivers/docker-container/).
The implementation plan must start with a capability gate on the installed
release toolchain: validate all three profiles against the pinned catalog,
prove named-builder selection, prove a tagged image is loaded into the VM's
Docker daemon, and prove explicit local output still exports an actually compiled
native-builder artifact. Validate representative Oracle G1 and Java-lite builds;
an exporter fixture alone does not prove recipe compatibility. Also inspect the
Java-lite native task/binary mapping. Confirm that the resolved module selection
does not require containerd Maven staging; do not add an unused backend dependency.

This gate is not yet executed. If the pinned plugin/toolchain cannot represent
all required cells or preserve the export contract, stop the implementation
and revise this spec. Do not silently revert cells to Bake, discard components,
change native options, or introduce a Docker wrapper to mask an unsupported
capability.

## Distribution validation and receipt adapter

Introduce a release-specific adapter for the three recipe reports. Reuse shared
scalar, digest and recipe checks while keeping existing recipe consumer contracts
strict. The artifact-only default control plane must not broaden validation for
container, soak, Kubernetes or comparison workflows.

For each group validate schema version, raw profile hash/name, effective tag,
exact modules, component identities, SDKs, build modes and expected image names.
Check declared JVM/native identity and effective native options/distribution.
Map `exec` to recipe SDK `bash`, services to their distinct component kind, and
report Dockerfile mode `container` to release flavor `default`. Native Java-lite
belongs to the native tag group. Validate the declared artifact-only control
plane in the default report separately.

Require every release image to have a complete successful assembly report and
a valid local image ID. Independently inspect each expected tag on the stack
VM and require OS Linux, architecture AMD64 and the same ID as the report.
The union must cover exactly the guarded `ImagePlan`, once per image. Missing
reports, stale reports, failed builds, duplicate cells or mismatched identities
produce no reusable AMD64 build receipt.

Return the existing `local-image-digest` evidence with `docker-daemon:` references
for all expected images, plus hashed profile/report/input evidence. A local ID
is a configuration identity, not a registry manifest digest. Preserve that
meaning in the receipt and the resume verifier. The registry-push phase retains
its `local-registry-digest` evidence with `docker://` references, and signs only
published digests through the existing later phases.

## DAG, resume and failure behavior

Replace only the AMD64 JVM prerequisite/Bake command generation with the recipe
assembly group. Remove AMD64 Bake staging when it has no remaining consumer;
retain the BuildKit configuration. ARM64 still uses its existing Bake inputs and
commands. Keep source-tests -> AMD64 build -> registry push -> benchmark
ordering and the current later release barriers.

The AMD64 phase fingerprint must bind raw profile hashes, resolved tags, source
archive/inventory, expected cells and modes/options, commands, builder selection,
driver options, BuildKit configuration and parallelism. Include recipe/report
file evidence in the outcome so Sonata verifies it before reusing a phase.
Tampered/deleted reports, different profiles, source, version or image IDs must
invalidate reuse. A partial group set never becomes a complete build receipt.
Later phase reuse must remain bound to that receipt through existing prerequisite
hashes. Never rebuild or repush merely to verify a reusable phase.

Existing credential acquisition, source-test barriers, benchmark count and
regression thresholds, ARM64 smoke, architecture publication, manifest/alias
creation, signing, attestations and release finalization retain their contracts.
Do not move GHCR or Cosign operations into recipe assembly. Source-test setup
can still stage ARM64 infrastructure as required by the existing shared DAG;
AMD64-first does not claim that the full release prefix provisions only one VM.

## Verification and completion

The implementation plan must cover:

1. The mandatory capability gate, profile validation and exact current matrix
   coverage. New source targets/modules or unsupported modes fail preflight.
2. Recipe report validation, artifact-only control plane, mixed SDK mappings,
   native/JVM options, version/tag binding, stale/partial publication and source
   additions/tampering. Local ID and registry digest are tested separately.
3. VM placement and owned-builder selection/loading; no AMD64 legacy Bake or
   direct artifact-preparation commands outside the recipe invocations. Test
   failure, transfer truncation, cancellation and compensation behavior.
4. Phase fingerprint and evidence verification: unchanged input resumes without
   build/push; changed recipe/report/source/image invalidates reuse. Registry
   push and all downstream release prerequisites still consume the same matrix.
5. The full NanoLab suite, lint, format, types and import contracts. Report the
   four preexisting low-severity Bandit B101 findings separately; this migration
   does not suppress or change unrelated assertions.
6. A real native-AMD64 VM run of the build and staging-push slice using the
   guarded source. Retain all 44 current image IDs/platform checks, three raw
   reports, actual builder selection/export evidence, registry manifest digests
   and ownership/cleanup results. Exercise representative JVM, Oracle G1,
   Java-lite and Dockerfile runtime artifacts and the benchmark image-selection
   boundary. Shared prerequisites must be genuine in an executable release DAG;
   controlled fixtures test logic and do not qualify a release.

The build migration is complete only when the matrix, evidence adapter, resume
behavior and native-AMD64 verification pass. Any unavailable VM verification
remains an explicit incomplete gate. A public release still requires the full
existing canonical source tests, benchmark/regression, ARM64, publication and
signing gates; a successful migration slice is not a qualified release.

Update the NanoLab README and roadmap with supported profiles, verification
scope/evidence and the remaining ARM64 migration. Keep the smoke-limit review
open and deferred, as requested.
