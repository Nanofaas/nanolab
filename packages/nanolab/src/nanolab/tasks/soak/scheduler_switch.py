"""The manual hot switch, driven under a soak's own load.

`docs/experiments/scheduler-switching-2026-09/NANOLAB.md` in the nanoFaaS
checkout records the procedure as it was actually run against a container stack:
read the revision from the **root** envelope, PATCH the `scheduler` namespace to
the other strategy carrying that revision, read the namespace back and assert the
engine's own selection — never the PATCH body — then hold the platform's own
meters against the switches that were made. This module is that procedure,
unchanged; the load it runs under is the soak's own generator, so nothing here
drives traffic.

Two things it does *not* do, both because the soak cannot survive them:

* It does not restart the control plane. `RoleBoundProbe` verifies the observed
  process's `process_id` and `process_started_at` on both sides of every scrape,
  so a restart makes every later sample unavailable and the run INCONCLUSIVE.
  The switch step moves a knob the platform already documents as not surviving a
  restart; proving that belongs on a stack where the restart is the subject, as
  it already is in the procedure's own §5.5.
* It does not own the invocation polling or the arrival counts. Those are the
  workload driver's, and duplicating them would be a second copy of the load.

The three meters it reads back cannot be answered by the snapshot catalogue:
`SchedulerConfiguration` registers them behind
`@ConditionalOnBean(SchedulingStrategy.class)`, and only the queue modules publish
one, so on a build without them the series do not exist at all. Read from the
control plane's exposition here, they are the *platform's* account of the
switches this driver made, not the driver's own claim about itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Any

import httpx

from nanolab.tasks.soak.ports import Clock
from nanolab.tasks.soak.probes import parse_exposition

# The admin route and the namespace it serves, as spelled by the platform.
ROOT_PATH = "/v1/admin/runtime-config"
SCHEDULER_NAMESPACE = "scheduler"
SCHEDULER_PATH = f"{ROOT_PATH}/{SCHEDULER_NAMESPACE}"

# The names Prometheus serves for the three meters, which are not the names the
# code registers for the timer: Micrometer appends the unit.
ACTIVE_METER = "scheduler_active"
SWITCH_METER = "scheduler_switch_total"
PAUSE_METER = "scheduler_switch_duration_seconds_max"
COMMITTED_OUTCOME = "committed"


class SwitchError(RuntimeError):
    """The step could not be carried out to the budget it is held to."""


@dataclass(frozen=True, slots=True)
class FrozenBudgets:
    """The campaign's budgets, cited rather than chosen.

    From `docs/experiments/scheduler-switching-2026-09/budgets.json`, frozen by
    the campaign before the run. They are constants here and not scenario fields
    on purpose: a soak that fails one has found a regression against a number
    decided in advance, and a soak that was free to restate one would have found
    nothing.

    The switch count and the index ceiling are enforced by this step, which fails
    the run, rather than by a `Criterion` row. Neither is expressible in
    `nanolab.config.soak`'s vocabulary: every `MetricOperation` is an upper bound,
    an equality, or a comparison against a baseline, and "at least a thousand"
    and "at most two series of this name" are none of those. See the report
    beside the task brief.
    """

    switches_in_soak: int = 1000
    max_switch_pause_ms: float = 250
    # Named and carried although nothing measures it: the platform publishes no
    # percentile histogram for the switch timer under any metrics profile, so
    # the p99 this budget names has no series behind it. Kept so the omission is
    # a recorded fact rather than an oversight.
    max_switch_pause_p99_ms: float = 100
    max_live_strategy_indexes: int = 2


FROZEN_BUDGETS = FrozenBudgets()


@dataclass(frozen=True, slots=True)
class SwitchReceipt:
    """What the step did, for the run's manifest and for the operator."""

    strategies: tuple[str, ...]
    initial: str
    final: str
    committed: int
    refused: int
    stale: int
    live_indexes: int
    pause_max_ms: float | None
    platform_committed: float | None
    restores: int


def _series(
    rows: tuple[tuple[str, tuple[tuple[str, str], ...], float], ...],
    name: str,
    **labels: str,
) -> list[float]:
    """Select one meter's samples, by name and any labels the caller pins."""
    return [
        value
        for metric, row_labels, value in rows
        if metric == name
        and all(dict(row_labels).get(key) == item for key, item in labels.items())
    ]


class SchedulerSwitchDriver:
    """Alternate the scheduler's strategy, and hold the platform to its budgets.

    One instance drives one phase. It is synchronous and owns its own HTTP
    client: the caller runs it beside the workload driver rather than inside it,
    so nothing here shares a connection pool with the load or with the observer.
    """

    def __init__(
        self,
        base_url: str,
        metrics_url: str,
        strategies: tuple[str, str],
        *,
        clock: Clock,
        budgets: FrozenBudgets = FROZEN_BUDGETS,
        timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Bind the endpoints, the pair to alternate and the clock to pace by."""
        if len(set(strategies)) != 2 or not all(strategies):
            raise ValueError("the switch alternates between two distinct strategies")
        if not base_url or not metrics_url:
            raise ValueError("the switch needs the admin route and the exposition")
        self._base = base_url.rstrip("/")
        self._metrics = metrics_url
        self._strategies = tuple(strategies)
        self._clock = clock
        self._budgets = budgets
        self._timeout = timeout_s
        self._transport = transport

    def run(self, *, window_s: float, cancelled: Event) -> SwitchReceipt:
        """Drive the alternating switch, then assert every budget it is held to.

        The switches are spread across `window_s` instead of fired as fast as
        the socket allows: a thousand in twenty seconds and a thousand across an
        hour are different experiments, and only the second is the one the
        campaign describes. A window too short for the count fails, which is the
        truthful answer rather than a burst that satisfies the arithmetic.
        """
        if not window_s > 0:
            raise ValueError("the switch needs a positive window to pace across")
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            namespace = self._json(client, SCHEDULER_PATH)
            available = tuple(namespace["available"])
            initial = str(namespace["strategy"])
            missing = [name for name in self._strategies if name not in available]
            if missing:
                raise SwitchError(
                    f"the control plane does not serve {missing!r}: "
                    f"available={list(available)!r}. The switch needs both queue "
                    "modules in the artifact and the admin API enabled."
                )
            live_before = self._readings(client, applied=0)[2]
            if live_before > self._budgets.max_live_strategy_indexes:
                raise SwitchError(
                    f"the run started with {live_before} live strategy indexes, "
                    f"budget {self._budgets.max_live_strategy_indexes}"
                )
            committed, refused, stale = self._alternate(
                client, initial, window_s, cancelled
            )
            restores = self._restore(client, initial)
            # Read back after the restore, so `applied` is every PATCH this step
            # landed: the count the platform publishes is the switches it
            # actually made, and a receipt that stopped short of the restore
            # would report one fewer than the exposition does.
            applied = committed + restores
            pause, counted, live_after = self._readings(client, applied=applied)
            self._assert_budgets(
                committed=committed,
                pause=pause,
                counted=counted,
                live=live_after,
                applied=applied,
            )
            return SwitchReceipt(
                strategies=self._strategies,
                initial=initial,
                final=self._json(client, SCHEDULER_PATH)["strategy"],
                committed=committed,
                refused=refused,
                stale=stale,
                live_indexes=live_after,
                pause_max_ms=pause,
                platform_committed=counted,
                restores=restores,
            )

    def _alternate(
        self, client: httpx.Client, initial: str, window_s: float, cancelled: Event
    ) -> tuple[int, int, int]:
        """Alternate until the count is met or the window closes."""
        target_count = self._budgets.switches_in_soak
        interval = window_s / target_count
        started = self._clock.monotonic()
        current, committed, refused, stale = initial, 0, 0, 0
        attempts = 0
        while committed < target_count:
            if cancelled.is_set() or self._clock.monotonic() >= started + window_s:
                break
            target = next(name for name in self._strategies if name != current)
            # The revision is read from the root envelope on every switch. The
            # `scheduler` namespace's own GET does not carry one, and reading a
            # stale number there is not an error the platform reports: it is a
            # 409 at best and a PATCH against the wrong revision at worst.
            status = self._patch(client, target, self._revision(client))
            if status == 200:
                # A fresh GET, so what is asserted is the engine's own selection
                # and not the body this driver just sent.
                live = self._json(client, SCHEDULER_PATH)["strategy"]
                if live != target:
                    raise SwitchError(
                        f"PATCH to {target!r} answered 200 but the namespace "
                        f"reports {live!r}"
                    )
                current = target
                committed += 1
            elif status == 409:
                stale += 1
            else:
                refused += 1
            attempts += 1
            if committed >= target_count:
                break
            if not self._clock.wait_until(started + interval * attempts, cancelled):
                break
        return committed, refused, stale

    def _restore(self, client: httpx.Client, initial: str) -> int:
        """Put the run back on the selection it started with, and verify it.

        A committed switch does not survive a restart and is not meant to: the
        run's configuration fingerprint is the startup selection, so a soak that
        ended on the other strategy would have ended in a state its own protocol
        does not describe. Returns 1 when a PATCH was needed, 0 when the
        alternation had already landed back on `initial`.
        """
        if self._json(client, SCHEDULER_PATH)["strategy"] == initial:
            return 0
        status = self._patch(client, initial, self._revision(client))
        if status != 200:
            raise SwitchError(
                f"the initial selection {initial!r} could not be restored: "
                f"PATCH answered HTTP {status}"
            )
        live = self._json(client, SCHEDULER_PATH)["strategy"]
        if live != initial:
            raise SwitchError(
                f"restore to {initial!r} answered 200 but the namespace "
                f"reports {live!r}"
            )
        return 1

    def _readings(
        self, client: httpx.Client, *, applied: int
    ) -> tuple[float | None, float | None, int]:
        """One scrape, three readings: the pause, the platform's count, the indexes.

        Absence is not a fault until at least one switch has been applied,
        because until then it is the truth: the counter is created by the first
        switch, so a step that committed nothing legitimately finds no series.
        Past that point absence means a broken scrape or a broken platform, and
        either way not the reading this step claims.

        The index reading is the number of `scheduler_active` series rather than
        any one value, which is what `maxLiveStrategyIndexes` bounds: one gauge
        per built-in strategy, so one of them is always 1 and the rest are 0.
        """
        response = client.get(self._metrics)
        response.raise_for_status()
        rows = parse_exposition(response.text)
        pauses = _series(rows, PAUSE_METER)
        counted = _series(rows, SWITCH_METER, outcome=COMMITTED_OUTCOME)
        if applied and (not pauses or not counted):
            raise SwitchError(
                "the control plane published no switch reading: "
                f"{PAUSE_METER} and {SWITCH_METER} "
                f"{{{COMMITTED_OUTCOME}=...}} must both answer after a switch"
            )
        return (
            max(pauses) * 1000.0 if pauses else None,
            counted[-1] if counted else None,
            len(_series(rows, ACTIVE_METER)),
        )

    def _assert_budgets(
        self,
        *,
        committed: int,
        pause: float | None,
        counted: float | None,
        live: int,
        applied: int,
    ) -> None:
        """Fail the run for every frozen budget this step is answerable for."""
        failures = []
        if committed < self._budgets.switches_in_soak:
            failures.append(
                f"{committed} switches committed, budget "
                f"{self._budgets.switches_in_soak}"
            )
        if pause is None:
            # Reachable only with nothing committed, which the count above has
            # already reported: there is nothing yet for the platform to count.
            failures.append("no switch-pause reading to hold against the budget")
        elif pause > self._budgets.max_switch_pause_ms:
            failures.append(
                f"worst switch pause {pause:.3f} ms, budget "
                f"{self._budgets.max_switch_pause_ms:.1f} ms"
            )
        if live > self._budgets.max_live_strategy_indexes:
            failures.append(
                f"{live} live strategy indexes, budget "
                f"{self._budgets.max_live_strategy_indexes}"
            )
        if counted is not None and counted != applied:
            failures.append(
                f"the platform counted {counted:g} committed switches, this "
                f"step committed {applied}"
            )
        if failures:
            raise SwitchError("; ".join(failures))

    def _revision(self, client: httpx.Client) -> int:
        """Read the revision from the root envelope, which is the only place it is."""
        envelope = self._json(client, ROOT_PATH)
        revision = envelope.get("revision")
        if type(revision) is not int:
            raise SwitchError(
                f"the runtime config carried no root revision: {envelope!r}"
            )
        return revision

    def _patch(self, client: httpx.Client, strategy: str, revision: int) -> int:
        response = client.patch(
            f"{self._base}{SCHEDULER_PATH}",
            json={"expectedRevision": revision, "values": {"strategy": strategy}},
        )
        return response.status_code

    def _json(self, client: httpx.Client, path: str) -> dict[str, Any]:
        response = client.get(f"{self._base}{path}")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise SwitchError(f"{path} did not answer with an object: {body!r}")
        return body
