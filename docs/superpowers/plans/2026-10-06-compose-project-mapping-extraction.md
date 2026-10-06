# Compose pre-clean and project directory mapping (#61, fifth slice)

> Execute inline with `superpowers:executing-plans`; obtain ONE fresh whole-slice review after implementation.

**Goal:** Remove NanoLab's duplicate Compose lifecycle and project-cwd translation by extending existing Sonata APIs.

**Spec:** `../specs/2026-10-06-sonata-extraction-assessment.md`, individual Compose and remote-project decisions.

**Architecture:** Add opt-in pre-clean to `docker_compose_resource`, using existing teardown flags and compensation. Add optional paired project roots to `VmCommandTaskExecutor`; translate cwd locally before invoking its runner. NanoLab keeps project role/env and destructive fresh-project choices, provider assembly, remote-home defaults and `/nanofaas` selection.

**Tech stack:** Python3.12, existing execution ports/resources, pathlib/dataclasses and the existing semantic-key helper. No new runtime dependencies or engine API.

## Global constraints

- Baselines: NanoLab7aed4c3, Sonata13921ec, public pair0.6.9. Isolated worktrees only; operator NanoFaaS untouched, test pin `/tmp/nanolab-61-pinned` at e7914be.
- Preserve default Compose deploy→wait→teardown and VM rejection of cwd unless mapping is explicitly configured. Preserve roles, env, expected exit codes, timeout rejection, dry-run forwarding and task results.
- `pre_clean=False` by default; pre-clean uses the caller's teardown flags. NanoLab opts in with volumes/orphans removal and retains its existing clear/deploy/readiness/compensation ordering, titles and dependencies.
- Preserve project subclass identity in the shared resource's generic return; no casts or new policy type in Sonata.
- Configure `local_root` and `remote_root` together or neither. Local paths are resolved and checked against the resolved root, including symlinks and nonexistent subpaths. Conflicting cwd/remote_dir fails before the runner; no cwd preserves the delegate's remote_dir/default semantics.
- Remote roots remain explicit nonempty POSIX paths, including relative roots compatible with existing NanoLab configuration. Backend resolves relative roots; no remote filesystem inspection or cwd expansion is introduced.
- Binding identity must retain the configured target AND mapping roots. Mapping/root changes invalidate task fingerprints rather than reuse work in another directory. Existing unconfigured VM target keys remain byte-identical.
- Prepare coherent0.6.10 only after checking PyPI availability. Consumer pins/lock stay on public0.6.9 during the explicit built-wheel trial. Publication requires separate authorization after Sonata merge.
- Keep coverage gates unchanged; report the established NanoLab86.20% versus90% deficit separately from functional failures.

## Review focus

- Pre-clean/down, deploy and readiness failures preserve the primary error and cleanup ordering; no deployment after failed pre-clean.
- Compose defaults never remove preexisting state during acquisition; opt-in teardown flags and target namespace remain explicit.
- Symlink escapes, relative cwd and cwd/remote_dir conflicts reject before backend effects; remote relative roots preserve backend semantics.
- Mapping config affects binding/task identity, including root changes for an unchanged target, while unconfigured executors keep their keys/options behavior.
- Consumer adapters retain subclass identity, dependency ordering, provider defaults and recipe-specific lifecycle guards.

## Task 1: Extend existing Sonata APIs

**Files:** compose.py/test_compose.py, execution/adapters.py/test_adapters.py, README/catalogue and coordinated version sites/lock.

**Interfaces:** `docker_compose_resource[T: DockerComposeProject](project:T,...,pre_clean:bool=False)->Resource[T]`. `VmCommandTaskExecutor(runner,*,target_key:str,local_root:Path|None=None,remote_root:str|None=None)`.

- [x] Baseline focused Compose/adapters/bindings and consumer mapping/lifecycle groups. Expected: no functional failures.
- [x] Write missing-option RED tests for pre-clean ordering/flags, failure/compensation, original defaults, generic project identity; cwd mapping, escapes, paired config, preserved options/dry-run, relative roots and changed mapping fingerprints. Run focused tests. Expected: new options unsupported.
- [x] Implement opt-in behavior using existing steps/teardown, VM adapter and semantic-key helper. Run focused tests. Expected: GREEN, old cases preserved.
- [x] Check PyPI0.6.10 unused; coordinate package versions/lock, document neutral ordinary examples and build wheels/sdists. Run catalogue/engine with explicit coverage configs, required hooks and six wheel installations. Expected: unchanged gates pass.
- [x] Exercise installed wheels in an ordinary Git-independent application: mapped remote directory through a concrete runner and an isolated opt-in Compose lifecycle, including real deployment/readiness/teardown when Docker is available. Only uniquely named disposable resources may be touched.
- [x] Commit shared API changes. Expected: concrete reviewable diff.

## Task 2: Thin NanoLab policy adapters and delivery

**Files:** tasks/compose.py, application/execution.py, tests/tasks/test_compose.py, tests/cli/test_execution_bindings.py, assessment and this plan.

**Interfaces:** Keep `isolated_compose_resource(project,*,executor,cwd=None,requires=())`; delegate with pre_clean=True, project.role/env and both teardown flags. Keep `build_role_bindings` public signature; pass explicit project roots when constructing VM executors in SSH/provider paths, remove the private translation wrapper.

- [x] Add a RED public mapping fingerprint regression and consumer lifecycle/dependency/identity tests. Expected: changing only remote_project_root currently leaves fingerprint unchanged; Compose lifecycle cases characterize existing consumer policy during delegation.
- [x] Trial exact built0.6.10 wheel pair only in isolated venv; retain public manifest/lock0.6.9 and use direct venv commands/UV_NO_SYNC=1 for hooks. Replace mechanics with shared APIs. Run focused cases. Expected: GREEN, provider default cwd/env and fresh project policy preserved.
- [x] Run full NanoLab with explicit coverage configuration against isolated CI pin, toolkit/hooks and installed product smoke. Expected: all functional cases pass; existing coverage deficit remains separately recorded.
- [x] Commit the local consumer trial and obtain ONE fresh whole-slice review. Regrade findings; reproduce/fix Important/Critical items RED→GREEN in one pass, run affected full suites/checks. Record every ruling and deferred minor.
- [x] Push reviewed Sonata branch and open PR; preserve consumer trial locally pending publication. Expected: public shared PR with verifiable evidence; no unpublished consumer dependency declarations.

## Decisions

- Ruling: Extend existing Compose/VM APIs instead of exporting another wrapper — both capabilities fit current lifecycle/execution ports and remove duplicate mechanics — cost if wrong: two optional VM constructor fields and one Compose flag to maintain.
- Ruling: Include mapping roots in configured VM binding identity — current translation can reuse fingerprints after remote-root changes — cost if wrong: existing NanoLab remote command fingerprints invalidate once and may rerun work.
- Ruling: Accept explicit nonempty relative POSIX remote roots — existing environment home/root configuration already permits them — cost if wrong: relative-root resolution remains backend-dependent, with no remote canonicalization guarantee.
- Pre-flight: Task1's pre_clean and paired-root interfaces are consumed by Task2; generic project typing must preserve NanoLab's subclass, and default_dir/home policy stays solely in consumer runners.

Task 2: Ruling: Keep consumer Compose lifecycle tests as characterization — extraction preserves behavior that already exists; new shared options and the consumer mapping fingerprint have genuine RED→GREEN regressions — cost if wrong: delegation itself is checked through behavior and types rather than a mock asserting the chosen implementation.

## Review and verification

One fresh whole-slice reviewer found no Critical, Important or Minor issues;
37 focused reviewer cases passed. No fix or re-review pass was needed.

Final: Ruling: Preserve best_effort's existing treatment of cleanup programming errors — operational cleanup failures retain primary errors, while changing programming-error propagation is a separate shared policy change — cost if wrong: a cleanup programming error may replace an acquisition error.
Final: Ruling: Leave configuration captured by resource callbacks under existing ResourceOp semantics — command binding identity is corrected here and no engine API is justified by this extraction — cost if wrong: hidden callback configuration changes may escape fingerprint invalidation.
Final: Ruling: Fingerprint declared mapping configuration, not mutable symlink targets — execution still resolves and rejects local containment escapes; filesystem-state evidence remains caller-owned — cost if wrong: changed symlink targets may not invalidate otherwise identical command fingerprints.
Final: Ruling: Leave remote canonicalization/symlink containment to the injected backend — the adapter translates explicit POSIX roots without inspecting remote state, preserving documented relative-root compatibility — cost if wrong: remote symlinks or backend-relative resolution can select another physical directory.
Final: Ruling: Compose namespace ownership remains caller policy — pre-clean is explicitly opt-in and the existing lifecycle is extended without introducing an ownership registry — cost if wrong: choosing a shared name with removal flags can delete another caller's state.
Final: Ruling: Keep NanoLab's 0.6.10 use as an explicit local wheel trial — publication requires separate authorization and public pins/lock must change only after index availability — cost if wrong: normal installation of this provisional consumer branch cannot use the new APIs until pins are updated.

Deferred minors: none.

Full trial: 3451 passed in 301.23s, original branch-coverage gate86.19%/90%
(previous86.20%). Shared catalogue561/91.37%, engine227/96.17%, all hooks,
six wheel configurations, ordinary real Compose/mapping probe, toolkit51,
installed CLI/assets and focused consumer60 passed.


## Delivery

Sonata commit `93844d1898dc2496cec00fe10022aa5984ffa95f` is pushed in
[PR #20](https://github.com/Nanofaas/sonata/pull/20). The consumer implementation
is local on `feat/61-compose-project-mapping` (`3c65264` plus evidence updates),
with all public dependency declarations/lock still on 0.6.9. Publishing0.6.10
requires separate user authorization after merge; public-index pin/lock adoption
and its NanoLab PR follow publication. The current task does not publish or merge.

CI for Sonata PR #20 at `93844d1`: all 18 checks succeeded (push and PR runs),
including isolated engine, catalogue, hooks and six wheel configurations.

Execution ledger completion records:

Task 1: complete (commits 7aed4c3..7aed4c3, tests: bash /tmp/verify-compose-mapping-shared.sh → 227 passed in 1.33s)
Task 2: complete (commits 7aed4c3..3c65264, tests: bash /tmp/verify-compose-mapping-consumer.sh → Trial verified: 60 focused + 3451 full functional passes; original coverage gate remains unmet at 86.19%/90%; toolkit51 and hooks pass)
