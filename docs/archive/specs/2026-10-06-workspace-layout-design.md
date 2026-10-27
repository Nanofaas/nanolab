# Workspace and distributed resources (#60)

Keep the two existing packages. The merged #59 already places shared execution
wiring in `application`; keep that boundary rather than rename it again.

Bundled presets are the single canonical distributed copy under
`assets/presets/{scenarios,recipes,environments}`. Scenario payloads remain under
`scenarios/payloads`; recipe and soak-policy references remain relative to the
owning scenario. Remove tracked checkout duplicates; preserve ignored operator
configurations. Source and wheel installations resolve the same resources.

CLI accepts existing explicit paths and preset basenames. Explicit paths win,
then named inputs under `NANOLAB_WORKSPACE/{scenarios,environments}`, then bundled
presets. Missing inputs produce a CLI parameter error; nested missing paths must
not silently become presets. Writable profiles and default run results stay in
`NANOLAB_WORKSPACE` (or cwd). Distributed assets are read-only inputs.

Move top-level recipe modules into `tasks/recipes`, using domain names without
the `recipe_` prefix. Move validation workflow and recovery into `tasks/validation`.
Group diagnostic helpers and probes in `assets/diagnostics`; preserve build
contexts, Dockerfile COPY paths, lock inputs and remote protocol behavior.

Move historical `tests/tasks/migrated` modules to matching feature directories;
move recipe and validation tests alongside their domains. Update cross-test
imports and monkeypatch module names. Preserve test bodies for structural moves.

Update documentation, CI, package-data and installed smoke checks. Task-module
paths are internal and move with their callers. The existing #59 public CLI/plan
compatibility aliases remain, deprecated for removal no earlier than 0.7; do not
create broad new alias trees. Old checkout preset paths are replaced by explicit
operator paths or preset names, with migration examples in documentation.

Verify path precedence, missing input diagnostics, no installation-directory
outputs, YAML references, installed wheels, both suites, types and import contracts.
NanoFaaS stays read-only; use the existing CI source pin for contract tests.
