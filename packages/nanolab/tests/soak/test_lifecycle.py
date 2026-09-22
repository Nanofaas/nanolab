from types import SimpleNamespace
from typing import cast

import pytest
from sonata_engine import Workflow

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.workflow import LifecycleHooks, SoakLifecycle, phase_order


def fake_config(**fields: object) -> SoakConfig:
    """Build a config stand-in; these tests never read its unset fields."""
    return cast(SoakConfig, SimpleNamespace(**fields))


def make_lifecycle(
    tmp_path, events, failure=None, under_load=None, driver=None, steady_s=5400
):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

        def wait_until(self, deadline, cancelled):
            self.now = deadline
            return not cancelled.is_set()

    clock = Clock()

    def action(name):
        events.append(name)
        if failure == name:
            raise RuntimeError(name)

    class Observer:
        def start(self, phase):
            action("observer-start")

        def set_phase(self, phase):
            action(phase)

        def stop(self, timeout):
            action("observer-stop")

    class Driver:
        def __init__(self, phase):
            self.phase = phase

        def run(self, output, duration, cancelled):
            action("load-" + self.phase)
            clock.now += duration
            return output / "receipt.json"

        def stop(self, timeout):
            action("stop-" + self.phase)

    config = fake_config(
        phases=SimpleNamespace(
            warmup_s=10,
            baseline_drain_s=2100,
            baseline_window_s=30,
            steady_s=steady_s,
            drain_s=2100,
        ),
        cancellation_timeout_s=1,
        diagnostics=SimpleNamespace(timeout_s=1),
    )
    hooks = LifecycleHooks(
        preflight=lambda: action("preflight"),
        prerequisites=lambda: action("prerequisites"),
        baseline_capture=lambda state, timeout: action("baseline-capture"),
        final_capture=lambda state, timeout: action("capture"),
        evaluate=lambda state: action("evaluate"),
        report=lambda state: (action("report"), tmp_path / "report.json")[1],
    )
    return SoakLifecycle(
        config,
        observer=Observer(),  # pyright: ignore[reportArgumentType]
        clock=clock,  # pyright: ignore[reportArgumentType]
        driver_factory=driver or Driver,  # pyright: ignore[reportArgumentType]
        hooks=hooks,
        run_dir=tmp_path,
        under_load=under_load,
    )


def test_phase_contract():
    order = phase_order()
    assert order.index("freeze-digests") < order.index("deploy")
    assert order.index("drain") < order.index("final-diagnostics")
    assert order.index("report") < order.index("cleanup")


def test_actual_sonata_lifecycle_full_schedule(tmp_path):
    events = []
    task = make_lifecycle(tmp_path, events)
    Workflow("soak").add(task).run()
    assert events.count("observer-start") == 1
    assert events[-4:] == ["capture", "observer-stop", "evaluate", "report"]
    assert task.state.windows["steady"][1] - task.state.windows["steady"][0] == 5400
    assert task.state.windows["drain"][1] - task.state.windows["drain"][0] == 2100


@pytest.mark.parametrize(
    "failure",
    [
        "preflight",
        "prerequisites",
        "observer-start",
        "load-warmup",
        "baseline",
        "load-steady",
        "drain",
        "capture",
        "observer-stop",
        "evaluate",
        "report",
    ],
)
def test_faults_preserve_finalization(tmp_path, failure):
    events = []
    task = make_lifecycle(tmp_path, events, failure)
    with pytest.raises(RuntimeError, match=failure):
        Workflow("soak").add(task).run()
    assert "capture" in events
    assert "evaluate" in events
    assert events[-1] == "report"
    if "load-steady" in events:
        assert events.index("stop-steady") < events.index("capture")


def test_cancellation_still_reports(tmp_path):
    events = []
    task = make_lifecycle(tmp_path, events)
    task.cancelled.set()
    with pytest.raises(KeyboardInterrupt, match="soak cancelled"):
        Workflow("soak").add(task).run()
    assert task.state.aborted
    assert events == ["capture", "evaluate", "report"]


def test_the_switch_step_runs_beside_the_steady_load(tmp_path):
    """Both run in the same window, and the step's receipt is kept."""
    events = []
    seen: list[tuple[float, object]] = []

    def under_load(window_s, cancelled):
        seen.append((window_s, cancelled))
        events.append("switch-steady")
        receipt = tmp_path / "scheduler-switch.json"
        receipt.write_text("{}")
        return receipt

    task = make_lifecycle(tmp_path, events, under_load=under_load)
    Workflow("soak").add(task).run()

    # The switch ran inside the steady window, not before or after it.
    assert "switch-steady" in events
    assert events.index("load-steady") < events.index("capture")
    assert "switch-steady" in events[: events.index("capture")]
    # It was handed the steady phase's own duration and the run's cancellation.
    assert seen == [(5400, task.cancelled)]
    # The receipt is the published path, not an in-memory object: it is what the
    # manifest references, exactly as the workload receipt is.
    assert task.state.switch_receipt == tmp_path / "scheduler-switch.json"
    assert task.state.workload_receipts["steady"].name == "receipt.json"


def test_a_failed_switch_step_fails_the_run_with_the_load_receipt_beside_it(tmp_path):
    """A switch that fails does not discard the load it ran under."""
    events = []

    def under_load(window_s, cancelled):
        events.append("switch-steady")
        raise RuntimeError("switches committed, budget 1000")

    task = make_lifecycle(tmp_path, events, under_load=under_load)
    with pytest.raises(RuntimeError, match="switches committed, budget 1000"):
        Workflow("soak").add(task).run()

    # The load's own receipt survived, and the run still finalized and reported.
    assert task.state.workload_receipts["steady"].name == "receipt.json"
    assert task.state.switch_receipt is None
    assert events[-3:] == ["observer-stop", "evaluate", "report"]


def test_a_switch_failure_stops_the_load_instead_of_waiting_out_the_window(tmp_path):
    """A failure one second in must not be held for the rest of the window.

    The executor's shutdown waits for its survivors, so without the stop signal
    a switch that blows its budget at t=0 in a ninety-minute steady phase is
    reported eighty-nine minutes later with the observer still sampling. An
    operator meets that with Ctrl-C, which turns a FAIL into an ABORT and loses
    the evidence.
    """
    import time

    events = []

    class SlowLoad:
        """Sleeps out the steady window unless it is cancelled first."""

        def __init__(self, phase):
            self.phase = phase

        def run(self, output, duration, cancelled):
            events.append("load-" + self.phase)
            if self.phase == "steady":
                deadline = time.monotonic() + duration
                while time.monotonic() < deadline and not cancelled.wait(0.01):
                    pass
            events.append("returned-" + self.phase)
            output.mkdir(parents=True, exist_ok=True)
            receipt = output / "receipt.json"
            receipt.write_text("{}")
            return receipt

        def stop(self, timeout):
            events.append("stop-" + self.phase)

    def under_load(window_s, cancelled):
        events.append("switch-steady")
        raise RuntimeError("switches committed, budget 1000")

    task = make_lifecycle(
        tmp_path, events, under_load=under_load, driver=SlowLoad, steady_s=20
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="budget 1000"):
        Workflow("soak").add(task).run()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"the failure was held for the whole window ({elapsed:.1f}s)"
    assert "returned-steady" in events
    assert task.state.workload_receipts["steady"].name == "receipt.json"


def test_a_load_failure_is_the_one_reported_when_the_switch_then_stops(tmp_path):
    """The switch's short count is a consequence, not a second fault."""
    import time

    events = []

    class FailingLoad:
        def __init__(self, phase):
            self.phase = phase

        def run(self, output, duration, cancelled):
            events.append("load-" + self.phase)
            if self.phase == "steady":
                raise RuntimeError("generator died")
            output.mkdir(parents=True, exist_ok=True)
            receipt = output / "receipt.json"
            receipt.write_text("{}")
            return receipt

        def stop(self, timeout):
            events.append("stop-" + self.phase)

    def under_load(window_s, cancelled):
        events.append("switch-steady")
        # Bounded, so a missing stop signal fails this test rather than hanging
        # it: the whole point is that something has to set the cancellation.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not cancelled.wait(0.01):
            pass
        raise RuntimeError("0 switches committed, budget 1000")

    task = make_lifecycle(
        tmp_path, events, under_load=under_load, driver=FailingLoad, steady_s=20
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="generator died"):
        Workflow("soak").add(task).run()
    assert time.monotonic() - started < 5.0
    assert "switch-steady" in events


def test_a_load_failure_keeps_the_receipt_the_load_wrote_before_it_died(tmp_path):
    """The other half of the same hinge, on the load's side of it.

    `K6WorkloadDriver` persists its receipt in a `finally` and re-raises after,
    so a generator that dies mid-steady leaves `workload-receipt.json` on disk.
    Reading only the future's result drops it: the manifest then references no
    workload evidence for a run whose load is exactly what failed.
    """
    import time

    events = []

    class DyingLoad:
        def __init__(self, phase):
            self.phase = phase

        def run(self, output, duration, cancelled):
            events.append("load-" + self.phase)
            output.mkdir(parents=True, exist_ok=True)
            receipt = output / "workload-receipt.json"
            if self.phase == "steady":
                # What the driver does before it raises: the receipt is written
                # and then the failure is surfaced.
                receipt.write_text('{"kind": "workload"}')
                raise RuntimeError("generator died")
            receipt.write_text("{}")
            return receipt

        def stop(self, timeout):
            events.append("stop-" + self.phase)

    def under_load(window_s, cancelled):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not cancelled.wait(0.01):
            pass
        raise RuntimeError("0 switches committed, budget 1000")

    task = make_lifecycle(
        tmp_path, events, under_load=under_load, driver=DyingLoad, steady_s=20
    )
    with pytest.raises(RuntimeError, match="generator died"):
        Workflow("soak").add(task).run()

    kept = task.state.workload_receipts["steady"]
    assert kept == tmp_path / "steady" / "workload-receipt.json"
    assert kept.is_file()


def test_a_soak_without_a_switch_step_runs_the_load_alone(tmp_path):
    events = []
    task = make_lifecycle(tmp_path, events)
    Workflow("soak").add(task).run()

    assert task.state.switch_receipt is None
    assert events.count("load-steady") == 1


def test_startup_budget_covers_all_sequential_scrapes():
    from nanolab.tasks.soak.workflow import observer_startup_timeout

    config = fake_config(
        roles={"control-plane": None, "java": None, "node": None}, scrape_timeout_s=2.0
    )
    assert observer_startup_timeout(config, overhead_s=5.0) == 11.0


def test_missing_live_runner_is_blocked_without_acquiring_artifact_root(tmp_path):
    from nanolab.tasks.soak.workflow import run_prerequisite_gate

    config = fake_config(
        purpose="p24",
        prerequisites=SimpleNamespace(required_coverage=["sync"], mode="run"),
    )
    with pytest.raises(RuntimeError, match="INCONCLUSIVE"):
        run_prerequisite_gate(
            config,
            inputs={},
            runner=None,
            run_dir=tmp_path / "run",
            receipt_root=tmp_path,
            timeout_s=60,
        )
    assert not (tmp_path / "run").exists()


def test_workload_bridge_rejects_missing_role_coverage():
    from nanolab.tasks.soak.workflow import make_workload_driver_factory

    config = fake_config(
        workload=SimpleNamespace(rates={"fn": 1.0}),
        roles={"control-plane": None, "fn": None},
    )
    with pytest.raises(ValueError, match=r"workload payloads and frozen images"):
        make_workload_driver_factory(
            config, base_url="http://127.0.0.1:8080", payloads={}, image_digests={}
        )


def test_workload_bridge_is_lazy_and_binds_the_landed_driver(tmp_path):
    from nanolab.tasks.soak.workflow import make_workload_driver_factory
    from nanolab.tasks.soak.workload import K6WorkloadDriver

    config = fake_config(
        workload=SimpleNamespace(preallocated_vus=2, max_vus=2, rates={"fn": 1.0}),
        roles={"control-plane": None, "fn": None},
        cancellation_timeout_s=2.0,
        artifact_limit_bytes=1024 * 1024,
        model_dump=lambda **kwargs: {"purpose": "smoke"},
    )
    factory = make_workload_driver_factory(
        config,
        base_url="http://127.0.0.1:8080",
        payloads={"fn": [{"input": {}, "expected": {}}]},
        image_digests=dict.fromkeys(config.roles, "image@sha256:" + "a" * 64),
        command=("nonexistent-k6-never-executed",),
    )
    assert isinstance(factory("warmup"), K6WorkloadDriver)
    assert isinstance(factory("steady"), K6WorkloadDriver)
    with pytest.raises(ValueError, match="workload may run only during warmup or s"):
        factory("drain")
