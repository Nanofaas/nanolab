import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
import typer
from typer.testing import CliRunner


@pytest.mark.parametrize(
    ("status", "code"),
    [("PASS", 0), ("FAIL", 1), ("INCONCLUSIVE", 2), ("ABORTED", 130)],
)
def test_exit_codes_preserve_verdict(status, code):
    from nanolab.cli.soak import soak_exit_code

    assert soak_exit_code(status) == code


@pytest.mark.parametrize(
    "selection",
    [{"resume": True}, {"only": "steady"}, {"start": "drain"}, {"until": "steady"}],
)
def test_soak_cannot_resume_or_select_partial_lifetime(selection):
    from nanolab.cli.soak import validate_soak_selection

    options = {"resume": False, "only": None, "start": None, "until": None}
    options.update(selection)
    with pytest.raises(ValueError, match=r"soak requires its complete"):
        validate_soak_selection(**options)


def test_whole_run_selection_is_allowed():
    from nanolab.cli.soak import validate_soak_selection

    validate_soak_selection(resume=False, only=None, start=None, until=None)


def test_missing_or_invalid_terminal_never_passes(tmp_path):
    from nanolab.cli.soak import terminal_status

    assert terminal_status(tmp_path) == "INCONCLUSIVE"
    (tmp_path / "terminal.json").write_text('{"status":"PASS"}')
    assert terminal_status(tmp_path) == "INCONCLUSIVE"
    (tmp_path / "terminal.json").write_text(
        '{"schema":"nanolab-soak-v1","status":"PASS"}'
    )
    assert terminal_status(tmp_path) == "PASS"


def test_policy_overlay_is_explicit_and_only_replaces_criteria(tmp_path):
    from nanolab.cli.soak import load_soak_policy

    policy = tmp_path / "policy.yaml"
    policy.write_text("schema: nanolab-soak-policy-v1\ncriteria:\n  - id: chosen\n")
    data = {
        "workflow": "soak",
        "soakPolicyFile": "policy.yaml",
        "soak": {"criteria": [], "purpose": "p24"},
    }
    resolved, receipt = load_soak_policy(data, tmp_path / "scenario.yaml")
    assert receipt is not None
    soak = cast("dict[str, Any]", resolved["soak"])
    assert soak["criteria"] == [{"id": "chosen"}]
    assert soak["purpose"] == "p24"
    assert "soakPolicyFile" not in resolved
    assert data["soak"]["criteria"] == []
    assert receipt["sha256"] and receipt["resolved_criteria_fingerprint"]


def test_policy_cannot_override_images_or_timing(tmp_path):
    from nanolab.cli.soak import resolve_soak_policy

    (tmp_path / "policy.yaml").write_text(
        "schema: nanolab-soak-policy-v1\ncriteria: []\nimages: {}\n"
    )
    with pytest.raises(ValueError, match=r"soak policy must contain schema"):
        resolve_soak_policy(
            {"workflow": "soak", "soakPolicyFile": "policy.yaml", "soak": {}},
            tmp_path / "scenario.yaml",
        )


def test_missing_policy_is_not_filled_with_permissive_defaults(tmp_path):
    from nanolab.cli.soak import resolve_soak_policy

    with pytest.raises(ValueError, match="required soak policy file unavailable"):
        resolve_soak_policy(
            {"workflow": "soak", "soakPolicyFile": "missing.yaml", "soak": {}},
            tmp_path / "scenario.yaml",
        )


def test_soak_routes_before_comparison_and_plan_has_no_side_effects(
    tmp_path, monkeypatch
):
    from nanolab.cli import product

    calls = []
    module = ModuleType("nanolab.plans.soak")
    module.build_soak_plan = lambda *args, **kwargs: (
        calls.append((args, kwargs)) or "soak-plan"
    )
    monkeypatch.setitem(sys.modules, "nanolab.plans.soak", module)
    monkeypatch.setattr(
        product, "build_role_bindings", lambda environment: ("bindings", "fetcher")
    )
    monkeypatch.setattr(
        product,
        "default_tool_paths",
        lambda: SimpleNamespace(
            nanofaas_root=tmp_path, tool_root=tmp_path, runs_dir=tmp_path / "runs"
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("soak must not fall through to a comparison/loadtest")

    monkeypatch.setattr(product, "is_runtime_comparison", forbidden)
    scenario, environment = (
        SimpleNamespace(workflow="soak"),
        SimpleNamespace(provider="local"),
    )
    assert product._workflow(scenario, environment, dry_run=True) == "soak-plan"
    assert calls[0][0] == (scenario, environment, "bindings")
    assert not (tmp_path / "runs").exists()


def test_soak_default_run_directories_are_unique(tmp_path):
    from nanolab.cli.product import _default_run_dir

    assert _default_run_dir(None, "soak", tmp_path) != _default_run_dir(
        None, "soak", tmp_path
    )


def test_tui_forwards_soak_dry_run(monkeypatch):
    from nanolab.tui import app

    calls = []
    monkeypatch.setattr(
        app, "_workflow", lambda *args, **kwargs: calls.append(kwargs) or "soak"
    )
    assert (
        app.NanofaasTUI._build_workflow(
            SimpleNamespace(workflow="soak"), object(), dry_run=True
        )
        == "soak"
    )
    assert calls == [{"dry_run": True}]


def test_offline_command_is_registered_and_preserves_inconclusive(
    tmp_path, monkeypatch
):
    from nanolab.cli import product, soak
    from nanolab.tasks.soak.models import CriterionResult

    monkeypatch.setattr(
        soak,
        "evaluate_run",
        lambda *args: (
            CriterionResult("coverage", "INCONCLUSIVE", "missing receipts", ()),
        ),
    )
    report = tmp_path / "report.json"
    report.write_text("{}")
    monkeypatch.setattr(soak, "write_report", lambda *args: report)
    app = typer.Typer()
    product.install_product_commands(app)
    result = CliRunner().invoke(app, ["soak-evaluate", str(tmp_path)])
    assert result.exit_code == 2
    assert "INCONCLUSIVE" in result.output
    assert str(report) in result.output


def test_soak_success_metadata_uses_terminal_status(tmp_path, monkeypatch):
    from nanolab.cli import product

    (tmp_path / "terminal.json").write_text(
        '{"schema":"nanolab-soak-v1","status":"INCONCLUSIVE"}'
    )
    calls = []
    monkeypatch.setattr(
        product, "_write_run_metadata", lambda *args, **kwargs: calls.append(kwargs)
    )
    product._write_success_metadata(
        tmp_path,
        started_at=None,
        scenario_path=Path("scenario.yaml"),
        scenario=SimpleNamespace(workflow="soak"),
        environment_path=None,
        environment=None,
        sink=None,
        provenance={},
    )
    assert calls[0]["status"] == "inconclusive"
