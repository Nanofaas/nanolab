import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.contracts import contract_asset
from nanolab.one_shot.models import CalibrationProfile, TimingQualification
from nanolab.tasks.one_shot.qualification import QualifyTimingTask
from nanolab.tasks.one_shot.timing import WallTimingSample


def task_fixture(tmp_path, monkeypatch, *, censored=False, release=True):
    preset = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/presets/scenarios/one-shot-qualification.yaml"
    )
    data = yaml.safe_load(preset.read_text())["oneShot"]
    data["profile"] = {"path": str(tmp_path / "profile.json"), "sha256": "a" * 64}
    settings = OneShotConfig.model_validate(data)
    document = json.loads(
        contract_asset("examples/synthetic-profile.json").read_bytes()
    )
    document.update(provider="multipass", synthetic=False)
    profile = CalibrationProfile.model_validate(document)
    calls = []

    class Client:
        def __init__(self, url, **kwargs):
            self.base_url = url

        def load_profile(self, *args, **kwargs):
            calls.append((self.base_url, "profile"))

        def status(self):
            return SimpleNamespace(revision=0)

        def drain_and_release(self):
            calls.append((self.base_url, "release"))
            return release

    target = "nanolab.tasks.one_shot.qualification."
    monkeypatch.setattr(target + "NanoFaasOneShotClient", Client)
    monkeypatch.setattr(
        target + "verify_profile_scope", lambda *args: profile.environment_fingerprint
    )
    monkeypatch.setattr(target + "configuration_for_node", lambda *args, **kwargs: {})
    monkeypatch.setattr(target + "configure_node", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        target + "upload_window", lambda *args, **kwargs: kwargs["revision"] + 1
    )
    monkeypatch.setattr(target + "wait_until", lambda *args, **kwargs: None)

    def prepare(*args, **kwargs):
        calls.append((kwargs["epoch"], "prepare"))
        return WallTimingSample(
            None if censored else 0.1, None if censored else 0.2, censored
        ), {"censored": censored}

    monkeypatch.setattr(target + "prepare_parallel_epoch", prepare)
    monkeypatch.setattr(
        target + "capture_replica_diagnostics",
        lambda *args, **kwargs: calls.append((kwargs["epoch"], "diagnostics")),
    )
    topology = SimpleNamespace(
        resources=SimpleNamespace(
            nodes={node.id: SimpleNamespace(config=node) for node in settings.nodes}
        ),
        distribution="built",
        endpoints={node.id: node.id for node in settings.nodes},
        function_aliases={"one-shot-workload-rust": "one-shot-workload"},
        function_settings={
            "one-shot-workload": settings.functions["one-shot-workload-rust"]
        },
    )
    monitor = SimpleNamespace(require_healthy=lambda: None)
    inputs = SimpleNamespace(
        resource=lambda key: monitor if key == "clock" else key, upstream=lambda: None
    )
    task = QualifyTimingTask(
        topology, settings, profile, clock=cast(Any, "clock"), run_dir=tmp_path
    )  # pyright: ignore[reportArgumentType]
    return task, cast(Any, inputs), calls


def test_complete_matrix_publishes_immutable_qualification(tmp_path, monkeypatch):
    task, inputs, calls = task_fixture(tmp_path, monkeypatch)
    result = task.run(inputs).value
    assert result is not None and result.qualified
    assert result.sample_count == 20 and len(result.matrix) == 2
    assert result.period_seconds == 20 and result.lead_seconds == pytest.approx(1.9)
    assert [row[0] for row in calls if row[1] == "prepare"] == list(range(20))
    assert len([row for row in calls if row[1] == "release"]) == 2
    assert (
        TimingQualification.model_validate_json((tmp_path / "timing.json").read_bytes())
        == result
    )


def test_censored_matrix_is_retained_and_never_qualified(tmp_path, monkeypatch):
    task, inputs, calls = task_fixture(tmp_path, monkeypatch, censored=True)
    with pytest.raises(ValueError, match="NOT_QUALIFIED"):
        task.run(inputs)
    result = TimingQualification.model_validate_json(
        (tmp_path / "timing.json").read_bytes()
    )
    assert not result.qualified and result.censored_count == 20
    assert [row[0] for row in calls if row[1] == "diagnostics"] == list(range(20))
    assert result.samples_seconds == [None] * 20
    assert len((tmp_path / "matrix.jsonl").read_text().splitlines()) == 20


def test_unconfirmed_replica_release_does_not_publish_success(tmp_path, monkeypatch):
    task, inputs, _calls = task_fixture(tmp_path, monkeypatch, release=False)
    with pytest.raises(RuntimeError, match="release unconfirmed"):
        task.run(inputs)
    assert not (tmp_path / "timing.json").exists()
    assert len((tmp_path / "matrix.jsonl").read_text().splitlines()) == 20


def test_censored_transition_diagnostics_keep_container_identity_and_lookup_failure(
    tmp_path, monkeypatch
):
    from nanolab.tasks.one_shot.qualification import capture_replica_diagnostics

    def lookup(node, name, **kwargs):
        if kwargs["replica"] == 2:
            raise RuntimeError("replica missing")
        return "http://runtime", {"State": {"StartedAt": "timestamp"}}

    monkeypatch.setattr("nanolab.tasks.one_shot.qualification.runtime_endpoint", lookup)
    topology = SimpleNamespace(
        resources=SimpleNamespace(nodes={"edge-0": object()}),
        function_settings={"work": SimpleNamespace(max_replicas=2)},
    )
    path = tmp_path / "replicas.jsonl"
    capture_replica_diagnostics(topology, object(), path, epoch=10)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["container"]["State"]["StartedAt"] == "timestamp"
    assert rows[1]["error"] == "replica missing"
    assert all(row["epoch"] == 10 for row in rows)
