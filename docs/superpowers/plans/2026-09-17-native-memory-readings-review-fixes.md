# Native Memory Readings Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the native-memory review findings without regressing valid large readings, optional-error handling, cancellation, or the default P24 observation path.

**Architecture:** Five tasks cover deadline scope, response-log lifetime, cleanup and receipts, worker states, and evidence accounting. Keep the absolute deadline for the supported local backend; a relative duration would restart the budget after exec startup. Delete response logs only after decoding and validation. Publish raw bytes through the existing artifact writer with a cumulative budget distinct from its JSON-record limit.

**Tech Stack:** Python 3.12, pytest. Tests use controlled clocks, temporary files and fake command runners; no Docker is required.

**Spec:** `docs/superpowers/specs/2026-09-16-control-plane-native-memory-readings-design.md`

Run commands from `/home/michele/Documenti/nanolab`. This is an implementation plan; editing it does not apply the feature changes below.

## Global Constraints

- Preserve the default P24 request, response, command sequence, transport limits and retained logs.
- Keep optional source errors separate from unresolved command completion and infrastructure failure.
- Never issue another diagnostic while JVM command completion is unresolved.
- Preserve `KeyboardInterrupt`/cancellation even if cleanup fails; the receipt must also retain cleanup uncertainty.
- A positive, normally completed command exit remains `CommandCompletedError`. A missing or negative exit code means unresolved completion when completion is required.
- Raw caps remain 8 MiB for smaps and 2 MiB for heap-info. The 1 MiB JSON-record limit must not apply to raw blobs.
- Apply cumulative evidence accounting before publication, under the writer lock, preserving terminal space and accounting for already-published files on failure.
- Keep existing assertions about behavior. Update test call sites only for the explicit writer API change and the response consumer added below; document those updates.
- No change to `_OPERATIONS`, `capture()`, checkpoint ordering, metric interpretation or the artifact tree.

## File map

- `packages/nanolab/assets/soak/diagnostic-worker.py`: failed/not-started read states; None-safe exit-code handling.
- `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py`: response consumer and log lifetime; 10-second owned-target cleanup budget.
- `packages/nanolab/src/nanolab/tasks/soak/artifacts.py`: cumulative-budget check and locked immutable raw publication.
- `packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py`: writer-backed raw publication and preserved trimmed pointers.
- `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py`: note-aware failure rendering and passing the writer to `persist_native`.
- `packages/nanolab/tests/soak/test_diagnostic_helper.py`: deadline, log, cleanup and worker regressions.
- `packages/nanolab/tests/heap_analysis/test_native_evidence.py`: raw accounting, publication failure and pointer tests. This is the existing test file; do not create a parallel `test_evidence.py`.
- `packages/nanolab/tests/heap_analysis/test_runtime.py`: cancellation receipt and existing session integration.
- `docs/heap-analysis.md`: local-clock scope and explicit source-state semantics.

---

### Task 1: Bound the supported local-clock contract (#3)

**Files:**
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`
- Modify: `docs/heap-analysis.md`

**Interfaces:** Keep `memory_deadline_s`, the absolute deadline computed after host identity checks; no `memory_budget_s` protocol change. The same deadline covers procfs and jcmd. This task establishes regression tests and documents supported scope rather than claiming cross-kernel Docker support.

The current helper requires a local `unix:///` Docker socket and verifies Docker PIDs through local `/proc`. A forwarded socket or VM-backed daemon is not made supported by changing a timeout field. Cross-clock support would require a separate transport/identity design. For the supported same-clock backend, the current absolute deadline correctly consumes exec startup delay. Sending `remaining - margin` and starting it again in the worker would lose that property.

- [ ] **Step 1: Add delayed-start and shared-deadline regression tests**

The existing test file defines `worker()`, `probe()`, `TARGET`, and `memory_owner(monkeypatch, tmp_path, ...)`, which returns `(owner, calls, cleanup)`. Use the real signature and return shape:

```python
def test_local_memory_request_keeps_the_remaining_absolute_deadline(
    monkeypatch, tmp_path
):
    import nanolab.tasks.soak.diagnostic_helper as helper

    owner, calls, _ = memory_owner(monkeypatch, tmp_path)
    now = [100.0]
    monkeypatch.setattr(helper.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(owner, "_check_target", lambda: now.__setitem__(0, 104.0))
    owner.read_memory(timeout_s=5, include_smaps=True)
    argv, options = calls[-1]
    cfg = json.loads(base64.b64decode(argv[-1]))
    assert "memory_budget_s" not in cfg
    assert 104.0 < cfg["memory_deadline_s"] < 105.0
    assert options["timeout_s"] == 1.0


def test_late_worker_does_not_restart_the_collection_budget(monkeypatch):
    module = worker()
    monkeypatch.setattr(module.time, "monotonic", lambda: 112.0)
    monkeypatch.setattr(module, "identity", lambda *a, **k: probe())
    monkeypatch.setattr(module, "read_proc", lambda *a: pytest.fail("expired read"))
    monkeypatch.setattr(module, "jcmd", lambda *a, **k: pytest.fail("expired attach"))
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "memory_deadline_s": 109.0,
        }
    )
    assert all(value == "not_started" for value in result["completion"].values())
    assert result["heap_info"] is None


def test_procfs_and_heap_info_share_one_deadline(monkeypatch):
    module = worker()
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module, "identity", lambda *a, **k: probe())

    def read(path, limit):
        now[0] += 3.0
        return "body"

    monkeypatch.setattr(module, "read_proc", read)
    monkeypatch.setattr(module, "jcmd", lambda *a, **k: pytest.fail("late attach"))
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_heap_info": True,
            "memory_deadline_s": 105.0,
        }
    )
    assert result["completion"]["heap_info"] == "not_started"
```

- [ ] **Step 2: Run the regressions against the existing implementation**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k 'remaining_absolute_deadline or late_worker or share_one_deadline' -q --no-cov
```

Expected: PASS already. These establish the behavior to preserve; they are not evidence that a new relative-budget implementation fixes anything. Do not manufacture a RED stage for this scope clarification.

- [ ] **Step 3: Document the clock-domain limit**

Add to `docs/heap-analysis.md`:

```markdown
The diagnostic helper uses the local Linux Docker backend and validates target
PIDs through the host's `/proc`. Its absolute monotonic deadline assumes this
supported shared clock domain and includes exec startup delay. A forwarded
Docker socket or a daemon in another kernel is not supported by this protocol.
The host deadline remains the outer bound; a late worker may return missing
readings or be interrupted, and unresolved JVM completion follows owned-target
cleanup. No timeout representation alone guarantees timely acknowledgement.
```

- [ ] **Step 4: Commit the scope and regression coverage**

```bash
git add docs/heap-analysis.md packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Document local deadline scope and cover delayed helper startup"
```

---

### Task 2: Delete response logs only after acceptance (#4)

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py`
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:** `_DockerCommands.run(..., consume=None)` retains the existing string result and log when `consume` is absent. When present, it passes the text to the consumer, removes the log only after successful acceptance, and returns the consumer's result. `_OwnedDockerHelper.read_memory` uses the consumer only for opt-in requests. Preserve `options["timeout_s"]` and the 64 MiB limit.

Do not unlink before `log.read_text()`. A zero Docker exit is not enough: JSON decoding, identity validation and JVM completion validation must also succeed. A failed command or failed consumer retains its log. Accepted source errors remain in the returned structured evidence and are published by the session.

- [ ] **Step 1: Add tests using the real `_DockerCommands.run`**

Add this complete helper beside `memory_owner`; it reuses the existing owner identity stubs but replaces the transport with the genuine run method. `spec`, `probe`, `TARGET`, `asdict`, `json` and `pytest` already exist in this test module.

```python
def logged_memory_owner(monkeypatch, tmp_path, *, raw=None, returncode=0):
    from threading import Event
    from nanolab.tasks.soak import diagnostic_helper as helper
    from nanolab.tasks.soak.processes import OwnedCommandResult

    owner, _, cleanup = memory_owner(monkeypatch, tmp_path)
    payload = (
        raw
        if raw is not None
        else json.dumps(
            {
                "schema": "nanolab-soak-memory-helper-v1",
                "target": asdict(TARGET),
                "before": probe(),
                "after": probe(),
                "errors": {},
                "status": "VmRSS: 4 kB\n",
                "smaps_rollup": "Pss: 4 kB\n",
                "smaps": "body",
                "heap_info": None,
                "completion": {"heap_info": "completed"},
            }
        )
    )

    class Runner:
        def __init__(self, *args, **kwargs):
            self.log = kwargs["log_path"]

        def run(self):
            self.log.write_text(payload)
            return OwnedCommandResult(returncode, False, True, ended_s=1.0)

    monkeypatch.setattr(helper, "OwnedCommandRunner", Runner)
    owner.commands = helper._DockerCommands(owner.spec, tmp_path, Event())
    return owner, cleanup


def test_accepted_optional_response_removes_duplicate_log(monkeypatch, tmp_path):
    owner, _ = logged_memory_owner(monkeypatch, tmp_path)
    assert owner.read_memory(include_smaps=True)["smaps"] == "body"
    assert not list(tmp_path.glob("docker-*.log"))


@pytest.mark.parametrize("raw,returncode", [("not JSON", 0), ("command failed", 1)])
def test_rejected_optional_response_keeps_log(monkeypatch, tmp_path, raw, returncode):
    owner, _ = logged_memory_owner(
        monkeypatch, tmp_path, raw=raw, returncode=returncode
    )
    with pytest.raises((ValueError, RuntimeError)):
        owner.read_memory(include_smaps=True)
    assert list(tmp_path.glob("docker-*.log"))


def test_unresolved_response_keeps_log_even_with_zero_exit(monkeypatch, tmp_path):
    payload = {
        "schema": "nanolab-soak-memory-helper-v1",
        "target": asdict(TARGET),
        "before": probe(),
        "after": probe(),
        "completion": {"heap_info": "unresolved"},
    }
    owner, cleanup = logged_memory_owner(monkeypatch, tmp_path, raw=json.dumps(payload))
    with pytest.raises(RuntimeError, match="completion unresolved"):
        owner.read_memory(include_heap_info=True)
    assert list(tmp_path.glob("docker-*.log"))
    assert cleanup == ["cancel-target", "close"]


def test_legacy_memory_log_is_retained(monkeypatch, tmp_path):
    owner, _ = logged_memory_owner(monkeypatch, tmp_path)
    owner.read_memory()
    assert list(tmp_path.glob("docker-*.log"))
```

- [ ] **Step 2: Run the log tests and confirm the accepted-response test fails**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k 'duplicate_log or keeps_log or memory_log_is_retained' -q --no-cov
```

- [ ] **Step 3: Add the consumer without changing default calls**

Extend the run signature:

```python
    def run(self, args, timeout_s=20.0, *, cleanup=False, limit=1024 * 1024, consume=None):
```

Replace its final `return log.read_text()` with:

```python
        text = log.read_text()
        if consume is None:
            return text
        accepted = consume(text)
        log.unlink(missing_ok=True)
        return accepted
```

Keep all command failure checks before this block. The callback decodes and validates this response, including the existing target inspection; final artifacts are published later by the session.

- [ ] **Step 4: Consume the opt-in response after validation**

Inside `_OwnedDockerHelper.read_memory`, replace the current block from `jvm_may_be_running = include_heap_info` through `return data` with:

```python
def accept(text):
    nonlocal jvm_may_be_running
    data = json.loads(text)
    self._check_target()
    before, after = data.get("before", {}), data.get("after", {})
    if (
        data.get("schema") != "nanolab-soak-memory-helper-v1"
        or data.get("target") != asdict(self.spec.target)
        or before != after
        or before.get("target_start_ticks") != self.process_identity["start_ticks"]
        or before.get("target_pid") != 1
        or before.get("uid") != self.spec.uid
        or before.get("target_uid") != self.spec.uid
        or not before.get("pid_namespace")
        or before["pid_namespace"] != before.get("target_pid_namespace")
    ):
        raise ValueError("memory response process/namespace identity changed")
    if include_heap_info:
        state = data.get("completion", {}).get("heap_info")
        if state not in {"completed", "not_started"}:
            raise MemoryCommandUnresolved("in-JVM command completion unresolved")
        jvm_may_be_running = False
    return data


jvm_may_be_running = include_heap_info
if include_smaps or include_heap_info:
    return self.commands.run(argv, **options, consume=accept)
return accept(self.commands.run(argv, **options))
```

`_check_target()` uses Docker inspection, so preserve its existing position relative to validation but do not recursively use the same response consumer: those inspection calls use default `run` and retain their logs. The consumer applies only to the memory exec response. If any inspection/validation fails, the memory response log remains.

Update the existing `memory_owner` fake's final return to execute the callback when present; this is a test-double API adjustment, not a weakened assertion:

```python
            consume = kwargs.get("consume")
            return consume(response) if consume is not None else response
```

Keep its JSON-size check before these lines, and keep all existing default-wire/timeout assertions.

- [ ] **Step 5: Run helper tests and commit**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -q --no-cov
git add packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Discard memory response logs after validation succeeds"
```

---

### Task 3: Preserve cancellation and expose cleanup uncertainty (#2, #6)

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py`
- Modify: `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py`
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`
- Test: `packages/nanolab/tests/heap_analysis/test_runtime.py`

**Interfaces:** No exception-type change. Keep the original opt-in exception and its cleanup note. Add `_failure_reason(error: BaseException) -> str` in the heap-analysis runtime to render notes in the receipt; `RunControlPlaneHeapAnalysis.run` must still distinguish `Exception` from cancellation.

- [ ] **Step 1: Add deterministic budget and cancellation tests**

In `test_diagnostic_helper.py`:

```python
def test_unresolved_memory_cleanup_receives_ten_seconds(monkeypatch, tmp_path):
    from nanolab.tasks.soak import diagnostic_helper as helper

    owner, _, _ = memory_owner(monkeypatch, tmp_path, completion="unresolved")
    monkeypatch.setattr(helper.time, "monotonic", lambda: 100.0)
    budgets = []
    monkeypatch.setattr(
        owner,
        "_cancel_remote",
        lambda: budgets.append(owner.commands.cleanup_deadline - 100.0),
    )
    with pytest.raises(helper.MemoryCommandUnresolved):
        owner.read_memory(include_heap_info=True)
    assert budgets == [10.0]


def test_cleanup_failure_keeps_the_original_interrupt(monkeypatch, tmp_path):
    owner, _, _ = memory_owner(monkeypatch, tmp_path)
    interrupted = KeyboardInterrupt("user cancelled")

    def fail_read(*args, **kwargs):
        raise interrupted

    def fail_cleanup():
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(owner.commands, "run", fail_read)
    monkeypatch.setattr(owner, "_cancel_remote", fail_cleanup)
    with pytest.raises(KeyboardInterrupt) as raised:
        owner.read_memory(include_heap_info=True)
    assert raised.value is interrupted
    assert any("cleanup unconfirmed" in note for note in interrupted.__notes__)
```

In `test_runtime.py`, use its existing `FakeSession`, `build`, `measure` and `evidence` helpers:

```python
def test_cleanup_note_survives_cancellation_receipt(tmp_path):
    class Interrupted(FakeSession):
        def load(self, phase, duration_s):
            if phase == "steady":
                error = KeyboardInterrupt("user cancelled")
                error.add_note("memory cleanup unconfirmed: daemon unavailable")
                raise error
            return super().load(phase, duration_s)

    task, _, run_dir = build(tmp_path, Interrupted([], evidence(tmp_path)))
    with pytest.raises(KeyboardInterrupt):
        measure(task)
    terminal = json.loads((run_dir / "terminal.json").read_text())
    report = json.loads((run_dir / "report.json").read_text())
    assert terminal["status"] == "ABORTED"
    assert "cleanup unconfirmed" in json.dumps(terminal)
    assert any("cleanup unconfirmed" in reason for reason in report["reasons"])
```

- [ ] **Step 2: Run the tests and confirm budget/receipt failures**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py packages/nanolab/tests/heap_analysis/test_runtime.py -k 'receives_ten_seconds or original_interrupt or cancellation_receipt' -q --no-cov
```

The original-interrupt assertion should already pass. Preserve it while fixing the other failures.

- [ ] **Step 3: Match the owned-target cancellation budget**

In the `read_memory` exception handler:

```python
                self.commands.cleanup_deadline = time.monotonic() + (
                    10.0 if jvm_may_be_running else 5.0
                )
```

Keep the existing opt-in `error.add_note(...)` and `raise error from cleanup_error`. Do not wrap `KeyboardInterrupt` in `RuntimeError`; chaining retains a cause but does not retain catch behavior.

- [ ] **Step 4: Render cleanup notes at the receipt boundary**

Add to the heap-analysis runtime:

```python
def _failure_reason(error: BaseException) -> str:
    """Keep cleanup uncertainty visible without changing exception identity."""
    notes = [str(note) for note in getattr(error, "__notes__", ())]
    cleanup = [note for note in notes if "cleanup unconfirmed" in note]
    other = [note for note in notes if "cleanup unconfirmed" not in note]
    parts = cleanup + [f"{type(error).__name__}: {error}"] + other
    return "; ".join(parts)[:1024]
```

Replace only the two failure-formatting branches in `RunControlPlaneHeapAnalysis.run`:

```python
        except Exception as error:
            reasons.append(_failure_reason(error))
        except BaseException as error:
            interrupted = error
            reasons.append(_failure_reason(error))
```

Keep the existing final re-raise and `aborted=interrupted is not None` behavior unchanged. No shared soak receipt changes are needed.

- [ ] **Step 5: Verify and commit**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py packages/nanolab/tests/heap_analysis/test_runtime.py -q --no-cov
git add packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py packages/nanolab/tests/soak/test_diagnostic_helper.py packages/nanolab/tests/heap_analysis/test_runtime.py
git commit -m "Preserve cancellation and report unconfirmed memory cleanup"
```

---

### Task 4: Distinguish failed reads and acknowledged command errors (#5, #8)

**Files:**
- Modify: `packages/nanolab/assets/soak/diagnostic-worker.py`
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`
- Modify: `docs/heap-analysis.md`

**Interfaces:** Procfs completion states are `not_started` (deadline prevented the attempt), `failed` (attempt raised), and `completed` (read returned). This does not redefine heap-info command completion: a positive acknowledged exit remains completed with an error. Missing/negative exit codes remain unresolved when `require_completion=True`.

- [ ] **Step 1: Add tests that reach the actual comparison**

```python
def test_failed_procfs_read_has_a_distinct_state(monkeypatch):
    module = worker()
    monkeypatch.setattr(module, "identity", lambda *a, **k: probe())

    def read(path, limit):
        if path.name == "smaps":
            raise ValueError("procfs evidence exceeds read bound")
        return "body"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "memory_deadline_s": time.monotonic() + 30,
        }
    )
    assert result["completion"]["smaps"] == "failed"
    assert result["completion"]["status"] == "completed"
    assert "exceeds read bound" in result["errors"]["smaps"]


@pytest.mark.parametrize(
    "code,exception_name",
    [
        (None, "CommandCompletionUnresolved"),
        (-9, "CommandCompletionUnresolved"),
        (1, "CommandCompletedError"),
        (0, None),
    ],
)
def test_command_exit_code_preserves_completion_semantics(
    monkeypatch, tmp_path, code, exception_name
):
    import sys
    from nanolab.tasks.soak import processes

    module = worker()
    result = processes.OwnedCommandResult(
        returncode=code,
        forced_stop=False,
        reaped=True,
        ended_s=1.0,
    )

    class Runner:
        def __init__(self, *args, **kwargs):
            kwargs["log_path"].write_text("acknowledged output")

        def run(self):
            return result

    monkeypatch.setitem(sys.modules, "processes", processes)
    monkeypatch.setattr(processes, "OwnedCommandRunner", Runner)
    if exception_name is None:
        assert (
            module.command(
                ("unused",), time.monotonic() + 30, tmp_path, require_completion=True
            )
            == "acknowledged output"
        )
    else:
        with pytest.raises(getattr(module, exception_name)):
            module.command(
                ("unused",), time.monotonic() + 30, tmp_path, require_completion=True
            )
```

The None case sets `forced_stop=False`, `reaped=True`, and a non-None `ended_s`, so it reaches the comparison. `OwnedCommandResult` does have an `errors` field; its default is empty. Do not patch a nonexistent worker-level `OwnedCommandRunner`: `command` imports it from `processes` inside the function.

- [ ] **Step 2: Run and confirm the None/state cases fail**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k 'distinct_state or exit_code_preserves' -q --no-cov
```

- [ ] **Step 3: Set read states around the actual attempt**

Replace the start of the procfs loop's try block with:

```python
        state = "not_started"
        try:
            if opt_in and time.monotonic() >= deadline:
                raise TimeoutError("memory collection deadline exhausted")
            state = "failed"
            result[name] = read_proc(Path("/proc/1") / name, limit)
            state = "completed"
```

Keep the existing catch and interval publication. Legacy responses still publish neither completion nor intervals.

- [ ] **Step 4: Guard None without absorbing positive exit codes**

Replace the negative-return-code term in the completion-required condition with:

```python
        or result.returncode is None
        or result.returncode < 0
```

Keep the following `if require_completion and result.returncode != 0: raise CommandCompletedError(...)` unchanged. Do not put `!= 0` in the unresolved condition.

- [ ] **Step 5: Document the states, verify and commit**

Add to `docs/heap-analysis.md`:

```markdown
For procfs sources, `not_started` means the deadline prevented the attempt,
`failed` means the attempted read raised an error, and `completed` means the
read returned. Source availability and parsing errors are reported separately.
For JVM commands, completion acknowledges the command lifetime: an ordinary
positive error exit can be completed with an error; a missing or signal exit
does not establish completion.
```

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -q --no-cov
git add packages/nanolab/assets/soak/diagnostic-worker.py packages/nanolab/tests/soak/test_diagnostic_helper.py docs/heap-analysis.md
git commit -m "Distinguish failed reads from unresolved JVM commands"
```

---

### Task 5: Account raw publication before writing and preserve pointers (#1, #7)

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/artifacts.py`
- Modify: `packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py`
- Modify: `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py`
- Test: `packages/nanolab/tests/heap_analysis/test_native_evidence.py`
- Test: `packages/nanolab/tests/heap_analysis/test_runtime.py`

**Interfaces:** Add `ArtifactWriter.write_blob(directory, name, body) -> Path` for raw evidence, with locked cumulative accounting but no JSON-record cap. Change `_write_raw` and `persist_native` to receive `ArtifactWriter`. `native_comparison(root)` remains path-based. No after-the-fact `charge_external` method is added.

- [ ] **Step 1: Add pointer and raw-accounting tests**

Add imports for `ArtifactWriter` and `_write_raw` to the existing evidence test module:

```python
def write_one_record(root, checkpoint, block):
    (root / f"runtime-{checkpoint}.json").write_text(json.dumps({"native": block}))
    return root


@pytest.mark.parametrize(
    "mappings,expected",
    [
        (
            "see evidence/native/natural-drain-smaps.txt",
            "see evidence/native/natural-drain-smaps.txt",
        ),
        ([{"size": 1}], "see evidence/runtime-natural-drain.json"),
    ],
)
def test_comparison_preserves_existing_pointer(tmp_path, mappings, expected):
    root = write_one_record(
        tmp_path,
        "natural-drain",
        {
            "smaps": {
                "available": True,
                "large_anonymous_mappings": {"count": 1, "mappings": mappings},
            },
        },
    )
    block = native_comparison(root)["natural-drain"]["native"]
    assert block["smaps"]["large_anonymous_mappings"]["mappings"] == expected


def test_valid_large_raw_is_charged_without_json_record_cap(tmp_path):
    writer = ArtifactWriter(tmp_path / "evidence", 16 * 1024 * 1024)
    body = b"x" * (2 * 1024 * 1024)
    receipt = _write_raw(writer, "natural-drain-smaps.txt", body, 16 * 1024 * 1024)
    assert (writer.root / receipt["path"]).read_bytes() == body
    assert writer._used_bytes == len(body)
    with pytest.raises(ArtifactLimitExceededError, match="individual evidence record"):
        writer.write_json("too-large.json", {"data": "x" * (2 * 1024 * 1024)})


def test_writer_refuses_raw_before_publication_when_budget_is_exhausted(tmp_path):
    writer = ArtifactWriter(tmp_path / "evidence", 4096)
    with pytest.raises(ArtifactLimitExceededError):
        _write_raw(writer, "natural-drain-smaps.txt", b"x" * 4096, 1 << 20)
    assert writer._used_bytes == 0
    assert not list(writer.root.rglob("*.txt"))


def test_raw_bytes_reduce_the_budget_for_later_json(tmp_path):
    writer = ArtifactWriter(tmp_path / "evidence", 8192)
    _write_raw(writer, "natural-drain-smaps.txt", b"x" * 6500, 1 << 20)
    with pytest.raises(ArtifactLimitExceededError, match="budget exhausted"):
        writer.write_json("next.json", {"data": "x" * 1000})
    assert not (writer.root / "next.json").exists()


def test_failed_raw_publication_does_not_charge_missing_bytes(monkeypatch, tmp_path):
    from nanolab.tasks.soak import artifacts

    writer = ArtifactWriter(tmp_path / "evidence", 1 << 20)

    def fail_link(*args, **kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(artifacts.os, "link", fail_link)
    with pytest.raises(OSError, match="publication failure"):
        _write_raw(writer, "natural-drain-smaps.txt", b"body", 1 << 20)
    assert writer._used_bytes == 0
    assert not list(writer.root.rglob("*.txt"))
    assert not list(writer.root.rglob(".pending-*"))


def test_published_raw_stays_charged_when_later_hashing_fails(monkeypatch, tmp_path):
    from nanolab.tasks.heap_analysis import evidence

    writer = ArtifactWriter(tmp_path / "evidence", 1 << 20)

    def fail_hash(path):
        raise OSError("synthetic hashing failure")

    monkeypatch.setattr(evidence, "describe_artifact", fail_hash)
    with pytest.raises(OSError, match="hashing failure"):
        _write_raw(writer, "natural-drain-smaps.txt", b"body", 1 << 20)
    assert writer._used_bytes == 4
    assert (writer.root / "native/natural-drain-smaps.txt").read_bytes() == b"body"
```

- [ ] **Step 2: Run the new tests and confirm failures on missing blob support/pointer preservation**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis/test_native_evidence.py -k 'existing_pointer or large_raw or refuses_raw or raw_bytes_reduce or raw_publication or published_raw' -q --no-cov
```

- [ ] **Step 3: Separate cumulative budget checks from JSON-record checks**

In `ArtifactWriter`, add `_check_budget` and replace `_check_write` with:

```python
def _check_budget(self, size: int, *, terminal: bool = False) -> None:
    if self._closed:
        raise RuntimeError("artifact writer is closed")
    budget = self.limit_bytes if terminal else self.limit_bytes - self._reserve
    if self._used_bytes + size > budget:
        raise ArtifactLimitExceededError(
            "artifact budget exhausted; terminal space is reserved"
        )


def _check_write(self, size: int, *, terminal: bool = False) -> None:
    if self._closed:
        raise RuntimeError("artifact writer is closed")
    if size > MAX_RECORD_BYTES:
        raise ArtifactLimitExceededError(
            "individual evidence record exceeds its size limit"
        )
    self._check_budget(size, terminal=terminal)
```

JSON append/write behavior, exception order and messages stay unchanged. The new blob path calls only `_check_budget`.

- [ ] **Step 4: Publish and account raw bytes under one writer lock**

Add this method beside `write_json`. Existing imports already provide `Path`, `os`, `tempfile`, and `_NAME`.

```python
    def write_blob(self, directory: str, name: str, body: bytes) -> Path:
        """Publish immutable raw evidence without imposing the JSON-record cap."""
        parent = self._target(directory)
        if _NAME.fullmatch(name) is None:
            raise ValueError("artifact name must be a single safe path component")
        target = parent / name
        with self._lock:
            self._check_budget(len(body))
            if parent.is_symlink():
                raise ValueError("raw evidence directory cannot be a symbolic link")
            parent.mkdir(exist_ok=True, mode=0o700)
            fd, filename = tempfile.mkstemp(prefix=".pending-", dir=parent)
            temporary = Path(filename)
            published = False
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, target)
                published = True
            finally:
                # A later cleanup error must not leave a published file uncharged.
                if published:
                    self._used_bytes += len(body)
                temporary.unlink(missing_ok=True)
        return target
```

Do not call `write_json` from inside this lock. Existing methods retain their locking and defaults. Source-specific raw caps remain enforced by `persist_native`.

- [ ] **Step 5: Pass the writer through the complete call chain**

Import `ArtifactWriter` in `heap_analysis/evidence.py`, then replace `_write_raw` with:

```python
def _write_raw(writer: ArtifactWriter, name: str, body: bytes, run_limit: int) -> dict:
    root = writer.root
    used = enforce_limit(root.parent, run_limit)
    if used + len(body) + _TERMINAL_RESERVE > run_limit:
        raise ArtifactLimitExceededError(
            "native evidence exceeds cumulative run budget"
        )
    target = writer.write_blob("native", name, body)
    enforce_limit(root.parent, run_limit)
    return {**describe_artifact(target), "path": str(target.relative_to(root))}
```

Remove now-unused `os` and `tempfile` imports from this evidence module. Change the start of `persist_native` to:

```python
def persist_native(
    writer: ArtifactWriter, checkpoint: str, response: dict, artifact_limit_bytes: int
) -> dict:
    if checkpoint not in CHECKPOINTS:
        raise ValueError("unknown memory checkpoint")
```

Remove the old `root.mkdir(...)` line. Keep the rest of its source-cap, parsing and summary logic, but call `_write_raw(writer, ...)` instead of `_write_raw(root, ...)`. The writer already owns its directory; do not construct another writer inside this function.

In `LocalHeapAnalysisSession.observe`, change only this argument:

```python
native = persist_native(
    self._writer,
    checkpoint,
    readings,
    self._config.artifact_limit_bytes,
)
```

Keep its cumulative checks around the runtime JSON: the writer accounts its own outputs, while `enforce_limit` also counts other run artifacts. Replace the stale comment above those checks with:

```python
        # Raw blobs are charged to the writer without its JSON-record cap.
        # The cumulative run check also includes artifacts from other producers.
```

Update every existing `persist_native` test to create one writer before adding files or symlinks under its root, and pass that writer. The exact conversion pattern is:

```python
    root = tmp_path / "evidence"
    writer = ArtifactWriter(root, 1048576)
    block = persist_native(writer, "natural-drain", raw, 1048576)
```

For the cumulative-budget test use `ArtifactWriter(root, 1024 + 4096)` before creating the sibling `other-artifact`; keep the original low run limit. For the symlink test create the writer before `root/native` is linked. Reuse the same writer across checkpoints. Keep the existing retained-content, symlink, summary-size and category assertions unchanged. The runtime integration test continues to construct its writer through `local_session`.

- [ ] **Step 6: Preserve an existing raw pointer while removing inline details**

Change `_trim_unbounded_smaps`, not just its caller:

```python
    smaps.pop("mapping_details", None)
    large = smaps.get("large_anonymous_mappings")
    if isinstance(large, dict) and isinstance(large.get("mappings"), list):
        large["mappings"] = pointer
```

Both callers can keep invoking it unconditionally on a smaps dictionary. Existing string pointers remain intact and any remaining inline `mapping_details` is still removed.

- [ ] **Step 7: Run evidence, runtime and shared-writer tests**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis packages/nanolab/tests/soak -q --no-cov
```

- [ ] **Step 8: Commit the complete API change**

```bash
git add packages/nanolab/src/nanolab/tasks/soak/artifacts.py packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py packages/nanolab/tests/heap_analysis/test_native_evidence.py packages/nanolab/tests/heap_analysis/test_runtime.py
git commit -m "Account raw evidence before publication and preserve trimmed pointers"
```

---

## Completion gate

```bash
NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --frozen ruff check packages
uv run --frozen ruff format --check packages
uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab
uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run pre-commit run --all-files
```

Before claiming implementation complete, confirm: 2–8 MiB raw evidence remains valid; later JSON sees those charged bytes; failed publication has consistent accounting; accepted response logs disappear while rejected-response logs remain; exit code 1 never becomes unresolved solely because it is nonzero; cancellation remains `ABORTED` with cleanup uncertainty visible; failed reads are not labeled `not_started`; already-trimmed pointers still reach the raw artifact.

No dedicated live run is added. These synthetic regressions can establish the logic fixes on plain Linux; the next real heap-analysis run checks deployment integration and operational timing. Do not claim support for another clock domain or infer unchanged wire behavior solely from old suite results.
