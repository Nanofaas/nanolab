"""Measured capacities determine every phase and preserve cloud-direct identity."""

from datetime import UTC, datetime
from importlib import import_module

import pytest

from nanolab.config.one_shot import OneShotConfig
from nanolab.tasks.one_shot.trace import oracle_entries, request_schedule


def settings(quantum=0.5):
    return OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "scientific-experiment",
            "flowQuantum": quantum,
            "nodes": [{"id": f"edge-{n}", "kind": "edge"} for n in range(1, 6)]
            + [{"id": "cloud", "kind": "cloud"}],
            "functions": {"work": {"input": {"seed": 7}}},
        }
    )


def make(capacities=None, quantum=0.5):
    return import_module("nanolab.tasks.one_shot.trace").capacity_trace(
        capacities or {**{f"edge-{n}": 10.0 for n in range(1, 6)}, "cloud": 100.0},
        settings(quantum),
        10,
        7,
    )


def test_all_54_epochs_and_six_origins_keep_requested_and_effective_rates():
    trace = make()
    assert len(trace.windows) == 54
    assert len(trace.phase_ids) == 54
    assert len(set(trace.phase_ids)) == 9
    assert trace.node_roles == {
        **{f"edge-{n}": "edge" for n in range(1, 6)},
        "cloud": "cloud-direct",
    }
    assert all(r["work"] > 0 for window in trace.windows for r in window.values())
    assert trace.windows[0] == {
        **{f"edge-{n}": {"work": 3.5} for n in range(1, 6)},
        "cloud": {"work": 5.0},
    }
    for i in range(6, 36, 6):
        rates = trace.windows[i]
        assert sorted(rates[f"edge-{n}"]["work"] for n in range(1, 6)) == [3.5] * 4 + [
            18.0
        ]
    assert trace.windows[36]["edge-1"]["work"] == 18.0
    assert trace.windows[36]["edge-2"]["work"] == 18.0
    assert all(trace.windows[42][f"edge-{n}"]["work"] == 11.0 for n in range(1, 6))
    assert trace.windows[-1] == trace.windows[0]
    assert trace.requested_windows == trace.windows


def test_quantization_rounds_hotspots_up_and_background_down():
    trace = make({**{f"edge-{n}": 10.1 for n in range(1, 6)}, "cloud": 100.0})
    assert trace.windows[0]["edge-1"]["work"] == 3.5
    assert trace.requested_windows[0]["edge-1"]["work"] == pytest.approx(3.535)
    assert trace.windows[42]["edge-1"]["work"] == 11.5
    assert trace.requested_windows[42]["edge-1"]["work"] == pytest.approx(11.11)
    assert (
        trace.sha256
        == make({**{f"edge-{n}": 10.1 for n in range(1, 6)}, "cloud": 100.0}).sha256
    )


@pytest.mark.parametrize("bad", ["positive", "scope", "cloud"])
def test_invalid_or_unsafe_capacity_is_rejected(bad):
    caps = {**{f"edge-{n}": 10.0 for n in range(1, 6)}, "cloud": 100.0}
    if bad == "positive":
        caps["edge-1"] = 0.1
    elif bad == "scope":
        caps.pop("edge-5")
    else:
        caps["cloud"] = 50.0
    with pytest.raises(ValueError, match=r"positive|scope|headroom"):
        make(caps)


def test_cloud_direct_has_schedule_but_never_forecasts():
    trace = make()
    anchor = datetime(2026, 10, 10, tzinfo=UTC)
    entries = oracle_entries(
        trace, node="cloud", generations={"work": 1}, anchor=anchor
    )
    assert entries == []
    schedule = request_schedule(
        trace,
        anchor=anchor,
        endpoints={n: "http://" + n for n in trace.nodes},
        run_id="test",
    )
    cloud = [r for r in schedule if r["origin"] == "cloud"]
    assert len(cloud) == 54 * 10 * 5
    assert all(
        r["originRole"] == "cloud-direct"
        and r["phaseId"] == trace.phase_ids[r["epoch"]]
        for r in cloud
    )
    edge = [r for r in schedule if r["origin"] == "edge-1"]
    entries = oracle_entries(
        trace, node="edge-1", generations={"work": 1}, anchor=anchor
    )
    assert len(edge) == sum(r["rate"] * 10 for r in entries)


def test_explicit_phase_epochs_expand_without_changing_legacy_hash():
    module = import_module("nanolab.tasks.one_shot.trace")
    original = settings().model_copy(
        update={
            "experiment": import_module(
                "nanolab.config.one_shot"
            ).ExperimentSettings.model_validate(
                {
                    "windows": [
                        {
                            "id": "control",
                            "epochs": 6,
                            "rates": {n.id: {"work": 1.0} for n in settings().nodes},
                        }
                    ]
                }
            )
        }
    )
    trace = module.experiment_trace(original, period=10, aliases={"work": "work"})
    assert len(trace.windows) == 6
    assert trace.phase_ids == ["control"] * 6
    assert (
        oracle_entries(
            trace, node="cloud", generations={"work": 1}, anchor=datetime.now(UTC)
        )
        == []
    )


def test_scientific_warmup_is_capacity_based_and_two_periods():
    module = import_module("nanolab.tasks.one_shot.trace")
    trace = make()
    warm = module.warmup_trace(
        trace,
        seconds=20,
        capacities={**{f"edge-{n}": 10.0 for n in range(1, 6)}, "cloud": 100.0},
    )
    assert warm.windows == [trace.windows[0]]
    assert warm.phase_ids == ["warmup"]
    with pytest.raises(ValueError, match="two periods"):
        module.warmup_trace(
            trace, seconds=5, capacities=dict.fromkeys(trace.nodes, 10.0)
        )


def test_cloud_headroom_uses_smaller_empirical_or_local_profile_capacity():
    module = import_module("nanolab.tasks.one_shot.trace")
    with pytest.raises(ValueError, match="headroom"):
        module.validate_cloud_headroom(
            make(), cloud="cloud", empirical=100, physical=50
        )


def test_cloud_direct_completions_do_not_change_edge_utility():
    module = import_module("nanolab.tasks.one_shot.verification")
    cells = [
        {
            "origin": "edge-1",
            "function": "work",
            "planned": 10,
            "errors": 0,
            "destinations": {"edge-1": 6, "edge-2": 2, "cloud": 2},
        }
    ]
    functions = settings().functions
    before = module.realized_utility(cells, functions=functions, cloud="cloud")
    cells.append(
        {
            "origin": "cloud",
            "function": "work",
            "planned": 1000,
            "errors": 0,
            "destinations": {"cloud": 1000},
        }
    )
    assert module.realized_utility(cells, functions=functions, cloud="cloud") == before
