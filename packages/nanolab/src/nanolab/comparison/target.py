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
    provider: VmCommandProvider, requests: Mapping[str, VmRequest]
) -> dict[str, object]:
    """Probe stable machine and Kubernetes identities without provisioning."""
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
        cluster = target["cluster"]
        if (
            not isinstance(machines, dict)
            or "stack" not in machines
            or not isinstance(cluster, dict)
        ):
            raise ValueError("incomplete")
        for machine in machines.values():
            _identifier(machine["machineId"], machine=True)
            _identifier(machine["productUuid"])
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
