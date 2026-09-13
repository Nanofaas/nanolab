"""The lifecycle bridge must preserve an explicitly selected load policy."""

from types import SimpleNamespace

from nanolab.tasks.soak.workflow import make_workload_driver_factory


def test_bridge_preserves_fractional_rates_and_distinct_vu_limits(monkeypatch):
    observed = {}

    def driver(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("nanolab.tasks.soak.workload.K6WorkloadDriver", driver)
    config = SimpleNamespace(
        workload=SimpleNamespace(
            rates={"fn": 0.5},
            preallocated_vus=4,
            max_vus=8,
        ),
        roles={"control-plane": {}, "fn": {}},
        cancellation_timeout_s=2,
        artifact_limit_bytes=1024 * 1024,
        model_dump=lambda **kwargs: {"purpose": "smoke"},
    )
    digest = "registry/image@sha256:" + "a" * 64
    factory = make_workload_driver_factory(
        config,
        base_url="http://127.0.0.1:18080",
        payloads={"fn": [{"input": {}, "expected": {}}]},
        image_digests={"control-plane": digest, "fn": digest},
        command=("k6",),
        request_timeout_s=1,
    )
    factory("steady")
    assert observed["function_rates"] == {"fn": 0.5}
    assert observed["vus"] == 4
    assert observed["max_vus"] == 8
    assert observed["artifact_limit_bytes"] == 1024 * 1024
