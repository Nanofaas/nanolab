from types import SimpleNamespace
from typing import cast

import pytest
from sonata_engine import Workflow

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.workflow import LifecycleHooks, SoakLifecycle, phase_order


def fake_config(**fields: object) -> SoakConfig:
    """Build a config stand-in; these tests never read its unset fields."""
    return cast(SoakConfig, SimpleNamespace(**fields))


def make_lifecycle(tmp_path, events, failure=None):
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
            steady_s=5400,
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
        driver_factory=Driver,  # pyright: ignore[reportArgumentType]
        hooks=hooks,
        run_dir=tmp_path,
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
