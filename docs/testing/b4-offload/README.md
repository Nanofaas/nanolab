# B4 preparation verification — 2026-11-01

Implementation base: NanoLab `85b7c479d7b665e9e24c9344ce2ad64bf31510d9`.
Source contract checked against nanoFaaS `b1cb1608f414df87fb039783aa8ecd6715b7db8e`.
No nanoFaaS changes were required. No VMs were launched.

From this NanoLab checkout, with its Python environment active and
`NANOFAAS_ROOT` pointing to that source checkout:

```sh
export PYTHONPATH="$PWD/packages/nanolab/src"
python -m pytest -c packages/nanolab/pyproject.toml --no-cov \
  packages/nanolab/tests/config \
  packages/nanolab/tests/tasks/provisioning \
  packages/nanolab/tests/cli/test_execution_bindings.py \
  packages/nanolab/tests/cli/test_vm_provider.py \
  packages/nanolab/tests/test_provisioning.py \
  packages/nanolab/tests/tasks/loadtest/test_offload_conservation.py \
  packages/nanolab/tests/tasks/loadtest/test_offload_loadtest.py \
  packages/nanolab/tests/release/test_environment.py \
  packages/nanolab/tests/plans/test_offload_loadtest.py \
  packages/nanolab/tests/plans/test_offload_experiment.py \
  packages/nanolab/tests/tasks/test_offload_experiment.py \
  packages/nanolab/tests/tasks/test_offload_experiment_report.py \
  packages/nanolab/tests/offload -q
NANOLAB_K6_TEST=1 python -m pytest -c packages/nanolab/pyproject.toml --no-cov \
  packages/nanolab/tests/tasks/test_offload_experiment_k6.py -q
```

The first command passed **562 tests**. The opt-in test passed with real k6
against a temporary loopback server in **43.19 s**. It uses the real medium
corpus, exact output oracle and target-URL offload header. It verifies ledger
parsing, both phases, tagged drop counters and global iteration reconciliation.
The loopback test needs socket-bind permission; the macOS sandbox denies that
permission, so it was run outside that sandbox. It creates no cloud resources.

All changed Python files passed Ruff. All changed production Python modules
outside bundled remote assets passed basedpyright with the installed NanoLab
Python environment; `git diff --check` passed. The preparer passed its CLI smoke,
producing nine configuration pairs accepted by the real validators, and its
commands were tested against a fake executable for quoting and stop-on-failure.

An earlier full-package attempt on macOS in the restricted sandbox was **not
clean**: 5734 passed, 413 failed, 87 errors, 14 skipped. Many failures explicitly
require Linux `/proc`, pidfd, or sandbox-denied socket/process operations. The
run also overlapped edits, so it is not a final full-suite result. These numbers
are not a claim that all failures have been classified or that Linux CI passes.
The focused final run above is the verified regression scope. Full Linux CI
remains to be run before integration.

Independent review found and verified corrections for the real offload header
format, oversized single-argument staging, missing server corroboration, remote
paths and metric collection, and the Sonata command-spec constructor. Real k6
also exposed its possible extra boundary iteration: the analyzer now retains
nominal versus actual schedules while requiring exact ledger accounting.
A final review traced hybrid provisioning, asset paths, endpoint discovery and
cleanup, and found no additional blocking issue.

Live DGX/Azure execution, regional image/Spot availability and controller Azure
credentials remain untested. The workflow checks edge-to-cloud routing before
load and k6 qualifies both endpoints. Qualification is not a capacity estimate:
the provisional pilot must run before freezing the paper campaign's rate grid.
