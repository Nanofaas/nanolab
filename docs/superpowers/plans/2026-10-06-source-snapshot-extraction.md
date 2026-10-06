# Source snapshot extraction (#61, fourth slice)

> Execute inline with `superpowers:executing-plans`; one fresh whole-slice review.

**Goal:** Share working-tree capture, inventory identity, verification and independent build workspaces through `sonata_tasks.sources`.

**Spec:** `../specs/2026-10-06-sonata-extraction-assessment.md`, individual source snapshot decision and acceptance criterion.

**Architecture:** Shared capture takes a caller-owned artifact writer (root, append, write_file). Its small structural protocol accepts both the existing Sonata writer and NanoLab policy adapter. The caller acquires/closes storage and writes its own receipt. SourceSnapshot/SourceEntry retain their current fields; shared source identity uses the existing canonical codec, preserving bare-hex digests. Entry inspection becomes public for recipe source checks.

**Tech stack:** Python 3.12, Git CLI, existing artifact storage and standard library. No new runtime dependency or engine API.

## Global constraints

- Sonata base c57ffc0, published pair 0.6.8; NanoLab trial base d170a20 (PR #67 still open), main d49c228. Keep pending Buildx adoption intact.
- Preserve tracked/deleted/nonignored untracked inputs, executable modes, safe relative and dangling links, concurrent-change rejection, 100000-entry bound, input byte budget, manifest sealing and independent workspaces.
- Keep `nanolab-soak-v1`, `.soak-owner`, 16 MiB journal quota/reservation and caller source selection local. Preserve snapshot.json/source-manifest.jsonl/tree paths, entry bytes and source identity.
- Keep `archive` committed-source remote transfer and NanoLab provenance hashing unchanged.
- Leave failed capture evidence available; never replace existing tree/manifest or workspace.
- Validate capture location before generating tree content. Treat filesystem changes as failures; no atomic guarantee against concurrent malicious mutation of caller-owned directories. Caller must control/serialize its namespace.
- Prepare coherent 0.6.9 wheels only after verifying the version is unused; public publication requires separate authorization after Sonata merge. NanoLab manifests/lock remain on public 0.6.8 during explicit wheel trial.
- Keep 90% coverage gates and operator NanoFaaS untouched. Use isolated CI pin /tmp/nanolab-61-pinned.

## Review focus

- Link target changes, directory links, deleted entries and files replaced between inventory/copy/verification must fail conservatively.
- Capture must not follow escaping paths or overwrite preexisting artifacts; verify/materialize must detect manifest and tree tampering, including unreadable traversal.
- Writer ownership must survive capture failure and remain usable for caller receipts; no product schema leaks into the shared module.
- Existing source identity and evidence layout must remain byte compatible; consumers using SourceEntry/SourceSnapshot and recipe checks must use shared types.

## Task 1: Shared snapshot capability

**Files:** Sonata sources.py, test_sources.py, package README/catalogue, coordinated version sites and lock.

**Interface:** `capture_source_snapshot(repo_root, writer, *, max_bytes) -> SourceSnapshot`; `source_entry(root, name, paths) -> SourceEntry`; `verify_snapshot(snapshot)`; `materialize_snapshot(snapshot, destination) -> Path`. Writer protocol exposes root, append and write_file; capture neither closes it nor writes a product receipt.

1. Run NanoLab's 18 existing source cases as baseline. Expected: 18 pass.
2. Transfer generic regressions using an ordinary Git fixture and shared writer; add caller ownership/empty repository/manifest-tamper/input validation/traversal checks. Run focused tests before module exists. Expected: missing shared capability RED.
3. Implement minimal shared module and public entry inspector. Run focused tests. Expected: GREEN.
4. Check all catalogue/engine tests, types/lint/import/security hooks; build both wheels and check all six wheel configurations. Run ordinary installed-wheel repository capture, independent executable workspaces and tamper rejection. Expected: suites and checks pass at unchanged gates.
5. Commit shared implementation after fresh verification. Expected: reviewable Sonata branch.

## Task 2: NanoLab policy adapter and delivery

**Files:** NanoLab soak/sources.py, tests/soak/test_sources.py, recipe_observation.py, assessment and this plan.

**Interface:** Preserve `capture_source_snapshot(repo_root, destination, *, max_bytes)` and receipt fields. Re-export shared dataclasses/error/verify/materialize and compatibility `_entry`; recipe observation uses public source_entry.

1. Add consumer identity/delegation and exact receipt/fingerprint/layout tests before delegation. Expected: missing shared binding RED.
2. Replace source mechanics with caller-owned NanoLab writer, shared capture and local receipt construction. Expected: existing and new source/recipe/build/acceptance cases GREEN.
3. Install the built 0.6.9 wheel pair explicitly only into the isolated trial venv. Run full NanoLab with explicit package coverage configuration against isolated CI pin; run toolkit/hooks/package smoke. Expected: no functional failures; report the established coverage deficit separately if still present.
4. Commit trial adapter, document evidence; obtain ONE fresh whole-slice review. Reproduce/fix Important/Critical findings RED→GREEN in one pass, then run affected suites/checks. Expected: no unresolved Important/Critical findings.
5. Push the reviewed Sonata feature branch and open PR; preserve NanoLab local trial on existing public pins until publication. Expected: concrete PR and no unpublished dependency in consumer manifests.

## Progress and decisions

- Baseline: 18 existing NanoLab source tests passed before changes.
- Pre-flight: Task 1 produces the writer protocol and snapshot types used by Task 2; the NanoLab writer provides every required member, caller close/receipt policy is retained.
- Ruling: Use a dedicated sources module with caller-owned storage — dirty working-tree capture differs from remote git archive and reuses the existing writer without importing NanoLab policy — cost if wrong: an additional public module/structural protocol to maintain.
- Ruling: Trial on pending PR #67 head while it is open — the snapshot contract can be evaluated independently, preserving Buildx adoption — cost if wrong: rebase the consumer trial after that PR merges.
- Ruling: Preserve inventory digests using exact codec bytes — avoids changing accepted snapshot identities and leaves general provenance hashing local — cost if wrong: a source-scoped hash helper remains in the shared capability.

- Ruling: Acquire caller-owned evidence before Git inventory — permits an existing bounded writer without a factory/callback API; invalid quota/location/existing destinations are still rejected before acquisition — cost if wrong: Git/input-budget preflight failures now leave a NanoLab ownership marker and require a fresh destination for retry.

- Final: Ruling: Hostile concurrent namespace mutation remains outside the contract — callers own and serialize directories, ordinary replacement is corrected — cost if wrong: capture does not guarantee atomic isolation against malicious concurrent writers.

- Final: Ruling: Empty directories/directory metadata remain unbound — preserve existing file/link inventory semantics — cost if wrong: adding empty directories or changing directory metadata is not detected.

- Final: Ruling: NanoLab adoption waits for published 0.6.9 and public pins — this is an explicit installed-wheel trial, not a consumer merge candidate — cost if wrong: the trial adapter cannot run with its declared 0.6.8 dependencies until adoption.

- Final: Ruling: Keep the existing NanoLab coverage deficit and other extraction candidates separate — source extraction does not justify changing gates or unrelated scope — cost if wrong: the global 90% NanoLab gate remains unsatisfied and later candidates remain outstanding.

## Verification and review evidence

- Sonata commit `87817a7` implements the capability; dc0137a fixes the sole Important finding. Focused source cases: 28 passed. Catalogue: 537 passed,91.24%, unchanged90% gate. Engine: 227 passed,96.17%. Required hooks pass.
- Both packages build wheels/sdists. Six installed-wheel configurations and the ordinary Git application consumer validate distribution without source checkout imports. Final wheel source bytes match the committed shared module and installed trial module.
- NanoLab source adapter: 19 passed with independent exact canonical manifest/fingerprint/receipt assertions. Initial full trial: 3447 passed,86.20%, exit 1 solely from the unchanged90%coverage gate. Corrected-wheel full verification: 3447 passed in 308.03s, 86.20%; exit 1 solely from the unchanged 90% coverage gate. Toolkit: 51 passed,93.71%; required hooks and installed product smoke pass.
- Fresh whole-slice review: one Important ordinary file→symlink replacement could chmod an unrelated external target. Regression failed on 0600→0755, then passed with regular-file validation and no-follow chmod; caller failure receipts remain writable. Full catalogue passes after the fix. No Critical or Minor findings. No second review is dispatched.
- NanoLab PR67 merged `c4b66e9`, tree-identical to the original trial base d170a20. Main is aligned and clean; the local trial rebases onto that merge without product changes. NanoLab's declared dependencies/lock stay on public 0.6.8 until 0.6.9 publication.

- Shared delivery: [Sonata PR19](https://github.com/Nanofaas/sonata/pull/19), head dc0137a, two reviewed commits. No merge/publication performed.
- Final six wheel configurations pass after the correction; the independent consumer and installed NanoLab CLI/assets smoke also pass against the rebuilt pair.

- Final verification: corrected installed pair matches the shared source bytes. NanoLab full suite passes all 3447 functional cases; its unchanged package branch-coverage gate still fails at 86.20%. Final source cases19/19, toolkit51/51, hooks and installed product smoke pass.
- Sonata PR19 head dc0137a has all18 GitHub push/PR checks successful. The consumer trial stays local; no consumer PR is opened against unpublished dependencies.
