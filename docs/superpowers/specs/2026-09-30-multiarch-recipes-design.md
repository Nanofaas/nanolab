# Multiarch recipe publication and container validation

## Purpose and scope

Exercise NanoFaaS recipes v2 with a reusable distribution containing a JVM
control plane and the Java JVM `word-stats` function, published for
`linux/amd64` and `linux/arm64`. Verify both published platform manifests and
run the existing container lifecycle on the Docker daemon's native platform.
Every deployed image must be selected from the verified publication and
referenced by digest. Publication proves coverage of both architectures;
invocation proves execution only on the host architecture.

Use Docker directly on the host. The current host advertises ARM64 only, so
the approved approach includes scoped QEMU setup for the foreign architecture
and a dedicated Buildx builder. Preserve the active `nanolab-heap-analysis`
builder, Minikube and unrelated containers. No Multipass VM is provisioned.

Native multiarch builds, additional functions or services, provenance,
signing, cross-architecture runtime invocation, comparison matrices, soak and
release migration are outside this slice. Existing single-platform workflows
retain their current evidence requirements.

## Approaches considered

1. **Publish, verify both platforms, then validate the host runtime (chosen).**
   Add an explicit multiarch publication contract and derive the checked host
   runtime inputs from registry evidence. Reuse the existing lifecycle tasks.
   This exercises publication and consumption together without duplicating
   deployment orchestration.
2. **Publication and manifest verification only.** Smaller, but does not prove
   that NanoLab can consume the published recipe in a lifecycle workflow.
3. **Execute the full lifecycle for both architectures under emulation.**
   Adds runtime emulation and a second orchestration cycle. The first slice
   needs proof of both artifacts and one native host invocation cycle.

## Checked-in profile and scenario

Add `packages/nanolab/recipes/validate-container-multiarch-jvm.yaml` with:

- The same exact module set as the existing container JVM validation profile:
  `container-deployment-provider` and `build-metadata`.
- One JVM control plane and one Java JVM `word-stats` function, using the
  existing container lifecycle's registration key and invocation assertions.
- Registry repository `127.0.0.1:5000/nanofaas`, a run-specific tag through
  the existing recipe tag override, and exactly the two platforms above.
- `provenance: false`. This slice verifies executable manifests; it does
  not introduce attestation validation.

Add `packages/nanolab/scenarios-v2/deployment-lifecycle-container-multiarch.yaml`
as a sibling of the existing JVM container scenario, selecting this profile.
Keep resource settings, HTTP assertions, metadata checks and cleanup aligned
with the existing scenario. Platform selection comes from the recipe, with
no new scenario field or generic multiarch task type.

Validate the profile against the CI-pinned NanoFaaS checkout
`e7914be065e844776af57fe9e449bce7f12e03c5`. Capture the actual source revision,
dirty state, tracked patch hash, staged recipe and effective tag for each run
using the existing source capture mechanism. Verify staged source identity
before publication; revision and dirty state alone cannot identify edits.

## Builder, registry and QEMU lifecycle

Determine the host platform from the Docker daemon, normalizing its
architecture to `amd64` or `arm64`. Reject other operating systems or
architectures before creating resources. Inspect Docker, Buildx and existing
binfmt registrations, retaining their relevant preflight state.

Use the existing owned local registry resource. Create a uniquely named
`docker-container` Buildx builder for the run, with host networking and an
ephemeral BuildKit configuration allowing plain HTTP for
`127.0.0.1:5000` only. Pass its name through
`-PrecipeBuilder=<name>`; never select it globally with `--use`. Require both
platforms in `docker buildx inspect --bootstrap` before publication. A builder
name collision is an error, and cannot authorize deletion of an existing
builder.

Reuse an already functioning foreign-architecture binfmt registration. If
absent, install only the required foreign architecture with the binfmt
installer, using a digest-pinned installer image and recording its reference
and registration state. The installer reference is a repository constant,
chosen and verified during implementation. Do not reset binfmt or install all
architectures. An existing disabled or incompatible registration fails
preflight with diagnostics rather than being replaced. A foreign-platform
container probe must succeed before building the recipe.

Serialize NanoLab's installation and removal of binfmt registrations with a
host-local lock. Record ownership immediately after creation. On release,
remove only a registration created by this run whose state still matches the
recorded installation. Preserve preexisting or subsequently changed entries;
record a cleanup conflict instead of deleting them. The lock coordinates
NanoLab runs; state comparison protects against unrelated host changes.

Provision in dependency order: registry, required emulation, builder,
publication, runtime. Release runtime containers before builder and registry,
then release any owned binfmt registration. Register compensation as each
resource is created, including partial acquisition and publication failure.
Use existing Sonata command/Docker tasks for infrastructure operations;
NanoLab owns recipe-specific resource composition and evidence. Introducing
general infrastructure task classes or a Sonata release is unnecessary for
this design.

## Publication contract

Run one `publishRecipe` invocation on the staged source with the selected
builder, recipe, tag and output directory. The Gradle task owns assembly and
publication. Multiarch assembly is cache-only and supplies no local image ID;
NanoLab must not add a separate assembly call or a legacy image build.
Retain the Gradle log outside the regenerated distribution directory.

Preserve the current strict single-platform `read_distribution` contract.
Introduce a separate multiarch publication representation and parser for
profiles declaring platforms. Keep validation of common source, recipe,
component and reference fields shared where practical. The multiarch parser
requires:

- Recipe hash, effective tag and source identity matching the captured run.
- Exact modules and component set; correct kind, name, SDK and JVM mode;
  exact expected registry references and no duplicate components/references.
- For every image, exactly the requested platform set, disabled provenance,
  published status, a valid SHA-256 index digest and a manifest digest for
  each requested platform. No local `id` is required or synthesized.
- No missing, additional, duplicated or malformed platform entries, digests
  or image records. `published-unverified` is a failure.

The raw report remains unchanged. A multiarch report must still fail when
passed to a consumer that requires a local single-platform distribution.
Comparison, Kubernetes, containerd and load-test consumers are not widened
to accept multiarch as part of this change.

## Registry evidence and immutable runtime inputs

For each component, resolve the reported tag and require its registry digest
to equal the report's index digest. Fetch the OCI index by that digest and
require exactly one executable descriptor for each requested platform.
Reject extra platforms, duplicate platform descriptors and attestations in
this provenance-disabled profile.

Fetch both child manifests and their image configurations by immutable
digest. Verify the SHA-256 of the registry response bytes against each
expected digest, validate the index-to-manifest-to-configuration links and
require each configuration's OS/architecture to match its platform.
Require the report's platform digest map to equal the executable descriptors.
Use registry responses that preserve the manifest bytes for hashing; parsed
JSON or command output with altered formatting cannot establish this proof.
Recheck the tag against the same index before recording publication as
verified. A moving tag, missing blob or mismatched identity fails the run.

Reuse common registry parsing and digest checks from comparison evidence
where suitable. Its single-executable-manifest requirement remains enforced;
multiarch verification has an explicit separate entry point.

Only after all components pass verification, derive the host runtime view:

- Select the manifest for the Docker daemon's platform.
- Set the runtime reference to `repository/image@<host-manifest-digest>`.
- Use that manifest's configuration digest as the expected Docker image ID.
- Retain the original index digest, both platform manifests and selected
  host platform separately as publication evidence.

The derived view is an adapter for existing lifecycle tasks, not a modified
distribution report. Validate recipe/tag references on the publication model
before deriving immutable references. Require the exact container workflow
component/module selection before deployment. Never fall back to a tag,
rebuild, missing platform or unverified local image.

## Host container validation

Reuse the existing Compose control-plane resource, function registration,
image checks, metadata checks, invocation assertions and resource inspection.
Compose uses the host control-plane digest with building disabled; function
registration uses the host function digest. Pull those immutable images and
check the running containers' `.Image` against the verified configuration
digests before accepting runtime evidence. The index digest is not a Docker
local image ID.

Check control-plane build metadata against the captured recipe distribution.
Run the existing Java word-stats lifecycle, including the current invocation
and resource assertions. Cleanup follows the same ownership rules as other
container scenarios. A runtime failure retains publication evidence and
diagnostics while still releasing owned resources.

## Evidence

Keep these artifacts under the existing run directory:

- Captured source identity, recipe and hash, effective tag, host platform,
  Docker/Buildx preflight, installer reference and binfmt ownership record.
- Builder name/configuration, bootstrap output and foreign-platform probe.
- Original `recipe/distribution/distribution.json` and publication log.
- Per-component registry index, manifests and configurations for both
  platforms, plus a verification record linking their expected and observed
  digests. Preserve raw response bytes alongside any parsed summaries.
- `runtime-images.json` with each component's index digest, selected host
  platform, immutable runtime reference and expected configuration digest.
- Existing lifecycle outputs and cleanup results, including ownership
  conflicts or failures.

Write the verified runtime mapping atomically after complete registry
verification. Failed verification cannot leave a mapping that appears usable.
This scenario does not add resume or replacement-host recovery semantics.

## Verification and acceptance

Add focused contract tests for valid two-platform reports, malformed or
inconsistent identities, registry digest/link/platform mismatches, mutable
tag changes, and failure before runtime when verification is incomplete.
Test the host projection for both AMD64 and ARM64 without requiring both
physical hosts. Existing single-platform consumers must continue rejecting
multiarch reports.

Exercise resource ownership with preexisting builders/registrations, name
collisions, partial acquisition, failed publication, cancellation and cleanup
conflicts. Verify explicit builder forwarding and absence of global builder
selection, legacy builds or duplicate assembly calls.

Add the checked-in profile to existing push/PR `validateRecipe` coverage;
add no cron workflow. Run the full NanoLab test suite, then the new scenario
locally with Docker and Gradle against the pinned NanoFaaS checkout. Acceptance
requires one successful publication, two verified platform artifacts per
component, digest-pinned host containers, matching metadata and image IDs,
successful word-stats invocation, and cleanup preserving unrelated resources
and the previously selected builder. Check existing JVM container validation
as the direct integration regression.

Document the command, host prerequisites, temporary QEMU registration,
builder/registry ownership, evidence paths and host-only runtime coverage in
the NanoLab README. Update the roadmap's multiarch item only after this actual
end-to-end verification succeeds.
