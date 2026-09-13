"""Persist function ownership and require positive evidence of deletion."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sonata_engine import Resource, TaskInputs

from nanolab.tasks.soak.retention import CleanupState

SCHEMA = "nanolab-soak-owned-function-v2"
StatusRequest = Callable[[str, str, float], int]


@dataclass(frozen=True)
class FunctionOwnership:
    """Bind a function to the exact local platform retained by one run."""

    name: str
    api_endpoint: str
    control_plane_image: str
    project_name: str
    cwd: str
    control_plane_container_id: str
    control_plane_started_at: str

    def __post_init__(self) -> None:
        """Reject ambiguous paths, identities and nonlocal API addresses."""
        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", self.name) is None
        ):
            raise ValueError("invalid owned function name")
        if not isinstance(self.api_endpoint, str):
            raise ValueError("owned API endpoint must be a string")
        endpoint = urlsplit(self.api_endpoint)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {"127.0.0.1", "::1"}
            or endpoint.port is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("owned API endpoint requires an explicit loopback port")
        if (
            not isinstance(self.control_plane_image, str)
            or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", self.control_plane_image)
            is None
        ):
            raise ValueError("owned control plane requires a frozen image digest")
        if (
            not isinstance(self.project_name, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", self.project_name) is None
        ):
            raise ValueError("invalid owned Compose project name")
        if not isinstance(self.cwd, str) or not Path(self.cwd).is_absolute():
            raise ValueError("owned execution directory must be absolute")
        if (
            not isinstance(self.control_plane_container_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.control_plane_container_id) is None
        ):
            raise ValueError("owned control plane requires its full container ID")
        if (
            not isinstance(self.control_plane_started_at, str)
            or re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z",
                self.control_plane_started_at,
            )
            is None
            or self.control_plane_started_at.startswith("0001-")
        ):
            raise ValueError("owned control plane requires its actual start timestamp")

    def record(self) -> dict[str, str]:
        """Return only JSON primitives, suitable for Sonata's retention journal."""
        return {
            "schema": SCHEMA,
            "name": self.name,
            "api_endpoint": self.api_endpoint,
            "control_plane_image": self.control_plane_image,
            "project_name": self.project_name,
            "cwd": self.cwd,
            "control_plane_container_id": self.control_plane_container_id,
            "control_plane_started_at": self.control_plane_started_at,
        }

    @classmethod
    def from_record(cls, value: object) -> "FunctionOwnership":
        """Restore a strictly shaped ownership record without network access."""
        keys = {
            "schema",
            "name",
            "api_endpoint",
            "control_plane_image",
            "project_name",
            "cwd",
            "control_plane_container_id",
            "control_plane_started_at",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("invalid owned function record fields")
        if value["schema"] != SCHEMA:
            raise ValueError("unsupported owned function record schema")
        return cls(**{key: item for key, item in value.items() if key != "schema"})


def _http_status(method: str, url: str, timeout_s: float) -> int:
    endpoint = urlsplit(url)
    assert endpoint.hostname is not None
    connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=timeout_s)
    try:
        connection.request(method, endpoint.path, headers={"Connection": "close"})
        response = connection.getresponse()
        # The status suffices. Do not buffer an unbounded response or follow
        # redirects into an endpoint outside this owned platform.
        return response.status
    finally:
        connection.close()


def delete_owned_function(
    ownership: FunctionOwnership,
    *,
    request: StatusRequest | None = None,
    timeout_s: float = 10.0,
    verify_owner: Callable[[FunctionOwnership], None],
) -> None:
    """Require absence after DELETE; connection failures never mean success.

    ``verify_owner`` must inspect the saved container ID, start timestamp,
    project, image and published endpoint. Matching the start timestamp rejects
    CP recreation/restart, where a 404 cannot prove catalog continuity. Runtime
    recovery must provide a stronger authoritative proof to handle that case.
    """
    request = request or _http_status
    verify_owner(ownership)
    url = ownership.api_endpoint.rstrip("/") + "/v1/functions/" + ownership.name
    status = request("DELETE", url, timeout_s)
    if type(status) is not int or status not in {204, 404}:
        raise RuntimeError(f"owned function DELETE did not succeed: HTTP {status}")
    absent = request("GET", url, timeout_s)
    if type(absent) is not int or absent != 404:
        raise RuntimeError(
            f"owned function removal is unconfirmed: subsequent GET returned {absent}"
        )
    verify_owner(ownership)


def journaled_function_resource(
    resource: Resource[Any],
    *,
    ownership: FunctionOwnership | Callable[[], FunctionOwnership],
    cleanup_state: CleanupState,
    verify_owner: Callable[[FunctionOwnership], None],
    request: StatusRequest | None = None,
    timeout_s: float = 10.0,
) -> Resource[dict[str, str]]:
    """Persist a replayable function identity and replace permissive deletion."""
    expected = ownership.record() if isinstance(ownership, FunctionOwnership) else None

    def acquire(inputs: TaskInputs) -> dict[str, str]:
        nonlocal expected
        identity = ownership() if callable(ownership) else ownership
        expected = identity.record()
        cleanup_state.prepare()
        try:
            verify_owner(identity)
            resource.acquire(inputs)
        except BaseException as error:
            cleanup_state.unknown_acquisition(resource.title, error)
            raise
        cleanup_state.retain(resource.title, expected)
        return dict(expected)

    def release(inputs: TaskInputs, value: dict[str, str]) -> None:
        try:
            if value != expected:
                raise ValueError(
                    "retained function identity differs from owned function"
                )
            delete_owned_function(
                FunctionOwnership.from_record(value),
                request=request,
                timeout_s=timeout_s,
                verify_owner=verify_owner,
            )
            cleanup_state.released(resource.title)
        except BaseException:
            cleanup_state.failed = True
            raise

    return Resource(
        title=resource.title,
        acquire=acquire,
        release=release,
        requires=resource.requires,
        always_release=False,
    )
