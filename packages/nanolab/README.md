# NanoFaaS control-plane tool

Distributed inputs live in `src/nanolab/assets/presets/{scenarios,recipes,environments}`;
scenario payloads are under `scenarios/payloads`. CLI examples below use preset
basenames, which also work from an installed wheel outside the checkout. Existing
explicit paths override presets; named inputs in the operator workspace override
distributed names. Relative recipe/policy paths resolve beside the scenario.
Use `NANOLAB_WORKSPACE` to choose the writable workspace (default cwd), or
`--run-dir` for explicit results. Custom scenarios, recipes, environment files and
payloads belong to that workspace; distributed resources are read-only defaults.

The control-plane tool is the orchestration entry point for provisioning and
validating NanoFaaS. A scenario defines *what* to execute; an environment binds
each role to a local host, a managed VM, or an external SSH host. Task
generic task implementations live in the pinned `sonata-tasks` package from
the Sonata repository; this package remains the product-facing composition
layer.

It is intentionally separate from the `nanofaas` CLI: the CLI calls the
control-plane HTTP API to manage functions, while this tool creates VMs, installs
k3s and Helm, distributes images, and runs end-to-end or load-test workflows.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) on the machine that runs the tool.
- Docker or a compatible runtime for container scenarios and image builds.
- Multipass for local VM-backed Kubernetes validation.
- SSH and Ansible for an external VM; provider credentials for Azure or Proxmox
  when using managed VMs.

For containerd workflows, set `containerdMavenRepository` to the isolated Maven
repository produced by NanoFaaS's `scripts/bootstrap-containerd-dependencies.sh`.
Staging accepts `containerd-java` and `containerd-java-cni` 0.24.0 with
`libcni-java` 0.23.0, and transfers only the reviewed artifact files and a checksum
receipt.

Run the CLI from the standalone repository root:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab --help
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab doctor
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab list
```

The installed wheel includes the public scenario, recipe, policy, payload and
environment presets. `nanolab list` prints their paths for `inspect`, `plan` and
`run`, and the TUI selects them directly. `NANOFAAS_ROOT` points to the separate
nanoFaaS source checkout needed to build or plan workloads.

Results default to `runs/` under the current working directory. Set
`NANOLAB_WORKSPACE=/path/to/workspace` to use another workspace for `runs/`,
`profiles/` and operator environment files, or use `--run-dir` for one run. The
TUI reads `environments/*.yaml` from that workspace and gives them precedence
over bundled presets of the same name. Copy cloud provider templates there and
edit those copies; presets inside the installation are read-only resources.
Public preset source files remain in this package's top-level directories;
update their bundled copies under `src/nanolab/assets/presets/` together (the
packaging regression verifies they match).

## Local Telegram notifications

Set both variables before `nanolab run` to receive one message after the workflow
and its cleanup complete. The message includes the workflow, scenario, duration,
and any failure summary.

```bash
export NANOLAB_TELEGRAM_BOT_TOKEN='…'
export NANOLAB_TELEGRAM_CHAT_ID='…'
```

The token is never read from YAML or written to a journal. Notifications are
disabled when either variable is absent and are always disabled when `CI` is set.

## First validation

Inspect the plan before executing it. The container scenario is the smallest
local path and does not require a Kubernetes cluster:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab plan deployment-lifecycle-container.yaml
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run deployment-lifecycle-container.yaml
```

To validate the public handler envelope locally across the bundled SDK
functions, including request headers, body, function status, response headers,
and binary payloads:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run handler-envelope-container.yaml
```

Sonata releases the functions, Compose project, and registry after the scenario,
including when a verification fails.

To verify persistent recovery instead, use the dedicated scenarios. They scale
`word-stats-java` to two replicas, restart only the control plane, then verify
the restored registration and unchanged managed resources before normal teardown:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run persistent-recovery-container.yaml
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run persistent-recovery-k8s.yaml \
  --environment multipass.yaml
```

The interactive UI uses exactly the same plan/run implementation:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab tui
```

The adapted TUI groups the four supported scenarios under **Validation**, **CLI**,
and **Load Testing**; **Tools** provides validated scenario inspection and the
same Docker/SSH prerequisite check as `doctor`. After choosing a workflow, select
an environment and whether to plan or run it. Non-local runs also ask whether to
provision the environment and whether cleanup should keep the infrastructure.

Every menu and result view keeps one invariant branded header at the top. Plans
remain in that shared frame, while runs open the live workflow dashboard with
phase state, nested verification work, errors, and command logs. Press `l` to
hide or restore the log panel while a workflow is running.

When only the provider templates are present, the TUI environment picker still
shows **Azure (setup required)** and **Proxmox (setup required)**. Selecting
either entry displays setup guidance, writes no files, and starts no workflow.
The TUI never loads or executes `.yaml.example` templates. Copy
`azure.yaml.example` to `azure.yaml` or `proxmox.yaml.example` to `proxmox.yaml`,
then fill in the provider values and configuration. Keep external authentication
outside YAML: run `az login` for Azure, and provide the Proxmox password through
the environment variable named by `password_env`. Do not store secrets in YAML.

## Commands

| Command | Purpose |
|---|---|
| `list` | List bundled scenarios. |
| `workflow` | List workflows, supported environment providers, and scenarios. (`workflows` is an alias.) |
| `inspect <scenario>` | Print validated scenario data. |
| `plan <scenario>` | Render the ordered operations without executing them. |
| `run <scenario>` | Execute a scenario in its selected environment. |
| `doctor` | Check commands required by the local host. |
| `tui` | Select and run the same workflows interactively. |

Use `--help` after any command to see its supported options. The supported
scenario files are in `packages/nanolab/src/nanolab/assets/presets/scenarios/`.

## Environments and VM lifecycle

Local execution is the default. VM-backed workflows bind the `stack` and optional
`loadgen` roles through an environment file.

| Environment | Use case | Lifecycle |
|---|---|---|
| none | Local container validation | No VM is created. |
| `packages/nanolab/src/nanolab/assets/presets/environments/multipass.yaml` | Local k3s VM | Managed VM; removed after the run by default. |
| `packages/nanolab/src/nanolab/assets/presets/environments/external.yaml.example` | Existing SSH-only VM | Never created or deleted by the tool. |
| `packages/nanolab/src/nanolab/assets/presets/environments/azure.yaml.example` | Azure VM | Managed VM; removed after the run by default. |
| `packages/nanolab/src/nanolab/assets/presets/environments/proxmox.yaml.example` | Proxmox VM | Managed VM; removed after the run by default. |

For a Multipass-backed Kubernetes run:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab plan deployment-lifecycle-k8s.yaml \
  --environment multipass.yaml
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run deployment-lifecycle-k8s.yaml \
  --environment multipass.yaml
```

The selected managed environment creates or reuses its VM, runs the shared
Ansible bootstrap, and synchronizes the repository. Managed VMs are deleted
even after a failure; pass `--keep` when they must remain available for
inspection. External hosts are never deleted. Remote commands run from
`<home>/nanofaas`.

Copy the Azure or Proxmox example before CLI use and fill in provider values;
pass the concrete `azure.yaml` or `proxmox.yaml` path explicitly. CLI path
selection remains the caller's responsibility.
Proxmox reads its password from the environment variable named by `password_env`.

## Load testing

Load testing follows the same plan-first workflow:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab plan autoscaling-cycle-k8s.yaml \
  --environment multipass.yaml
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run autoscaling-cycle-k8s.yaml \
  --environment multipass.yaml \
  --run-dir runs/experiment-1
```

The workflow deploys the stack with Helm, registers the selected function, runs
k6, observes autoscaling, and captures Prometheus data. Use an environment with
a `loadgen` role when k6 must run on a dedicated VM. `--only`, `--from`, and
`--until` select task subsets.

`autoscaling-cycle-k8s.yaml` verifies NanoFaaS internal scale-to-zero. To test
Kubernetes HPA scale-to-zero, run `autoscaling-cycle-k8s-hpa.yaml` with
`environments/multipass-hpa-scale-to-zero.yaml`; it enables the Prometheus
Adapter and k3s's HPA alpha feature gate only for that VM, then verifies
`0 → N → 0`.

## Development

For direct development inside this package:

```bash
cd packages/nanolab
uv sync --dev --locked
uv run pytest -q
uv run ruff check .
uv run basedpyright
uv run lint-imports
uv run python -m nanolab.devtools.package_report
uv run pydeps nanolab
```

## Related documentation

- [Standalone repository overview](../../README.md)

## Image releases

Official releases run only through `nanolab run scenarios/release.yaml` on
the pinned Azure profile, after `nanolab release prepare` has committed the
version. The standalone release configuration is in
[`release.yaml`](src/nanolab/assets/presets/release.yaml). GitHub Actions never publishes images, and
local/Multipass/Proxmox builds cannot promote to GHCR.

## Recipe runtime comparison

`compare` uses the nine reusable `recipes/comparison-*.yaml` profiles and runs
`publishRecipe` on the measured VM. It captures tracked NanoFaaS changes once,
always publishes the JVM distribution with Java and JavaScript word-stats first,
then publishes each selected control-plane variant once. Cells consume image
references from `distribution.json` and verify registry digests, build metadata,
running Pod identities and the active scheduler before k6, including on retry.

```bash
NANOFAAS_ROOT=/path/to/nanofaas ./nanolab.sh compare \
  runtime-comparison-jvm.yaml \
  --environment multipass.yaml \
  --variants jvm --repetitions 1 --run-dir /tmp/comparison-my-run
```

The measured scheduler is the unified SchedulerEngine, per-function strategy
(module id: async-queue). Queue-module names survived the scheduler merge;
this matrix varies runtime builds while holding scheduling and admission fixed.
All profiles use k8s-deployment-provider, async-queue and build-metadata, with
runtime scheduler switching disabled. The `jvm` baseline explicitly uses Serial
GC and C1. Earlier samples that omitted the tiering flag or build-metadata belong
to a different experiment series.

`comparison-manifest.json` records immutable inputs, original machine/cluster
identities and committed publications. Captured inputs live under `inputs/` and
`profiles/`; `prepare/<variant>/` contains distribution reports, Gradle logs and
source/JVM/registry evidence. Each cell retains image and scheduler evidence,
k6 summary and Prometheus snapshot; `comparison-report.html` compares cells.
Untracked NanoFaaS files are excluded from the captured source.

The VM is retained after success or failure. Repeating the command resumes only
on the original VM and cluster, after checking inputs, evidence and registry
images; committed images are never rebuilt. Completed cells are skipped.
`--fresh` reruns all cells using the same verified artifacts and infrastructure;
it cannot bypass the identity checks. Changed inputs, replaced machines/clusters,
legacy directories and manifests without a recorded target require a new run
directory. Success removes only that run's remote source staging.

Native profiles compile with the host builder on the measured VM; Oracle GraalVM
is required for G1. Profile validation and fake native tests do not establish
successful native publication. Other load-test backends remain outside this
comparison migration.

## Bash recipe validation

Run `deployment-lifecycle-container-bash.yaml` for the same local Docker
validation cycle with Bash word-stats. The reusable recipe is
`recipes/validate-container-bash.yaml`; the catalog calls its runtime `exec`.
See [profile, command and evidence](src/nanolab/assets/presets/recipes/README.md#bash-container-validation).

## Recipe Java service validation

Run `deployment-lifecycle-container-services.yaml` to validate JVM warm-echo
alongside JVM word-stats with Docker on the host. Java services selected by the
recipe use the same managed registration and cleanup cycle as functions, with
their own distribution component and image checks. See
[profile, command and evidence](src/nanolab/assets/presets/recipes/README.md#java-service-container-validation).

The mixed scenarios `deployment-lifecycle-container-jvm-service-native.yaml`
and `deployment-lifecycle-container-native-service-jvm.yaml` exercise independent
control-plane and service modes. See
[mixed build profiles and commands](src/nanolab/assets/presets/recipes/README.md#independent-service-build-modes).

## Recipe watchdog artifact validation

Run `deployment-lifecycle-container-watchdog.yaml` to publish the Dockerfile
watchdog and verify its executable version, image identity and exit code,
alongside the usual JVM platform validation. See
[profile, command, scope and evidence](src/nanolab/assets/presets/recipes/README.md#watchdog-artifact-validation).

## Multiarch recipe container validation

Run the JVM control plane and Java word-stats lifecycle from a publication for
both AMD64 and ARM64:

```bash
NANOFAAS_ROOT=/path/to/pinned/nanofaas ./nanolab.sh run \
  deployment-lifecycle-container-multiarch.yaml \
  --run-dir /tmp/nanolab-multiarch-run
```

Run this command from the NanoLab repository root. The reusable profile is
`recipes/validate-container-multiarch-jvm.yaml`; CI validates it against its
NanoFaaS pin. The host needs Linux Docker on AMD64 or ARM64, Buildx, the Java
toolchain required by the pinned Gradle project, and access to the image
registries. NanoLab uses host Docker directly and provisions no VM.

The run creates an owned local registry and a dedicated `docker-container`
builder, passed explicitly to Gradle without changing the selected builder.
It uses a digest-pinned binfmt installer when the foreign architecture lacks
a registration. Registration inspection/setup uses a scoped privileged
container; only the required foreign architecture is installed. Existing
functioning registrations are reused. Disabled or incompatible registrations
fail preflight. A daemon-scoped lock serializes NanoLab runs using this path;
a concurrent run receives a busy-lock error and can retry after cleanup.
The lock uses a fixed host path independent of `TMPDIR`. Builder cleanup
checks a unique node ownership marker, including interrupted creation;
failure to write evidence does not bypass compensation.

One `publishRecipe` builds and publishes both platforms. NanoLab checks the
index, each child manifest and each configuration against their SHA-256
digests, then pulls and deploys the native host manifests by immutable digest.
Both platform artifacts are verified; HTTP invocation runs on the host
architecture only. The scenario covers JVM builds with provenance disabled.
Platform checks accept baseline AMD64 (`v1` or unspecified) and ARM64 (`v8`
or unspecified), and reject higher or inconsistent CPU variants.

Evidence under the run directory includes `builder.json`,
`buildkitd.toml`, `recipe/recipe-inputs.json`, the captured profile/source,
`recipe/distribution/distribution.json`, `recipe/gradle.log`, raw registry
bytes and verification records under `registry/`, and `runtime-images.json`
with the selected host references and configuration digests. The original
multiarch report contains no local image IDs; existing runtime checks use
configuration digests derived from verified manifests.

Cleanup releases runtime resources, the owned builder/registry, and any
unchanged binfmt registration created by the run. Changed registrations are
preserved with a cleanup conflict recorded in `builder-cleanup.json`.
Publication or invocation failure retains diagnostics and releases owned
resources. Use automatic cleanup: `--keep`, `--teardown` and `--resume` are
unsupported for this scenario. Start a new run directory for a fresh proof;
runtime mappings are written only after complete artifact verification.

## Recipe container smoke soak

Run the ARM64 JVM/Node smoke using the reusable
`recipes/soak-container-smoke-jvm.yaml` profile:

```bash
NANOFAAS_ROOT=/path/to/pinned/nanofaas ./nanolab.sh run \
  memory-soak-smoke-recipe-container.yaml \
  --environment local.yaml \
  --run-dir /tmp/nanolab-soak-recipe-run
```

Use a fresh run directory. The host needs Linux ARM64 Docker, Buildx, k6 and
the pinned NanoFaaS Gradle project's Java toolchain. NanoLab captures one
immutable source snapshot, including dirty/untracked inputs, then invokes
`publishRecipe` once for the JVM control plane and Java/JavaScript word-stats.
The owned builder uses the verified BuildKit v0.27.1 image pin and the local
HTTP registry; the operator's selected builder stays selected.

Preparation retains maximum provenance, raw registry bytes and actual
Java/Gradle/Node/BuildKit observations. Node compilation occurs in the plugin's
initial build; its inputs are checked against the cached push. Runtime images
use executable manifest digests, with configuration digests checked against
the deployed images. Build recipes in the report are descriptors with
`bake: null`; the recipe artifacts are independently verified offline.

The scenario preserves the original smoke's phases, rates, criteria and
cleanup. Its final report distinguishes numerical results from evidence
completeness and always records `purpose: smoke`, `p24_qualified: false`.
See [the soak contract](../../docs/soak.md) for verification results and
retention/attribution requirements.
