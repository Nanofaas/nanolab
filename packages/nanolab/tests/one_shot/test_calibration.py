import httpx
import pytest

from nanolab.tasks.one_shot.calibration import measure_invocation, workload_checksum
from nanolab.tasks.one_shot.statistics import summarize_service


def transport(proof, *, timeout=False, output=42):
    def respond(request):
        if request.url.path == "/invoke":
            if timeout:
                raise httpx.ReadTimeout("caller timeout", request=request)
            return httpx.Response(200, json=output)
        if proof is None:
            return httpx.Response(404)
        return httpx.Response(200, json=proof)

    return httpx.Client(transport=httpx.MockTransport(respond))


def proof(**changes):
    return {
        "executionId": "sample",
        "incarnation": "vm-runtime",
        "dispatchAttempt": "1",
        "state": "RELEASED",
        "occupancySeconds": 0.125,
        "handlerStarted": True,
        "responseStatus": "SUCCESS",
        **changes,
    }


def measure(http):
    return measure_invocation(
        http,
        "http://runtime",
        payload={},
        execution_id="sample",
        expected_output=42,
        timeout_seconds=1,
        release_timeout_seconds=0.01,
    )


def test_service_uses_physical_occupancy_only():
    with transport(proof()) as http:
        sample = measure(http)
    assert sample["state"] == "complete"
    assert sample["occupancySeconds"] == 0.125
    assert sample["httpSeconds"] != 0.125


@pytest.mark.parametrize(
    ("runtime", "timeout", "state"),
    [
        (None, False, "missing"),
        (proof(state="ACTIVE", occupancySeconds=None), True, "censored"),
        (proof(), True, "censored"),
    ],
)
def test_incomplete_observations_are_preserved(runtime, timeout, state):
    with transport(runtime, timeout=timeout) as http:
        sample = measure(http)
    assert sample["state"] == state
    with pytest.raises(ValueError, match="incomplete"):
        summarize_service([sample], minimum_samples=1, relative_ci=0.1)


def test_wrong_execution_proof_or_checksum_is_rejected():
    with transport(proof(executionId="other")) as http:
        assert measure(http)["state"] == "error"
    with transport(proof(), output=43) as http:
        assert measure(http)["state"] == "error"


def test_finite_positive_samples_and_stability_are_required():
    samples = [{"state": "complete", "occupancySeconds": v} for v in [1, 1, 1]]
    stats = summarize_service(samples, minimum_samples=3, relative_ci=0.01)
    assert stats["meanSeconds"] == 1
    assert stats["qualified"]
    samples[-1]["occupancySeconds"] = 20
    assert not summarize_service(samples, minimum_samples=3, relative_ci=0.01)[
        "qualified"
    ]
    samples[-1]["occupancySeconds"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        summarize_service(samples, minimum_samples=3, relative_ci=0.1)


def test_declared_quantiles_are_reproducible():
    samples = [{"state": "complete", "occupancySeconds": v} for v in [1, 2, 3, 4]]
    first = summarize_service(
        samples,
        minimum_samples=4,
        relative_ci=1,
        quantiles=[0.5, 0.9],
        seed=19,
    )
    second = summarize_service(
        samples,
        minimum_samples=4,
        relative_ci=1,
        quantiles=[0.5, 0.9],
        seed=19,
    )
    assert first == second
    assert first["quantilesSeconds"] == {"0.5": 2.5, "0.9": 3.7}


def test_workload_checksum_matches_small_independent_corpus():
    assert workload_checksum({"iterations": 0, "working_set_bytes": 0, "seed": 7}) == 7
    assert (
        workload_checksum({"iterations": 0, "working_set_bytes": 1, "seed": 7}) == 231
    )


def test_measurement_failure_keeps_raw_samples_and_releases_resources(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from sonata_engine import Resource, Steps, Task, TaskOutcome, Workflow

    from nanolab.config.one_shot import OneShotConfig
    from nanolab.tasks.one_shot.calibration import MeasureServiceTask
    from nanolab.tasks.one_shot.clock import ClockMonitor

    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"f": {"input": {}}},
            "calibration": {"minSamples": 2, "maxSamples": 2, "repetitions": 1},
        }
    )
    released = []
    clock = Resource(
        title="Owned clock",
        acquire=lambda _: ClockMonitor(lambda: None, interval_seconds=10),
        release=lambda *_: released.append(True),
    )
    http = transport(None)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.calibration.httpx.Client", lambda **kw: http
    )

    class Prepared(Task[dict]):
        title = "Existing warmup evidence"

        def run(self, inputs):
            return TaskOutcome(
                value={
                    "outputs": {"f": 42},
                    "runtimes": {"edge-0/f": "http://runtime"},
                }
            )

    topology = SimpleNamespace(
        function_settings=settings.functions,
        resources=SimpleNamespace(nodes={"edge-0": object()}),
    )
    measure = MeasureServiceTask(topology, settings, tmp_path, clock=clock)
    workflow = Workflow("failed-calibration")
    workflow.add(
        Steps(title="Calibration", steps=(Prepared(), measure)), requires=(clock,)
    )
    with pytest.raises(ValueError, match="incomplete"):
        workflow.run()
    assert released == [True]
    rows = (tmp_path / "samples.jsonl").read_text().splitlines()
    assert len(rows) == 2
    assert all('"state":"missing"' in row for row in rows)
    assert not (tmp_path / "profile.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"iterations": -1, "working_set_bytes": 0, "seed": 1},
        {"iterations": 1, "working_set_bytes": 0, "seed": 2**64},
    ],
)
def test_invalid_workload_inputs_are_rejected(payload):
    with pytest.raises(ValueError, match="workload"):
        workload_checksum(payload)


def test_environment_fingerprint_includes_vm_os_but_not_run_addresses():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from nanolab.config.one_shot import OneShotConfig
    from nanolab.tasks.one_shot.calibration import environment_identity
    from nanolab.tasks.one_shot.preflight import NodeEvidence, TopologyEvidence

    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"f": {"input": {}}},
        }
    )
    node = NodeEvidence.model_validate(
        {
            "node_id": "edge-0",
            "kind": "edge",
            "vm_name": "run-first",
            "host": "10.0.0.1",
            "cpus": 2,
            "memory_mib": 3891,
            "memory_capacity_mib": 256,
            "backend": "container-local",
            "concurrency": 1,
            "hpa_active": False,
            "ready": True,
            "peer_count": 1,
            "image_digests": {"f": "sha256:" + "a" * 64},
            "clock_offset_seconds": 0.01,
            "clock_uncertainty_seconds": 0.01,
            "rtt_seconds": 0.02,
            "bandwidth_mbps": 1000,
            "os": "Ubuntu 26.04 kernel A",
            "architecture": "aarch64",
        }
    )
    topology = TopologyEvidence(
        measured_at=datetime.now(UTC),
        generator_location="operator-host",
        nodes=[node],
    )
    built = SimpleNamespace(
        source={"revision": "a" * 40}, recipe_sha256="b" * 64, components=[]
    )
    first, _ = environment_identity(settings, topology, built)
    node.host, node.vm_name = "10.0.0.2", "run-second"
    assert environment_identity(settings, topology, built)[0] == first
    node.os = "Ubuntu 26.04 kernel B"
    assert environment_identity(settings, topology, built)[0] != first


def test_output_oracle_matches_the_measured_rust_workload_golden():
    # SDK release receipts from the independent B3 Multipass run recorded this u64.
    assert (
        workload_checksum(
            {"iterations": 20000000, "working_set_bytes": 4096, "seed": 7}
        )
        == 10654779122033555500
    )


@pytest.mark.parametrize(
    "failure", ["handler-http", "proof-http", "dispatch", "occupancy"]
)
def test_invalid_runtime_transport_or_release_evidence_never_qualifies(failure):
    def respond(request):
        if request.url.path == "/invoke":
            return httpx.Response(503 if failure == "handler-http" else 200, json=42)
        if failure == "proof-http":
            raise httpx.ConnectError("proof unavailable", request=request)
        return httpx.Response(
            200,
            json=proof(
                dispatchAttempt="2" if failure == "dispatch" else "1",
                occupancySeconds=0 if failure == "occupancy" else 0.125,
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        sample = measure(http)
    assert sample["state"] != "complete"
    if failure == "proof-http":
        assert "proofError" in sample
