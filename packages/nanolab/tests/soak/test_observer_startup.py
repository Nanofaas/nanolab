from threading import Event

import pytest

from nanolab.tasks.soak.artifacts import ArtifactWriter, read_records
from nanolab.tasks.soak.models import Sample, Target
from nanolab.tasks.soak.observer import Observer, SystemClock


class GatedProbe:
    def __init__(self):
        self.release = Event()
        self.target = Target("cp", "container", 1, "started", "digest", "jvm")

    def targets(self):
        return (self.target,)

    def sample(self, target, phase, scheduled_s):
        if not self.release.wait(1):
            raise TimeoutError("test probe was not released")
        now = SystemClock().monotonic()
        return (
            Sample(
                target,
                phase,
                scheduled_s,
                now,
                now,
                "rss",
                (),
                "bytes",
                1,
                "observed",
                "fake",
                None,
            ),
        )


def test_first_sample_uses_configured_multi_role_budget(tmp_path, monkeypatch):
    probe = GatedProbe()
    writer = ArtifactWriter(tmp_path / "run", 10000)
    observer = Observer(probe, SystemClock(), writer, 60, startup_timeout_s=12)
    original_wait = observer._ready.wait
    budgets = []

    def simulated_six_second_collection(timeout):
        budgets.append(timeout)
        if timeout < 6:
            return False
        probe.release.set()
        return original_wait(1)

    monkeypatch.setattr(observer._ready, "wait", simulated_six_second_collection)
    try:
        observer.start("warmup")
    finally:
        probe.release.set()
        observer.stop(1)
        writer.close()
    assert budgets == [12]
    assert len(list(read_records(tmp_path / "run" / "samples.jsonl"))) == 1


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_startup_budget_is_rejected_before_start(tmp_path, timeout):
    writer = ArtifactWriter(tmp_path / "run", 10000)
    try:
        with pytest.raises(ValueError, match="expected positive finite duration"):
            Observer(GatedProbe(), SystemClock(), writer, 1, startup_timeout_s=timeout)
    finally:
        writer.close()


def test_expired_startup_cancels_and_can_be_joined(tmp_path):
    probe = GatedProbe()
    writer = ArtifactWriter(tmp_path / "run", 10000)
    observer = Observer(probe, SystemClock(), writer, 60, startup_timeout_s=0.01)
    try:
        with pytest.raises(
            TimeoutError, match="observer first sample exceeded startup t"
        ):
            observer.start("warmup")
        assert observer._cancelled.is_set()
    finally:
        probe.release.set()
        observer.stop(1)
        writer.close()
    assert not observer._thread.is_alive()  # pyright: ignore[reportOptionalMemberAccess]
