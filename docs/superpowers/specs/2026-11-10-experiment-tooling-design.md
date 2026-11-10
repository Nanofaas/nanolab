# Uniform experiment tooling

The operator is the existing Linux `.35` server. The intended topology is five edge VMs plus one cloud VM. OpenTofu and the Multipass provider own VM provisioning; Ansible owns guest configuration, image distribution and container lifecycle; Sonata owns scheduling, compensation and the resource journal. Python retains experiment algorithms, HTTP probes and time-sensitive supervision.

Use existing APIs and formats. Do not add a registry, a dedicated operator VM, a second scheduler or a generic recovery framework. Keep NanoFaaS sources immutable. Preserve compatibility for bootstrap consumers that still build software inside their guests.

## Responsibilities and approach

Resolve connection facts once from acquired VM values, including provider endpoint, SSH port/key and site jump. Write named Ansible inventory directly; do not construct a command and parse it back into connection data. Run playbooks through Sonata's Ansible task, preserving isolated stdin and actionable failures.

Use an explicit runtime bootstrap profile for one-shot guests. Build tools remain available to legacy builder guests. Declare the operator's Ansible/Python requirements in the uv-managed installation and check prerequisites/collection installation before VM acquisition.

Build the runtime once and reuse the existing distribution manifest, image IDs and hash-verified archives for calibration, qualification and experiment. Document executable commands and validate the reused inputs. Do not introduce another artifact format.

Technical evidence collection belongs to Workbench's existing collect action. Extend collection to attempt independent channels even when metrics fail, optionally quiesce a log collector before a final archive, verify transfer and retain source data. Nanolab owns the collector evidence task, delivery/loss assessment and VM retention on unconfirmed recovery. Sonata's composite collects before releasing members and preserves primary errors. A new catch-all component combining scientific decisions, filesystem copying and scheduling would obscure ownership; extending existing collect and a focused Nanolab task is smaller.

Keep campaign finalization and signal coordination in the existing supervisor unless a self-contained extraction demonstrably simplifies them. Preserve all error prioritization, writer shutdown checks, archive ownership and checksums. No automatic deletion of unverified evidence or unrelated infrastructure.

## Acceptance

Unit/contract tests cover cloud/site connection facts, runtime-only bootstrap, failed acquisition, cleanup errors, collection with a dead service, archive failure and preserved source/VM. Use real Ansible/Docker on Linux for collection and deployment; run a six-VM (five edge plus cloud) functional workflow after targeted tests, with calibration, qualification and experiment distinguished. Scientific acceptance requires compatible verified inputs and healthy telemetry; a failed gate must be reported, never bypassed.

Alternatives considered: keeping Python Docker/SSH wrappers preserves current complexity; moving all polling to Ansible adds overhead and changes measurement timing. The selected boundary centralizes configuration and lifecycle while retaining the existing measurement path.

## Separate architectural views

The experiment data flow, resource lifecycle and deployment topology are distinct.
A diagram must not make tools, machines and scientific stages siblings.

Experiment data flow:

```text
prepared images ──────────────────────────────────────────┐
                                                        │
calibration ── profile ── qualification ── timing ── experiment
                                                    baseline / oracle / ewma
```

Each stage owns a resource lifecycle: acquire infrastructure, configure/deploy,
run measurements, then quiesce/collect/verify/release on success, failure or
interruption. Evidence recovery is part of finalization, not a scientific stage
that only executes after successful measurements. Unverified raw recovery blocks
fleet destruction; scientific invalidity and raw recovery are separate decisions.

Component responsibilities:

```text
Nanolab (experiment composition, measurements, validity, retention policy)
    └── Sonata (dependencies, resource ownership, compensation)
          ├── OpenTofu task → Multipass provider → VM lifecycle
          ├── Ansible task
          │     ├── Nanolab playbooks → NanoFaaS runtime lifecycle
          │     └── Workbench roles → platform components and technical evidence
          └── Nanolab tasks → NanoFaaS APIs, measurements, evaluation
```

Workbench supplies reusable technical capabilities; it does not decide scientific
acceptance or VM retention. Nanolab's collector-evidence task composes collection
and applies that policy. The operator host and five-edge-plus-cloud layout form
a separate deployment view, not another layer in the experiment task graph.
