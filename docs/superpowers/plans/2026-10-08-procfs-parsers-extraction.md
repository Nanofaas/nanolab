# Pure procfs parser extraction implementation plan (#61, ninth slice)

> **For agentic workers:** Use superpowers:executing-plans inline; one fresh whole-slice reviewer, no per-task agents. Steps use checkbox syntax.

**Goal:** Remove reusable procfs decoding from NanoLab through ordinary dependency-free Sonata APIs while preserving diagnostic and soak contracts.

**Architecture:** Add sonata_tasks.procfs.parse_smaps and parse_kib_field. NanoLab keeps thin smaps large-mapping and bounded soak field adapters, residency selection, JVM parsing, availability and evidence policy.

**Tech Stack:** Python >=3.12, stdlib re/TypedDict, uv/pytest, existing type/import/wheel checks. No engine API or new dependency.

**Spec:** ../specs/2026-10-06-sonata-extraction-assessment.md, Ninth slice: pure procfs decoding.

## Global Constraints

- Bases NanoLab8313e6e and Sonata1ad57f2; isolated /tmp worktrees, no operator/cloud changes. Source tests use /tmp/nanolab-61-pinned at e7914be.
- parse_smaps(text: str) -> SmapsResult: typed dictionary with mappings:int, mapping_details:list[SmapsMapping], and anonymous/file/shared_memory/unknown:SmapsTotals. Totals are size/rss/pss bytes. Mapping adds address, permissions, backing and path.
- parse_kib_field(text: str | None, field: str) -> int | None: strict selected-field match, missing/None distinct from0, duplicate/malformed selected lines raise ValueError. No filesystem I/O or implicit size quota.
- Retain current smaps regex/header/required-field semantics; incomplete records cannot produce totals. Mapping backing does not attribute resident pages or allocator ownership.
- NanoLab keeps33554432-byte large anonymous classification,1048576-character soak text bound, selected residency fields/lenient parsing, JVM interpretation, diagnostic schemas and evidence trimming.
- Check0.6.14 unused, coordinate engine/tasks versions, trial explicit built wheels. Consumer3pins/lock stay public0.6.13 until later publication authorization.
- Original shared90% and toolkit80% gates remain. Full consumer uses explicit original branch config outside sandbox; known86.18%/90% debt is reported, never weakened.

## Review Focus

- Empty/truncated/reversed/duplicate smaps records must fail without partial totals.
- File VMA COW residency must not be attributed to anonymous backing; shared/named/unknown mappings retain categories.
- Selected KiB field rejects wrong units, negatives and duplicates while preserving absent versus zero and unrelated records.
- Consumer bounds, large-mapping boundary, unavailable-source errors and JSON trimming remain unchanged.
- Installed base-only Python3.12 usage must parse ordinary live/captured procfs without NanoLab, HTTP/VM extras or collection side effects.

## Task 1: Shared pure parsers

**Files:** Sonata packages/sonata-tasks/src/sonata_tasks/procfs.py, tests/test_procfs.py, README, root/tasks pyproject.toml and uv.lock.

**Interfaces:** Produce parse_smaps and parse_kib_field above, with SmapsTotals/SmapsMapping/SmapsResult TypedDicts.

- [x] Baseline existing NanoLab native/soak parser suites and Sonata catalogue. Expected: functional pass.
- [x] Write RED shared tests: literal mappings/totals/categories; malformed/duplicate/truncated/reversed inputs; field0/missing/units/duplicate/unrelated inputs. Run focused pytest --no-cov. Expected: missing APIs fail.
- [x] Move pure parsing preserving semantics, excluding large-mapping policy; add typed results and public docstrings. Run focused tests. Expected: GREEN.
- [x] Verify unused0.6.14, coordinate pair/lock, document ordinary text use and trust/bounds. Run catalogue/engine original gates, hooks, builds and six wheel configurations. Expected: original gates pass.
- [x] Base-only installed Python3.12 proof reads its own real procfs and checks captured malformed/zero/missing data. Commit shared implementation. Expected: no optional integration or NanoLab import needed.

## Task 2: Consumer adapters and trial

**Files:** NanoLab tasks/heap_analysis/native.py, tasks/soak/probes.py, tests/heap_analysis/test_native.py, tests/soak/test_probes.py, assessment and this plan.

**Interfaces:** Consume both Task1 helpers; preserve existing product functions/results.

- [x] Add focused contract tests for missing-versus-zero, duplicate values, character bound and smaps record failures; run original implementation. Expected: existing contract passes.
- [x] Explicitly install built0.6.14 wheels, delegate smaps and field parsing, retain large-mapping and bounds policy. Run native/evidence/soak parser tests. Expected: GREEN and public pins/lock unchanged0.6.13.
- [x] Full NanoLab original branch config/source pin, toolkit, hooks, package builds, fresh installed CLI/assets and native/soak proof. Expected: functional pass; original coverage debt reported.
- [ ] Commit trial; one fresh whole-slice review with plan/spec/ledger. Regrade and fix Critical/Important once with RED→GREEN and affected full suites; defer Minor explicitly. Expected: no unresolved Critical/Important.
- [ ] Push shared Sonata feature branch and open PR; keep consumer trial local. Expected: shared PR reviewable, no publication/public adoption yet.

## Decisions and evidence

Ruling: Extract pure procfs parsers, leave JVM and collectors local — independently useful text contracts remove meaningful duplicated decoding without importing the diagnostic experiment — cost if wrong: a small public module needs maintenance while JVM/collection duplication remains.
Ruling: Keep large-mapping threshold, field selection and text bounds local — they define product evidence and soak sample policy — cost if wrong: consumer wrappers remain and shared callers must select fields and bound input.
Ruling: Do not combine Prometheus acquisition with this slice — its small loop and product policies need a separate worthwhile contract — cost if wrong: temporal snapshot logic remains local.


## Verification before final review

Shared Sonata1ad57f2..7630bc2:31 focused RED→GREEN cases; catalogue757/92.29%
and engine227/96.17% pass both original90% gates. Initial engine
test_dunder_version_matches_the_pyproject caught a forgotten runtime constant
during version preparation; completing the0.6.14 bump resolves it and the full
engine suite passes. All14 hooks, four wheel/sdist source-byte/metadata proofs,
six final installation modes and base-only installed Python3.12 actual procfs
proof pass. Both PyPI0.6.14 endpoints were404 before preparation; no publication.

Consumer161 focused cases, original-toolkit51/93.71%, all15 hooks, builds and
fresh installed CLI/assets plus procfs adapter proof pass. Full original
NanoLab branch configuration with isolated NanoFaaS source e7914be:
3490 functional cases pass in312.39s. The command exits1 solely at86.17%
against the unchanged90% gate (previous slice86.18%); coverage debt is explicit.
The three public pins and registry lock are byte-identical to8313e6e at0.6.13.
Trial/fresh installed environments use explicitly identified0.6.14 wheel
overlays. A single frozen live procfs capture produces full smaps/residency/soak
and availability-schema results equal to the original implementation.
