import json

from nanolab.tasks.soak.models import CriterionResult


def test_reports_are_immutable_and_smoke_is_not_p24(tmp_path):
    from nanolab.tasks.soak.report import write_report

    (tmp_path / "evaluation-input.json").write_text(
        json.dumps(
            {
                "schema": "nanolab-soak-v1",
                "purpose": "smoke",
                "targets": [],
                "criteria": [],
            }
        )
    )
    result = (
        CriterionResult("rss", "PASS", "numerical check only", ("samples.jsonl",)),
    )
    first = write_report(tmp_path, result, False)
    original = first.read_bytes()
    second = write_report(tmp_path, result, False)
    assert first != second
    assert first.read_bytes() == original
    payload = json.loads(second.read_text())
    assert payload["p24_qualified"] is False
    assert payload["scope"] == "numerical-only"
    assert (second.parent / "report.md").is_file()


def test_report_keeps_failure_and_abort(tmp_path):
    from nanolab.tasks.soak.report import write_report

    results = (
        CriterionResult("expired", "FAIL", "payload retained", ()),
        CriterionResult("sdk", "INCONCLUSIVE", "scrape unavailable", ()),
    )
    path = write_report(tmp_path, results, True)
    data = json.loads(path.read_text())
    assert data["status"] == "ABORTED"
    assert {row["status"] for row in data["results"]} == {"FAIL", "INCONCLUSIVE"}


def test_missing_manifest_does_not_invent_p24_qualification(tmp_path):
    from nanolab.tasks.soak.report import write_report

    data = json.loads(write_report(tmp_path, (), False).read_text())
    assert data["purpose"] == "unknown"
    assert data["status"] == "INCONCLUSIVE"
    assert data["p24_qualified"] is False
