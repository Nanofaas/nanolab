"""The observation entry point uses the package Sonata builder."""


def test_observation_builder_and_tasks_live_in_package():
    from nanolab.plans.one_shot_observation import build_one_shot_observation_plan
    from nanolab.tasks.one_shot.observation import ConfigureOracle, ObservationFreeze

    assert callable(build_one_shot_observation_plan)
    assert ConfigureOracle.__module__ == "nanolab.tasks.one_shot.observation"
    assert ObservationFreeze.__module__ == "nanolab.tasks.one_shot.observation"


def test_external_runtime_asset_drift_is_rejected_before_use(tmp_path):
    import pytest

    from nanolab.tasks.one_shot.observation import sha, verify_files

    source = tmp_path / "source"
    source.mkdir()
    asset = source / "bootstrap.sh"
    asset.write_text("approved bootstrap")
    frozen = {
        "files": {},
        "source": str(source),
        "sourceFiles": {"bootstrap.sh": sha(asset)},
    }
    verify_files(tmp_path, frozen)
    asset.write_text("changed bootstrap")
    with pytest.raises(ValueError, match="external runtime asset changed"):
        verify_files(tmp_path, frozen)


def test_precheck_registration_failure_closes_acquired_sampler(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from typing import Any, cast

    import pytest

    from nanolab.tasks.one_shot import observation

    closed = []
    session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(observation, "VmTelemetrySession", lambda *a, **k: session)

    def failed_watch(*a, **k):
        raise OSError("cannot register sampler")

    supervision = SimpleNamespace(watch=failed_watch, unwatch=lambda *_: None)
    run = SimpleNamespace(
        run_dir=tmp_path,
        topology=object(),
        settings=object(),
        clock=object(),
        supervision=supervision,
    )
    inputs = SimpleNamespace(resource=lambda _: SimpleNamespace(refresh=lambda: None))
    with pytest.raises(OSError, match="cannot register sampler"):
        observation.ObserverPrecheck(run).run(cast(Any, inputs))
    assert closed == [True]


def test_added_external_source_file_invalidates_fingerprint(tmp_path):
    import subprocess

    import pytest

    from nanolab.tasks.one_shot.observation import sha, verify_files

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    asset = source / "asset.yaml"
    asset.write_text("approved")
    frozen = {
        "files": {},
        "source": str(source),
        "sourceFiles": {"asset.yaml": sha(asset)},
        "sourceInventoryComplete": True,
    }
    verify_files(tmp_path, frozen)
    (source / "new-function.yaml").write_text("new runtime definition")
    with pytest.raises(ValueError, match="source inventory changed"):
        verify_files(tmp_path, frozen)


def test_queue_preflight_requires_real_admission_and_records_capacity():
    import httpx
    import pytest

    from nanolab.tasks.one_shot.observation import verify_queue_admission

    for enabled in (False, True):
        invoked = False

        def handle(request, enabled=enabled):
            nonlocal invoked
            if request.method == "POST":
                invoked = True
                return httpx.Response(200, json={"status": "success"})
            count = 1 if enabled and invoked else 0
            return httpx.Response(
                200,
                text=(
                    f'sync_queue_admitted_total{{function="f"}} {count}\n'
                    'sync_queue_capacity{function="f"} 100\n'
                    'sync_queue_capacity{function=""} 200\n'
                ),
            )

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            if enabled:
                result = verify_queue_admission(client, "http://node:8080", "f", {})
                assert result["admittedAfter"] - result["admittedBefore"] == 1
                assert result["functionQueueCapacity"] == 100
                assert result["globalQueueCapacity"] == 200
            else:
                with pytest.raises(
                    RuntimeError, match="queue admission did not increase"
                ):
                    verify_queue_admission(client, "http://node:8080", "f", {})
