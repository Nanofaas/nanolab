"""The switch step, against a fake platform that behaves like the real one.

The fake is deliberately unfriendly in the two ways the campaign measured: the
`scheduler` namespace GET carries no `revision`, and `scheduler_switch_total`
does not exist until the first switch has happened.
"""

from __future__ import annotations

import json
from threading import Event

import httpx
import pytest

from nanolab.tasks.soak.scheduler_switch import (
    FROZEN_BUDGETS,
    FrozenBudgets,
    SchedulerSwitchDriver,
    SwitchError,
    receipt_document,
)

STRATEGIES = ("per-function", "shared-queue")
BASE = "http://127.0.0.1:8080"
METRICS = "http://127.0.0.1:8081/actuator/prometheus"


class Platform:
    """The admin route and the exposition, with the platform's real semantics."""

    def __init__(self, available=STRATEGIES, strategy=None, pause_s=0.0016):
        self.available = list(available)
        self.strategy = strategy or available[0]
        self.revision = 0
        self.committed = 0
        self.pause_s = pause_s
        self.patches = 0
        self.refusals: set[str] = set()

    def envelope(self):
        return {
            "revision": self.revision,
            "namespaces": {"scheduler": self.namespace()},
        }

    def namespace(self):
        # No `revision` here, exactly as the platform serves it.
        return {
            "available": list(self.available),
            "strategy": self.strategy,
            "persistence": "restart",
        }

    def exposition(self):
        lines = [
            f'scheduler_active{{strategy="{name}"}} '
            f"{'1.0' if name == self.strategy else '0.0'}"
            for name in self.available
        ]
        # Registered by the first switch, not at composition time.
        if self.committed:
            lines.append(
                f'scheduler_switch_total{{outcome="committed"}} {self.committed}.0'
            )
        lines.append(f"scheduler_switch_duration_seconds_max {self.pause_s}")
        return "\n".join(lines) + "\n"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/actuator/prometheus":
            return httpx.Response(200, text=self.exposition())
        if path == "/v1/admin/runtime-config":
            return httpx.Response(200, json=self.envelope())
        if path == "/v1/admin/runtime-config/scheduler":
            if request.method == "PATCH":
                self.patches += 1
                body = json.loads(request.content)
                target = body["values"]["strategy"]
                if target in self.refusals:
                    return httpx.Response(503, json={"error": "refused"})
                if body["expectedRevision"] != self.revision:
                    return httpx.Response(409, json={"error": "revision mismatch"})
                if target not in self.available:
                    return httpx.Response(422, json={"error": "unknown strategy"})
                self.revision += 1
                self.strategy = target
                self.committed += 1
                return httpx.Response(200, json=self.namespace())
            return httpx.Response(200, json=self.namespace())
        raise AssertionError(f"unexpected request: {request.method} {path}")


class FakeClock:
    """Advance only when asked, so pacing is deterministic and instant."""

    def __init__(self):
        self.now = 0.0
        self.waits = 0

    def monotonic(self):
        return self.now

    def wait_until(self, deadline_s, cancelled):
        self.waits += 1
        self.now = deadline_s
        return not cancelled.is_set()


def driver(platform, clock, **budget_changes):
    budgets = FrozenBudgets(**budget_changes)
    return SchedulerSwitchDriver(
        BASE,
        METRICS,
        STRATEGIES,
        clock=clock,
        budgets=budgets,
        transport=httpx.MockTransport(platform.handle),
    )


def small(**changes):
    return {"switches_in_soak": 5, **changes}


def test_the_run_alternates_and_restores_the_selection_it_found():
    """An odd count leaves the run on the other strategy, so it is restored."""
    platform = Platform()
    receipt = driver(platform, FakeClock(), **small()).run(
        window_s=5, cancelled=Event()
    )

    assert receipt.initial == "per-function"
    assert receipt.final == "per-function"
    assert receipt.committed == 5
    assert receipt.stale == 0
    assert receipt.refused == 0
    assert receipt.restores == 1
    # Five switches plus the restore, alternating strictly: per-function,
    # shared-queue, per-function, shared-queue, per-function, then back.
    assert platform.patches == 6
    assert platform.committed == 6
    assert receipt.platform_committed == 6.0
    assert platform.strategy == "per-function"


def test_an_even_count_needs_no_restore_because_it_lands_back():
    platform = Platform()
    receipt = driver(platform, FakeClock(), switches_in_soak=4).run(
        window_s=4, cancelled=Event()
    )

    assert receipt.committed == 4
    assert receipt.restores == 0
    assert platform.patches == 4
    assert receipt.final == receipt.initial == "per-function"


def test_the_revision_comes_from_the_root_envelope():
    """The namespace GET has no revision, so a driver reading one there fails."""
    platform = Platform()
    assert "revision" not in platform.namespace()
    driver(platform, FakeClock(), **small()).run(window_s=5, cancelled=Event())
    # Every PATCH carried the root revision and was accepted: a stale read would
    # have produced a 409 and left the count short.
    assert platform.committed == 6


def test_a_window_that_runs_out_fails_on_the_frozen_count():
    """A window the switches do not fit in is a short count, not a burst."""
    platform = Platform()

    class TooSlow(FakeClock):
        def wait_until(self, deadline_s, cancelled):
            self.waits += 1
            self.now += 5.0  # one switch consumes the whole window
            return not cancelled.is_set()

    with pytest.raises(SwitchError, match=r"1 switches committed, budget 5"):
        driver(platform, TooSlow(), **small()).run(window_s=5, cancelled=Event())


def test_a_pause_over_the_frozen_budget_fails_the_run():
    platform = Platform(pause_s=0.2500001)
    with pytest.raises(SwitchError, match=r"worst switch pause .* budget 250.0 ms"):
        driver(platform, FakeClock(), **small()).run(window_s=5, cancelled=Event())


def test_a_pause_at_the_budget_passes():
    platform = Platform(pause_s=0.25)
    receipt = driver(platform, FakeClock(), **small()).run(
        window_s=5, cancelled=Event()
    )
    assert receipt.pause_max_ms == 250.0


def test_an_artifact_without_a_queue_module_is_refused_by_name():
    """One strategy indexed means no alternation, whatever the flag says."""
    platform = Platform(available=("per-function",))
    with pytest.raises(SwitchError, match=r"does not serve \['shared-queue'\]"):
        driver(platform, FakeClock(), **small()).run(window_s=5, cancelled=Event())


def test_more_live_indexes_than_the_budget_are_refused_before_switching():
    platform = Platform(available=("per-function", "shared-queue", "third"))
    with pytest.raises(SwitchError, match="3 live strategy indexes"):
        driver(platform, FakeClock(), **small()).run(window_s=5, cancelled=Event())
    assert platform.patches == 0


def test_a_refused_patch_is_counted_and_not_hidden():
    platform = Platform()
    platform.refusals = {"shared-queue"}
    with pytest.raises(SwitchError, match=r"0 switches committed, budget 5"):
        driver(platform, FakeClock(), **small()).run(window_s=5, cancelled=Event())
    assert platform.committed == 0
    assert platform.patches == 5


def test_a_platform_that_stops_answering_the_meters_fails_rather_than_reads_zero():
    platform = Platform()
    original = platform.exposition
    platform.exposition = lambda: 'scheduler_active{strategy="per-function"} 1.0\n'
    with pytest.raises(SwitchError, match="published no switch reading"):
        driver(platform, FakeClock(), **small()).run(window_s=5, cancelled=Event())
    platform.exposition = original


def test_the_frozen_budgets_are_the_campaigns_cited_numbers():
    assert FROZEN_BUDGETS.switches_in_soak == 1000
    assert FROZEN_BUDGETS.max_switch_pause_ms == 250
    assert FROZEN_BUDGETS.max_switch_pause_p99_ms == 100
    assert FROZEN_BUDGETS.max_live_strategy_indexes == 2


def test_the_receipt_records_the_window_the_switches_happened_over():
    """A count without its window would read the same for an hour and a minute.

    Nothing floors a soak's steady phase, so the artifact has to answer the
    campaign's criterion itself: a thousand switches *and* how long that took.
    """
    platform = Platform()
    receipt = driver(platform, FakeClock(), **small()).run(
        window_s=5, cancelled=Event()
    )

    assert receipt.window_s == 5.0
    assert 0 < receipt.elapsed_s <= receipt.window_s


def test_the_receipt_document_is_what_the_manifest_references():
    platform = Platform()
    receipt = driver(platform, FakeClock(), **small()).run(
        window_s=5, cancelled=Event()
    )
    document = receipt_document(receipt)

    assert document["schema"] == "nanolab-soak-v1"
    assert document["kind"] == "scheduler-switch"
    assert document["committed"] == 5
    assert document["window_s"] == 5.0
    assert document["live_indexes"] == 2
    assert document["platform_committed"] == 6.0
    assert document["budgets"]["switches_in_soak"] == 1000
    # JSON-ready, because the acceptance manifest encodes it.
    assert json.loads(json.dumps(document))["committed"] == 5


def test_two_identical_strategies_are_not_a_switch():
    with pytest.raises(ValueError, match="two distinct strategies"):
        SchedulerSwitchDriver(
            BASE,
            METRICS,
            ("per-function", "per-function"),
            clock=FakeClock(),
        )
