import importlib.util
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


def probe_module():
    path = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/diagnostics/idle_reactivation_probe.py"
    )
    assert path.is_file(), "B6 remote probe is missing"
    spec = importlib.util.spec_from_file_location("idle_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status = 302 if body["input"] == "redirect" else 200
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/redirected")
            self.end_headers()
            if body["input"] == "slow":
                time.sleep(0.2)
                return
            self.wfile.write(
                json.dumps({"status": "success", "output": body["input"]}).encode()
            )

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()
    thread.join()
    server.server_close()


def test_probe_measures_full_correct_response_without_redirect_or_retry(endpoint):
    probe = probe_module()
    url, calls = endpoint
    result = probe.invoke(
        url, "word-stats-java", {"input": {"n": 2}, "expected": {"n": 2}}
    )
    assert result["valid"] is True
    assert result["status"] == 200
    import base64

    assert json.loads(base64.b64decode(result["body_base64"]))["output"] == {"n": 2}
    assert result["ended_monotonic_ns"] >= result["started_monotonic_ns"]
    wrong = probe.invoke(
        url, "word-stats-java", {"input": {"n": True}, "expected": {"n": 1}}
    )
    assert wrong["valid"] is False
    redirect = probe.invoke(
        url, "word-stats-java", {"input": "redirect", "expected": "redirect"}
    )
    assert redirect["status"] == 302
    assert redirect["valid"] is False
    assert len(calls) == 3


def test_probe_refuses_stale_oracle_before_sending_requests(tmp_path):
    probe = probe_module()
    corpus = {
        "profile": "medium",
        "cases": [{"name": str(i), "input": i} for i in range(4)],
    }
    contract = {
        "contract": "word-stats-runtime-v1",
        "cases": [
            {"name": f"benchmark {i}", "input": i, "expected": i} for i in range(4)
        ],
    }
    c, o = tmp_path / "corpus.json", tmp_path / "contract.json"
    c.write_text(json.dumps(corpus))
    o.write_text(json.dumps(contract))
    assert len(probe.load_inputs(c, o)) == 4
    contract["cases"][0]["input"] = "wrong"
    o.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match="oracle"):
        probe.load_inputs(c, o)


def test_probe_enforces_a_whole_response_deadline(endpoint, monkeypatch):
    probe = probe_module()
    monkeypatch.setattr(probe, "TIMEOUT_SECONDS", 0.05, raising=False)
    record = probe.invoke(
        endpoint[0], "word-stats-java", {"input": "slow", "expected": "slow"}
    )
    assert record["error"] is not None
    assert record["duration_ms"] < 180


def test_probe_flushes_attempt_before_the_response(endpoint, tmp_path, monkeypatch):
    import sys

    probe = probe_module()
    cases = [{"name": str(i), "input": i} for i in range(4)]
    corpus, contract, output = (
        tmp_path / "c.json",
        tmp_path / "o.json",
        tmp_path / "events.jsonl",
    )
    corpus.write_text(json.dumps({"profile": "medium", "cases": cases}))
    contract.write_text(
        json.dumps(
            {
                "contract": "word-stats-runtime-v1",
                "cases": [
                    {"name": f"benchmark {i}", "input": i, "expected": i}
                    for i in range(4)
                ],
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--url",
            endpoint[0],
            "--function",
            "word-stats-java",
            "--corpus",
            str(corpus),
            "--contract",
            str(contract),
            "--output",
            str(output),
            "--phase",
            "cycle",
        ],
    )
    probe.main()
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [r["event"] for r in records] == ["started", "response"] * 6
    assert records[0]["request_id"] == records[1]["request_id"]


def test_oracle_accepts_json_numbers_across_runtimes_but_not_booleans():
    probe = probe_module()
    assert probe.equal({"averageWordLength": 6}, {"averageWordLength": 6.0})
    assert probe.equal({"averageWordLength": 6.0}, {"averageWordLength": 6})
    assert not probe.equal({"n": True}, {"n": 1})
    assert not probe.equal({"n": 1}, {"n": True})
