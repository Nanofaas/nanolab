# Immutable source archive extraction implementation plan (#61, eighth slice)

> **For agentic workers:** Use superpowers:executing-plans inline; one fresh whole-slice reviewer, no per-task agents. Steps use checkbox syntax.

**Goal:** Reuse a frozen source archive with its expected digest across targets, safely extract it and report cleanup failures, through Sonata's existing archive module.

**Architecture:** Add ordinary `stage_source_archive` and `remove_source_archive` helpers using unchanged RemoteProvider. Extend `source_archive_resource` with frozen-archive and strict-cleanup opt-ins; preserve existing commit-export/default cleanup behavior. NanoLab keeps its guarded Git export, receipts, remote path policy and resource dependencies; a thin adapter retains release connection retries.

**Tech Stack:** Python >=3.12, stdlib hashlib/tarfile, existing Resource/RemoteProvider/best_effort, uv/pytest/type/import/wheel checks. No new dependency or engine API.

**Spec:** ../specs/2026-10-06-sonata-extraction-assessment.md, individually assessed immutable-archive candidate.

## Global Constraints

- Bases: Sonata 3471f82, NanoLab 92a5230; coherent public pair 0.6.12. Isolated worktrees under /tmp; untouched source contract pin /tmp/nanolab-61-pinned at e7914be. No operator/cloud actions.
- Public `stage_source_archive[RequestT](provider: RemoteProvider[RequestT], request: RequestT, *, archive: Path, remote_archive: str, remote_source_dir: str, expected_digest: str | None = None) -> None` validates bytes against expected SHA-256 before remote changes, replaces the owned destination, transfers, checks remote SHA-256 and extracts using Python's data filter. Direct helper callers own normal release; failed remote acquisition compensates both paths.
- Public `remove_source_archive[RequestT](provider: RemoteProvider[RequestT], request: RequestT, *, remote_archive: str, remote_source_dir: str) -> None` strictly checks rm's status. Both helpers require caller-owned, canonical absolute nonroot paths, disjoint archive/source; validate before deletion. Target Python >=3.12, mkdir/rm/sha256sum required; directory modes normalized to0755 after safe extraction, file executable bits retained.
- Extended `source_archive_resource`: optional repo_root/commit defaultNone, `archive: Path | None = None`, `expected_digest: str | None = None`, `strict_cleanup: bool = False`. Export mode still requires repo_root+commit, exports per acquire, tar extraction, old best-effort release. Frozen mode requires expected_digest, never exports/mutates local archive, uses safe staging; strict_cleanup removes both remote paths and propagates failures; frozen resources always_release.
- SHA-256 accepts exactly64 hex characters or sha256: prefix, normalizes case; invalid expected values fail before remote operations. All new shared command/transfer boundaries reject missing/bool/noninteger status; empty/malformed checksum fails explicitly before extraction.
- Failure compensation preserves original operational error/interrupt and notes operational cleanup failure through existing best_effort. Programming errors retain the existing best_effort behavior. Trusted providers and caller-owned remote parent namespaces; no protection against malicious providers, concurrent writers/hostile target namespaces or forced death.
- NanoLab keeps create_source_archive's before/after clean commit guards, retained receipt copying and digest checks, source path policy, recipe staging, retries and Resource requires/always_release. Local extraction reuses the shared data-filter script; no generic Git guard or receipt model.
- Check0.6.13 unused, prepare coherent shared versions, build and trial explicit local wheels only. Keep consumer public pins and registry lock0.6.12 until later postmerge authorization. Deliver shared PR and local NanoLab trial.
- Preserve coverage gates. Run full NanoLab suite outside sandbox (known sandbox asyncio thread hang), explicit branch config and isolated source pin; report existing86.18%/90% debt separately. Use direct venv commands/UV_NO_SYNC=1 during wheel trial.

## Review Focus

- Changed local bytes or malformed expected checksums must fail before destructive remote operations; shared frozen reuse must not silently re-export or replace expected evidence.
- Unsafe/overlapping remote paths and option-like operands must never authorize removal of an unowned ancestor or cause archive deletion before consumption.
- Partial transfer, malformed/empty remote checksum, failed extraction and interrupts must compensate both owned remote paths, preserving primary failures and reporting operational cleanup failures.
- Tar traversal, symlink escape and executable/directory modes must respect the data filter on both local planning extraction and remote staging; caller-owned parent namespaces remain explicit.
- Base-only wheel consumers must stage/reuse ordinary application archives without optional SDKs; NanoLab must retain clean-source guards, connection retries, two-VM frozen reuse and strict resource cleanup.

## Task 1: Shared frozen archive staging

**Files:** Sonata packages/sonata-tasks/src/sonata_tasks/archive.py, tests/test_archive_frozen.py, existing tests/test_archive.py, README, root/tasks pyproject.toml and uv.lock.

**Interfaces:** Produce the two ordinary helpers and resource opt-ins above with unchanged RemoteProvider. Export the safe extraction script for callers requiring identical local extraction behavior.

- [x] Baseline Sonata archive/transfer tests and NanoLab release build/resources at pinned source. Expected: functional pass.
- [x] Write failing real-target shared tests for frozen two-target reuse/local mutation; preflight path/digest rejection; safe extraction traversal/symlinks/modes; partial acquisition, malformed results, checksum failure, interrupts and cleanup notes; strict/default resource release. Expected: missing APIs/opt-ins fail.
- [x] Implement minimal helpers with shared existing command/transfer checks; retain old export/default path. Run focused old/new tests. Expected: GREEN, unchanged default behavior.
- [x] Check unused0.6.13, coordinate version/lock, document ordinary usage/ownership/target requirements. Run full catalogue and engine at original gates, hooks, builds and six wheel configurations. Expected: all original shared gates pass.
- [x] Run a base-only installed ordinary archive consumer against actual local target commands for success, two-target reuse and failed transfer cleanup. Commit shared change. Expected: no optional integration or NanoLab import required.

## Task 2: NanoLab source adapter and trial

**Files:** NanoLab release/build.py, release/resources.py, tests/release/test_build.py, tests/release/test_resources.py, assessment and this plan.

**Interfaces:** Consume shared helpers through `_ArchiveProvider`, preserving existing retry_on_connection_death and richer execution options; product wrappers keep existing names and expected digest/error adaptation as necessary.

- [x] Add RED regressions for observed malformed transfer/command status acceptance and empty checksum handling; keep frozen/resource policy tests. Expected: old code incorrectly accepts statuses or raises IndexError.
- [x] Explicitly install built0.6.13 pair in consumer venv, delegate remote staging/strict cleanup and shared extraction script; remove duplicate mechanics. Update assertions only where ownership moves or acquisition compensation changes. Run release tests. Expected: GREEN, three public pins/lock still0.6.12.
- [x] Run full NanoLab with original branch config/source pin, toolkit, hooks, builds and fresh installed CLI/assets smoke. Expected: functional pass; coverage debt reported unchanged.
- [ ] Commit consumer trial; one fresh whole-slice review of both branches and rulings. Regrade, fix Critical/Important in one RED→GREEN pass with affected suites; ledger deferred minors and declined judgments. Expected: no unresolved Critical/Important findings.
- [ ] Push Sonata feature branch and open shared PR. Keep NanoLab trial local, publication and public adoption pending later user authorization.

## Decisions and evidence

- Ruling: Extend the existing archive module with ordinary staging/removal and opt-in frozen Resource reuse — these are independently useful archive integrity/lifetime contracts — cost if wrong: two helpers and three resource options become public API to maintain.
- Ruling: Keep guarded export, receipts, retry policy and release resource graph in NanoLab — these encode product planning/evidence and target behavior — cost if wrong: small product wrappers and adapters remain.
- Ruling: Use Python data-filter extraction for frozen archives and preserve existing tar/default resource behavior — retain safety and directory-mode semantics without changing existing consumers — cost if wrong: frozen targets require Python >=3.12 and two extraction modes remain supported.
- Ruling: Treat remote destinations and parent namespaces as caller-owned and validate canonical nonoverlapping absolute paths — no generic API can infer remote ownership, but accidental ancestor/option deletion must fail closed — cost if wrong: callers must reserve exclusive remote paths and coordinate writers.
- Pre-flight: Task1's stage/remove signatures match Task2's wrapper inputs; optional prefixed digest matches ArtifactEvidence.digest. Product resource dependency/always_release remains unchanged. Trial uses built wheels without public pin edits.

Verification before final review: NanoLab3480 functional cases/302.31s, original branch coverage86.18%/90% (exit1 solely coverage debt); toolkit51/93.71%, hooks, build and fresh installed CLI/assets plus guarded two-VM receipt/cleanup smoke pass. Public pins/lock remain0.6.12.
