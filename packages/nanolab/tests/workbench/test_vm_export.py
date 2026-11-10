"""vm-resources rows rebuilt from the workbench Prometheus (stub HTTP server)."""

import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
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
CONTROL = {
    "container_cpu_usage_seconds_total": 0.25,
    "container_memory_usage_bytes": 300,
    "container_cpu_cfs_throttled_seconds_total": 0.002,  # 2 ms throttled per s
    "container_cpu_cfs_periods_total": 10,
    "container_cpu_cfs_throttled_periods_total": 2,
    "container_spec_cpu_quota": 50000,
    "container_spec_cpu_period": 100000,
    "container_start_time_seconds": 1762646400.5,
}
CONTAINERS = [
    (
        "/docker/fff",
        f"wb-log-collector-{NODE}",
        {},
        {"container_cpu_usage_seconds_total": 0.0, "container_memory_usage_bytes": 0},
    ),
    # (cgroup id, name, extra labels, metric -> value)
    ("/system.slice/docker-aaa.scope", "nanolab-one-shot-control-plane", {}, CONTROL),
    (
        "/system.slice/docker-bbb.scope",
        "fn-work-1",
        FUNCTION_LABELS,
        {"container_cpu_usage_seconds_total": 0.25, "container_memory_usage_bytes": 64},
    ),
    (
        "/system.slice/docker-ccc.scope",
        f"wb-node-exporter-{NODE}",
        {},
        {"container_cpu_usage_seconds_total": 0.01, "container_memory_usage_bytes": 10},
    ),
    (
        "/system.slice/docker-ddd.scope",
        f"wb-cadvisor-{NODE}",
        {},
        {"container_cpu_usage_seconds_total": 0.02, "container_memory_usage_bytes": 20},
    ),
    (
        "/system.slice/docker-eee.scope",
        "someone-else",
        {},
        {"container_cpu_usage_seconds_total": 0.0, "container_memory_usage_bytes": 1},
    ),
]


@dataclass
class Stub:
    """What the stub Prometheus saw; tests change it before exporting."""

    scraped: list[int] = field(default_factory=lambda: list(STEPS))
    # step point -> step point whose scrape it re-reads (a scrape read twice)
    reread: dict[int, int] = field(default_factory=dict)
    # container name -> step points where cAdvisor no longer reported it
    dropped: dict[str, set[int]] = field(default_factory=dict)


def _series(labels: dict, value: Callable[[int], float], steps: list[int]) -> dict:
    return {
        "metric": {"node": NODE, **labels},
        "values": [
            [(ANCHOR + timedelta(seconds=s)).timestamp(), repr(value(s))] for s in steps
        ],
    }


def canned(query: str, stub: Stub) -> list[dict]:
    """Answer as a Prometheus that scraped NODE as `stub` describes."""
    if f'node="{NODE}"' not in query:
        return []
    if query.startswith("timestamp("):
        lag = (ANCHOR - timedelta(seconds=SCRAPE_LAG)).timestamp()
        return [_series({}, lambda s: lag + stub.reread.get(s, s), stub.scraped)]
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
    return [
        _series(
            {"id": cgroup, "name": name, **labels},
            lambda _s, v=value: v,
            [s for s in STEPS if s not in stub.dropped.get(name, set())],
        )
        for cgroup, name, labels, metrics in CONTAINERS
        for metric, value in metrics.items()
        if metric + "{" in query
    ]


@pytest.fixture
def prometheus() -> Iterator[Callable[[Stub], str]]:
    current = Stub()

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
                        "result": canned(params["query"][0], current),
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

    def start(stub: Stub) -> str:
        nonlocal current
        current = stub
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


def export(url: str, run_dir: Path, **extra: NodeRoles) -> list[dict]:
    export_vm_resources(
        url,
        run_dir,
        nodes={NODE: ROLES, **extra},
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
    rows = export(prometheus(Stub()), tmp_path)
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
    assert row["expectedContainerIds"] == ["aaa", "bbb", "ccc", "ddd", "eee", "fff"]
    assert [c["containerId"] for c in row["containerInventory"]] == [
        "aaa",
        "bbb",
        "ccc",
        "ddd",
        "eee",
        "fff",
    ]
    control, function = row["containers"][:2]
    # The sampler's units: cores, fractions, per-sample deltas over one period.
    assert control == {
        "containerId": "aaa",
        "containerName": "nanolab-one-shot-control-plane",
        "role": "control-plane",
        "function": None,
        "replica": None,
        "incarnation": "1762646400.5",
        "cpuUsedCores": 0.25,
        "cpuVmFraction": 0.0625,
        "cpuQuotaCores": 0.5,
        "cpuQuotaFraction": 0.5,
        "memoryCurrentBytes": 300,
        "periodsDelta": 50,
        "throttledPeriodsDelta": 10,
        "throttledUsecDelta": 10000,
    }
    assert function["cpuQuotaCores"] is None and function["cpuQuotaFraction"] is None
    assert function["incarnation"] is None and function["throttledUsecDelta"] is None
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
    row = export(prometheus(Stub()), tmp_path)[0]
    assert row["vcpuCount"] == 4
    assert row["cpuUtilization"] == 0.25
    assert row["cpuUsedCores"] == 1.0
    assert row["cpuIowaitFraction"] == 0.0
    assert row["cpuStealFraction"] == 0.0


@pytest.mark.usefixtures("collection")
def test_container_roles_mapped(prometheus, tmp_path):
    row = export(prometheus(Stub()), tmp_path)[0]
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
        (f"wb-log-collector-{NODE}", "collector", None, None),
    ]


@pytest.mark.usefixtures("collection")
def test_scrape_gap_is_missing_slots(prometheus, tmp_path):
    rows = export(prometheus(Stub(scraped=[s for s in STEPS if s != 0])), tmp_path)
    assert len(rows) == len(STEPS) - 1
    node = summarize(tmp_path)["nodes"][NODE]
    assert node["missingSeconds"] == [-5]
    assert not node["complete"]


@pytest.mark.usefixtures("collection")
def test_summary_complete_at_five_second_policy(prometheus, tmp_path):
    export(prometheus(Stub()), tmp_path)
    result = summarize(tmp_path)
    node = result["nodes"][NODE]
    assert result["complete"], node
    assert node["expectedSamples"] == 4
    assert node["observedSeconds"] == 4
    assert node["observedRoles"] == ["collector", "control-plane", "function", "other"]


@pytest.mark.usefixtures("collection")
def test_dropped_mapped_container_is_a_missing_sample(prometheus, tmp_path):
    stub = Stub(dropped={"nanolab-one-shot-control-plane": {5}, "fn-work-1": {5}})
    rows = export(prometheus(stub), tmp_path)
    # Only containers the role map names stay expected once they disappear.
    assert rows[3]["expectedContainerIds"] == ["aaa", "ccc", "ddd", "eee", "fff"]
    assert "aaa" not in [c["containerId"] for c in rows[3]["containers"]]
    node = summarize(tmp_path)["nodes"][NODE]
    assert node["missingContainerSamples"] == [{"second": 0, "containerIds": ["aaa"]}]


@pytest.mark.usefixtures("collection")
def test_scrape_read_twice_is_one_row(prometheus, tmp_path):
    rows = export(prometheus(Stub(reread={0: -5})), tmp_path)
    stamps = [row["timestampUtc"] for row in rows]
    assert len(rows) == len(STEPS) - 1 and len(set(stamps)) == len(stamps)


@pytest.mark.usefixtures("collection")
def test_node_absent_from_prometheus_is_an_empty_file(prometheus, tmp_path):
    export(prometheus(Stub()), tmp_path, ghost=NodeRoles(roles={}))
    assert (tmp_path / "vm-resources/ghost.jsonl").read_bytes() == b""
    result = summarize_resources(
        tmp_path,
        anchor=ANCHOR,
        warmup=10,
        period=10,
        epochs=1,
        nodes=["ghost"],
        mode="ewma",
        functions=["work"],
        run_id="r",
    )
    assert result["nodes"]["ghost"]["missingSeconds"] == [-10, -5, 0, 5]


@pytest.mark.usefixtures("collection")
def test_v2_schedule_run_correlates_on_scrape_utc_and_charts_continuously(
    prometheus, tmp_path
):
    from nanolab.one_shot.report import resource_figures
    from tests.one_shot.test_report import manifest

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "parameters": {
                    "logicalSchedule": {
                        "protocolVersion": 2,
                        "anchorMonotonicSeconds": 1000.0,
                    }
                }
            }
        )
    )
    rows = export(prometheus(Stub()), tmp_path)
    result = summarize(tmp_path)
    assert result["complete"], result["nodes"][NODE]
    charts = resource_figures(
        tmp_path,
        manifest().model_copy(update={"anchor": ANCHOR.isoformat(), "nodes": [NODE]}),
    )
    cpu = re.search(r'"x":\[(.*?)\]', charts[0])
    assert cpu is not None
    # Every scrape is a point, and 5 s apart is no gap: no null breaks the line.
    assert json.loads("[" + cpu.group(1) + "]") == [r["timestampUtc"] for r in rows]


@pytest.mark.usefixtures("collection")
@pytest.mark.parametrize("missing", ["all", "control", "collector"])
def test_absent_mandatory_containers_make_coverage_incomplete(
    prometheus, tmp_path, missing
):
    names = {name for _id, name, _labels, _metrics in CONTAINERS}
    if missing == "control":
        names = {"nanolab-one-shot-control-plane"}
    elif missing == "collector":
        names = {f"wb-log-collector-{NODE}"}
    rows = export(
        prometheus(Stub(dropped={name: set(STEPS) for name in names})), tmp_path
    )
    coverage = summarize(tmp_path)
    assert not coverage["complete"]
    errors = coverage["nodes"][NODE]["errors"]
    assert errors
    assert any(e.get("containerName") in names for e in errors)
    assert all(row["memoryTotalBytes"] == 1000 for row in rows)
    if missing == "all":
        assert all(row["expectedContainerIds"] == [] for row in rows)


@pytest.mark.usefixtures("collection")
def test_recreated_role_and_late_function_do_not_expect_old_ids(
    prometheus, tmp_path, monkeypatch
):
    original = canned

    def recreated(query, stub):
        result = original(query, stub)
        for series in list(result):
            if series["metric"].get("name") == "nanolab-one-shot-control-plane":
                future = [v for v in series["values"] if v[0] >= ANCHOR.timestamp()]
                series["values"] = [
                    v for v in series["values"] if v[0] < ANCHOR.timestamp()
                ]
                result.append(
                    {
                        "metric": {**series["metric"], "id": "/docker/restarted"},
                        "values": future,
                    }
                )
        return result

    monkeypatch.setattr(f"{__name__}.canned", recreated)
    rows = export(prometheus(Stub(dropped={"fn-work-1": {-10, -5}})), tmp_path)
    assert summarize(tmp_path)["complete"]
    assert "aaa" not in rows[-1]["expectedContainerIds"]
    assert "restarted" in rows[-1]["expectedContainerIds"]
    assert "bbb" not in rows[0]["expectedContainerIds"]
