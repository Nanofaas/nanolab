import json
import time
from dataclasses import asdict, replace

import pytest

from nanolab.tasks.soak.artifacts import describe_artifact
from nanolab.tasks.soak.models import Target

TARGET = Target("cp", "container", 42, "started", "repo@sha256:" + "a" * 64, "jvm")
HELPER = "helper@sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("before", "after", "requested", "expected"),
    [
        (4, 4, True, False),
        (None, None, True, False),
        (4, 5, True, True),
        (4, 5, False, False),
        (5, 1, True, False),
        (-1, 1, True, False),
        (True, 2, True, False),
        (1, 2.0, True, False),
        (1, 2, 1, False),
    ],
)
def test_gc_command_success_is_not_collection_completion(
    before, after, requested, expected
):
    from nanolab.tasks.soak.diagnostics import gc_completed

    assert gc_completed(before, after, requested) is expected


def artifact(path, root):
    result = describe_artifact(path)
    result["path"] = str(path.relative_to(root))
    return result


class Executor:
    def __init__(self, identity=TARGET, mode="ok"):
        self.identity = identity
        self.mode = mode
        self.requests = []

    def inspect(self, target, timeout_s):
        return self.identity

    def execute(self, request):
        from nanolab.tasks.soak.diagnostics import DiagnosticOutcome

        self.requests.append(request)
        if self.mode == "timeout":
            raise TimeoutError("attach timed out")
        if self.mode == "attach-failure":
            return DiagnosticOutcome(1, False, 4, 4, None)
        if request.operation == "gc":
            event = {
                "schema": "nanolab-soak-v1",
                "kind": "full_gc_completed",
                "request_id": request.request_id,
                "target": asdict(request.target),
                "source": "verified-full-cycle",
                "started_s": request.started_s,
                "ended_s": time.monotonic(),
            }
            if self.mode == "wrong-full-cycle":
                event["kind"] = "young_gc_completed"
            if self.mode == "wrong-request":
                event["request_id"] = "other-request"
            path = request.output_dir / "gc-event.json"
            path.write_text(json.dumps(event))
            return DiagnosticOutcome(0, True, 4, 5, "gc-event.json")
        (request.output_dir / "capture.bin").write_bytes(b"captured")
        return DiagnosticOutcome(0, True, None, None, None)


def setup_adapter(
    tmp_path,
    *,
    runtime="jvm",
    mode="ok",
    operations=None,
    target=None,
    natural=True,
    private=True,
    max_dumps=1,
    natural_phase=None,
):
    from nanolab.tasks.soak.diagnostics import (
        DiagnosticBudget,
        DiagnosticCapabilities,
        JvmDiagnosticAdapter,
        NativeDiagnosticAdapter,
        NodeDiagnosticAdapter,
    )

    bound = replace(TARGET, runtime=runtime)
    evidence = tmp_path / "capabilities.json"
    evidence.write_text('{"observed":true}')
    samples = tmp_path / "natural.jsonl"
    samples.write_text('{"phase":"drain"}\n')
    natural_path = tmp_path / "natural-checkpoint.json"
    natural_path.write_text(
        json.dumps(
            {
                "schema": "nanolab-soak-v1",
                "kind": "natural_checkpoint",
                "completed": natural,
                "phase": "drain",
                "target": asdict(bound),
                "ended_s": time.monotonic(),
                "artifacts": [artifact(samples, tmp_path)],
            }
        )
    )
    observed = (
        operations
        if operations is not None
        else frozenset(("gc", "histogram", "heap_dump", "jfr"))
    )
    caps = DiagnosticCapabilities(
        bound, HELPER, observed, evidence, True, True, True, private
    )
    executor = Executor(target or bound, mode)
    cls = {
        "jvm": JvmDiagnosticAdapter,
        "node": NodeDiagnosticAdapter,
        "native": NativeDiagnosticAdapter,
    }[runtime]
    kwargs = {"command_prefix": ("/opt/jdk/bin/jcmd",)} if runtime == "jvm" else {}
    if natural_phase is not None:
        kwargs["natural_phase"] = natural_phase
    adapter = cls(
        caps,
        executor,
        DiagnosticBudget(max_dumps, 10000, 1000000),
        natural_checkpoint=natural_path,
        max_capture_bytes=10000,
        full_gc_source="verified-full-cycle",
        **kwargs,
    )
    return adapter, executor


def capture(adapter, tmp_path, operation="gc", target=TARGET):
    path = adapter.capture(target, operation, tmp_path / "capture", 1)
    return json.loads(path.read_text())


def test_verified_gc_receipt_preserves_identity_and_perturbation(tmp_path):
    adapter, executor = setup_adapter(tmp_path)
    receipt = capture(adapter, tmp_path)
    assert receipt["status"] == "PASS"
    assert receipt["scope"] == "diagnostic-only"
    assert receipt["full_gc_verified"] is True
    assert receipt["target"] == asdict(TARGET)
    assert receipt["helper_digest"] == HELPER
    assert receipt["perturbation"]["exclude_from_natural_windows"] is True
    assert receipt["artifacts"][0]["sha256"]
    assert executor.requests[0].argv == ("/opt/jdk/bin/jcmd", "42", "GC.run")


@pytest.mark.parametrize(
    "mode", ["wrong-full-cycle", "wrong-request", "attach-failure", "timeout"]
)
def test_incomplete_or_wrong_gc_cannot_pass(tmp_path, mode):
    adapter, _ = setup_adapter(tmp_path, mode=mode)
    receipt = capture(adapter, tmp_path)
    assert receipt["status"] == "INCONCLUSIVE"
    assert receipt["full_gc_verified"] is False


def test_wrong_pid_is_rejected_before_command(tmp_path):
    adapter, executor = setup_adapter(tmp_path, target=replace(TARGET, process_id=43))
    assert capture(adapter, tmp_path)["status"] == "INCONCLUSIVE"
    assert executor.requests == []


def test_natural_checkpoint_must_be_complete_before_intrusive_capture(tmp_path):
    adapter, executor = setup_adapter(tmp_path, natural=False)
    assert capture(adapter, tmp_path)["status"] == "INCONCLUSIVE"
    assert executor.requests == []


def test_default_adapter_still_requires_a_drain_checkpoint(tmp_path):
    adapter, executor = setup_adapter(tmp_path, natural_phase=None)
    (tmp_path / "natural-checkpoint.json").write_text(
        (tmp_path / "natural-checkpoint.json")
        .read_text()
        .replace('"drain"', '"baseline"')
    )
    assert capture(adapter, tmp_path)["status"] == "INCONCLUSIVE"
    assert executor.requests == []


def test_declared_natural_phase_must_match_the_checkpoint(tmp_path):
    adapter, executor = setup_adapter(tmp_path, natural_phase="baseline")
    assert capture(adapter, tmp_path)["status"] == "INCONCLUSIVE"
    assert executor.requests == []


def test_declared_baseline_phase_accepts_its_own_checkpoint(tmp_path):
    adapter, _executor = setup_adapter(tmp_path, natural_phase="baseline")
    (tmp_path / "natural-checkpoint.json").write_text(
        (tmp_path / "natural-checkpoint.json")
        .read_text()
        .replace('"phase": "drain"', '"phase": "baseline"')
    )
    assert capture(adapter, tmp_path)["status"] == "PASS"


def test_natural_artifact_corruption_blocks_intrusion(tmp_path):
    adapter, executor = setup_adapter(tmp_path)
    (tmp_path / "natural.jsonl").write_text("corrupted")
    assert capture(adapter, tmp_path)["status"] == "INCONCLUSIVE"
    assert executor.requests == []


def test_dump_budget_is_reserved_before_command(tmp_path):
    adapter, executor = setup_adapter(tmp_path, max_dumps=0)
    assert capture(adapter, tmp_path, "heap_dump")["status"] == "INCONCLUSIVE"
    assert executor.requests == []


def test_dump_count_shared_across_checkpoints(tmp_path):
    adapter, executor = setup_adapter(tmp_path)
    assert capture(adapter, tmp_path, "heap_dump")["status"] == "PASS"
    second = adapter.capture(TARGET, "heap_dump", tmp_path / "second", 1)
    assert json.loads(second.read_text())["status"] == "INCONCLUSIVE"
    assert len(executor.requests) == 1


def test_native_does_not_inherit_jvm_capabilities(tmp_path):
    adapter, executor = setup_adapter(
        tmp_path, runtime="native", operations=frozenset()
    )
    native = replace(TARGET, runtime="native")
    assert adapter.capabilities(native) == frozenset()
    receipt = capture(adapter, tmp_path, target=native)
    assert receipt["availability"] == "unavailable"
    assert adapter.metric_availability("jvm_memory_used_bytes") == "not_applicable"
    assert adapter.metric_availability("process_rss_bytes") == "unavailable"
    assert executor.requests == []


def test_node_uses_explicit_private_inspector_transport(tmp_path):
    adapter, executor = setup_adapter(tmp_path, runtime="node")
    node = replace(TARGET, runtime="node")
    assert capture(adapter, tmp_path, "heap_dump", node)["status"] == "PASS"
    assert executor.requests[0].protocol_method == "HeapProfiler.takeHeapSnapshot"
    assert executor.requests[0].argv == ()
    assert "jfr" not in adapter.capabilities(node)


def test_node_without_private_control_is_unsupported(tmp_path):
    adapter, executor = setup_adapter(tmp_path, runtime="node", private=False)
    node = replace(TARGET, runtime="node")
    assert adapter.capabilities(node) == frozenset()
    assert capture(adapter, tmp_path, target=node)["status"] == "INCONCLUSIVE"
    assert executor.requests == []


def test_occupied_capture_directory_is_preserved(tmp_path):
    adapter, executor = setup_adapter(tmp_path)
    directory = tmp_path / "capture"
    directory.mkdir()
    (directory / "prior.json").write_text("prior")
    with pytest.raises((ValueError, FileExistsError)):
        adapter.capture(TARGET, "gc", directory, 1)
    assert (directory / "prior.json").read_text() == "prior"
    assert executor.requests == []


def attribution(tmp_path):
    path = tmp_path / "analysis.txt"
    path.write_text("owner inspection")
    policy = tmp_path / "policy.json"
    policy.write_text('{"immutable":"policy"}')
    return {
        "schema": "nanolab-soak-v1",
        "criterion_id": "rss-review",
        "status": "resolved",
        "owner": "queue",
        "population": "live queue entries",
        "expected_lifetime_s": 30,
        "remaining_bytes": 10,
        "budget_bytes": 20,
        "rationale": "bounded live owner state",
        "reviewer": "operator",
        "policy_override": False,
        "policy_artifact": artifact(policy, tmp_path),
        "artifacts": [artifact(path, tmp_path)],
    }


def test_valid_attribution_has_narrow_scope(tmp_path):
    from nanolab.tasks.soak.diagnostics import validate_attribution

    result = validate_attribution(attribution(tmp_path), tmp_path)
    assert result.status == "PASS"
    assert "does not waive" in result.reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer", ""),
        ("owner", ""),
        ("status", "unresolved"),
        ("expected_lifetime_s", -1),
        ("policy_override", True),
        ("remaining_bytes", True),
    ],
)
def test_incomplete_attribution_cannot_pass(tmp_path, field, value):
    from nanolab.tasks.soak.diagnostics import validate_attribution

    record = attribution(tmp_path)
    record[field] = value
    assert validate_attribution(record, tmp_path).status == "INCONCLUSIVE"


def test_attribution_cannot_waive_budget(tmp_path):
    from nanolab.tasks.soak.diagnostics import validate_attribution

    record = attribution(tmp_path)
    record["remaining_bytes"] = 21
    assert validate_attribution(record, tmp_path).status == "FAIL"


def test_corrupt_or_outside_attribution_artifact_cannot_pass(tmp_path):
    from nanolab.tasks.soak.diagnostics import validate_attribution

    record = attribution(tmp_path)
    (tmp_path / "analysis.txt").write_text("changed")
    assert validate_attribution(record, tmp_path).status == "INCONCLUSIVE"
    record["artifacts"][0]["path"] = "../outside"
    assert validate_attribution(record, tmp_path).status == "INCONCLUSIVE"
