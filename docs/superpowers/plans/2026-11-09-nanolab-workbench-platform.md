# nanolab: the one-shot platform as a workbench resource — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The one-shot workflows (calibration, qualification, experiment, observation, and their multifunction / heterogeneous / multisite variants) get their monitoring, logging, load generation and health checks from the `nanofaas.workbench` Ansible collection v0.1.0, deployed by Ansible as one composite Sonata resource. The run evidence keeps today's shape.

**Architecture:** A new package `nanolab.workbench` holds four things:
- a Sonata task that runs one workbench playbook on one host, reusing the provisioning path nanolab already has (`_ansible_operation` → `retarget_cloud_operations` → `through_site` → `run_bootstrap_operations`);
- one task resource per component instance;
- a `compose(...)` platform whose collect tasks archive Prometheus, logs and `vm-resources` into the run directory;
- a client for the workbench supervisor.

The one-shot plans then swap their hand-made resources for members of that platform. `CampaignSupervision` keeps every reaction and stops doing the remote checks itself.

**Tech Stack:** Python 3.12, uv workspace, Sonata 0.7.0, ansible-core 2.20 with the `nanofaas.workbench` collection (git tag `v0.1.0`, bringing `community.docker`), pytest, ruff, basedpyright, import-linter.

**Spec:** `docs/superpowers/specs/2026-11-08-nanofaas-workbench-design.md`, Part 3 (and the Part 2 "Decided" list). Workbench API: that repo's README, its `docs/vars.md`, and `docs/superpowers/plans/2026-11-09-nanofaas-workbench-repo.md` Interfaces blocks.

## Preconditions (operator)

1. Tag `v0.1.0` exists on `Nanofaas/nanofaas-workbench` and `ghcr.io/nanofaas/workbench-supervisor:0.1.0` pulls without login.
2. The machine running nanolab for measurement runs is Linux with Docker. Today's operator Loki and the swap checks already need Linux.

## Global Constraints

- nanolab must never write to the nanoFaaS checkout (`NANOFAAS_ROOT`). The fluent-bit files are *read* from `deploy/observability` there and passed to the workbench by path.
- Only the one-shot workflows move. These keep compiling and passing their tests exactly as wired today: loadtest, soak, experiment-02, comparisons, `scripts/one-shot-logging-overhead.py`.
- These survive unchanged: CPU placement of operator/logging (`HostCpuPlacement`, now applied as each instance's `cpuset`), the clock checks (`tasks/one_shot/clock.py` stays in nanolab), the swap refusal before a run (`operator_memory.sample` + `require_quiet` stay), and every run-evidence file the report reads.
- The workbench is pinned in one place: `tasks/infra/ansible_assets/requirements.yml` lists `git+https://github.com/Nanofaas/nanofaas-workbench.git` at `version: v0.1.0`. Nothing is added to `pyproject.toml` / `uv.lock`, and `grep -c 'git+' uv.lock` stays `0` (that invariant is about Python dependencies).
- Per-queue telemetry stays nanolab's own 1 s scrape of `/actuator/prometheus` (`tasks/one_shot/queue_telemetry.py`). The workbench Prometheus scrapes only node_exporter and cAdvisor.
- Placement comes from the one-shot topology:
  - Loki and the k6 loadgen go where today's operator Loki and k6 run, i.e. the nanolab host. A single-site run whose environment sets `logs.backend_owned` puts Loki where `backend_resource` puts it today.
  - Prometheus and the supervisor go on the nanolab host.
  - node-exporter and cAdvisor go on every node VM plus the nanolab host.
  - A log-collector goes on every node VM.
  - Every one-shot node is a Docker host, so every instance takes the container form.
- Commands as in CLAUDE.md, with `NANOFAAS_ROOT` at the CI pin.

## Rulings made while planning (spec silent or contradicted by the code)

1. **vm-resources at the scrape period.** `summarize_resources` counts one slot per second. Exporting 5 s scrapes would make every run look 80 % "missing". So the export writes `policy.json` with `periodSeconds = scrape interval` and `phaseCorrelation = "prometheus-scrape-utc"`, and `summarize_resources` / the slot count read the slot size from the policy. A policy-1 s run (every old run) summarizes byte-identically. `report.py`, `campaign.py` and `capacity.py` stay unchanged. Cost if wrong: a reader that hard-codes 1 s slots somewhere else, which the shape check in Task 8 would catch.
2. **"Sampler cost" becomes observer cost.** The rows' `sampler` field (CPU cores, peak RSS) is derived from cAdvisor's series for that node's `wb-node-exporter`, `wb-cadvisor` and `wb-log-collector` containers. `ObserverPrecheck` keeps its output file and method name, re-pointed to these.
3. **The loadgen container runs on the nanolab host,** with the run directory bind-mounted at `/out` and the host network. `LiveEvidenceCollector` keeps tailing a local `generator.jsonl`, and schedules keep their loopback URLs. A remote loadgen would need a live stream; that waits for a real experiment that places it elsewhere.
4. **Exporters behind sites are scraped through forwards.** Prometheus reaches node exporters the same way nanolab reaches each node's control plane today: a node's exporter ports join the forwards opened for its endpoints, and the scrape target is the forwarded local address, labelled `node=<node id>`.
5. **`site_cpu.py` stays.** It is host CPU placement for sites (orchestration), not a recorder. What gets deleted is `vm_sampler.py`, `OperatorMemoryRecorder` and the sampler parts of `vm_resources.py`.
6. **The clock check stays in-process.** It needs host-monotonic probes that only nanolab has. The workbench supervisor gets no clock kind in 0.1.0.

## Review Focus

1. A platform acquire that fails at member *k* (e.g. one VM's log-collector) leaves no workbench container on any host from members 1..*k*−1 — Task 5 `test_failed_platform_acquire_undeploys_earlier_members`.
2. Two problems must not be confused:
   - a supervisor that is unreachable for longer than its tolerance is an operational failure, as an unreachable remote node is today;
   - a brief blip within the tolerance is not a failure.

   Test: Task 4 `test_supervisor_unreachable_tolerated_then_fault`.
3. A run released by `nanolab teardown` from a later process (journal revive) runs every member's undeploy on the right host — Task 5 `test_platform_release_from_journal`.
4. The vm-resources export over a window where one node's exporter was down for 20 s reports those slots missing, without interpolating — Task 3 `test_scrape_gap_is_missing_slots`.
5. An intentional stop must never be reported as a fault: after `unwatch` of a collector, stopping its container raises nothing — Task 4 `test_unwatched_target_stop_is_silent`.

---

### Task 1: Install the collection, the vars model, the per-host playbook task

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/infra/ansible_assets/requirements.yml` (the pin)
- Modify: `tasks/infra/ansible.py` (add `workbench_collections_path() -> Path`, which is `$NANOLAB_WORKSPACE/.ansible/collections`), `packages/nanolab/.importlinter` (new layer `nanolab.workbench` below `nanolab.plans`, above `nanolab.tasks`)
- Create: `packages/nanolab/src/nanolab/workbench/__init__.py`, `workbench/inventory.py`, `workbench/install.py`, `workbench/playbook.py`
- Test: `packages/nanolab/tests/workbench/test_inventory.py`, `test_install.py`, `test_playbook.py`

**Interfaces:**
- `workbench/inventory.py` is the typed model of the collection's `docs/vars.md` format. nanolab is the only caller that generates it.
  ```python
  Component = Literal["prometheus", "loki", "log-collector", "loadgen",
                      "node-exporter", "cadvisor", "supervisor"]
  class Instance(BaseModel):  # frozen, extra="forbid"
      name: str                     # [a-z0-9-]+, unique per host
      component: Component
      form: Literal["container", "pod"] = "container"
      cpuset: str | None = None     # kernel CPU list, e.g. "2-3"
      config: dict[str, Any] = {}
  class HostVars(BaseModel):
      instances: tuple[Instance, ...]
      collect_dir: str | None = None
  def write_host_vars(path: Path, host_vars: HostVars) -> Path  # JSON, sorted keys
  ```
- `workbench/install.py`:
  - `InstallWorkbench(Task[Path])`, `idempotent=True`, runs `ansible-galaxy collection install -r <ansible_assets/requirements.yml> -p <workbench_collections_path()>`;
  - it skips the install when `ansible_collections/nanofaas/workbench/MANIFEST.json` already reports the pinned version, and returns the collections path;
  - `workbench_resource() -> Resource[Path]` wraps it (its release is a no-op), so every workbench member requires the install once.
- `workbench/playbook.py`:
  ```python
  @dataclass(frozen=True)
  class WorkbenchHost:
      key: str                          # node id, or "operator"
      node: NodeResource | None         # None = the nanolab host
  class WorkbenchPlaybook(Task[None]):
      def __init__(self, title: str, host: WorkbenchHost,
                   action: Literal["deploy", "collect", "undeploy"],
                   host_vars: HostVars, *, vars_dir: Path, installed: Resource[Path])
  ```
  `run` does three things:
  1. writes `vars_dir/<host.key>-<action>.json`;
  2. builds the command with `build_ansible_argv`:
     - the playbook is the installed file `<installed>/ansible_collections/nanofaas/workbench/playbooks/<action>.yml`, given as an absolute path, so `build_ansible_argv`'s `Path.absolute()` leaves it unchanged;
     - `extra_vars={"workbench_vars": path}`;
     - env `ANSIBLE_COLLECTIONS_PATH=<installed>`, plus nanolab's own `ansible.cfg`;
  3. for a node, reuses the provisioning path of `plans/one_shot_common._bootstrap` (`scenario_context`, `retarget_cloud_operations`, `through_site` when `node.site_host`, `run_bootstrap_operations(node.provider, …, role=node.config.id)`); for the nanolab host, runs locally against `localhost,`.

  The VM comes from inputs through `host.node.vm`, and the owning resource requires it.
- To avoid copying `_ansible_operation`, make it public in `tasks/components/bootstrap.py` as `ansible_operation(..., playbook: Path, env: Mapping[str, str] = {})`. Current callers pass `bundled_ansible_root()/"playbooks"/name`.

- [ ] **Step 1: Write failing tests:**
  - `test_duplicate_instance_names_rejected`, `test_bad_cpuset_rejected`, `test_vars_round_trip`;
  - `test_install_skipped_when_version_present` (a fake `MANIFEST.json` at `0.1.0` → no command runs);
  - `test_install_runs_galaxy_with_pin` (argv contains `-r …/requirements.yml -p …`);
  - `test_requirements_pin_the_tag` (the YAML lists the repo at `v0.1.0`);
  - `test_operator_host_runs_locally` (argv starts `ansible-playbook -i localhost,`; env has `ANSIBLE_COLLECTIONS_PATH`; the vars file equals `HostVars`);
  - `test_site_node_goes_through_site`;
  - `test_existing_bootstrap_operations_unchanged` (the provision-base argv is equal before and after the refactor).
- [ ] **Step 2: Run** `… pytest … packages/nanolab/tests/workbench packages/nanolab/tests/tasks/components -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; ruff, basedpyright, lint-imports clean.
- [ ] **Step 5: Commit** `Install the workbench collection and run its playbooks per host`.

### Task 2: Component resources

**Files:**
- Create: `workbench/components.py`
- Test: `tests/workbench/test_components.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Deployed:            # JSON-revivable value of every component resource
      host: str; instance: str; component: str; url: str | None
  def component_resource(host: WorkbenchHost, instance: Instance, *, url: str | None,
                         vars_dir: Path, requires: tuple[Resource[Any], ...] = ()) -> Resource[Deployed]
  ```
  `acquire` is `WorkbenchPlaybook(f"Deploy {instance.name} on {host.key}", host, "deploy", HostVars(instances=(instance,)))` followed by returning `Deployed`. Use a two-step `Steps`, or a small task that runs the playbook and returns the value. `release` is the matching `undeploy`. `revive=lambda raw: Deployed(**raw)`, `always_release=True` (an undeploy never strands containers on a kept VM), `requires=(installed, host.node.vm, *requires)` for nodes, `(installed, *requires)` for the nanolab host.
- `collect_task(host, instances, collect_dir, vars_dir) -> Task[None]` runs the `collect` playbook for those instances.

- [ ] **Step 1: Write failing tests:**
  - `test_component_resource_deploys_then_undeploys` (a workflow with a fake executor records `deploy` before the consumer and `undeploy` after);
  - `test_value_revives_from_json`;
  - `test_undeploy_runs_when_kept` (`keep` does not skip it: always_release);
  - `test_titles_name_instance_and_host`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Workbench component resources`.

### Task 3: vm-resources from Prometheus

**Files:**
- Create: `workbench/vm_export.py`
- Modify: `one_shot/vm_resources.py` (slot size from policy; move `correlate_sample` and `resource_phase` out of `vm_sampler.py` into this module, since the summary keeps using them)
- Test: `tests/workbench/test_vm_export.py`, `tests/one_shot/test_vm_resources.py` (extend)

**Interfaces:**
- `export_vm_resources(prometheus_url, run_dir, *, nodes: Mapping[str, NodeRoles], start: datetime, end: datetime, step_seconds: int) -> None` writes `run_dir/vm-resources/<node>.jsonl` and `policy.json`. `NodeRoles` maps container names to today's roles; reuse the role names `container_identity` assigns (`control-plane`, `collector`, function roles).
- Each row has the fields `summarize_resources` and the report read today:
  - `node`, `timestampUtc` (the scrape time);
  - `vcpuCount`, `cpuUtilization`, `cpuUsedCores`, `cpuIowaitFraction`, `cpuStealFraction` (from `rate(node_cpu_seconds_total[step])` by mode);
  - the memory/swap byte fields of `memory_values` (from `node_memory_*`);
  - `containers` (`containerId`, `role`, CPU cores and memory from cAdvisor);
  - `containerInventory`, `expectedContainerIds`, `sampler` (Ruling 2), `errors: []`.

  Rows carry no `monotonicSeconds`, so `correlate_sample` takes its UTC path. Data comes from `query_prometheus_range_series` (`tasks/loadtest/prometheus.py`).
- `policy.json` matches `telemetry_policy()` except for these keys: `periodSeconds: step_seconds`, `transport: "prometheus-node-exporter-cadvisor"`, `phaseCorrelation: "prometheus-scrape-utc"`, and `samplerSha256` replaced by `exporterImages` (image digests read from the installed collection's `roles/{node-exporter,cadvisor}/defaults/main.yml`).

- [ ] **Step 1: Write failing tests** (a stub Prometheus HTTP server with canned range series):
  - `test_rows_have_the_summary_fields`;
  - `test_cpu_fractions_from_modes` (idle 3 / user 1 per step on 4 vCPUs → `cpuUtilization == 0.25`);
  - `test_container_roles_mapped`;
  - `test_scrape_gap_is_missing_slots` (Review Focus 4);
  - `test_summary_complete_at_five_second_policy`;
  - `test_one_second_policy_summary_unchanged` (an existing fixture run summarizes to the same JSON as before).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `… pytest … tests/workbench tests/one_shot -k "vm_resources or vm_export or report or campaign or capacity" -q` → PASS.
- [ ] **Step 5: Commit** `Export vm-resources from Prometheus`.

### Task 4: Supervisor client in CampaignSupervision

**Files:**
- Create: `workbench/supervisor.py`
- Modify: `one_shot/supervision.py`
- Test: `tests/workbench/test_supervisor_client.py`, `tests/one_shot/test_supervision.py` (extend)

**Interfaces:**
- `SupervisorClient(url, *, timeout_seconds)` with methods `put(name, kind, params, tolerated_seconds)`, `delete(name)` and `status() -> dict`; the dict follows the workbench `GET /status` schema.
- `CampaignSupervision` gains `attach(client, *, tolerated_seconds)`, which registers one in-process check named `workbench`. On each poll that check:
  - raises `RuntimeError(f"{anomaly.target}: {anomaly.kind}: {anomaly.detail}")` on `fault`;
  - tolerates `degraded`;
  - raises once the client has been unreachable longer than `tolerated_seconds` (use `REMOTE_UNREACHABLE_SECONDS`);
  - appends every per-target change to `liveness.jsonl`, in the row shape `LivenessCheck` writes today.
- New method `watch_remote(name, kind, params, *, tolerated_seconds=0)` calls `put` and records `process.watched`.
- `unwatch(name)` also calls `delete` when `name` is remote.
- In-process watches stay as they are: generator, evidence collector, queue telemetry, clock.

- [ ] **Step 1: Write failing tests:**
  - `test_fault_becomes_operational_failure` (cause carries target and kind);
  - `test_degraded_is_tolerated`;
  - `test_supervisor_unreachable_tolerated_then_fault` (Review Focus 2);
  - `test_unwatched_target_stop_is_silent` (Review Focus 5);
  - `test_liveness_rows_keep_their_shape`;
  - `test_in_process_watches_unchanged` (existing tests still pass untouched).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `… pytest … tests/workbench tests/one_shot/test_supervision.py -q` → PASS.
- [ ] **Step 5: Commit** `Read health from the workbench supervisor`.

### Task 5: The one-shot platform composite

**Files:**
- Create: `workbench/platform.py`
- Test: `tests/workbench/test_platform.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class OneShotPlatform:
      resource: Resource[Members]
      loki: Resource[Deployed]; prometheus: Resource[Deployed]; supervisor: Resource[Deployed]
      loadgen_host: WorkbenchHost
      collectors: Mapping[str, Resource[Deployed]]      # node id → log collector
  def one_shot_platform(topology, settings, *, run_dir: Path, nanofaas_root: Path,
                        forwards: Mapping[str, Mapping[str, str]]) -> OneShotPlatform
  ```
  `forwards[node]["node-exporter" | "cadvisor"]` is the local address Prometheus scrapes (Ruling 4).
- Member order: exporters (operator first, then nodes) → Loki → log collectors → Prometheus → supervisor. The supervisor's initial targets are:
  - `scrape_up` for every exporter;
  - `container_running` for every node's control plane and collector;
  - `swap_quiet` on the operator.

  Prometheus scrape jobs come from the same table.
- Collect tasks, in order:
  1. Prometheus snapshot into `run_dir/workbench/`;
  2. `export_vm_resources` for the run window;
  3. today's Loki export and ingestion verification (`tasks/one_shot/logging.export_queue_events`, `verify_loki_ingestion`);
  4. each collector's `collect`, then today's `collection_evidence` on its `metrics.txt`;
  5. supervisor final `/status` into `run_dir/workbench/supervisor-status.json`.
- Loki `cpuset` and log-collector `cpuset` come from `HostCpuPlacement` / `OperatorLogging.cpus`. Each instance's `docker inspect` (image digest, cpuset) goes into today's backend and collector evidence fields.
- The loadgen is **not** a member: it is a job started per run (Task 7).

- [ ] **Step 1: Write failing tests** (fake `WorkbenchPlaybook` executor recording `(host, action, instances)`):
  - `test_member_order_and_reverse_release`;
  - `test_failed_platform_acquire_undeploys_earlier_members` (Review Focus 1);
  - `test_platform_release_from_journal` (Review Focus 3: release in a fresh process from the journal runs every undeploy on the recorded hosts);
  - `test_supervisor_targets_cover_every_node`;
  - `test_cpusets_follow_host_cpu_placement`;
  - `test_collect_runs_before_undeploy`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `The one-shot platform composite`.

### Task 6: One-shot logging on the platform

**Files:**
- Modify: `plans/one_shot_common.py` (`add_one_shot_platforms`: replace `operator_loki_resource`, `backend_resource`, `collector_resource` and `watch_container` with the platform; `topology` gains `platform`, `collectors` become the platform's)
- Modify: `tasks/one_shot/operator_logging.py` (keep `log_routes`, `ReverseTunnel` and `site_bridge`, which now point at the Loki container's published port)
- Modify: `tasks/one_shot/preflight.py` (`CollectTopologyTask` reads collector evidence from the platform values)
- Tests: `tests/one_shot/test_logging.py`, `test_operator_logging.py`, `test_loki_ingestion.py`, `test_preflight.py`, `test_multisite_resources.py`, `tests/plans/test_one_shot_experiment.py`

**Interfaces:** Consumes Task 5. `scripts/one-shot-logging-overhead.py` keeps importing `backend_resource` / `collector_resource`, so those stay defined until it migrates; mark them `# ponytail: kept for scripts/one-shot-logging-overhead.py, delete when it moves`.

- [ ] **Step 1: Convert** the plan wiring; the reverse tunnels and site routes target the Loki instance's `url`.
- [ ] **Step 2: Update tests**:
  - assertions on titles/argv of the old Loki and collector processes become assertions on the platform's playbook calls;
  - assertions on the evidence files (ingestion verification, collector evidence keys, `logging/backend/…` paths) stay unchanged.
- [ ] **Step 3: Run** `… pytest … tests/one_shot tests/plans -q` → PASS.
- [ ] **Step 4: Commit** `One-shot logging on the workbench platform`.

### Task 7: Telemetry, load generator and supervision on the platform

**Files:**
- Create: `workbench/loadgen.py`
- Modify:
  - `plans/one_shot_observation.py` and `plans/one_shot_experiment.py`: drop `vm_telemetry_resource` and `check_sampler`; `generator_resource` becomes the loadgen job; call `supervision.attach(...)` once the platform is acquired;
  - `tasks/one_shot/experiment.py`: `generator_resource` and `evidence_collector_resource`;
  - `tasks/one_shot/capacity.py:318` and `tasks/one_shot/qualification_load.py:198`: they drive the generator by hand inside an `ExitStack`; keep that, using `acquire_now` / `release_now` on the new resource;
  - `tasks/one_shot/observation.py`: `ObserverPrecheck` uses Ruling 2;
  - `scripts/one-shot-observe.py`: drop `OperatorMemoryRecorder`, keep `sample` + `require_quiet`.
- Tests: `tests/one_shot/test_first_observation.py`, `test_observation_workflow.py`, `test_capacity_sweep.py`, `test_qualification_load.py`, `test_generator_paths.py`, `test_observation_final_regressions.py`, `tests/plans/test_one_shot_experiment.py`

**Interfaces:**
- `loadgen_resource(host, *, run_dir, vus, duration, settings, requires) -> Resource[LoadgenRun]`. It keeps today's title `"Own bounded one-shot k6 generator"` and today's env computation (`ONE_SHOT_SCHEDULE`, `ONE_SHOT_VUS`, `ONE_SHOT_DURATION`, with paths under `/out`).
- `LoadgenRun.poll() -> int | None` reads `docker inspect wb-<name>`: `None` while the container runs, its exit code after. `CampaignSupervision.check_process` keeps working unchanged.
- The release is the loadgen undeploy (stop with the grace `process_exit_seconds`).
- The k6 script now comes from the workbench (`script: "one-shot.js"`). Delete `assets/k6/one-shot.js` from nanolab.
- Manifests keep recording `("k6", "version")`, now read from the pinned image.

- [ ] **Step 1: Write failing tests:**
  - `test_loadgen_poll_tracks_container` (fake `docker inspect`: running → `None`, exited 3 → `3`);
  - `test_check_process_on_loadgen` (an early exit 0 before `expected_end` raises, as with `Popen`);
  - `test_generator_env_unchanged` (env equals today's except the `/out` paths);
  - `test_observer_precheck_uses_exporter_cost`;
  - `test_no_operator_memory_recorder` (the observe script never constructs it; the precheck still refuses a swapping operator).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**; convert the listed tests.
- [ ] **Step 4: Run** `… pytest … tests/one_shot tests/plans -q` → PASS.
- [ ] **Step 5: Commit** `One-shot telemetry, load and supervision on the workbench`.

### Task 8: Delete what the workbench replaced; shape check; full verification

**Files:**
- Delete:
  - `one_shot/vm_sampler.py`;
  - `OperatorMemoryRecorder` from `one_shot/operator_memory.py`;
  - `VmTelemetrySession`, `vm_telemetry_resource` and `check_sampler`;
  - `operator_loki_resource`;
  - their tests, or the parts of tests that cover only them.
- Create: `scripts/compare-run-shape.py`
- Test: `tests/scripts/test_compare_run_shape.py`

**Interfaces:**
- `compare-run-shape.py REFERENCE_RUN NEW_RUN` exits 0 when the two runs have the same shape, and prints every difference otherwise. It compares:
  - the Prometheus series names and label keys the reports read (`report.py`, `campaign.py`);
  - the Loki event kinds in the exported logs;
  - the key sets of every `vm-resources` row and of `coverage.json`;
  - the supervisor fault kinds in `events.jsonl`;
  - whether `report.json` regenerates.

  It checks values only for plausibility: same order of magnitude, non-empty.

- [ ] **Step 1: Write failing tests:** `test_same_shape_passes`, `test_missing_series_reported`, `test_extra_row_key_reported`, all on small fixture run directories.
- [ ] **Step 2: Implement; delete.** Run `grep -rn "vm_sampler\|OperatorMemoryRecorder\|check_sampler\|vm_telemetry_resource\|operator_loki_resource" packages scripts`. The only hits allowed are the ponytail-kept `backend_resource`/`collector_resource` users.
- [ ] **Step 3: Full verification:** the four CLAUDE.md commands at the CI pin, plus `uv run pre-commit run --all-files`. Compare against the current-main baseline: no failing test ids beyond it, coverage at or above `fail_under`.
- [ ] **Step 4: Commit** `Remove the sampler and recorders the workbench replaced`.

- [ ] **Step 5 — acceptance (operator; not CI):**
  1. Run the multisite 10-VM preset on `main` before this branch, then on this branch, at the same NanoFaaS pin.
  2. Run `scripts/compare-run-shape.py OLD NEW` → exit 0.
  3. Rerun the report and the model comparison on NEW.
  4. Inject failures on NEW:
     - `docker stop wb-node-exporter` on one VM → a `scrape_up` fault;
     - force swap on the operator (`stress-ng --vm` beyond available memory) → a `swap_quiet` fault.

     Both runs must end `failed` with that cause.
