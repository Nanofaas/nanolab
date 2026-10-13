"""Typed, revision-aware boundary to versioned NanoFaaS one-shot APIs."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
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


def _check_configuration(settings: dict[str, JsonValue]) -> None:
    version = settings.get("schemaVersion")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported configuration version")
    if version == 2:
        _session_path(settings.get("sessionId"))


def _session_path(session: JsonValue) -> str:
    if not isinstance(session, str) or not session.strip() or len(session) > 256:
        raise ValueError("logical sessionId must contain 1..256 characters")
    return "/v1/admin/offload/one-shot/sessions/" + quote(session, safe="")


class RevisionSnapshot(ApiModel):
    """Configuration revision and the versioned settings accepted by the node."""

    revision: int = Field(ge=1)
    settings: dict[str, JsonValue]

    @model_validator(mode="after")
    def check_version(self) -> Self:
        """Enforce the schema version carried by settings."""
        _check_configuration(self.settings)
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

    schema_version: Literal[1, 2] = Field(alias="schemaVersion")
    session_id: str | None = Field(default=None, alias="sessionId")
    automatic_armed: bool = Field(default=False, alias="automaticArmed")
    state: str
    busy: bool
    clock_healthy: bool = Field(alias="clockHealthy")
    catalog_generations: dict[str, int] = Field(alias="catalogGenerations")
    peer_endpoints: list[PeerEndpoint] = Field(alias="peerEndpoints")
    local_endpoint: PeerEndpoint | dict[str, JsonValue] = Field(alias="localEndpoint")
    revision: int = Field(ge=0)
    active_plan: dict[str, JsonValue] = Field(alias="activePlan")

    @model_validator(mode="after")
    def check_session(self) -> Self:
        """Logical state always identifies its session; v1 archives need none."""
        if self.schema_version == 2:
            _session_path(self.session_id)
        return self


class AssignmentDiagnostic(ApiModel):
    """Local handshake observations; missing replies do not prove nondelivery."""

    observed_at_utc: datetime = Field(alias="observedAtUtc")
    monotonic_offset_seconds: float = Field(
        alias="monotonicOffsetSeconds", ge=0, allow_inf_nan=False
    )
    session_id: str | None = Field(alias="sessionId")
    node_id: str = Field(alias="nodeId")
    epoch: int = Field(ge=0)
    round: int = Field(ge=0)
    assignment_id: str = Field(alias="assignmentId")
    function: str
    version: str
    buyer_generation: int = Field(alias="buyerGeneration", ge=1)
    seller_generation: int = Field(alias="sellerGeneration", ge=1)
    buyer_id: str = Field(alias="buyerId")
    buyer_incarnation: str = Field(alias="buyerIncarnation")
    seller_id: str = Field(alias="sellerId")
    seller_incarnation: str = Field(alias="sellerIncarnation")
    negotiated_quantity: int = Field(alias="negotiatedQuantity", ge=0)
    confirmed_quantity: int = Field(alias="confirmedQuantity", ge=0)
    ready_received: bool = Field(alias="readyReceived")
    ack_sent: bool = Field(alias="ackSent")
    ack_received: bool = Field(alias="ackReceived")
    ack_response_received: bool = Field(alias="ackResponseReceived")
    reason: Literal[
        "CONFIRMED",
        "PARTIAL_READY",
        "TIMEOUT",
        "PEER_REFUSED",
        "TRANSPORT_ERROR",
        "READY_NOT_RECEIVED",
        "ACK_NOT_RECEIVED",
        "NOT_ATTEMPTED_TIME_WINDOW",
    ]
    residual_cloud_rate: float = Field(
        alias="residualCloudRate", ge=0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_quantities(self) -> Self:
        """Reject confirmations beyond the negotiated assignment quantity."""
        if self.confirmed_quantity > self.negotiated_quantity:
            raise ValueError("confirmed quantity exceeds negotiated assignment")
        if self.node_id not in {self.buyer_id, self.seller_id}:
            raise ValueError("diagnostic node is not an assignment endpoint")
        return self


class PlanActivation(ApiModel):
    """Prepare result, including readiness failures rather than just HTTP success."""

    status: Literal["NEGOTIATED", "PREPARED", "DEGRADED", "FAILED"]
    plan: dict[str, JsonValue] | None
    desired_replicas: dict[str, int] = Field(alias="desiredReplicas")
    ready_replicas: dict[str, int] = Field(alias="readyReplicas")
    reason: str | None
    prepared_at: datetime = Field(alias="preparedAt")
    diagnostics: list[AssignmentDiagnostic] = Field(default_factory=list)


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

    def __init__(
        self, base_url: str, *, http: httpx.Client, timeout_seconds: float = 30
    ) -> None:
        """Borrow an explicitly configured HTTP transport."""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("API timeout must be finite and positive")
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url.rstrip("/")
        self.http = http

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self.http.request(
            method, self.base_url + path, timeout=self.timeout_seconds, **kwargs
        )
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
        _check_configuration(settings)
        receipt = RevisionSnapshot.model_validate(
            self._request(
                "PUT",
                "/v1/admin/offload/one-shot/config",
                json=settings,
                headers={"If-Match": str(revision)},
            ).json()
        )
        if any(
            receipt.settings.get(key) != settings.get(key)
            for key in ("schemaVersion", "sessionId")
        ):
            raise ValueError("configuration receipt identity mismatch")
        return receipt

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

    def prepare_logical_epoch(
        self,
        session_id: str,
        epoch: int,
        *,
        starts_at: datetime,
        ends_at: datetime,
        activation_delay_seconds: float,
    ) -> PlanActivation:
        """Negotiate v2 without activating physical replicas or retrying a trigger."""
        path = _session_path(session_id)
        if epoch < 0 or ends_at <= starts_at:
            raise ValueError("invalid epoch window")
        if (
            not math.isfinite(activation_delay_seconds)
            or not 0 < activation_delay_seconds <= 86400
        ):
            raise ValueError("activation delay must be finite and in (0, 86400]")
        duration = f"{activation_delay_seconds:.9f}".rstrip("0").rstrip(".")
        return PlanActivation.model_validate(
            self._request(
                "POST",
                f"{path}/epochs/{epoch}/prepare",
                json={
                    "startsAt": starts_at.isoformat(),
                    "endsAt": ends_at.isoformat(),
                    "activationDelay": f"PT{duration}S",
                },
            ).json()
        )

    def activate_epoch(self, session_id: str, epoch: int) -> PlanActivation:
        """Activate an already negotiated logical epoch exactly once."""
        path = _session_path(session_id)
        if epoch < 0:
            raise ValueError("invalid epoch")
        return PlanActivation.model_validate(
            self._request("POST", f"{path}/epochs/{epoch}/activate").json()
        )

    def status(self) -> NodeStatus:
        """Read validated state without substituting stale data."""
        return NodeStatus.model_validate(
            self._request("GET", "/v1/admin/offload/one-shot/status").json()
        )

    def epoch_events(
        self, epoch: int, *, after: int = 0, limit: int = 100, max_pages: int = 100
    ) -> list[EventPage]:
        """Keep the complete-page list contract for existing qualification clients."""
        return list(
            self.iter_epoch_events(epoch, after=after, limit=limit, max_pages=max_pages)
        )

    def iter_epoch_events(
        self, epoch: int, *, after: int = 0, limit: int = 100, max_pages: int = 100
    ) -> Iterator[EventPage]:
        """Return all bounded pages and reject stalled cursors or truncation."""
        if epoch < 0 or after < 0 or not 1 <= limit <= 1000 or max_pages < 1:
            raise ValueError("invalid event pagination")
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
            yield page
            if len(page.entries) < limit:
                return
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
