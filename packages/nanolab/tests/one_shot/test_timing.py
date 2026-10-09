import pytest

from nanolab.tasks.one_shot.timing import WallTimingSample, select_timing


def select(samples, **overrides):
    options = {
        "period_candidates": [60, 240, 300],
        "max_trace_resolution": 300,
        "minimum_samples": 10,
        "quantile": 0.9,
        "margin_seconds": 2,
        "ready_margin_seconds": 1,
        "epsilon": 0.05,
        "protocol_budget_seconds": 0.5,
        "provider": "multipass",
        "purpose": "workflow-validation",
        "fingerprint": "sha256:" + "a" * 64,
        "profile_sha256": "b" * 64,
    }
    return select_timing(samples, **{**options, **overrides})


def test_dimensioning_twelve_seconds_requires_two_hundred_forty():
    samples = [
        WallTimingSample(auction_seconds=10, ready_seconds=12) for _ in range(10)
    ]
    result = select(samples)
    assert result.qualified
    assert result.period_seconds == 240
    assert result.lead_seconds == 13
    assert not select(samples, period_candidates=[60]).qualified


@pytest.mark.parametrize(
    "samples",
    [
        [WallTimingSample(auction_seconds=None, ready_seconds=None, censored=True)]
        * 10,
        [WallTimingSample(auction_seconds=0.1, ready_seconds=0.2)] * 3,
        [WallTimingSample(auction_seconds=0.1, ready_seconds=0.2)] * 9
        + [WallTimingSample(auction_seconds=1, ready_seconds=1, censored=True)],
    ],
)
def test_censorship_or_insufficient_samples_never_qualifies(samples):
    assert not select(samples).qualified


def test_parallel_cpu_time_does_not_change_auction_wall_budget():
    measured = [
        WallTimingSample(auction_seconds=10, ready_seconds=12, node_cpu_seconds=[1, 1])
    ] * 10
    inflated = [
        WallTimingSample(
            auction_seconds=10, ready_seconds=12, node_cpu_seconds=[1000, 1000]
        )
    ] * 10
    assert select(measured) == select(inflated)


def test_trace_resolution_and_protocol_budget_are_also_binding():
    samples = [WallTimingSample(auction_seconds=0.1, ready_seconds=0.2)] * 10
    assert not select(samples, max_trace_resolution=5).qualified
    result = select(samples, protocol_budget_seconds=13)
    assert result.period_seconds == 300


def test_non_finite_or_negative_wall_times_are_rejected():
    with pytest.raises(ValueError, match="finite"):
        select([WallTimingSample(auction_seconds=float("nan"), ready_seconds=1)] * 10)


def test_ambiguous_prepare_is_not_retried_and_keeps_censored_raw_evidence(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import httpx

    from nanolab.tasks.one_shot.qualification import prepare_parallel_epoch

    attempts = []

    def prepare(epoch, **kwargs):
        attempts.append(epoch)
        raise httpx.ReadTimeout("ambiguous trigger")

    clients = {
        key: SimpleNamespace(
            base_url="http://" + key,
            http=object(),
            prepare_epoch=prepare,
            epoch_events=lambda _: [],
        )
        for key in ("edge-0", "edge-1")
    }
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.qualification.scrape_metrics",
        lambda *args: {},
    )
    now = datetime.now(UTC)
    sample, observed = prepare_parallel_epoch(
        clients,  # pyright: ignore[reportArgumentType]
        epoch=7,
        starts_at=now + timedelta(seconds=3),
        ends_at=now + timedelta(seconds=23),
        output=tmp_path / "timing.jsonl",
    )
    assert attempts == [7, 7]
    assert sample.censored and sample.auction_seconds is None
    assert all(not row["complete"] for row in observed["nodes"].values())
    assert '"ambiguous trigger"' in (tmp_path / "timing.jsonl").read_text()


def test_unqualified_timing_retains_null_durations():
    result = select(
        [WallTimingSample(auction_seconds=None, ready_seconds=None, censored=True)] * 10
    )
    assert result.samples_seconds == [None] * 10
    assert result.censored_count == 10


def test_configuration_refusal_preserves_structured_api_diagnosis(tmp_path):
    import httpx

    from nanolab.one_shot.client import NanoFaasOneShotClient
    from nanolab.tasks.one_shot.qualification import configure_node

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            400,
            json={
                "error": "INVALID_ONE_SHOT_INPUT",
                "message": "incompatible resource",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = NanoFaasOneShotClient("http://edge", http=http)
        with pytest.raises(httpx.HTTPStatusError, match="400"):
            configure_node(
                client, {"schemaVersion": 1}, revision=0, output=tmp_path / "api.jsonl"
            )
    rows = (tmp_path / "api.jsonl").read_text()
    assert "incompatible resource" in rows
    assert len(calls) == 1


def test_imported_qualification_cannot_understate_measured_wall_time():
    from nanolab.one_shot.models import TimingQualification

    result = select([WallTimingSample(auction_seconds=10, ready_seconds=12)] * 10)
    altered = {**result.model_dump(), "quantile_seconds": 1}
    with pytest.raises(ValueError, match="understates"):
        TimingQualification.model_validate(altered)


def test_imported_qualification_cannot_claim_unresolved_tail_quantile():
    from nanolab.one_shot.models import TimingQualification

    result = select([WallTimingSample(auction_seconds=10, ready_seconds=12)] * 10)
    altered = {**result.model_dump(), "quantile": 0.99}
    with pytest.raises(ValueError, match="few samples"):
        TimingQualification.model_validate(altered)
