"""Fresh deployments and recoverable measurements; no provisioning of its own."""

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.comparison.cells import write_json_atomically
from nanolab.config.module_cost import BUDGETS
from nanolab.core.deployment import DEFAULT_NAMESPACE
from nanolab.metrics.catalogue import container_queries, function_runtime_queries
from nanolab.plans.diagnostics import collect_control_plane_log
from nanolab.tasks.components.helm import control_plane_helm_values, helm_set_args
from nanolab.tasks.idle_reactivation_runtime import LifecycleWatcher
from nanolab.tasks.k6 import K6Task
from nanolab.tasks.loadtest.models import K6Config, PrometheusQuery, TimeWindow
from nanolab.tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher
from nanolab.tasks.loadtest.tasks import CapturePrometheusSnapshot, FetchVmResults
from nanolab.tasks.module_cost_build import command, stage_bytes
from nanolab.tasks.platform import PlatformFunction, PlatformRequest, add_platform
from nanolab.tasks.recipes.workflow import RecipeBinding

FUNCTION = "word-stats-java"


def fixed_function(image: str) -> PlatformFunction:
    """Apply identical, explicitly inactive policies to every composition."""
    return PlatformFunction(
        name=FUNCTION,
        image=image,
        payload="{}",
        build_argv=(),
        resources=BUDGETS[FUNCTION],
        concurrency=2,
        queue_size=20,
        max_retries=0,
        timeout_ms=30000,
        scaling_config={
            "strategy": "NONE",
            "minReplicas": 1,
            "maxReplicas": 1,
            "concurrencyControl": {"mode": "FIXED"},
        },
    )


class FailureEvidenceExecutor:
    """Save startup failures before Sonata compensates the failed acquisition."""

    def __init__(self, executor: CommandTaskExecutor, directory: Path):
        """Bind diagnostics to the platform executor."""
        self.executor, self.directory = executor, directory

    def binding_key(self, role):
        """Preserve the underlying Sonata binding identity."""
        return self.executor.binding_key(role)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False):
        """Capture failed acquisitions before their compensation."""
        try:
            result = self.executor.run(task, dry_run=dry_run)
        except Exception:
            self.capture()
            raise
        if result.return_code:
            self.capture()
        return result

    def capture(self):
        """Attempt diagnostic recovery without masking the original failure."""
        # Diagnostics must not hide the original command failure.
        try:
            collect_control_plane_log(
                executor=self.executor,
                role="stack",
                namespace=DEFAULT_NAMESPACE,
                destination=self.directory / "startup-failure.log",
            ).run(TaskInputs.empty())
        except Exception as error:
            (self.directory / "diagnostics-error.txt").write_text(str(error))


@dataclass
class RunModuleArm(Task[None]):
    """Deploy one recipe and release it before the next arm."""

    arm: str
    images: Resource[Any]
    executor: CommandTaskExecutor
    run_dir: Path
    source: Path
    remote_source: Path
    load_home: Path
    url: str
    client: PrometheusClient
    fetcher: RemoteFileFetcher | None
    title: str = field(init=False)

    def __post_init__(self):
        """Give each arm a distinct workflow identity."""
        self.title = f"Measure module cost: {self.arm}"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Execute the scoped measurement and preserve failure evidence."""
        built = inputs.resource(self.images)
        distribution = built[self.arm]
        directory = self.run_dir / "arms" / self.arm
        directory.mkdir(parents=True, exist_ok=False)
        image_data = json.loads(
            (self.run_dir / "build/jvm-base/images.json").read_text()
        )
        function = fixed_function(image_data["function"]["digest"])
        cp_images = json.loads(
            (self.run_dir / "build" / self.arm / "images.json").read_text()
        )
        # Pull through containerd before launch; Docker's cache is not the k3s cache.
        for image in (cp_images["control-plane"]["reference"], function.image):
            command(self.executor, "stack", ("sudo", "k3s", "crictl", "pull", image))
        binding = RecipeBinding(
            Resource(
                title=f"Recipe {self.arm}",
                acquire=lambda _inputs: distribution,
                release=lambda _inputs, _value: None,
            ),
            {},
            directory,
        )
        values = control_plane_helm_values(
            namespace=DEFAULT_NAMESPACE,
            control_plane_image=distribution.control_plane().image.reference,
            image_pull_policy="IfNotPresent",
            expose_node_port=True,
            metrics_profile="advanced",
            container_metrics=True,
            control_plane_resources=BUDGETS["control-plane"],
        )
        index = sum(
            k.endswith(".name") and k.startswith("controlPlane.extraEnv[")
            for k in values
        )
        values[f"controlPlane.extraEnv[{index}].name"] = "NANOFAAS_K8S_IMAGEPULLPOLICY"
        values[f"controlPlane.extraEnv[{index}].value"] = "IfNotPresent"
        request = PlatformRequest(
            backend="k8s",
            functions=(function,),
            recipe=binding,
            helm_chart=str(self.remote_source / "deploy/helm/nanofaas"),
            helm_values=helm_set_args(values),
            label=self.arm,
        )
        write_json_atomically(
            directory / "registration.json", function.manifest().body()
        )
        write_json_atomically(
            directory / "deployment.json",
            {
                "helm_values": values,
                "images": {
                    "control-plane": cp_images["control-plane"]["digest"],
                    "function": function.image,
                },
                "modules": distribution.modules,
            },
        )
        guarded = FailureEvidenceExecutor(self.executor, directory)
        workflow = Workflow(self.title)
        platform = add_platform(workflow, request, executor=guarded, cwd=self.source)
        workflow.add(
            MeasureModuleArm(
                directory,
                self.run_dir,
                self.load_home,
                self.url,
                self.executor,
                self.client,
                self.fetcher,
            ),
            requires=(*platform.resources, *platform.functions),
        )
        try:
            workflow.run()
            from nanolab.tasks.module_cost_report import analyze_arm

            analyze_arm(
                self.run_dir,
                self.arm,
                json.loads((self.run_dir / "protocol.json").read_text()),
            )
        except BaseException as error:
            (directory / "failure.txt").write_text(str(error))
            raise
        return TaskOutcome(value=None)


@dataclass
class MeasureModuleArm(Task[None]):
    """Collect warmup, drained idle and fixed-load evidence."""

    directory: Path
    block: Path
    load_home: Path
    url: str
    executor: CommandTaskExecutor
    client: PrometheusClient
    fetcher: RemoteFileFetcher | None
    title: str = "Warm, idle and measure a module composition"

    def load(self, phase: str, seconds: int, inputs: TaskInputs) -> None:
        """Run the frozen corpus and recover both request and summary files."""
        remote = (
            self.load_home
            / "module-cost"
            / self.block.name
            / self.directory.name
            / phase
        )
        command(self.executor, "loadgen", ("mkdir", "-p", str(remote)))
        # Use the archived inputs, not whatever is currently in another checkout.
        for name in ("performance-medium.json", "runtime-contract.json"):
            stage_bytes(
                self.executor,
                "loadgen",
                remote / name,
                (self.block / "inputs" / name).read_bytes(),
            )
        config = K6Config(
            script_path=self.load_home / "nanolab-assets/k6/module-cost.js",
            target_url=self.url,
            summary_output_path=remote / "summary.json",
            env={
                "MC_PHASE": phase,
                "MC_SECONDS": str(seconds),
                "NANOFAAS_CONTRACT": str(remote / "runtime-contract.json"),
            },
            payload_path=remote / "performance-medium.json",
            process_env={"K6_CONSOLE_OUTPUT": str(remote / "events.jsonl")},
        )
        destination = self.directory / phase
        destination.mkdir()
        try:
            result = K6Task(
                config, executor=self.executor, role="loadgen", require_pass=True
            ).run(inputs)
            write_json_atomically(
                destination / "runner.json", {"result": str(result.value)}
            )
        finally:
            errors = []
            for name in ("events.jsonl", "summary.json"):
                try:
                    if self.fetcher:
                        FetchVmResults(
                            "",
                            "Recover module-cost load",
                            self.fetcher,
                            str(remote / name),
                            destination,
                        ).run()
                    else:
                        (destination / name).write_text(
                            command(
                                self.executor, "loadgen", ("cat", str(remote / name))
                            ).stdout
                        )
                except Exception as error:
                    errors.append(f"{name}: {error}")
            if errors:
                (destination / "collection-errors.txt").write_text("\n".join(errors))
        from nanolab.tasks.module_cost_report import read_load

        read_load(destination, self.block / "inputs", seconds, phase)

    def spec(self, name: str) -> None:
        """Archive the effective server-side registration."""
        result = command(
            self.executor,
            "stack",
            (
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "15",
                f"http://127.0.0.1:30080/v1/functions/{FUNCTION}",
            ),
        )
        write_json_atomically(
            self.directory / f"spec-{name}.json", json.loads(result.stdout)
        )

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Execute the scoped measurement and preserve failure evidence."""
        watcher = LifecycleWatcher(
            self.executor,
            FUNCTION,
            DEFAULT_NAMESPACE,
            self.directory / "lifecycle.jsonl",
        )
        report: dict[str, Any] = {"start": time.time(), "status": "incomplete"}
        error = None
        try:
            watcher.start()
            self.spec("before")
            self.load("warmup", 60, inputs)
            # k6 has returned and read_load reconciled every send with a terminal.
            report["idle_start"] = time.time()
            time.sleep(180)
            report["idle_end"] = time.time()
            watcher.observe()
            report["load_start"] = time.time()
            self.load("measurement", 180, inputs)
            report["load_end"] = time.time()
            self.spec("after")
            report["status"] = "collected"
        except BaseException as caught:
            error = caught
            report["error"] = str(caught)
        finally:
            watcher.observe()
            report["end"] = time.time()
            for name, collect in (
                ("lifecycle", watcher.stop),
                (
                    "log",
                    lambda: collect_control_plane_log(
                        executor=self.executor,
                        role="stack",
                        namespace=DEFAULT_NAMESPACE,
                        destination=self.directory / "control-plane.log",
                    ).run(inputs),
                ),
                ("metrics", lambda: self.snapshot(report)),
                (
                    "host",
                    lambda: (self.directory / "boot-id.txt").write_text(
                        command(
                            self.executor,
                            "stack",
                            ("cat", "/proc/sys/kernel/random/boot_id"),
                        ).stdout.strip()
                    ),
                ),
            ):
                try:
                    collect()
                except Exception as caught:
                    report.setdefault("collection_errors", {})[name] = str(caught)
                    if error is None:
                        error = caught
            write_json_atomically(self.directory / "measurement.json", report)
        if error:
            raise error
        return TaskOutcome(value=None)

    def snapshot(self, report):
        """Collect labelled resource counters over the full arm."""
        queries = (
            *container_queries((FUNCTION,)),
            *function_runtime_queries(FUNCTION),
            PrometheusQuery("scrape_health", "up", True),
            PrometheusQuery(
                "function_effective_concurrency",
                f'function_effective_concurrency{{function="{FUNCTION}"}}',
                True,
            ),
        )
        CapturePrometheusSnapshot(
            "",
            "Capture module cost",
            self.client,
            queries,
            TimeWindow(
                datetime.fromtimestamp(report["start"], UTC),
                datetime.fromtimestamp(report["end"], UTC),
            ),
            self.directory,
            lead_seconds=0,
        ).run()


@dataclass
class AnalyzeModuleBlock(Task[None]):
    """Publish the paired result after all deployments have been released."""

    directory: Path
    title: str = "Verify module-cost block"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Reject incomplete comparisons while retaining their analysis file."""
        from nanolab.tasks.module_cost_report import analyze_module_cost

        result = analyze_module_cost(self.directory)
        if not result["comparable"]:
            raise ValueError(str(result["errors"]))
        return TaskOutcome(value=None)
