"""Reject incomplete or recycled native process evidence."""

import os
import subprocess
from copy import deepcopy

import pytest


def observation():
    identity = {
        "podUid": "owned-pod",
        "containerId": "containerd://" + "a" * 64,
        "node": "owned-node",
        "pid": 402,
        "startTime": "10412",
    }
    return {
        "before": identity,
        "after": dict(identity),
        "commandLine": ["/app/application", "-Dreactor.netty.ioWorkerCount=1"],
        "cpuLimit": "1",
        "logsBefore": "Started ControlPlaneApplication",
        "logsAfter": "Started ControlPlaneApplication",
        "threads": [{"id": 403, "name": "reactor-http-ep"}],
    }


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing-log",
        "reflection",
        "resource",
        "unsupported",
        "argument",
        "jvm",
        "workers",
        "nio",
        "pid",
        "start",
        "uid",
        "container",
        "node",
        "missing-process",
        "cpu",
        "oversize",
        "threads",
    ],
)
def test_runtime_evidence_is_complete_and_belongs_to_the_same_native_process(fault):
    from nanolab.assets.diagnostics.native_k8s_runtime import verify_native_runtime

    data = deepcopy(observation())
    if fault == "missing-log":
        data["logsBefore"] = ""
    elif fault in ("reflection", "resource", "unsupported"):
        errors = {
            "reflection": "MissingReflectionRegistrationError",
            "resource": "MissingResourceRegistrationError",
            "unsupported": "UnsupportedFeatureError",
        }
        data["logsAfter"] += errors[fault]
    elif fault == "argument":
        data["commandLine"] = ["/app/application"]
    elif fault == "jvm":
        data["commandLine"][0] = "/usr/bin/java"
    elif fault == "workers":
        data["threads"].append({"id": 404, "name": "reactor-http-ep"})
    elif fault == "nio":
        data["threads"][0]["name"] = "reactor-http-ni"
    elif fault in ("pid", "start", "uid", "container", "node"):
        key = {"start": "startTime", "uid": "podUid", "container": "containerId"}.get(
            fault, fault
        )
        data["after"][key] = "replaced"
    elif fault == "missing-process":
        data["before"]["pid"] = 0
    elif fault == "cpu":
        data["cpuLimit"] = "4"
    elif fault == "oversize":
        data["logsAfter"] = "x" * (8 * 1024 * 1024 + 1)
    elif fault == "threads":
        data["threads"] = [{"id": n, "name": "other"} for n in range(4097)]
    if fault is None:
        verify_native_runtime(data, expected_workers=1, require_epoll=True)
    else:
        with pytest.raises(RuntimeError):
            verify_native_runtime(data, expected_workers=1, require_epoll=True)


@pytest.mark.parametrize(
    "fault", [None, "missing", "wrong-id", "wrong-pod", "missing-pid", "ambiguous-pid"]
)
def test_cri_process_requires_exact_container_and_pod_identity(fault):
    from nanolab.assets.diagnostics.native_k8s_runtime import cri_pid

    container_id = "a" * 64
    data = {
        "status": {
            "id": container_id,
            "state": "CONTAINER_RUNNING",
            "labels": {"io.kubernetes.pod.uid": "owned-pod"},
        },
        "info": {"pid": 402},
    }
    if fault == "missing":
        data = {}
    elif fault == "wrong-id":
        data["status"]["id"] = "b" * 64
    elif fault == "wrong-pod":
        data["status"]["labels"]["io.kubernetes.pod.uid"] = "foreign-pod"
    elif fault == "missing-pid":
        data["info"]["pid"] = 0
    elif fault == "ambiguous-pid":
        data["info"]["pid"] = [402, 403]
    if fault is None:
        assert cri_pid(data, container_id=container_id, pod_uid="owned-pod") == 402
    else:
        with pytest.raises(RuntimeError):
            cri_pid(data, container_id=container_id, pod_uid="owned-pod")


def test_procfs_reader_observes_a_real_linux_process():
    from nanolab.assets.diagnostics.native_k8s_runtime import (
        PROCFS_SCRIPT,
        parse_procfs,
    )

    result = subprocess.run(
        ["sh", "-c", PROCFS_SCRIPT, "probe", str(os.getpid())],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    data = parse_procfs(result.stdout)
    assert data["before"] == data["after"]
    assert data["before"].isdigit()
    assert data["commandLine"] and data["threads"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "before\t10\n",
        "before\t10\nbefore\t11\n",
        "truncated",
        "x" * (1024 * 1024 + 1),
    ],
)
def test_procfs_receipt_rejects_missing_or_truncated_data(text):
    from nanolab.assets.diagnostics.native_k8s_runtime import parse_procfs

    with pytest.raises(RuntimeError):
        parse_procfs(text)


@pytest.mark.parametrize("name", ["or-http-epoll-1", "actor-http-nio-1"])
def test_graalvm_suffix_names_still_distinguish_workers_and_transport(name):
    from nanolab.assets.diagnostics.native_k8s_runtime import verify_native_runtime

    data = observation()
    data["threads"] = [
        {"id": 403, "name": name},
        {"id": 404, "name": "-select-epoll-1"},
        {"id": 405, "name": "as-mgmt-epoll-2"},
    ]
    if "nio" in name:
        with pytest.raises(RuntimeError, match="epoll"):
            verify_native_runtime(data, expected_workers=1, require_epoll=True)
    else:
        verify_native_runtime(data, expected_workers=1, require_epoll=True)
