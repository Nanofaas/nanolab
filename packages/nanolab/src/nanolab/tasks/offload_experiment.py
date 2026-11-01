"""Run one matched B4 arm and preserve evidence before evaluating it."""

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override
from uuid import uuid4

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.assets.diagnostics.idle_reactivation_probe import load_inputs
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.k6 import K6Task
from nanolab.tasks.loadtest import RunK6Task
from nanolab.tasks.loadtest.models import K6Config
from nanolab.tasks.loadtest.ports import RemoteFileFetcher
from nanolab.tasks.loadtest.tasks import FetchVmResults
from nanolab.workspace.paths import bundled_assets_root
from nanolab.workspace.provenance import source_fingerprint


def write_json(path: Path, data: Any) -> None:
    """Write readable durable evidence in the run directory."""
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@dataclass
class RunOffloadExperiment(Task[dict[str, Any]]):
    """One invocation owns its unique remote directory and diagnostic collection."""

    config: ScenarioConfig
    source_root: Path
    run_dir: Path
    remote_home: Path
    edge_url: str
    cloud_url: str
    executor: CommandTaskExecutor
    fetcher: RemoteFileFetcher
    namespace: str
    logs: tuple[Task[Any], ...] = ()
    title: str = "Measure the B4 matched offload arm"

    def _observe(
        self, role: str, stage: str, start: float | None = None
    ) -> dict[str, Any]:
        script = (
            bundled_assets_root() / "diagnostics/offload_observation.py"
        ).read_text()
        command = ("python3", "-c", script, self.namespace)
        if start is not None:
            command += (str(start),)
        try:
            result = self.executor.run(
                CommandTaskSpec(
                    task_id=f"b4.observe.{role}.{stage}",
                    summary=f"Capture {role} {stage}",
                    argv=command,
                    role=role,
                )
            )
            if result.return_code:
                raise RuntimeError(result.stderr or "observation failed")
            observation = json.loads(result.stdout)
        except Exception as error:
            observation = {"errors": [str(error)]}
        write_json(self.run_dir / "observations" / f"{role}-{stage}.json", observation)
        return observation

    def _stage_inputs(self, remote: Path, fixtures: list[dict[str, Any]]) -> None:
        encoded = base64.b64encode(json.dumps(fixtures).encode()).decode()
        # Bound the whole SSH command as well as each Linux argv element.
        for offset in range(0, len(encoded), 48000):
            chunk = encoded[offset : offset + 48000]
            first = offset == 0
            script = (
                "import base64,pathlib,sys;p=pathlib.Path(sys.argv[1]);"
                + ("p.mkdir(parents=True,exist_ok=False);" if first else "")
                + f"f=(p/'inputs.json').open({'xb' if first else 'ab'!r});"
                + "f.write(base64.b64decode(sys.argv[2]));f.close()"
            )
            result = self.executor.run(
                CommandTaskSpec(
                    task_id=f"b4.inputs.{offset}",
                    summary="Stage B4 inputs",
                    argv=("python3", "-c", script, str(remote), chunk),
                    role="loadgen",
                )
            )
            if result.return_code:
                raise RuntimeError(result.stderr or "Could not stage B4 inputs")

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[dict[str, Any]]:
        del inputs
        protocol = self.config.offload_experiment
        if protocol is None:
            raise ValueError("offloadExperiment is required")
        destination = self.run_dir / "inputs"
        destination.mkdir(parents=True, exist_ok=False)
        (self.run_dir / "observations").mkdir(exist_ok=False)
        for name in ("performance-medium.json", "runtime-contract.json"):
            (destination / name).write_bytes(
                (
                    self.source_root / "functions/test-data/word-stats" / name
                ).read_bytes()
            )
        fixtures = load_inputs(
            destination / "performance-medium.json",
            destination / "runtime-contract.json",
        )
        remote = self.remote_home / "nanofaas-loadtest" / ("b4-" + uuid4().hex)
        write_json(
            self.run_dir / "protocol.json",
            {
                "scenario": self.config.model_dump(mode="json", by_alias=True),
                "function": self.config.functions[0],
                "edge_url": self.edge_url,
                "cloud_url": self.cloud_url,
                "source_fingerprint": source_fingerprint(self.source_root),
                "remote_results": str(remote),
                "warmup_rate": 20,
                "phase_drain_seconds": 40,
                "client_timeout_seconds": 30,
                "client_retries": 0,
                "latency_boundary": (
                    "client wall-clock around HTTP call including full "
                    "response; HTTP duration also retained"
                ),
            },
        )
        starts: dict[str, float] = {}
        errors: list[str] = []
        failure: BaseException | None = None
        k6_passed = False
        try:
            for role in ("stack", "cloud"):
                observation = self._observe(role, "before")
                if observation.get("errors"):
                    raise RuntimeError(
                        f"{role} preflight evidence: {observation['errors']}"
                    )
                starts[role] = observation["captured_at"]
            route = self.executor.run(
                CommandTaskSpec(
                    task_id="b4.cloud-route",
                    summary="Verify edge-to-cloud route",
                    argv=(
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--connect-timeout",
                        "5",
                        "--max-time",
                        "15",
                        f"{self.cloud_url}/v1/functions/{self.config.functions[0]}",
                    ),
                    role="stack",
                )
            )
            write_json(
                self.run_dir / "cloud-route.json",
                {
                    "return_code": route.return_code,
                    "stdout": route.stdout,
                    "stderr": route.stderr,
                },
            )
            if route.return_code:
                raise RuntimeError("Edge cannot reach the terminal cloud function")
            self._stage_inputs(remote, fixtures)
            run = RunK6Task(
                run_k6=K6Task(
                    executor=self.executor,
                    role="loadgen",
                    remote_dir=".",
                    config=K6Config(
                        script_path=self.remote_home
                        / "nanolab-assets/k6/offload-experiment.js",
                        target_url=self.edge_url,
                        summary_output_path=remote / "k6-summary.json",
                        env={
                            "B4_INPUTS": str(remote / "inputs.json"),
                            "B4_EDGE": self.edge_url,
                            "B4_CLOUD": self.cloud_url,
                            "NANOFAAS_FUNCTION": self.config.functions[0],
                            "B4_MODE": protocol.mode,
                            "B4_RATE": str(protocol.rate),
                            "B4_WARMUP_SECONDS": str(protocol.warmup_seconds),
                            "B4_DURATION_SECONDS": str(protocol.duration_seconds),
                            "B4_VUS": str(protocol.vus),
                        },
                        process_env={
                            "K6_CONSOLE_OUTPUT": str(remote / "k6-events.jsonl")
                        },
                    ),
                ),
                events=FetchVmResults(
                    "",
                    "Fetch B4 request ledger",
                    self.fetcher,
                    str(remote / "k6-events.jsonl"),
                    self.run_dir,
                ),
            )
            outcome = run.run(TaskInputs.empty()).value
            k6_passed = outcome is not None and outcome.k6.passed
            if outcome is not None:
                write_json(
                    self.run_dir / "k6-result.json",
                    {"passed": outcome.k6.passed, "result": str(outcome.k6)},
                )
        except BaseException as error:
            failure = error
            errors.append(str(error))
        finally:
            try:
                FetchVmResults(
                    "",
                    "Fetch B4 summary",
                    self.fetcher,
                    str(remote / "k6-summary.json"),
                    self.run_dir,
                ).run()
            except Exception as error:
                errors.append(f"summary: {error}")
            for role in ("stack", "cloud"):
                observation = self._observe(role, "after", starts.get(role))
                errors.extend(f"{role}: {e}" for e in observation.get("errors", []))
            for task in self.logs:
                try:
                    task.run(TaskInputs.empty())
                except Exception as error:
                    errors.append(f"{task.title}: {error}")
            write_json(
                self.run_dir / "execution.json",
                {"k6_passed": k6_passed, "errors": errors},
            )
        # Import after evidence is durable: even an analysis error preserves the run.
        from nanolab.tasks.offload_experiment_report import analyze_experiment

        report = analyze_experiment(self.run_dir)
        if failure is not None:
            raise failure
        if not report["integrity_passed"]:
            raise RuntimeError("B4 evidence verification failed; inspect report.json")
        return TaskOutcome(value=report)
