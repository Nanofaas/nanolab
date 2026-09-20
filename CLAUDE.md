# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The standalone home for the operational tooling extracted from nanoFaaS. It is
a uv workspace with three members:

- `packages/nanolab` — the nanoFaaS operations CLI and supporting tooling
  (console scripts: `nanolab`, `nanolab-package-report`, `nanolab-quality`).
- `packages/tui-toolkit` — shared terminal UI components, published separately
  and also consumed by other projects.
- `sonata-tasks` — pinned to a released version from the Sonata repository, not
  vendored here.

## Setup

```bash
uv sync --all-packages --all-groups
```

The toolchain (ruff, basedpyright, bandit, import-linter, pytest, pre-commit)
lives in each package's `[dependency-groups].dev`, which a sync installs. It
must stay there and not move to `[project.optional-dependencies]`: extras are
skipped by `uv run`, so the basedpyright pre-commit hook — which runs
`uv run --frozen --all-packages --all-groups basedpyright` — would fail in CI
with "Failed to spawn: basedpyright".

## The nanoFaaS checkout is read-only

Most repository-dependent commands need a nanoFaaS checkout, pointed at by
`NANOFAAS_ROOT`:

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
```

`nanolab` **reads** it (the plan and smoke workflows drive it); it must never
write to it. Several tests fail with `RuntimeError: NANOFAAS_ROOT must point to
a nanoFaaS checkout` when the variable is unset — that is the guard, not a bug.

CI checks that checkout out into `.nanofaas-source/`, which is gitignored, so
pre-commit never sees it.

## Commands

Each package is checked on its own; there is no repository-wide lint command
that covers both, because their configurations differ.

```bash
# nanolab
NANOFAAS_ROOT=... uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --frozen ruff check packages
uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab
uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache

# tui-toolkit
uv run --frozen --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests
uv run --frozen --all-packages --all-groups basedpyright --project packages/tui-toolkit

# Everything CI runs, in one go
uv run pre-commit run --all-files
```

Two nanolab tests fail against a checkout that does not match the pinned
nanoFaaS revision. Check whether they failed before your change (`git stash`)
before assuming you broke them.

## Tooling

The same stack as the sibling projects.

| Concern | Tool | Config |
| --- | --- | --- |
| Lint + format | ruff (88 cols) | `[tool.ruff]` in each package's `pyproject.toml` |
| Types | basedpyright (standard) | `[tool.basedpyright]` |
| Security | bandit | `[tool.bandit]` |
| Import layers | import-linter | `packages/*/.importlinter` |
| Coverage | pytest-cov | `[tool.coverage.report]`, `fail_under` |

Notes that are easy to get wrong:

- `SLF` (flake8-self) is part of the rule set here, as it was before the
  migration; the per-file-ignores in each package record where private access is
  deliberate.
- `tui-toolkit` supports Python 3.11, so `reportImplicitOverride` is off for it.
  nanolab is 3.12 but keeps the same setting so the two packages agree.
- The coverage threshold is declared once, in `[tool.coverage.report]`. In
  nanolab it is also named explicitly on the pytest command line
  (`--cov=nanolab`), because a bare `--cov` means "everything importable" and
  pulled the sibling `tui-toolkit` into nanolab's number.
- Both packages set `extend-exclude = ["build", "dist"]`: ruff's built-in
  exclude list has `dist` but not `build`.
- `reportAny`-style noise aside, bandit produces several false positives here by
  name-matching: a `shell=` keyword argument that is this project's own
  `ShellBackend`, a dict named `requests` whose `.get()` is not an HTTP call, and
  an error message containing the word "secret". Each carries a `# nosec` with
  the reason. A `# nosec` only works on the same line as the flagged statement.

## Pin discipline

`nanolab` pins two packages from the Sonata repository, both to the **same
released version** on PyPI:

```toml
"sonata-engine==0.6.2"
"sonata-tasks[shell,prometheus,multipass,azure,proxmox]==0.6.2"
```

They are one lockstep pair: `sonata-tasks` carries
`Requires-Dist: sonata-engine==<its own version>`, because the two are released
from one repository under one tag and a tasks without its matching engine has no
meaning. So a bump moves **both** numbers and the engine pin inside
`sonata-tasks`, in Sonata, before this repository sees anything:

1. In Sonata, move all four version sites together — the two pyprojects, the
   `__version__` in `src/sonata_engine/__init__.py` that
   `test_dunder_version_matches_the_pyproject` compares, and the
   `sonata-engine==` pin in `packages/sonata-tasks/pyproject.toml` — then the
   lock. The release workflow publishes `sonata-engine` first and `sonata-tasks`
   after it, on a `needs:`, so a failed engine upload cannot leave a tasks on
   PyPI that pins an engine that does not exist.
2. Only then update the three pins here (the workspace `pyproject.toml` and the
   two in `packages/nanolab/pyproject.toml`) and re-lock.

Updating the pins here before the version exists on PyPI fails in `uv lock`, not
in CI. `grep -c 'git+' uv.lock` returning `0` is the invariant to keep: these
used to be pinned to git revisions on two different branches, and a lock with a
git URL in it means something has reverted to that.
