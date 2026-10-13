"""Packaged templates keep strict resources and six-minute scope explicit."""

from decimal import Decimal
from importlib.resources import files

import pytest
import yaml

from nanolab.config.scenario import ScenarioConfig
from nanolab.one_shot.capacity import comparison_replica_vectors


@pytest.mark.parametrize("stage", ["calibration", "qualification", "pilot"])
def test_multifunction_templates_are_strict_and_block_unfilled_artifacts(stage):
    path = files("nanolab").joinpath(
        "assets/presets/scenarios/one-shot-multifunction-" + stage + ".yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))
    settings = config.one_shot
    assert settings is not None and settings.strict_memory
    assert len(config.functions) == len(settings.functions) == 5
    assert [node.cpus for node in settings.nodes] == [1, 1, 2, 2, 3, 8]
    assert [node.memory_capacity_mib for node in settings.nodes] == [
        832,
        832,
        1664,
        1664,
        2496,
        5824,
    ]
    assert [
        next(iter(node.function_limits.values())).max_replicas
        for node in settings.nodes
    ] == [13, 13, 26, 26, 39, 91]
    expected_memory = {
        "one-shot-arithmetic-rust": 64,
        "one-shot-sort-rust": 128,
        "one-shot-memory-rust": 256,
        "one-shot-matrix-rust": 128,
        "one-shot-mixed-rust": 256,
    }
    assert {
        name: fn.memory_mib for name, fn in settings.functions.items()
    } == expected_memory
    for node in settings.nodes:
        assert {
            name: limit.memory_mib for name, limit in node.function_limits.items()
        } == expected_memory
    initial = comparison_replica_vectors(
        settings, {name: name for name in settings.functions}
    )
    for node, count in zip(settings.nodes, [1, 1, 2, 2, 3, 7], strict=True):
        assert set(initial[node.id].values()) == {count}
    assert settings.observability.logs.enabled
    assert (
        settings.runtime_distribution is not None
        and settings.runtime_distribution.sha256 == "0" * 64
    )
    if stage == "qualification":
        assert settings.timing is not None
        assert settings.timing.period_candidates == [360]
        assert settings.timing.minimum_samples == 50
        assert settings.timing.quantile == 0.95 and settings.timing.epsilon == 0.05
        assert settings.timing.protocol.preparation_seconds == 1
    elif stage == "pilot":
        assert settings.experiment is not None
        assert settings.experiment.warmup_seconds == 720
        assert all(window.epochs == 1 for window in settings.experiment.windows)


@pytest.mark.parametrize("stage", ["calibration", "qualification", "pilot"])
def test_multifunction_cpu_limits_remain_configured_and_representable(stage):
    path = files("nanolab").joinpath(
        "assets/presets/scenarios/one-shot-multifunction-" + stage + ".yaml"
    )
    settings = ScenarioConfig.model_validate(yaml.safe_load(path.read_text())).one_shot
    assert settings is not None
    for node, expected in zip(
        settings.nodes, [0.10, 0.10, 0.14, 0.14, 0.16, 0.21], strict=True
    ):
        for limit in node.function_limits.values():
            # systemd/runc rounds CPU quota per second to 10ms, i.e. 0.01 CPU.
            cpu = Decimal(str(limit.cpu))
            assert cpu % Decimal("0.01") == 0
            assert limit.cpu == expected


def test_multifunction_transport_reserves_slots_for_discovery_and_auction():
    scenario = yaml.safe_load(
        files("nanolab")
        .joinpath("assets/presets/scenarios/one-shot-multifunction-qualification.yaml")
        .read_text()
    )
    recipe = yaml.safe_load(
        files("nanolab")
        .joinpath("assets/presets/recipes/one-shot-multifunction-jvm.yaml")
        .read_text()
    )
    degree = max(len(peers) for peers in scenario["oneShot"]["neighbors"].values())
    auction = scenario["oneShot"]["timing"]["protocol"]["parallelism"]
    # Advertisements and one-shot share the same bounded transport semaphore.
    capacity = recipe["controlPlane"]["config"]["nanofaas"]["p2p"].get(
        "max-concurrent-requests", 4
    )
    assert capacity >= degree + auction
