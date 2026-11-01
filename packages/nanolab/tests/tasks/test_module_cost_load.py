import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from nanolab.assets.diagnostics.idle_reactivation_probe import load_inputs
from nanolab.comparison.experiment_01 import read_events
from nanolab.workspace.paths import bundled_assets_root
from tests.conftest import source_contract_root

pytestmark = [
    pytest.mark.nanofaas,
    pytest.mark.skipif(
        os.environ.get("NANOLAB_K6_TEST") != "1", reason="opt-in k6 loopback fixture"
    ),
]


@pytest.mark.parametrize("invalid", [False, True])
def test_k6_exact_oracle_request_ledger_and_error_verdict(tmp_path, invalid):
    root = source_contract_root() / "functions/test-data/word-stats"
    fixtures = load_inputs(
        root / "performance-medium.json", root / "runtime-contract.json"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fixture = next(f for f in fixtures if f["input"] == data["input"])
            body = json.dumps(
                {"status": "success", "output": {} if invalid else fixture["expected"]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ | {
        "MC_SECONDS": "1",
        "MC_PHASE": "measurement",
        "NANOFAAS_PAYLOAD": str(root / "performance-medium.json"),
        "NANOFAAS_CONTRACT": str(root / "runtime-contract.json"),
        "NANOFAAS_URL": f"http://127.0.0.1:{server.server_port}",
        "K6_CONSOLE_OUTPUT": str(tmp_path / "events.jsonl"),
    }
    executable = shutil.which("k6")
    assert executable is not None
    try:
        run = subprocess.run(
            [
                executable,
                "run",
                "--quiet",
                "--summary-export",
                str(tmp_path / "summary.json"),
                str(bundled_assets_root() / "k6/module-cost.js"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=80,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    events, malformed = read_events(tmp_path / "events.jsonl")
    assert malformed == 0
    sends = [e for e in events if e["t"] == "send"]
    terminals = [e for e in events if e["t"] == "terminal"]
    assert {e["id"] for e in sends} == {e["id"] for e in terminals}
    assert all(e["ts"] >= e["started_at"] and e["duration_ms"] >= 0 for e in terminals)
    if invalid:
        assert run.returncode != 0
        assert len(terminals) == 1 and terminals[0]["valid"] is False
    else:
        assert run.returncode == 0, run.stderr
        assert len(terminals) in (44, 45)
        assert all(e["valid"] for e in terminals)
        summary = json.loads((tmp_path / "summary.json").read_text())
        assert summary["metrics"]["iterations"]["count"] == len(terminals) - 4

        from nanolab.tasks.module_cost_report import read_load

        result = read_load(tmp_path, root, 1, "measurement")
        assert result["valid"] == len(terminals) - 4
        # Missing final responses must not appear as successful low resource use.
        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        (tmp_path / "events.jsonl").write_text("\n".join(lines[:-1]))
        with pytest.raises(ValueError, match="missing"):
            read_load(tmp_path, root, 1, "measurement")
