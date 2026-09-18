"""Synthetic evidence, quota and owned-process cancellation regressions."""

import contextlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


def module():
    return importlib.import_module("nanolab.tasks.soak.workload")


def driver(command, **kwargs):
    arguments = {
        "base_url": "http://127.0.0.1:8080",
        "function_rates": {"words": 3},
        "payloads": {"words": [{"input": {"text": "one"}, "expected": {"total": 1}}]},
        "image_digests": {
            "control-plane": "sha256:" + "a" * 64,
            "words": "sha256:" + "b" * 64,
        },
        "config": {"retry": 3},
        "vus": 4,
        "command": command,
        "graceful_stop_s": 0.5,
        "request_timeout_s": 0.2,
    }
    arguments.update(kwargs)
    return module().K6WorkloadDriver(**arguments)


def fake(tmp_path, source):
    path = tmp_path / "fake_k6.py"
    path.write_text(source)
    return (sys.executable, str(path))


def summary_metrics(functions=1, offered_counts=None):
    metrics = {}
    offered_counts = [3] * functions if offered_counts is None else offered_counts
    for name in ("offered", "success", "error", "retry", "replay"):
        total = 0
        for index in range(functions):
            offered = offered_counts[index]
            value = {
                "offered": offered,
                "success": max(0, offered - 1),
                "error": min(1, offered),
                "retry": 0,
                "replay": 0,
            }[name]
            total += value
            metrics[f"soak_{name}{{scenario:fn_{index}}}"] = {
                "values": {"count": value}
            }
        metrics["soak_" + name] = {"values": {"count": total}}
    metrics["dropped_iterations"] = {"values": {"count": 0}}
    for index in range(functions):
        metrics[f"dropped_iterations{{scenario:fn_{index}}}"] = {"values": {"count": 0}}
    return metrics


def summary_command(tmp_path, *, metrics=None, code=0):
    document = json.dumps(
        {"metrics": summary_metrics() if metrics is None else metrics}
    )
    return fake(
        tmp_path,
        """import os, sys
marker=os.environ['NANOLAB_SOAK_SUMMARY_MARKER']
sys.stdout.write('\\n'+marker+':START\\n'+"""
        + repr(document)
        + """+'\\n'+marker+':END\\n')
sys.stdout.flush()
sys.exit("""
        + str(code)
        + ")\n",
    )


def test_constant_arrival_shape():
    assert module().constant_arrival_options(100, 5400, 200) == {
        "executor": "constant-arrival-rate",
        "rate": 100,
        "timeUnit": "1s",
        "duration": "5400s",
        "preAllocatedVUs": 200,
        "maxVUs": 200,
    }


@pytest.mark.parametrize(
    ("rate", "integer", "time_unit"),
    [(0.35, 7, "20s"), (1.2, 6, "5s"), (0.001, 1, "1000s")],
)
def test_fractional_rate_and_separate_vu_policy(rate, integer, time_unit):
    result = module().constant_arrival_options(rate, 5400, 20, max_vus=80)
    assert result["rate"] == integer
    assert result["timeUnit"] == time_unit
    assert result["preAllocatedVUs"] == 20 and result["maxVUs"] == 80


@pytest.mark.parametrize(
    ("rate", "duration", "vus"),
    [
        (0, 1, 1),
        (1, 0, 1),
        (1, float("nan"), 1),
        (1, 1, 0),
        (True, 1, 1),
        (float("inf"), 1, 1),
        (1 / 3, 1, 1),
    ],
)
def test_invalid_options(rate, duration, vus):
    with pytest.raises(ValueError, match=r"positive|timeUnit"):
        module().constant_arrival_options(rate, duration, vus)


def test_max_vus_cannot_undercut_preallocation():
    with pytest.raises(ValueError, match="max_vus must be at least vus"):
        module().constant_arrival_options(1, 10, 20, max_vus=19)


@pytest.mark.parametrize("code", [0, 99])
def test_receipt_freezes_inputs_and_preserves_threshold_failure(tmp_path, code):
    rates = {"words": 0.35}
    d = driver(summary_command(tmp_path, code=code), function_rates=rates, max_vus=12)
    rates["words"] = 999
    receipt = json.loads(d.run(tmp_path / "run", 10, threading.Event()).read_text())
    assert receipt["schema"] == "nanolab-soak-v1" and receipt["kind"] == "workload"
    assert "schema_version" not in receipt
    assert receipt["exit_code"] == code
    assert receipt["threshold_failed"] is (code == 99)
    assert receipt["completed"] is True
    assert receipt["cleanup_complete"] is True
    assert receipt["counters"]["offered"]["value"] == 3
    assert receipt["counters"]["admitted"]["value"] is None
    assert receipt["counters"]["admitted"]["availability"] == "unavailable"
    assert receipt["per_function"]["words"]["success"]["value"] == 2
    assert receipt["provenance"]["function_rates"] == {"words": 0.35}
    cfg = json.loads((tmp_path / "run/workload-config.json").read_text())
    assert cfg["functions"][0]["options"]["maxVUs"] == 12
    assert cfg["functions"][0]["options"]["timeUnit"] == "20s"
    assert len(receipt["provenance"]["script_sha256"]) == 64
    assert receipt["generator_end_s"] >= receipt["started_s"]
    d.stop(0.1)


@pytest.mark.parametrize(
    "mutation",
    ["outcomes", "aggregate", "missing", "fractional", "negative", "malformed"],
)
def test_inconsistent_counters_prevent_completion(tmp_path, mutation):
    metrics = summary_metrics()
    if mutation == "outcomes":
        metrics["soak_offered"]["values"]["count"] = 4
        metrics["soak_offered{scenario:fn_0}"]["values"]["count"] = 4
    elif mutation == "aggregate":
        metrics["soak_retry"]["values"]["count"] = 1
    elif mutation == "missing":
        del metrics["soak_success{scenario:fn_0}"]
    elif mutation == "fractional":
        metrics["soak_success"]["values"]["count"] = 1.5
    elif mutation == "negative":
        metrics["soak_retry"]["values"]["count"] = -1
    else:
        metrics["soak_error"] = []
    receipt = json.loads(
        driver(summary_command(tmp_path, metrics=metrics))
        .run(tmp_path / "run", 0.1, threading.Event())
        .read_text()
    )
    assert not receipt["completed"]
    assert receipt["errors"]


def test_two_function_conservation(tmp_path):
    d = driver(
        summary_command(tmp_path, metrics=summary_metrics(2, [3, 18])),
        function_rates={"words": 3, "other": 0.5},
        payloads={
            "words": [{"input": 1, "expected": 1}],
            "other": [{"input": 2, "expected": 2}],
        },
    )
    receipt = json.loads(d.run(tmp_path / "run", 6, threading.Event()).read_text())
    assert receipt["completed"]
    assert receipt["counters"]["offered"]["value"] == 21


def test_missing_summary_is_not_zero_or_success(tmp_path):
    d = driver(fake(tmp_path, "pass\n"))
    result = json.loads(d.run(tmp_path / "run", 0.1, threading.Event()).read_text())
    assert not result["completed"]
    assert result["counters"]["offered"]["value"] is None
    assert result["errors"]


def test_crash_persists_receipt_and_raises(tmp_path):
    d = driver(fake(tmp_path, "raise SystemExit(7)\n"))
    with pytest.raises(RuntimeError, match="k6 driver crashed with exit code 7"):
        d.run(tmp_path / "run", 0.1, threading.Event())
    assert (
        json.loads((tmp_path / "run/workload-receipt.json").read_text())["exit_code"]
        == 7
    )


def test_pre_cancelled_does_not_spawn(tmp_path):
    event = threading.Event()
    event.set()
    result = json.loads(
        driver(("/no/such/executable",)).run(tmp_path / "run", 1, event).read_text()
    )
    assert result["cancelled"] and not result["completed"]


@pytest.mark.parametrize(
    ("stubborn", "detached"), [(False, False), (True, False), (True, True)]
)
def test_cancellation_reaps_owned_child_and_grandchild(tmp_path, stubborn, detached):
    pid_file = tmp_path / "pids.json"
    command = fake(
        tmp_path,
        """import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN if """
        + repr(stubborn)
        + """ else lambda *args: sys.exit(0))
child = subprocess.Popen([
    sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(60)'
], start_new_session="""
        + repr(detached)
        + """)
Path("""
        + repr(str(pid_file))
        + """).write_text(json.dumps([os.getpid(),child.pid]))
while True: time.sleep(.01)
""",
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    event = threading.Event()
    d = driver(command)
    result, errors = [], []

    def run():
        try:
            result.append(d.run(tmp_path / "run", 30, event))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    pids = []
    try:
        thread.start()
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        pids = json.loads(pid_file.read_text())
        before = time.monotonic()
        event.set()
        thread.join(3)
        assert not thread.is_alive()
        assert time.monotonic() - before < 2
        assert not errors
        receipt = json.loads(result[0].read_text())
        assert receipt["cancelled"] and not receipt["completed"]
        assert receipt["cleanup_complete"]
        for pid in pids:
            with pytest.raises(ProcessLookupError, match="Errno 3"):
                os.kill(pid, 0)
        assert unrelated.poll() is None
        d.stop(0.1)
        d.stop(0.1)
    finally:
        event.set()
        d.stop(0.5)
        thread.join(3)
        # Failure-only hygiene for synthetic fixtures; these recorded PIDs were
        # created by this test, never discovered through global process names.
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)
        unrelated.terminate()
        unrelated.wait(timeout=2)


def test_normal_parent_exit_cleans_detached_child_and_invalidates_completion(tmp_path):
    pid_file = tmp_path / "detached.pid"
    command = fake(
        tmp_path,
        """import subprocess, sys
from pathlib import Path
child=subprocess.Popen(
    [sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True
)
Path("""
        + repr(str(pid_file))
        + """).write_text(str(child.pid))
""",
    )
    receipt = json.loads(
        driver(command).run(tmp_path / "run", 0.1, threading.Event()).read_text()
    )
    pid = int(pid_file.read_text())
    try:
        assert receipt["cleanup_complete"] and receipt["forced_stop"]
        assert not receipt["completed"]
        with pytest.raises(ProcessLookupError, match="Errno 3"):
            os.kill(pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)


@pytest.mark.parametrize("stream", ["log", "summary"])
def test_output_quota_enforced_while_generator_runs_and_receipt_reserved(
    tmp_path, stream
):
    command = fake(
        tmp_path,
        """import os, sys, time
if """
        + repr(stream)
        + """ == 'summary':
    sys.stdout.write('\\n'+os.environ['NANOLAB_SOAK_SUMMARY_MARKER']+':START\\n')
    sys.stdout.flush()
while True:
    os.write(1,b'x'*8192)
    time.sleep(.001)
""",
    )
    limit = 65536
    before = time.monotonic()
    receipt = json.loads(
        driver(command, artifact_limit_bytes=limit)
        .run(tmp_path / "run", 30, threading.Event())
        .read_text()
    )
    assert time.monotonic() - before < 3
    assert receipt["quota_exceeded"] and not receipt["completed"]
    assert receipt["cleanup_complete"]
    assert receipt["artifact_limit_bytes"] == limit
    assert receipt["errors"]
    assert (
        sum(
            path.stat().st_size
            for path in (tmp_path / "run").iterdir()
            if path.is_file()
        )
        <= limit
    )


def test_quota_counts_existing_artifacts_and_rejects_before_launch(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    (output / "existing.bin").write_bytes(b"x" * 65536)
    with pytest.raises(ValueError, match=r"artifact budget cannot fit frozen"):
        driver(("/no/such/executable",), artifact_limit_bytes=65536).run(
            output, 0.1, threading.Event()
        )
    assert (output / "existing.bin").stat().st_size == 65536


def test_payload_requires_expected(tmp_path):
    with pytest.raises(ValueError, match=r"each payload requires input and"):
        module().K6WorkloadDriver(
            base_url="http://localhost",
            function_rates={"x": 1},
            payloads={"x": [{"input": 1}]},
            image_digests={"x": "sha256:" + "a" * 64},
            config={},
            vus=1,
        )


def test_actual_script_validates_body_and_streams_summary_without_http(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    script = Path(__file__).parents[2] / "assets/k6/soak-workload.js"
    source = "\n".join(
        line
        for line in script.read_text().splitlines()
        if not line.startswith("import ")
    )
    harness = """const __ENV={
    NANOLAB_SOAK_CONFIG:'unused',NANOLAB_SOAK_SUMMARY_MARKER:'TEST'
};
const __ITER=0;
const execution={scenario:{name:'fn_0'}};
const cfg={base_url:'http://unused',request_timeout_s:1,functions:[{name:'words',scenario:'fn_0',options:{},payloads:[{input:{text:'one'},expected:{total:1}}]}]};
const open=()=>JSON.stringify(cfg);
let reply; const http={post:()=>reply};
let seen={}; class Counter {
    constructor(n){this.n=n;}
    add(v){seen[this.n]=(seen[this.n]||0)+v;}
}
const check=(r,checks)=>Object.values(checks).every(f=>f(r));
"""
    tail = """
for (const [status,body,ok] of [
    [200,{status:'success',output:{total:1}},true],
    [200,{status:'success',output:{total:9}},false],
    [200,{output:{total:1}},false],[429,{},false],[200,'bad json',false]
]) {
seen={};reply={status,body:typeof body==='string'?body:JSON.stringify(body)};invoke();
if(seen.soak_success!==(ok?1:0)||seen.soak_error!==(ok?0:1)||seen.soak_offered!==1)
    throw Error(JSON.stringify(seen));
}
const summary=handleSummary({metrics:{}});
if(Object.keys(summary).join(',')!=='stdout'||
   !summary.stdout.includes('TEST:START')||!summary.stdout.includes('TEST:END'))
    throw Error('summary bypasses bounded pipe');
"""
    source = source.replace("export default ", "").replace("export ", "")
    result = subprocess.run(
        [node, "--input-type=module", "-e", harness + source + tail],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_shared_owned_command_preserves_cwd_env_and_exit(tmp_path):
    from nanolab.tasks.soak.processes import run_owned_command

    log = tmp_path / "command.log"
    result = run_owned_command(
        [
            sys.executable,
            "-c",
            'import os,sys; print(os.getcwd()); print(os.environ["SOAK_TEST_VALUE"]); '
            "sys.exit(7)",
        ],
        cwd=tmp_path,
        env={**os.environ, "SOAK_TEST_VALUE": "bound-value"},
        log_path=log,
        timeout_s=2,
        cancelled=threading.Event(),
        output_limit_bytes=1024,
    )
    assert result.returncode == 7 and result.reaped and not result.forced_stop
    assert str(tmp_path) in log.read_text() and "bound-value" in log.read_text()


@pytest.mark.parametrize("reason", ["timeout", "quota", "cancelled"])
def test_shared_owned_command_bounds_execution_and_output(tmp_path, reason):
    from nanolab.tasks.soak.processes import run_owned_command

    event = threading.Event()
    timer = threading.Timer(0.1, event.set) if reason == "cancelled" else None
    if timer:
        timer.start()
    try:
        source = (
            'import os,time\nwhile True:\n os.write(1,b"x"*2048);time.sleep(.01)'
            if reason == "quota"
            else "import time;time.sleep(60)"
        )
        result = run_owned_command(
            [sys.executable, "-c", source],
            cwd=tmp_path,
            env=os.environ,
            log_path=tmp_path / "command.log",
            timeout_s=0.15 if reason == "timeout" else 3,
            cancelled=event,
            output_limit_bytes=1024,
        )
        assert result.reaped
        assert (tmp_path / "command.log").stat().st_size <= 1024
        assert getattr(
            result,
            {
                "timeout": "timed_out",
                "quota": "quota_exceeded",
                "cancelled": "cancelled",
            }[reason],
        )
    finally:
        if timer:
            timer.cancel()
            timer.join()


def test_shared_runner_caller_exception_stops_and_reaps(tmp_path):
    from nanolab.tasks.soak.processes import OwnedCommandRunner

    pid_file = tmp_path / "caller-error.pid"

    class FailingWait:
        def is_set(self):
            return False

        def wait(self, timeout):
            if pid_file.exists():
                raise RuntimeError("caller wait failed")
            time.sleep(timeout)

    runner = OwnedCommandRunner(
        [
            sys.executable,
            "-c",
            "import os,time;from pathlib import Path;Path("
            + repr(str(pid_file))
            + ").write_text(str(os.getpid()));time.sleep(60)",
        ],
        cwd=tmp_path,
        env=os.environ,
        log_path=tmp_path / "error.log",
        timeout_s=30,
        cancelled=FailingWait(),
        output_limit_bytes=1024,
        stop_timeout_s=0.5,
    )
    try:
        with pytest.raises(RuntimeError, match="caller wait failed"):
            runner.run()
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError, match="Errno 3"):
            os.kill(pid, 0)
    finally:
        runner.stop(0.5)


def test_global_vu_allocations_conserve_both_budgets_deterministically(tmp_path):
    kwargs = {
        "function_rates": {"z": 5, "b": 2, "a": 1},
        "payloads": {name: [{"input": 1, "expected": 1}] for name in ("z", "b", "a")},
        "vus": 7,
        "max_vus": 11,
    }
    d = driver(
        summary_command(tmp_path, metrics=summary_metrics(3, [1, 2, 5])), **kwargs
    )
    receipt = json.loads(d.run(tmp_path / "run", 1, threading.Event()).read_text())
    cfg = json.loads((tmp_path / "run/workload-config.json").read_text())
    allocations = {
        fn["name"]: (fn["options"]["preAllocatedVUs"], fn["options"]["maxVUs"])
        for fn in cfg["functions"]
    }
    assert allocations == {"a": (2, 3), "b": (2, 3), "z": (3, 5)}
    assert sum(pre for pre, _ in allocations.values()) == 7
    assert sum(maximum for _, maximum in allocations.values()) == 11
    assert all(maximum >= pre for pre, maximum in allocations.values())
    assert receipt["vu_policy"]["scope"] == "global"
    assert receipt["completed"]


def test_global_vus_below_function_count_are_rejected_before_launch(tmp_path):
    with pytest.raises(ValueError, match=r"function|prealloc"):
        driver(
            ("/no/such/executable",),
            function_rates={"a": 1, "b": 1},
            vus=1,
            max_vus=4,
            payloads={name: [{"input": 1, "expected": 1}] for name in ("a", "b")},
        )


@pytest.mark.parametrize(
    ("observed", "expected"),
    [(1, False), (998, False), (999, True), (1000, True), (1001, True), (1002, False)],
)
def test_rate_conservation_allows_only_one_boundary_iteration(
    tmp_path, observed, expected
):
    d = driver(
        summary_command(tmp_path, metrics=summary_metrics(offered_counts=[observed])),
        function_rates={"words": 100},
    )
    receipt = json.loads(d.run(tmp_path / "run", 10, threading.Event()).read_text())
    assert receipt["completed"] is expected
    demand = receipt["scheduled_demand"]["words"]
    assert demand["minimum_iterations"] == 999
    assert demand["maximum_iterations"] == 1001
    assert demand["observed_iterations"] == observed
    if not expected:
        assert any("scheduled demand" in error for error in receipt["errors"])


def test_dropped_work_participates_in_scheduled_demand_conservation(tmp_path):
    metrics = summary_metrics(offered_counts=[7])
    for name in ("dropped_iterations", "dropped_iterations{scenario:fn_0}"):
        metrics[name]["values"]["count"] = 3
    d = driver(
        summary_command(tmp_path, metrics=metrics, code=99),
        function_rates={"words": 10},
    )
    receipt = json.loads(d.run(tmp_path / "run", 1, threading.Event()).read_text())
    assert receipt["scheduled_demand"]["words"]["observed_iterations"] == 10
    assert receipt["completed"] and receipt["threshold_failed"]


def test_per_function_underload_cannot_be_hidden_by_other_function_overload(tmp_path):
    d = driver(
        summary_command(tmp_path, metrics=summary_metrics(2, [1, 199])),
        function_rates={"a": 10, "b": 10},
        payloads={name: [{"input": 1, "expected": 1}] for name in ("a", "b")},
    )
    receipt = json.loads(d.run(tmp_path / "run", 10, threading.Event()).read_text())
    assert not receipt["completed"]
    assert sum("scheduled demand" in error for error in receipt["errors"]) == 2


def test_duration_precision_is_preserved_in_effective_schedule():
    assert (
        module().constant_arrival_options(100000, 1.23456789, 1)["duration"]
        == "1.23456789s"
    )


def test_an_adopted_child_may_stop_on_its_own_after_the_leader(tmp_path):
    """A single-use helper that stops with the leader is not a stray.

    This supervisor is a subreaper, so a leader's orphaned child is adopted and
    was killed in the very iteration the leader exited. Gradle's `--no-daemon`
    build forks exactly such a helper and announces that it stops at the end of
    the build, so killing it reported a successful build as a forced stop.
    """
    from nanolab.tasks.soak.processes import run_owned_command

    leader = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.15)'])\n"
        "time.sleep(0.05)\n"
    )
    result = run_owned_command(
        [sys.executable, "-c", leader],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "leader.log",
        timeout_s=10,
        cancelled=threading.Event(),
        output_limit_bytes=1024,
    )

    assert result.returncode == 0
    assert result.reaped and not result.forced_stop


def test_an_adopted_child_that_outlives_the_grace_is_still_forced(tmp_path):
    """The grace is bounded: a child that will not leave is still killed."""
    from nanolab.tasks.soak.processes import run_owned_command

    leader = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(0.05)\n"
    )
    result = run_owned_command(
        [sys.executable, "-c", leader],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "leader.log",
        timeout_s=10,
        cancelled=threading.Event(),
        output_limit_bytes=1024,
    )

    assert result.reaped and result.forced_stop
