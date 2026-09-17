# Native Memory Readings Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the eight findings the code review raised against PR #39, without changing what the readings measure or what the P24 soak path does.

**Architecture:** Five tasks grouped so each one is independently reviewable: the cross-clock deadline, the duplicated raw storage, the cleanup budget and its lost signal, two worker-side correctness slips, and two evidence-module slips. Nothing here changes the opt-in contract, the artifact tree or the summary's content.

**Tech Stack:** Python 3.12, pytest. No Docker is needed by any test in this plan.

**Spec:** `docs/superpowers/specs/2026-09-16-control-plane-native-memory-readings-design.md`

## Global Constraints

- Preserve the default P24 soak request, response and collection behavior: no new reads, commands, fields or output limits when options are absent.
- Unavailable optional readings do not themselves change the run's status; existing target-identity and diagnostic completion requirements still apply.
- Killing `jcmd` is not proof that the command inside the JVM has completed. Do not issue a later diagnostic while command completion is unresolved.
- Truncation or an over-limit smaps is marked explicitly as partial/unavailable. Publish no totals derived from incomplete smaps.
- Report measurements and missing evidence without recommending a cause or tuning change.
- Every existing test must keep passing. These are corrections, not redesigns: if a fix requires changing an existing assertion, say so in the task report and explain why the old assertion was wrong.

---

## File map

- Modify `packages/nanolab/assets/soak/diagnostic-worker.py`: anchor the deadline locally (#3), set the completion state after the read (#5), order the `returncode` comparison None-safely (#8).
- Modify `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py`: send a relative budget (#3), stop retaining the duplicated response log (#4), restore the cleanup budget (#2) and the unconfirmed-cleanup signal (#6).
- Modify `packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py`: keep the already-trimmed pointer (#1), account raw writes against the writer (#7).
- Modify `packages/nanolab/tests/soak/test_diagnostic_helper.py`: tests for #2, #3, #4, #5, #6, #8.
- Modify `packages/nanolab/tests/heap_analysis/test_evidence.py`: tests for #1 and #7. Create it if the implementation put these tests elsewhere; in that case add them to the file that already covers `evidence.py`.

---

### Task 1: Anchor the collection deadline inside the container (#3)

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py:532`
- Modify: `packages/nanolab/assets/soak/diagnostic-worker.py` (the `memory` function, around lines 137-141 and the `heap_info` block)
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: request key `memory_budget_s` (a duration in seconds) replacing `memory_deadline_s` (an absolute host timestamp). Tasks 2-5 do not touch it.

The host currently sends an absolute `time.monotonic()` value:

```python
                cfg["memory_deadline_s"] = self.commands.deadline - margin
```

and the worker compares it against its own clock:

```python
            if opt_in and time.monotonic() >= deadline:
```

Every other worker subcommand anchors its deadline locally — `deadline = time.monotonic() + cfg.get("timeout_s", 40)` and `deadline = started + request["timeout_s"]` — so this is the only place a monotonic value crosses the process boundary. Two processes share `CLOCK_MONOTONIC` only when they share a kernel; under a VM-backed Docker or a remote `docker_host` the comparison is meaningless. Ahead, every read times out and the run still passes with all sources `not_started`; behind, `jcmd` can outlive the exec timeout while `jvm_may_be_running` stays true, and the measured control plane is killed mid-run.

- [ ] **Step 1: Write the failing tests**

```python
def test_memory_request_sends_a_relative_budget_not_a_host_timestamp(monkeypatch):
    owner, calls = memory_owner(monkeypatch)
    owner.read_memory(timeout_s=10, include_smaps=True)
    cfg = json.loads(base64.b64decode(calls[-1][0][-1]))
    assert "memory_deadline_s" not in cfg
    assert 0 < cfg["memory_budget_s"] <= 10


def test_worker_anchors_the_budget_on_its_own_clock(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    monkeypatch.setattr(module, "read_proc", lambda path, limit: "body")
    # A host clock far behind the container's would have expired a deadline
    # sent as an absolute value; a relative budget is immune to the offset.
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "memory_budget_s": 30.0,
        }
    )
    assert result["smaps"] == "body"
    assert result["errors"] == {}


def test_worker_still_refuses_to_start_once_its_own_budget_is_gone(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    monkeypatch.setattr(module, "read_proc", lambda path, limit: "body")
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "memory_budget_s": 0.0,
        }
    )
    assert result["smaps"] is None
    assert "deadline exhausted" in result["errors"]["smaps"]
```

- [ ] **Step 2: Run the tests and confirm RED**

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k "relative_budget or anchors_the_budget or own_budget_is_gone" -q --no-cov
```

Expected: failures on the missing `memory_budget_s` key.

- [ ] **Step 3: Send a duration from the host**

```python
                cfg["memory_budget_s"] = remaining - margin
```

Keep the surrounding `remaining`/`margin` computation exactly as it is; only the key and the value's meaning change.

- [ ] **Step 4: Anchor it in the worker**

Where the worker currently reads `cfg["memory_deadline_s"]` into `deadline`, replace it with a locally anchored value computed once, before the read loop:

```python
    deadline = time.monotonic() + cfg["memory_budget_s"] if opt_in else None
```

Use that same `deadline` for the `heap_info` block instead of `cfg["heap_info_deadline_s"]` if the implementation introduced a second absolute value there; a single locally anchored deadline covers the whole collection, which is what the spec's "all reads and the command share one overall collection budget" asks for.

- [ ] **Step 5: Run the tests and confirm GREEN**

Run the Step 2 command, then the whole file:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -q --no-cov
```

`test_expired_budget_never_launches_jcmd` pins the expired-budget response. It should still pass; if it asserted the old key, update it and say so in the report.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/assets/soak/diagnostic-worker.py \
  packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py \
  packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Anchor the memory collection budget on the container clock"
```

---

### Task 2: Stop retaining the duplicated response log (#4)

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py` (`_DockerCommands.run` around line 356, and the opt-in read around line 537)
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `_DockerCommands.run(..., retain_log=True)`, defaulting to today's behaviour. Only the opt-in memory read passes `False`.

`run` writes the command's whole output to `self.root / ("docker-" + uuid4().hex + ".log")` and never removes it. For heap analysis `output_root` is the evidence directory, and `measure_tree` excludes only `workspace-*` and dot-prefixed parts, so those logs count against `artifact_limit_bytes`. Every opt-in reading is therefore stored twice: once in the log and once republished under `evidence/native/`. With the escaping headroom the existing tests exercise, three checkpoints can add over a hundred megabytes of pure duplication and fail `enforce_limit` before the run finishes.

The raw content is republished as evidence on success, so the log adds nothing there. On failure it is the only record, so it stays.

- [ ] **Step 1: Write the failing test**

```python
def test_opt_in_read_leaves_no_duplicate_response_log(monkeypatch, tmp_path):
    owner, _calls = memory_owner(monkeypatch, smaps="x" * 4096, root=tmp_path)
    owner.read_memory(timeout_s=10, include_smaps=True)
    assert list(tmp_path.glob("docker-*.log")) == []


def test_a_failed_opt_in_read_keeps_its_log_for_forensics(monkeypatch, tmp_path):
    owner, _calls = memory_owner(monkeypatch, fail=True, root=tmp_path)
    with pytest.raises(BaseException):
        owner.read_memory(timeout_s=10, include_smaps=True)
    assert list(tmp_path.glob("docker-*.log"))


def test_legacy_read_keeps_its_log_unchanged(monkeypatch, tmp_path):
    owner, _calls = memory_owner(monkeypatch, root=tmp_path)
    owner.read_memory(timeout_s=10)
    assert list(tmp_path.glob("docker-*.log"))
```

Extend the `memory_owner` helper so it writes a real log file through the genuine `run` path rather than replacing it wholesale, and accepts `root` and `fail`. If the existing helper fakes `run` entirely, add a second helper that exercises the real `_DockerCommands.run` against a fake `docker` executable, following the pattern the file already uses for command execution.

- [ ] **Step 2: Run the tests and confirm RED**

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k "duplicate_response_log or keeps_its_log" -q --no-cov
```

Expected: the first test fails because the log survives.

- [ ] **Step 3: Let the caller opt out of retaining the log**

In `_DockerCommands.run`, add the keyword and unlink on success:

```python
    def run(
        self,
        args,
        timeout_s=20.0,
        *,
        cleanup=False,
        limit=1024 * 1024,
        retain_log=True,
    ):
```

and, at the point where `run` is about to return its output:

```python
        if not retain_log:
            # The caller republishes this payload as its own evidence, so the
            # log is pure duplication inside the artifact budget. A failure
            # returns before this point and keeps the log.
            log.unlink(missing_ok=True)
```

- [ ] **Step 4: Use it from the opt-in read only**

```python
            limits = {"limit": _OPT_IN_RESPONSE_BYTES, "retain_log": False} if ... else {}
```

Keep the legacy branch passing nothing, so its log behaviour is untouched.

- [ ] **Step 5: Run the tests and confirm GREEN**

Run the Step 2 command, then the whole file.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py \
  packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Stop storing every optional reading twice in the artifact tree"
```

---

### Task 3: Restore the cleanup budget and the unconfirmed-cleanup signal (#2, #6)

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py:566` and `:575`
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: no new names; the raised exception's `str()` regains the cleanup-unconfirmed text.

Two independent slips in the same `except` block.

The budget: this path sets `cleanup_deadline = time.monotonic() + 5.0`, but when `jvm_may_be_running` it calls `_cancel_remote()`, which makes three or more Docker calls. `cancel_remote()` grants `10.0` for the same work, and the 5 s budget elsewhere covers a shorter sequence. On a busy daemon the deadline expires inside `run`, cleanup is unconfirmed, the helper leaks, and whether the target was stopped is unknown.

The signal: the opt-in branch calls `error.add_note(...)` and re-raises the original error, while the legacy branch raises a `RuntimeError` whose message carries the text. `_publish` records failures as `f"{type(error).__name__}: {str(error)[:1024]}"`, and `str()` does not render notes — so on the higher-risk path, where the target may still be running a `jcmd`, `report.json` shows only the bare unresolved-completion message.

- [ ] **Step 1: Write the failing tests**

```python
def test_opt_in_cleanup_gets_the_same_budget_as_cancel_remote(monkeypatch):
    owner, budgets = memory_owner_recording_cleanup_budget(monkeypatch)
    with pytest.raises(BaseException):
        owner.read_memory(timeout_s=10, include_heap_info=True)
    assert budgets and min(budgets) >= 10.0


def test_unconfirmed_cleanup_survives_str_for_the_receipt(monkeypatch):
    owner = memory_owner_with_failing_cleanup(monkeypatch)
    with pytest.raises(BaseException) as raised:
        owner.read_memory(timeout_s=10, include_heap_info=True)
    assert "cleanup unconfirmed" in str(raised.value)
```

Build the two helpers beside the existing fakes: the first records the value assigned to `commands.cleanup_deadline` relative to `time.monotonic()`; the second makes `_cancel_remote` raise.

- [ ] **Step 2: Run the tests and confirm RED**

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k "cleanup_budget or cleanup_survives_str" -q --no-cov
```

- [ ] **Step 3: Give the cancellation path the budget its work needs**

```python
                self.commands.cleanup_deadline = time.monotonic() + (
                    10.0 if jvm_may_be_running else 5.0
                )
```

The procfs-only case keeps 5 s, which is what it has always had for a shorter sequence.

- [ ] **Step 4: Put the text back in the message**

```python
            except BaseException as cleanup_error:
                raise RuntimeError(
                    "memory read failed; remote reader cleanup unconfirmed: "
                    f"{cleanup_error}"
                ) from error
```

Use the same message on both branches: a receipt that renders `str(error)` must show it, and the opt-in path is the one where it matters most. If the implementation needs the original exception type preserved for a caller, chain it rather than annotate it.

- [ ] **Step 5: Run the tests and confirm GREEN**

Run the Step 2 command, then the whole file.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py \
  packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Give optional-read cleanup its full budget and keep its signal"
```

---

### Task 4: Two worker correctness slips (#5, #8)

**Files:**
- Modify: `packages/nanolab/assets/soak/diagnostic-worker.py:141` and `:243`
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: no new names.

`state = "completed"` is assigned before `read_proc` runs, so a read that raises is still published as completed — `evidence.py` surfaces that as `sources["smaps"]["completion"]`, contradicting the error recorded beside it.

`result.returncode < 0` is evaluated before any `!= 0` comparison, and `OwnedCommandResult.returncode` is `int | None`. The preceding terms should make `None` unreachable, but every other site in this codebase puts the None-safe comparison first, and a `TypeError` here is outside the caller's `except (OSError, ValueError, RuntimeError)`: it would escape `memory()`, fail the worker, and through `jvm_may_be_running` kill the measured target.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_failed_read_is_not_reported_as_completed(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)

    def read(path, limit):
        if path.name == "smaps":
            raise ValueError("procfs evidence exceeds read bound")
        return "body"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory(
        {"target": asdict(TARGET), "include_smaps": True, "memory_budget_s": 30.0}
    )
    assert result["completion"]["smaps"] != "completed"
    assert "exceeds read bound" in result["errors"]["smaps"]
    assert result["completion"]["status"] == "completed"


def test_command_tolerates_a_missing_return_code(monkeypatch, tmp_path):
    module = worker()
    from processes import OwnedCommandResult

    result = OwnedCommandResult(returncode=None, forced_stop=True, reaped=True)
    monkeypatch.setattr(
        module, "OwnedCommandRunner", lambda *a, **k: _Runner(result)
    )
    with pytest.raises(module.CommandCompletionUnresolved):
        module.command(("/bin/true",), time.monotonic() + 30, tmp_path)
```

The check lives in `command(argv, deadline, scratch, limit=2 * 1024 * 1024, *, require_completion=False)` at `diagnostic-worker.py:213`, which imports `OwnedCommandRunner` from `processes` inside the function body — so monkeypatching the module attribute is not enough on its own; patch `processes.OwnedCommandRunner` or inject through the module the way the file's existing command tests do. `OwnedCommandResult` (`processes.py:266`) takes `returncode: int | None`, `forced_stop`, `reaped`, then keyword defaults; `errors` is not a field, so build it as shown. `_Runner` is a two-line stand-in whose `run()` returns the prepared result.

- [ ] **Step 2: Run the tests and confirm RED**

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k "not_reported_as_completed or missing_return_code" -q --no-cov
```

- [ ] **Step 3: Set the completion state after the read succeeds**

```python
        state = "not_started"
        try:
            if opt_in and time.monotonic() >= deadline:
                raise TimeoutError("memory collection deadline exhausted")
            result[name] = read_proc(Path("/proc/1") / name, limit)
            state = "completed"
```

- [ ] **Step 4: Order the return-code comparison None-safely**

```python
        or result.returncode != 0
        # A negative returncode means the child died from a signal (a killed
        # helper container, the kernel OOM killer), which is no proof at all
        # that the in-JVM command finished.
        or result.returncode < 0
```

Putting the `!= 0` term first short-circuits `None` before the ordering comparison, matching `command()` and `_DockerCommands.run`. Check the surrounding boolean: if `!= 0` changes the outcome for a legitimately non-zero-but-completed case, keep the ordering fix but guard with `result.returncode is None or ...` instead, and say which you chose and why in the report.

- [ ] **Step 5: Run the tests and confirm GREEN**

Run the Step 2 command, then the whole file.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/assets/soak/diagnostic-worker.py \
  packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Report a failed read as failed and tolerate a missing exit code"
```

---

### Task 5: Two evidence slips (#1, #7)

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py:31` and `:155`
- Test: the file that already covers `evidence.py`; create `packages/nanolab/tests/heap_analysis/test_evidence.py` if there is none.

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: no new names.

`native_comparison` re-points the trimmed mappings unconditionally:

```python
            smaps = block.get("smaps")
            if isinstance(smaps, dict):
                _trim_unbounded_smaps(smaps, f"see evidence/runtime-{checkpoint}.json")
```

When `persist_native` already trimmed the block, the stored value is the string `"see evidence/native/<checkpoint>-smaps.txt"` — the correct pointer — and this overwrites it with a pointer to `runtime-<checkpoint>.json`, the file whose mappings were removed. The reader is sent to a dead end in exactly the case where the trim was needed.

`_write_raw` publishes into `writer.root/native/` directly. It does enforce the run budget itself, but `ArtifactWriter._used_bytes` never learns about those bytes, so every later `write_json` — the natural checkpoint, the terminal receipt — checks its per-record limit and terminal reserve against a figure that can be tens of megabytes too low.

- [ ] **Step 1: Write the failing tests**

```python
def test_comparison_keeps_a_pointer_that_was_already_trimmed(tmp_path):
    block = {
        "smaps": {
            "available": True,
            "large_anonymous_mappings": {
                "count": 2,
                "mappings": "see evidence/native/natural-drain-smaps.txt",
            },
        }
    }
    root = write_one_record(tmp_path, "natural-drain", block)
    entry = native_comparison(root)["natural-drain"]
    pointer = entry["native"]["smaps"]["large_anonymous_mappings"]["mappings"]
    assert pointer == "see evidence/native/natural-drain-smaps.txt"


def test_comparison_repoints_a_list_that_is_still_inline(tmp_path):
    block = {
        "smaps": {
            "available": True,
            "large_anonymous_mappings": {"count": 1, "mappings": [{"size": 1}]},
        }
    }
    root = write_one_record(tmp_path, "natural-drain", block)
    entry = native_comparison(root)["natural-drain"]
    pointer = entry["native"]["smaps"]["large_anonymous_mappings"]["mappings"]
    assert pointer == "see evidence/runtime-natural-drain.json"


def test_raw_writes_are_charged_to_the_artifact_writer(tmp_path):
    writer = ArtifactWriter(tmp_path, 4096)
    before = writer._used_bytes
    _write_raw(writer.root, "natural-drain-smaps.txt", b"x" * 1024, 1 << 20)
    assert writer._used_bytes >= before + 1024
```

`native_comparison(root: Path) -> dict` (`evidence.py:133`) takes a directory and returns one entry per checkpoint, so both tests need a written record to read. `write_one_record` is a three-line local helper that writes `{"native": block}` to `root / "runtime-<checkpoint>.json"` and returns `root`. `_write_raw(root: Path, name: str, body: bytes, run_limit: int) -> dict` is at `evidence.py:31`, and the writer's counter is `ArtifactWriter._used_bytes` (`artifacts.py:102`). Both tests take `tmp_path`.

- [ ] **Step 2: Run the tests and confirm RED**

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis -k "already_trimmed or still_inline or charged_to_the_artifact_writer" -q --no-cov
```

- [ ] **Step 3: Only re-point a pointer that is still a list**

```python
            smaps = block.get("smaps")
            if isinstance(smaps, dict):
                large = smaps.get("large_anonymous_mappings")
                # persist_native may already have trimmed this to a pointer at
                # the raw file. That pointer is the useful one: runtime-*.json
                # is precisely the record the mappings were removed from.
                if isinstance(large, dict) and isinstance(large.get("mappings"), list):
                    _trim_unbounded_smaps(
                        smaps, f"see evidence/runtime-{checkpoint}.json"
                    )
```

- [ ] **Step 4: Charge raw writes to the writer**

`ArtifactWriter` has no public way to record a write it did not make: `_used_bytes` is set at `artifacts.py:102` and updated only inside its own write paths (lines 143 and 165). Add one method beside `write_json` that charges an external write and applies the same budget check `write_json` already applies at line 119:

```python
    def charge_external(self, size: int) -> None:
        """Account bytes this writer owns but did not write itself."""
        self._check_write(size, terminal=False)
        self._used_bytes += size
```

Then give `_write_raw` the writer instead of the bare root and call `writer.charge_external(len(body))` after the file lands. `_write_raw` keeps its own `enforce_limit` call: that one bounds the whole run tree, this one keeps the writer's own figure honest. Do not change `write_json`'s behaviour.

- [ ] **Step 5: Run the tests and confirm GREEN**

Run the Step 2 command, then the whole heap-analysis suite:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis -q --no-cov
```

- [ ] **Step 6: Run everything and the checks**

```bash
cd /home/michele/Documenti/nanolab && NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --frozen ruff check packages && uv run --frozen ruff format --check packages
uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab
uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run pre-commit run --all-files
```

- [ ] **Step 7: Commit**

```bash
git add packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py \
  packages/nanolab/src/nanolab/tasks/soak/artifacts.py \
  packages/nanolab/tests/heap_analysis
git commit -m "Keep the trimmed pointer and charge raw writes to the writer"
```

---

## Completion gate

Before claiming completion:

```bash
cd /home/michele/Documenti/nanolab && NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run pre-commit run --all-files
```

The soak suites must pass with no edits to `tests/soak/` beyond the tests these tasks add, which is the evidence that the P24 path is still unchanged.

Findings #3 and #4 are the two that change the outcome of a real run — a killed control plane and a budget failure at the second or third checkpoint. Neither is reproducible on plain Linux Docker with a small heap, so the suite cannot prove them fixed; the next real heap-analysis run is the check that the collection still completes end to end.
