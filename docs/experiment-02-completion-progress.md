# Experiment 02 completion progress — 28 October 2026

Plan: nanofaas-paper/notes/experiment-02-completion-prompt.md.

Ruling: derive the isolated completion branch from 7da834b, preserving completed review work without merging main. The author requested implementation; no push or merge is implied.

Baseline: 133 experiment-02 tests passed before changes.

Minor 1: failing regression proved build_workloads lacked the recorded-commit argument. Build and ensure now accept and propagate the identity commit from application orchestration. A real temporary Git repository advances HEAD; the transferred archive still contains the recorded source.
Minor 2: failing regression proved package metadata was not refreshed. tinyFaaS installation now updates apt before installing the toolchain.
Focused verification: 23 regression/install tests passed after both fixes; final experiment-02 task and CLI selection: 145 passed. pre-commit passed, basedpyright reported zero errors/warnings. macOS full suite: 4641 passed, 391 failed, 45 errors, 28 skipped; Linux-pidfd restrictions are one confirmed cause. Linux disposable-container suite and real-source contracts remain in progress; no complete-suite or coverage success claimed. A fresh-context reviewer found no important issues in the diff and plans.

Ruling: minor 3 needs no extra prefetch. At tinyFaaS eff00d060f6f6e96bf8f35475a0e9a8e6e42de32, Makefile's manager build depends on runtimes for the architecture; their blob.tar.gz targets run Docker builds, export the filesystems, and embed them in the manager. binary/Dockerfile starts FROM scratch and adds that embedded blob. The Go/Alpine base downloads therefore occur during installation, before cells. Source inspected from the official Git repository; no third-party code executed locally. Confirm on the next fresh Azure install.

Azure read-only inventory: 24 matching resources, zero nanofaas VMs, five unattached disks and five unattached NICs. NSGs reference subnets; deleting NSGs before their VNets would fail unless associations are explicitly removed. Never delete a shared resource. No resource deleted or created. Detailed inventory and approval proposal belong in the paper notes.

Plan 3 implementation approved by the author in chat.

Pending: author approval for explicit Azure deletions; plan 3 VM proposal; plan 4 implementation approval; later frozen campaign approval. Fn trigger errors, tinyFaaS process names and disk growth require Azure evidence. No campaign or paper result produced.
