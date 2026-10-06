# Recipe-backed Kubernetes validation

Date: 2026-09-28
Status: design approved; reviewed on 2026-09-29; implementation not started.

## Goal

Migrate the existing `deployment-lifecycle-k8s.yaml` validation scenario to a
reusable NanoFaaS recipe v2 profile. Keep one scenario and one profile for all
supported environments. The environment provider selects where NanoFaaS builds
and how its images reach Kubernetes:

- `local` means Minikube on the host. Build with `assembleRecipe` on the host and
  load the built images into Minikube.
- An explicitly selected `multipass` environment means build and publish inside
  the stack VM, into the registry already configured for its k3s cluster.

Do not provision or use a VM for the local path. Keep the scenario's existing
Kubernetes lifecycle, invocation, queue, metadata and resource assertions.

## Scope

This slice supports only `local` with Minikube and explicit `multipass`.
Other environment providers fail with a clear unsupported-provider error for
recipe-backed Kubernetes validation. Containerd validation, other recipe
components, scheduled workflows and cluster lifecycle management are out of
scope. NanoLab does not start, stop, create or delete the user's Minikube cluster.

The checked-in scenario remains
`packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml`; it selects one
profile at `packages/nanolab/recipes/validate-k8s-jvm.yaml`. The provider is
selected by the existing `--environment` option (or the existing local default),
not by duplicating the scenario or profile.

## Recipe profile

The profile builds the control plane as a JVM container and the Java JVM
`word-stats` function. The control plane includes `k8s-deployment-provider`,
`build-metadata`, and `sync-queue`, matching the Kubernetes lifecycle checks.
Use a unique tag per run and the existing local repository
`127.0.0.1:5000/nanofaas`.

Set `nanofaas.k8s.image-pull-policy: IfNotPresent` in the profile. NanoFaaS
currently defaults this setting to `Always`, and its Kubernetes image validator
creates a pod using that policy. `IfNotPresent` lets local Minikube use images
loaded by the workflow and lets the Multipass cluster pull newly published,
run-tagged images from its configured registry. Set Helm's control-plane image
pull policy to `IfNotPresent` for this recipe path as well. Keep the normal
non-recipe defaults unchanged.

The profile is validated by NanoFaaS `validateRecipe` in CI. NanoLab continues
to delegate recipe-schema and build semantics to NanoFaaS.

## Environment-specific execution

### Local Minikube

1. Preflight the Minikube CLI and active Kubernetes context. Fail before building
   if Minikube is unavailable or the context does not target a running Minikube
   profile with the Docker driver. Derive the profile from the current context,
   verify membership in Minikube profile metadata and pin that context/profile
   in subsequent commands. Verify host Docker, Helm, kubectl and k6, and host/node
   architecture compatibility. Do not change the context or cluster configuration.
2. Stage the NanoFaaS source and profile in the run directory and run
   `assembleRecipe` on the host, preserving the selected source revision and
   tracked working-tree changes as the existing recipe flow does.
3. Load each recipe image reference from the host Docker daemon into Minikube
   with `minikube image load --daemon`. Also load the existing Kubernetes queue
   probe image, which remains outside the recipe but is built from the same
   staged source using a run-specific tag. Disable its legacy Docker push on
   this path. Give its build and load explicit dependency edges before registration.
4. Set the recipe control-plane image from the distribution report when
   installing Helm. Register functions using the report references. Do not
   publish to or require the host's local registry for this path.
5. After function deletion and Helm uninstall, wait for the run's Pods to
   disappear, then remove only run-tagged recipe and probe images loaded by this
   workflow. Track partial loads for compensation. Refuse to take ownership of
   an image reference already present before this acquisition.

Minikube documents `image load --daemon` for images already in the host Docker
engine and supports image loading across its supported container runtimes. The
implementation must retain the report image ID, verify the image available to
Minikube matches it, and record the deployed Pod's image reference and runtime
`imageID`. A tag match alone is insufficient.

### Explicit Multipass

1. Use the existing provisioning lifecycle and the registry configured for the
   stack VM. Do not create a host-to-VM registry tunnel.
2. Stage the exact NanoFaaS source and selected profile into a unique writable
   run directory on the VM. Do not build from the regular synchronized checkout
   or modify the host NanoFaaS checkout. Preserve tracked source changes and
   retain the source provenance on the host.
3. Run `publishRecipe` in the VM against its local registry. Fetch the report and
   Gradle log to the host run directory before consuming the distribution.
4. Build and push the queue-probe image from the same staged source in the VM,
   with a run-specific tag. Use the recipe report's control-plane and function
   references for Helm and function registration.
5. Verify Pod image identity through node runtime inspection and the published
   manifest/config relationship described below; retain report and Pod evidence.

The VM staging path is an explicit exception to the existing local-only recipe
flow. Transfer failures, Gradle failures and malformed reports must fail before
Helm install or registration. Preserve staged inputs and logs on failure for
diagnosis on the host before normal VM teardown. Retain the remote stage on
failure when the environment retains its VM; this does not override the existing
VM lifecycle. Remove only the temporary VM staging directory after successful
verification and cleanup, before VM teardown. Keep an execution success marker after all checks;
resource release alone does not prove success because it also runs on failure.
Attempt to fetch diagnostics even when Gradle fails, without masking the original
error or accepting a stale successful report. Transfer the staged Git metadata
needed to reproduce revision/dirty identity, without credentials, hooks, caches,
build output or Git alternates that point outside the stage. Verify source/profile
hashes on the VM before invoking Gradle. Remote paths must never be treated as
host filesystem paths. Use the existing VM provider transfer/exec/fetch interfaces.

## Shared workflow and evidence

Allocate a unique namespace `nanofaas-recipe-<run-token>` for either path. Create
it as an owned resource before Helm (the current helper sets namespace creation
to false); refuse to acquire an existing namespace. Delete only this namespace
after function/Helm teardown. Preserve the cluster and unrelated releases.

Both provider paths use the same scenario, profile semantics, function binding,
control-plane metadata check, invocation check, queue lifecycle, resource checks
and cleanup graph. The recipe distribution remains an ordinary dependency
resource, so deployment cannot start before the report passes validation.

Helm resolves the control-plane image reference from the distribution when the
release is acquired. Function registration uses the report's `(name, sdk)`
component mapping. Keep the existing Minikube/Kubernetes resource checks and
readiness checks; do not route Kubernetes image checks through Docker Compose
inspection. Remove the Compose requirement from the common recipe binding;
Compose data stays optional and is consumed only by container validation.

Local HTTP consumers cannot assume the Service ClusterIP is routable from the
host. Acquire a loopback-only `kubectl port-forward` to the control-plane Service
after Helm is ready, obtain the assigned local port, and expose it as an
`Endpoint` resource. Registration, metadata, invocation and k6 use that resource.
Multipass retains the ClusterIP endpoint and executes HTTP commands in role
`stack`. Release the forwarding process before uninstalling Helm, including on
partial acquisition failure. This is API access, not a registry tunnel.

Recipe binding applies only to selected recipe functions. The queue probe must
keep its ordinary registration and explicit build path when legacy recipe
component builds are disabled; it must not index the recipe function mapping.
Use the same staged source for the probe, chart and recipe to preserve source
isolation. Carry dynamic endpoints through the existing `Endpoint` type rather
than casting resource endpoints to strings.

Add a Kubernetes-specific image check that records the control-plane and
function Pod image references, node names and runtime image IDs. Resolve the
actual Pod runtime image through node CRI inspection (Minikube node commands
locally; k3s runtime commands in the VM). Verify the resolved image config digest
against the report's Docker image ID. For published images, also verify the
registry manifest digest and its config descriptor against the report.

Docker image IDs identify image configuration; registry digests identify manifests.
Do not equate them or merely strip a runtime prefix and compare arbitrary hashes.
A runtime may report either identity: establish the manifest-to-config relation
from runtime content/inspection before accepting it. Inspect every running target
container selected through the Deployment's ownership chain; an unrelated Pod with
a matching tag is not evidence. Require ready target containers and retain the
raw Pod, CRI and (where needed) manifest evidence. A missing, changed or ambiguous
identity fails validation, even if the tag matches.

Keep metadata responses, reports, image-store evidence, Pod evidence and Gradle
logs on the host under the selected run directory after teardown.

Planning and dry-run remain side-effect free: they must not build, call Minikube,
copy staged source to a VM, or create Kubernetes resources. Acquisitions use
compensation so failures after image loading or Helm installation release only
resources acquired by this run.

## Acceptance checks

- One Kubernetes scenario and one reusable profile serve both supported
  providers; no environment-specific scenario duplicate is added.
- Config accepts `recipeProfile` for `validate`/`k8s` only with `local` or
  explicit `multipass`; unsupported providers and conflicting legacy overrides
  fail clearly.
- The local plan runs `assembleRecipe`, loads recipe and queue-probe images into
  Minikube, uses the report's control-plane/function references and does not
  start a VM or publish to the local registry.
- The Multipass plan stages the recipe inputs, runs `publishRecipe` in the stack
  VM, fetches its report and uses the report references; the build does not run
  on the host and no registry tunnel is acquired.
- Tests cover report contracts, environment-specific task roles, image loading
  and cleanup, dynamic Helm image selection, function registration, Kubernetes
  image evidence, failure compensation and dry-run purity.
- CI validates the checked-in Kubernetes profile with the actual NanoFaaS
  `validateRecipe` resolver.
- Run the full Kubernetes lifecycle locally against Minikube. Also verify the
  Multipass plan and run the full Multipass lifecycle when its explicit
  environment is available. Keep these runs unscheduled.

## Implementation constraints

Reuse `AssembleRecipeTask`, `PublishRecipeTask`, source staging and report
parsing. Extend these only for what the environment-aware workflow requires.
Keep the queue probe outside the profile for this slice; it is an existing
backend check, not part of the selected recipe function set. Keep changes in
NanoLab and the checked-in recipe profile; no NanoFaaS plugin change is needed.

Minikube's image-store ID and Pod `imageID` formats can differ by runtime. Before
finalizing the Kubernetes identity check, exercise the selected host Minikube
runtime and assert the actual values; fail closed if the check cannot prove the
loaded image matches the report. Do not weaken identity verification to a tag
comparison to accommodate a runtime difference.

## Review corrections (2026-09-29)

The code review found and resolved these gaps in the original design:

- `_helm_release_with_endpoint` returns a ClusterIP, which does not establish
  host connectivity to Minikube. Local validation now owns an API port-forward.
- `build_images=False` skips every function build, including the auxiliary queue
  probe, while recipe registration would look up that probe in an absent mapping.
  The probe now has an explicit staged build/delivery path and ordinary registration.
- Kubernetes evidence must resolve configuration IDs and manifest digests rather
  than assuming they are the same hash.
- The Helm helper disables namespace creation, while local runs skip VM bootstrap.
  The workflow now owns a unique namespace and has explicit teardown ordering.
- Remote recipe commands cannot use host paths or read remote reports via host
  `Path` operations. Transfers, remote verification and report fetching are explicit.

The shared source/profile contract and the user's local/Multipass selection remain
unchanged. No implementation or infrastructure run was performed during this review.
