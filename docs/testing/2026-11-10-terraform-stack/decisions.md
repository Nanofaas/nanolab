# Decisioni registrate e rilievi rinviati

Estratto completo dei due registri di esecuzione, in ordine interno a ciascun piano.

## Revisione iniziale

- Ruling: task IDs are S1/N1/etc rather than numeric Task N, and work spans three repositories; track briefs by original headings and commits/tests in this ledger instead of numeric task-start/task-done parsers. Same completion evidence required.

- Ruling: native worktree tool targets only Nanolab; sibling repos use git worktrees in /private/tmp. Originals remain unchanged.

- Ruling: local host has substantial compression/swap pressure; start Linux runner with 4 CPU/4 GiB/40 GiB, execute suites serially; expand or use Azure only if needed. Risk: kind may require more memory, never infer scientific performance from this VM.

## Terraform e collaudo

- Ruling: user approved design and explicitly said proceed with native implementation; execute this concrete plan without another approval round.

- Ruling: default stack parallelism is 1, exposed explicitly in spec, because the provider documents an actual Multipass 1.17 ARM64 concurrent-launch AppArmor race. No host security changes; cost is slower fleet creation.

- Ruling: consolidate provider smoke and Nanolab minimum topology into three VMs (2+1+1 vCPU, 8+2+2 GiB RAM, 32+8+8 GiB disks); OneShotConfig requires >=3 nodes. Fits .35 storage admission; avoids a separate disposable two-VM launch.

- Ruling: Linux tar copies contained macOS AppleDouble metadata and lacked valid worktree Git metadata. Remove only AppleDouble files from owned copies, disable metadata on future transfers, initialize isolated candidate Git snapshot; these are runner preparation corrections, not product changes. Cost: snapshot commit is test-only, original implementation commits recorded separately.

- Ruling: candidate install tests hardcode released Workbench0.1.1; adapt only isolated runner fixtures to candidate0.2.0 along with requirements, leaving public pins unchanged until publication. Cost: public-artifact smoke must be rerun after release.

- Final: minor (deferred): Workbench Prometheus/Supervisor READMEs retain obsolete host-network statements beside new pod-network instructions; fix before release documentation publication.

- Final: Ruling: final gate results are evidence work, not code-review findings; complete them before claiming success. Cost if wrong: unobserved integration regressions remain.

- Final: Ruling: ten-VM scientific acceptance remains a separate unexecuted gate without compatible freeze/reference and adequate storage; no inference from small fleet. Cost: scientific qualification is not established.

- Final: Ruling: keep publication and public pins unchanged until explicit release authorization; isolated candidate install verifies compatibility. Cost: new OpenTofu provisioning is unavailable with released Sonata0.7.0.

- Final: Ruling: remote/cloud backend init side effects are outside this local-state-only feature; retain restriction and add real local custom-backend negative test, no remote state service mutation. Cost: generic CLI operations used explicitly with external backends require their own integration tests.

- Final: Ruling: provider-internal changes remain outside the three-repository remediation; validate pinned signed provider behavior with actual fleet and fault tests. Cost: unseen provider defects remain possible.

- Ruling: use bounded workflow-validation calibration with3VMs1CPU/2GiB/8GiB each to exercise real one-shot application provisioning; it is a functional integration test, not scientific workload qualification. Cost: it cannot validate the missing frozen10VM experiment or reference-comparison acceptance.

- Ruling: preserve native worktrees/branches and candidate artifacts; no integration/publication requested, so no push/merge or public-pin change. This completes implementation without inferring release authorization.

- Final: Ruling: calibration supervisor fault is a new open review finding, outside the approved ten-fix pass; preserve raw status and label49step run functional only, not scientifically qualified. Cost: calibration still publishes despite a supervisor fault until a dedicated health-policy correction.

- Ruling: recovery SSH wrapper is isolated to explicit cleanup after verified evidence export, not shipped behavior; default fresh-connection provider instability remains documented. Cost: operators may need explicit recovery under the same network condition.
