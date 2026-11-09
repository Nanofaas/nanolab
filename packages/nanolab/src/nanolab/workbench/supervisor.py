"""Client for the workbench supervisor's HTTP API (signals only)."""

import json
import urllib.request
from collections.abc import Mapping
from typing import Any


class SupervisorClient:
    """PUT/DELETE targets and read `GET /status`; every failure is an OSError."""

    def __init__(self, url: str, *, timeout_seconds: float, opener: Any = None):
        """Bound each call by `timeout_seconds` (a socket timeout, per operation)."""
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"supervisor url must be http(s): {url!r}")
        self.url = url.rstrip("/")
        self.timeout = timeout_seconds
        self.opener = opener or urllib.request.build_opener()

    def _call(self, method: str, path: str, body: object = None) -> bytes:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read()

    def put(
        self,
        name: str,
        kind: str,
        params: Mapping[str, Any],
        tolerated_seconds: float,
        grace_seconds: float | None = None,
    ) -> None:
        """Add or replace a target."""
        body: dict[str, Any] = {
            "kind": kind,
            "params": dict(params),
            "toleratedSeconds": tolerated_seconds,
        }
        if grace_seconds is not None:
            body["graceSeconds"] = grace_seconds
        self._call("PUT", f"/targets/{name}", body)

    def delete(self, name: str) -> None:
        """Remove a target; absent is fine."""
        self._call("DELETE", f"/targets/{name}")

    def status(self) -> dict[str, Any]:
        """Return the `GET /status` document."""
        return json.loads(self._call("GET", "/status"))
