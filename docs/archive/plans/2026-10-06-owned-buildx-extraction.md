# Owned Buildx extraction implementation plan (#61, third slice)

> **For agentic workers:** Execute inline with `superpowers:executing-plans`;
> obtain one fresh whole-slice review after implementation.

**Goal:** Reuse strict Buildx acquisition and identity-checked compensation
through Sonata, keeping NanoLab's emulation and recipe policies local.

**Architecture:** Extend the existing `buildx_builder_resource`, preserving its
default reuse/replacement behavior. NanoLab composes its emulation lifetime with
the strict shared resource; it retains daemon selection, registration guards,
image/configuration choices, platform validation and evidence.

**Tech stack:** Python 3.12, existing command executor ports, Docker Buildx,
stdlib UUID/regex. No new runtime dependencies; engine remains dependency-free.

**Spec:** `../specs/2026-10-06-sonata-extraction-assessment.md`, individual
Buildx/binfmt decisions and the owned-Buildx acceptance criterion.

## Global constraints

- Baselines: NanoLab `d49c228`, Sonata `ced86ed`, published pair 0.6.7.
- Keep ordinary reuse, opt-in replacement, validation-key checks, role/options,
  dependencies, state type `Resource[str]` and existing default `--use` behavior.
- Strict acquisition never adopts a pre-existing builder. Reconcile partial
  creation/cancellation using the unique owner node; remove only a matching
  single-node docker-container builder, otherwise retain it and report failure.
- Reject incompatible replacement/ownership options before commands. Normalize
  strict builder/node names as Buildx does; retain the older default path.
- Keep NanoLab's `.json` evidence fields, native platform, pinned installer and
  probe images, registry `127.0.0.1:5000`, host network and platform pair local.
- Keep builder cleanup before binfmt removal. If shared cleanup cannot confirm
  removal or absence, retain the registration and report it, including failed
  partial creation. Always release the local lock even when cleanup fails.
- Keep binfmt local: a client-local daemon-ID lock does not establish exclusion
  across clients or multiple daemons sharing the host kernel registration table.
- Identity comparison and deletion require a stable executor/client namespace;
  callers serialize external mutation during that pair of commands. No atomic
  cross-client deletion guarantee or generic binfmt API is introduced.
- Publish the coherent next engine/tasks pair before updating NanoLab's pins.
  Before separate publication authorization, trial only explicitly built wheels.
- Preserve the existing coverage gates and leave operator NanoFaaS untouched.

## Review focus

- Foreign replacement or additional nodes after acquisition must not be removed.
- Unknown cleanup state after partial create must not permit binfmt removal.
- Cancellation and cleanup errors preserve the original acquisition error.
- A pre-existing builder must be refused before emulation is installed.
- Default callers' reuse, replacement and active-builder selection remain intact.

## Task 1: Extend Sonata's existing Buildx resource

**Files:** `sonata_tasks/buildx.py`, `tests/test_buildx.py`, package README and
catalogue; coordinated next version sites and lock only after checking PyPI.

**Interface:** Add `exclusive: bool = False`, `owner_node: str | None = None`,
`use: bool = True` to `buildx_builder_resource`. Explicit owner nodes require
exclusive mode; exclusive plus replace_existing is invalid. Otherwise generate
an owner node. Strict `release(inputs, name)` reconciles attempted creation and
is idempotent after confirmed absence/removal, including failed acquisition.

- [x] Baseline: run existing seven Buildx cases and full catalogue/engine suites.
- [x] Add strict lifecycle, existing-builder refusal, independent UUID ownership,
  no active-selection mutation, partial-create/cancellation, foreign replacement,
  added-node, unavailable inspection and cleanup-error regressions. Observe RED
  with `pytest --no-cov packages/sonata-tasks/tests/test_buildx.py`.
- [x] Implement opt-in strict behavior in the existing resource; share creation
  argv/bootstrap/validation. Observe GREEN, retaining the original seven cases.
- [x] Verify catalogue/engine suites, required hooks, builds and installed wheels.
  Exercise an ordinary application builder outside NanoFaaS using an isolated
  Buildx client configuration; record exactly what real-Docker checks establish.

## Task 2: Compose NanoLab's emulation with shared strict Buildx

**Files:** `tasks/recipes/builder.py`, `tests/tasks/recipes/test_builder.py`,
the assessment and this plan. Recipe workflow/multiarch callers stay intact.

**Interface:** Keep `recipe_builder_resource(executor, run_dir, tag, requires)`
and `RecipeBuilder(name, platform)`. Feed the shared resource `exclusive=True`,
the receipt's UUID owner node, `use=False`, host network and local configuration.
Use `validate` to retain bootstrap text and check AMD64/ARM64; preserve the early
collision preflight before emulation. Retry shared release during compensation
even if acquisition failed; registration removal requires confirmed cleanup.

- [x] Baseline: existing builder/multiarch/workflow group must pass (71 cases).
- [x] Add failing consumer-boundary and unresolved-partial-cleanup regressions.
  Observe RED before delegation; then characterize foreign replacement and added
  nodes in the integration tests.
- [x] Delegate strict lifecycle, retain emulation policies and evidence. Observe
  GREEN; test cleanup ordering, cancellation and an immediate second attempt.
- [x] Trial built Sonata wheels only in the isolated venv. Run full NanoLab
  against the exact CI NanoFaaS pin, toolkit, hooks and installed-wheel smoke.
  Record functional results and the known 90% coverage-gate deficit separately.
- [x] Obtain fresh whole-slice review, reproduce Important/Critical findings RED
  and fix in one pass, then run the affected full suites and required checks.
- [x] Deliver the reviewed Sonata PR and preserve the NanoLab trial branch.
  Publication and public-index adoption follow separate authorization after merge.

## Progress and rulings

- The user requested the next assessed lot; continue inline with isolated
  worktrees as in the previous slices. The existing assessment supplies the
  agreed purpose and boundary; routine API choices are recorded here.
- Pre-flight: Task 1 supplies strict `Resource[str]` lifecycle; Task 2 consumes
  release reconciliation even after failed acquisition. Binfmt policy remains
  local and is never silently generalized into Sonata.
- Ruling: defer generic binfmt and host locking. Client-local locks cannot
  serialize remote clients or prove shared kernel identity. Cost: the existing
  emulation implementation remains local until a stronger independent contract
  is demonstrated; do not describe it as a generic distributed lock.
- Ruling: preserve NanoLab's early builder-name check before emulation, in
  addition to strict acquisition's own check. Cost: one extra list command;
  it preserves the existing refusal before any installation side effect.
- Ruling: retain registrations on unresolved shared cleanup, including partial
  create failures. Cost: an operator may need to reconcile retained resources;
  removing emulation while an unresolved builder can still run is less safe.

## Verification

- Existing NanoLab builder/multiarch/workflow group: 71 passed.
- Existing Sonata Buildx group: seven passed.
- Docker client available: Buildx v0.35.0. Native name validation lowercases
  builder/node identifiers and requires a leading letter. Its node-update path
  refuses a different owner node on an existing nonempty builder without append.
  Sources: https://github.com/docker/buildx/blob/v0.35.0/store/util.go and
  https://github.com/docker/buildx/blob/v0.35.0/store/nodegroup.go.

- Third-slice RED/GREEN: Sonata 22 new failures before API extension; NanoLab
  two new failures before delegation and pending-cleanup reconciliation. Final
  focused groups: Sonata 32 passed; NanoLab recipe group 78 passed.
- Catalogue full suite: 506 passed, coverage 91.13%; engine: 227 passed, 96.17%.
  All Sonata and NanoLab hooks passed; toolkit: 51 passed, 93.71%.
- Installed base-wheel real Docker consumer: scratch OCI build produced AMD64
  and ARM64 manifests, selection unchanged, builder/container removed and
  duplicate release harmless. No binfmt installation or remote-client proof.

- Final Sonata verification after review fixes: **509 passed, 91.16% coverage**;
  engine **227 passed, 96.17%**; all hooks and six installed-wheel configurations
  passed. Installed-wheel Docker OCI test and NanoLab recipe trial were repeated
  after the fixes (**81 passed** for the installed NanoLab wheel).
- Sonata delivery commit: `14d1c2f`, branch `feat/exclusive-buildx`.
- Fresh whole-slice reviewer found no Critical issues, two Important issues
  and one Minor. Both Important findings were reproduced before production
  changes: failed rm deleting the local record could permit early binfmt removal;
  secondary cleanup cancellation/exception could mask the acquisition failure.
  The fixes keep removal uncertainty after unsuccessful rm and retain original
  failures, cleanup receipts and lock closure.
- Deferred Minor: `buildx ls --format {{.Name}}` also emits node names. Matching
  a foreign node may incorrectly refuse an unused builder name, conservatively.
  Cost: choose another name until top-level-only parsing is added.

### Final rulings from review

- Final: Ruling: cross-client/shared-kernel binfmt exclusion remains deferred — existing local lock is not a distributed ownership proof — cost: no cross-client guarantee.
- Final: Ruling: client-store mutation between inspect/remove remains caller-serialized — CLI lacks atomic compare/delete — cost: unsynchronized external mutation can race removal.
- Final: Ruling: restart recovery remains outside this in-memory resource — receipts support operator reconciliation, no persisted cleanup API promised — cost: manual orphan recovery after restart.
- Final: Ruling: concurrent acquisition on the same resource remains unsupported — one resource instance represents one active lifetime — cost: callers must create/serialize instances.
- Final: Ruling: explicit owner nodes must remain unique — identity cannot distinguish deliberate reuse/spoofing — cost: caller-supplied duplicate identities defeat ownership proof.
- Final: Ruling: post-install binfmt inspection failure stays in the existing local implementation — generic host ownership is separately deferred — cost: uncertain installer side effects may need operator reconciliation.
- Final: Ruling: pre-publication NanoLab trial stayed on declared 0.6.7 until public 0.6.8 — install built wheels explicitly only for verification — cost: local trial is not merge-ready adoption.
- Final: Ruling: other assessment candidates stay in later slices — this lot is Buildx — cost: remaining local implementations persist.
- Final: Ruling: the executor completes final full/wheel verification the reviewer did not rerun — use fresh command exits/logs with explicit coverage configuration — cost: CI remains independent confirmation.
- Ruling: refuse disappeared records after unsuccessful rm without a new daemon API — local-store absence cannot establish daemon teardown — cost: manual reconciliation when Buildx has already discarded its record.

- Sonata PR: https://github.com/Nanofaas/sonata/pull/18, commit
  `14d1c2f41ac3fe0b01f2984b51bf4cbcc418af1d`; all 18 push/PR CI checks succeeded.
- Final pinned NanoLab trial: **3446 passed** in **296.34 s**; process exit 1
  solely because the unchanged 90% branch-coverage gate reports **86.20%**.
  Previous published storage baseline: 3436 passed, 86.16%. No functional test
  failed. Coverage configuration was supplied explicitly and output was isolated
  from the toolkit run. Toolkit remains 51 passed, 93.71%; all delivery hooks pass.
- Ruling: retain the existing coverage gate and leave its baseline deficit outside
  this Buildx slice. Cost: NanoLab's gate continues to fail until separate coverage
  work; do not present the trial as a green coverage result.
- The pre-publication trial preserved all three Sonata pins and the lock at 0.6.7.
  Local implementation commit: `c37d788`, branch `feat/61-owned-buildx`.
  At that stage it was a tested implementation branch, before public adoption.
  Publication of coherent 0.6.8 and adoption were pending at that stage;
  the completed publication and adoption are recorded below.


## Publication and public-index adoption

- User authorization received after merging Sonata PR #18.
- Tag `v0.6.8` points at `c57ffc0eade6e806848b018a1de845b784615701`.
- Release workflow: https://github.com/Nanofaas/sonata/actions/runs/37517416738.
  Engine published successfully before tasks; PyPI exposes wheel and sdist for
  each. A fresh base installation from the public index runs the independent
  catalogue consumer. Initial simple-index propagation lag was confirmed by
  probing the version API and both simple representations, then resolved.
- Root engine pin, NanoLab engine pin and catalogue/extras pin are 0.6.8.
  Lock source is `https://pypi.org/simple`; all four hashes match the version API.
  Only the two external Sonata entries and their consuming workspace requirement
  metadata changed. No other dependency was upgraded.
- Forced public-index reinstall replaced both local trial wheels in the consumer
  venv. Full pinned NanoLab, toolkit, hooks and installed-wheel checks follow.

- Final public-index verification: full NanoLab against the exact CI NanoFaaS
  pin **3446 passed in 293.38 s**, coverage **86.20%**, command exit 1 solely
  from the unchanged 90% gate (same as the local trial). Toolkit **51 passed**,
  **93.71%**; all hooks and `uv lock --check` passed. Wheel/sdist builds, installed
  CLI/assets smoke and normal wheel dependency resolution passed. Both Sonata
  installations have no direct URL override; engine runtime dependencies are zero.
- The public Sonata base installation also repeated the real Docker scratch
  AMD64/ARM64 OCI build: selected builder unchanged, private builder/container
  removed and duplicate release harmless. No binfmt installation was performed.
- Adoption is ready for the NanoLab PR; #61 remains open for the assessed later
  slices and deferred capabilities. Coverage thresholds and CI were not changed.
