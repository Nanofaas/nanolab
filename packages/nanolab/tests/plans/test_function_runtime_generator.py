"""Run the actual B5 generator with a network-free k6 stub."""

import json
import subprocess
from pathlib import Path

import pytest

from nanolab.comparison.mixed_http import write_http_accounting

SCRIPT = Path(__file__).parents[2] / "src/nanolab/assets/k6/function-runtime.js"
HARNESS = r"""
const fixture=JSON.parse(process.argv[1]);
const __ENV={NANOFAAS_FUNCTION:'word-stats-rust'}, __ITER=0;
const execution={scenario:{iterationInTest:fixture.iteration||0}};
const cases=Array.from({length:4},(_,i)=>
    ({name:`medium-${i}`,input:{text:`text${i}`}}));
const open=path=>JSON.stringify(path===__ENV.NANOFAAS_PAYLOAD
    ? {profile:'medium',cases}
    : {contract:'word-stats-runtime-v1',cases:cases.map(c=>
       ({name:`benchmark ${c.name}`,input:c.input,expected:{wordCount:1}}))});
__ENV.NANOFAAS_PAYLOAD='payload'; __ENV.NANOFAAS_CONTRACT='contract';
class SharedArray {constructor(name,fn){return fn();}}
const totals={},events=[],requests=[],checks=[];
class Counter {
 constructor(name){this.name=name;totals[name]=0;}
 add(n){totals[this.name]+=n;}
}
const durations=[];
class Trend {add(n){durations.push(n);}}
const console={log:line=>events.push(JSON.parse(line))};
const check=(r,rules)=>Object.values(rules).forEach(rule=>checks.push(rule(r)));
const http={post:(url,body,options)=>{
 requests.push({url,body:JSON.parse(body),options});
 return {status:fixture.status,error_code:fixture.status===0?1050:0,
   body:fixture.body,timings:{duration:7}};
}};
"""


def run_generator(status=200, body=None, action="invoke()", iteration=0):
    if body is None:
        body = json.dumps({"status": "success", "output": {"wordCount": 1}})
    source = "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.startswith("import ")
    ).replace("export ", "")
    command = (
        HARNESS
        + source
        + "\nlet error=null;try{"
        + action
        + ";}catch(e){error=e.message;}"
        + "process.stdout.write(JSON.stringify({counters:totals,events,requests,"
        + "checks,durations,options,error}));"
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            command,
            json.dumps({"status": status, "body": body, "iteration": iteration}),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (200, None, "valid_completed"),
        (200, "{}", "invalid_output"),
        (200, "null", "invalid_output"),
        (200, "invalid json", "invalid_output"),
        (200, '{"status":"success","output":{"wordCount":2}}', "invalid_output"),
        (429, None, "refused"),
        (503, None, "refused"),
        (500, None, "http_error"),
        (0, None, "http_error"),
    ],
)
def test_exact_output_and_response_classification(status, body, outcome):
    result = run_generator(status, body)
    assert result["counters"]["b5_offered"] == 1
    assert result["counters"][f"b5_{outcome}"] == 1
    assert sum(result["counters"].values()) == 2
    assert result["durations"] == ([7] if outcome == "valid_completed" else [])
    assert result["checks"] == [outcome in ("valid_completed", "refused")]
    assert result["events"][0]["outcome"] == outcome
    assert result["events"][0]["phase"] == "measurement"
    assert result["events"][0]["case_name"] == "benchmark medium-0"
    assert result["events"][0]["iteration"] == 0
    assert len(result["requests"]) == 1  # No client retries or second function.


def test_qualification_is_separate_and_fails_before_load():
    result = run_generator(action="setup()")
    assert len(result["requests"]) == 4
    assert not result["error"] and not any(result["counters"].values())
    assert {e["phase"] for e in result["events"]} == {"qualification"}
    assert [e["case_name"] for e in result["events"]] == [
        f"benchmark medium-{i}" for i in range(4)
    ]
    assert all(e["iteration"] is None for e in result["events"])
    assert run_generator(body="{}", action="setup()")["error"].startswith(
        "B5 qualification failed"
    )


def test_same_single_function_schedule_and_deterministic_payloads():
    result = run_generator(iteration=7)
    (scenario,) = result["options"]["scenarios"].values()
    assert sum(int(s["duration"][:-1]) for s in scenario["stages"]) == 450
    assert [s["target"] for s in scenario["stages"]] == [
        40,
        200,
        200,
        600,
        600,
        200,
        200,
        350,
        350,
        900,
        900,
        40,
    ]
    assert scenario["gracefulStop"] == "40s" and scenario["preAllocatedVUs"] == 5000
    assert result["requests"][0]["body"] == {"input": {"text": "text3"}}
    assert result["events"][0]["case_name"] == "benchmark medium-3"
    assert result["events"][0]["iteration"] == 7


def test_body_counters_reconcile_with_measurement_ledger(tmp_path):
    qualification = run_generator(action="setup()")
    measurement = run_generator(body="{}")
    events = qualification["events"] + measurement["events"]
    (tmp_path / "k6-events.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
    metrics = {"http_reqs": {"count": 5}, "http_req_failed": {"passes": 0}}
    metrics.update(
        {name: {"count": count} for name, count in measurement["counters"].items()}
    )
    (tmp_path / "k6-summary.json").write_text(json.dumps({"metrics": metrics}))
    result = write_http_accounting(tmp_path)
    assert result["measurement"]["invalid_output"] == 1
    assert result["measurement"]["offered"] == 1
    metrics["b5_invalid_output"]["count"] = 0
    (tmp_path / "k6-summary.json").write_text(json.dumps({"metrics": metrics}))
    with pytest.raises(RuntimeError, match="does not reconcile"):
        write_http_accounting(tmp_path)
