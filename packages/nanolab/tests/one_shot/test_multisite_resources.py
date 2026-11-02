"""Remote Multipass sites and Azure placement, without contacting any host."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanolab.config import EnvironmentConfig
from nanolab.config.environment import MultipassSite
from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot import multisite, site_cpu, sites
from nanolab.one_shot.infrastructure import build_one_shot_resources
from nanolab.tasks.vm.models import VmInfo

SITE = MultipassSite(
    ssh="spark", address="149.132.179.35", architecture="arm64", multipass="/opt/mp"
)


def test_site_commands_share_one_bounded_connection():
    argv = sites.site_multipass_argv(SITE, "info", "--format", "json")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv and "ControlMaster=auto" in argv
    assert argv[-2:] == ["spark", "/opt/mp info --format json"]


def test_a_home_relative_client_is_expanded_on_the_site():
    site = SITE.model_copy(update={"multipass": "~/mp dir/multipass"})
    argv = sites.site_multipass_argv(site, "list")
    assert argv[-1] == "~/'mp dir/multipass' list"


def test_cloud_init_file_is_staged_on_the_site_and_removed(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sites.subprocess, "run", run)
    local = tmp_path / "tmpabc.yaml"
    local.write_text("ssh_authorized_keys: [k]\n")
    result = sites.SiteCommandBackend(SITE).run(
        ["/opt/mp", "launch", "--name", "vm", "--cloud-init", str(local)]
    )
    assert result.returncode == 0
    stage, launch, cleanup = calls
    assert stage[0][-1] == "cat > /tmp/nl-cloud-init-tmpabc.yaml"
    assert stage[1] == local.read_bytes()
    assert launch[0][-1] == (
        "/opt/mp launch --name vm --cloud-init /tmp/nl-cloud-init-tmpabc.yaml"
    )
    assert cleanup[0][-1] == "rm -f /tmp/nl-cloud-init-tmpabc.yaml"


def test_vm_ssh_jumps_through_the_site_keeping_its_control_path(tmp_path):
    key = tmp_path / "id_vm"
    key.write_text("private")
    key.with_suffix(".pub").write_text("ssh-ed25519 AAAA vm\n")
    site = SITE.model_copy(update={"vm_ssh_key": str(key)})
    provider = sites.SiteVmOrchestrator(tmp_path, site)
    options = provider._ssh_options()
    proxy = next(o for o in options if o.startswith("ProxyCommand="))
    assert "-W %h:%p" in proxy and proxy.endswith("spark -W %h:%p")
    assert "nl-ssh-%%C" in proxy
    assert provider._ssh_credentials() == ("ssh-ed25519 AAAA vm", key)


def node_argv(lifecycle, site_host=None):
    from nanolab.one_shot.infrastructure import NodeResource

    request = SimpleNamespace(lifecycle=lifecycle, name="nl-os-x-edge-1")
    provider = SimpleNamespace(ssh_private_key_path=lambda _r: Path("/k"))
    node = NodeResource(
        SimpleNamespace(id="edge-1"),  # type: ignore[arg-type]
        request,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        provider,
        site_host=site_host,
    )
    vm = VmInfo(name="nl-os-x-edge-1", host="20.1.2.3", user="azureuser", home="/h")
    return node.vm_argv(vm, "sudo", "true")


def test_in_vm_commands_follow_the_node_placement():
    assert node_argv("multipass") == (
        "multipass",
        "exec",
        "nl-os-x-edge-1",
        "--",
        "sudo",
        "true",
    )
    remote = node_argv("multipass", sites.SiteHost(SITE))
    assert remote[-1] == "/opt/mp exec nl-os-x-edge-1 -- sudo true"
    azure = node_argv("azure")
    assert azure[-2:] == ("azureuser@20.1.2.3", "sudo true")
    assert azure[azure.index("-i") : azure.index("-i") + 2] == ("-i", "/k")
    # Periodic checks reuse one connection to the cloud over the WAN.
    assert "ControlMaster=auto" in azure and "ConnectTimeout=15" in azure


def test_bootstrap_groups_reach_later_cloud_commands(monkeypatch):
    from nanolab.one_shot import infrastructure
    from nanolab.one_shot.infrastructure import NodeResource

    calls = []
    monkeypatch.setattr(
        infrastructure.subprocess, "run", lambda argv, **_: calls.append(argv)
    )
    vm = VmInfo(name="c", host="20.1.2.3", user="azureuser", home="/h")
    for lifecycle in ("azure", "multipass"):
        NodeResource(
            SimpleNamespace(id="n"),  # type: ignore[arg-type]
            SimpleNamespace(lifecycle=lifecycle, name="c"),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(ssh_private_key_path=lambda _r: None),
        ).reset_shared_ssh(vm)
    (argv,) = calls
    assert argv[-3:] == ("-O", "exit", "azureuser@20.1.2.3")
    assert any(option.startswith("ControlPath=") for option in argv)


def test_a_tunnel_to_the_cloud_gets_its_own_connection():
    from nanolab.one_shot.infrastructure import NodeResource

    node = NodeResource(
        SimpleNamespace(id="cloud"),  # type: ignore[arg-type]
        SimpleNamespace(lifecycle="azure", name="c"),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(ssh_private_key_path=lambda _r: None),
    )
    vm = VmInfo(name="c", host="20.1.2.3", user="azureuser", home="/h")
    dedicated = node.azure_ssh(vm, shared=False)
    assert "ControlMaster=no" in dedicated and "ControlPath=none" in dedicated
    assert not any(option.startswith("ControlPersist") for option in dedicated)


def test_kernel_cpu_lists_are_parsed():
    assert site_cpu.parse_cpu_list("0-2,5,7-8\n") == [0, 1, 2, 5, 7, 8]


def test_thread_affinity_is_read_from_mirrored_status(tmp_path):
    (tmp_path / "status").write_text("Name:\tqemu\nCpus_allowed_list:\t11-12\n")
    assert site_cpu.status_affinity(tmp_path) == [11, 12]


def scenario(**placement):
    data = {
        "provider": "multipass",
        "purpose": "workflow-validation",
        "nodes": [
            {"id": "edge-1", "kind": "edge", "cpus": 1},
            {"id": "edge-2", "kind": "edge", "cpus": 1},
            {"id": "cloud", "kind": "cloud", "cpus": 2},
        ],
        "functions": {"f": {"input": {}, "cpu": 0.1}},
    }
    if placement:
        data["hostCpuPlacement"] = {
            "nodes": placement,
            "operatorCpus": [4],
        }
    return OneShotConfig.model_validate(data)


def environment(**placement):
    return EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {"stack": {"name": "one-shot"}},
            "sites": {"spark-35": SITE.model_dump(by_alias=True)},
            "placement": placement
            or {"edge-1": "spark-35", "edge-2": "spark-35", "cloud": "azure"},
            "azure": {
                "resource_group": "rg",
                "location": "westeurope",
                "priority": "Spot",
                "eviction_policy": "Delete",
                "work_dir": "/datassd/tofu",
            },
        }
    )


@pytest.fixture
def offline_site(monkeypatch):
    monkeypatch.setattr(sites.SiteHost, "capacity", lambda self: (20, 120000))
    monkeypatch.setattr(sites.SiteHost, "external_vm_cpus", lambda self: 0)
    monkeypatch.setattr(multisite, "egress_address", lambda _run: "149.132.179.35")
    monkeypatch.setattr(
        sites.SiteVmOrchestrator, "__init__", lambda self, root, site: None
    )


def test_nodes_are_owned_on_their_placed_sites(offline_site, tmp_path):
    resources = build_one_shot_resources(
        scenario(), environment(), run_id="r1", repo_root=tmp_path
    )
    assert list(resources.nodes) == ["edge-1", "edge-2", "cloud"]
    assert resources.shared_physical_host is False
    edge, cloud = resources.nodes["edge-2"], resources.nodes["cloud"]
    assert edge.request.name == "nl-os-r1-edge-2"
    assert edge.addresses(
        VmInfo(name="x", host="10.83.80.9", user="ubuntu", home="/home/ubuntu")
    ).api == ("http://149.132.179.35:18002")
    assert cloud.request.lifecycle == "azure"
    assert cloud.request.azure_priority == "Spot"
    assert cloud.request.disk == "64G"
    # Both egress addresses are faked to the site: the rules collapse to it.
    assert cloud.request.azure_ingress == {
        8080: ("149.132.179.35/32",),
        9090: ("149.132.179.35/32",),
        17111: ("149.132.179.35/32",),
    }
    assert isinstance(cloud.provider, multisite.AzureSiteProvider)
    assert cloud.addresses(
        VmInfo(name="c", host="20.1.2.3", user="azureuser", home="/h")
    ).api == ("http://20.1.2.3:8080")


@pytest.mark.parametrize(
    ("placement", "message"),
    [
        ({"edge-1": "spark-35", "edge-2": "spark-35"}, "missing cloud"),
        (
            {"edge-1": "spark-35", "edge-2": "azure", "cloud": "azure"},
            "only the terminal cloud",
        ),
    ],
)
def test_invalid_placement_is_refused_before_any_host(
    offline_site, tmp_path, placement, message
):
    with pytest.raises(ValueError, match=message):
        build_one_shot_resources(
            scenario(), environment(**placement), run_id="r1", repo_root=tmp_path
        )


def test_azure_nodes_must_not_have_host_cpus(offline_site, tmp_path, monkeypatch):
    monkeypatch.setattr(
        multisite.host_cpu, "validate_host_placement", lambda *_a, **_k: {}
    )
    pinned = scenario(
        **{
            "edge-1": {"cpus": [0], "maxFrequencyKHz": 2808000},
            "edge-2": {"cpus": [1], "maxFrequencyKHz": 2808000},
            "cloud": {"cpus": [2, 3], "maxFrequencyKHz": 3900000},
        }
    )
    with pytest.raises(ValueError, match="Azure nodes cannot have host CPU"):
        build_one_shot_resources(pinned, environment(), run_id="r1", repo_root=tmp_path)


def test_a_placement_host_must_be_the_node_site(offline_site, tmp_path, monkeypatch):
    monkeypatch.setattr(
        multisite.host_cpu, "validate_host_placement", lambda *_a, **_k: {}
    )
    pinned = scenario(
        **{
            "edge-1": {"cpus": [0], "maxFrequencyKHz": 2808000},
            "edge-2": {"cpus": [1], "maxFrequencyKHz": 2808000},
        }
    )
    assert pinned.host_cpu_placement is not None
    pinned.host_cpu_placement.hosts.update({"edge-1": "spark-35", "edge-2": "other"})
    with pytest.raises(ValueError, match="names another host for spark-35"):
        build_one_shot_resources(pinned, environment(), run_id="r1", repo_root=tmp_path)


def test_site_capacity_is_checked_against_that_site(
    offline_site, tmp_path, monkeypatch
):
    monkeypatch.setattr(sites.SiteHost, "capacity", lambda self: (1, 120000))
    with pytest.raises(ValueError, match="CPU allocation exceeds host capacity"):
        build_one_shot_resources(
            scenario(), environment(), run_id="r1", repo_root=tmp_path
        )


def test_cloud_ingress_is_bounded_to_operator_and_sites():
    ingress = multisite.cloud_ingress(
        "149.132.176.50", ["149.132.179.35", "149.132.179.175"], extra_ports=(43100,)
    )
    operator = ("149.132.176.50/32",)
    everyone = ("149.132.176.50/32", "149.132.179.175/32", "149.132.179.35/32")
    assert ingress == {
        8080: everyone,
        9090: operator,
        17111: everyone,
        43100: operator,
    }


def test_site_host_failures_surface(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.CalledProcessError(255, argv, stderr="timed out")

    monkeypatch.setattr(sites.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        sites.SiteHost(SITE).capacity()


def test_remote_and_azure_replicas_are_read_through_the_probe_relay():
    vm = VmInfo(name="v", host="10.83.80.9", user="ubuntu", home="/home/ubuntu")
    local = SimpleNamespace(lifecycle="multipass", name="v")
    from nanolab.one_shot.infrastructure import NodeResource

    def node(request, site_host=None):
        return NodeResource(
            SimpleNamespace(id="edge-1"),  # type: ignore[arg-type]
            request,  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            None,
            site_host=site_host,
            ordinal=1,
        )

    assert node(local).replica_url(vm, 32768) == "http://10.83.80.9:32768"
    assert node(local, sites.SiteHost(SITE)).replica_url(vm, 32768) == (
        "http://149.132.179.35:17201/relay/32768"
    )
    azure = SimpleNamespace(lifecycle="azure", name="c")
    assert node(azure).replica_url(vm, 32768) == ("http://10.83.80.9:17111/relay/32768")


@pytest.fixture
def running_probe():
    import socket
    import sys
    import time
    from importlib.resources import as_file, files

    with socket.socket() as probe_port:
        if probe_port.connect_ex(("127.0.0.1", 17111)) == 0:
            pytest.skip("probe port 17111 is in use on this host")
    with as_file(files("nanolab").joinpath("assets", "one-shot", "probe.py")) as path:
        process = subprocess.Popen((sys.executable, str(path)))
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with socket.socket() as attempt:
                    if attempt.connect_ex(("127.0.0.1", 17111)) == 0:
                        break
                time.sleep(0.05)
            yield "http://127.0.0.1:17111"
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_probe_relays_runtime_reads_and_invocations_to_loopback(running_probe):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx

    class Runtime(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"path": "' + self.path.encode() + b'"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            received = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(
                {
                    "path": self.path,
                    "body": received.decode(),
                    "execution": self.headers.get("X-Execution-Id"),
                    "attempt": self.headers.get("X-Dispatch-Attempt"),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Runtime)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with httpx.Client(trust_env=False) as http:
            ok = http.get(f"{running_probe}/relay/{port}/runtime/executions/e-1")
            assert ok.status_code == 200
            assert ok.json() == {"path": "/runtime/executions/e-1"}
            assert ok.headers["content-type"] == "application/json"
            metrics = http.get(f"{running_probe}/relay/{port}/metrics")
            assert metrics.json() == {"path": "/metrics"}
            assert http.get(f"{running_probe}/relay/{port}/v1/x").status_code == 403
            assert http.get(f"{running_probe}/relay/{port}/metricsx").status_code == 403
            assert http.get(f"{running_probe}/relay/0/runtime/x").status_code == 403
            assert http.get(f"{running_probe}/relay/1/runtime/x").status_code == 502
            invoked = http.post(
                f"{running_probe}/relay/{port}/invoke",
                json={"input": {"n": 1}},
                headers={"X-Execution-Id": "e-2", "X-Dispatch-Attempt": "1"},
            )
            assert invoked.status_code == 200
            assert invoked.json() == {
                "path": "/invoke",
                "body": '{"input":{"n":1}}',
                "execution": "e-2",
                "attempt": "1",
            }
            for refused in ("runtime/status", "v1/functions/f:invoke"):
                response = http.post(f"{running_probe}/relay/{port}/{refused}", json={})
                assert response.status_code == 403
            assert http.post(f"{running_probe}/clock").status_code == 404
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    ("machine", "architecture"), [("x86_64", "amd64"), ("aarch64", "arm64")]
)
def test_machine_types_map_to_image_architectures(machine, architecture):
    from nanolab.one_shot.infrastructure import machine_architecture

    assert machine_architecture(machine) == architecture


def test_unknown_machine_type_is_refused():
    from nanolab.one_shot.infrastructure import machine_architecture

    with pytest.raises(ValueError, match="unsupported"):
        machine_architecture("riscv64")


PREBUILT = Path("/datassd/nanolab-twospark/images")


@pytest.mark.skipif(
    not (PREBUILT / "manifest.json").exists(), reason="prebuilt images not present"
)
def test_prebuilt_archives_are_verified_per_architecture(tmp_path):
    import hashlib
    from importlib.resources import files

    from sonata_engine import TaskInputs

    from nanolab.config.one_shot import ArtifactReference
    from nanolab.plans.one_shot_common import _architecture_images

    def reference(arch):
        path = PREBUILT / arch / "distribution.json"
        return ArtifactReference(
            path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        )

    settings = SimpleNamespace(
        runtime_distributions={"arm64": reference("arm64"), "amd64": reference("amd64")}
    )
    recipe = Path(
        str(
            files("nanolab").joinpath(
                "assets", "presets", "recipes", "one-shot-multifunction-jvm.yaml"
            )
        )
    )
    names = ("arithmetic", "sort", "memory", "matrix", "mixed")
    selected = tuple((f"one-shot-{name}", "rust") for name in names)
    distributions, archives = _architecture_images(
        settings,
        recipe=recipe,
        selected=selected,
        run_dir=tmp_path,
        architectures={"arm64", "amd64"},
    )
    for arch in ("arm64", "amd64"):
        built = distributions[arch].acquire(TaskInputs._for_resources({}, set()))
        inputs = TaskInputs._for_resources(
            {distributions[arch]: built}, {distributions[arch]}
        )
        assert archives[arch].acquire(inputs) == PREBUILT / arch / f"images-{arch}.tar"
        assert (tmp_path / f"runtime-distribution-{arch}.json").exists()
    with pytest.raises(ValueError, match="no runtime distribution for riscv"):
        _architecture_images(
            settings,
            recipe=recipe,
            selected=selected,
            run_dir=tmp_path,
            architectures={"riscv"},
        )


def test_an_azure_upload_is_bounded_and_retried_on_a_fresh_connection(
    tmp_path, monkeypatch
):
    """A stalled SFTP upload once held a run for half an hour."""
    from nanolab.one_shot import infrastructure

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, argv, stderr="lost connection")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(infrastructure.subprocess, "run", run)
    node = SimpleNamespace(
        config=SimpleNamespace(id="cloud"),
        request=SimpleNamespace(lifecycle="azure"),
        azure_ssh=lambda vm, shared: (
            "ssh",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            f"ControlMaster={'auto' if shared else 'no'}",
            f"{vm.user}@{vm.host}",
        ),
    )
    vm = VmInfo(name="c", host="20.1.2.3", user="azureuser", home="/home/azureuser")
    archive = tmp_path / "images.tar"
    infrastructure.transfer_to_vm(cast(Any, node), vm, archive, "/home/azureuser/i.tar")
    assert len(calls) == 3
    argv, timeout = calls[-1]
    assert argv == [
        "scp",
        "-q",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ControlMaster=no",
        str(archive),
        "azureuser@20.1.2.3:/home/azureuser/i.tar",
    ]
    assert timeout == infrastructure.AZURE_TRANSFER_SECONDS
    calls.clear()
    monkeypatch.setattr(
        infrastructure.subprocess,
        "run",
        lambda argv, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, argv, stderr="refused")
        ),
    )
    with pytest.raises(RuntimeError, match=r"images\.tar failed: refused; refused"):
        infrastructure.transfer_to_vm(cast(Any, node), vm, archive, "/x")


def test_a_site_upload_still_goes_through_the_provider(tmp_path):
    from nanolab.one_shot import infrastructure

    sent = []
    node = SimpleNamespace(
        config=SimpleNamespace(id="edge-6"),
        request=SimpleNamespace(lifecycle="multipass"),
        provider=SimpleNamespace(
            transfer_to=lambda request, *, source, destination: (
                sent.append((source, destination)) or SimpleNamespace(return_code=0)
            )
        ),
    )
    vm = VmInfo(name="e", host="10.0.0.2", user="ubuntu", home="/home/ubuntu")
    infrastructure.transfer_to_vm(cast(Any, node), vm, tmp_path / "f", "/home/ubuntu/f")
    assert sent == [(tmp_path / "f", "/home/ubuntu/f")]
