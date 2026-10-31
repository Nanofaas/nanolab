"""Observe B6 lifecycle transitions without inferring zero from missing data."""

from __future__ import annotations

from itertools import pairwise
from typing import Any


def _pod(item: dict[str, Any], container: str) -> dict[str, Any]:
    metadata = item["metadata"]
    statuses = [
        s
        for s in item.get("status", {}).get("containerStatuses", [])
        if s["name"] == container
    ]
    status = statuses[0] if len(statuses) == 1 else {}
    return {
        "uid": metadata["uid"],
        "name": metadata["name"],
        "created_at": metadata.get("creationTimestamp"),
        "ready": status.get("ready", False),
        "restarts": status.get("restartCount"),
        "image_id": status.get("imageID"),
        "deleting": bool(metadata.get("deletionTimestamp")),
    }


def parse_observation(
    data: dict[str, Any], function: str, timestamp: float
) -> dict[str, Any]:
    """Keep terminating pods visible, selecting from the actual Deployment."""
    deployments = [
        d
        for d in data["items"]
        if d["kind"] == "Deployment" and d["metadata"]["name"] == f"fn-{function}"
    ]
    if len(deployments) != 1:
        raise ValueError("function Deployment is missing or ambiguous")
    deployment = deployments[0]
    selector = deployment["spec"]["selector"]["matchLabels"]
    if not selector:
        raise ValueError("function Deployment has no selector")
    pods = [p for p in data["items"] if p["kind"] == "Pod"]
    matching = [
        p
        for p in pods
        if all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())
    ]
    control = [
        p
        for p in pods
        if any(
            c["name"] == "control-plane"
            for c in p.get("spec", {}).get("containers", [])
        )
    ]
    return {
        "time": timestamp,
        "desired": deployment["spec"].get("replicas", 1),
        "ready": deployment.get("status", {}).get("readyReplicas", 0),
        "pods": [_pod(p, "function") for p in matching],
        "control_plane": [_pod(p, "control-plane") for p in control],
    }


def _stable_identity(pods: list[dict[str, Any]]) -> tuple[str, str] | None:
    if len(pods) != 1:
        return None
    pod = pods[0]
    if (
        pod.get("restarts") != 0
        or pod.get("deleting", False)
        or not pod.get("ready", True)
        or not pod.get("image_id")
    ):
        return None
    return pod["uid"], pod["image_id"]


def qualifies_idle(
    samples: list[dict[str, Any]], start: float, end: float, treatment: str
) -> bool:
    """Require a fully observed 60-second window; at most five seconds per gap."""
    window = [s for s in samples if start - 2 <= s["time"] <= end + 0.01]
    if not window or window[0]["time"] > start + 2 or end - window[-1]["time"] > 2:
        return False
    if any(b["time"] - a["time"] > 5 for a, b in pairwise(window)):
        return False
    identities, control = set(), set()
    for sample in window:
        if "error" in sample:
            return False
        cp = _stable_identity(sample.get("control_plane", []))
        if cp is None:
            return False
        control.add(cp)
        if treatment == "scale-to-zero":
            if sample["desired"] != 0 or sample["ready"] != 0 or sample["pods"]:
                return False
        else:
            identity = _stable_identity(sample["pods"])
            if sample["desired"] != 1 or sample["ready"] != 1 or identity is None:
                return False
            identities.add(identity)
    return len(control) == 1 and (treatment == "scale-to-zero" or len(identities) == 1)


def qualifies_response(
    before: dict[str, Any],
    after: dict[str, Any],
    treatment: str,
    seen_uids: set[str],
    image_id: str,
) -> bool:
    """Require the expected pod identity after the complete request burst."""
    if "error" in before or "error" in after:
        return False
    current = _stable_identity(after.get("pods", []))
    cp = _stable_identity(before.get("control_plane", []))
    if (
        current is None
        or current[1] != image_id
        or cp is None
        or cp != _stable_identity(after.get("control_plane", []))
    ):
        return False
    if treatment == "scale-to-zero":
        return current[0] not in seen_uids
    return current == _stable_identity(before.get("pods", []))


def qualifies_burst(
    samples: list[dict[str, Any]], start: float, end: float, after: dict[str, Any]
) -> bool:
    """Reject observed replacement/restart within the first-plus-followup burst."""
    final = _stable_identity(after.get("pods", []))
    if final is None:
        return False
    for sample in samples:
        if not start <= sample["time"] <= end:
            continue
        if "error" in sample:
            return False
        for pod in sample.get("pods", []):
            if (
                pod["uid"] != final[0]
                or pod.get("deleting")
                or pod.get("restarts") not in (None, 0)
            ):
                return False
    return True
