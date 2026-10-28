# Experiment 02 completion progress — 28 October 2026

Plan: nanofaas-paper/notes/experiment-02-completion-prompt.md.
Earlier entries are historical; the final integration section records current status.

Ruling: derive the isolated completion branch from 7da834b, preserving completed review work without merging main. The author requested implementation; no push or merge is implied.

Baseline: 133 experiment-02 tests passed before changes.

Minor 1: failing regression proved build_workloads lacked the recorded-commit argument. Build and ensure now accept and propagate the identity commit from application orchestration. A real temporary Git repository advances HEAD; the transferred archive still contains the recorded source.
Minor 2: failing regression proved package metadata was not refreshed. tinyFaaS installation now updates apt before installing the toolchain.
Focused verification: 23 regression/install tests passed after both fixes; final experiment-02 task and CLI selection: 145 passed. pre-commit passed, basedpyright reported zero errors/warnings. macOS full suite: 4641 passed, 391 failed, 45 errors, 28 skipped; Linux-pidfd restrictions are one confirmed cause. Linux disposable-container suite and real-source contracts remain in progress; no complete-suite or coverage success claimed. A fresh-context reviewer found no important issues in the diff and plans.

Ruling: minor 3 needs no extra prefetch. At tinyFaaS eff00d060f6f6e96bf8f35475a0e9a8e6e42de32, Makefile's manager build depends on runtimes for the architecture; their blob.tar.gz targets run Docker builds, export the filesystems, and embed them in the manager. binary/Dockerfile starts FROM scratch and adds that embedded blob. The Go/Alpine base downloads therefore occur during installation, before cells. Source inspected from the official Git repository; no third-party code executed locally. Confirm on the next fresh Azure install.

Azure read-only inventory: 24 matching resources, zero nanofaas VMs, five unattached disks and five unattached NICs. NSGs reference subnets; deleting NSGs before their VNets would fail unless associations are explicitly removed. Never delete a shared resource. No resource deleted or created. Detailed inventory and approval proposal belong in the paper notes.

Plan 3 implementation approved by the author in chat.

Pending: author approval for explicit Azure deletions; plan 3 VM proposal; plan 4 implementation approval; later frozen campaign approval. Fn trigger errors, tinyFaaS process names and disk growth require Azure evidence. No campaign or paper result produced.

## Plan 3 implementation

Branch: experiment-02/faasd, based on minor-fix commit 2c2a815.
Role/provisioning, native installation, image transfer, cell lifecycle, metrics and resume fingerprints implemented. Final focused run: 195 passed. Pre-commit, types, import contracts and security checks passed including new staged files.

Fresh reviewer findings: native container counts, CNI cleanup and runtime identity coverage. All reproduced with failing tests and corrected. An additional failing shell test demonstrated masked ctr inventory errors; command substitution now propagates them.

Ruling: use faasd's official CNI-aware API deletion before stopping services, then refuse subsequent cells if cleanup/API/CNI state remains. Do not implement CNI DEL by hand or delete task metadata prematurely. This preserves valid measurement boundaries and diagnostic state if recovery requires operator intervention.
Ruling: keep faasd explicit rather than silently adding a third VM to the ten existing default configurations.
Ruling: native engine-only metrics use containerd.service/daemon, not its parent containing platform tasks. Full deployment remains the parent SUT slice.

Manual Azure Spot probe: private mount/cgroup namespaces successfully contain services, shims and openfaas/openfaas-fn tasks under 2 CPU / 2 GiB / no swap. This is not a QR correctness result. VM deallocation verified.

All 24 historical Azure resource IDs were deleted after explicit author approval; the separate Spot VM resources were retained. One VNet delete required a successful retry.

Full Linux pure tests passed before final review corrections (5070 passed, 37 skipped); local macOS is unsupported for Linux pidfd cases. Combined verification is still being completed with real pinned-source contracts. Test-container corrections: install Node/system Python/util-linux, bind temporary files outside the limited Docker disk, and copy the real NanoLab Git history rather than using a source-only directory. No fake nanoFaaS checkout used.

Full QR smoke approved in chat: two new Spot VMs plus reactivation of the existing faasd Spot VM, bounded two-hour allocation and explicit price ceilings; all three must be deallocated afterward. Campaign remains unapproved.


## Final integration and SDK update

Sonata 0.6.17 (f4b5da8) and tag v0.6.17 were published after explicit author
approval; release workflow 40728067449 succeeded. Linux engine/catalogue:
227 + 770 tests passed. NanoLab eaf7391 pins the published pair and SDK 0.8.0;
199 targeted tests and all pre-commit checks passed.

Explicit existing Azure hosts use verified SSH/SCP without OpenTofu lifecycle
adoption (7c2219b). Pre-authentication KEX failures exposed connection churn:
bounded retries and private native OpenSSH multiplexing were added with
regression tests (8ab5f9e, 38e5cae, 2d9a23a); 23 provider/CLI tests passed.
No remote command failure, timeout or authentication error is retried.

The first full QR cell at 2d9a23a was correctly discarded on DELETE cleanup
failure. HTTP deletion had completed after the old two-second timeout, so
retries returned 404. Regression tests and fix 5ac285e allow a longer timeout
and idempotent absence only with zero task/container/CNI postconditions.
158 experiment task tests and all pre-commit checks passed.

Accepted smoke: runs/experiment-02-faasd-smoke-20261028T1748 at 5ac285e.
One cell measured, complete canonical PNG hash verified independently; all six
task PIDs and host faasd/containerd processes in the SUT; effective budget
2 CPU / 2048 MiB. Resume skipped the cell and preserved 21 files and mtimes.
An extra empty-text request returned HTTP 500; post-probe cleanup passed.
No password or Basic-auth encoding found in the checked run/paper evidence.
All three x86_64 hosts used kernel 6.17.0-1020-azure/systemd 255 with NTP synced;
faasd and loadgen had no Docker executable.

All three Spot VMs were deallocated before 18:02 UTC; disks, public IPs,
networks and shutdown schedules are retained, not authorized for deletion.
The 24 historical approved IDs remain deleted. No campaign executed and no
NanoLab push/merge. Remaining Azure minor checks: Fn HTTP-trigger error status,
tinyFaaS process-name evidence and fresh-install prefetch confirmation; monitor
disk growth during the future approved pilot/campaign.

Final whole-branch fresh-context review of 7da834b..5ac285e found no actionable
correctness/security/measurement issues. Reviewer left actual smoke, resume and
secret acceptance to the primary; these were independently verified above.
Final repository verification of code 5ac285e: Linux pure 5109 passed, 37 skipped, 20 subtests passed; real pinned-source contracts 644 passed, 1 skipped; TUI toolkit 53 passed (94.84% coverage); combined NanoLab branch coverage 87.55%, exceeding 86%. Pre-commit, types, import contracts and security checks passed. The five initial failures came from missing /usr/bin/python3 in the disposable test container; all five passed unchanged after installing system Python, then the entire pure suite passed. The pure-only coverage exit is expected before adding source contracts; the independent combined-coverage gate exits zero.
