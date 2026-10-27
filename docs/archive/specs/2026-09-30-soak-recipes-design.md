# Recipe preparation for the container smoke soak

## Purpose and approved scope

Migrate the preparation of one short container soak to NanoFaaS recipes v2.
Use one reusable profile to build and publish a JVM control plane, Java JVM
word-stats and JavaScript word-stats from one immutable source snapshot.
Prove the complete preparation-to-report workflow with Docker on the host.

This is roadmap step 3, beginning with soak. Release migration is a subsequent
slice. Preserve the soak's existing measurement phases, numerical policy,
runtime checks, workload, evidence limits and resource ownership contracts.
The resulting smoke remains `purpose: smoke` and `p24_qualified: false`.
Passing it demonstrates workflow integration, not P24 acceptance.

Native, multiarch, containerd, Kubernetes, Multipass, signing, release and
full P24 campaigns are outside this slice. Do not add a scheduled action.

## Approaches considered

1. **Integrate recipe publication into soak preparation (chosen).** Build all
   application roles through one `publishRecipe` invocation, validate its
   distribution and preserve observed provenance in the existing soak
   receipts. Deployment and measurement consume the frozen results.
2. **Publish separately and import prebuilt images.** Requires another public
   receipt-import boundary; the current preparation explicitly rejects
   prebuilt inputs. It adds operator steps and does not exercise integrated
   preparation, so it is deferred.

## Profile and scenario

Add `packages/nanolab/recipes/soak-container-smoke-jvm.yaml` and a sibling
scenario `packages/nanolab/scenarios-v2/memory-soak-smoke-recipe-container.yaml`.
Keep the original smoke scenario as a regression consumer of its current path.

The profile selects exactly:

- A JVM control plane with `container-deployment-provider`, `async-queue` and
  `build-metadata`, matching the existing smoke's control-plane selection.
- Java `word-stats` built in JVM mode and JavaScript `word-stats`.
- A local registry at `127.0.0.1:5000/nanofaas` and a unique run tag.
- Build provenance enabled, retaining the existing soak's maximum BuildKit
  provenance requirement and the additional observed toolchain evidence.
- One platform, matching the Docker daemon used for both build and measurement.

Use the existing scenario `recipeProfile` field, resolving relative paths
against the scenario directory as in the other recipe workflows. Extend its
validation to admit only the supported container smoke combination. Preserve
rejection of incompatible build selectors elsewhere.

The recipe is authoritative for build selection. Existing `soak.images`
entries remain the per-role expectations for this first integration: validate
their modes, variants, modules and platforms against the selected recipe
before provisioning. They must not generate an additional legacy build.
Reject unsupported overrides and contradictory inputs rather than silently
choosing one declaration. Removing this duplication belongs to the later
consumer migration and legacy-removal step.

Read the actual architecture from Docker daemon information. The checked-in
scenario targets ARM64 for the current host; an AMD64 run requires an explicit
scenario with matching platform expectations. Reject a foreign platform before
publication. Do not silently rewrite a frozen scenario or install emulation.
Resource limits, phases, workload rates and smoke criteria follow the existing
container smoke. Any change required to make that existing contract executable
must be identified and documented separately from recipe integration.

## Source identity and publication

Reuse `capture_source_snapshot`, its manifest and verification boundaries.
Capture exactly one snapshot for all application roles, including local changes
and eligible untracked files under its current policy. Record the original
revision, dirty state and content fingerprint. A clean Git revision cannot
replace the dirty-inclusive fingerprint.

Capture and hash the profile before building. Materialize a writable build
workspace from the snapshot without modifying the immutable snapshot or the
operator checkout. Recipe execution must consume those exact files; the
tracked-only staging in `prepare_recipe_run` cannot replace the soak snapshot
inventory. Do not recapture the live checkout separately for each role.

Reuse the recipe command construction and distribution reader where compatible,
with the source contract extended only as needed for a plain snapshot workspace.
Git information created for execution must be recorded as staging metadata;
never present a synthetic staging commit as the original source revision.

One `publishRecipe` invocation builds and publishes all three application
images before any measurement. Build observation commands are permitted;
separate application builds, a preceding `assembleRecipe` and retries that
silently republish a partial result are not. Preserve command cancellation,
timeouts and process reaping through the owned soak executor.

## Distribution and observed provenance

Retain the raw profile, raw `distribution.json`, publication command/logs,
source manifest, observation records and verified registry artifacts under the
run's evidence budget. Identify original inputs and any generated observation
instrumentation separately, including their content hashes.

Validate recipe hash, effective tag, exact components, modules, build modes
and source binding. Require successful publication and a valid digest for each
role. A local image ID or requested tag cannot substitute for registry evidence.

Verify the registry objects by content digest, resolve any attested index to
the selected executable platform manifest, and verify its image configuration.
Keep publication/index identity, executable manifest identity and configuration
identity distinct. Never deploy an attestation descriptor as a runtime image.
Bind BuildKit provenance to the verified image, source materials and build
invocation; preserve the current collector's material and toolchain checks.

The distribution report is one part of the evidence. It does not certify the
effective compiler, Gradle executable, Node toolchain, BuildKit builder or base
images. Capture successful version commands from the actual compilation/build
contexts, and verify effective base-image material digests. Requested versions,
Dockerfile strings, build arguments and an unrelated host executable are
insufficient.

Adapt the existing soak observation and receipt logic to the recipe invocation.
Instrumentation may modify only the materialized workspace, with original and
effective inputs fingerprinted separately. Prove that all generated recipe
build stages required by the three roles are observable before proceeding to
the runtime integration. Do not retain an unobserved second build to fill gaps.

If the pinned NanoFaaS recipe implementation cannot expose a required build
observation or attestation, report the exact capability gap and stop the
migration. A follow-up plugin change requires a separately reviewed design;
neither this spec nor a smoke exemption authorizes weakening provenance.

Produce the existing `BuildReceipt` contract for every role, bound to the single
snapshot fingerprint, effective recipe identity, verified image digest,
observed toolchains, base images and hashed logs. Reuse compatible helpers;
do not conflate the existing internal `BuildRecipe` bake descriptor with the
NanoFaaS recipe profile.

## Frozen runtime and resource lifecycle

Feed verified receipts into the current `PreparedSoak` and frozen runtime
composition. All three application images use executable manifest digest
references. Pull and verify local configuration identity against the verified
registry artifacts, then check the actual deployed images and runtime settings.
No application build or publication is allowed after freeze.

Reuse the existing owned registry, builder, endpoint lease, Compose project,
function registration, observer and cleanup journal. Pass the owned builder
explicitly to publication; preserve the operator's selected builder. Preserve
the existing cleanup and retention behavior of successful, failed and cancelled
soak runs. Helper builds remain governed by their current separate contracts.

Failures in publication, evidence validation, observation or source verification
stop preparation before measurement. Preserve partial evidence and compensate
owned resources. Evidence-write failures must not bypass cleanup. Do not delete
preexisting resources or broaden retained-state cleanup authority.

## Acceptance and verification

Validate the new profile with `validateRecipe` against the CI-pinned NanoFaaS
revision `e7914be065e844776af57fe9e449bce7f12e03c5`; record the actual revision
and dirty-inclusive source fingerprint for every execution. Add profile
validation to the existing push/PR CI gate.

Focused tests must demonstrate:

- Early rejection of incompatible scenarios, profile expectations and platforms.
- One snapshot and one application publication, including dirty and untracked
  source binding; mutations across verification boundaries are rejected.
- Missing/mismatched distributions, artifacts, provenance and observation
  records fail before measurement, with partial evidence retained.
- Receipt-to-runtime mapping uses verified digests and configuration identities.
- Cancellation and failed preparation release owned resources without affecting
  preexisting resources; no legacy build follows recipe selection.
- Existing smoke, prebuilt rejection and non-soak recipe consumers retain their
  contracts.

Run the new smoke end to end using host Docker: source preparation, publication,
artifact/provenance verification, deployment, effective runtime preflight,
existing prerequisites, workload and all phases, collection, evaluation,
report and cleanup. Record counts and verdicts rather than inferring success
from process exit alone. The smoke's numerical outcome and completeness outcome
must both be reported; infrastructure success cannot hide incomplete evidence.

Record resource state before and after, checking preservation of unrelated
containers and builders. Run an ordinary JVM recipe lifecycle regression and
the full NanoLab suite after implementation. Document any environmental blocker
or incomplete phase explicitly; profile validation and unit tests alone do not
mark this roadmap slice complete.

Update `docs/soak.md`, the NanoLab README and roadmap with the supported recipe
entry point, evidence locations and observed end-to-end result. Retain
`p24_qualified: false` and the existing P24 readiness limits.
