"""The drain observer: what it records, and what it refuses to invent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanolab.tasks.loadtest.soak import (
    DEFAULT_CHECKPOINTS_S,
    ObserveDrainTask,
    count_meters,
    parse_prometheus,
)

EXPOSITION = """\
# HELP nanofaas_execution_store_live Live executions
# TYPE nanofaas_execution_store_live gauge
nanofaas_execution_store_live 4.0
nanofaas_execution_store_outcomes 11.0
jvm_memory_used_bytes{area="heap",id="G1 Eden Space"} 100.0
jvm_memory_used_bytes{area="heap",id="G1 Old Gen"} 250.0
nanofaas_invocations_total{function="a"} 9999.0
"""


def test_labelled_series_are_summed_per_name() -> None:
    """Per-label values are one population; a per-function split grows with names."""
    values = parse_prometheus(EXPOSITION, ("jvm_memory_used_bytes",))

    assert values == {"jvm_memory_used_bytes": 350.0}


def test_series_absent_from_the_payload_are_absent_from_the_result() -> None:
    """A module never loaded has no population; a zero would claim an observation."""
    values = parse_prometheus(EXPOSITION, ("nanofaas_replica_snapshot_entries",))

    assert values == {}


def test_only_the_requested_series_are_returned() -> None:
    """A cumulative counter is not a population and must not be collected as one."""
    values = parse_prometheus(EXPOSITION, ("nanofaas_execution_store_live",))

    assert "nanofaas_invocations_total" not in values
    assert values == {"nanofaas_execution_store_live": 4.0}


def test_meter_count_is_distinct_names_not_lines() -> None:
    """Two labelled series of one metric are one meter name, not two."""
    assert count_meters(EXPOSITION) == 4


def test_checkpoints_cross_the_longest_retention_window() -> None:
    """Past the longest window is the only point where 'still held' means 'leaked'."""
    assert DEFAULT_CHECKPOINTS_S[-1] >= 1800


def test_the_observer_walks_checkpoints_and_writes_every_sample(tmp_path: Path) -> None:
    """Waits are the gaps between checkpoints, not the checkpoints themselves."""
    slept: list[float] = []
    clock = iter([0.0, 0.0, 10.0, 70.0])
    task = ObserveDrainTask(
        task_id="t",
        title="drain",
        management_url="http://127.0.0.1:1",
        output_path=tmp_path / "drain.json",
        checkpoints_s=(0, 10, 70),
        sleep=slept.append,
        now=lambda: next(clock),
    )

    written = task.run()
    payload = json.loads(written.read_text())

    assert slept == [10, 60]
    assert [s["checkpoint_s"] for s in payload["samples"]] == [0, 10, 70]


def test_unreachable_endpoint_is_unavailable_not_empty(tmp_path: Path) -> None:
    """'Nothing retained' and 'could not look' are different answers."""
    task = ObserveDrainTask(
        task_id="t",
        title="drain",
        # Port 1 is not listening, so the scrape fails without a stub.
        management_url="http://127.0.0.1:1",
        output_path=tmp_path / "drain.json",
        checkpoints_s=(0,),
        scrape_timeout_s=0.25,
        sleep=lambda _: None,
        now=lambda: 0.0,
    )

    payload = json.loads(task.run().read_text())
    sample = payload["samples"][0]

    assert sample["status"] == "unavailable"
    assert "populations" not in sample


@pytest.mark.parametrize("line", ["", "# a comment", "malformed_without_value"])
def test_unparseable_lines_are_skipped(line: str) -> None:
    """A payload is scraped text; one bad line must not lose the rest of the sample."""
    assert parse_prometheus(line, ("anything",)) == {}


def test_drain_checkpoints_never_sample_past_the_window() -> None:
    """A shorter drain stops earlier instead of reporting a window it never observed."""
    from nanolab.plans.loadtest import drain_checkpoints

    assert drain_checkpoints(40) == (0, 30, 300, 1800, 2400)
    assert drain_checkpoints(5) == (0, 30, 300)
    assert drain_checkpoints(1) == (0, 30, 60)


def test_only_the_container_backend_claims_an_unproxied_management_url() -> None:
    """A Kubernetes run needs a port-forward this observer does not own."""
    from nanolab.plans.loadtest import management_url_for

    assert (
        management_url_for("container", "http://127.0.0.1:8080")
        == "http://127.0.0.1:8081"
    )
    assert management_url_for("k8s", "http://127.0.0.1:30080") is None


def test_the_drain_step_is_added_exactly_when_the_scenario_asks_for_one(
    tmp_path: Path,
) -> None:
    """Wiring, not intent: the post-run builder is what decides the step exists."""
    from nanolab.plans.loadtest import _build_steps_after

    common: dict[str, object] = {
        "remote": False,
        "executor": None,
        "summary_path": tmp_path / "summary.json",
        "run_dir": tmp_path,
        "fetcher": None,
        "watcher": None,
        "replica_probe": None,
        "bindings": None,
        "request": None,
        "target": None,
        "replica_floor": None,
        "prometheus_client": None,
    }

    without = _build_steps_after(**common)  # type: ignore[arg-type]
    with_drain = _build_steps_after(
        **common,  # type: ignore[arg-type]
        drain_minutes=35,
        management_url="http://127.0.0.1:8081",
    )

    def titles(steps: object) -> list[str]:
        return [getattr(t, "title", type(t).__name__) for t in steps]  # type: ignore

    assert "Observe drain" not in titles(without)
    # First, because it has to sample while the stack is still up and before the
    # reporting steps spend time of their own.
    assert titles(with_drain)[0] == "Observe drain"


def test_a_backend_without_a_readable_endpoint_gets_no_drain_step(
    tmp_path: Path,
) -> None:
    """Asking for a drain is not enough: something has to be reachable to observe."""
    from nanolab.plans.loadtest import _build_steps_after

    steps = _build_steps_after(
        remote=False,
        executor=None,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.json",
        run_dir=tmp_path,
        fetcher=None,
        watcher=None,
        replica_probe=None,
        bindings=None,  # type: ignore[arg-type]
        request=None,  # type: ignore[arg-type]
        target=None,
        replica_floor=None,  # type: ignore[arg-type]
        prometheus_client=None,  # type: ignore[arg-type]
        drain_minutes=35,
        management_url=None,
    )

    assert "Observe drain" not in [getattr(t, "title", type(t).__name__) for t in steps]
