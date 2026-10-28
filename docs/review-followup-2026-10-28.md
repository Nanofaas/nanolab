# Review follow-up — 28 October 2026

Base: `818e3fd`; local branch: `refactor/review-followup-20261028`.

## Scope and decisions

- P2: reuse installed baseline identities and republish their cached image tags. A missing cached image stops the run instead of silently rebuilding it.
- Finite Git, Docker inventory, Multipass inventory and k6-version queries have explicit timeouts. Docker inspect uses 30 seconds, Docker save 1800 seconds, and release inventory 600 seconds; archive and inventory publication is atomic. Existing supervised observers, builds and test runners retain their lifecycle budgets.
- Provisioning and experiment-02 orchestration live in `application`; the CLI injects preparation and progress reporting.
- The seven S7 functions were refactored in individual commits after characterization. Tests preserve cleanup, identity checks, evidence budgets and cancellation.
- Tests use an isolated operator workspace by default; tests of cwd behavior explicitly unset the override.

## S5 decisions

- Remove `build_arm64_image_plan`: it duplicates the direct call that the release contracts require. Tests now use `build_image_plan(..., architectures=("arm64",))`; the guarded call in plans/release.py is unchanged.
- Keep `select_presentation` and `show_launch_failure`: scripts/one-shot-observe.py calls both.
- Keep `render_bake_json`: it is the small serialization API for a supported Bake document; deleting it would remove a useful callable format adapter rather than simplify runtime logic.
- Keep `runtime_class`: it exposes the distinction between historical build labels and executable runtime class, which its contract tests exercise.
- Keep `render_paired_campaign`: it is a callable report API producing paired numerical JSON and HTML. The lack of a CLI caller is not enough reason to remove that reporting behavior.
- Keep `MODULES_WITHOUT_QUERIES`: it is declarative classification data used by the real nanoFaaS catalogue-completeness contract, not an unused function.

## S12

The distribution metadata is the version source. Content width comes from the active UIContext; Console width remains a rendering property set by init_ui. A temporary bind_ui context now also affects width queries. The console-to-context import stays local to avoid their initialization cycle.

## Retained local imports

107 unneeded local import statements were hoisted. The table records the reason for each of the 83 retained statements at the time of that change. Dependency-cycle detection is conservative and includes imports made inside other functions; it avoids introducing import-time cycles during this cleanup.

| Module | Function | Import | Reason |
|---|---|---|---|
| `plans/dispatch.py` | `build_scenario_workflow` | `from nanolab.plans.one_shot_calibration import build_one_shot_calibration_plan` | conditional or nested scope; preserve deferred initialization |
| `plans/dispatch.py` | `build_scenario_workflow` | `from nanolab.plans.one_shot_experiment import build_one_shot_experiment_plan` | conditional or nested scope; preserve deferred initialization |
| `plans/dispatch.py` | `build_scenario_workflow` | `from nanolab.plans.one_shot_qualification import build_one_shot_qualification_plan` | conditional or nested scope; preserve deferred initialization |
| `plans/dispatch.py` | `build_scenario_workflow` | `from nanolab.plans.contract import build_contract_plan` | conditional or nested scope; preserve deferred initialization |
| `plans/dispatch.py` | `build_scenario_workflow` | `from nanolab.plans.soak import build_soak_plan` | conditional or nested scope; preserve deferred initialization |
| `plans/dispatch.py` | `build_scenario_workflow` | `from nanolab.plans.heap_analysis import build_heap_analysis_plan, unique_heap_analysis_run_dir` | conditional or nested scope; preserve deferred initialization |
| `plans/soak.py` | `build_soak_plan` | `from nanolab.plans.containerd_soak import build_containerd_soak_plan` | conditional or nested scope; preserve deferred initialization |
| `plans/soak.py` | `build_soak_plan` | `from nanolab.tasks.soak.preparation import check_local_process_support` | conditional or nested scope; preserve deferred initialization |
| `plans/soak.py` | `build_soak_plan` | `from nanolab.tasks.soak.recipe import validate_soak_recipe` | conditional or nested scope; preserve deferred initialization |
| `plans/validate.py` | `require_recipe_environment` | `from nanolab.tasks.soak.recipe import validate_soak_recipe` | conditional or nested scope; preserve deferred initialization |
| `plans/cli.py` | `build_cli_plan` | `from nanolab.plans.cli_recipe import build_recipe_cli_plan` | conditional or nested scope; preserve deferred initialization |
| `plans/cli.py` | `build_cli_plan` | `from nanolab.workspace.paths import operator_workspace_root` | conditional or nested scope; preserve deferred initialization |
| `config/one_shot.py` | `validate_endpoint` | `from ipaddress import IPv4Address` | conditional or nested scope; preserve deferred initialization |
| `tui/workflow.py` | `start` | `import termios` | conditional or nested scope; preserve deferred initialization |
| `tui/workflow.py` | `start` | `import tty` | conditional or nested scope; preserve deferred initialization |
| `one_shot/capacity.py` | `read_concurrent_capacity` | `from nanolab.one_shot.profiles import relative_reference` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/models.py` | `validate_budget` | `from nanolab.tasks.one_shot.statistics import quantile` | conditional or nested scope; preserve deferred initialization |
| `one_shot/models.py` | `checked` | `from nanolab.tasks.one_shot.trace import TraceArtifact` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/models.py` | `checked` | `from nanolab.one_shot.campaign import analysis_denominator, analysis_epochs` | conditional or nested scope; preserve deferred initialization |
| `one_shot/models.py` | `checked` | `from nanolab.one_shot.vm_resources import telemetry_policy` | conditional or nested scope; preserve deferred initialization |
| `one_shot/campaign.py` | `freeze_campaign_protocol` | `from nanolab.one_shot.profiles import read_profiles, validate_trace_capacity` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/campaign.py` | `freeze_campaign_protocol` | `from nanolab.tasks.one_shot.experiment import read_qualification, verify_qualified_load` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/campaign.py` | `freeze_campaign_protocol` | `from nanolab.tasks.one_shot.trace import capacity_trace` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/campaign.py` | `freeze_campaign_protocol` | `from nanolab.tasks.one_shot.trace import concurrent_trace` | conditional or nested scope; preserve deferred initialization |
| `one_shot/campaign.py` | `read_campaign_binding` | `from nanolab.one_shot.models import CampaignProtocol` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/campaign.py` | `read_campaign_binding` | `from nanolab.tasks.one_shot.trace import experiment_trace` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/supervision.py` | `_copy_archive_controls` | `from nanolab.one_shot.report import finalize_operational_outcome` | conditional or nested scope; preserve deferred initialization |
| `one_shot/topology.py` | `observe_neighbors` | `from nanolab.one_shot.client import NanoFaasOneShotClient` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/profiles.py` | `read_profiles` | `from nanolab.one_shot.capacity import read_concurrent_capacity` | conditional or nested scope; preserve deferred initialization |
| `one_shot/profiles.py` | `synchronized_timing_trace` | `from nanolab.tasks.one_shot.trace import concurrent_trace` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/profiles.py` | `validate_trace_capacity` | `from nanolab.tasks.one_shot.trace import validate_cloud_headroom` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/profiles.py` | `validate_trace_capacity` | `from nanolab.one_shot.capacity import capacity_configurations` | conditional or nested scope; preserve deferred initialization |
| `one_shot/profiles.py` | `validate_trace_capacity` | `from nanolab.tasks.one_shot.trace import cloud_capacity_requirements, validate_multi_cloud_bounds` | conditional or nested scope; preserve deferred initialization |
| `one_shot/report.py` | `render_report` | `from nanolab.one_shot.returned_errors import summarize_responses` | conditional or nested scope; preserve deferred initialization |
| `one_shot/report.py` | `render_report` | `from nanolab.tasks.one_shot.experiment import generator_rows` | conditional or nested scope; preserve deferred initialization |
| `one_shot/report.py` | `render_report` | `from nanolab.one_shot.vm_resources import summarize_resources` | conditional or nested scope; preserve deferred initialization |
| `one_shot/report.py` | `render_paired_campaign` | `from nanolab.one_shot.campaign import paired_summary` | dependency has a path back to this module; avoid initialization cycle |
| `one_shot/views.py` | `open_presentation` | `from nanolab.tui.workflow_controller import TuiWorkflowController` | conditional or nested scope; preserve deferred initialization |
| `cli/product.py` | `_default_run_dir` | `from nanolab.plans.heap_analysis import unique_heap_analysis_run_dir` | conditional or nested scope; preserve deferred initialization |
| `cli/product.py` | `execute_workflow` | `from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor` | conditional or nested scope; preserve deferred initialization |
| `cli/product.py` | `execute_workflow` | `from nanolab.plans.contract import finalize_contract_run` | conditional or nested scope; preserve deferred initialization |
| `cli/product.py` | `execute_workflow` | `from nanolab.one_shot.report import render_campaign` | conditional or nested scope; preserve deferred initialization |
| `cli/product.py` | `run_command` | `from nanolab.plans.soak import teardown_soak_run` | conditional or nested scope; preserve deferred initialization |
| `cli/product.py` | `run_command` | `from nanolab.tasks.soak.containerd_runtime import finalize_containerd_terminal` | conditional or nested scope; preserve deferred initialization |
| `cli/product.py` | `run_command` | `from nanolab.tasks.soak.containerd_runtime import finalize_containerd_terminal` | conditional or nested scope; preserve deferred initialization |
| `comparison/prepare.py` | `prepare_comparison` | `from nanolab.comparison.evidence import require_recorded_publications, verify_comparison_publication` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/logging.py` | `acquire` | `from nanolab.one_shot.host_cpu import pin_logging_project` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/trace.py` | `concurrent_trace` | `from nanolab.one_shot.capacity import capacity_configurations` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/qualification_load.py` | `__enter__` | `from nanolab.tasks.one_shot.experiment import LiveEvidenceCollector, generator_resource` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/qualification_load.py` | `__enter__` | `from nanolab.one_shot.vm_resources import VmTelemetrySession` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/qualification_load.py` | `audit` | `from nanolab.tasks.one_shot.experiment import generator_rows, physical_attempts` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/capacity.py` | `_run_capacity_trial` | `from nanolab.tasks.one_shot.calibration import measure_invocation` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/capacity.py` | `_run_capacity_trial` | `from nanolab.tasks.one_shot.experiment import LiveEvidenceCollector, classify_result, collect_runtime_evidence, generator_resource, generator_rows, physical_attempts` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/capacity.py` | `_run_capacity_trial` | `from nanolab.tasks.one_shot.preflight import runtime_endpoint` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/capacity.py` | `_run_capacity_trial` | `from nanolab.tasks.one_shot.preflight import collect_container_cgroup, validate_strict_cgroup` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/experiment.py` | `run` | `from nanolab.one_shot.capacity import comparison_replica_vectors` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/experiment.py` | `run` | `from nanolab.one_shot.profiles import read_profiles` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/experiment.py` | `run` | `from nanolab.one_shot.capacity import comparison_replica_vectors` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/experiment.py` | `collect_original_evidence` | `from nanolab.one_shot.report import render_report` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/qualification.py` | `run` | `from nanolab.one_shot.profiles import synchronized_timing_trace` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/qualification.py` | `run` | `from nanolab.one_shot.capacity import comparison_replica_vectors` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/qualification.py` | `run` | `from nanolab.tasks.one_shot.preflight import collect_container_cgroup, validate_strict_cgroup` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/calibration.py` | `workload_checksum` | `from nanolab.tasks.one_shot.workload import expected_output` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/calibration.py` | `run` | `from nanolab.one_shot.capacity import capacity_configurations` | local import alias differs from module binding |
| `tasks/one_shot/calibration.py` | `run` | `from nanolab.tasks.one_shot.trace import cloud_capacity_requirements, concurrent_trace` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/calibration.py` | `_publish_nodes` | `from nanolab.one_shot.capacity import read_concurrent_capacity` | conditional or nested scope; preserve deferred initialization |
| `tasks/one_shot/preflight.py` | `_collect` | `from nanolab.one_shot.client import NanoFaasOneShotClient` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/one_shot/preflight.py` | `_collect` | `from nanolab.one_shot.host_cpu import collect_host_placement` | conditional or nested scope; preserve deferred initialization |
| `tasks/provisioning/providers.py` | `provider_for` | `from sonata_tasks.vm.providers.azure import AzureVmProvider` | conditional or nested scope; preserve deferred initialization |
| `tasks/provisioning/providers.py` | `provider_for` | `from sonata_tasks.vm.providers.proxmox import ProxmoxVmProvider` | conditional or nested scope; preserve deferred initialization |
| `tasks/soak/acceptance.py` | `_provenance` | `from nanolab.tasks.soak.recipe_evidence import verify_soak_recipe_receipt` | conditional or nested scope; preserve deferred initialization |
| `tasks/soak/builds.py` | `run` | `from nanolab.tasks.soak.build_observation import BuildObservationCapture` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/soak/runtime.py` | `_make_runtime_prerequisites` | `from nanolab.tasks.soak.prerequisite_platform import freeze_effective_config, make_prerequisite_platform_factory` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/soak/runtime.py` | `prerequisites` | `import asyncio` | conditional or nested scope; preserve deferred initialization |
| `tasks/soak/runtime.py` | `prerequisites` | `from nanolab.tasks.soak.artifacts import ArtifactWriter` | conditional or nested scope; preserve deferred initialization |
| `tasks/soak/runtime.py` | `prerequisites` | `from nanolab.tasks.soak.prerequisites import run_prerequisites` | conditional or nested scope; preserve deferred initialization |
| `tasks/soak/runtime.py` | `run` | `from nanolab.tasks.soak.composition import compose_frozen_soak_workflow` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/soak/build_observation.py` | `start` | `from nanolab.tasks.soak.build_provenance import write_observation_request` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/soak/build_observation.py` | `complete` | `from nanolab.tasks.soak.build_provenance import provenance_materials, provenance_predicate` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/soak/retention.py` | `restore` | `from nanolab.tasks.soak.teardown import read_cleanup_records` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/soak/composition.py` | `compose_frozen_soak_workflow` | `from nanolab.tasks.soak.runtime import capture_function_owner, verify_function_owner` | dependency has a path back to this module; avoid initialization cycle |
| `tasks/soak/preparation.py` | `check_preparation_support` | `from nanolab.tasks.soak.collector import collect_http` | conditional or nested scope; preserve deferred initialization |
| `tasks/recipes/multiarch.py` | `acquire` | `from nanolab.tasks.recipes.registry import fetch_local_registry, verify_multiarch_registry` | dependency has a path back to this module; avoid initialization cycle |

## Verification

CI of the base: https://github.com/Nanofaas/nanolab/actions/runs/40665453203 — all seven jobs passed, including cli-container and validateRecipe.

Before the import cleanup: 293 Linux domain tests passed, one skipped; type checking passed. MacOS-specific baseline comparison reproduced all 173 failures of the broad timeout-domain run on clean 818e3fd, with identical test IDs. Full branch verification results will be recorded below.

No PR or push is authorized. nanoFaaS remains read-only; Linux checks use a disposable source snapshot. Commit trailers identify the actual Codex thread rather than inventing a Claude session URL.

## Review outcome

The independent whole-branch review found no remaining Critical/Important issue
after the three remaining subprocess deadlines were fixed. It confirmed AST
equivalence of the 13 acceptance checks and the experiment-02 orchestration,
and imported all 51 modified modules in fresh interpreters without errors.

Two non-blocking cleanup suggestions remain deliberately separate: the redundant
ValueError rethrow in application/experiment_02.py and the unused CLI copy of
EXPERIMENT_02_SCRIPT_NAME. Neither changes execution behavior.

Real Azure provisioning, live registry migration, new builds, long soak runs and
quantitative timeout calibration were not repeated. The pinned source contracts
and Linux suites are the verification boundary for this refactoring.

## Complexity measurements

Ruff C901 against 818e3fd, measured after the responsibility extractions. The
branches still exist in named helpers or methods; these numbers describe each
original entry point rather than claiming that domain rules were removed.

| Function | Before | After |
|---|---:|---:|
| `create_soak_lifecycle` | 67 | 58 |
| `_run_capacity_trial` | 59 | 50 |
| `evaluate_acceptance` | 52 | 1 |
| `_exchange` | 49 | 32 |
| `collector_resource` | 45 | 33 |
| `create_server` | 43 | 1 |
| `collect_runtime_evidence` | 41 | 21 |

## Additional verification fix

The complete suite exposed overlapping status updates sharing `status.json.tmp`.
A deterministic two-writer test reproduced the missing-temporary-file error.
Each status write now gets a unique temporary file, published atomically and
cleaned on every exit. All 215 recording, presentation and supervision tests
passed, including storage errors and signals during receipt persistence.
This fix followed the independent review and was verified by the regression
and the full-suite checks reported below.

## Final verification results

- Linux pure NanoLab suite: **5066 passed, 37 skipped**, no source checkout in
  the environment; 981.04 seconds.
- NanoFaaS contracts against the real read-only CI pin: **644 passed, 1 skipped**;
  340.65 seconds. Combined branch coverage: **87.51%**, above the required 86%.
- tui-toolkit complete suite: **53 passed**, coverage **94.84%**.
- All pre-commit hooks passed, including both type checks, import contracts,
  Bandit, Ruff and file hygiene.
- The last release fixture correction touched contract-only tests; production
  sources stayed byte-identical to the snapshot used by the green pure suite.
- Linux checks used Python 3.12 in a local disposable container, executed from
  the repository root. Pytest used `tmp_path_retention_policy=failed` and owned
  temporary directories, cleaned between phases. No NanoFaaS checkout was written.

The branch remains local, with no PR, merge or push. The intentionally excluded
soak repository default, app directory name, scripts, direct ARM64 plan seam and
commented gitignore entries remain unchanged.
