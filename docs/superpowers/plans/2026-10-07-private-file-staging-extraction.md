# Private file staging extraction (#61, seventh slice)

> Implement inline with superpowers:executing-plans; one fresh whole-slice reviewer, no per-task agents.

**Goal:** Share private file validation/copy and temporary remote staging while retaining GHCR/cosign and release policy in NanoLab.

**Architecture:** Add `sonata_tasks.credentials` using the existing typed `RemoteProvider[RequestT]` port. Two public functions and one sanitized cleanup exception own the generic file lifetime. NanoLab retains login/signing contexts, its return models and error adapter; a thin provider adapter translates its richer SDK invocation.

**Tech stack:** Python 3.12, POSIX file descriptors, stdlib contextlib/shutil/tempfile, existing uv/pytest/type/import/wheel checks.

**Spec:** ../specs/2026-10-06-sonata-extraction-assessment.md, individual private-file and immutable-archive decisions.

## Global constraints

- Bases: NanoLab `8d4f4f5`, Sonata `0f4bc72`, coherent public pair `0.6.11`. Isolated worktrees only; tests use untouched CI pin `/tmp/nanolab-61-pinned` at `e7914be` and synthetic files, never live credentials or cloud targets.
- APIs: `validate_private_file(path: Path) -> Path`; decorated `stage_private_files[RequestT](provider: RemoteProvider[RequestT], request: RequestT, files: Mapping[str, Path], *, prefix: str = "sonata-credentials") -> Iterator[tuple[str, dict[str, str]]]`; `CredentialCleanupError(operation_type: str)` exposes only a type name.
- Require POSIX current-user ownership and no-follow/nonblocking source opens. Source files must be nonempty regular owner-readable files without group/world permissions. Verify lstat/open identity; copy via binary streams into exclusive 0600 files under a private 0700 directory. Local cleanup also covers failed copying/hardening.
- Validate nonempty basename mappings and safe public prefix before any remote action. Remote target supports `mktemp -d /tmp/<prefix>.XXXXXX`, chmod and rm. Accept only the exact escaped prefix with six alphanumeric suffix characters; refuse unsafe/unavailable paths without guessing cleanup destinations. The provider and filesystem namespaces are caller-controlled/trusted.
- Reuse RemoteProvider unchanged, with integer non-boolean status required for both command and transfer. Sanitize operational OSError/RuntimeError and malformed-result failures without stdout/stderr/cause. Preserve programming/body exceptions after successful cleanup; replace cleanup failures with safe operation type and suppress exception context across remote and local cleanup.
- NanoLab retains `nanofaas-release-credentials` prefix, GHCR login argv/environment, cosign path models, ReleaseCredentialCleanupError and always-release workflow leases. Public generic paths contain no credential contents; callers choose public filenames/prefixes. Immutable archive/source guards and benchmark/release policy remain local.
- No new runtime dependency, protocol, framework or engine API. Check `0.6.12` unused before preparing coherent versions. Consumer trial explicitly installs built wheels only in its isolated venv; keep public pins/lock `0.6.11` until separately authorized publication.
- Preserve all coverage gates. Existing NanoLab branch coverage is 86.19% versus its 90% gate; report the functional suite and gate separately. Use direct venv commands/UV_NO_SYNC=1 during the local wheel trial.

## Review focus

- Filename/prefix/path inputs and malformed remote statuses must fail before secret transfer and must never authorize cleanup outside the verified temporary directory.
- Symlink/FIFO/regular-file replacement between validation and open must not leak contents, block staging or bypass ownership/permission checks; parent namespaces remain caller-controlled.
- Failed copy/hardening/transfer and exceptions or interrupts across yield must release both private staging locations; simultaneous cleanup/body failure must not disclose secrets in formatted tracebacks.
- Provider programming errors must retain their type when cleanup succeeds; local cleanup failures and noninteger statuses must not be mistaken for a successful credential lease.
- Base-only installed wheels must stage ordinary application credentials through the existing port without optional SDKs, while NanoLab keeps GHCR/cosign paths, always-release behavior and product error adaptation.

## Task 1: Shared private file lifetime

**Files:** Create Sonata src/sonata_tasks/credentials.py and tests/test_credentials.py; update task README and coherent version/lock sites.

**Interfaces:** Produce the APIs above using unchanged RemoteProvider; return only remote directory/file paths, never materialized credential strings.

- [x] Baseline existing Sonata archive/transfer and NanoLab secret/resource cases. Expected: functional pass.
- [x] Write meaningful shared tests first for private file validation, source replacement, safe names/prefix/result validation, binary private copy, success/partial acquisition/body failure/interrupt cleanup and sanitized dual failure including local cleanup. Run new file --no-cov. Expected: missing shared APIs fail.
- [x] Implement minimal stdlib lifetime/validation with existing port. Run new/existing focused cases. Expected: GREEN and no changed existing transfer/archive behavior.
- [x] Check unused0.6.12, coordinate versions/lock and document POSIX/trusted-provider boundary plus ordinary use. Run full catalogue/engine with explicit coverage configs, all hooks, build and six wheel configurations. Expected: original shared gates pass.
- [x] Verify base-only installed wheel with synthetic ordinary application files and a real local target implementing the remote port: private modes, usable staged paths, unchanged source and full success/failure cleanup without optional SDKs. Commit. Expected: independent generic lifetime passes.

## Task 2: NanoLab adapter and local trial

**Files:** NanoLab release/secrets.py, tests/release/test_secrets.py, assessment and this plan.

**Interfaces:** Reexport validate_private_file as validate_secret_file; delegate staging via a thin existing-port adapter, preserve GHCR/cosign public contracts and translate only CredentialCleanupError into the product cleanup error.

- [x] Add RED consumer regressions for observed missing/bool status acceptance and safe cleanup adaptation; retain product policy characterization. Expected: invalid status fails against the old code; existing policy remains valid.
- [x] Explicitly install built0.6.12 pair only in isolated consumer; remove duplicate file/staging mechanics, update mechanical-test patch targets to shared owner and retain meaningful behavioral assertions. Run all release tests and focused credential-facing CLI cases. Expected: GREEN with unchanged public pins/lock0.6.11 and no secret content in arguments/results/errors.
- [x] Run full NanoLab at the original coverage config/isolated pin, toolkit, hooks, build and fresh installed CLI/assets trial smoke. Expected: functional pass; existing coverage debt separately reported.
- [ ] Commit consumer trial; one fresh whole-slice review of both branches. Regrade all findings; one RED→GREEN fix pass for Critical/Important plus affected full suites, ledger every ruling/deferred minor. Expected: no unresolved Critical/Important findings.
- [ ] Push Sonata branch and open shared PR; keep NanoLab trial local pending merge and separate publication authorization. Expected: reviewable shared change with no premature public pins or publication.

## Decisions

- Ruling: Share the private file lifetime through the existing RemoteProvider port — local validation/copy and remote temporary cleanup form a useful standalone contract without another protocol — cost if wrong: two functions and one cleanup error become catalogue API to maintain.
- Ruling: Retain GHCR/cosign models, login environment and release error names locally — they describe product authentication and workflow behavior, while a thin adapter supplies the generic provider signature — cost if wrong: those adapters and a small product command guard remain in NanoLab.
- Ruling: Defer the immutable archive candidate independently — frozen reuse, verified extraction and cleanup reporting require separate archive semantics; selecting it together would combine unrelated lifetimes — cost if wrong: duplicate archive staging remains for a later assessment.
- Ruling: Reject observed missing/bool statuses at both shared and product command boundaries — the current guard accepts them as successful credential operations — cost if wrong: malformed providers must return a valid integer status before acquiring credentials.
- Pre-flight: Task1 yields the same directory/path tuple consumed by Task2; the product supplies its existing prefix, existing port adapter and cleanup-error translation. Public0.6.11 pins remain intact during the trial.

- Task 2 Ruling: Run the complete consumer suite outside the restricted sandbox — the same inert soak projection hangs in asyncio.to_thread inside sandbox but passes unchanged in 0.25s outside; rerun without changing product code or gates — cost if wrong: the full-suite result covers the normal host runtime, not restricted sandbox scheduling.
