"""Sonata tasks for paired sparse invocations and recoverable B6 evidence."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.config.idle_reactivation import IdleReactivationConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.metrics.catalogue import queries_for
from nanolab.tasks.idle_reactivation import (
    parse_observation,
    qualifies_burst,
    qualifies_idle,
    qualifies_response,
)
from nanolab.tasks.loadtest.models import PrometheusQuery, TimeWindow
from nanolab.tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher
from nanolab.tasks.loadtest.tasks import CapturePrometheusSnapshot, FetchVmResults
from nanolab.workspace.provenance import source_fingerprint


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


@dataclass
class RecordIdleInputs(Task[None]):
    """Freeze the exact oracle, corpus and protocol before building either arm."""

    config: ScenarioConfig
    source_root: Path
    run_dir: Path
    title: str = "Freeze B6 protocol and workload inputs"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Retain inputs for independent offline response validation."""
        del inputs
        destination = self.run_dir / "inputs"
        destination.mkdir(parents=True, exist_ok=False)
        for name in ("performance-medium.json", "runtime-contract.json"):
            (destination / name).write_bytes(
                (
                    self.source_root / "functions/test-data/word-stats" / name
                ).read_bytes()
            )
        _write_json(
            self.run_dir / "protocol.json",
            {
                "scenario": self.config.model_dump(mode="json", by_alias=True),
                "source_fingerprint": source_fingerprint(self.source_root),
                "client_timeout_seconds": 40,
                "client_retries": 0,
                "warmup_requests": 60,
                "warmup_interval_seconds": 1,
                "followup_requests": 5,
                "idle_window_seconds": 60,
                "cadvisor_scrape_seconds": 10,
                "prometheus_query_step_seconds": 5,
            },
        )
        return TaskOutcome(value=None)


class LifecycleWatcher:
    """Retain bounded Kubernetes observations while remote requests are running."""

    def __init__(
        self, executor: CommandTaskExecutor, function: str, namespace: str, output: Path
    ) -> None:
        """Bind to the stack role and a new append-only evidence file."""
        self.executor, self.function, self.namespace = executor, function, namespace
        self.output = output
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def observe(self) -> dict[str, Any]:
        """Record unknown observations explicitly, retaining the raw API reply."""
        with self._lock:
            raw = None
            try:
                result = self.executor.run(
                    CommandTaskSpec(
                        task_id="b6-observe",
                        summary="Observe B6 pod lifecycle",
                        role="stack",
                        argv=(
                            "timeout",
                            "--kill-after=5s",
                            "15s",
                            "sudo",
                            "kubectl",
                            "--request-timeout=5s",
                            "-n",
                            self.namespace,
                            "get",
                            "deployments,pods",
                            "-o",
                            "json",
                        ),
                    )
                )
                if result.return_code != 0:
                    raise RuntimeError(result.stderr or "Kubernetes observation failed")
                raw = json.loads(result.stdout)
                sample = parse_observation(raw, self.function, time.time())
            except Exception as error:
                sample = {"time": time.time(), "error": str(error)}
            self.samples.append(sample)
            with self.output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({**sample, "raw": raw}) + "\n")
            return sample

    def _loop(self) -> None:
        while not self._stop.wait(2):
            self.observe()

    def start(self) -> None:
        """Observe before the first request and start periodic sampling."""
        self.observe()
        self._thread.start()

    def stop(self) -> None:
        """Join the bounded command before releasing its platform."""
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=20)
            if self._thread.is_alive():
                raise RuntimeError("B6 lifecycle observer did not stop")


@dataclass
class RunIdleReactivation(Task[dict[str, Any]]):
    """Measure one arm; publish errors before the final paired gate judges it."""

    function: str
    treatment: str
    namespace: str
    protocol: IdleReactivationConfig
    executor: CommandTaskExecutor
    prometheus_client: PrometheusClient
    fetcher: RemoteFileFetcher | None
    run_dir: Path
    remote_home: Path
    control_plane_url: str

    title: str = field(init=False)

    def __post_init__(self) -> None:
        """Name the treatment in the normal Sonata execution dashboard."""
        self.title = f"Measure idle reactivation: {self.treatment}"

    def _probe(self, phase: str, cycle: int = 0) -> list[dict[str, Any]]:
        name = f"{phase}-{cycle:04d}.jsonl"
        remote = (
            self.remote_home
            / "nanofaas-b6"
            / self.run_dir.parent.name
            / self.treatment
            / name
        )
        corpus = (
            self.remote_home
            / "nanofaas/functions/test-data/word-stats/performance-medium.json"
        )
        argv = (
            "python3",
            str(
                self.remote_home
                / "nanolab-assets/diagnostics/idle_reactivation_probe.py"
            ),
            "--url",
            self.control_plane_url,
            "--function",
            self.function,
            "--corpus",
            str(corpus),
            "--contract",
            str(corpus.with_name("runtime-contract.json")),
            "--phase",
            phase,
            "--cycle",
            str(cycle),
            "--output",
            str(remote),
        )
        result = None
        try:
            result = self.executor.run(
                CommandTaskSpec(
                    task_id=f"b6-{phase}-{cycle}",
                    summary=f"B6 {phase} {cycle}",
                    argv=("timeout", "--kill-after=5s", "300s", *argv),
                    role="loadgen",
                )
            )
        finally:
            # stdout is an independent fallback if a VM disappears before scp.
            if result is not None:
                (self.run_dir / f"{name}.stdout").write_text(
                    result.stdout, encoding="utf-8"
                )
            if self.fetcher is not None:
                FetchVmResults(
                    "", "Recover B6 requests", self.fetcher, str(remote), self.run_dir
                ).run()
        if result is None or result.return_code != 0:
            raise RuntimeError(
                result.stderr if result else "B6 probe returned no result"
            )
        destination = self.run_dir / name
        if self.fetcher is None:
            destination.write_text(result.stdout, encoding="utf-8")
        events = [
            json.loads(line)
            for line in destination.read_text().splitlines()
            if line.strip()
        ]
        records = [r for r in events if r.get("event") == "response"]
        if len(events) != len(records) * 2 or any(
            events[2 * i].get("event") != "started"
            or events[2 * i].get("request_id") != record.get("request_id")
            for i, record in enumerate(records)
        ):
            raise RuntimeError(
                "B6 request ledger contains unresolved or unmatched attempts"
            )
        expected = {"qualification": 4, "warmup": 60, "cycle": 6}[phase]
        if (
            not records
            or len(records) > expected
            or any(
                r.get("phase") != phase
                or r.get("cycle") != cycle
                or r.get("part") != i
                or r.get("function") != self.function
                for i, r in enumerate(records)
            )
        ):
            raise RuntimeError("B6 request ledger is missing, foreign or out of order")
        if len(records) != expected and all(r.get("valid") for r in records):
            raise RuntimeError("B6 request ledger is truncated")
        return records

    def _idle(self, watcher: LifecycleWatcher) -> tuple[float, float]:
        start = time.time()
        time.sleep(self.protocol.idle_seconds)
        watcher.observe()
        return start, time.time()

    def _measure(self, watcher: LifecycleWatcher, report: dict[str, Any]) -> None:
        for phase in ("qualification", "warmup"):
            if not all(r["valid"] for r in self._probe(phase)):
                raise RuntimeError(f"B6 {phase} failed; no measured cycles started")
        initial = watcher.observe()
        cp, pods = initial.get("control_plane", []), initial.get("pods", [])
        if (
            len(cp) != 1
            or len(pods) != 1
            or not cp[0].get("image_id")
            or not pods[0].get("image_id")
        ):
            raise RuntimeError(
                "B6 cannot establish initial control-plane/function image identity"
            )
        report["identities"] = {
            "control_plane": cp[0]["image_id"],
            "function": pods[0]["image_id"],
        }
        report["control_plane_uid"] = cp[0]["uid"]
        report["measurement_start"] = initial["time"]
        for index in range(self.protocol.cycles):
            idle_start, idle_end = self._idle(watcher)
            before = watcher.samples[-1]
            seen = {p["uid"] for s in watcher.samples for p in s.get("pods", [])}
            idle_ok = qualifies_idle(
                watcher.samples, idle_end - 60, idle_end, self.treatment
            )
            cycle: dict[str, Any] = {
                "cycle": index,
                "idle_start": idle_start,
                "idle_end": idle_end,
                "idle_qualified": idle_ok,
            }
            report["cycles"].append(cycle)
            _write_json(self.run_dir / "cycles.json", report["cycles"])
            requests = self._probe("cycle", index)
            after = watcher.observe()
            cycle.update(
                requests=requests,
                end=time.time(),
                service_success=requests[0]["valid"],
                lifecycle_qualified=qualifies_response(
                    before, after, self.treatment, seen, pods[0]["image_id"]
                )
                and qualifies_burst(
                    watcher.samples, before["time"], after["time"], after
                ),
            )
            _write_json(self.run_dir / "cycles.json", report["cycles"])
            if not all(r["valid"] for r in requests):
                raise RuntimeError(
                    "B6 request failed; server completion is unproven, arm stopped"
                )
        # The final return to idle has no following cycle to observe it for us.
        tail_start, tail_end = self._idle(watcher)
        report["tail"] = {
            "start": tail_start,
            "end": tail_end,
            "qualified": qualifies_idle(
                watcher.samples, tail_end - 60, tail_end, self.treatment
            ),
        }

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[dict[str, Any]]:
        """Preserve partial evidence and capture metrics before resource release."""
        del inputs
        self.run_dir.mkdir(parents=True, exist_ok=False)
        report: dict[str, Any] = {
            "treatment": self.treatment,
            "function": self.function,
            "configured_cycles": self.protocol.cycles,
            "cycles": [],
            "errors": [],
            "complete": False,
        }
        started = datetime.now(UTC)
        watcher = LifecycleWatcher(
            self.executor,
            self.function,
            self.namespace,
            self.run_dir / "lifecycle.jsonl",
        )
        interrupted = None
        try:
            watcher.start()
            self._measure(watcher, report)
        except Exception as error:
            report["errors"].append(str(error))
        except BaseException as error:
            interrupted = error
            report["errors"].append(type(error).__name__)
        finally:
            ended = datetime.now(UTC)
            try:
                watcher.stop()
            except Exception as error:
                report["errors"].append(str(error))
            queries = queries_for(
                self.function,
                modules=("autoscaler", "async-queue"),
                container_functions=(self.function,),
                function_runtime=True,
            )
            queries = (*queries, PrometheusQuery("scrape_health", "up", True))
            try:
                CapturePrometheusSnapshot(
                    "",
                    "Capture B6 raw metrics",
                    self.prometheus_client,
                    queries,
                    TimeWindow(started, ended),
                    self.run_dir,
                    lead_seconds=0,
                ).run()
            except Exception as error:
                report["errors"].append(str(error))
            if report.get("measurement_start") is not None:
                stable = all(
                    "error" not in s
                    and len(s.get("control_plane", [])) == 1
                    and s["control_plane"][0]["uid"] == report["control_plane_uid"]
                    and s["control_plane"][0]["restarts"] == 0
                    and s["control_plane"][0]["ready"]
                    for s in watcher.samples
                    if s["time"] >= report["measurement_start"]
                )
                if not stable:
                    report["errors"].append(
                        "Control plane changed or was unobservable during measurement"
                    )
            offered = [c for c in report["cycles"] if c.get("requests")]
            report["offered_cycles"] = len(offered)
            # A command that lost its ledger has an unknown outcome, not zero traffic.
            report["not_attempted"] = self.protocol.cycles - len(report["cycles"])
            report["unresolved_cycles"] = len(report["cycles"]) - len(offered)
            report["service_successes"] = sum(
                c.get("service_success", False) for c in offered
            )
            report["qualified_successes"] = sum(
                c.get("service_success", False)
                and c["idle_qualified"]
                and c.get("lifecycle_qualified", False)
                for c in offered
            )
            report["complete"] = (
                not report["errors"]
                and len(offered) == self.protocol.cycles
                and report["qualified_successes"] == self.protocol.cycles
                and report.get("tail", {}).get("qualified", False)
            )
            report.update(start=started.isoformat(), end=ended.isoformat())
            _write_json(self.run_dir / "summary.json", report)
        if interrupted is not None:
            raise interrupted
        return TaskOutcome(value=report)


@dataclass
class VerifyIdlePair(Task[dict[str, Any]]):
    """Fail after both arms have saved their evidence and released deployments."""

    run_dir: Path
    title: str = "Verify paired idle-reactivation evidence"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[dict[str, Any]]:
        """Check both complete arms used identical immutable runtime images."""
        del inputs
        from nanolab.tasks.idle_reactivation_report import analyze_pair

        result = analyze_pair(self.run_dir)
        passed = result["passed"]
        if not passed:
            raise RuntimeError(
                "B6 paired evidence did not qualify; see paired-summary.json"
            )
        return TaskOutcome(value=result)
