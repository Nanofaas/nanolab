# Recipe-backed Kubernetes validation

Date: 2026-09-28
Status: design approved; implementation not started.

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
   if Minikube is unavailable or the context does not target the selected
   Minikube profile. Do not change the context or cluster configuration.
2. Stage the NanoFaaS source and profile in the run directory and run
   `assembleRecipe` on the host, preserving the selected source revision and
   tracked working-tree changes as the existing recipe flow does.
3. Load each recipe image reference from the host Docker daemon into Minikube
   with `minikube image load --daemon`. Also load the existing Kubernetes queue
   probe image, which remains built by its current task outside the recipe.
4. Set the recipe control-plane image from the distribution report when
   installing Helm. Register functions using the report references. Do not
   publish to or require the host's local registry for this path.
5. After Helm and function cleanup, remove only run-tagged recipe images loaded
   by this workflow. Preserve the queue-probe image according to its existing
   lifecycle.

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
4. Build and push the existing queue-probe image in the VM as its current
   Kubernetes workflow does. Use the recipe report's control-plane and function
   references for Helm and function registration.
5. Verify the deployed Pod's image reference and runtime image identity against
   the published distribution digest; retain both the report and Pod evidence.

The VM staging path is an explicit exception to the existing local-only recipe
flow. Transfer failures, Gradle failures and malformed reports must fail before
Helm install or registration. Preserve staged inputs and logs on failure for
diagnosis; remove only the temporary VM staging directory after successful
verification and cleanup.

## Shared workflow and evidence

Both provider paths use the same scenario, profile semantics, function binding,
control-plane metadata check, invocation check, queue lifecycle, resource checks
and cleanup graph. The recipe distribution remains an ordinary dependency
resource, so deployment cannot start before the report passes validation.

Helm resolves the control-plane image reference from the distribution when the
release is acquired. Function registration uses the report's `(name, sdk)`
component mapping. Keep the existing Minikube/Kubernetes resource checks and
readiness checks; do not route Kubernetes image checks through Docker Compose
inspection.

Add a Kubernetes-specific image check that records the control-plane and
function Pod image references and runtime image IDs. For local Minikube, also
prove the Minikube image store contains the reported local image ID. For
Multipass, compare the Pod runtime image identity with the registry digest,
normalizing the runtime's documented prefix. Keep the image metadata response,
report, image-store evidence, Pod evidence and Gradle logs under the selected
run directory after teardown.

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
