"""Independent oracles for five distinct, bounded Rust workloads."""

import json
from pathlib import Path

import pytest

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/multi-workloads.json").read_text()
)


@pytest.mark.parametrize(
    "row", VECTORS, ids=lambda r: r["function"] + "-" + str(r["input"]["seed"])
)
def test_expected_outputs_from_independent_reference(row):
    from nanolab.tasks.one_shot.workload import expected_output

    assert expected_output(row["function"], row["input"]) == row["expected"]


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("one-shot-arithmetic", {"iterations": 2**64 - 1, "seed": 0}),
        ("one-shot-sort", {"items": 2**64 - 1, "passes": 1, "seed": 0}),
        ("one-shot-memory", {"working_set_bytes": 2**64 - 1, "passes": 1, "seed": 0}),
        ("one-shot-matrix", {"dimension": 2**64 - 1, "repetitions": 1, "seed": 0}),
        ("one-shot-arithmetic", {"iterations": 0, "seed": True}),
        ("one-shot-arithmetic", {"iterations": 0, "seed": 0, "extra": 1}),
    ],
)
def test_invalid_inputs_fail_before_work(name, payload):
    from nanolab.tasks.one_shot.workload import expected_output

    with pytest.raises(ValueError, match="workload"):
        expected_output(name, payload)


@pytest.mark.parametrize("row", VECTORS)
def test_calibration_checks_each_named_function(row):
    from nanolab.tasks.one_shot.calibration import workload_checksum

    assert workload_checksum(row["input"], function=row["function"]) == row["expected"]


def test_six_nodes_register_five_explicit_handler_identities(tmp_path, monkeypatch):
    from sonata_engine import Workflow

    from nanolab.config import EnvironmentConfig
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.plans import one_shot_common
    from nanolab.plans.one_shot_common import add_one_shot_platforms

    requests = []
    original = one_shot_common.add_platform

    def capture(workflow, request, **kwargs):
        requests.append(request)
        return original(workflow, request, **kwargs)

    monkeypatch.setattr(one_shot_common, "add_platform", capture)
    names = [row["function"] for row in VECTORS[:5]]
    for name in names:
        (tmp_path / "functions/rust" / name).mkdir(parents=True)
    functions = {
        row["function"] + "-rust": {
            "input": row["input"],
            "memoryMiB": 64,
            "cpu": 0.05,
            "maxReplicas": 5,
        }
        for row in VECTORS[:5]
    }
    config = ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-calibration",
            "backend": "container",
            "functions": list(functions),
            "oneShot": {
                "provider": "multipass",
                "purpose": "workflow-validation",
                "strictMemory": True,
                "nodes": [
                    {"id": f"edge-{i}", "kind": "edge", "memoryCapacityMiB": 320}
                    for i in range(5)
                ]
                + [{"id": "cloud", "kind": "cloud", "memoryCapacityMiB": 320}],
                "functions": functions,
            },
        }
    )
    topology = add_one_shot_platforms(
        Workflow("five-workloads"),
        config,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
        ),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
    )
    assert len(topology.platforms) == 6
    assert len(set(topology.function_aliases.values())) == 5
    assert len(requests) == 6
    for platform in requests:
        assert {fn.name for fn in platform.functions} == set(names)
        for fn in platform.functions:
            assert fn.env["FUNCTION_HANDLER"] == fn.name
            assert fn.env["NANOFAAS_MAX_CONCURRENT_HANDLERS"] == "1"
            assert fn.max_retries == 0


def test_packaged_recipe_exports_each_workload_and_enables_strict_memory():
    from importlib.resources import files

    import yaml

    path = files("nanolab").joinpath(
        "assets/presets/recipes/one-shot-multifunction-jvm.yaml"
    )
    recipe = yaml.safe_load(path.read_text())
    assert (
        recipe["controlPlane"]["config"]["nanofaas"]["container-local"]["strict-memory"]
        is True
    )
    functions = recipe["functions"]
    assert {fn["name"] for fn in functions} == {row["function"] for row in VECTORS[:5]}
    assert {fn["sdk"] for fn in functions} == {"rust"}
    assert len({fn["container"]["image"] for fn in functions}) == 5
