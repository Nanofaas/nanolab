"""Bounded local-Linux Docker/procfs/HTTP helper, run in a killable child.

Remote daemons and processes outside the container init PID are unsupported.
Failure to prove local procfs ownership makes that source unavailable.
"""

import http.client
import json
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

_MAX_BODY = 1024 * 1024
_MAX_OUTPUT = 4 * 1024 * 1024


def read_bounded(stream: BinaryIO, limit: int = _MAX_BODY) -> bytes:
    """Read at most ``limit`` bytes, failing rather than truncating silently."""
    value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("collection body limit exceeded")
    return value


def _read_proc(path: Path) -> str:
    with path.open("rb") as stream:
        return read_bounded(stream).decode("utf-8")


def _start_ticks(stat: str) -> str:
    closing = stat.rfind(")")
    fields = stat[closing + 1 :].split()
    if closing < 0 or len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("invalid procfs process identity")
    return fields[19]


def collect_procfs(
    pid: int, container_id: str, root: Path = Path("/proc")
) -> dict[str, Any]:
    """Read one process's RSS/PSS and identity, keeping the two distinct.

    Fields that cannot be read stay absent rather than becoming zero.
    """
    if (
        type(pid) is not int
        or pid <= 0
        or not re.fullmatch(r"[a-f0-9]{64}", container_id)
    ):
        raise ValueError("invalid procfs target")
    directory = root / str(pid)
    before_cgroup = _read_proc(directory / "cgroup")
    if container_id not in before_cgroup:
        raise ValueError("cannot prove local procfs container ownership")
    before = _start_ticks(_read_proc(directory / "stat"))
    status = _read_proc(directory / "status")
    try:
        smaps = _read_proc(directory / "smaps_rollup")
    except (FileNotFoundError, PermissionError):
        smaps = None
    after = _start_ticks(_read_proc(directory / "stat"))
    if before != after or _read_proc(directory / "cgroup") != before_cgroup:
        raise ValueError("procfs process identity changed during collection")
    return {"status": status, "smaps_rollup": smaps, "start_ticks": before}


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _docker_get(path: str, docker_socket: str, timeout: float) -> dict[str, Any]:
    connection = _UnixConnection(docker_socket, timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"Docker API returned HTTP {response.status}")
        value = json.loads(read_bounded(response))
        if not isinstance(value, dict):
            raise ValueError("Docker API response must be an object")
        return value
    finally:
        connection.close()


def _identity(container_id: str, docker_socket: str, remaining) -> dict[str, Any]:
    container = _docker_get(
        f"/containers/{container_id}/json", docker_socket, remaining()
    )
    image_id = urllib.parse.quote(container["Image"], safe="")
    image = _docker_get(f"/images/{image_id}/json", docker_socket, remaining())
    state = container["State"]
    return {
        "container_id": container["Id"],
        "process_id": state["Pid"],
        "process_started_at": state["StartedAt"],
        "running": state["Running"],
        "image_digests": image.get("RepoDigests", []),
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("metrics endpoint redirects are unsupported")


def collect_http(endpoint: str, timeout: float) -> str:
    """Fetch one bounded exposition, with redirects and proxies disabled."""
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("metrics endpoint must use HTTP(S)")
    # Local measurements must not be silently routed through environment proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        endpoint, headers={"Accept": "text/plain; version=0.0.4"}
    )
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"metrics endpoint returned HTTP {response.status}")
        return read_bounded(response).decode("utf-8")


def collect(request: dict[str, Any]) -> dict[str, Any]:
    """Collect one target's procfs, cgroup and exposition readings.

    The container's identity is verified before and after, so a restart
    invalidates the scrape instead of silently attributing it elsewhere.
    """
    target = request["target"]
    container_id = target["container_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        raise ValueError("full immutable container ID required")
    deadline = time.monotonic() + float(request["timeout_s"])

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("collection deadline exceeded")
        return value

    docker_socket = request["docker_socket"]
    result: dict[str, Any] = {"errors": {}}
    operations = (
        ("before", lambda: _identity(container_id, docker_socket, remaining)),
        ("procfs", lambda: collect_procfs(target["process_id"], container_id)),
        (
            "stats",
            lambda: _docker_get(
                f"/containers/{container_id}/stats?stream=false&one-shot=true",
                docker_socket,
                remaining(),
            ),
        ),
        ("exposition", lambda: collect_http(request["endpoint"], remaining())),
        ("after", lambda: _identity(container_id, docker_socket, remaining)),
    )
    for name, operation in operations:
        try:
            remaining()
            if name == "exposition" and request.get("endpoint") is None:
                raise ValueError("no metrics endpoint configured")
            result[name] = operation()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            http.client.HTTPException,
        ) as error:
            result["errors"][name] = f"{type(error).__name__}: {str(error)[:512]}"
    return result


def main() -> int:
    """Run one collection from argv and write its bounded JSON reply."""
    try:
        result = collect(json.loads(sys.argv[1]))
        encoded = json.dumps(result, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_OUTPUT:
            raise ValueError("collector output limit exceeded")
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception as error:
        sys.stderr.write(f"{type(error).__name__}: {str(error)[:512]}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
