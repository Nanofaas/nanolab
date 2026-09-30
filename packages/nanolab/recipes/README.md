# NanoFaaS recipe profiles

These reusable NanoFaaS recipes describe builds; NanoLab scenarios describe how
to deploy and test them. Keep generated distributions and run evidence out of
this directory.

| Profile | Control plane | Function | Purpose |
| --- | --- | --- | --- |
| `validate-container-jvm.yaml` | JVM, container provider, build metadata | Java JVM word-stats | JVM container lifecycle validation |
| `validate-container-bash.yaml` | JVM, container provider, build metadata | Bash word-stats | Bash container lifecycle validation |
| `validate-container-services-jvm.yaml` | JVM, container provider, build metadata | Java JVM word-stats and warm-echo service | Managed Java service lifecycle validation |
| `validate-container-jvm-service-native.yaml` | JVM, container provider, build metadata | Java JVM word-stats and native warm-echo | Native service with a JVM control plane |
| `validate-container-native-service-jvm.yaml` | Native container builder, container provider, build metadata | Java JVM word-stats and JVM warm-echo | JVM service with a native control plane |
| `validate-container-native.yaml` | Native container builder, container provider, build metadata | Java native word-stats | Native container lifecycle validation |
| `validate-k8s-jvm.yaml` | JVM, Kubernetes provider, build metadata, sync queue | Java JVM word-stats | Kubernetes lifecycle validation on Minikube or Multipass |
| `validate-containerd-jvm.yaml` | JVM, containerd provider, build metadata | Java JVM word-stats | Rootless containerd lifecycle validation on Multipass |
| `loadtest-container-jvm.yaml` | JVM, container provider, autoscaler, async queue, build metadata | Java JVM word-stats | Container autoscaling load test |

All profiles use the repository `127.0.0.1:5000/nanofaas` and single-platform
Docker images. The native profile compiles the control plane and word-stats
inside the container builder. The Kubernetes profile uses `IfNotPresent` so
Minikube can run images loaded from the host; Multipass publishes images to its
VM-local registry. The services profiles cover JVM and native warm-echo with
independent control-plane modes. Dockerfile services and multi-platform
publication remain to be verified.

## Use directly

From a **disposable NanoFaaS working copy**, with the absolute path to this
NanoLab checkout in `NANOLAB_ROOT`:

```bash
./gradlew validateRecipe \
  -Precipe="$NANOLAB_ROOT/packages/nanolab/recipes/validate-container-jvm.yaml"

./gradlew validateRecipe \
  -Precipe="$NANOLAB_ROOT/packages/nanolab/recipes/validate-container-native.yaml"

./gradlew validateRecipe \
  -Precipe="$NANOLAB_ROOT/packages/nanolab/recipes/validate-k8s-jvm.yaml"

./gradlew validateRecipe \
  -Precipe="$NANOLAB_ROOT/packages/nanolab/recipes/validate-containerd-jvm.yaml"

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

`deployment-lifecycle-container.yaml` and
`deployment-lifecycle-container-native.yaml` select the JVM and native profiles.
Each validation workflow runs NanoLab's `PublishRecipeTask`, which calls Gradle
`publishRecipe`: assembly and registry publication in one command. For a local
build without publication, `AssembleRecipeTask` calls Gradle `assembleRecipe`.
The two tasks are alternatives; do not run them in sequence for one distribution.

The recipe task stores the copied profile, staged source, Gradle log and
`distribution.json` under `runs/recipe-<id>/recipe/` by default. The runtime
metadata response and inspected image IDs are saved in `runs/recipe-<id>/`.
`--run-dir` chooses that run directory directly.

The single `deployment-lifecycle-k8s.yaml` scenario selects the Kubernetes
profile. With the default local environment, NanoLab checks the active Docker
driver Minikube profile, runs `assembleRecipe` in a staged checkout, loads the
recipe and queue-probe images, and forwards the control-plane API to host
loopback. It creates a namespace for the run and removes it after validation;
it does not create or delete the Minikube cluster. With an explicit Multipass
environment, it stages the same captured inputs in the VM and runs
`publishRecipe` there. Reports, logs and image evidence remain in `--run-dir`.

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml \
  --run-dir packages/nanolab/runs/recipe-k8s-local
./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml \
  --environment packages/nanolab/environments/multipass.yaml \
  --run-dir packages/nanolab/runs/recipe-k8s-multipass
```

`deployment-lifecycle-containerd.yaml` selects the containerd profile. NanoLab
creates a disposable Multipass VM, stages the pinned 0.23.0 containerd Maven
artifacts, and runs `publishRecipe` once in the VM. The systemd control plane
uses the JAR from that staged build; the function registration uses the image
reference in `distribution.json`. NanoLab checks the running build metadata,
the owned containerd image and OCI resource limits, then removes the VM.

The environment must provide an absolute host path to the Maven repository
produced by `scripts/bootstrap-containerd-dependencies.sh` in NanoFaaS. Copy
`packages/nanolab/environments/multipass-containerd.yaml.example`, set that
path, and run:

```bash
./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-containerd.yaml \
  --environment /path/to/multipass-containerd.yaml \
  --run-dir packages/nanolab/runs/recipe-containerd-multipass
```

`autoscaling-cycle-container.yaml` uses `loadtest-container-jvm.yaml`.
NanoLab publishes one distribution, starts Compose with its control-plane image,
registers the reported function image, then runs the existing k6 and autoscaling
checks. The control-plane image is checked before the load. The function image
is checked immediately after k6, while its container still exists; invoking it
earlier would change the initial scale-to-zero measurement.

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh run packages/nanolab/scenarios-v2/autoscaling-cycle-container.yaml \
  --run-dir packages/nanolab/runs/recipe-loadtest-container
```

The run directory retains `recipe/gradle.log`, `recipe/distribution/distribution.json`,
the image identity files, `k6-summary.json`, `metrics/prometheus-snapshot.json`,
`report.html` and `summary.json`.

## Runtime comparison profiles

`comparison-*.yaml` provides nine profiles: JVM Serial/G1 with C1/full tiering,
two Serial JVM variants with one event loop, and native Serial `s`/`3` and G1 `3`.
All control planes contain exactly `k8s-deployment-provider`, `async-queue` and
`build-metadata`. The `comparison-jvm.yaml` distribution also contains JVM Java
and container JavaScript word-stats; `compare` always prepares this distribution
first, including when JVM is not selected for measurement.

Native comparison profiles use the host builder on the measured VM. G1 requires
Oracle GraalVM and records effective JFR monitoring. These profiles have passed
`validateRecipe`; native publication and the complete native matrix remain to be
verified. See [runtime comparison usage and evidence](../README.md#recipe-runtime-comparison).

## Bash container validation

`validate-container-bash.yaml` publishes the JVM control plane and the existing
Bash word-stats image. `deployment-lifecycle-container-bash.yaml` selects catalog
function `word-stats-exec`; NanoLab matches catalog runtime `exec` to recipe SDK
`bash` when checking and reading the distribution. Invocation settings remain
those of the catalog function.

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-container-bash.yaml \
  --run-dir /tmp/nanolab-bash-my-run
```

The workflow uses the same publication, registration, metadata/image checks,
resource inspection and cleanup as JVM container validation. It runs Docker on
the host and needs no VM. The CI recipe validation step includes this profile.
The Docker cycle passed on 30 September 2026 against NanoFaaS `e7914be0`;
evidence is in `/tmp/nanolab-recipe-bash-e2e-20260930/`, with the execution log
at `/tmp/nanolab-recipe-bash-e2e-20260930.log`.

## Java service container validation

`validate-container-services-jvm.yaml` builds and publishes the JVM control plane,
word-stats function and warm-echo service in one recipe. The scenario selects
word-stats through the function catalog; NanoLab also resolves the Java services
declared by the recipe from `services/java/<name>/function.yaml`. No catalog
entry or new scenario field is needed for services.

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-container-services.yaml \
  --run-dir /tmp/nanolab-services-my-run
```

This support is limited to local Docker `validate` runs and Java services with
a `DEPLOYMENT` invocation manifest. Unsupported SDKs, missing manifests and
conflicting registration names are rejected. Services are registered through
the ordinary function API; component selection and image evidence retain
their recipe kind `service`.

The workflow checks build metadata, all three running image IDs, invocations,
container resource inspection, warm-echo's exact echo output and cleanup.
Without declared resource limits, inspection only establishes that the
container exists. CI validates the reusable profile with `validateRecipe`.

The Docker cycle passed on 30 September 2026 against NanoFaaS `e7914be0`.
Evidence is in `/tmp/nanolab-recipe-services-e2e-20260930/`, including
`image-service-warm-echo.json`; the log is
`/tmp/nanolab-recipe-services-e2e-20260930.log`. Mixed JVM/native modes are
covered below; the Dockerfile watchdog requires separate verification.

## Independent service build modes

The two mixed profiles keep word-stats on the JVM and choose the control plane
and warm-echo modes independently. Native components use `builder: container`,
so the host needs Docker with BuildKit and does not need a local GraalVM.

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-container-jvm-service-native.yaml \
  --run-dir /tmp/nanolab-jvm-service-native-my-run
./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-container-native-service-jvm.yaml \
  --run-dir /tmp/nanolab-native-service-jvm-my-run
```

Run these scenarios sequentially: they use the existing local validation ports
and Docker network. The same tasks publish the recipe, compare each component's
reported mode to the profile, verify the running image IDs, invoke both managed
workloads, check warm-echo's exact output, inspect containers and clean up.
CI runs `validateRecipe` on both reusable profiles.

Both Docker cycles passed on 30 September 2026 against the clean NanoFaaS
revision `e7914be0`. Each published one recipe and verified metadata, all three
image IDs, invocations, exact echo output, container inspection and cleanup.
`distribution.json` records the JVM/native modes independently. Native
components record Community GraalVM, Serial GC, optimization 3 and the
container builder; the control-plane metadata reports JVM/C2 or native/3
respectively.

Evidence roots are `/tmp/nanolab-jvm-service-native-e2e-20260930/` and
`/tmp/nanolab-native-service-jvm-e2e-20260930/`, with matching `.log` files
beside them. No NanoLab task implementation changes were needed for these
profiles. This verification does not cover the native runtime comparison
matrix or Oracle G1 builds.
