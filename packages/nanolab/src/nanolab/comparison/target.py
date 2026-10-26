"""Read-only probes for the machines and cluster an experiment originally used."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.tasks.vm.models import VmRequest

_MACHINE_ID = re.compile(r"[0-9a-f]{32}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _probe(
    provider: VmCommandProvider, request: VmRequest, argv: tuple[str, ...]
) -> str:
    result = provider.exec_argv(request, argv, env=None, remote_dir=None, dry_run=False)
    if result.return_code != 0:
        raise RuntimeError(
            f"Cannot probe comparison target identity: {argv}: {result.stderr}"
        )
    return result.stdout.strip()


def _identifier(value: object, *, machine: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing comparison target identity")
    normalized = value.lower()
    expression = _MACHINE_ID if machine else _UUID
    if not expression.fullmatch(normalized) or set(normalized.replace("-", "")) == {
        "0"
    }:
        raise ValueError("Missing or malformed comparison target identity")
    return normalized


def read_comparison_target(
    provider: VmCommandProvider,
    requests: Mapping[str, VmRequest],
    *,
    kubernetes: bool = True,
) -> dict[str, object]:
    """Probe stable machine identities, and the cluster's or the Docker engine's.

    A stack without Kubernetes, like experiment 02's, is identified by its
    Docker engine instead: a reinstalled engine is a different target.
    """
    if "stack" not in requests:
        raise ValueError("Missing stack target identity")
    machines = {}
    for role, request in requests.items():
        machines[role] = {
            "name": request.name,
            "machineId": _identifier(
                _probe(provider, request, ("cat", "/etc/machine-id")), machine=True
            ),
            "productUuid": _identifier(
                _probe(
                    provider, request, ("sudo", "cat", "/sys/class/dmi/id/product_uuid")
                )
            ),
        }
    request = requests["stack"]
    if not kubernetes:
        engine = _probe(provider, request, ("docker", "info", "--format", "{{.ID}}"))
        if not engine or any(character.isspace() for character in engine):
            raise ValueError("Missing or malformed Docker engine identity")
        return {"machines": machines, "engine": {"dockerId": engine}}
    try:
        namespace = json.loads(
            _probe(
                provider,
                request,
                (
                    "sudo",
                    "k3s",
                    "kubectl",
                    "get",
                    "namespace",
                    "kube-system",
                    "-o",
                    "json",
                ),
            )
        )
        nodes = json.loads(
            _probe(
                provider,
                request,
                ("sudo", "k3s", "kubectl", "get", "nodes", "-o", "json"),
            )
        )
        node_uids = {
            node["metadata"]["name"]: _identifier(node["metadata"]["uid"])
            for node in nodes["items"]
        }
        namespace_uid = _identifier(namespace["metadata"]["uid"])
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("Invalid comparison cluster identity") from error
    if not node_uids or len(node_uids) != len(nodes["items"]):
        raise ValueError("Missing or duplicate comparison node identity")
    return {
        "machines": machines,
        "cluster": {"namespaceUid": namespace_uid, "nodes": node_uids},
    }


def _validate_target(target: Mapping[str, object]) -> None:
    try:
        machines = target["machines"]
        if not isinstance(machines, dict) or "stack" not in machines:
            raise ValueError("incomplete")
        for machine in machines.values():
            _identifier(machine["machineId"], machine=True)
            _identifier(machine["productUuid"])
        if "engine" in target:
            engine = target["engine"]
            if not isinstance(engine, dict) or not engine.get("dockerId"):
                raise ValueError("incomplete engine")
            return
        cluster = target["cluster"]
        if not isinstance(cluster, dict):
            raise ValueError("incomplete")
        _identifier(cluster["namespaceUid"])
        if not cluster["nodes"]:
            raise ValueError("empty nodes")
        for uid in cluster["nodes"].values():
            _identifier(uid)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("Incomplete comparison target identity") from error


def require_comparison_target(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> None:
    """Refuse replacements even when they have the original machine names."""
    _validate_target(expected)
    _validate_target(actual)
    if expected != actual:
        raise ValueError("Comparison target identity differs; use a new run directory")


HOST_FACTS_PROBE = (
    "sh",
    "-c",
    "uname -m; uname -r; nproc; "
    "awk '/^MemTotal:/ {print $2}' /proc/meminfo; "
    '. /etc/os-release && echo "$PRETTY_NAME"',
)


def read_host_facts(
    provider: VmCommandProvider, requests: Mapping[str, VmRequest]
) -> dict[str, dict[str, object]]:
    """Read what each host is, beyond which host it is.

    Architecture, kernel, CPUs, memory and OS, per role. The SKU is already in
    the role's request; these are what that SKU actually delivered, and a
    resume onto a host whose facts differ is refused like any other change.
    """
    facts: dict[str, dict[str, object]] = {}
    for role, request in requests.items():
        lines = _probe(provider, request, HOST_FACTS_PROBE).splitlines()
        if len(lines) != 5 or not lines[2].isdigit() or not lines[3].isdigit():
            raise ValueError(f"Invalid host facts for {role}")
        facts[role] = {
            "architecture": lines[0],
            "kernel": lines[1],
            "cpus": int(lines[2]),
            "memoryKiB": int(lines[3]),
            "os": lines[4],
        }
    return facts
