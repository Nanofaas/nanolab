"""vm-resources rows rebuilt from the workbench Prometheus (stub HTTP server)."""

import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from nanolab.one_shot.vm_resources import read_samples, summarize_resources
from nanolab.workbench.vm_export import NodeRoles, export_vm_resources

ANCHOR = datetime(2026, 11, 9, tzinfo=UTC)
STEPS = [-10, -5, 0, 5, 10]  # step points relative to the anchor
SCRAPE_LAG = 4  # each step point reads the scrape 4 s before it
NODE = "edge1"
ROLES = NodeRoles(
    roles={
        "nanolab-one-shot-control-plane": "control-plane",
        f"wb-log-collector-{NODE}": "collector",
    },
    observers=(
        f"wb-node-exporter-{NODE}",
        f"wb-cadvisor-{NODE}",
        f"wb-log-collector-{NODE}",
    ),
)
FUNCTION_LABELS = {
    "container_label_io_nanofaas_managed": "true",
    "container_label_io_nanofaas_function": "ns/work",
    "container_label_io_nanofaas_replica": "1",
}
CONTAINERS = [
    # (cgroup id, name, extra labels, cpu cores, memory bytes)
    ("/system.slice/docker-aaa.scope", "nanolab-one-shot-control-plane", {}, 0.5, 300),
    ("/system.slice/docker-bbb.scope", "fn-work-1", FUNCTION_LABELS, 0.25, 64),
    ("/system.slice/docker-ccc.scope", f"wb-node-exporter-{NODE}", {}, 0.01, 10),
    ("/system.slice/docker-ddd.scope", f"wb-cadvisor-{NODE}", {}, 0.02, 20),
    ("/system.slice/docker-eee.scope", "someone-else", {}, 0.0, 1),
]


def _series(labels: dict, value: Callable[[int], float], steps: list[int]) -> dict:
    return {
        "metric": {"node": NODE, **labels},
        "values": [
            [(ANCHOR + timedelta(seconds=s)).timestamp(), repr(value(s))] for s in steps
        ],
    }


def canned(query: str, *, scraped: list[int]) -> list[dict]:
    """Answer as a Prometheus that scraped NODE at the `scraped` step points."""
    if f'node="{NODE}"' not in query:
        return []
    if query.startswith("timestamp("):
        scrape = (ANCHOR - timedelta(seconds=SCRAPE_LAG)).timestamp()
        return [_series({}, lambda s: scrape + s, scraped)]
    if "node_memory" in query:
        memory = {"MemTotal": 1000, "MemAvailable": 400, "SwapTotal": 100}
        memory["SwapFree"] = 70
        return [
            _series({"__name__": f"node_memory_{k}_bytes"}, lambda _s, v=v: v, STEPS)
            for k, v in memory.items()
        ]
    if "node_cpu_seconds_total" in query:
        return [
            _series({"cpu": str(cpu), "mode": mode}, lambda _s, r=rate: r, STEPS)
            for cpu in range(4)
            for mode, rate in [("idle", 0.75), ("user", 0.25)]
        ]
    column = 3 if "container_cpu" in query else 4
    return [
        _series(
            {"id": item[0], "name": item[1], **item[2]},
            lambda _s, v=item[column]: v,
            STEPS,
        )
        for item in CONTAINERS
    ]


@pytest.fixture
def prometheus() -> Iterator[Callable[[list[int]], str]]:
    scraped = list(STEPS)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            assert url.path == "/api/v1/query_range"
            params = parse_qs(url.query)
            assert params["step"] == ["5s"]
            body = json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": canned(params["query"][0], scraped=scraped),
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def start(steps: list[int]) -> str:
        scraped[:] = steps
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    server.shutdown()


@pytest.fixture
def collection(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    roles = workspace / ".ansible/collections/ansible_collections/nanofaas/workbench"
    for role in ["node_exporter", "cadvisor"]:
        defaults = roles / "roles" / role / "defaults/main.yml"
        defaults.parent.mkdir(parents=True)
        defaults.write_text(f"---\n{role}_image: img/{role}@sha256:1\n")
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(workspace))


def export(url: str, run_dir: Path) -> list[dict]:
    export_vm_resources(
        url,
        run_dir,
        nodes={NODE: ROLES},
        start=ANCHOR - timedelta(seconds=10),
        end=ANCHOR + timedelta(seconds=10),
        step_seconds=5,
    )
    rows, malformed = read_samples(run_dir / "vm-resources" / f"{NODE}.jsonl")
    assert malformed == 0
    return rows


def summarize(run_dir: Path) -> dict:
    return summarize_resources(
        run_dir,
        anchor=ANCHOR,
        warmup=10,
        period=10,
        epochs=1,
        nodes=[NODE],
        mode="ewma",
        functions=["work"],
        run_id="r",
    )


@pytest.mark.usefixtures("collection")
def test_rows_have_the_summary_fields(prometheus, tmp_path):
    rows = export(prometheus(STEPS), tmp_path)
    assert len(rows) == len(STEPS)
    row = rows[0]
    assert row["node"] == NODE
    assert row["timestampUtc"] == (ANCHOR - timedelta(seconds=14)).isoformat()
    assert "monotonicSeconds" not in row and "bootId" not in row
    assert row["errors"] == []
    assert {k: row[k] for k in row if k.startswith(("memory", "swap"))} == {
        "memoryTotalBytes": 1000,
        "memoryAvailableBytes": 400,
        "memoryUsedBytes": 600,
        "swapTotalBytes": 100,
        "swapFreeBytes": 70,
        "swapUsedBytes": 30,
    }
    assert row["expectedContainerIds"] == ["aaa", "bbb", "ccc", "ddd", "eee"]
    assert [c["containerId"] for c in row["containerInventory"]] == [
        "aaa",
        "bbb",
        "ccc",
        "ddd",
        "eee",
    ]
    control = row["containers"][0]
    assert control["cpuUsedCores"] == 0.5
    assert control["cpuVmFraction"] == 0.125
    assert control["memoryCurrentBytes"] == 300
    assert row["sampler"]["cpuUsedCores"] == pytest.approx(0.03)
    assert row["sampler"]["memoryRssPeakBytes"] == 30
    policy = json.loads((tmp_path / "vm-resources/policy.json").read_text())
    assert policy["periodSeconds"] == 5
    assert policy["transport"] == "prometheus-node-exporter-cadvisor"
    assert policy["phaseCorrelation"] == "prometheus-scrape-utc"
    assert "samplerSha256" not in policy
    assert policy["exporterImages"] == {
        "node-exporter": "img/node_exporter@sha256:1",
        "cadvisor": "img/cadvisor@sha256:1",
    }


@pytest.mark.usefixtures("collection")
def test_cpu_fractions_from_modes(prometheus, tmp_path):
    row = export(prometheus(STEPS), tmp_path)[0]
    assert row["vcpuCount"] == 4
    assert row["cpuUtilization"] == 0.25
    assert row["cpuUsedCores"] == 1.0
    assert row["cpuIowaitFraction"] == 0.0
    assert row["cpuStealFraction"] == 0.0


@pytest.mark.usefixtures("collection")
def test_container_roles_mapped(prometheus, tmp_path):
    row = export(prometheus(STEPS), tmp_path)[0]
    identities = [
        (c["containerName"], c["role"], c["function"], c["replica"])
        for c in row["containers"]
    ]
    assert identities == [
        ("nanolab-one-shot-control-plane", "control-plane", None, None),
        ("fn-work-1", "function", "work", 1),
        (f"wb-node-exporter-{NODE}", "other", None, None),
        (f"wb-cadvisor-{NODE}", "other", None, None),
        ("someone-else", "other", None, None),
    ]


@pytest.mark.usefixtures("collection")
def test_scrape_gap_is_missing_slots(prometheus, tmp_path):
    rows = export(prometheus([s for s in STEPS if s != 0]), tmp_path)
    assert len(rows) == len(STEPS) - 1
    node = summarize(tmp_path)["nodes"][NODE]
    assert node["missingSeconds"] == [-5]
    assert not node["complete"]


@pytest.mark.usefixtures("collection")
def test_summary_complete_at_five_second_policy(prometheus, tmp_path):
    export(prometheus(STEPS), tmp_path)
    result = summarize(tmp_path)
    node = result["nodes"][NODE]
    assert result["complete"], node
    assert node["expectedSamples"] == 4
    assert node["observedSeconds"] == 4
    assert node["observedRoles"] == ["control-plane", "function", "other"]
