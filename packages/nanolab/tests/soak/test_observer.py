from threading import Event

import pytest

from nanolab.tasks.soak.artifacts import ArtifactWriter, read_records
from nanolab.tasks.soak.models import Sample, Target


class FakeClock:
    def __init__(self, rounds):
        self.now = 100.0
        self.rounds = rounds
        self.finished = Event()

    def monotonic(self):
        return self.now

    def wait_until(self, deadline_s, cancelled):
        if self.rounds == 0 or cancelled.is_set():
            self.finished.set()
            return False
        self.rounds -= 1
        self.now = max(self.now, deadline_s)
        return True


class Probe:
    def __init__(self, clock, delay=0):
        self.clock = clock
        self.delay = delay
        self.count = 0

    def targets(self):
        return (Target("cp", "container", 1, "start", "digest", "jvm"),)

    def sample(self, target, phase, scheduled_s):
        start = self.clock.monotonic()
        self.clock.now += self.delay
        self.count += 1
        return (
            Sample(
                target,
                phase,
                scheduled_s,
                start,
                self.clock.monotonic(),
                "metric",
                (("changing", str(self.count)),),
                "bytes",
                1.0,
                "observed",
                "fake",
                None,
            ),
        )


def test_absolute_deadlines():
    from nanolab.tasks.soak.observer import sample_deadlines

    assert list(sample_deadlines(100, 130, 10)) == [100, 110, 120, 130]


@pytest.mark.parametrize(
    "args", [(0, 1, 0), (0, 1, -1), (2, 1, 1), (0, float("inf"), 1)]
)
def test_invalid_schedule(args):
    from nanolab.tasks.soak.observer import sample_deadlines

    with pytest.raises(
        ValueError, match=r"expected positive finite|invalid observation window"
    ):
        list(sample_deadlines(*args))


def test_slow_scrapes_skip_deadlines_and_persist_drain(tmp_path):
    from nanolab.tasks.soak.observer import Observer

    clock = FakeClock(3)
    writer = ArtifactWriter(tmp_path / "run", 100000)
    observer = Observer(Probe(clock, 25), clock, writer, 10)
    observer.start("drain")
    assert clock.finished.wait(2)
    observer.stop(2)
    observer.stop(2)
    rows = list(read_records(tmp_path / "run" / "samples.jsonl"))
    assert [r["scheduled_s"] for r in rows] == [100, 130, 160]
    assert all(r["phase"] == "drain" for r in rows)
    events = list(read_records(tmp_path / "run" / "events.jsonl"))
    assert any(r["kind"] == "observation_gap" for r in events)
    writer.close()


def test_full_disk_is_not_silent(tmp_path):
    from nanolab.tasks.soak.observer import Observer

    clock = FakeClock(3)
    observer = Observer(Probe(clock), clock, ArtifactWriter(tmp_path / "run", 128), 1)
    observer.start("steady")
    with pytest.raises(RuntimeError, match="observer failed"):
        observer.stop(2)


def test_fixed_state_with_100000_changing_labels():
    from nanolab.tasks.soak.observer import Observer

    class CountingWriter:
        def __init__(self):
            self.count = 0

        def append(self, stream, record):
            if stream == "samples":
                self.count += 1

    clock = FakeClock(100000)
    writer = CountingWriter()
    observer = Observer(Probe(clock), clock, writer, 1)  # pyright: ignore[reportArgumentType]
    observer.start("steady")
    assert clock.finished.wait(15)
    observer.stop(2)
    assert writer.count == 100000
    assert not any(isinstance(v, (list, dict, set)) for v in vars(observer).values())


def test_cancellation_and_phase_transition(tmp_path):
    from nanolab.tasks.soak.observer import Observer, SystemClock

    class LiveProbe(Probe):
        def sample(self, target, phase, scheduled_s):
            now = self.clock.monotonic()
            sampled.set()
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

    sampled = Event()
    clock = SystemClock()
    writer = ArtifactWriter(tmp_path / "run", 100000)
    observer = Observer(LiveProbe(clock), clock, writer, 60)
    observer.start("warmup")
    assert sampled.wait(2)
    observer.set_phase("drain")
    observer.stop(2)
    assert any(
        row.get("phase") == "drain"
        for row in read_records(tmp_path / "run" / "events.jsonl")
    )
    with pytest.raises(RuntimeError, match="observer cannot be restarted"):
        observer.start("steady")


def test_cumulative_budget_guard_aborts_the_run_and_preserves_evidence(tmp_path):
    """Every generated file counts, not only what the writer itself persisted."""
    from nanolab.tasks.soak.artifacts import ArtifactLimitExceededError, enforce_limit
    from nanolab.tasks.soak.observer import Observer

    run = tmp_path / "run"
    clock = FakeClock(5)
    writer = ArtifactWriter(run / "evidence", 100000)
    # A journal written outside the writer, exactly the case the writer's own
    # accounting cannot see.
    (run / "runtime-journal.jsonl").write_bytes(b"x" * 4096)

    observer = Observer(
        Probe(clock),
        clock,
        writer,
        10,
        budget=lambda: enforce_limit(run, 4000),
    )
    observer.start("steady")
    with pytest.raises(RuntimeError, match="observer failed"):
        observer.stop(5)
    assert isinstance(observer.failure(), ArtifactLimitExceededError)
    # Samples taken before the abort survive.
    assert list(read_records(run / "evidence" / "samples.jsonl"))


def test_cumulative_budget_guard_allows_a_run_inside_its_limit(tmp_path):
    from nanolab.tasks.soak.artifacts import enforce_limit
    from nanolab.tasks.soak.observer import Observer

    run = tmp_path / "run"
    clock = FakeClock(3)
    writer = ArtifactWriter(run / "evidence", 100000)
    observer = Observer(
        Probe(clock), clock, writer, 10, budget=lambda: enforce_limit(run, 10**9)
    )
    observer.start("steady")
    clock.finished.wait(5)
    observer.stop(5)
    assert len(list(read_records(run / "evidence" / "samples.jsonl"))) == 3


def test_measure_tree_counts_every_file_but_not_build_workspaces(tmp_path):
    from nanolab.tasks.soak.artifacts import measure_tree

    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "samples.jsonl").write_bytes(b"a" * 10)
    (tmp_path / "build-command-logs").mkdir()
    (tmp_path / "build-command-logs" / "b.log").write_bytes(b"b" * 20)
    (tmp_path / "runtime-journal.jsonl").write_bytes(b"c" * 30)
    (tmp_path / "workspace-cp").mkdir()
    (tmp_path / "workspace-cp" / "huge").write_bytes(b"d" * 1000)
    (tmp_path / ".soak-owner").write_bytes(b"e" * 500)

    assert measure_tree(tmp_path) == 60


def test_enforce_limit_returns_the_measured_total_when_it_fits(tmp_path):
    from nanolab.tasks.soak.artifacts import ArtifactLimitExceededError, enforce_limit

    (tmp_path / "a.log").write_bytes(b"x" * 100)

    assert enforce_limit(tmp_path, 100) == 100
    with pytest.raises(
        ArtifactLimitExceededError, match=r"cumulative run artifacts 100 exceed"
    ):
        enforce_limit(tmp_path, 99)
