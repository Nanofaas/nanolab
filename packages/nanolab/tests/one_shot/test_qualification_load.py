import pytest


def test_qualification_load_requires_actual_emission_and_sdk_occupancy():
    from nanolab.tasks.one_shot.qualification_load import audit_load

    planned = [
        {"originalId": "x", "scheduledAt": 10, "origin": "edge", "function": "work"}
    ]
    emitted = {**planned[0], "event": "emitted", "emittedAt": 10.01}
    returned = {
        **emitted,
        "event": "result",
        "statusCode": 200,
        "responseStatus": "success",
        "terminalExecutionId": "terminal",
    }
    proof = {
        **planned[0],
        "executionId": "terminal",
        "incarnation": "runtime",
        "dispatchAttempt": "1",
        "state": "RELEASED",
        "handlerStarted": True,
        "occupancySeconds": 0.1,
    }
    args = {
        "schedule": planned,
        "rows": [emitted, returned],
        "proofs": [proof],
        "cutoff": 11,
        "max_lateness": 0.25,
    }
    assert audit_load(**args)["valid"]
    assert not audit_load(**{**args, "proofs": []})["valid"]
    assert not audit_load(**{**args, "rows": []})["valid"]
    assert not audit_load(**{**args, "proofs": [{**proof, "executionId": "wrong"}]})[
        "valid"
    ]
    assert not audit_load(**{**args, "rows": [{**emitted, "emittedAt": 11}, returned]})[
        "valid"
    ]


@pytest.mark.parametrize("failure", [None, "timeout", "child", "collector", "cleanup"])
def test_owned_load_compensates_generator_and_collector_on_failure(
    tmp_path, monkeypatch, failure
):
    from types import SimpleNamespace

    from nanolab.tasks.one_shot.qualification_load import QualificationLoad

    topology = SimpleNamespace(
        function_aliases={"work": "work"},
        function_settings={"work": SimpleNamespace(input={})},
        endpoints={"edge": "url"},
    )
    settings = SimpleNamespace(
        timing=SimpleNamespace(minimum_samples=1), flow_quantum=1, seed=7
    )
    inputs = SimpleNamespace(resource=lambda key: "http://edge")
    cell = SimpleNamespace(rates={"edge": {"work": 4}}, id="bounded")
    actions = []
    child = SimpleNamespace(poll=lambda: 0 if failure == "child" else None)

    def acquire(_):
        actions.append("acquire")
        return child

    def release(*args):
        actions.append("release")
        if failure == "cleanup":
            raise OSError("release failed")

    resource = SimpleNamespace(acquire=acquire, release=release)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.generator_resource",
        lambda **kwargs: resource,
    )

    class Collector:
        def __init__(self, *args):
            actions.append("collector")

        def require_healthy(self):
            if failure == "collector":
                raise RuntimeError("collector failed")

        def close(self):
            actions.append("close")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.LiveEvidenceCollector", Collector
    )
    monkeypatch.setattr(
        QualificationLoad,
        "audit",
        lambda *args, **kwargs: {"valid": failure != "timeout"},
    )
    if failure == "timeout":
        ticks = iter([0, 1, 31])
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.qualification_load.time.monotonic",
            lambda: next(ticks),
        )
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.qualification_load.time.sleep", lambda _: None
        )
    load = QualificationLoad(
        topology,
        settings,
        inputs,
        cell=cell,
        period=20,
        run_dir=tmp_path / "load",
        clock=SimpleNamespace(require_healthy=lambda: None),
    )
    if failure:
        with pytest.raises(RuntimeError) as error, load:
            raise RuntimeError("auction failed")
        if failure == "cleanup":
            assert any("release failed" in note for note in error.value.__notes__)
    else:
        with load:
            load.require_healthy()
    assert actions == ["acquire", "collector", "close", "release"]
