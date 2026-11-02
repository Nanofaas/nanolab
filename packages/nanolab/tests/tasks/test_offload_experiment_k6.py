"""Opt-in real k6 check: NANOLAB_K6_TEST=1, about 45 seconds, loopback only."""

import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from nanolab.assets.diagnostics.idle_reactivation_probe import load_inputs
from nanolab.tasks.offload_experiment_report import _events, _requests
from nanolab.workspace.paths import bundled_assets_root
from tests.conftest import source_contract_root

pytestmark = [
    pytest.mark.nanofaas,
    pytest.mark.skipif(
        os.environ.get("NANOLAB_K6_TEST") != "1" or not shutil.which("k6"),
        reason="opt-in real k6 loopback check",
    ),
]


def test_real_k6_ledger_matches_parser_and_url_header(tmp_path):
    k6 = shutil.which("k6")
    assert k6 is not None
    corpus = source_contract_root() / "functions/test-data/word-stats"
    fixtures = load_inputs(
        corpus / "performance-medium.json", corpus / "runtime-contract.json"
    )
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(fixtures))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fixture = next(f for f in fixtures if f["input"] == data["input"])
            body = json.dumps(
                {"status": "success", "output": fixture["expected"]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if self.path.startswith("/edge/"):
                self.send_header("X-NanoFaaS-Offloaded", cloud)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    cloud = base + "/cloud"
    env = os.environ | {
        "B4_INPUTS": str(inputs),
        "B4_EDGE": base + "/edge",
        "B4_CLOUD": cloud,
        "NANOFAAS_FUNCTION": "word-stats-java",
        "B4_MODE": "pressure",
        "B4_RATE": "4",
        "B4_WARMUP_SECONDS": "1",
        "B4_DURATION_SECONDS": "1",
        "B4_VUS": "16",
        "K6_CONSOLE_OUTPUT": str(tmp_path / "k6-events.jsonl"),
    }
    try:
        result = subprocess.run(
            [
                k6,
                "run",
                "--quiet",
                "--summary-export",
                str(tmp_path / "k6-summary.json"),
                str(bundled_assets_root() / "k6/offload-experiment.js"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=100,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    errors = []
    rows = _requests(_events(tmp_path, errors), fixtures, "pressure", cloud, errors)
    assert errors == []
    assert len([r for r in rows if r["phase"] == "qualification"]) == 8
    assert len([r for r in rows if r["phase"] == "warmup"]) in (20, 21)
    measured = [r for r in rows if r["phase"] == "measurement"]
    assert len(measured) in (4, 5)
    assert all(r["observed_path"] == "offloaded" for r in measured)
    assert all(r["outcome"] == "success" for r in rows)
    summary = json.loads((tmp_path / "k6-summary.json").read_text())
    assert summary["metrics"]["iterations"]["count"] == len(rows) - 8
    for phase in ("measurement", "warmup"):
        assert (
            summary["metrics"][f"dropped_iterations{{scenario:{phase}}}"]["count"] == 0
        )
