"""Typed, revision-aware boundary to the frozen NanoFaaS v1 API."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from nanolab.one_shot.contracts import validate_contract
from nanolab.one_shot.models import CalibrationProfile


class ApiModel(BaseModel):
    """Reject additions to the pinned API contract instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RevisionSnapshot(ApiModel):
    """Configuration revision and the versioned settings accepted by the node."""

    revision: int = Field(ge=1)
    settings: dict[str, JsonValue]

    @model_validator(mode="after")
    def check_version(self) -> Self:
        """Enforce the schema version carried by settings."""
        if self.settings.get("schemaVersion") != 1:
            raise ValueError("unsupported configuration version")
        return self


class StoredProfile(ApiModel):
    """Exact identity and revision of a stored calibration."""

    revision: int = Field(ge=1)
    content_hash: str = Field(alias="contentHash", pattern=r"^sha256:[0-9a-f]{64}$")
    profile: CalibrationProfile
    bytes: int = Field(gt=0)


class ForecastSummary(ApiModel):
    """Receipt for the versioned oracle trace."""

    revision: int = Field(ge=1)
    entry_count: int = Field(alias="entryCount", ge=0)
    node_id: str = Field(alias="nodeId")
    produced_at: datetime = Field(alias="producedAt")


class PeerEndpoint(ApiModel):
    """Invocation identity learned through the node's own P2P discovery."""

    peer_id: str = Field(alias="peerId")
    incarnation: str
    invocation_uri: str = Field(alias="invocationUri")


class NodeStatus(ApiModel):
    """Versioned node state; an empty endpoint/plan means not yet available."""

    schema_version: Literal[1] = Field(alias="schemaVersion")
    state: str
    busy: bool
    clock_healthy: bool = Field(alias="clockHealthy")
    catalog_generations: dict[str, int] = Field(alias="catalogGenerations")
    peer_endpoints: list[PeerEndpoint] = Field(alias="peerEndpoints")
    local_endpoint: PeerEndpoint | dict[str, JsonValue] = Field(alias="localEndpoint")
    revision: int = Field(ge=0)
    active_plan: dict[str, JsonValue] = Field(alias="activePlan")


class PlanActivation(ApiModel):
    """Prepare result, including readiness failures rather than just HTTP success."""

    status: Literal["PREPARED", "DEGRADED", "FAILED"]
    plan: dict[str, JsonValue] | None
    desired_replicas: dict[str, int] = Field(alias="desiredReplicas")
    ready_replicas: dict[str, int] = Field(alias="readyReplicas")
    reason: str | None
    prepared_at: datetime = Field(alias="preparedAt")


class EventEntry(ApiModel):
    """Cursor plus an event checked against the frozen run-event schema."""

    cursor: int = Field(gt=0)
    event: dict[str, JsonValue]
    operational_nanos: int = Field(alias="operationalNanos", ge=0)

    @model_validator(mode="after")
    def check_event(self) -> Self:
        """Validate every event before exposing it to analysis."""
        validate_contract("run-events", self.event)
        return self


class EventPage(ApiModel):
    """Bounded page with its server-side serialization hash."""

    schema_version: Literal[1] = Field(alias="schemaVersion")
    content_hash: str = Field(alias="contentHash", pattern=r"^sha256:[0-9a-f]{64}$")
    entries: list[EventEntry]


class ClockReceipt(ApiModel):
    """Whether a measured clock sample satisfies the node's threshold."""

    healthy: bool


class OneShotConflictError(RuntimeError):
    """The supplied revision lost a race; the client must not overwrite it."""


class NanoFaasOneShotClient:
    """No implicit retries: transport, API refusal and invalid data stay distinct."""

    def __init__(self, base_url: str, *, http: httpx.Client) -> None:
        """Borrow an explicitly configured HTTP transport."""
        self.base_url = base_url.rstrip("/")
        self.http = http

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self.http.request(method, self.base_url + path, timeout=30, **kwargs)
        if response.status_code == 409:
            raise OneShotConflictError(f"revision conflict at {path}")
        response.raise_for_status()
        return response

    def load_profile(
        self, profile: CalibrationProfile, *, revision: int
    ) -> StoredProfile:
        """Hash the actual wire bytes and check the receipt identity."""
        raw = profile.model_dump_json(by_alias=True).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        response = self._request(
            "PUT",
            "/v1/admin/offload/one-shot/profiles/" + quote(profile.profile_id, safe=""),
            content=raw,
            headers={
                "If-Match": str(revision),
                "X-Content-SHA256": digest,
                "Content-Type": "application/json",
            },
        )
        stored = StoredProfile.model_validate(response.json())
        if (
            stored.content_hash != digest
            or stored.profile != profile
            or stored.bytes != len(raw)
        ):
            raise ValueError("profile receipt identity mismatch")
        return stored

    def load_trace(
        self, trace: dict[str, JsonValue], *, revision: int
    ) -> ForecastSummary:
        """Validate the frozen forecast before sending a conditional update."""
        validate_contract("forecast", trace)
        return ForecastSummary.model_validate(
            self._request(
                "PUT",
                "/v1/admin/forecasting/trace",
                json=trace,
                headers={"If-Match": str(revision)},
            ).json()
        )

    def configure(
        self, settings: dict[str, JsonValue], *, revision: int
    ) -> RevisionSnapshot:
        """Send a versioned next-epoch configuration exactly once."""
        if settings.get("schemaVersion") != 1:
            raise ValueError("unsupported configuration version")
        return RevisionSnapshot.model_validate(
            self._request(
                "PUT",
                "/v1/admin/offload/one-shot/config",
                json=settings,
                headers={"If-Match": str(revision)},
            ).json()
        )

    def prepare_epoch(
        self, epoch: int, *, starts_at: datetime, ends_at: datetime
    ) -> PlanActivation:
        """Trigger one epoch; never repeat a trigger after an ambiguous timeout."""
        if epoch < 0 or ends_at <= starts_at:
            raise ValueError("invalid epoch window")
        return PlanActivation.model_validate(
            self._request(
                "POST",
                f"/v1/admin/offload/one-shot/epochs/{epoch}/prepare",
                json={"startsAt": starts_at.isoformat(), "endsAt": ends_at.isoformat()},
            ).json()
        )

    def status(self) -> NodeStatus:
        """Read validated state without substituting stale data."""
        return NodeStatus.model_validate(
            self._request("GET", "/v1/admin/offload/one-shot/status").json()
        )

    def epoch_events(
        self, epoch: int, *, after: int = 0, limit: int = 100, max_pages: int = 100
    ) -> list[EventPage]:
        """Return all bounded pages and reject stalled cursors or truncation."""
        if epoch < 0 or after < 0 or not 1 <= limit <= 1000 or max_pages < 1:
            raise ValueError("invalid event pagination")
        pages: list[EventPage] = []
        for _ in range(max_pages):
            page = EventPage.model_validate(
                self._request(
                    "GET",
                    f"/v1/admin/offload/one-shot/epochs/{epoch}/events",
                    params={"after": after, "limit": limit},
                ).json()
            )
            for entry in page.entries:
                if entry.cursor <= after or entry.event["epoch"] != epoch:
                    raise ValueError("invalid event cursor or epoch")
                after = entry.cursor
            pages.append(page)
            if len(page.entries) < limit:
                return pages
        raise ValueError("event collection exceeded page budget; evidence truncated")

    def update_clock_health(
        self, *, offset_seconds: float, measured_at: datetime
    ) -> ClockReceipt:
        """Upload a measured offset in ISO-8601 duration units."""
        sign = "-" if offset_seconds < 0 else ""
        return ClockReceipt.model_validate(
            self._request(
                "PUT",
                "/v1/admin/offload/one-shot/clock-health",
                json={
                    "offset": f"{sign}PT{abs(offset_seconds):.9f}S",
                    "measuredAt": measured_at.isoformat(),
                },
            ).json()
        )

    def drain_and_release(self) -> bool:
        """Release node-owned replicas before VM teardown."""
        result = self._request(
            "POST", "/v1/admin/offload/one-shot/drain-and-release"
        ).json()
        if not isinstance(result, bool):
            raise ValueError("invalid drain receipt")
        return result
