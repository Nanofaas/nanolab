# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The standalone home for the operational tooling extracted from nanoFaaS. It is
a uv workspace with three members:

- `packages/nanolab` — the nanoFaaS operations CLI and supporting tooling
  (console scripts: `nanolab`, `nanolab-package-report`, `nanolab-quality`).
- `packages/tui-toolkit` — shared terminal UI components, published separately
  and also consumed by other projects.
- `sonata-tasks` — pinned from the Sonata repository, not vendored here.

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

`nanolab` pins two packages from the Sonata repository, and they are pinned to
**different commits on purpose**:

- `sonata-engine` → the tip of the `feature/workflow-observers` branch, which is
  **not** an ancestor of Sonata's `main`. nanolab imports `WorkflowObserver` and
  `WorkflowCompletion`, which exist only there.
- `sonata-tasks` → a commit on Sonata's `main`, because that is the package that
  carries the shellcraft and proxmox-sdk pins.

Bumping one and "unifying" the other onto the same revision silently reverts
nanolab onto a tree without the observers and breaks the `package`, `plans` and
`smoke` jobs with `ImportError: cannot import name 'WorkflowObserver'`.
