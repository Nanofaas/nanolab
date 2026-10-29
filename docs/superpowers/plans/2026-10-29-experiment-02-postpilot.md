# Experiment 02 bootstrap repairs implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; one fresh final review.

**Goal:** make the approved fresh-host preparation complete without manual fixes.
**Architecture:** existing systemd setup and baseline installer; one native-host command helper using staged source pins and existing remote logging. Published Sonata handles NSGs.
**Tech stack:** Python, shell, existing uv/pytest/command interfaces.
**Spec:** docs/superpowers/specs/2026-10-29-experiment-02-postpilot.md.

## Global constraints

Active pilot74d5758 is immutable. This branch stays local; no new VM, push or merge.
No dependency other than the published Sonata0.6.18 pair, no new quality gates.
Keep registry behavior, experiment01 and source/read-back integrity unchanged.

## Review focus

- Services must become inactive before initial slice changes, without cached old cgroups.
- Native-only preparation uses captured GraalVM pins and passes paths safely.
- Existing wrong GraalVM fails before compilation; installer checksum remains enforced.
- JVM/experiment01/cached publications do not acquire unnecessary native dependencies.
- Fresh FunLess needs Compose; incorrect/error responses still remain raw data.

### Task 1: cold engine transition

Files: tasks/experiment_02/budget.py and its existing test_budget.py.
- [ ] Add regression requiring registry removal and full engine stop before daemon reload/start; observe RED.
- [ ] Move registry removal first, stop docker.socket/docker/containerd, create configuration, daemon-reload and start engines. Keep per-cell restart unchanged.
- [ ] Verify budget tests and commit.

### Task 2: native host prerequisites

Files: new tasks/recipes/native_host.py, comparison/prepare.py and focused tests.
- [ ] Tests: read pins from staged Gradle properties, safe paths, missing pins rejected; execute preparation against fake host tools, repeat without reinstall, reject wrong existing version. Observe RED.
- [ ] Build one root command installing build-essential/zlib and the captured source's pinned Community installer. Log tool versions and hashes. Invoke only for missing experiment02 native publications and set the native command's JAVA_HOME/GRAALVM_HOME.
- [ ] Verify existing recipe/comparison callers and commit.

### Task 3: FunLess Compose prerequisite and final checks

Files: tasks/experiment_02/baselines.py, baseline tests and docs/experiment-02.md.
- [ ] Test fresh preparation makes docker compose available; RED, install Ubuntu docker-compose-v2 in the selected FunLess script, GREEN.
- [ ] Document repairs and published Sonata adoption. Run pre-commit, full Linux pure/real-pin contracts with combined coverage above86%, TUI and one fresh review.
- [ ] Commit; leave this branch/worktree ready for review, preserving the active pilot branch.
