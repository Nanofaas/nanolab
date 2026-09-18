# Control-plane Native Memory Readings Implementation Plan

> **Correction (2026-09-17).** Task 4's `_HEAP`/`_METASPACE` regexes and every `heap_info`
> fixture below were written for pre-JDK-25 `GC.heap_info` output. JDK 25 prints
> `garbage-first heap   total reserved <R>K, committed <C>K, used <U>K [...]` and no Metaspace
> line at all, so those regexes match nothing on the JDK 25 target this code requires
> (`diagnostic_helper.py` refuses any non-`25.` version, and the helper image is
> `eclipse-temurin:25-jdk`). A run built from Task 4 as written records the heap reading as
> silently unavailable. **Do not re-execute that parser or those fixtures.** The corrected
> parser and real captures now live in
> `packages/nanolab/src/nanolab/tasks/heap_analysis/native.py` and
> `packages/nanolab/tests/heap_analysis/test_native.py`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two opt-in memory readings — `GC.heap_info` and the full `/proc/<pid>/smaps` — to the heap-analysis checkpoints, with an aggregate summary, without changing the P24 soak path.

**Architecture:** Both readings use the existing observation channel (`read_memory()` → `docker exec` → the worker's `memory(cfg)` mode). Command completion is explicit; unresolved JVM commands use existing owned-target cancellation before helper removal. They are requested through explicit keyword options that default to off; when both are off the worker request, response shape, read sequence and 1 MiB transport limit stay exactly as they are today. Aggregation lives in `tasks/heap_analysis/native.py`; bounded raw storage and report assembly live in `tasks/heap_analysis/evidence.py`. Shared soak defaults remain unchanged.

**Tech Stack:** Python 3.12, pytest, Docker (not needed by any test in this plan), JDK 25 `jcmd`, Linux procfs.

**Spec:** `docs/superpowers/specs/2026-09-16-control-plane-native-memory-readings-design.md`

Run commands from `/home/michele/Documenti/nanolab`. This document is a plan; none of the feature code below is implemented by editing it.

## Global Constraints

- Preserve the default P24 soak request, response and collection behavior: no new reads, commands, fields or output limits when options are absent.
- No change to `_OPERATIONS`, to `capture()`, or to the diagnostic evidence contract.
- Unavailable optional readings do not themselves change the run's status; existing target-identity and diagnostic completion requirements still apply.
- Optional evidence errors must leave the helper usable for the mandatory GC and dump operations.
- Killing `jcmd` is not proof that the command inside the JVM has completed. Do not issue a later diagnostic while command completion is unresolved.
- Truncation or an over-limit smaps is marked explicitly as partial/unavailable. Publish no totals derived from incomplete smaps.
- "Large" means virtual mapping size at least 32 MiB, using a named constant. Report qualifying mappings individually. Do not report a glibc arena count or attribute these bytes to glibc.
- Retain the kernel's residency categories: `RssAnon`, `RssFile`, `RssShmem` from status and `Pss_Anon`, `Pss_File`, `Pss_Shmem` from smaps_rollup. Missing fields stay unavailable, never zero.
- Do not classify resident pages solely by a mapping's pathname: a file mapping can contain anonymous copy-on-write pages.
- Normalize parsed values to bytes; mark unrecognized or missing fields unavailable rather than zero.
- Report measurements and missing evidence without recommending a cause or tuning change.
- Raw smaps byte cap: 8 MiB (`8388608`). Command output bound: 2 MiB (`2097152`). Opt-in serialized response ceiling: 64 MiB (`67108864`). Legacy default transport limit: 1 MiB (`1048576`).

---

## File map

- Modify `packages/nanolab/assets/soak/diagnostic-worker.py`: opt-in smaps read and opt-in `GC.heap_info` inside `memory(cfg)`.
- Modify `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py`: opt-in keyword options and derived response budget on the three `read_memory` definitions.
- Create `packages/nanolab/src/nanolab/tasks/heap_analysis/native.py`: parse `heap_info`, parse smaps, build the summary.
- Create `packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py`: bounded raw artifact publication and explicit checkpoint comparisons.
- Create `packages/nanolab/tests/heap_analysis/test_native_evidence.py`: provenance, cumulative budget and missing-checkpoint tests.
- Modify `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py`: request the readings in `observe()`, write raw artifacts and the `native` block, move `after-final-gc` ahead of the final dump, extend `report.json`.
- Modify `packages/nanolab/tests/soak/test_diagnostic_helper.py`: worker and transport tests.
- Create `packages/nanolab/tests/heap_analysis/test_native.py`: aggregator tests.
- Modify `packages/nanolab/tests/heap_analysis/test_runtime.py`: checkpoint order and session wiring.
- Modify `docs/heap-analysis.md`: document the readings, their artifacts and their limits.

---

### Task 1: Opt-in smaps read in the worker

**Files:**
- Modify: `packages/nanolab/assets/soak/diagnostic-worker.py` (function `memory`, currently at line 118)
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: the existing `read_proc(path, limit)` helper and the `cfg` dict already passed to `memory`.
- Produces: `memory(cfg)` honouring `cfg["include_smaps"]`; response key `"smaps"` and, on failure, `result["errors"]["smaps"]`. Task 3 sends the flag; Task 4 parses the value.

The existing loop reads two files with a per-file cap and records per-file failures without interrupting its siblings:

```python
    for name, limit in (("status", 65536), ("smaps_rollup", 262144)):
        try:
            result[name] = read_proc(Path("/proc/1") / name, limit)
        except (OSError, ValueError) as error:
            result[name] = None
            result["errors"][name] = f"{type(error).__name__}: {error}"
```

`read_proc` already raises `ValueError("procfs evidence exceeds read bound")` when the file exceeds its cap, so an over-limit smaps becomes a recorded error and never a truncated string.

- [ ] **Step 1: Write the failing tests**

Add to `packages/nanolab/tests/soak/test_diagnostic_helper.py`, following the existing `test_memory_worker_preserves_raw_pss_and_permission_denial` pattern (it loads the script with the file-local `worker()` helper and monkeypatches `read_proc` and `identity`):

```python
def test_memory_worker_omits_smaps_unless_requested(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    requested = []

    def read(path, limit):
        requested.append((path.name, limit))
        return "Name:\tjava\n"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory({"target": asdict(TARGET)})
    assert requested == [("status", 65536), ("smaps_rollup", 262144)]
    assert "smaps" not in result
    assert result["errors"] == {}


def test_memory_worker_reads_smaps_when_requested(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    requested = []

    def read(path, limit):
        requested.append((path.name, limit))
        return "7f00-7f01 rw-p 0 00:00 0\nSize: 4 kB\n"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory({"target": asdict(TARGET), "include_smaps": True})
    assert requested == [
        ("status", 65536),
        ("smaps_rollup", 262144),
        ("smaps", 8388608),
    ]
    assert result["smaps"] == "7f00-7f01 rw-p 0 00:00 0\nSize: 4 kB\n"


def test_memory_worker_records_an_over_limit_smaps_without_losing_siblings(
    monkeypatch,
):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)

    def read(path, limit):
        if path.name == "smaps":
            raise ValueError("procfs evidence exceeds read bound")
        return f"{path.name}-body"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory({"target": asdict(TARGET), "include_smaps": True})
    assert result["smaps"] is None
    assert "exceeds read bound" in result["errors"]["smaps"]
    assert result["status"] == "status-body"
    assert result["smaps_rollup"] == "smaps_rollup-body"
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k memory_worker -q --no-cov
```

Expected: the two new opt-in tests fail because `memory` ignores `include_smaps`; `test_memory_worker_omits_smaps_unless_requested` already passes.

- [ ] **Step 3: Build the read list from the options**

Replace the fixed tuple with a list built from `cfg`, leaving the default path identical:

```python
    reads = [("status", 65536), ("smaps_rollup", 262144)]
    if cfg.get("include_smaps"):
        reads.append(("smaps", 8388608))
    for name, limit in reads:
        try:
            result[name] = read_proc(Path("/proc/1") / name, limit)
        except (OSError, ValueError) as error:
            result[name] = None
            result["errors"][name] = f"{type(error).__name__}: {error}"
```

- [ ] **Step 4: Run the tests and confirm GREEN**

Run the Step 2 command. Expected: all three pass.

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab/assets/soak/diagnostic-worker.py \
  packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Read the full smaps when the caller asks for it"
```

---

### Task 2: Observe heap-info with explicit completion state

**Files:**
- Modify: `packages/nanolab/assets/soak/diagnostic-worker.py`
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: `include_smaps`, `include_heap_info`, and `memory_deadline_s` (absolute monotonic deadline from the host on this local Linux Docker backend).
- Produces: opt-in `intervals` and `completion` dictionaries; `heap_info` text or `None`; bounded per-source errors. Heap-info completion is `completed`, `not_started`, or `unresolved`.
- An ordinary, acknowledged command failure is optional evidence. Timeout, forced termination, quota termination, missing reap/finish acknowledgement, or runner errors mean unresolved completion. The host must stop further diagnostics and use the existing owned-target cancellation path.

- [ ] **Step 1: Add worker tests before changing production code**

Add `time` and `tempfile` imports to the test file. The existing `worker()`, `probe()` and `TARGET` are defined there. Use this fixture to keep `/work` out of host tests:

```python
@pytest.fixture
def memory_worker(monkeypatch, tmp_path):
    module = worker()
    checks, order = [], []

    def identity(cfg, **kwargs):
        checks.append(kwargs.get("require_shared_tmp"))
        return probe()

    def read(path, limit):
        order.append(path.name)
        return path.name + "-body"

    monkeypatch.setattr(module, "identity", identity)
    monkeypatch.setattr(module, "read_proc", read)
    monkeypatch.setattr(
        module,
        "TemporaryDirectory",
        lambda **kwargs: tempfile.TemporaryDirectory(dir=tmp_path),
    )
    return module, checks, order


def test_legacy_memory_response_has_no_new_metadata(memory_worker, monkeypatch):
    module, checks, order = memory_worker
    monkeypatch.setattr(
        module, "jcmd", lambda *a, **k: pytest.fail("unexpected attach")
    )
    for options in ({}, {"include_smaps": False, "include_heap_info": False}):
        result = module.memory({"target": asdict(TARGET), **options})
        assert set(result) == {
            "schema",
            "target",
            "source",
            "started_s",
            "errors",
            "before",
            "status",
            "smaps_rollup",
            "after",
            "ended_s",
        }
    assert order == ["status", "smaps_rollup"] * 2
    assert checks == [False] * 4


@pytest.mark.parametrize("failure,state", [(False, "completed"), (True, "completed")])
def test_heap_info_follows_procfs_and_records_acknowledged_completion(
    memory_worker,
    monkeypatch,
    failure,
    state,
):
    module, checks, order = memory_worker

    def jcmd(args, deadline, scratch, *, require_completion=False):
        assert args == ("GC.heap_info",)
        assert deadline > time.monotonic()
        assert require_completion
        order.append("heap_info")
        if failure:
            raise module.CommandCompletedError("acknowledged command error")
        return "garbage-first heap total 1024K, used 512K\n"

    monkeypatch.setattr(module, "jcmd", jcmd)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() + 5,
        }
    )
    assert order == ["status", "smaps_rollup", "smaps", "heap_info"]
    assert checks == [True, True]
    assert result["completion"]["heap_info"] == state
    assert (result["heap_info"] is None) == failure
    assert set(result["intervals"]) == set(order)
    for interval in result["intervals"].values():
        assert interval["ended_s"] >= interval["started_s"]


def test_worker_does_not_disguise_unresolved_completion(memory_worker, monkeypatch):
    module, _, _ = memory_worker

    def unresolved(*args, **kwargs):
        raise module.CommandCompletionUnresolved("jcmd deadline exceeded")

    monkeypatch.setattr(module, "jcmd", unresolved)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() + 5,
        }
    )
    assert result["completion"]["heap_info"] == "unresolved"
    assert result["heap_info"] is None
    assert result["status"] == "status-body"


def test_expired_budget_never_launches_jcmd(memory_worker, monkeypatch):
    module, _, _ = memory_worker
    monkeypatch.setattr(module, "jcmd", lambda *a, **k: pytest.fail("expired attach"))
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() - 1,
        }
    )
    assert result["completion"]["heap_info"] == "not_started"
```

- [ ] **Step 2: Run the worker tests and observe missing completion/interval behavior**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k 'memory or heap_info or expired_budget' -q --no-cov
```

- [ ] **Step 3: Distinguish unresolved command completion in the existing runner**

Add this exception in the worker:

```python
class CommandCompletionUnresolved(RuntimeError):
    """The command runner cannot acknowledge in-JVM completion."""


class CommandCompletedError(RuntimeError):
    """A normally completed command reported an error."""
```

Extend `command` with this signature:

```python
def command(argv, deadline, scratch, limit=2 * 1024 * 1024, *, require_completion=False):
```

Keep its body, inserting the following immediately after `OwnedCommandRunner(...).run()`, before the existing failure check:

```python
if require_completion and (
    not result.reaped
    or result.ended_s is None
    or result.forced_stop
    or result.errors
    or result.cancelled
    or result.timed_out
    or result.quota_exceeded
):
    raise CommandCompletionUnresolved("remote command completion unresolved")
if require_completion and result.returncode != 0:
    raise CommandCompletedError("remote command exited with an error")
```

Keep the existing nonzero-exit check and log handling unchanged. Replace `jcmd` with:

```python
def jcmd(args, deadline, scratch, *, require_completion=False):
    argv = ("/opt/java/openjdk/bin/jcmd", "1", *args)
    if require_completion:
        return command(argv, deadline, scratch, require_completion=True)
    return command(argv, deadline, scratch)
```

This keeps default call arguments and exception behavior unchanged for existing capture callers. The opt-in command still uses the existing 2 MiB output cap.

- [ ] **Step 4: Replace `memory` with the opt-in implementation**

`TemporaryDirectory`, `Path`, `time` and `json` are already imported. Scratch belongs in bounded helper-owned `/work`, not the target's shared `/tmp`.

```python
def memory(cfg):
    include_heap = bool(cfg.get("include_heap_info"))
    opt_in = bool(cfg.get("include_smaps") or include_heap)
    result = {
        "schema": "nanolab-soak-memory-helper-v1",
        "target": cfg["target"],
        "source": "docker-exec:owned-pid-namespace:/proc/1",
        "started_s": time.monotonic(),
        "errors": {},
    }
    deadline = cfg.get("memory_deadline_s", result["started_s"] + 5.0)
    if opt_in:
        result["intervals"], result["completion"] = {}, {}
    result["before"] = identity(cfg, require_shared_tmp=include_heap)
    reads = [("status", 65536), ("smaps_rollup", 262144)]
    if cfg.get("include_smaps"):
        reads.append(("smaps", 8388608))
    for name, limit in reads:
        begin = time.monotonic() if opt_in else None
        state = "not_started"
        try:
            if opt_in and time.monotonic() >= deadline:
                raise TimeoutError("memory collection deadline exhausted")
            state = "completed"
            result[name] = read_proc(Path("/proc/1") / name, limit)
        except (OSError, ValueError) as error:
            result[name] = None
            message = f"{type(error).__name__}: {error}"
            result["errors"][name] = message[:1024] if opt_in else message
        if opt_in:
            result["completion"][name] = state
            result["intervals"][name] = {
                "started_s": begin,
                "ended_s": time.monotonic(),
            }
    if include_heap:
        begin = time.monotonic()
        result["heap_info"] = None
        state = "not_started"
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("memory collection deadline exhausted")
            with TemporaryDirectory(dir="/work") as scratch:
                state = "unresolved"
                result["heap_info"] = jcmd(
                    ("GC.heap_info",),
                    deadline,
                    Path(scratch),
                    require_completion=True,
                )
                state = "completed"
        except CommandCompletionUnresolved as error:
            result["errors"]["heap_info"] = str(error)[:1024]
        except CommandCompletedError as error:
            state = "completed"
            result["errors"]["heap_info"] = str(error)[:1024]
        except (OSError, ValueError, RuntimeError) as error:
            # Before launch this is not_started; after launch it remains
            # unresolved unless jcmd has already acknowledged completion.
            result["errors"]["heap_info"] = str(error)[:1024]
        result["completion"]["heap_info"] = state
        result["intervals"]["heap_info"] = {
            "started_s": begin,
            "ended_s": time.monotonic(),
        }
    result["after"] = identity(cfg, require_shared_tmp=include_heap)
    result["ended_s"] = time.monotonic()
    if opt_in:
        raw_keys = {"status", "smaps_rollup", "smaps", "heap_info"}
        metadata = {key: value for key, value in result.items() if key not in raw_keys}
        if len(json.dumps(metadata).encode("utf-8")) > 65536:
            raise ValueError("memory response metadata exceeds its bound")
    return result
```

- [ ] **Step 5: Test the actual runner classification, not just a stubbed jcmd**

Add this test using the worker's existing `processes` module import path:

```python
@pytest.mark.parametrize(
    "flag,value",
    [
        ("timed_out", True),
        ("forced_stop", True),
        ("cancelled", True),
        ("quota_exceeded", True),
        ("errors", ("runner error",)),
        ("reaped", False),
        ("ended_s", None),
    ],
)
def test_memory_command_requires_acknowledged_completion(
    monkeypatch, tmp_path, flag, value
):
    module = worker()
    import sys
    from nanolab.tasks.soak import processes
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "processes", processes)

    result = dict(
        returncode=0,
        reaped=True,
        ended_s=1.0,
        forced_stop=False,
        errors=(),
        cancelled=False,
        timed_out=False,
        quota_exceeded=False,
    )
    result[flag] = value

    class Runner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return SimpleNamespace(**result)

    monkeypatch.setattr(processes, "OwnedCommandRunner", Runner)
    with pytest.raises(module.CommandCompletionUnresolved):
        module.command(
            ("unused",), time.monotonic() + 5, tmp_path, require_completion=True
        )
```

The helper image copies this module to top-level `processes.py`; the test installs that import alias temporarily. No Docker command runs.

- [ ] **Step 6: Run the whole helper test file and commit**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -q --no-cov
git add packages/nanolab/assets/soak/diagnostic-worker.py packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Bound optional heap readings and track command completion"
```

---
### Task 3: Bound host transport and preserve command ownership

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py`
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Produces `read_memory(timeout_s=5.0, *, include_smaps=False, include_heap_info=False)` on `_OwnedDockerHelper`, `PreparedDockerDiagnostics` and `PreparedDockerMemory`.
- Sends enabled flags and `memory_deadline_s` only for opt-in requests. The local Docker helper shares the host's monotonic clock; this contract is not for a remote Docker host.
- Raises `MemoryCommandUnresolved` for unacknowledged JVM completion, with owned-target cancellation before helper removal. Source errors with acknowledged completion return normally.
- Default requests retain their keys, response, 1 MiB output cap and existing timeout/cleanup behavior.

- [ ] **Step 1: Add a complete transport double and regression tests**

Add `base64` and `time` to test imports. No undefined `owned_memory_helper` fixture is needed:

```python
def memory_owner(monkeypatch, tmp_path, *, smaps=None, completion="completed"):
    import nanolab.tasks.soak.diagnostic_helper as helper

    cfg = spec(tmp_path)
    process = {"start_ticks": "314", "ns_pid": 1}
    calls, cleanup = [], []

    class Commands:
        deadline = None
        cleanup_deadline = None

        def run(self, args, **kwargs):
            calls.append((tuple(args), kwargs))
            request = json.loads(base64.b64decode(args[-1]))
            payload = {
                "schema": "nanolab-soak-memory-helper-v1",
                "target": asdict(TARGET),
                "before": probe(),
                "after": probe(),
                "status": "VmRSS:\t512 kB\n",
                "smaps_rollup": "Pss:\t137 kB\n",
                "errors": {},
            }
            if request.get("include_smaps"):
                payload["smaps"] = smaps
            if request.get("include_heap_info"):
                payload["heap_info"] = None
                payload["completion"] = {"heap_info": completion}
                payload["errors"]["heap_info"] = "synthetic command error"
            response = json.dumps(payload)
            if len(response.encode()) > kwargs.get("limit", 1048576):
                raise RuntimeError("transport quota exceeded")
            return response

    owner = helper._OwnedDockerHelper(cfg, Commands(), "e" * 64, process)
    monkeypatch.setattr(owner, "_check_target", lambda: None)
    monkeypatch.setattr(owner, "_helper_owned", lambda **kw: {"State": {"Pid": 5678}})
    monkeypatch.setattr(helper, "_proc_identity", lambda pid: {"ns_pid": 8})

    def close():
        cleanup.append("close")
        owner._closed = True

    def cancel_remote():
        cleanup.append("cancel-target")
        close()

    monkeypatch.setattr(owner, "close", close)
    monkeypatch.setattr(owner, "_cancel_remote", cancel_remote)
    return owner, calls, cleanup


def test_read_memory_preserves_legacy_wire_behavior(monkeypatch, tmp_path):
    owner, calls, cleanup = memory_owner(monkeypatch, tmp_path)
    owner.read_memory(timeout_s=1)
    args, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(args[-1]))
    assert set(cfg) == {"target", "target_start_ticks", "helper_pid"}
    assert kwargs == {}
    assert cleanup == []


@pytest.mark.parametrize("raw", ["x" * 2097152, "\x01" * 8388608])
def test_read_memory_transports_large_escaped_responses(monkeypatch, tmp_path, raw):
    owner, calls, cleanup = memory_owner(monkeypatch, tmp_path, smaps=raw)
    result = owner.read_memory(timeout_s=120, include_smaps=True)
    args, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(args[-1]))
    assert result["smaps"] == raw
    assert kwargs["limit"] == 67108864
    assert 20 < kwargs["timeout_s"] <= 120
    assert cfg["include_smaps"] is True
    assert "include_heap_info" not in cfg
    assert cleanup == []


def test_worker_deadline_uses_remaining_host_budget(monkeypatch, tmp_path):
    import nanolab.tasks.soak.diagnostic_helper as helper

    owner, calls, _ = memory_owner(monkeypatch, tmp_path)
    now = [100.0]
    monkeypatch.setattr(helper.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(owner, "_check_target", lambda: now.__setitem__(0, 104.0))
    owner.read_memory(timeout_s=5, include_heap_info=True)
    args, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(args[-1]))
    assert kwargs["timeout_s"] == 1.0
    assert 104.0 < cfg["memory_deadline_s"] < 105.0


def test_unresolved_heap_command_cancels_owned_target(monkeypatch, tmp_path):
    import nanolab.tasks.soak.diagnostic_helper as helper

    owner, _, cleanup = memory_owner(monkeypatch, tmp_path, completion="unresolved")
    with pytest.raises(helper.MemoryCommandUnresolved):
        owner.read_memory(include_heap_info=True)
    assert cleanup == ["cancel-target", "close"]


def test_completed_source_error_leaves_helper_usable(monkeypatch, tmp_path):
    owner, _, cleanup = memory_owner(monkeypatch, tmp_path)
    result = owner.read_memory(include_heap_info=True)
    assert result["errors"]["heap_info"]
    assert not owner._closed
    assert cleanup == []
    owner.read_memory()  # another operation remains possible


@pytest.mark.parametrize("error", [RuntimeError("lost reply"), KeyboardInterrupt()])
def test_lost_reply_or_cancel_preserves_exception_and_stops_target(
    monkeypatch,
    tmp_path,
    error,
):
    owner, _, cleanup = memory_owner(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(owner.commands, "run", fail)
    with pytest.raises(type(error)) as raised:
        owner.read_memory(include_heap_info=True)
    assert raised.value is error
    assert cleanup == ["cancel-target", "close"]
```

- [ ] **Step 2: Run the transport tests and confirm they fail for missing opt-in support**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k 'read_memory or worker_deadline or unresolved_heap or completed_source or lost_reply' -q --no-cov
```

- [ ] **Step 3: Replace `_OwnedDockerHelper.read_memory`**

Add these module-level definitions:

```python
_OPT_IN_RESPONSE_BYTES = 67108864


class MemoryCommandUnresolved(RuntimeError):
    """No acknowledgement permits a later diagnostic on this target."""
```

Use one host deadline starting before lock acquisition. After identity inspections compute the remaining budget, pass it explicitly to `commands.run` (overriding the default 20 seconds), and give the worker an earlier deadline for its acknowledgement. Do not restart a duration after procfs reads.

```python
def read_memory(self, timeout_s=5.0, *, include_smaps=False, include_heap_info=False):
    if not 0 < timeout_s <= 300:
        raise ValueError("memory collection timeout must be in (0, 300]")
    if include_heap_info and (
        self.spec.memory_only
        or self.spec.target.runtime != "jvm"
        or not self.spec.allow_target_stop_on_cancel
    ):
        raise ValueError("heap-info requires an owned JVM diagnostic helper")
    started = time.monotonic()
    if not self._serial.acquire(timeout=timeout_s):
        raise TimeoutError("helper busy with another operation")
    self.commands.deadline = started + timeout_s
    jvm_may_be_running = False
    try:
        if self._closed:
            raise RuntimeError("memory helper is closed")
        self._check_target()
        remote = self._helper_owned(cleanup=False)
        holder = _proc_identity(remote["State"]["Pid"])
        cfg = {
            "target": asdict(self.spec.target),
            "target_start_ticks": self.process_identity["start_ticks"],
            "helper_pid": holder["ns_pid"],
        }
        options = {}
        if include_smaps or include_heap_info:
            remaining = self.commands.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("memory collection deadline exhausted")
            margin = min(1.0, remaining / 5)
            cfg["memory_deadline_s"] = self.commands.deadline - margin
            if include_smaps:
                cfg["include_smaps"] = True
            if include_heap_info:
                cfg["include_heap_info"] = True
            options = {"timeout_s": remaining, "limit": _OPT_IN_RESPONSE_BYTES}
        argv = ("exec", self.helper_id, PYTHON, WORKER, "memory", _encoded(cfg))
        jvm_may_be_running = include_heap_info
        data = json.loads(self.commands.run(argv, **options))
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
    except BaseException as error:
        try:
            self.commands.cleanup_deadline = time.monotonic() + 5.0
            if jvm_may_be_running:
                # This validates ownership, stops the exact target, confirms
                # it stopped, then removes the helper. close() alone is not enough.
                self._cancel_remote()
            else:
                self.close()
        except BaseException as cleanup_error:
            if include_smaps or include_heap_info:
                error.add_note(f"memory cleanup unconfirmed: {cleanup_error}")
                raise error from cleanup_error
            raise RuntimeError(
                "memory read failed; remote reader cleanup unconfirmed: "
                f"{cleanup_error}"
            ) from error
        raise
    finally:
        self.commands.deadline = None
        self.commands.cleanup_deadline = None
        self._serial.release()
```

Do not clear session handles on this exception. The run must stop, and the lifecycle still needs the handle for cleanup if removal failed. Preserve `KeyboardInterrupt` as cancellation. Rebinding cannot make an unresolved command safe; ordinary completed source errors never close the helper in the first place. No change to `_OPERATIONS` or `capture()` is involved.

- [ ] **Step 4: Forward flags without changing default wrapper calls**

For `PreparedDockerDiagnostics`:

```python
def read_memory(self, timeout_s=5.0, *, include_smaps=False, include_heap_info=False):
    if not include_smaps and not include_heap_info:
        return self.executor.read_memory(timeout_s)
    return self.executor.read_memory(
        timeout_s,
        include_smaps=include_smaps,
        include_heap_info=include_heap_info,
    )
```

For `PreparedDockerMemory`:

```python
def read_memory(self, timeout_s=5.0, *, include_smaps=False, include_heap_info=False):
    if not include_smaps and not include_heap_info:
        return self._owner.read_memory(timeout_s)
    return self._owner.read_memory(
        timeout_s,
        include_smaps=include_smaps,
        include_heap_info=include_heap_info,
    )
```

The owner rejects heap-info on procfs-only helpers. The 64 MiB ceiling covers worst-case encoding of the four raw caps plus 64 KiB metadata; worker errors are capped individually. Check peak allocation for strings plus JSON against the provisioned diagnostic helper memory before implementation sign-off; this ceiling does not raise legacy helper limits.

- [ ] **Step 5: Verify the shared path and commit**

```bash
NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak packages/nanolab/tests/config/test_soak.py packages/nanolab/tests/plans/test_soak.py -q --no-cov
git add packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Carry bounded memory observations with owned cancellation"
```

---

### Task 4: The native summary aggregator

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/heap_analysis/native.py`
- Test: `packages/nanolab/tests/heap_analysis/test_native.py`

**Interfaces:**
- Consumes: the raw strings Tasks 1-3 deliver — `status`, `smaps_rollup`, `smaps`, `heap_info` — and the per-source `errors` mapping.
- Produces: `LARGE_MAPPING_BYTES = 33554432`, `parse_heap_info(text) -> dict`, `parse_smaps(text) -> dict`, `residency(status, rollup) -> dict`, `summarize(response) -> dict`. Task 5 calls only `summarize`.

`summarize` returns the `native` block. A source that is absent, `None`, or carries an error in `response["errors"]` is reported unavailable, and no total is derived from it.

- [ ] **Step 1: Write the failing tests**

Create `packages/nanolab/tests/heap_analysis/test_native.py`:

```python
import pytest

from nanolab.tasks.heap_analysis.native import (
    LARGE_MAPPING_BYTES,
    parse_heap_info,
    parse_smaps,
    residency,
    summarize,
)

HEAP_INFO = (
    " garbage-first heap   total 2097152K, used 1048576K "
    "[0x0000000700000000, 0x0000000800000000)\n"
    "  region size 1024K, 100 young (102400K), 8 survivors (8192K)\n"
    " Metaspace       used 65536K, committed 66560K, reserved 1114112K\n"
)

SMAPS = """\
7f0000000000-7f0004000000 rw-p 00000000 00:00 0
Size:              65536 kB
Rss:               32768 kB
Pss:               32768 kB
7f0010000000-7f0010001000 r--p 00000000 08:01 1234    /usr/lib/libc.so.6
Size:                  4 kB
Rss:                   4 kB
Pss:                   2 kB
7f0020000000-7f0022000000 ---p 00000000 00:00 0
Size:              32768 kB
Rss:                   0 kB
Pss:                   0 kB
"""


def test_heap_info_is_normalized_to_bytes():
    parsed = parse_heap_info(HEAP_INFO)
    assert parsed["heap"] == {"committed": 2097152 * 1024, "used": 1048576 * 1024}
    assert parsed["metaspace"] == {"committed": 66560 * 1024, "used": 65536 * 1024}


def test_heap_info_marks_unrecognized_output_unavailable_instead_of_zero():
    parsed = parse_heap_info("Shenandoah Heap\n")
    assert parsed["heap"] is None
    assert parsed["metaspace"] is None


def test_smaps_reports_each_mapping_and_keeps_categories_separate():
    parsed = parse_smaps(SMAPS)
    assert parsed["mappings"] == 3
    assert parsed["anonymous"] == {
        "size": (65536 + 32768) * 1024,
        "rss": 32768 * 1024,
        "pss": 32768 * 1024,
    }
    assert parsed["file"] == {"size": 4 * 1024, "rss": 4 * 1024, "pss": 2 * 1024}


def test_smaps_reports_large_mappings_individually_without_naming_an_owner():
    parsed = parse_smaps(SMAPS)
    large = parsed["large_anonymous_mappings"]
    assert large["count"] == 2
    assert large["size"] == (65536 + 32768) * 1024
    assert sorted(item["size"] for item in large["mappings"]) == [
        32768 * 1024,
        65536 * 1024,
    ]
    assert LARGE_MAPPING_BYTES == 33554432
    assert "arena" not in repr(parsed)


def test_residency_keeps_kernel_categories_and_leaves_missing_fields_absent():
    status = "Name:\tjava\nRssAnon:\t 32768 kB\nRssFile:\t  4096 kB\n"
    rollup = "Pss_Anon:\t 32768 kB\nPss_File:\t  2048 kB\n"
    values = residency(status, rollup)
    assert values["RssAnon"] == 32768 * 1024
    assert values["RssFile"] == 4096 * 1024
    assert "RssShmem" not in values
    assert values["Pss_Anon"] == 32768 * 1024
    assert "Pss_Shmem" not in values


def test_summary_publishes_no_smaps_totals_when_the_read_was_incomplete():
    block = summarize(
        {
            "status": "RssAnon:\t 32768 kB\n",
            "smaps_rollup": "Pss_Anon:\t 32768 kB\n",
            "smaps": None,
            "heap_info": HEAP_INFO,
            "errors": {"smaps": "ValueError: procfs evidence exceeds read bound"},
        }
    )
    assert block["smaps"] == {
        "available": False,
        "error": "ValueError: procfs evidence exceeds read bound",
    }
    assert block["heap_info"]["available"] is True
    assert block["residency"]["RssAnon"] == 32768 * 1024


def test_summary_marks_a_source_unavailable_when_it_was_never_requested():
    block = summarize({"status": "RssAnon:\t 4 kB\n", "smaps_rollup": "", "errors": {}})
    assert block["smaps"]["available"] is False
    assert block["heap_info"]["available"] is False
    assert "error" not in block["smaps"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "unrecognized output",
        SMAPS.rsplit("Pss:", 1)[0],
        "1000-2000 rw-p 0 00:00 0\nSize: 4 kB\nRss: 4 kB\n",
    ],
)
def test_incomplete_smaps_never_produces_totals(text):
    block = summarize({"smaps": text, "errors": {}})["smaps"]
    assert block["available"] is False
    assert "anonymous" not in block
    assert "large_anonymous_mappings" not in block


@pytest.mark.parametrize(
    "permissions,path,category",
    [
        ("rw-p", "[heap]", "anonymous"),
        ("rw-p", "[anon:java-heap]", "anonymous"),
        ("rw-s", "[anon_shmem:shared]", "shared_memory"),
        ("rw-s", "/dev/shm/shared", "shared_memory"),
        ("rw-s", "", "shared_memory"),
        ("r-xp", "[vdso]", "unknown"),
        ("rw-p", "/tmp/private-file", "file"),
    ],
)
def test_smaps_backing_categories_are_explicit(permissions, path, category):
    raw = (
        f"10000000-14000000 {permissions} 0 00:00 0 {path}\n"
        "Size: 65536 kB\nRss: 4 kB\nPss: 4 kB\nAnonymous: 4 kB\n"
    )
    parsed = parse_smaps(raw)
    assert parsed[category]["rss"] == 4096
    # A file VMA containing anonymous COW pages remains a file VMA;
    # process page residency comes from status/rollup, not this category.
    assert parsed["mapping_details"][0]["backing"] == category
    assert parsed["large_anonymous_mappings"]["count"] == (category == "anonymous")


@pytest.mark.parametrize(
    "size_kb,qualifies", [(32767, False), (32768, True), (32769, True)]
)
def test_large_mapping_boundary(size_kb, qualifies):
    end = 0x10000000 + size_kb * 1024
    raw = f"10000000-{end:x} ---p 0 00:00 0\nSize: {size_kb} kB\nRss: 0 kB\nPss: 0 kB\n"
    assert parse_smaps(raw)["large_anonymous_mappings"]["count"] == int(qualifies)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis/test_native.py -q --no-cov
```

Expected: collection fails because `nanolab.tasks.heap_analysis.native` does not exist.

- [ ] **Step 3: Implement the module**

```python
"""Summarize the optional native memory readings without inferring a cause.

Process residency uses the kernel page categories. VMA backing labels describe
mappings, not every resident page inside them; no bytes are attributed to an
allocator.
"""

from __future__ import annotations

import re

# A descriptive threshold on virtual mapping size, not an allocator signature.
# JVM mappings can pass it; reserved size and residency are distinct.
LARGE_MAPPING_BYTES = 33554432

_KB = re.compile(r"^(\w+):\s+(\d+) kB$", re.MULTILINE)
_HEAP = re.compile(r"garbage-first heap\s+total (\d+)K, used (\d+)K")
_METASPACE = re.compile(r"Metaspace\s+used (\d+)K, committed (\d+)K")
_HEADER = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+)[ \t]+([rwxps-]{4})[ \t]+"
    r"[0-9a-f]+[ \t]+([0-9a-f]+:[0-9a-f]+)[ \t]+([0-9]+)"
    r"(?:[ \t]+(.*))?$",
    re.MULTILINE,
)
_RESIDENCY = ("RssAnon", "RssFile", "RssShmem", "Pss_Anon", "Pss_File", "Pss_Shmem")


def _kilobytes(value: str) -> int:
    return int(value) * 1024


def parse_heap_info(text: str) -> dict[str, dict[str, int] | None]:
    """Normalize committed/used to bytes; unrecognized output stays unavailable."""
    heap = _HEAP.search(text)
    metaspace = _METASPACE.search(text)
    return {
        "heap": (
            {"committed": _kilobytes(heap[1]), "used": _kilobytes(heap[2])}
            if heap
            else None
        ),
        "metaspace": (
            {
                "committed": _kilobytes(metaspace[2]),
                "used": _kilobytes(metaspace[1]),
            }
            if metaspace
            else None
        ),
    }


def _backing(path: str, permissions: str) -> str:
    """Describe VMA backing, never the backing of every resident page."""
    if path.startswith(("[anon_shmem:", "/dev/shm/", "/memfd:", "/SYSV")) or (
        not path and permissions.endswith("s")
    ):
        return "shared_memory"
    if permissions.endswith("p") and (
        not path
        or path in {"[heap]", "[stack]"}
        or path.startswith(("[anon:", "[stack:"))
    ):
        return "anonymous"
    if path.startswith("/"):
        return "file"
    return "unknown"


def parse_smaps(text: str) -> dict[str, object]:
    """Reject incomplete records and separate VMA backing from page residency."""
    headers = list(_HEADER.finditer(text))
    if not headers or text[: headers[0].start()].strip():
        raise ValueError("smaps has no complete mapping header")
    totals = {
        name: {"size": 0, "rss": 0, "pss": 0}
        for name in ("anonymous", "file", "shared_memory", "unknown")
    }
    records, large = [], []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end() : end]
        # A malformed subsequent header must not silently join this record.
        if re.search(r"^[0-9a-f]+-", body, re.MULTILINE):
            raise ValueError("smaps contains an unrecognized mapping header")
        pairs = _KB.findall(body)
        fields = dict(pairs)
        required = {"Size", "Rss", "Pss"}
        if any(sum(name == key for name, _ in pairs) != 1 for key in required):
            raise ValueError("smaps mapping has missing or duplicate Size/Rss/Pss")
        if int(header[2], 16) <= int(header[1], 16):
            raise ValueError("invalid smaps address range")
        path = (header[6] or "").strip()
        backed = _backing(path, header[3])
        values = {
            "size": _kilobytes(fields["Size"]),
            "rss": _kilobytes(fields["Rss"]),
            "pss": _kilobytes(fields["Pss"]),
        }
        record = {
            "address": f"{header[1]}-{header[2]}",
            "permissions": header[3],
            "backing": backed,
            "path": path,
            **values,
        }
        records.append(record)
        for key, value in values.items():
            totals[backed][key] += value
        if backed == "anonymous" and values["size"] >= LARGE_MAPPING_BYTES:
            large.append(record)
    return {
        "mappings": len(records),
        "mapping_details": records,
        **totals,
        "large_anonymous_mappings": {
            "count": len(large),
            "size": sum(item["size"] for item in large),
            "rss": sum(item["rss"] for item in large),
            "pss": sum(item["pss"] for item in large),
            "mappings": large,
        },
    }


def residency(status: str | None, rollup: str | None) -> dict[str, int]:
    """Keep the kernel's own categories; a missing field stays absent, not zero."""
    values = dict(_KB.findall(status or "")) | dict(_KB.findall(rollup or ""))
    return {name: _kilobytes(values[name]) for name in _RESIDENCY if name in values}


def _source(response: dict, name: str) -> tuple[bool, str | None]:
    error = (response.get("errors") or {}).get(name)
    return response.get(name) is not None and error is None, error


def summarize(response: dict) -> dict[str, object]:
    """Build the native block, publishing no total derived from a failed source."""
    block: dict[str, object] = {
        "residency": residency(response.get("status"), response.get("smaps_rollup"))
    }
    for name, parse in (("smaps", parse_smaps), ("heap_info", parse_heap_info)):
        available, error = _source(response, name)
        if available:
            try:
                parsed = parse(response[name])
            except ValueError as parse_error:
                block[name] = {"available": False, "error": str(parse_error)}
            else:
                recognized = name != "heap_info" or any(
                    value is not None for value in parsed.values()
                )
                block[name] = {"available": recognized, **parsed}
        elif error is not None:
            block[name] = {"available": False, "error": error}
        else:
            block[name] = {"available": False}
    return block
```

- [ ] **Step 4: Run the tests and confirm GREEN**

Run the Step 2 command. Expected: all original examples and the new category, boundary and incomplete-input cases pass.

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/tasks/heap_analysis/native.py \
  packages/nanolab/tests/heap_analysis/test_native.py
git commit -m "Summarize the native memory readings"
```

---

### Task 5: Persist raw sources and publish a bounded comparison

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py`
- Create: `packages/nanolab/tests/heap_analysis/test_native_evidence.py`

**Interfaces:**
- Consumes: Task 4's `summarize`, the complete opt-in response, and the existing run artifact limit.
- Produces: `persist_native(root, checkpoint, response, artifact_limit_bytes) -> dict` and `native_comparison(root) -> dict`; `root` is the existing evidence directory.
- Uses existing `measure_tree`, `enforce_limit` and `describe_artifact` from `tasks/soak/artifacts.py`. Raw files may exceed `ArtifactWriter`'s 1 MiB JSON-record limit, so use bounded immutable raw writes with cumulative run accounting. Do not change the shared writer or its default limits.

- [ ] **Step 1: Add focused evidence tests**

```python
import json

import pytest

from nanolab.tasks.heap_analysis.evidence import native_comparison, persist_native
from nanolab.tasks.soak.artifacts import ArtifactLimitExceededError


def reading():
    return {
        "status": "RssAnon: 4 kB\n",
        "smaps_rollup": "Pss_Anon: 4 kB\n",
        "smaps": "1000-2000 rw-p 0 00:00 0\nSize: 4 kB\nRss: 4 kB\nPss: 4 kB\n",
        "heap_info": "garbage-first heap total 1024K, used 512K\n",
        "intervals": {"status": {"started_s": 1.0, "ended_s": 2.0}},
        "completion": {"heap_info": "completed"},
        "errors": {},
        "started_s": 1.0,
        "ended_s": 4.0,
    }


def test_persist_native_keeps_all_sources_and_intervals(tmp_path):
    root = tmp_path / "evidence"
    raw = reading()
    block = persist_native(root, "natural-drain", raw, 1048576)
    for key in ("status", "smaps_rollup", "smaps", "heap_info"):
        source = block["sources"][key]
        assert (root / source["artifact"]["path"]).read_text() == raw[key]
    assert block["sources"]["status"]["interval"] == raw["intervals"]["status"]
    assert block["sources"]["heap_info"]["completion"] == "completed"
    assert block["collection"]["ended_s"] == 4.0


def test_raw_writes_respect_cumulative_budget_before_writing(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    (tmp_path / "other-artifact").write_bytes(b"x" * 1024)
    with pytest.raises(ArtifactLimitExceededError):
        persist_native(root, "natural-drain", reading(), 1024 + 4096)
    assert not list(root.rglob("*.txt"))


def test_missing_source_is_visible_and_not_written(tmp_path):
    raw = reading()
    raw["smaps"] = None
    raw["errors"]["smaps"] = "read bound exceeded"
    block = persist_native(tmp_path / "evidence", "natural-drain", raw, 1048576)
    assert block["smaps"]["available"] is False
    assert "artifact" not in block["sources"]["smaps"]
    assert block["sources"]["smaps"]["error"] == "read bound exceeded"
    assert block["residency"]["RssAnon"] == 4096


def test_comparison_retains_missing_and_malformed_checkpoints(tmp_path):
    (tmp_path / "runtime-natural-drain.json").write_text("not JSON")
    (tmp_path / "runtime-after-final-gc.json").write_text(
        json.dumps(
            {
                "native": {"residency": {"RssAnon": 4096}},
            }
        )
    )
    comparison = native_comparison(tmp_path)
    assert set(comparison) == {"before-baseline", "natural-drain", "after-final-gc"}
    assert comparison["before-baseline"]["available"] is False
    assert comparison["natural-drain"]["available"] is False
    assert comparison["after-final-gc"]["native"]["residency"]["RssAnon"] == 4096
    assert "before final dump" in comparison["after-final-gc"]["phase"]
```

- [ ] **Step 2: Run the new test module and confirm the missing module failure**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis/test_native_evidence.py -q --no-cov
```

- [ ] **Step 3: Implement the evidence helpers**

```python
"""Bounded raw memory evidence and a comparison with explicit missing entries."""

import json
import os
import tempfile
from pathlib import Path

from nanolab.tasks.heap_analysis.native import summarize
from nanolab.tasks.soak.artifacts import (
    MAX_RECORD_BYTES,
    ArtifactLimitExceededError,
    describe_artifact,
    enforce_limit,
)

CHECKPOINTS = {
    "before-baseline": "after warmup, before baseline GC and dump",
    "natural-drain": "after natural drain, before final GC",
    "after-final-gc": "after completed explicit final GC, before final dump",
}
_RAW = {
    "status": ("status.txt", 65536),
    "smaps_rollup": ("smaps-rollup.txt", 262144),
    "smaps": ("smaps.txt", 8388608),
    "heap_info": ("heap-info.txt", 2097152),
}
_TERMINAL_RESERVE = 4096


def _write_raw(root: Path, name: str, body: bytes, run_limit: int) -> dict:
    """Publish once, accounting for all retained artifacts in this serial session."""
    run_root = root.parent
    used = enforce_limit(run_root, run_limit)
    if used + len(body) + _TERMINAL_RESERVE > run_limit:
        raise ArtifactLimitExceededError(
            "native evidence exceeds cumulative run budget"
        )
    directory = root / "native"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=directory)
    temporary = Path(temporary_name)
    target = directory / name
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # fails if already published; never overwrites
    finally:
        temporary.unlink(missing_ok=True)
    enforce_limit(run_root, run_limit)
    return {**describe_artifact(target), "path": str(target.relative_to(root))}


def persist_native(
    root: Path, checkpoint: str, response: dict, artifact_limit_bytes: int
) -> dict:
    if checkpoint not in CHECKPOINTS:
        raise ValueError("unknown memory checkpoint")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    sources = {}
    errors = response.get("errors") or {}
    for key, (suffix, maximum) in _RAW.items():
        source = {
            "interval": (response.get("intervals") or {}).get(key),
            "completion": (response.get("completion") or {}).get(key),
            "available": response.get(key) is not None and key not in errors,
        }
        if key in errors:
            source["error"] = errors[key]
        text = response.get(key)
        if text is not None:
            body = text.encode("utf-8")
            if len(body) > maximum:
                raise ValueError(f"{key} violates its raw evidence bound")
            source["artifact"] = _write_raw(
                root,
                f"{checkpoint}-{suffix}",
                body,
                artifact_limit_bytes,
            )
        sources[key] = source
    block = {
        **summarize(response),
        "sources": sources,
        "phase": CHECKPOINTS[checkpoint],
        "collection": {
            key: response[key]
            for key in ("target", "before", "after", "started_s", "ended_s")
            if key in response
        },
    }
    # Leave room in the existing 1 MiB runtime JSON record for ordinary
    # observations. Full raw mappings stay available even if the summary is huge.
    if len(json.dumps(block).encode("utf-8")) > MAX_RECORD_BYTES // 2:
        block["smaps"] = {
            "available": False,
            "error": "parsed smaps summary exceeds checkpoint record budget; see raw artifact",
        }
    return block


def native_comparison(root: Path) -> dict:
    comparison = {}
    for checkpoint, phase in CHECKPOINTS.items():
        entry = {"phase": phase, "available": False}
        try:
            document = json.loads((root / f"runtime-{checkpoint}.json").read_text())
            block = document["native"]
            if not isinstance(block, dict):
                raise ValueError("native checkpoint block is not an object")
        except (OSError, ValueError, TypeError, KeyError) as error:
            entry["error"] = f"{type(error).__name__}: {error}"[:1024]
        else:
            entry.update(available=True, native=block)
        comparison[checkpoint] = entry
    return comparison
```

The raw-writing helper is specific to heap-analysis evidence, not a new transport or capture framework. It counts files outside `ArtifactWriter` using the existing cumulative accounting utilities, preserves private file permissions and reserves terminal space. Task 6 checks the cumulative limit again after the runtime JSON is written. Evidence-storage failures retain the existing artifact-budget failure semantics.

- [ ] **Step 4: Verify the evidence tests and commit**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis/test_native_evidence.py -q --no-cov
git add packages/nanolab/src/nanolab/tasks/heap_analysis/evidence.py packages/nanolab/tests/heap_analysis/test_native_evidence.py
git commit -m "Retain bounded raw memory evidence and checkpoint comparisons"
```

---
### Task 6: Wire observations into the lifecycle and publish the report

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py`
- Modify: `packages/nanolab/tests/heap_analysis/test_runtime.py`
- Modify: `docs/heap-analysis.md`

**Interfaces:**
- Consumes: Task 3's helper options and Task 5's `persist_native` / `native_comparison`.
- Uses `LocalHeapAnalysisSession._root` and `_writer` while observing, and `wiring.writer.root` in `RunControlPlaneHeapAnalysis._publish`. The latter class has no `_writer` attribute; `HeapAnalysisWiring` has no `prepared` attribute.
- Retains all three checkpoint names but collects `after-final-gc` before `heap_dump("final")`.

- [ ] **Step 1: Correct the test scaffolding and add lifecycle assertions**

The existing `local_session(tmp_path)` returns `(session, deployment)`. There is no global `TARGET` or `events` fixture in this test module. Use the existing `Target`, `DIGEST`, `FakeHelper`, `FakeSession`, `build`, `measure`, and `evidence` definitions:

```python
def fake_session_with_readings(tmp_path, *, response=None, error=None):
    session, _deployment = local_session(tmp_path)

    class ReadingHelper(FakeHelper):
        def __init__(self):
            super().__init__()
            self.options = []

        def read_memory(self, timeout_s=5.0, **options):
            self.options.append(options)
            if error is not None:
                raise error
            return response or {
                "status": "RssAnon: 4 kB\n",
                "smaps_rollup": "Pss_Anon: 4 kB\n",
                "smaps": "1000-2000 rw-p 0 00:00 0\nSize: 4 kB\nRss: 4 kB\nPss: 4 kB\n",
                "heap_info": "garbage-first heap total 1024K, used 512K\n",
                "intervals": {"status": {"started_s": 1.0, "ended_s": 2.0}},
                "completion": {"heap_info": "completed"},
                "errors": {},
            }

    session._helper = ReadingHelper()
    session._target = Target(
        "control-plane",
        "c" * 64,
        1,
        "2026-09-15T00:00:00Z",
        DIGEST,
        "jvm",
    )
    return session


def test_after_final_gc_is_observed_before_the_final_dump(tmp_path):
    events = []
    task, _, _ = build(tmp_path, FakeSession(events, evidence(tmp_path)))
    measure(task)
    assert events.index("gc:final") < events.index("observe:after-final-gc")
    assert events.index("observe:after-final-gc") < events.index("dump:final")


def test_observation_retains_native_sources_and_intervals(tmp_path):
    session = fake_session_with_readings(tmp_path)
    path = session.observe("natural-drain")
    document = json.loads(path.read_text())
    native = document["native"]
    assert native["heap_info"]["heap"]["used"] == 512 * 1024
    assert native["sources"]["status"]["interval"]["ended_s"] == 2.0
    for source in native["sources"].values():
        assert (session._root / source["artifact"]["path"]).is_file()
    assert session._helper.options == [
        {"include_smaps": True, "include_heap_info": True}
    ]
    natural = json.loads((session._root / "natural-drain.json").read_text())
    assert natural["completed"] is True


def test_completed_optional_error_keeps_session_usable(tmp_path):
    session = fake_session_with_readings(
        tmp_path,
        response={
            "status": "RssAnon: 4 kB\n",
            "smaps_rollup": "Pss_Anon: 4 kB\n",
            "smaps": None,
            "heap_info": None,
            "errors": {
                "smaps": "read bound exceeded",
                "heap_info": "acknowledged failure",
            },
            "completion": {"heap_info": "completed"},
        },
    )
    document = json.loads(session.observe("natural-drain").read_text())
    assert document["native"]["heap_info"]["available"] is False
    assert document["native"]["residency"]["RssAnon"] == 4096
    assert session._helper.closed == 0
    session.observe("after-final-gc")
    assert session._helper.closed == 0


@pytest.mark.parametrize(
    "error", [RuntimeError("cleanup unconfirmed"), KeyboardInterrupt()]
)
def test_observation_keeps_cleanup_handle_and_original_exception(tmp_path, error):
    session = fake_session_with_readings(tmp_path, error=error)
    helper = session._helper
    with pytest.raises(type(error)) as raised:
        session.observe("natural-drain")
    assert raised.value is error
    assert session._helper is helper
    session.close()
    assert helper.closed == 1


def test_report_publishes_three_explicit_checkpoint_entries(tmp_path):
    events = []
    task, _, _ = build(tmp_path, FakeSession(events, evidence(tmp_path)))
    result = measure(task)
    report = json.loads(result.report.read_text())
    assert set(report["native"]) == {
        "before-baseline",
        "natural-drain",
        "after-final-gc",
    }
    # This FakeSession does not publish native data into wiring.writer.root.
    # Its absence is visible and does not prevent terminal publication.
    assert all(entry["available"] is False for entry in report["native"].values())
    assert result.status == "PASS"
```

In the existing `SAFETY_ORDER` list, swap only these two entries:

```python
("gc:final",)
("observe:after-final-gc",)
("dump:final",)
```

- [ ] **Step 2: Run the runtime tests and confirm behavior failures rather than fixture errors**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis/test_runtime.py -q --no-cov
```

- [ ] **Step 3: Move the final observation before the dump**

In `_MeasureControlPlaneHeap.run`, use:

```python
            session.observe("natural-drain")
            session.full_gc("final")
            session.observe("after-final-gc")
            final = session.heap_dump("final")
```

The dump command does not pass `-all` and requests another full GC. Preserve all existing natural-checkpoint and explicit full-GC completion requirements.

- [ ] **Step 4: Collect and persist optional evidence in `observe`**

Add imports:

```python
from nanolab.tasks.heap_analysis.evidence import persist_native, native_comparison
from nanolab.tasks.soak.artifacts import (
    ArtifactLimitExceededError,
    enforce_limit,
    measure_tree,
)
```

Replace `observe` up to, but not including, the existing `phase = _CHECKPOINT_PHASES.get(checkpoint)` block with:

```python
def observe(self, checkpoint: str) -> Path:
    targets = self._deployment.discover()
    observed = self._deployment.observations(targets)
    _target, helper = self._bind()
    readings = helper.read_memory(
        timeout_s=min(self._config.diagnostic_timeout_s, 300),
        include_smaps=True,
        include_heap_info=True,
    )
    native = persist_native(
        self._root,
        checkpoint,
        readings,
        self._config.artifact_limit_bytes,
    )
    document = {
        "schema": "nanolab-soak-v1",
        "kind": "runtime_observation",
        "checkpoint": checkpoint,
        "observed": observed,
        "native": native,
    }
    # Raw files are outside ArtifactWriter's individual-record accounting.
    # Check the cumulative run budget before and after the JSON write too.
    required = len(json.dumps(document).encode("utf-8")) + 1
    if (
        measure_tree(self._root.parent) + required + 4096
        > self._config.artifact_limit_bytes
    ):
        raise ArtifactLimitExceededError(
            "runtime observation exceeds cumulative artifact budget"
        )
    evidence = self._writer.write_json(f"runtime-{checkpoint}.json", document)
    enforce_limit(self._root.parent, self._config.artifact_limit_bytes)
```

Keep the existing natural-checkpoint block and return statement. Do not add `except BaseException` that converts cancellation into `RuntimeError` or drops cleanup handles. Completed source errors are data; unresolved command/transport failures propagate and stop the run. Existing deployment teardown and session cleanup then execute, and no subsequent diagnostic runs.

The session keeps the helper even on failure so unsuccessful cleanup can still be reported/retried through its owner. Do not rebind to the same target while command completion is unresolved. The normal optional-error path never closes a helper and needs no recovery machinery.

- [ ] **Step 5: Add the comparison at the actual report publisher**

In `RunControlPlaneHeapAnalysis._publish`, add this field to the report dictionary inside the existing `try/finally`:

```python
                    "native": native_comparison(wiring.writer.root),
```

Do not access `self._writer`. Keep writer closure and terminal-receipt publication in their existing `finally`. `native_comparison` includes every expected checkpoint, even if its file/block is absent or unreadable, and adds the GC/dump phase label.

- [ ] **Step 6: Update the user documentation with the exact evidence contract**

Add this section to `docs/heap-analysis.md` and update its existing sequence to put the final observation before the dump:

```markdown
### Optional memory readings

Heap-analysis collects `GC.heap_info` and full procfs mappings at
`before-baseline`, `natural-drain`, and `after-final-gc`. The last observation
is immediately after the verified explicit full GC and before the final dump.
The dump requests a further full GC, so its effects are outside that reading.

Each `evidence/runtime-<checkpoint>.json` contains a `native` block with parsed
measurements, source intervals, completion states, errors and raw artifact
references. Available sources are retained as:

    evidence/native/<checkpoint>-status.txt
    evidence/native/<checkpoint>-smaps-rollup.txt
    evidence/native/<checkpoint>-smaps.txt
    evidence/native/<checkpoint>-heap-info.txt

`report.json` compares all three checkpoints and shows missing evidence
explicitly. Partial or malformed smaps produces no mapping totals; complete
sibling sources remain usable. Exceptionally large parsed summaries are marked
unavailable in the checkpoint record, with complete raw evidence retained.

Committed heap is not resident heap. Stable committed heap does not establish
that RSS growth is outside Java heap, and net live-set decline can mask growth
in individual object populations. Large anonymous mappings describe virtual
regions, not glibc arena counts or allocator ownership. Process residency uses
the kernel's anonymous, file and shared-memory categories; mapping labels do
not classify every resident page, including copy-on-write pages.

These are bounded diagnostics with collection overhead: heap-info acquires the
JVM heap lock. Completed optional reading errors do not themselves change the
verdict. Unresolved command completion stops further diagnostics and follows
owned-target cleanup; cancellation remains cancellation. All evidence shares
the run's artifact budget. The P24 soak and Node default observation paths
enable neither reading and retain their existing behavior.
```

- [ ] **Step 7: Run focused suites, then existing required checks**

```bash
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis packages/nanolab/tests/soak/test_diagnostic_helper.py -q --no-cov
NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --frozen ruff check packages
uv run --frozen ruff format --check packages
uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab
uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run pre-commit run --all-files
```

Resolve concrete failures before committing; do not weaken existing soak or capture assertions to accommodate a regression. The tests here are implementation gates, not claims that the unimplemented feature already passes.

- [ ] **Step 8: Commit the integration**

```bash
git add packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py packages/nanolab/tests/heap_analysis/test_runtime.py docs/heap-analysis.md
git commit -m "Record native memory evidence around the explicit final GC"
```

---

## Completion gate

```bash
NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run pre-commit run --all-files
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

No dedicated live run is added. The next real heap-analysis run validates attachment, overhead and the observation window. Preserve the existing soak/config/plan expectations; passing old tests alone does not prove wire compatibility, which has dedicated request/response tests in Tasks 2 and 3.

Before declaring implementation complete, verify the revised spec against Tasks 1–6: both flags default off; finite raw/serialized/disk budgets; acknowledged command completion; GC-before-observation-before-dump; correct mapping and page categories; complete raw source/interval provenance; explicit unavailable entries; and publication through `wiring.writer.root`.
