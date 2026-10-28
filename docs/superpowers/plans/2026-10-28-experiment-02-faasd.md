# Experiment 02 — plan 3: faasd

Status: implementation approved by the author in chat on 28 October 2026. Native containment probe completed on an approved Spot VM, now deallocated. Full three-role Spot smoke separately approved; implementation remains pending that integration acceptance.
Base: experiment-02/completion-minors, derived from review commit 7da834b.
Authority: nanofaas-paper/notes/experiment-02-platform-comparison-prompt.md.

## Contract

Run the existing shared Rust QR implementation through the existing classic-watchdog wrapper, paired with nanoFaaS P1. Use a separate containerd-only Azure VM matching the stack SKU and exact image version. Include faasd, provider, containerd, gateway, supporting services and function containers in one enforced CPU/memory budget with swap disabled. Only measurement agents and the remote generator are excluded. Record backend differences and all effective cgroup paths/limits.

## Tasks

1. Characterize current Azure role validation and provisioning with failing tests. Extend the existing application/provisioning and experiment-02 orchestration to accept the faasd role only when selected; preserve stack/loadgen requirements. Keep presentation dependencies outside application. Verify existing configuration/provisioning contracts and commit.
2. Characterize installation, identity and resume using RecordingProvider. Add the faasd adapter beside existing baselines, using immutable verified release identities, the paper wrapper and the recorded workload commit. Verify applicable licence terms and precise deadline before use. Keep credentials ephemeral and exclude them from persisted commands, configs and logs. Reuse existing installation identity/drift checks; commit after tests pass.
3. Characterize containerd budget enforcement and evidence parsing, including an escaped required process and ancestor limits. Implement with native systemd/containerd facilities. Include every required service, including the local registry if required at runtime, in accounting. Verify all PID paths and effective limits; fail closed if enforcement cannot be proven. No automatic protocol exception. Commit after tests pass.
4. Prepare a concrete Azure proposal: role names, SKU/image/region, ownership, estimated hours/cost and explicit resource actions. Obtain author confirmation before creation. Build and test only on Azure. Render the canonical payload and compare the complete measured PNG SHA-256 from workload.py. Exercise failure responses, process accounting and secret-free evidence. Verify resume skips completed cells while allowing identity checks and necessary idempotent restoration. Use a new run directory after any code change.
5. Run repository checks and source contracts against the real CI pin; combined coverage must exceed 86%. Obtain one fresh-context review of the whole branch. Record commits, run directories, exclusions and remaining resources. No push or merge without author instruction.

## Acceptance

All selected cells use the same workload commit and canonical payload; correct PNG; all required processes within budget; no persisted credentials; no repeated completed cells; pure and source-contract suites pass. An unavailable or unenforceable platform is reported explicitly, without fabricated measurements.
