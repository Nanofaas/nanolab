# nanofaas-workbench 0.1.0 (Ansible collection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new repository, `nanofaas-workbench`, that defines the one-shot platform components (Prometheus, Loki, log collector, k6 load generator, node_exporter, cAdvisor, supervisor) as the Ansible collection `nanofaas.workbench`, with a container form and a Helm pod form. A release is a git tag plus one GHCR image.

**Architecture:** The repository is an Ansible collection.
- **Components:** each component folder is a role under `roles/<component>/`. Its `defaults/` pin the image, `templates/` hold the config, and `tasks/` deploy / collect / undeploy it in either form.
- **Playbooks:** three collection playbooks, `nanofaas.workbench.deploy`, `.collect` and `.undeploy`, run the roles of the instances that a host's vars file lists.
- **Chart:** the Helm chart lives in the collection.
- **Supervisor:** the only custom code and the only image this repository builds. It is a small Python project under `supervisor/`, which is not published as a package.
- **Distribution:** callers install the collection from the GitHub tag. There is no PyPI package.

**Tech Stack:** ansible-core 2.20, `community.docker`, `kubernetes.core`, ansible-lint; Helm 3; Docker; Python 3.12 + uv (supervisor and tests only), ruff (88 cols), basedpyright, bandit, pytest, pre-commit; GitHub Actions (GHCR).

**Spec:** `docs/superpowers/specs/2026-11-08-nanofaas-workbench-design.md` (nanolab repo), Part 2.

## Preconditions

1. Repository `Nanofaas/nanofaas-workbench` exists, public, cloned empty at `~/Downloads/nanofaas-workbench` (done 2026-11-09).

## Global Constraints

- No Sonata, VM, Multipass, Azure or tunnel knowledge anywhere in the repo; every playbook runs by hand against any inventory.
- The supervisor signals only: it never stops, kills or restarts anything.
- Run form is per instance: `container` (`community.docker.docker_container`) or `pod` (`kubernetes.core.helm` on the workbench chart). The same rendered config files feed both forms (container: bind mount; pod: ConfigMap). No bare-process form.
- Collection `nanofaas.workbench`; `galaxy.yml` `version` equals the release tag without `v`; `dependencies` pin `community.docker` and `kubernetes.core` exactly.
- Upstream images pinned by digest in each role's `defaults/main.yml`. Loki at the same version nanolab pins today in `OperatorLogging.loki_binary`, fluent-bit at the version of nanoFaaS `deploy/observability` at CI pin `401391a0`, k6 at the version nanolab's operators run today (`k6 version` recorded in one-shot manifests), Prometheus / node_exporter / cAdvisor at current stable.
- Supervisor image: `ghcr.io/nanofaas/workbench-supervisor:<collection version>`, the default `image` of `roles/supervisor`.
- Resource scrape interval default `5s`, configurable; `honor_timestamps: false` on every scrape job.
- node_exporter and cAdvisor use the standard read-only host mounts (`/proc`, `/sys`, `/`, cgroup, Docker socket for cAdvisor), `pid: host`; pod form `hostPID: true`.
- Every container carries labels `nanofaas.workbench/instance=<instance name>` and `nanofaas.workbench/component=<component>`; container name `wb-<instance name>`; pod-form Helm release name `wb-<instance name>`. Undeploy removes exactly those and is idempotent (a missing instance is success).
- Playbooks targeting `localhost` / `127.0.0.1` use a local connection.
- Python tooling (supervisor + tests) as in nanolab: ruff 88 cols, basedpyright standard, bandit, `SLF` on, coverage `fail_under` in `[tool.coverage.report]`.

## Review Focus

1. An undeploy run against a host where deploy failed halfway (some instances never created) succeeds and removes the ones that exist — Task 3 `test_undeploy_after_partial_deploy`.
2. A supervisor target whose check raises (Prometheus down, malformed query result) is reported `failing`, not a supervisor crash; the HTTP server keeps answering — Task 6 `test_check_error_is_a_failing_target`.
3. Removing a supervisor target while its check is running never lets that check's result reappear in the status — Task 6 `test_removed_target_never_reported`.
4. A rendered Prometheus config with zero targets for a job is still valid (`promtool check config`) — Task 4 `test_empty_job_is_valid`.
5. Re-running deploy with an unchanged vars file does not restart running containers (Ansible idempotence: second run reports `changed=0`) — Task 3 `test_deploy_is_idempotent`.

---

### Task 1: Collection skeleton, the vars format, CI

**Files:**
- Create: `galaxy.yml`, `meta/runtime.yml` (`requires_ansible: ">=2.20,<2.21"`), `README.md`, `CLAUDE.md`, `.gitignore`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`
- Create: `pyproject.toml` + `uv.lock` at the root, a non-package uv project carrying the dev tools and the `supervisor/` sources (`[tool.uv] package = false`, test paths `tests/`, `supervisor/tests/`)
- Create: `playbooks/{deploy,collect,undeploy}.yml` (stubs that load and validate the vars file, no roles yet)
- Create: `docs/vars.md` (the host vars format)
- Test: `tests/test_collection.py`, `tests/test_vars_validation.py`

**Interfaces:**
- Host vars file (JSON; a caller passes it as `-e workbench_vars=<path>`; playbooks `include_vars` it):
  ```json
  {"instances": [{"name": "loki", "component": "loki", "form": "container",
                  "cpuset": "2-3", "config": {"port": 3100}}],
   "collect_dir": "/abs/controller/path"}
  ```
  `component` ∈ `prometheus, loki, log-collector, loadgen, node-exporter, cadvisor, supervisor`; `form` ∈ `container, pod` (default `container`); `name` matches `^[a-z0-9-]+$` and is unique in the file; `cpuset` is a kernel CPU list or absent; `collect_dir` optional.
- Each playbook's first task is an `ansible.builtin.assert` enforcing those rules with a message naming the bad instance — the collection's own validation; callers may validate more.
- Install by hand: `ansible-galaxy collection install git+https://github.com/Nanofaas/nanofaas-workbench.git,v<version>`; run: `ansible-playbook -i <inventory> -e workbench_vars=<file> nanofaas.workbench.deploy`.

- [ ] **Step 1: Write failing tests:** `test_collection_builds` (`ansible-galaxy collection build` produces `nanofaas-workbench-<version>.tar.gz`), `test_installs_and_resolves_playbooks` (install the built tarball into a tmp `-p` path; `ansible-playbook --syntax-check nanofaas.workbench.deploy` with `ANSIBLE_COLLECTIONS_PATH` set passes), `test_duplicate_instance_names_rejected`, `test_unknown_component_rejected`, `test_bad_cpuset_rejected`, `test_valid_vars_pass` (the last four run the `deploy` playbook against `localhost,` with a crafted vars file and check exit code and message).
- [ ] **Step 2: Run** `uv run pytest -q` → FAIL.
- [ ] **Step 3: Implement**; CI job: ruff, basedpyright, bandit, ansible-lint, `ansible-galaxy collection install -r` of `galaxy.yml` dependencies, pytest.
- [ ] **Step 4: Run** `uv run pre-commit run --all-files` and `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `Collection skeleton and the host vars format`.

### Task 2: Playbooks and the first component (node-exporter), container form

**Files:**
- Create: `playbooks/{deploy,collect,undeploy}.yml (fill in the stubs)`
- Create: `roles/node-exporter/{defaults/main.yml,tasks/{deploy,collect,undeploy}.yml,README.md}`
- Create: `tests/integration/conftest.py` (fixture `run_playbook(name, host_vars) -> CompletedProcess` against `localhost,` with the collection installed from the working tree into a session tmp path (`ansible-galaxy collection install . -p …`, `ANSIBLE_COLLECTIONS_PATH`); tests marked `docker`, skipped when `docker info` fails), `tests/integration/test_node_exporter.py`

**Interfaces:**
- Each playbook: `hosts: all`, loads `workbench_vars`, loops over `instances` and `include_role: name={{ item.component }} tasks_from={{ playbook action }}` with `instance: "{{ item }}"`. Deploy order is the vars file order; undeploy runs the list reversed.
- Each role's `tasks/deploy.yml` branches on `instance.form`; this task implements `container` only and fails clearly on `pod` (Task 7 adds it).
- node-exporter `config`: `port` (default `9100`). Value a caller reads back: `http://<host>:<port>/metrics`.

- [ ] **Step 1: Write failing tests:** `test_node_exporter_serves_metrics` (deploy → GET `/metrics` contains `node_cpu_seconds_total`), `test_undeploy_removes_only_labelled_instance` (a second, unrelated container survives), `test_undeploy_missing_instance_succeeds`.
- [ ] **Step 2: Run** `uv run pytest -m docker tests/integration -q` → FAIL.
- [ ] **Step 3: Implement** the playbooks and the role (container `pid_mode: host`, `network_mode: host`, the read-only mounts of the Global Constraints, `cpuset_cpus` from `instance.cpuset`, a `wait_for` on the port).
- [ ] **Step 4: Run** → PASS; ansible-lint clean.
- [ ] **Step 5: Commit** `Playbooks and the node-exporter component`.

### Task 3: cAdvisor and Loki, container form

**Files:** `roles/{cadvisor,loki}/…` as in Task 2; `tests/integration/test_cadvisor.py`, `test_loki.py`, `test_playbooks.py`

**Interfaces:**
- cadvisor `config`: `port` (`8080`), `housekeeping_interval` (`5s`). Runs with `--docker_only=true`, `--store_container_labels=true`, `--whitelisted_container_labels=` unset, so `container_last_seen{name=…}` and Docker labels appear as series labels.
- loki `config`: `bind` (`127.0.0.1`), `port`, `data_dir` (host path; bind-mounted at `/loki`, so the caller owns the raw archive), `retention`, `limits` (ingestion limits). The template is today's: copy nanolab's rendered Loki config (`packages/nanolab/src/nanolab/tasks/one_shot/operator_logging.py:loki_config` and the template it receives) and parametrise only `bind`, `port` and the paths. Readiness: GET `/ready` == 200.
- `collect` for loki: when `collect_dir` is set and the host is not the controller, `fetch` the data dir as a tarball into `<collect_dir>/<instance>/`; on the controller it is a no-op (the data dir is already the caller's).

- [ ] **Step 1: Write failing tests:** `test_cadvisor_reports_labelled_containers` (a container started with label `role=probe` shows `container_last_seen{name="probe",container_label_role="probe"}`), `test_loki_ready_and_writes_to_data_dir`, `test_loki_cpuset_applied` (`docker inspect` `HostConfig.CpusetCpus == "0"`), `test_deploy_is_idempotent` (Review Focus 5: second deploy `changed=0`), `test_undeploy_after_partial_deploy` (Review Focus 1: vars list loki + node-exporter, only node-exporter created by hand; undeploy succeeds and removes it).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `cAdvisor and Loki components`.

### Task 4: Prometheus and the log collector, container form

**Files:** `roles/{prometheus,log-collector}/…`; `tests/integration/test_prometheus.py`, `test_log_collector.py`

**Interfaces:**
- prometheus `config`: `port` (`9090`), `scrape_interval` (`5s`), `retention` (`7d`), `jobs: {<job>: [{target: "host:port", labels: {node: …}}]}`. Template: one `scrape_configs` entry per job, `honor_timestamps: false`, static targets with their labels; admin API enabled (`--web.enable-admin-api`). `collect`: POST `/api/v1/admin/tsdb/snapshot`, archive the snapshot directory to `<collect_dir>/<instance>/prometheus-snapshot.tar.gz`.
- log-collector `config`: `port` (fluent-bit HTTP, `2020`), `config_files` (controller paths of `fluent-bit.conf` and `parsers.conf` — the caller passes nanoFaaS' own `deploy/observability` files, the workbench ships none), `loki: {host, port, uri, tls}` exported as `LOKI_HOST`/`LOKI_PORT`/`LOKI_URI`/`LOKI_TLS` (names fixed by those files), `storage_dir`. `collect`: fetch GET `/api/v2/metrics/prometheus` to `<collect_dir>/<instance>/metrics.txt` and the storage dir tarball.

- [ ] **Step 1: Write failing tests:** `test_empty_job_is_valid` (Review Focus 4: render with `jobs: {node: []}` then `promtool check config` inside the pinned image passes), `test_prometheus_scrapes_node_exporter_with_labels` (deploy node-exporter + prometheus with label `node=n1`; within 20 s `up{job="node",node="n1"} == 1`), `test_scrape_time_not_honored` (rendered config has `honor_timestamps: false` on every job), `test_prometheus_snapshot_collected` (tarball exists and lists `chunks_head` or a block dir), `test_log_collector_ships_to_loki` (deploy loki + log-collector with a minimal tail config from `tests/fixtures/fluent-bit/`; a line written to the tailed file is queryable in Loki within 30 s; `metrics.txt` collected contains `fluentbit_input_records_total`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Prometheus and log-collector components`.

### Task 5: Load generator (k6) and its scripts

**Files:** `roles/loadgen/…` with `files/scripts/one-shot.js` (moved verbatim from nanolab `packages/nanolab/src/nanolab/assets/k6/one-shot.js`); `tests/integration/test_loadgen.py`

**Interfaces:**
- loadgen is a job, not a service. `config`: `script` (a bundled script name, e.g. `one-shot.js`), `env` (mapping passed to k6), `mounts` (`{host_path: container_path}`, read-only unless the container path is `output_dir`), `output_dir` (host path mounted read-write at `/out`), `network` (`host` default, so loopback URLs in schedules work).
- Deploy starts `k6 run --quiet --log-format=json --log-output=file=/out/generator.jsonl /scripts/<script>` detached (`restart_policy: no`, `detach: true`), stderr to `/out/generator.stderr` via the container's command wrapper (`sh -c '… 2>/out/generator.stderr'`).
- `collect` waits for nothing; it fetches `output_dir` when the host is not the controller. Undeploy stops (10 s grace) and removes the container.
- A caller learns exit status with `docker inspect wb-<name> --format '{{.State.Status}} {{.State.ExitCode}}'`; the role README documents it.

- [ ] **Step 1: Write failing tests:** `test_one_shot_script_writes_emitted_and_result_rows` (a 2-row schedule against a local echo container; `generator.jsonl` has 2 `emitted` and 2 `result` rows with `statusCode == 200`), `test_exit_code_visible_after_finish` (status `exited`, code `0`), `test_undeploy_stops_a_running_generator` (a long schedule; undeploy returns within 15 s; no container left).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Load generator component with the one-shot script`.

### Task 6: Supervisor service and image

**Files:**
- Create: `supervisor/src/workbench_supervisor/{__init__,__main__,checks,state,server}.py`, `images/supervisor/Dockerfile` (base `python:3.12-slim` pinned by digest, copies `supervisor/src`), `roles/supervisor/…`
- Test: `tests/supervisor/test_state.py`, `test_checks.py`, `test_server.py`, `tests/integration/test_supervisor.py`

**Interfaces:**
- Stdlib only (`http.server.ThreadingHTTPServer`, `urllib.request`, `threading`); no new runtime dependency.
- Run: `python -m workbench_supervisor --port 8099 --prometheus http://127.0.0.1:9090 --interval 5`.
- Target (JSON body of `PUT /targets/<name>`): `{"kind": …, "params": {…}, "toleratedSeconds": 0}`. Kinds:
  - `scrape_up` — params `job`, `labels`: fails unless `up{job=…, <labels>} == 1`.
  - `container_running` — params `name`, `node`: fails unless `time() - container_last_seen{name=…, node=…} <= 3 * interval`.
  - `swap_quiet` — params `node`, `maxSwapPages` (`256`), `minAvailableBytes` (`8 GiB`), `window` (`30s`): fails if `increase(node_vmstat_pswpin + node_vmstat_pswpout)[window] > maxSwapPages` or `node_memory_MemAvailable_bytes < minAvailableBytes` (the values nanolab's `operator_memory` uses today).
  - `http_ok` — params `url`, `timeoutSeconds`: fails unless the GET returns 2xx.
- `DELETE /targets/<name>` → 204 (also when absent). `GET /status` → `{"state": "healthy"|"degraded"|"fault", "targets": {name: {"kind", "failing": bool, "since": iso|null, "detail": str|null}}, "anomalies": [{"target", "kind", "detail", "since"}], "checkedAt": iso}`. A target failing for less than `toleratedSeconds` makes the state `degraded`; longer, `fault`. `fault` is sticky until the target is removed. `GET /metrics` → `workbench_supervisor_state{state="…"} 1` and `workbench_supervisor_target_failing{target="…",kind="…"} 0|1`.
- Role: container form, `network_mode: host`, `config`: `port`, `prometheus_url`, `interval`, `targets` (initial targets, same JSON as PUT).

- [ ] **Step 1: Write failing tests:** `test_tolerance_degrades_then_faults` (fake clock: failing 3 s with tolerance 10 → `degraded`; 11 s → `fault`), `test_fault_is_sticky_until_removed`, `test_check_error_is_a_failing_target` (Review Focus 2: a check raising `URLError` → target `failing`, `detail` carries the message; next `GET /status` still 200), `test_removed_target_never_reported` (Review Focus 3: a check blocked on an event; DELETE while blocked; release; status has no such target), each kind against a stub Prometheus HTTP server returning canned `/api/v1/query` JSON (`test_scrape_up_*`, `test_container_running_*`, `test_swap_quiet_*`, `test_http_ok_*`), `test_metrics_exposition` (text parses with a regex per line), and the integration `test_supervisor_faults_when_exporter_stops` (deploy node-exporter + prometheus + supervisor with a `scrape_up` target; stop the exporter container; within 30 s state `fault` with anomaly kind `scrape_up`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; the integration test builds the image locally (`docker build -t ghcr.io/nanofaas/workbench-supervisor:test`) and the role takes `image` from `config` when given.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Supervisor service, image and component`.

### Task 7: Pod form — the workbench Helm chart

**Files:**
- Create: `chart/{Chart.yaml,values.yaml,templates/<component>.yaml…}`; the pod branch of every role's `deploy`/`undeploy`
- Test: `tests/test_chart.py` (`helm lint`, `helm template` per component), `tests/integration/test_pod_form.py` (marked `kind`, skipped without `kind` + `kubectl`)

**Interfaces:**
- One release per instance (`wb-<instance>`); values `<component>.enabled: true` plus the instance's `config`; the role renders the same templates as the container form and passes the files in values (`files: {name: content}`) so the chart's ConfigMap carries byte-identical config. node-exporter / cAdvisor render a DaemonSet-free single Pod pinned with `nodeName` from `config.node_name` (one instance per node, as in container form). `cpuset` → `resources.requests/limits.cpu` equal to the CPU count of the list. Undeploy is `helm uninstall` (missing release = success).

- [ ] **Step 1: Write failing tests:** `test_chart_lints`, `test_every_component_templates` (each component enabled alone renders ≥1 object and every object carries the instance label), `test_configmap_matches_container_config` (for prometheus: the ConfigMap's `prometheus.yml` equals the file the container form writes for the same `config`), integration `test_pod_form_node_exporter_and_prometheus` on kind (prometheus scrapes node-exporter; `helm list` empty after undeploy).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; CI gains a `kind` job (`helm/kind-action`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Pod form: the workbench Helm chart`.

### Task 8: End-to-end check, docs, release 0.1.0

**Files:**
- Create: `tests/integration/test_one_shot_shape.py`, `.github/workflows/release.yml`
- Modify: `README.md` (components table, the inventory, deploy/collect/undeploy by hand, run forms, supervisor API), each role's `README.md`

**Interfaces:** `release.yml` on tag `v*`: fail unless the tag equals `v` + `galaxy.yml` `version` and `roles/supervisor/defaults/main.yml` names that version; build and push `ghcr.io/nanofaas/workbench-supervisor:<version>` (public package); build the collection tarball and attach it to a GitHub Release with the pushed image digest in the notes. No PyPI, no Galaxy upload.

- [ ] **Step 1: Write the failing test** `test_one_shot_shape`: one vars file with every container-form component on localhost (node-exporter, cadvisor, loki, log-collector, prometheus scraping both exporters, supervisor with `scrape_up` + `container_running` targets, loadgen with a 3-row schedule); deploy → supervisor `healthy`, loadgen output has 3 results, Prometheus has `node_cpu_seconds_total` and `container_cpu_usage_seconds_total`; collect → snapshot, fluent-bit metrics present under `collect_dir`; undeploy → `docker ps -a --filter label=nanofaas.workbench/instance` empty.
- [ ] **Step 2: Run** → PASS (it exercises Tasks 2–6 together; fix whatever it exposes).
- [ ] **Step 3: Write the docs.**
- [ ] **Step 4: Full check** — `uv run pre-commit run --all-files`, `uv run pytest -q` (unit), `uv run pytest -m docker -q`, CI green on the PR.
- [ ] **Step 5: Commit** `End-to-end shape check, docs and release workflow`.
- [ ] **Step 6 (operator, after merge):** tag `v0.1.0`, push; confirm `ansible-galaxy collection install git+https://github.com/Nanofaas/nanofaas-workbench.git,v0.1.0 -p /tmp/wb` succeeds and `docker pull ghcr.io/nanofaas/workbench-supervisor:0.1.0` works without login.
