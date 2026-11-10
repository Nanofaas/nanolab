# SDD ledger — plan: docs/superpowers/plans/2026-11-10-review-remediation.md
Native execution, 2026-11-10. Plan read in full; original design read during review.
Roots: Nanolab /Users/micheleciavotta/.codex/worktrees/c5a7/nanolab (742c0d39); Sonata /private/tmp/nl-review-sonata (3a08cc6); Workbench /private/tmp/nl-review-workbench (150037d). All on codex/review-remediation.
Pre-flight W1/N4: archive format producer and consumer change together; candidate installation must not reinstall released pin.
Pre-flight S1/N1: callbacks protect resource ownership; node.recovery protects raw evidence before VM deletion. No interface conflict.
Ruling: task IDs are S1/N1/etc rather than numeric Task N, and work spans three repositories; track briefs by original headings and commits/tests in this ledger instead of numeric task-start/task-done parsers. Same completion evidence required.
Ruling: native worktree tool targets only Nanolab; sibling repos use git worktrees in /private/tmp. Originals remain unchanged.
S1 started: baseline 61 passed (compose, resource_cleanup, steps_resume). Evidence /private/tmp/nl-review-evidence.
Task S1: complete (71bc2d6; 4 new tests RED→GREEN; engine suite 266 passed, ruff check/format passed). Callback supports nested resources and None values; cleanup failures remain notes.
Ruling: local host has substantial compression/swap pressure; start Linux runner with 4 CPU/4 GiB/40 GiB, execute suites serially; expand or use Azure only if needed. Risk: kind may require more memory, never infer scientific performance from this VM.
Task N1 started: review real _owned_node guard and both collector teardown paths.
Task S2: complete (36 focused tests, 782 catalog passed/30 platform skips; five RED→GREEN cases cover stable and changed fingerprints plus rejected resume). Initial combined suite command caused duplicate test module import; reran catalog using its own pytest config.
Ruling N2: pass cpuset through the existing loadgen partial and use platform.loadgen in experiment; do not inject full settings into calibration/qualification because it activates logical schedule timing. Existing settings caller still derives placement. Cost if wrong: a direct caller omitting both placement/settings remains unconstrained.
Task N1: implementation committed 663a6d0d, targeted failures RED→GREEN; Linux validation passed in combined run; all remaining failures in broader plans were missing node executable.
Task N2: implementation and caller fixtures ready; RED 6 failures, workbench 101 passed; Linux placement tests passed. Broader plan rerun installing Node.js.
Task N1: complete (663a6d0d). Linux N1/N2/workbench/plans regression group: 653 passed,154 preexisting skips; logging/resources/placement group no platform failure.
Task N2: complete (commit below in history). Same Linux group 653 passed; ruff clean. VM real affinity check remains V1.
Task S2 commit: 5193036. Resume compatibility note in README.
Task W1: RED Linux 6 failed/3 passed; namespaced host path implemented; GREEN running.
Task W1: complete (commit in workbench history): 6 RED failures→9 Linux Docker tests passed, including sequential and same-invocation multihost tarballs and rejected traversal. Stable alias path also applied to all existing integration assertions and docs. No Docker skip.
Task W2: implementation committed 6533aa1, role RED4→GREEN31 chart/role tests. Kind service-client verification prepared for V1.
Task W3: implementation committed (history), chart RED2 and role RED3→GREEN35 chart/role tests. Mandatory node, no controller hostPath creation, Running/Succeeded/Failed pod phase verification even on existing active jobs. Real two-node kind tests running in V1.
Task W4: complete (history), RED4→GREEN80 unit/HTTP tests. Malformed types rejected before lookup; no broad exception catch.
Task N3: complete 3bb4dc31. RED4→GREEN13 vm_export; Linux full required group118 passed. Mandatory names emit errors even if never observed; current ID per name prevents false missing-old-ID after recreation. Mac full group cannot read /proc boot_id, Linux successful.
Task N4: complete (history): RED13→GREEN113 workbench tests. Inventory aliases validated; retargeted provider argv/env preserved, only inline inventory replaced with JSON alias->ansible_host. Azure/Proxmox port/key and same-private-IP multisite tested. Candidate collection real install in V1 pending.
Task N5: complete (history): RED5→GREEN14 compare tests. All recognized evidence files keyed by relative path, coverage keyed path+node; empty metrics and memory medians checked per file. Test expectation messages updated to explicit paths.
Ruling N5: evidence schema comparisons use exact relative files, including supervisor status. Adding/removing a recognized evidence file is now reported, even with empty facets; prevents absence being hidden. Cost: historical migration comparisons explicitly report new supervisor artifacts rather than silently union them.
V1 kind first run: loadgen two-node happy path/retry and missing directory Pending failure passed; Service test failed on immediate DNS resolution (wget bad address wb-pr), diagnosing startup/DNS before claiming W2 complete.

V1 continuation: Sonata final engine suite after 0.7.1 dunder correction passed locally (JUnit sonata-engine-final.xml); Workbench ruff, format and bandit passed. Linux full suites remain in sessions 66337 and 6748; no completion inferred.
Infrastructure: SSH michele@149.132.179.35 works; Linux ARM64, 20 logical CPUs, 121 GiB total/115 GiB available memory, 80 GiB free on root, OpenTofu 1.13.9, Multipass 1.17.0, Docker 29.6.2. No resources created on server. Existing sampled freeze has six nodes and no environment.yaml; not the required ten-VM acceptance input.
Infrastructure blocker: local Multipass exec/transfer return No route to host for both owned VMs although direct TCP/22 and ping succeed; VM processes and original test sessions remain alive. No restart/delete performed; raw evidence remains in VMs.
Scope clarification: user supplied https://github.com/Nanofaas/terraform-provider-multipass and means a generic Sonata Terraform/OpenTofu task. Read provider and Helm/CommandTask patterns; pending lifecycle preference before designing this new feature. Provider supports workstation OpenTofu + SSH remote Multipass and ARM64 builds; README says registry registration pending. Clone is /private/tmp/nl-review-terraform-provider-multipass.
