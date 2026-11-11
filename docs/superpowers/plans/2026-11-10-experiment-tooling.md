# Experiment Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, in native mode.

**Goal:** Unify deployment and evidence handling while preserving reproducible experiments and recovery.

**Architecture:** OpenTofu provisions, Ansible configures/deploys/collects, Sonata composes lifecycle. Workbench provides technical collection; Nanolab assesses scientific evidence and retains infrastructure on failed recovery.

**Tech Stack:** Python 3.12+, Sonata 0.7.1, Ansible core 2.20, uv, Docker, OpenTofu/Multipass.

**Spec:** docs/superpowers/specs/2026-11-10-experiment-tooling-design.md

## Global Constraints

- Five edge VMs plus one cloud; operator remains `.35`; no ten-VM rerun.
- Reuse distribution manifests and hashes; NanoFaaS checkout stays read-only.
- Preserve provider/site connectivity, isolated command stdin, journal revival and primary-error precedence.
- Never destroy a VM holding unconfirmed evidence; never alter unrelated resources.
- Work only in the isolated Nanolab and Workbench branches; no generic orchestration framework.

## Review Focus

- Site aliases sharing a private IP must retain separate inventories and archive paths.
- Interrupted/partial deployment must remain compensatable without live acquisition values.
- Dead metrics endpoints must not prevent buffer collection; failed archives must preserve source and VM.
- Ansible installation/Python mismatch must fail before creating infrastructure.
- Reused artifacts must match image IDs/architecture; no scientific health gate may be weakened.

### Task 1: Resolved Ansible targets and shared execution

**Files:** Nanolab `tasks/infra/ansible.py`, `workbench/playbook.py`, `plans/one_shot_common.py`, their tests; existing Sonata Ansible task/executor API.

**Interfaces:** Consumes acquired `VmInfo`, `NodeResource`; produces resolved named inventory and a shared playbook invocation using `AnsiblePlaybookTask`.

- [ ] Add tests for local, Multipass, Azure, Proxmox and jumped hosts (including identical private IPs), missing VM and nonzero playbook exit.
- [ ] Run `pytest packages/nanolab/tests/workbench/test_playbook.py --no-cov`; expect new target tests to fail before implementation.
- [ ] Resolve host/user/port/key/jump directly, write JSON inventory, run the existing Sonata task with stdin isolation. Migrate Workbench and one-shot bootstrap callers; retain legacy planner compatibility only where still required by non-one-shot flows.
- [ ] Run playbook and bootstrap tests; expect all pass. Commit this deliverable.

### Task 2: Runtime bootstrap and reproducible operator preflight

**Files:** Nanolab `tasks/components/bootstrap.py`, `tasks/infra/ansible_assets/playbooks/provision-base.yml`, `workbench/install.py`, `plans/one_shot_common.py`, package metadata/uv lock/CI and corresponding tests.

**Interfaces:** Consumes Task 1 runner; produces runtime-only bootstrap and operator preflight resource that precedes VM acquisition.

- [ ] Add tests that runtime guests omit Java/Buildx/Skopeo/Helm, legacy builders retain them, and a missing operator prerequisite prevents VM acquisition.
- [ ] Run focused bootstrap/install/plan tests; expect new behavior tests to fail.
- [ ] Add minimal explicit capability selection, declare operator runtime dependencies in uv, and order preflight before provisioning.
- [ ] Run focused tests and lock validation; expect pass. Commit.

### Task 3: Ansible runtime distribution and lifecycle

**Files:** Nanolab `plans/one_shot_common.py`, new bundled runtime playbook and focused runtime helper, control-plane lifecycle tests.

**Interfaces:** Consumes Task 1 target/runner and existing distribution/image archive; produces the same control-plane resource value and journal/compensation behavior.

- [ ] Add tests for failed image identity, per-architecture inputs, preserved runtime flags, deploy failure compensation and missing-container cleanup.
- [ ] Run focused control-plane tests; expect new Ansible path tests to fail.
- [ ] Move image copy/load, peer configuration and container start/stop/log capture into Ansible. Keep URL readiness/measurement probes and existing image ID validation.
- [ ] Run focused tests and real Linux Ansible deploy/redeploy/cleanup smoke; expect pass. Commit.

### Task 4: Workbench collection and Nanolab recovery boundary

**Files:** Workbench `roles/log_collector/tasks/collect.yml`, `roles/common/tasks/fetch_dir_tarball.yml`, docs/integration tests; Nanolab `workbench/platform.py`, new `workbench/evidence.py`, logging callers/tests.

**Interfaces:** Consumes Task 1 runner; produces Workbench final collection with verified archive and a focused Nanolab evidence task composed before release.

- [ ] Add real Ansible tests: dead collector still archives buffers; final collection stops before archive; missing source fails without deletion. Add Nanolab tests for archive failure retaining VM and primary error surviving cleanup.
- [ ] Run new tests; expect failures before implementation.
- [ ] Extend existing collect, preserve sources, and move collector recovery orchestration out of platform composition. Remove replaced Docker/SSH archive code only after checking every caller.
- [ ] Run Workbench integration and Nanolab platform/recovery/supervision tests; expect pass. Commit each repository's changes.

### Task 5: Reuse build artifacts and align documentation

**Files:** Existing recipe/one-shot distribution code and tests, Nanolab workflow docs, Workbench README/release tests.

**Interfaces:** Consumes unchanged distribution contracts and Tasks 2–4; produces a documented prepare-once/calibrate/qualify/experiment path and consistent collection/version documentation.

- [ ] Add regression tests proving reused distributions skip compilation and reject wrong archive/architecture; add README/version consistency test.
- [ ] Run focused tests; expect any newly uncovered defects to fail before correction.
- [ ] Reuse existing recipe commands/artifacts, remove redundant export/build work where present, align installation pins and host-qualified evidence paths.
- [ ] Run distribution and documentation contract tests; expect pass. Commit.

### Task 6: Integrated verification and review

**Files:** Integration fixtures/run inputs, evidence receipts and verification report; version pins only if Workbench API release is needed.

**Interfaces:** Consumes all preceding tasks; produces test evidence and an honest functional/scientific outcome.

- [ ] Run targeted fault-injection tests, lint/type checks and complete Linux suite; expect pass with documented platform skips.
- [ ] Run real five-edge-plus-cloud workflow on `.35`, reuse one build, and verify inventory, collection, qualification gates and owned-resource cleanup. Record any external scientific-input blocker without bypassing it.
- [ ] Request one fresh whole-branch review; fix important findings with reproducing tests, record minor deferrals.
- [ ] Record exact results/limitations and commit final review fixes. Integrate only after required checks pass.
