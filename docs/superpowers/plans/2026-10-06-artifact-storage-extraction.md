# Artifact storage extraction implementation plan (#61, second slice)

> **For agentic workers:** Use `superpowers:executing-plans` inline. Preserve
> the approved assessment's storage boundary and record verification below.

**Goal:** Reuse bounded, immutable artifact storage through Sonata while
preserving NanoLab's evidence formats, terminal reservation and run accounting.

**Architecture:** One stdlib-only `sonata_tasks.artifacts` module owns storage
and strict JSONL decoding. NanoLab retains a small `ArtifactWriter` wrapper
and decoder adapter for its policies; tree inventory/accounting remain local.

**Tech stack:** Python 3.12, filesystem exclusive creation/hard links/fsync,
thread locks, JSONL, uv and pytest. No new runtime dependencies.

**Spec:** `../specs/2026-10-06-sonata-extraction-assessment.md`, individual
artifact decisions and the second slice's acceptance criteria.

## Global constraints

- Preserve exclusive empty-directory ownership, no overwrite, bounded record
  reads, append serialization, partial-write accounting and observable errors.
- Preserve static symlink checks and no-follow append. Do not claim protection
  from arbitrary concurrent directory replacement beyond the existing contract.
- Keep `.soak-owner`, `TERMINAL_RESERVE = 4096`, the minimum NanoLab quota of
  128 bytes, `min(4096, limit_bytes // 8)`, `terminal.json` privilege and
  cumulative accounting exclusions in NanoLab.
- Keep the record cap `MAX_RECORD_BYTES = 1024 * 1024`; raw writes use the
  total quota without the JSON record cap.
- Preserve JSON bytes, bare-hex fingerprints and existing artifact descriptor
  fields. A torn final record becomes NanoLab's existing observation-gap row.
- Publish engine then tasks together before changing NanoLab's three exact
  pins/lock. Until then, adopt built wheels only in an isolated trial environment.
- Baseline: NanoLab `6e385fd`, Sonata `b85eb8c`, published pair 0.6.6.
- NanoFaaS operator checkout remains read-only. Do not weaken coverage gates.

## Review focus

- Failed or partial writes cannot erase a valid prefix or forget published bytes.
- Root, append target, raw parent and immutable destination symlinks cannot
  redirect writes under the preserved static-path guarantees.
- Cumulative usage callbacks must still charge sibling/direct writes before
  reserving terminal capacity; callbacks do not synchronize separate writers.
- Torn tail is distinct from a complete malformed/non-object/non-finite record.
- Canonical fingerprints must preserve strict JSON input acceptance and current
  hashes, including numeric-key ordering before JSON encoding.

## Task 1: Sonata bounded artifact storage

**Files:** New `packages/sonata-tasks/src/sonata_tasks/artifacts.py` and
`packages/sonata-tasks/tests/test_artifacts.py`; README and catalogue docs;
the four coordinated version sites and lock for the next release.

**Interface:** `ArtifactWriter(root: Path, limit_bytes: int, *,
owner_marker: str = ".artifact-owner", reserve_bytes: int = 0,
measure_usage: Callable[[], int] | None = None)`. Existing append, write_file,
write_blob and close signatures; `write_json(name, value, *, use_reserve=False)`.
Export `ArtifactLimitExceededError`, `ArtifactCorruptionError`,
`IncompleteRecordError(path, line_number)`, `MAX_RECORD_BYTES`, `encode_record`,
`describe_artifact`, `read_records(path)`.

- [x] Run unchanged baseline artifact/native/accounting tests and Sonata suites.
- [x] Transfer independent storage/decoder tests first. Add ordinary audit-dir
  use, configurable reserve/marker/usage, incomplete-tail error, append short
  writes/no-progress/partial errors, atomic publication/cleanup errors, record
  boundaries, symlink checks and invalid constructor inputs. Observe RED.
- [x] Move storage without redesigning its filesystem algorithm. Replace only
  product policy with the explicit interface above. Observe GREEN.
- [x] Run catalogue and engine suites plus required hooks. Build/install wheels
  outside the checkouts and exercise an ordinary bounded command audit directory.
- [x] Verify the next version is free and prepare the coherent pair, without
  publishing it. Obtain a fresh independent review of the whole slice.

## Task 2: NanoLab adapter and isolated adoption

**Files:** `tasks/soak/artifacts.py`, `tests/soak/test_artifacts.py`, generic raw
storage cases in `tests/heap_analysis/test_native_evidence.py`, adapter tests.

**Interface:** Keep `ArtifactWriter(root, limit_bytes, *, budget_root=None)`.
Pass the legacy marker, calculated reserve and local measurement callback to
Sonata; only `write_json("terminal.json", ...)` may consume reserved capacity.
Catch Sonata's incomplete-record exception and yield the unchanged gap row.
Hash the shared encoder's exact bytes (without the newline) with stdlib SHA-256
to preserve existing numeric-key ordering. Keep local tree functions untouched.

- [x] Add a failing consumer test showing a NanoLab writer uses the shared
  storage implementation; pin legacy marker, terminal reserve, cumulative
  usage and strict JSON/hash/gap compatibility.
- [x] Replace duplicated storage with the adapter and import shared codec/file
  description/errors; remove generic tests transferred to Sonata and keep
  NanoLab terminal/cumulative/native integration cases.
- [x] Install built Sonata wheels only in the trial venv. Run all NanoLab tests
  against the exact CI source pin, toolkit, hooks and installed-wheel smoke.
- [ ] After authorized publication, update all three exact pins and lock,
  verify public-index installation, then deliver NanoLab's adoption PR.

## Progress

- The user requested the next assessed slice; execution continues inline as in
  the first slice. No unrelated extraction is included.
- Pre-flight: Task 1 exports the writer/codec and neutral incomplete-tail error;
  Task 2 adds only the legacy marker, reserve, counting and gap-schema policies.
- Ruling: use composition for the NanoLab writer rather than inheritance, so
  `write_json` keeps its exact signature and cannot expose Sonata's generic
  reserved-write flag. Cost: four simple forwarding methods, with no duplicate
  storage algorithm. Native integration tests now assert observable quota behavior
  instead of inspecting the shared implementation's private byte counter.
- Ruling: retain byte-compatible fingerprint hashing in NanoLab. JSON round-trip
  normalization changes the lexical order of nested numeric keys and therefore
  persistent identities. Cost: one local stdlib hash expression; avoid exporting
  another hashing API or migrating accepted evidence identities.
- Review fix pass: add directory-wide acquisition locking independent of marker
  names and reject JSON float overflow during parsing. The three Important findings
  were reproduced RED; the focused owner/decoder/hash regressions pass GREEN.
- Declined-to-judge rulings: concurrent directory replacement remains outside the
  preserved static-path contract; shared-usage producer synchronization remains
  the caller's responsibility; public-index adoption remains gated on publication.
  These limits are explicit in the API documentation and delivery boundary.
- Delivery boundary: the next PyPI upload needs separate authorization. Before
  publication, main and dependency pins remain on released Sonata 0.6.6.
- Final: no Critical findings or deferred minors. All three Important findings
  were accepted and fixed in one pass with failing reproductions before fixes.
- Final: Ruling: arbitrary concurrent directory replacement remains outside the
  static-path contract. This extraction preserves the existing guarantee rather
  than introducing descriptor-relative path traversal; cost if wrong: callers
  needing adversarial path replacement protection need a separately designed API.
- Final: Ruling: concurrent external producers sharing usage accounting must be
  serialized by the caller. The callback cannot make separate producers atomic;
  cost if wrong: unsynchronized producers can race the quota, as before extraction.
- Final: Ruling: defer public-index adoption until the coherent pair is published.
  Local trials explicitly install both built wheels; cost: the NanoLab branch
  cannot be installed from its declared dependencies until the adoption step.

## Verification

- Baseline: 46 NanoLab artifact/native/accounting cases passed; Sonata catalogue
  397 passed with 90.40% coverage.
- TDD: the missing shared module failed 69 cases; additional invalid-input
  regressions failed nine cases; the NanoLab shared-writer boundary failed before
  the adapter. The owner race, float overflow and numeric-key hash regressions
  failed on the reviewed implementation, then passed after the single fix pass.
- Final Sonata: 481 catalogue tests passed, coverage 90.96%; 227 engine tests
  passed, coverage 96.17%. The 84 storage cases pass; storage coverage is 98%.
- Required pre-commit hooks passed in both worktrees, including types,
  import boundaries, security and formatting.
- Built both coherent Sonata 0.6.7 wheel/sdist pairs and the NanoLab/toolkit pair.
  Installed Sonata's base pair in a clean Python 3.12 environment: the README
  command audit succeeded with no errors and a reaped child; engine has no
  runtime dependencies. The official independent wheel consumer also passed.
- NanoLab's installed-wheel CLI/assets smoke and an installed storage adapter
  trial passed, including the legacy marker, terminal reserve, torn-tail schema
  and numeric-key fingerprint. These are local wheel trials, not public-index
  adoption; the declared NanoLab pins remain 0.6.6.
- Toolkit: 51 tests passed, coverage 93.71%.
- Environment diagnosis: the existing asynchronous prerequisite-platform test
  stalls under the restricted sandbox while its workflow thread waits for the
  event loop; a bounded reproduction captured both stacks. The identical test
  passed outside that sandbox in 2.89 seconds. The superseded full run was
  stopped, and the final suite runs with local socket support against CI's exact
  temporary NanoFaaS checkout, leaving the operator checkout untouched.
- Final NanoLab: all 3,436 tests passed in 293.97 seconds. The command exited 1
  solely because coverage is 86.16%, below the unchanged 90% gate. The previous
  published-pair adoption run passed 3,443 tests with 86.18% coverage; extracting
  storage removes generic consumer tests and changes the coverage denominator.
  The existing coverage deficit is recorded, not waived or presented as green CI.
- Sonata commit `afdfbaf`: draft PR https://github.com/Nanofaas/sonata/pull/17.
  NanoLab pins and lock intentionally remain on the published 0.6.6 pair.
- Sonata CI passed all nine jobs on both push and PR runs for that exact commit:
  engine, catalogue, required hooks and the six installed-wheel configurations.
