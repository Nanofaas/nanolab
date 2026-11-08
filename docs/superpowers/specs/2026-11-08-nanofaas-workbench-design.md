# nanofaas-workbench: the experiment platform as one Sonata resource

Status: draft for review — 2026-11-08

## Intent

Separate two concerns that today live together in nanolab:

- **The platform** (`nanofaas-workbench`, a new repository): what observes,
  supervises and loads a nanoFaaS deployment during an experiment —
  monitoring (Prometheus, Loki, log collectors, node_exporter, cAdvisor), load
  generation (k6 and its scripts), supervision (health checks and fault
  status).
- **The orchestrator** (`nanolab`, this repository): what creates the VMs,
  installs the workbench and nanoFaaS on them, opens tunnels, runs the
  experiment and analyses the results (reports, model comparison, campaigns).

Why: in the one-shot 10-VM runs the platform pieces are spread over many
nanolab tasks (`tasks/one_shot/logging.py`, `operator_logging.py`,
`tasks/loadtest/prometheus.py`, `one_shot/vm_sampler.py`, `supervision.py`,
the recorders), each knowing how to start its own process or binary. Making
the whole platform **one Sonata resource**, built from reusable per-component
tasks, should make the workflow tasks simpler: they ask the resource for an
endpoint instead of knowing how Loki or a sampler is started.

## Goals

- Each platform component lives in its own folder of the workbench repo,
  containerised, with its configuration and its two run forms.
- In nanolab the platform is one Sonata resource whose acquire/release are
  sequences of the components' tasks.
- Sonata supports resources made of tasks and their composition; callable
  resources disappear.
- First milestone: the one-shot 10-VM experiment runs on the workbench
  resource and produces the same signals as today (same Prometheus series,
  same Loki logs, same samples, same supervisor faults), with leaner tasks.

## Non-goals

- Migrating loadtest, soak, experiment-02, comparisons and the other
  experiments. They keep today's wiring and migrate later, one at a time.
- The workbench choosing where it runs. Placement is the orchestrator's,
  per experiment.
- Moving analysis (`one_shot/report.py`, `campaign.py`, `comparison/`) out of
  nanolab. Analysis is "experiment", not "platform".
- A DAG or parallelism inside a resource: an ordered sequence is enough.

## Anti-goals

- The workbench knowing about VMs, Azure, Multipass, tunnels or Sonata. It
  must stay usable by hand.
- The supervisor acting on its own (stopping k6, killing nodes). It signals;
  nanolab is the single place that decides and reacts.
- A one-shot run that "works" but whose observations differ from today's
  without an explained reason.

## Constraints

- nanolab must never write to the nanoFaaS checkout (`NANOFAAS_ROOT`).
- Sonata pin discipline (CLAUDE.md, "Pin discipline"): `sonata-engine` and
  `sonata-tasks` released together at one version, published on PyPI before
  nanolab's pins move; `grep -c 'git+' uv.lock` stays `0`.
- The one-shot runs are measurement runs: CPU placement of operator/logging
  (`HostCpuPlacement`, Loki `cpus`), clock checks, swap refusal and the
  existing run evidence must survive the move.

---

## Part 1 — Sonata: resources made of tasks

Done first; it blocks the other two parts.

### Decided

1. **The task is the reusable unit.** A resource is a pair of tasks: an
   acquire `Task` producing the value, a release `Task` consuming it. Acquire
   and release therefore run through the normal runner: journalled, emitting
   the normal workflow events, resumable.
2. **Callable resources are removed.** `Resource(acquire=callable,
   release=callable)` goes away; there is one way to declare a resource.
   Released as a single breaking Sonata version (engine and tasks under one
   tag); `sonata-tasks`' 9 callable resources are converted in the same tag.
3. **Composite resource.** A resource can be declared as an ordered sequence
   of elements, each either a plain `Task` (runs once during acquire: render a
   config, verify ingestion) or a task resource (started during acquire,
   stopped during release). Semantics:
   - acquire runs the elements in order, like `Steps`; each step journalled;
     the composite's value is the `Members` mapping (see "Settled while
     planning");
   - release first runs an optional ordered sequence of **collect** tasks
     (Prometheus snapshot, log export), then the release task of each member
     resource **that was acquired**, in reverse order;
   - acquire failing at step *k* releases only the member resources acquired
     before *k*, in reverse; the journal records which, so a teardown in a
     later process does the same;
   - resume skips completed steps with the same rules as `Steps`.
   Consumers are unchanged: `Workflow.add(task, requires=(platform,))`.

### Settled while planning (author's calls)

- The composite's members are task resources; plain tasks in the sequence
  are not needed for the first milestone (a "render config" step belongs
  inside the member's own acquire task) — dropped, add when a real case needs
  one.
- The composite's value is a `Members` mapping (member title → member value)
  with a typed accessor `members.of(resource)`; consumers that require the
  platform read `inputs.resource(platform).of(prometheus)`.
- A failing composite acquire unwinds its own already-acquired members
  before re-raising (the engine never releases an acquire that did not
  complete — the same rule `sonata_tasks.process` follows today).
- A composite is `always_release` when any member is, so `keep` never strands
  a member that refuses to be kept.
- A resource that is a member of a composite may not also be required
  directly by a task: compile refuses it, since it would be acquired twice.

### Kept, to be carried onto task resources

`requires`, `keep` / `always_release`, `revive` (a release in a later process
still rebuilds the value from the journal's JSON), `acquire_idempotent`
(becomes the acquire task's own `idempotent`), identity semantics (`eq=False`,
keyed by `id()`), the compiler splicing acquire before the first consumer and
release after the last.

### Journal compatibility (decided)

No compatibility code: the new version need not read journals written by
the callable version. Every journal feature is kept for task resources:
resuming an interrupted run, retaining a resource with `keep`, and reviving
and releasing it from a later process. Before bumping nanolab, kept
resources are released and interrupted runs finished or discarded with the
old version; the plan includes that one-off drain step.

### Left to the builder

- API names (`TaskResource`, `CompositeResource` are working names) and how
  the release task receives the acquired value (via `inputs.resource(...)`,
  `inputs._upstream`, or a dedicated accessor).
- How a composite's members' acquired state is journalled and revived.
- Whether a collect-task failure blocks the stops (recommendation: no — run
  every stop, then report the collect failure, as `supervision.py` does today
  with deferred errors).
- Before releasing: search the GitHub organisation for other importers of
  `sonata_engine.Resource`; if any is found, insert a deprecation release
  first.

---

## Part 2 — nanofaas-workbench

### Decided

- **The workbench is the platform's definition**, not just images. Per
  component, one folder holding:
  - the image: an upstream image pinned by digest when one exists
    (Prometheus, Loki, k6, fluent-bit, node_exporter, cAdvisor); built and
    published by this repo only when none exists (the supervisor);
  - its configuration (scrape config, retention, k6 scripts, supervisor
    checks), parametrised on the targets it observes;
  - **an Ansible role** that deploys, undeploys and (where relevant)
    collects it, in **two run forms** from the same configuration files:
    container (Docker, `community.docker.docker_container`) and pod (via the
    workbench Helm chart, `kubernetes.core.helm`).
- **Deployment is Ansible + Helm** (decided). Ansible is the single entry
  point for every placement; the workbench ships the roles plus top-level
  playbooks (deploy / collect / undeploy) driven by an inventory that says,
  per host, which component instances it receives, their run form and
  placement options. Usable by hand with any inventory.
- **Pod form is one small workbench Helm chart** (decided), one template per
  component enabled through values, so `helm uninstall` removes exactly what
  was installed. The component's config files are passed in as ConfigMaps,
  identical to those mounted in the container form. Not the upstream
  umbrella charts (kube-prometheus-stack, the official Loki chart): they bring
  operators, CRDs and components we do not use, and their configuration would
  diverge from the container form.
- **Placement is never assumed.** No component is tied to another or to a
  fixed "operator" host: nanolab, k6, Loki, Prometheus may each land on any
  host the experiment chooses.
- **Run form is per instance**, not per component or experiment, and follows
  only the kind of host the instance is placed on: a cluster node → pod; any
  other host → Docker container. The experiment may override it. Mixing is
  allowed. There is no bare-process form: a host receiving a workbench
  container without Docker gets Docker from nanolab's provisioning.
- **CPU pinning is a per-instance placement option** (container:
  `--cpuset-cpus`; pod: resource requests/limits), replacing today's
  process-level pinning of Loki/logging CPUs where an experiment needs it.
- **No Sonata, no VM knowledge in the workbench.** It is a versioned Python
  package (published, pinned in nanolab like Sonata) shipping the roles,
  playbooks and configurations as package data (as nanolab bundles its own
  playbooks in `tasks/infra/ansible_assets`), plus the inventory schema; its
  custom images are published to the registry under the same version.
- **Supervisor** is a workbench service: given the targets, it runs periodic
  checks (exporters alive, operator swap, clock drift, nodes responsive,
  Prometheus/Loki up), keeps `healthy | degraded | fault` with anomalies, and
  exposes that over HTTP (and as a Prometheus metric). It does not act.

- **Supervisor targets are mutable** (author's call): its HTTP API lets the
  orchestrator add and remove watched targets while running, mirroring
  today's `watch`/`unwatch`, so an intentional stop or release is never
  reported as a fault. nanolab polls the status at the current supervision
  cadence (5 s), so fault latency matches today's in-process monitor.
- **Standard exporters replace the custom sampler and recorders**
  (decided): **node_exporter** for VM/host CPU and memory, **cAdvisor** for
  per-container CPU, memory and throttling, both upstream images scraped by
  the workbench Prometheus. Default resource scrape interval **5 s**,
  configurable (10 s acceptable); today's sampler samples every 1 s, so
  resolution drops by design. Both run with the standard host mounts
  (`/proc`, `/sys`, cgroup, Docker socket, read-only; `hostPID` pod on
  cluster nodes).
- **One clock** (author's call): resource series use Prometheus' scrape
  time (`honor_timestamps: false`), so samples need no host/guest monotonic
  bracketing to be placed on the experiment timeline; the clock monitor
  stays, to measure and record VM clock drift.
- **The report keeps reading today's files** (author's call): a collect task
  exports the node_exporter/cAdvisor series for the run window into the
  existing `vm-resources` evidence schema (fields with no exporter
  equivalent — per-sample errors, sampler self-CPU — are dropped or derived
  from `up`/scrape gaps), so `vm_resources.py`, `report.py`, `campaign.py`
  and `capacity.py` run unchanged.
- **Images go to GitHub Container Registry** (`ghcr.io/nanofaas/...`),
  versioned with the package. If a site cannot pull, nanolab preloads the
  images on its VMs.
- **Repository and names** (author's call): GitHub organisation `Nanofaas`,
  repository and PyPI distribution `nanofaas-workbench`.

### Components (first milestone: what the one-shot run uses today)

| Folder | Today in nanolab | Image |
| --- | --- | --- |
| `prometheus/` | `tasks/loadtest/prometheus.py`, `tasks/one_shot/observation.py`, `queue_telemetry.py` | upstream |
| `loki/` | `operator_loki_resource` (pinned binary on operator loopback) | upstream |
| `log-collector/` | `collector_resource` (fluent-bit per node, configs from nanoFaaS `deploy/observability`) | upstream |
| `loadgen/` | `tasks/k6.py`, `assets/k6/one-shot.js` | upstream k6 + scripts |
| `node-exporter/` | VM part of `one_shot/vm_sampler.py`; `site_cpu.py`, `operator_memory.py` recorders | upstream |
| `cadvisor/` | container part of `one_shot/vm_sampler.py` | upstream |
| `supervisor/` | health half of `one_shot/supervision.py` | custom |

`vm_sampler.py` and the host recorders are deleted once the export task
reproduces their evidence. `host_cpu.py` stays in nanolab: it validates and
applies host CPU placement for VMs, which is orchestration.
`tasks/one_shot/clock.py` stays in nanolab as a supervisor input.

### Left to the builder

- Repository tooling: same stack as nanolab (uv, ruff 88 cols, basedpyright,
  bandit, pytest, pre-commit) unless a reason appears.
- Exact folder names.
- Role layout and variable names; Jinja templates for configs (Ansible's
  own templating, no other engine).
- Whether one playbook per component or tags on one playbook.

---

## Part 3 — nanolab: the platform as a resource

### Decided

- Each component gets nanolab **tasks** (start/stop, snapshot, export, run
  k6), usable alone in any workflow; each component is a task resource
  (`StartLoki`/`StopLoki`, …). Start/stop/collect tasks are the existing
  `sonata-tasks` Ansible task running the workbench playbooks, limited to the
  component (tag or playbook) and with an inventory nanolab generates from
  the experiment topology. Docker on non-cluster hosts is installed by the
  same mechanism (nanolab's `provision-base.yml` or a workbench role).
- The platform is a **composite resource** of those tasks; its acquire
  receives the experiment's topology (where each instance goes, which targets
  it observes) and returns the endpoints; its collect tasks archive
  snapshots and logs into the run directory.
- The current callable resources become task resources. Those for logging,
  sampling and recording are rewritten anyway for the workbench; the others
  (VMs, provisioning, tunnels — about 47 constructions in 24 files) are
  converted to task resources as part of the Sonata bump.
- Supervision splits: the **health checks** go to the workbench supervisor;
  **reaction** stays in nanolab (`CampaignSupervision` keeps interruption,
  signals, status/event recording, compensation, archive verification) and
  reads the supervisor's status instead of running the checks itself.
- Only the one-shot workflows (calibration, qualification, experiment, and
  their multifunction / heterogeneous / multisite variants) move in the
  first milestone.

### Acceptance

A one-shot 10-VM run (multisite preset) on the workbench resource, compared
with a run on today's main at the same NanoFaaS pin, yields the same run
evidence. "Same" (author's call) means the same *shape*, not identical
values: the same Prometheus series names and labels the reports read, the
same Loki event kinds, the same `vm-resources` evidence schema (now produced by the export task), the same
supervisor fault kinds on injected failures (an exporter killed, swap on the
operator), and the existing report and model comparison running unchanged
on the new run. Values are checked only for plausibility against the
reference run, not for equality.

### Left to the builder

- Order of conversion inside nanolab; whether non-one-shot experiments keep
  compiling on converted resources in the same change (they must keep
  working, wired as today).
- How tunnels/forwarders expose workbench endpoints across sites.

## Order of work

1. Sonata: task resources, composite, removal of callables; release.
2. nanofaas-workbench: repo, one-shot components, supervisor; release.
3. nanolab: bump both pins, convert resources, platform resource for the
   one-shot workflows; acceptance run.
