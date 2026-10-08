from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict

import pytest
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from tests.functions.test_contracts import frozen_fixture as frozen_fixture
from tests.plans.test_heap_analysis import _CompileOnlyExecutor
from tests.tasks.validation.test_contract_resources import DockerBoundary


def test_plan_has_no_platform_side_effects_or_marker(frozen_fixture, tmp_path):
    from nanolab.plans.contract import build_contract_plan

    source = frozen_fixture
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        "workflow: contract\nbackend: container\n"
        "functions: [word-stats]\ncontract: {}\n"
    )
    run_dir = tmp_path / "attempt"
    workflow = build_contract_plan(
        ScenarioConfig.model_validate(
            {
                "workflow": "contract",
                "backend": "container",
                "functions": ["word-stats"],
                "contract": {},
            }
        ),
        RoleBindings({"host": _CompileOnlyExecutor()}),
        repo_root=source,
        environment=EnvironmentConfig(provider="local"),
        run_dir=run_dir,
        scenario_path=scenario,
    )
    titles = [entry.task.title for entry in workflow.compile().tasks]
    assert any("contract" in title.lower() for title in titles)
    assert not run_dir.exists()


@pytest.fixture
def finished_attempt(tmp_path):
    from nanolab.functions.contracts import ContractCase

    root = tmp_path / "run"
    root.mkdir()
    case = ContractCase("normal", {}, {"answer": 1}, 200, None)
    matrix = {
        "source": {"revision": "frozen"},
        "corpusHashes": {"corpus": "sha"},
        "subset": ["word-stats-python"],
        "cases": {"word-stats": [asdict(case)]},
        "images": [
            {
                "target": {
                    "name": "python-word-stats",
                    "family": "word-stats",
                    "runtime": "python",
                },
                "flavor": "default",
            }
        ],
    }
    execution = "case-one"
    raw = json.dumps(case.expected).encode()
    callback_raw = json.dumps({"success": True, "output": case.expected}).encode()
    identifier = "python-word-stats-default/sdk/000"
    receipt = {
        "id": identifier,
        "case": asdict(case),
        "executionId": execution,
        "imageId": "sha256:image",
        "containerId": "container",
        "status": "passed",
        "error": None,
        "exitCode": None,
        "http": {
            "status": 200,
            "executionId": execution,
            "headers": {},
            "bodyBase64": base64.b64encode(raw).decode(),
        },
        "callbacks": [
            {
                "executionId": execution,
                "method": "POST",
                "path": f"/v1/executions/{execution}:complete",
                "bodyBase64": base64.b64encode(callback_raw).decode(),
            }
        ],
    }
    path = root / "cases" / identifier / "receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt))
    (path.parent / "response.raw").write_bytes(raw)
    (root / "matrix.json").write_text(json.dumps(matrix))
    (root / "case-index.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "audited": True,
                "expectedHttp": 1,
                "expectedOneShot": 0,
                "expectedCallbacks": 1,
                "cases": [
                    {
                        "path": str(path.relative_to(root)),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
    )
    (root / "builder-cleanup.json").write_text(
        json.dumps({"name": "private", "absent": True})
    )
    (root / "ownership").mkdir()
    (root / "cleanup").mkdir()
    (root / "images").mkdir()
    (root / "images" / "000.json").write_text(
        json.dumps(
            {
                "cell": matrix["images"][0],
                "image": {"Id": "sha256:image"},
            }
        )
    )
    for kind, identity in (
        ("image", "sha256:image"),
        ("network", "network"),
        ("container", "container"),
    ):
        owned = {
            "kind": kind,
            "reference": identity,
            "identity": identity,
            "owner": "test",
        }
        (root / "ownership" / f"{kind}.json").write_text(json.dumps(owned))
        (root / "cleanup" / f"{kind}.json").write_text(json.dumps({"absent": True}))
    return root, path, DockerBoundary()


def test_subset_reports_exact_matrix(finished_attempt):
    from nanolab.plans.contract import finalize_contract_run

    root, _, executor = finished_attempt
    marker = finalize_contract_run(root, executor=executor)
    value = json.loads(marker.read_text())
    assert value["counts"] == {"http": 1, "oneShot": 0, "callbacks": 1}
    assert value["subset"] == ["word-stats-python"]
    assert value["cleanupVerified"] is True
    assert value["source"] == {"revision": "frozen"}
    assert value["corpusHashes"] == {"corpus": "sha"}
    assert len(value["receipts"]) == 1
    assert value["exclusions"]


@pytest.mark.parametrize(
    "fault", ["missing", "changed", "omitted", "false-audit", "wrong-body"]
)
def test_missing_or_changed_receipt_prevents_marker(finished_attempt, fault):
    from nanolab.plans.contract import finalize_contract_run

    root, path, executor = finished_attempt
    if fault == "missing":
        path.unlink()
    elif fault == "changed":
        path.write_text("{}")
    else:
        index = json.loads((root / "case-index.json").read_text())
        if fault == "omitted":
            index["cases"] = []
        elif fault == "false-audit":
            index["audited"] = False
        else:
            receipt = json.loads(path.read_text())
            receipt["http"]["bodyBase64"] = base64.b64encode(b"{}").decode()
            path.write_text(json.dumps(receipt))
            index["cases"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (root / "case-index.json").write_text(json.dumps(index))
    with pytest.raises((ValueError, OSError)):
        finalize_contract_run(root, executor=executor)
    assert not (root / "qualification.json").exists()


def test_fresh_attempt_cannot_overwrite_marker(finished_attempt):
    from nanolab.plans.contract import finalize_contract_run

    root, _, executor = finished_attempt
    marker = finalize_contract_run(root, executor=executor)
    before = marker.read_bytes()
    with pytest.raises(FileExistsError):
        finalize_contract_run(root, executor=executor)
    assert marker.read_bytes() == before


def test_release_failure_prevents_qualification(finished_attempt):
    from nanolab.plans.contract import finalize_contract_run

    root, _, executor = finished_attempt
    (root / "builder-cleanup.json").write_text(
        json.dumps({"name": "private", "absent": False})
    )
    with pytest.raises(RuntimeError, match="absence"):
        finalize_contract_run(root, executor=executor)
    assert not (root / "qualification.json").exists()


@pytest.mark.parametrize("fault", ["missing-image", "changed-image", "empty-ownership"])
def test_missing_artifact_identity_prevents_qualification(finished_attempt, fault):
    from nanolab.plans.contract import finalize_contract_run

    root, path, executor = finished_attempt
    if fault == "missing-image":
        (root / "images" / "000.json").unlink()
    elif fault == "changed-image":
        receipt = json.loads(path.read_text())
        receipt["imageId"] = "sha256:replacement"
        path.write_text(json.dumps(receipt))
        index = json.loads((root / "case-index.json").read_text())
        index["cases"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (root / "case-index.json").write_text(json.dumps(index))
    else:
        for owned in (root / "ownership").glob("*.json"):
            owned.unlink()
    with pytest.raises((ValueError, OSError)):
        finalize_contract_run(root, executor=executor)
    assert not (root / "qualification.json").exists()


def test_unknown_selector_rejected_before_acquisition(frozen_fixture, tmp_path):
    from nanolab.plans.contract import build_contract_plan

    config = ScenarioConfig.model_validate(
        {
            "workflow": "contract",
            "backend": "container",
            "functions": ["unknown"],
            "contract": {},
        }
    )
    with pytest.raises(ValueError, match="unknown"):
        build_contract_plan(
            config,
            RoleBindings({"host": _CompileOnlyExecutor()}),
            repo_root=frozen_fixture,
            environment=EnvironmentConfig(provider="local"),
            run_dir=tmp_path / "attempt",
            scenario_path=tmp_path / "scenario.yaml",
        )
    assert not (tmp_path / "attempt").exists()
