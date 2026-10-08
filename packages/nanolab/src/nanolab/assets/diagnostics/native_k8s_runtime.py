"""Bounded Linux process observations for native Kubernetes qualification."""

from __future__ import annotations

import base64
from typing import Any

LOG_LIMIT = 8 * 1024 * 1024
RUNTIME_LIMIT = 1024 * 1024
THREAD_LIMIT = 4096
NATIVE_ERRORS = (
    "UnsupportedFeatureError",
    "MissingReflectionRegistrationError",
    "MissingResourceRegistrationError",
)

# Run on the selected node as root. No Python installation in the node is needed.
# The numeric PID is supplied as a positional argument, never interpolated into shell.
PROCFS_SCRIPT = r"""
set -eu
pid="$1"
case "$pid" in ''|*[!0-9]*) exit 1;; esac
test "$pid" -gt 0
start() { sed 's/.*) //' "/proc/$pid/stat" | awk '{print $20}'; }
printf 'before\t%s\n' "$(start)"
printf 'command\t'
base64 -w0 "/proc/$pid/cmdline"
printf '\n'
count=0
for path in /proc/"$pid"/task/*/comm; do
    count=$((count + 1))
    test "$count" -le 4096
    IFS= read -r name < "$path"
    tid="${path%/comm}"
    printf 'thread\t%s\t%s\n' "${tid##*/}" "$name"
done
test "$count" -gt 0
printf 'after\t%s\n' "$(start)"
"""


def cri_pid(data: dict[str, Any], *, container_id: str, pod_uid: str) -> int:
    """Resolve an exact CRI container, refusing prefixes or unrelated processes."""
    status, info = data.get("status", {}), data.get("info", {})
    if (
        not isinstance(status, dict)
        or not isinstance(info, dict)
        or status.get("id") != container_id
        or status.get("state") != "CONTAINER_RUNNING"
        or status.get("labels", {}).get("io.kubernetes.pod.uid") != pod_uid
    ):
        raise RuntimeError("native CRI container identity is missing or differs")
    pid = info.get("pid")
    if type(pid) is not int or pid <= 0:
        raise RuntimeError("native CRI process is missing or ambiguous")
    return pid


def parse_procfs(text: str) -> dict[str, Any]:
    """Read the complete, bounded procfs receipt without discarding identity."""
    if len(text.encode()) > RUNTIME_LIMIT:
        raise RuntimeError("native runtime receipt exceeds byte bound")
    result: dict[str, Any] = {"threads": []}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[0] == "thread":
            result["threads"].append({"id": int(fields[1]), "name": fields[2]})
        elif len(fields) == 2 and fields[0] in ("before", "after", "command"):
            if fields[0] in result:
                raise RuntimeError("native runtime receipt has duplicate fields")
            result[fields[0]] = fields[1]
        else:
            raise RuntimeError("native runtime receipt is malformed")
    if not all(result.get(key) for key in ("before", "after", "command", "threads")):
        raise RuntimeError("native runtime receipt is incomplete")
    raw = base64.b64decode(result.pop("command"), validate=True)
    if not raw.endswith(b"\0"):
        raise RuntimeError("native command line is truncated")
    result["commandLine"] = raw[:-1].decode().split("\0")
    return result


def verify_native_runtime(
    observation: dict[str, object], *, expected_workers: int, require_epoll: bool
) -> None:
    """Qualify logs, worker arguments/count and a continuous process identity."""
    before, after = observation.get("before"), observation.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict) or before != after:
        raise RuntimeError("native pod or process identity changed during observation")
    for key in ("podUid", "containerId", "node", "startTime"):
        if not isinstance(before.get(key), str) or not before[key]:
            raise RuntimeError("native process identity is incomplete")
    if (
        not before["startTime"].isdigit()
        or type(before.get("pid")) is not int
        or before["pid"] <= 0
    ):
        raise RuntimeError("native process identity has no PID/start time")
    for key in ("logsBefore", "logsAfter"):
        logs = observation.get(key)
        if not isinstance(logs, str) or not logs or len(logs.encode()) > LOG_LIMIT:
            raise RuntimeError("native logs are missing or exceed byte bound")
        if any(error in logs for error in NATIVE_ERRORS):
            raise RuntimeError(
                "native logs contain an unsupported/reflection/resource error"
            )
    if observation.get("cpuLimit") not in ("1", "1000m"):
        raise RuntimeError("native runtime must run with a one-CPU limit")
    argv = observation.get("commandLine")
    if (
        not isinstance(argv, list)
        or not argv
        or str(argv[0]).rsplit("/", 1)[-1] == "java"
    ):
        raise RuntimeError("native runtime command is missing or starts a JVM")
    worker_args = [
        value
        for value in argv
        if isinstance(value, str) and value.startswith("-Dreactor.netty.ioWorkerCount=")
    ]
    if worker_args != [f"-Dreactor.netty.ioWorkerCount={expected_workers}"]:
        raise RuntimeError("native runtime worker argument differs from the chart")
    threads = observation.get("threads")
    if not isinstance(threads, list) or not threads or len(threads) > THREAD_LIMIT:
        raise RuntimeError("native runtime thread evidence is missing or exceeds bound")
    ids = set()
    workers: list[str] = []
    for row in threads:
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or row["id"] in ids
            or not isinstance(row.get("name"), str)
        ):
            raise RuntimeError("native runtime thread evidence is malformed")
        ids.add(row["id"])
        name = row["name"]
        if name.startswith(("reactor-http-ep", "reactor-http-ni")):
            workers.append(name)
    if len(workers) != expected_workers or (
        require_epoll
        and any(not name.startswith("reactor-http-ep") for name in workers)
    ):
        raise RuntimeError("native Reactor worker count or epoll transport differs")
