# Control-plane Native Memory Readings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two opt-in memory readings — `GC.heap_info` and the full `/proc/<pid>/smaps` — to the heap-analysis checkpoints, with an aggregate summary, without changing the P24 soak path.

**Architecture:** Both readings travel the existing observation channel (`read_memory()` → `docker exec` → the worker's `memory(cfg)` mode), not `capture()`. They are requested through explicit keyword options that default to off; when both are off the worker request, response shape, read sequence and 1 MiB transport limit stay exactly as they are today. Aggregation lives in a new `tasks/heap_analysis/native.py` and never in shared soak code.

**Tech Stack:** Python 3.12, pytest, Docker (not needed by any test in this plan), JDK 25 `jcmd`, Linux procfs.

**Spec:** `docs/superpowers/specs/2026-09-16-control-plane-native-memory-readings-design.md`

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

### Task 2: Opt-in GC.heap_info in the worker

**Files:**
- Modify: `packages/nanolab/assets/soak/diagnostic-worker.py` (function `memory`)
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: the existing `jcmd(args, deadline, scratch)` helper (line 182), which runs `/opt/java/openjdk/bin/jcmd 1 <args>` through the owned command runner and returns its log text.
- Produces: `memory(cfg)` honouring `cfg["include_heap_info"]` and `cfg["heap_info_deadline_s"]`; response key `"heap_info"`, and `result["errors"]["heap_info"]` on failure. Task 3 sends both; Task 4 parses the value.

The procfs reads must complete before the attach command, so the procfs sample precedes this checkpoint's JVM command.

- [ ] **Step 1: Write the failing tests**

```python
def test_memory_worker_omits_heap_info_unless_requested(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    monkeypatch.setattr(module, "read_proc", lambda path, limit: "body")

    def refuse(*args, **kwargs):
        raise AssertionError("jcmd must not run unless requested")

    monkeypatch.setattr(module, "jcmd", refuse)
    result = module.memory({"target": asdict(TARGET)})
    assert "heap_info" not in result


def test_memory_worker_runs_heap_info_after_the_procfs_reads(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    order = []

    def read(path, limit):
        order.append("read:" + path.name)
        return "body"

    def fake_jcmd(args, deadline, scratch):
        order.append("jcmd:" + args[0])
        return "garbage-first heap   total 1024K, used 512K\n"

    monkeypatch.setattr(module, "read_proc", read)
    monkeypatch.setattr(module, "jcmd", fake_jcmd)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "heap_info_deadline_s": 30.0,
        }
    )
    assert order == [
        "read:status",
        "read:smaps_rollup",
        "read:smaps",
        "jcmd:GC.heap_info",
    ]
    assert result["heap_info"] == "garbage-first heap   total 1024K, used 512K\n"


def test_memory_worker_records_a_failed_heap_info_without_losing_procfs(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    monkeypatch.setattr(module, "read_proc", lambda path, limit: f"{path.name}-body")

    def failing_jcmd(args, deadline, scratch):
        raise RuntimeError("remote owned child failed: attach timed out")

    monkeypatch.setattr(module, "jcmd", failing_jcmd)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_heap_info": True,
            "heap_info_deadline_s": 30.0,
        }
    )
    assert result["heap_info"] is None
    assert "attach timed out" in result["errors"]["heap_info"]
    assert result["status"] == "status-body"
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k heap_info -q --no-cov
```

Expected: the two requesting tests fail because `memory` never calls `jcmd`.

- [ ] **Step 3: Run the command after the reads**

Add this block after the read loop and before `result["after"] = ...`, so the sample brackets stay honest:

```python
    if cfg.get("include_heap_info"):
        deadline = time.monotonic() + cfg["heap_info_deadline_s"]
        with tempfile.TemporaryDirectory() as scratch:
            try:
                result["heap_info"] = jcmd(
                    ("GC.heap_info",), deadline, Path(scratch)
                )
            except (OSError, ValueError, RuntimeError) as error:
                result["heap_info"] = None
                result["errors"]["heap_info"] = f"{type(error).__name__}: {error}"
```

If `tempfile` is not already imported in this file, add it to the existing import block at the top.

- [ ] **Step 4: Record per-source intervals**

The spec requires per-source completion information, so the summary can be checked against its inputs. `memory` already stamps `started_s` and `ended_s` for the whole call; add a per-source interval alongside them. Wrap each read and the command:

```python
    result["intervals"] = {}
```

immediately after `result["errors"] = {}`, then bracket every source. For the read loop:

```python
    for name, limit in reads:
        begin = time.monotonic()
        try:
            result[name] = read_proc(Path("/proc/1") / name, limit)
        except (OSError, ValueError) as error:
            result[name] = None
            result["errors"][name] = f"{type(error).__name__}: {error}"
        result["intervals"][name] = {"started_s": begin, "ended_s": time.monotonic()}
```

and the same `begin`/`intervals` pair around the `heap_info` block. Add to the Step 1 test file:

```python
def test_memory_worker_times_each_source_separately(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    monkeypatch.setattr(module, "read_proc", lambda path, limit: "body")
    monkeypatch.setattr(module, "jcmd", lambda args, deadline, scratch: "heap\n")
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "heap_info_deadline_s": 30.0,
        }
    )
    assert set(result["intervals"]) == {
        "status",
        "smaps_rollup",
        "smaps",
        "heap_info",
    }
    for interval in result["intervals"].values():
        assert interval["ended_s"] >= interval["started_s"]
```

Legacy responses gain the key only for the two sources they already read, and no other field changes.

- [ ] **Step 5: Run the tests and confirm GREEN**

Run the Step 2 command. Expected: all three pass.

- [ ] **Step 6: Run the whole worker test file**

Run:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -q --no-cov
```

Expected: every existing test still passes, proving the default path is untouched.

- [ ] **Step 7: Commit**

```bash
git add packages/nanolab/assets/soak/diagnostic-worker.py \
  packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Read GC.heap_info when the caller asks for it"
```

---

### Task 3: Opt-in options and response budget on the host

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py` (`_OwnedDockerHelper.read_memory` at line 493, `PreparedDockerDiagnostics.read_memory` at line 624, `PreparedDockerMemory.read_memory` at line 642)
- Test: `packages/nanolab/tests/soak/test_diagnostic_helper.py`

**Interfaces:**
- Consumes: Tasks 1 and 2's worker options `include_smaps`, `include_heap_info`, `heap_info_deadline_s`.
- Produces: `read_memory(timeout_s=5.0, *, include_smaps=False, include_heap_info=False)` on all three classes. Task 5 calls it through `PreparedDockerDiagnostics`.

`self.commands.run(...)` defaults to `limit=1048576`. The worker's whole response arrives as that one stdout blob, so an 8 MiB smaps cannot come back under the default. The opt-in path passes an explicit larger limit; the legacy path must keep passing none.

The ceiling covers worst-case JSON escaping (six bytes per raw byte for control characters) of every enabled field plus bounded metadata:

```text
status 65536 + rollup 262144 + smaps 8388608 + heap_info 2097152 = 10813440
10813440 * 6 = 64880640, plus 65536 of metadata/errors, fits 67108864
```

- [ ] **Step 1: Write the failing tests**

The existing test at line 653 shows how the transport is faked; follow it. Add:

```python
def test_read_memory_keeps_the_legacy_request_and_transport_limit(monkeypatch):
    owner, calls = memory_owner(monkeypatch)
    owner.read_memory(timeout_s=1)
    argv, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(argv[-1]))
    assert "include_smaps" not in cfg and "include_heap_info" not in cfg
    assert "limit" not in kwargs


def test_read_memory_requests_only_the_enabled_options(monkeypatch):
    owner, calls = memory_owner(monkeypatch)
    owner.read_memory(timeout_s=1, include_smaps=True)
    cfg = json.loads(base64.b64decode(calls[-1][0][-1]))
    assert cfg["include_smaps"] is True
    assert "include_heap_info" not in cfg


def test_read_memory_raises_the_transport_limit_for_opt_in_requests(monkeypatch):
    owner, calls = memory_owner(monkeypatch)
    owner.read_memory(timeout_s=1, include_smaps=True, include_heap_info=True)
    argv, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(argv[-1]))
    assert cfg["include_heap_info"] is True
    assert cfg["heap_info_deadline_s"] > 0
    assert kwargs["limit"] == 67108864


def test_read_memory_survives_a_response_larger_than_the_legacy_limit(monkeypatch):
    owner, _calls = memory_owner(monkeypatch, smaps="x" * (2 * 1048576))
    data = owner.read_memory(timeout_s=1, include_smaps=True)
    assert len(data["smaps"]) == 2 * 1048576
```

Add this fixture helper beside them, modelled on the existing transport fake:

```python
def memory_owner(monkeypatch, smaps=None):
    """Return an owned helper whose docker transport is recorded, not run."""
    calls = []

    def run(args, timeout_s=20.0, *, cleanup=False, **kwargs):
        calls.append((tuple(args), kwargs))
        observed = probe()
        payload = {
            "schema": "nanolab-soak-memory-helper-v1",
            "target": asdict(TARGET),
            "before": observed,
            "after": observed,
            "status": "Name:\tjava\n",
            "smaps_rollup": "Pss_Anon:\t4 kB\n",
            "errors": {},
        }
        if smaps is not None:
            payload["smaps"] = smaps
        return json.dumps(payload)

    owner = owned_memory_helper(monkeypatch)
    monkeypatch.setattr(owner.commands, "run", run)
    return owner, calls
```

`owned_memory_helper` is the existing construction used by the test at line 653; extract it into a helper if it is currently inline, so both call sites share it.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -k read_memory -q --no-cov
```

Expected: the opt-in tests fail with `TypeError: read_memory() got an unexpected keyword argument`.

- [ ] **Step 3: Add the options and the derived limit**

In `_OwnedDockerHelper.read_memory`, change the signature and the `cfg`/`run` construction. Everything else in the method, including the identity checks and the `except BaseException` cleanup, stays as it is:

```python
    def read_memory(
        self,
        timeout_s=5.0,
        *,
        include_smaps=False,
        include_heap_info=False,
    ):
        """Return raw target procfs data; unavailable PSS is never zero-filled.

        The optional readings are omitted from the request entirely when not
        asked for, so the default path keeps its exact legacy request, response
        shape and 1 MiB transport limit.
        """
```

Build the request and pick the limit:

```python
            cfg = {
                "target": asdict(self.spec.target),
                "target_start_ticks": self.process_identity["start_ticks"],
                "helper_pid": holder["ns_pid"],
            }
            if include_smaps:
                cfg["include_smaps"] = True
            if include_heap_info:
                cfg["include_heap_info"] = True
                cfg["heap_info_deadline_s"] = max(0.5, timeout_s / 2)
            argv = ("exec", self.helper_id, PYTHON, WORKER, "memory", _encoded(cfg))
            limits = {"limit": _OPT_IN_RESPONSE_BYTES} if cfg.keys() - _BASE_KEYS else {}
            data = json.loads(self.commands.run(argv, **limits))
```

Define the two module-level constants next to the other module constants:

```python
# The worker's whole response arrives as one stdout blob, so the transport
# limit must cover worst-case JSON escaping of every enabled raw field plus
# bounded metadata. Legacy requests keep _DockerCommands.run's 1 MiB default.
_OPT_IN_RESPONSE_BYTES = 67108864
_BASE_KEYS = frozenset(("target", "target_start_ticks", "helper_pid"))
```

- [ ] **Step 4: Forward the options through both wrappers**

```python
    def read_memory(
        self, timeout_s=5.0, *, include_smaps=False, include_heap_info=False
    ):
        """Collect raw RSS/PSS evidence through the existing same-UID helper."""
        return self.executor.read_memory(
            timeout_s,
            include_smaps=include_smaps,
            include_heap_info=include_heap_info,
        )
```

Apply the same change to `PreparedDockerMemory.read_memory`, forwarding to `self._owner.read_memory`.

- [ ] **Step 5: Correct the stale cleanup comment**

The comment inside `except BaseException` currently reads "This operation only reads procfs." That stops being true once `include_heap_info` can attach to the JVM. Replace it:

```python
        except BaseException as error:
            # Procfs-only reads are cancelled by removing the helper cgroup,
            # which never stops the target. An opt-in GC.heap_info attaches to
            # the JVM, and killing jcmd does not prove the in-JVM command
            # finished, so the caller must not issue a later diagnostic on this
            # handle: close() marks it unusable and the caller rebinds.
```

- [ ] **Step 6: Run the tests and confirm GREEN**

Run the Step 2 command, then the whole file:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak/test_diagnostic_helper.py -q --no-cov
```

Expected: all pass, including every pre-existing test.

- [ ] **Step 7: Prove the soak path is untouched**

Run:

```bash
cd /home/michele/Documenti/nanolab && NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/soak packages/nanolab/tests/config/test_soak.py packages/nanolab/tests/plans/test_soak.py -q --no-cov
```

Expected: all pass with no edits to those files.

- [ ] **Step 8: Commit**

```bash
git add packages/nanolab/src/nanolab/tasks/soak/diagnostic_helper.py \
  packages/nanolab/tests/soak/test_diagnostic_helper.py
git commit -m "Carry the optional memory readings over the helper transport"
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

The categories here are the kernel's own. A mapping's pathname describes the
mapping, not the backing of every resident page inside it, so nothing in this
module classifies resident pages by name or attributes bytes to an allocator.
"""

from __future__ import annotations

import re

# A 64-bit glibc heap is 64 MiB, so 32 MiB catches one while staying clear of
# the JVM's smaller anonymous regions. This is a descriptive filter: JVM heap
# regions and other allocations pass it too, and mapping count is not arena
# count.
LARGE_MAPPING_BYTES = 33554432

_KB = re.compile(r"^(\w+):\s+(\d+) kB$", re.MULTILINE)
_HEAP = re.compile(r"garbage-first heap\s+total (\d+)K, used (\d+)K")
_METASPACE = re.compile(r"Metaspace\s+used (\d+)K, committed (\d+)K")
_HEADER = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+) (\S{4}) \S+ \S+ \S+\s*(\S.*)?$", re.MULTILINE
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


def parse_smaps(text: str) -> dict[str, object]:
    """Total each mapping by kernel category and list the large anonymous ones."""
    totals = {
        "anonymous": {"size": 0, "rss": 0, "pss": 0},
        "file": {"size": 0, "rss": 0, "pss": 0},
    }
    large: list[dict[str, object]] = []
    count = 0
    headers = list(_HEADER.finditer(text))
    for index, header in enumerate(headers):
        count += 1
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        fields = {
            name: _kilobytes(value)
            for name, value in _KB.findall(text[header.start() : end])
        }
        path = (header[4] or "").strip()
        backed = "file" if path and not path.startswith("[") else "anonymous"
        size = fields.get("Size", 0)
        totals[backed]["size"] += size
        totals[backed]["rss"] += fields.get("Rss", 0)
        totals[backed]["pss"] += fields.get("Pss", 0)
        if backed == "anonymous" and size >= LARGE_MAPPING_BYTES:
            large.append(
                {
                    "address": f"{header[1]}-{header[2]}",
                    "permissions": header[3],
                    "size": size,
                    "rss": fields.get("Rss", 0),
                    "pss": fields.get("Pss", 0),
                }
            )
    return {
        "mappings": count,
        "anonymous": totals["anonymous"],
        "file": totals["file"],
        "large_anonymous_mappings": {
            "count": len(large),
            "size": sum(int(item["size"]) for item in large),
            "rss": sum(int(item["rss"]) for item in large),
            "pss": sum(int(item["pss"]) for item in large),
            "mappings": large,
        },
    }


def residency(status: str | None, rollup: str | None) -> dict[str, int]:
    """Keep the kernel's own categories; a missing field stays absent, not zero."""
    values = dict(_KB.findall(status or "")) | dict(_KB.findall(rollup or ""))
    return {
        name: _kilobytes(values[name]) for name in _RESIDENCY if name in values
    }


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
            block[name] = {"available": True, **parse(response[name])}
        elif error is not None:
            block[name] = {"available": False, "error": error}
        else:
            block[name] = {"available": False}
    return block
```

- [ ] **Step 4: Run the tests and confirm GREEN**

Run the Step 2 command. Expected: all seven pass.

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/tasks/heap_analysis/native.py \
  packages/nanolab/tests/heap_analysis/test_native.py
git commit -m "Summarize the native memory readings"
```

---

### Task 5: Wire the readings into the session and reorder the final checkpoint

**Files:**
- Modify: `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py` (`observe` at line 498, the measurement sequence at lines 291-301)
- Test: `packages/nanolab/tests/heap_analysis/test_runtime.py`
- Modify: `docs/heap-analysis.md`

**Interfaces:**
- Consumes: `summarize` from Task 4 and `read_memory(..., include_smaps=True, include_heap_info=True)` from Task 3.
- Produces: the `native` block inside `evidence/runtime-<checkpoint>.json`, the raw files under `evidence/native/`, and the `native` comparison in `report.json`.

The measurement sequence currently ends:

```python
            session.observe("natural-drain")
            session.full_gc("final")
            final = session.heap_dump("final")
            session.observe("after-final-gc")
```

`GC.heap_dump` is issued without `-all` (see `diagnostics.py:484` and `diagnostic-worker.py:263`), so the dump triggers its own full GC and an observation taken after it cannot isolate the explicit GC before it. The observation moves ahead of the dump.

- [ ] **Step 1: Write the failing tests**

In `packages/nanolab/tests/heap_analysis/test_runtime.py`, update the existing `SAFETY_ORDER` list so `observe:after-final-gc` precedes `dump:final`:

```python
SAFETY_ORDER = [
    "deploy",
    "warmup",
    "observe:before-baseline",
    "gc:baseline",
    "dump:baseline",
    "steady",
    "drain",
    "observe:natural-drain",
    "gc:final",
    "observe:after-final-gc",
    "dump:final",
    "release-deployment",
    "mat",
    "cleanup-helper",
]
```

Add:

```python
def test_after_final_gc_is_observed_before_the_final_dump(events):
    assert events.index("observe:after-final-gc") < events.index("dump:final")
    assert events.index("gc:final") < events.index("observe:after-final-gc")


def test_observation_writes_the_native_block_and_its_raw_sources(tmp_path):
    session = fake_session_with_readings(
        tmp_path,
        smaps="7f00-7f01 rw-p 0 00:00 0\nSize: 65536 kB\nRss: 4 kB\nPss: 4 kB\n",
        heap_info="garbage-first heap   total 1024K, used 512K\n",
    )
    evidence = session.observe("natural-drain")
    document = json.loads(evidence.read_text())
    assert document["native"]["heap_info"]["heap"]["used"] == 512 * 1024
    root = evidence.parent / "native"
    assert (root / "natural-drain-smaps.txt").read_text().startswith("7f00-7f01")
    assert (root / "natural-drain-heap-info.txt").read_text().startswith("garbage")


def test_observation_records_a_failed_reading_without_failing_the_run(tmp_path):
    session = fake_session_with_readings(
        tmp_path, error={"smaps": "ValueError: procfs evidence exceeds read bound"}
    )
    document = json.loads(session.observe("natural-drain").read_text())
    assert document["native"]["smaps"]["available"] is False
    assert "exceeds read bound" in document["native"]["smaps"]["error"]
    assert not (
        session._writer.root / "native" / "natural-drain-smaps.txt"
    ).exists()
```

Add the fake beside the file's existing session fakes:

```python
def fake_session_with_readings(tmp_path, *, smaps=None, heap_info=None, error=None):
    """Build a real LocalHeapAnalysisSession whose helper returns fixed readings."""
    session = local_session(tmp_path)

    class Helper:
        def read_memory(self, timeout_s=5.0, **options):
            return {
                "status": "RssAnon:\t 4 kB\n",
                "smaps_rollup": "Pss_Anon:\t 4 kB\n",
                "smaps": smaps,
                "heap_info": heap_info,
                "errors": error or {},
            }

    session._helper = Helper()
    session._target = TARGET
    return session
```

`local_session` is the file's existing constructor for a real session over fake collaborators, added in the Task-3 fix wave of the heap-analysis plan; reuse it rather than writing a second one.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis/test_runtime.py -q --no-cov
```

Expected: the ordering test fails because the observation still runs after the dump, and the two native tests fail because `observe` writes no `native` block.

- [ ] **Step 3: Reorder the measurement sequence**

```python
            session.observe("natural-drain")
            session.full_gc("final")
            session.observe("after-final-gc")
            final = session.heap_dump("final")
```

- [ ] **Step 4: Collect the readings in `observe`**

At the top of `observe`, before the existing `write_json`, take the readings through the bound helper and keep them out of the terminal:

```python
    def observe(self, checkpoint: str) -> Path:
        """Record observed runtime evidence and seal its natural checkpoint."""
        targets = self._deployment.discover()
        observed = self._deployment.observations(targets)
        _target, helper = self._bind()
        readings = helper.read_memory(
            timeout_s=self._config.diagnostic_timeout_s,
            include_smaps=True,
            include_heap_info=True,
        )
        native = summarize(readings)
        root = self._root / "native"
        root.mkdir(parents=True, exist_ok=True)
        for name, suffix in (("smaps", "smaps.txt"), ("heap_info", "heap-info.txt")):
            if readings.get(name) is not None:
                (root / f"{checkpoint}-{suffix}").write_text(readings[name])
        evidence = self._writer.write_json(
            f"runtime-{checkpoint}.json",
            {
                "schema": "nanolab-soak-v1",
                "kind": "runtime_observation",
                "checkpoint": checkpoint,
                "observed": observed,
                "native": native,
            },
        )
```

Import `summarize` at the top of the module:

```python
from nanolab.tasks.heap_analysis.native import summarize
```

The rest of `observe`, including the natural-checkpoint block, stays unchanged. Note that `_bind()` is now called for every checkpoint, not only the two with a phase; it is idempotent and caches its helper.

- [ ] **Step 5: Separate a recorded source error from a transport failure**

These are two different failures and the spec treats them differently.

A per-source error arrives *inside* a successful response, in `readings["errors"]`. It is optional evidence: record it, publish no total from it, continue. Task 4's `summarize` already does this and the run stays `PASS`.

A transport failure is an exception out of `read_memory`, and `read_memory`'s own `except BaseException` has already called `self.close()`. Two things follow. The session must not keep the closed handle, or the mandatory `full_gc` and `heap_dump` will fail on it. And when `include_heap_info` was requested, killing the reader does not prove the in-JVM command finished, so issuing a further diagnostic on that target is exactly what the spec forbids.

So a transport failure during an optional reading is an infrastructure failure, not optional evidence. Let it propagate, after dropping the dead handle so nothing reuses it:

```python
        try:
            readings = helper.read_memory(
                timeout_s=self._config.diagnostic_timeout_s,
                include_smaps=True,
                include_heap_info=True,
            )
        except BaseException as error:
            # read_memory already closed the helper. Drop the cached handle so
            # no later operation reuses it, and do not continue: an opt-in
            # GC.heap_info may still be running inside the JVM, and issuing
            # another diagnostic while its completion is unresolved is exactly
            # what the diagnostic contract forbids.
            self._helper = None
            self._target = None
            raise RuntimeError(
                f"optional memory reading failed at {checkpoint}; "
                "in-JVM command completion unresolved"
            ) from error
```

Add the covering tests:

```python
def test_a_recorded_source_error_leaves_the_run_usable(tmp_path):
    session = fake_session_with_readings(
        tmp_path, error={"heap_info": "RuntimeError: attach timed out"}
    )
    document = json.loads(session.observe("natural-drain").read_text())
    assert document["native"]["heap_info"]["available"] is False
    assert session._helper is not None


def test_a_transport_failure_drops_the_helper_and_stops_the_run(tmp_path):
    session = local_session(tmp_path)

    class Broken:
        def read_memory(self, timeout_s=5.0, **options):
            raise RuntimeError("memory read failed; remote reader cleanup unconfirmed")

    session._helper = Broken()
    session._target = TARGET
    with pytest.raises(RuntimeError, match="completion unresolved"):
        session.observe("natural-drain")
    assert session._helper is None
```

Run them and confirm they fail before the change and pass after:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis/test_runtime.py -k "transport_failure or recorded_source_error" -q --no-cov
```

- [ ] **Step 6: Add the comparison to report.json**

Where `_publish` builds the terminal report, add a block that carries each checkpoint's native summary side by side:

```python
        "native": {
            checkpoint: json.loads((self._writer.root / f"runtime-{checkpoint}.json").read_text())["native"]
            for checkpoint in ("before-baseline", "natural-drain", "after-final-gc")
            if (self._writer.root / f"runtime-{checkpoint}.json").exists()
        },
```

- [ ] **Step 7: Run the tests and confirm GREEN**

Run the Step 2 command, then the whole heap-analysis suite:

```bash
cd /home/michele/Documenti/nanolab && uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/heap_analysis -q --no-cov
```

Expected: all pass.

- [ ] **Step 8: Document the readings**

In `docs/heap-analysis.md`, extend the artifact tree with:

```text
evidence/native/<checkpoint>-smaps.txt
evidence/native/<checkpoint>-heap-info.txt
```

and add a short section stating: the readings are bounded diagnostics, not free observations — `GC.heap_info` holds the JVM heap lock and is documented as medium impact; committed heap is not resident heap, so stable committed heap does not place an RSS increase outside the Java heap; large anonymous mappings are a descriptive filter and never an arena count; and a missing block means the reading failed, never zero.

- [ ] **Step 9: Run the full suite and the checks**

```bash
cd /home/michele/Documenti/nanolab && NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --frozen ruff check packages && uv run --frozen ruff format --check packages
uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab
uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run pre-commit run --all-files
```

Expected: all green. Bandit may flag the `/tmp` string in the new worker scratch path; if so add a same-line `# nosec` with its reason, as `mat.py` already does.

- [ ] **Step 10: Commit**

```bash
git add packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py \
  packages/nanolab/tests/heap_analysis/test_runtime.py docs/heap-analysis.md
git commit -m "Record the native memory readings at each heap-analysis checkpoint"
```

---

## Completion gate

Before claiming completion:

```bash
cd /home/michele/Documenti/nanolab && NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run pre-commit run --all-files
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

The soak suites must pass with no edits to `tests/soak/`, `tests/config/test_soak.py` or `tests/plans/test_soak.py` beyond the worker and transport tests added by Tasks 1-3, which is the evidence that the P24 path is unchanged.

No live run is added. The next real heap-analysis run validates the integration; synthetic tests do not establish the real collection overhead or whether the observation window reproduces the RSS drift.
