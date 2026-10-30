"""faasd uses containerd namespaces and ephemeral authentication."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import PurePosixPath

import pytest

from nanolab.tasks.experiment_02.faasd import Faasd, teardown_argv
from nanolab.tasks.experiment_02.platforms import FIRST_RESPONSE

from .fakes import RecordingProvider, Result, vm
from .test_baselines_containers import PAYLOAD, PROBE, _protocol


def test_faasd_target_and_payload_preserve_the_shared_function_contract():
    platform = Faasd(root=PurePosixPath("/opt/faasd"), image="registry/qr:recorded")
    target = platform.target("faasd.internal")
    assert target.url == "http://faasd.internal:30080/function/qr"
    assert target.extractor == "json-base64"
    assert platform.body(b'{"input":{"text":"A"}}') == b'{"input":{"text":"A"}}'
    assert "faasd" in platform.host_commands


def test_faasd_cleanup_is_confined_to_its_containerd_namespaces():
    argv = teardown_argv()
    script = argv[-1]
    assert "docker" not in script
    assert "openfaas openfaas-fn" in script
    assert "systemctl stop faasd faasd-provider" in script
    assert script.index("--request DELETE") < script.index("systemctl stop")
    assert "tasks delete" not in script
    assert "CNI leases remain" in script


def test_faasd_start_does_not_touch_docker_or_persist_the_password(tmp_path):
    def handler(argv, _env):
        if argv[0] == "python3":
            provider.files[str(FIRST_RESPONSE)] = b'"..."'
            return Result(0, json.dumps(PROBE))
        return None

    provider = RecordingProvider(handler=handler)
    platform = Faasd(root=PurePosixPath("/var/lib/faasd"), image="registry/qr:recorded")
    timings = platform.start(
        vm(provider),
        protocol=_protocol(),
        payload=PAYLOAD,
        evidence=tmp_path,
    )
    assert timings is not None
    assert not any("docker" in argv for argv in provider.commands())
    spec = json.loads((tmp_path / "readiness-spec.json").read_text())
    assert spec["invoke"]["headersCommand"]
    assert "Authorization" not in spec["invoke"]["headers"]


def test_cleanup_refuses_an_unreachable_containerd(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, content in {
        "ctr": "exit 1",
        "systemctl": "exit 0",
    }.items():
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{content}\n")
        path.chmod(0o755)
    result = subprocess.run(
        ("sh", "-c", teardown_argv()[-1]),
        env={**os.environ, "PATH": f"{bindir}:{os.defpath}"},
        capture_output=True,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("http_status", "residual", "success"),
    [
        ("404", False, True),
        ("404", True, False),
        ("401", False, False),
        ("204", False, True),
    ],
)
def test_cleanup_verifies_state_after_idempotent_delete(
    tmp_path, http_status, residual, success
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "listed"
    root = tmp_path / "faasd"
    (root / "secrets").mkdir(parents=True)
    (root / "secrets/basic-auth-password").write_text("test-password")
    ctr = f'if [ ! -e "{state}" ]; then touch "{state}"; echo qr;'
    ctr += " else echo qr;" if residual else " else :;"
    ctr += " fi"
    for name, content in {
        "ctr": ctr,
        "systemctl": "exit 0",
        "curl": (
            'for arg; do case "$arg" in -f|-fsS) '
            f"[ {http_status} -lt 400 ] || exit 22;; esac; done; "
            f"printf {http_status}"
        ),
    }.items():
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{content}\n")
        path.chmod(0o755)
    result = subprocess.run(
        ("sh", "-c", teardown_argv(PurePosixPath(root))[-1]),
        env={**os.environ, "PATH": f"{bindir}:{os.defpath}"},
        capture_output=True,
    )
    assert (result.returncode == 0) is success


def _orphan_cleanup(tmp_path, owner="qr-99999999"):
    """Exercise the actual teardown with an IPAM lease from a crashed task."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    leases = tmp_path / "ipam/openfaas-cni-bridge"
    leases.mkdir(parents=True)
    cache = tmp_path / "cache/results"
    cache.mkdir(parents=True)
    configs = tmp_path / "conf"
    configs.mkdir()
    namespaces = tmp_path / "netns"
    namespaces.mkdir()
    nat = tmp_path / "masquerade-rule"
    nat.touch()
    config = {
        "cniVersion": "0.4.0",
        "name": "openfaas-cni-bridge",
        "plugins": [
            {
                "type": "bridge",
                "bridge": "openfaas0",
                "ipMasq": True,
                "ipam": {
                    "type": "host-local",
                    "dataDir": str(leases.parent),
                },
            },
            {"type": "firewall"},
        ],
    }
    (configs / "10-openfaas.conflist").write_text(json.dumps(config))
    lease = leases / "10.62.0.12"
    lease.write_text(owner + "\neth1\n")
    for name, interface, network in [
        ("openfaas-cni-bridge", "eth1", config),
        (
            "cni-loopback",
            "lo",
            {
                "cniVersion": "0.3.1",
                "name": "cni-loopback",
                "plugins": [{"type": "loopback"}],
            },
        ),
    ]:
        (cache / f"{name}-{owner}-{interface}").write_text(
            json.dumps(
                {
                    "kind": "cniCacheV1",
                    "containerId": owner,
                    "networkName": name,
                    "ifName": interface,
                    "config": base64.b64encode(json.dumps(network).encode()).decode(),
                    "result": {
                        "cniVersion": network["cniVersion"],
                        "interfaces": [{"name": interface}],
                        "ips": [{"address": "10.62.0.12/16", "interface": 0}],
                    },
                }
            )
        )
    calls = tmp_path / "calls.jsonl"
    for name in ["ctr", "systemctl"]:
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    ip = bindir / "ip"
    ip.write_text(
        "#!/usr/bin/python3\nimport json, pathlib, sys\n"
        f"with open({str(tmp_path / 'ip-calls.jsonl')!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['netns', 'add']:\n"
        f"    namespace = pathlib.Path({str(namespaces)!r}) / sys.argv[3]\n"
        f"    if pathlib.Path({str(tmp_path / 'namespace-fail')!r}).exists():\n"
        "        namespace.write_text('pre-existing namespace')\n"
        "        sys.exit(1)\n"
        "    namespace.touch(exist_ok=False)\n"
        "elif sys.argv[1:3] == ['netns', 'del']:\n"
        f"    (pathlib.Path({str(namespaces)!r}) / sys.argv[3]).unlink()\n"
    )
    ip.chmod(0o755)
    iptables = bindir / "iptables-save"
    chain = (
        "CNI-"
        + hashlib.sha512(("openfaas-cni-bridge" + owner).encode()).hexdigest()[:24]
    )
    iptables.write_text(
        "#!/usr/bin/python3\nimport pathlib\n"
        "print('*nat')\n"
        f"if pathlib.Path({str(nat)!r}).exists():\n"
        f"    print({(':' + chain + ' - [0:0]')!r})\n"
        "print('COMMIT')\n"
    )
    iptables.chmod(0o755)
    for name in ["firewall", "bridge", "loopback"]:
        path = bindir / name
        path.write_text(
            "#!/usr/bin/python3\nimport json, os, pathlib, sys\n"
            "config = json.load(sys.stdin)\n"
            "assert os.environ['CNI_COMMAND'] == 'DEL'\n"
            f"with open({str(calls)!r}, 'a') as stream:\n"
            "    stream.write(json.dumps({'plugin': pathlib.Path(sys.argv[0]).name, "
            "'id': os.environ['CNI_CONTAINERID'], 'config': config}) + '\\n')\n"
            f"failure = pathlib.Path({str(tmp_path / 'fail')!r})\n"
            "if failure.exists():\n"
            "    if failure.read_text() in ['', pathlib.Path(sys.argv[0]).name]:\n"
            "        print(json.dumps({'code': 100, 'msg': 'injected CNI failure'}))\n"
            "        sys.exit(1)\n"
            f"if pathlib.Path({str(tmp_path / 'no_release')!r}).exists(): sys.exit(0)\n"
            "if config['type'] == 'bridge':\n"
            f"    for lease in pathlib.Path({str(leases)!r}).iterdir():\n"
            "        owner = lease.read_text().splitlines()[0]\n"
            "        if owner == os.environ['CNI_CONTAINERID']:\n"
            "            lease.unlink()\n"
            # Pinned bridge DEL skips masquerade cleanup for an empty netns.
            "    if os.environ['CNI_NETNS']:\n"
            "        assert pathlib.Path(os.environ['CNI_NETNS']).exists()\n"
            f"        absent = not pathlib.Path({str(nat)!r}).exists()\n"
            "        if config.get('ipMasq') and absent:\n"
            "            print(json.dumps({'code': 999, 'msg': 'NAT chain absent'}))\n"
            "            sys.exit(1)\n"
            f"        pathlib.Path({str(nat)!r}).unlink(missing_ok=True)\n"
        )
        path.chmod(0o755)
    script = teardown_argv()[-1]
    for original, replacement in [
        ("/var/run/cni", str(leases.parent)),
        ("/var/lib/cni", str(cache.parent)),
        ("/etc/cni/net.d", str(configs)),
        ("/opt/cni/bin", str(bindir)),
        ("/var/run/netns", str(namespaces)),
    ]:
        script = script.replace(original, replacement)

    def run():
        return subprocess.run(
            ("sh", "-c", script),
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{bindir}:{os.defpath}"},
        )

    return run, lease, cache, calls


def test_cleanup_releases_crashed_task_network_through_cni_del(tmp_path):
    run, lease, cache, calls = _orphan_cleanup(tmp_path)
    result = run()
    assert result.returncode == 0, result.stderr
    assert not lease.exists()
    assert not list(cache.iterdir())
    operations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [op["plugin"] for op in operations] == ["firewall", "bridge", "loopback"]
    assert {op["id"] for op in operations} == {"qr-99999999"}
    assert operations[0]["config"]["prevResult"]["ips"][0]["address"] == "10.62.0.12/16"
    assert run().returncode == 0
    assert len(calls.read_text().splitlines()) == 3


def test_cleanup_removes_masquerade_rule_with_an_owned_temporary_namespace(tmp_path):
    run, _lease, _cache, _calls = _orphan_cleanup(tmp_path)
    result = run()
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "masquerade-rule").exists()
    assert not list((tmp_path / "netns").iterdir())
    commands = [
        json.loads(line)
        for line in (tmp_path / "ip-calls.jsonl").read_text().splitlines()
    ]
    assert commands[0][:2] == ["netns", "add"]
    assert commands[-1] == ["netns", "del", commands[0][2]]


def test_cleanup_does_not_remove_a_namespace_when_creation_failed(tmp_path):
    run, lease, cache, calls = _orphan_cleanup(tmp_path)
    (tmp_path / "namespace-fail").touch()
    assert run().returncode != 0
    assert lease.exists()
    assert len(list(cache.iterdir())) == 2
    assert not calls.exists()
    namespaces = list((tmp_path / "netns").iterdir())
    assert len(namespaces) == 1
    assert namespaces[0].read_text() == "pre-existing namespace"


@pytest.mark.parametrize(
    "problem",
    [
        "foreign_owner",
        "missing_cache",
        "wrong_config",
        "wrong_interface",
        "ipv6",
        "live_pid",
    ],
)
def test_cleanup_refuses_unknown_or_live_network_before_any_cni_mutation(
    tmp_path, problem
):
    owner = f"qr-{os.getpid()}" if problem == "live_pid" else "qr-99999999"
    run, lease, cache, calls = _orphan_cleanup(tmp_path, owner)
    if problem == "foreign_owner":
        (lease.parent / "10.62.0.13").write_text("foreign-99999998\neth1\n")
    elif problem == "missing_cache":
        (cache / f"openfaas-cni-bridge-{owner}-eth1").unlink()
    elif problem in {"wrong_config", "wrong_interface", "ipv6"}:
        path = cache / f"openfaas-cni-bridge-{owner}-eth1"
        value = json.loads(path.read_text())
        if problem == "wrong_config":
            value["config"] = base64.b64encode(b'{"name":"other-network"}').decode()
        elif problem == "wrong_interface":
            value["result"]["interfaces"][0]["name"] = "foreign-interface"
        else:
            value["result"]["ips"][0]["address"] = "fd00::1/64"
        path.write_text(json.dumps(value))
    result = run()
    assert result.returncode != 0
    assert lease.exists()
    assert not calls.exists()


def test_cleanup_keeps_evidence_and_refuses_when_cni_del_fails(tmp_path):
    run, lease, cache, calls = _orphan_cleanup(tmp_path)
    (tmp_path / "fail").touch()
    result = run()
    assert result.returncode != 0
    assert "injected CNI failure" in result.stderr
    assert lease.exists()
    assert len(list(cache.iterdir())) == 2
    assert len(calls.read_text().splitlines()) == 1
    assert not list((tmp_path / "netns").iterdir())


def test_cleanup_retries_cached_attachment_when_lease_was_already_released(tmp_path):
    run, lease, cache, calls = _orphan_cleanup(tmp_path)
    failure = tmp_path / "fail"
    failure.write_text("loopback")
    assert run().returncode != 0
    assert not lease.exists()
    assert len(list(cache.iterdir())) == 2
    failure.unlink()
    result = run()
    assert result.returncode == 0, result.stderr
    assert not list(cache.iterdir())
    assert len(calls.read_text().splitlines()) == 6


def test_cleanup_retains_cache_until_ipam_release_is_verified(tmp_path):
    run, lease, cache, _calls = _orphan_cleanup(tmp_path)
    (tmp_path / "no_release").touch()
    assert run().returncode != 0
    assert lease.exists()
    assert len(list(cache.iterdir())) == 2


def test_cleanup_finishes_when_reboot_already_removed_masquerade_chain(tmp_path):
    run, lease, cache, calls = _orphan_cleanup(tmp_path)
    (tmp_path / "masquerade-rule").unlink()
    result = run()
    assert result.returncode == 0, result.stderr
    assert not lease.exists()
    assert not list(cache.iterdir())
    bridge = next(
        json.loads(line)
        for line in calls.read_text().splitlines()
        if json.loads(line)["plugin"] == "bridge"
    )
    assert bridge["config"]["ipMasq"] is False
