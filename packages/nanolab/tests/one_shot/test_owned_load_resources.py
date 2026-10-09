from subprocess import TimeoutExpired
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sonata_engine import TaskInputs

from nanolab.tasks.one_shot.experiment import LiveEvidenceCollector, generator_resource


@pytest.mark.parametrize("fails", [False, True])
def test_generator_child_is_reaped_and_streams_close_even_if_launch_fails(
    tmp_path, monkeypatch, fails
):
    streams = []
    actions = []

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            actions.append("terminate")

        def wait(self, **_):
            if self.returncode is None:
                raise TimeoutExpired("k6", 5)
            return self.returncode

        def kill(self):
            actions.append("kill")
            self.returncode = -9

    def start(argv, **kwargs):
        assert argv[0] == "k6"
        assert kwargs["env"]["ONE_SHOT_SCHEDULE"] == str(tmp_path / "schedule.json")
        streams.extend([kwargs["stdout"], kwargs["stderr"]])
        if fails:
            raise OSError("launch refused")
        return Child()

    monkeypatch.setattr("nanolab.tasks.one_shot.experiment.subprocess.Popen", start)
    resource = generator_resource(run_dir=tmp_path, vus=4, duration=20)
    inputs = TaskInputs._for_resources({}, set())
    if fails:
        with pytest.raises(OSError, match="launch refused"):
            resource.acquire(inputs)
    else:
        child = resource.acquire(inputs)
        resource.release(inputs, child)
        assert actions == ["terminate", "kill"]
    assert all(stream.closed for stream in streams)


def test_live_collector_failure_is_visible_and_owned_thread_is_joined(
    tmp_path, monkeypatch
):
    called = Event()

    def collect(*_):
        called.set()
        raise ValueError("SDK collection unavailable")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.collect_runtime_evidence", collect
    )
    collector = LiveEvidenceCollector(SimpleNamespace(run_dir=tmp_path), object())
    assert called.wait(2)
    collector.close()
    with pytest.raises(RuntimeError, match="collector failed"):
        collector.require_healthy()
    assert not collector.thread.is_alive()
    collector.close()


def test_live_collector_unjoined_thread_is_not_reported_as_clean():
    collector = cast(Any, object.__new__(LiveEvidenceCollector))
    collector.stop = Event()
    collector.thread = SimpleNamespace(join=lambda **_: None, is_alive=lambda: True)
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        collector.close()
