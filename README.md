# nanolab

`Nanofaas/nanolab` is the standalone home for the operational tooling extracted
from nanofaas. It contains three Python workspace members:

- `packages/nanolab`: the nanofaas operations CLI and supporting tooling
- the pinned `sonata-tasks` package from the Sonata repository: reusable workflow task primitives
- `packages/tui-toolkit`: shared terminal UI components

The initial source snapshot comes from nanofaas commit
`4e0aa0751b5f3a5008012994bd4a8843de801316`. Its Git history was intentionally
not preserved.

## Development

Point the tooling at a nanofaas checkout before running repository-dependent
commands:

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
```

For example:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
```

Or use the bundled launcher, which checks for `uv` and forwards all
arguments — equivalent to `uv run --package nanolab nanolab ...`:

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
```

## CI gate

`.github/workflows/ci.yml` runs on every push and pull request against
`main`. It checks out this repo and the pinned nanoFaaS source at
`e7914be065e844776af57fe9e449bce7f12e03c5` into `.nanofaas-source`, points
`NANOFAAS_ROOT` at that checkout, and runs the full gate below. The gate
validates all checked-in recipe profiles. To reproduce profile validation
and the full container lifecycles locally, use the commands below.
`miciav/nanofaas` is private, so the cross-repo checkout authenticates with
the repository secret `NANOFAAS_CHECKOUT_TOKEN` (a fine-grained PAT scoped to
read-only Contents access on `miciav/nanofaas`) rather than the default
`GITHUB_TOKEN`. To reproduce it locally, run the same commands in the same
order against a local nanoFaaS checkout:

```bash
export NANOFAAS_ROOT=/path/to/nanofaas   # e.g. your working mcFaas checkout

uv lock --check
uv sync --locked --all-packages --all-groups

uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --locked --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests

uv run --locked --all-packages --all-groups ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups basedpyright --project packages/tui-toolkit

uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --locked --all-packages --all-groups lint-imports --config packages/tui-toolkit/.importlinter --no-cache

# The same hooks a developer runs on commit: ruff format, bandit and the
# generic file-hygiene checks live here, not in the commands above.
uv run --locked --all-packages --all-groups pre-commit run --all-files

uv build --all-packages --out-dir dist --clear

uv venv .wheel-smoke
uv pip install dist/*.whl --python .wheel-smoke/bin/python
.wheel-smoke/bin/python -c "import nanolab, sonata_tasks, tui_toolkit"
.wheel-smoke/bin/nanolab --help

uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml --environment packages/nanolab/environments/local.yaml
uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --environment packages/nanolab/environments/local.yaml
uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --environment packages/nanolab/environments/multipass.yaml
"$NANOFAAS_ROOT/gradlew" -p "$NANOFAAS_ROOT" validateRecipe -Precipe="$PWD/packages/nanolab/recipes/validate-container-jvm.yaml"
"$NANOFAAS_ROOT/gradlew" -p "$NANOFAAS_ROOT" validateRecipe -Precipe="$PWD/packages/nanolab/recipes/validate-container-native.yaml"
"$NANOFAAS_ROOT/gradlew" -p "$NANOFAAS_ROOT" validateRecipe -Precipe="$PWD/packages/nanolab/recipes/validate-k8s-jvm.yaml"
"$NANOFAAS_ROOT/gradlew" -p "$NANOFAAS_ROOT" validateRecipe -Precipe="$PWD/packages/nanolab/recipes/validate-containerd-jvm.yaml"
"$NANOFAAS_ROOT/gradlew" -p "$NANOFAAS_ROOT" validateRecipe -Precipe="$PWD/packages/nanolab/recipes/loadtest-container-jvm.yaml"
# Full Docker lifecycle E2E (requires Docker):
uv run --package nanolab nanolab run packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml --run-dir "packages/nanolab/runs/recipe-jvm-local"
uv run --package nanolab nanolab run packages/nanolab/scenarios-v2/deployment-lifecycle-container-native.yaml --run-dir "packages/nanolab/runs/recipe-native-local"
# Kubernetes lifecycle E2E (requires running Docker-driver Minikube):
uv run --package nanolab nanolab run packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --run-dir "packages/nanolab/runs/recipe-k8s-local"
# Explicit Multipass uses the same scenario and builds inside the VM:
uv run --package nanolab nanolab run packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --environment packages/nanolab/environments/multipass.yaml --run-dir "packages/nanolab/runs/recipe-k8s-multipass"
```

Nothing in this gate should modify `uv.lock`; if it does, run `uv lock` and
commit the updated lockfile separately.

## AMD64 and ARM64 release recipes

Each native release VM assembles three reusable profiles from
[`packages/nanolab/recipes`](packages/nanolab/recipes):

| Profiles (one per architecture) | Images at NanoFaaS `e7914be0` | Build policy |
| --- | ---: | --- |
| `release-{amd64,arm64}-jvm.yaml` | 9 per architecture | JVM G1/C2: control plane, Java functions and warm-echo |
| `release-{amd64,arm64}-native.yaml` | 12 per architecture | Oracle/O3/G1/JFR for Spring; Community/O3/serial for Java-lite |
| `release-{amd64,arm64}-default.yaml` | 23 per architecture | Bash, Go, JavaScript, Python and watchdog Dockerfiles |

The default control plane is an artifact only. All six profiles select the
same eight explicit modules and receive the guarded release version as their
tag. The frozen archive and profiles define 44 images per architecture.

Both assembly phases use an owned Buildx builder with local loading. Separate
staging phases verify local IDs before pushing to the stack registry. ARM64
assembly needs no registry tunnel; ARM64 push and smoke acquire it. Retained
profiles, source inventory, builder facts, complete logs and raw reports live
under `<run-dir>/releases/<version>/recipe-evidence/<architecture>/`. Resume
verifies retained files and each local image on its owning VM.

CLI boundaries are `--until build-arm64-images` (assembly only),
`--until push-arm64-images-to-local-registry` (also staging push), and
`--until test-arm64-images` (also smoke, before public publication/signing).
Journals from the previous combined ARM build/push DAG fail topology validation;
use a new `--run-dir` to rerun the genuine source tests and benchmark gates.
Existing journals are preserved. Source tests, the three benchmarks, regression,
publication and signing retain their order and policies.

**Verification status (2 October 2026):** all six pinned `validateRecipe` checks,
local tests and executable preflight pass. The real canonical run reached stack
VM acquisition, where Azure rejected network resource creation twice (including after renewed login) with
`401 RequestDisallowedByAzure` because MFA was required. No image assembly,
benchmark or ARM smoke ran. Native ARM64 capability, the isolated staging/runtime
slice and real resume remain incomplete; AMD64 qualification is independently
pending. These migrations are not release-qualified. Evidence is retained under
`/tmp/nanolab-release-arm64-verification/`; see the
[ARM64 plan](docs/superpowers/plans/2026-10-02-release-arm64-recipes.md) and
[AMD64 plan](docs/superpowers/plans/2026-10-01-release-amd64-recipes.md).

## Roadmap

See the [recipes v2 roadmap](docs/recipes-roadmap.md) for the next NanoLab
workflow migrations and validation goals.

## Local SonarQube analysis

With Docker running and `sonar-scanner` installed (`brew install sonar-scanner`
on macOS), scan all three workspace packages with:

```bash
./scripts/sonar.sh
```

The script starts an ephemeral SonarQube Community container on
`127.0.0.1:9000`, analyses `packages/nanolab/src` and
`packages/tui-toolkit/src` together with their
test trees, then prints the open issue counts. It leaves the server running so
the findings remain browsable and writes their complete API response to
`.scannerwork/issues.json`. Use `./scripts/sonar.sh --rm` to remove the server
after the scan, or `./scripts/sonar.sh --dry-run` to inspect the scanner command.

### Using a newer nanoFaaS checkout

CI always pins `NANOFAAS_ROOT` to the nanoFaaS commit recorded above, so the
gate stays reproducible. For local development against a newer nanoFaaS
checkout (e.g. picking up source changes that haven't been re-pinned yet),
just point `NANOFAAS_ROOT` at that checkout instead:

```bash
NANOFAAS_ROOT=/path/to/newer/nanofaas uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
```

Bumping the pin used by CI means updating both the `ref:` in
`.github/actions/setup-workspace/action.yml` and the commit noted in this
README; `test_readme_quotes_the_nanofaas_commit_ci_actually_pins` fails if you
change one and forget the other.

Single-version memory soak: see the [operator guide](docs/soak.md) for presets, required policy, and current readiness limits.

Control-plane heap analysis: see the [operator guide](docs/heap-analysis.md) for the public command, timeline, resource bounds, and how to read the result. It is a diagnostic, not a P24 measurement.

## Runtime comparison with recipes

`./nanolab.sh compare` publishes reusable comparison recipes on the measured VM
and verifies images and scheduling before load. See the [usage and resume
contract](packages/nanolab/README.md#recipe-runtime-comparison),
[profiles](packages/nanolab/recipes/README.md) and
[migration roadmap](docs/recipes-roadmap.md).
