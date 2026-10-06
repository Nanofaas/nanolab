# NanoLab conventions

## Responsibilities and dependencies

Use names describing the responsibility, following neighboring modules. Extract
cohesive policy, evidence validation or observation code when it can change
independently. File length alone is not a reason to split a module.

- `application/` contains environment translation, executor bindings, endpoint
  resolution, function resolution and workload policy shared by entry points.
- `plans/` composes scenario workflows; `tasks/` owns runtime behavior and resources.
- `release/` owns release data, evidence and execution; `comparison/` owns matrix data.
- `cli/` and `tui/` parse, present and invoke the product.
- `core/` remains independent of product packages.

Import contracts in `.importlinter` forbid runtime dependencies on presentation
and plan builders. Sonata is a published dependency, not local source. Reuse its
execution/resource primitives and argument builders before adding equivalents.
Keep provider retargeting and resource ownership explicit.

## Output and execution

CLI presentation may use Typer and Rich. Interactive rendering uses `tui_toolkit`;
workflow event contexts and sinks come from `sonata_engine`. Runtime tasks emit
workflow events and structured artifacts without importing the TUI. Theme setup
lives in `nanolab.tui.setup.setup_ui()`.

Use injected command executors/providers at external boundaries. Drive Gradle,
Docker, Helm and kubectl through the existing Sonata command interfaces. Preserve
cleanup, evidence budgets and validation at those boundaries.

## Tests

Test behavior, formats and failure modes. A module does not need a corresponding
test file merely because it exists; avoid tests repeating constant definitions.
Use recording executors for unit tests and real exported samples for parsers.
Missing measurements must remain distinct from zero.

Pure tests must run with `NANOFAAS_ROOT` unset. Source contracts use
`pytest.mark.nanofaas` or the `nanofaas_root` / `nanofaas_checkout` fixtures.
Modules deriving checkout constants during collection use `source_contract_root`
from the test conftest; shared helpers should defer source access until called.
Configured sources are exported with `git archive` before collection. Installed
wheel checks run outside the source tree and write to the operator workspace.

Quality tools belong to the dev dependency group; run repository commands with
`python -m nanolab.devtools.quality` / `python -m nanolab.devtools.package_report`.
