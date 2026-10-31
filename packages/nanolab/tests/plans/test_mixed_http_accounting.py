"""Exercise the actual mixed generator with HTTP and metrics stubs."""

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "src/nanolab/assets/k6/mixed-workload.js"
HARNESS = r"""
const fixture=JSON.parse(process.argv[1]);
const __ENV={},__VU=7,__ITER=3;
const checks=[],events=[],requests=[];
class Metric {constructor(name){this.name=name;} add(){}}
const Counter=Metric,Rate=Metric,Trend=Metric;
const console={log:line=>events.push(JSON.parse(line))};
const check=(response, rules)=>{
  for(const [name,rule] of Object.entries(rules))
    checks.push({name,pass:rule(response)});
};
let position=0;
function response(){const status=fixture.statuses[position++];return {status,
  error_code:status===0?1050:undefined,headers:{'X-Execution-Id':'same'},timings:{duration:2}};}
const http={post:(url,body,options)=>{
  requests.push({url,body,options});return response();
},get:()=>response()};
"""


def run_generator(action, statuses):
    source = "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.startswith("import ")
    ).replace("export ", "")
    command = (
        HARNESS
        + source
        + "\n"
        + action
        + "('word-stats-java');"
        + "process.stdout.write(JSON.stringify({checks,events,requests}));"
    )
    result = subprocess.run(
        ["node", "-e", command, json.dumps({"statuses": statuses})],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "statuses", [(500, 200), (200, 409), (0, 200), (429, 503), (200, 200)]
)
def test_both_idempotent_responses_are_checked_and_recorded(statuses):
    result = run_generator("idempotent", statuses)
    status_checks = [
        c for c in result["checks"] if c["name"] == "idempotent request answered"
    ]
    assert [c["pass"] for c in status_checks] == [
        s in (200, 429, 503) for s in statuses
    ]
    assert [e["status"] for e in result["events"]] == list(statuses)
    assert [e["part"] for e in result["events"]] == ["first", "second"]
    assert all(
        e["fn"] == "word-stats-java" and e["path"] == "sync" and e["idem"] == "yes"
        for e in result["events"]
    )
    if 0 in statuses:
        assert (
            next(e for e in result["events"] if e["status"] == 0)["error_code"] == 1050
        )
    first, second = result["requests"]
    assert first == second
    assert first["options"]["headers"]["Idempotency-Key"] == "k6-7-3"
    agreement = [
        c
        for c in result["checks"]
        if c["name"] == "idempotent pair shares one execution"
    ]
    assert len(agreement) == int(statuses == (200, 200))


@pytest.mark.parametrize(
    ("action", "status", "path"),
    [
        ("synchronous", 200, "sync"),
        ("asynchronous", 202, "async"),
        ("probe", 200, "probe"),
    ],
)
def test_every_http_response_has_a_ledger_entry(action, status, path):
    result = run_generator(action, [status])
    assert len(result["events"]) == 1
    assert result["events"][0]["path"] == path
    assert result["events"][0]["status"] == status


def test_status_accounting_reconciles_and_preserves_unexpected_codes(tmp_path):
    from nanolab.comparison.mixed_http import write_http_accounting

    events = run_generator("idempotent", (409, 200))["events"]
    (tmp_path / "k6-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    summary = {"metrics": {"http_reqs": {"count": 2}, "http_req_failed": {"passes": 1}}}
    (tmp_path / "k6-summary.json").write_text(json.dumps(summary))
    result = write_http_accounting(tmp_path)
    assert result["unexpected_http"] == 1
    assert result["rows"][0]["status"] == 409
    assert result["rows"][0]["part"] == "first"
    summary["metrics"]["http_reqs"]["count"] = 3
    (tmp_path / "k6-summary.json").write_text(json.dumps(summary))
    with pytest.raises(RuntimeError, match="does not reconcile"):
        write_http_accounting(tmp_path)
    assert (tmp_path / "http-status-counts.json").exists()
