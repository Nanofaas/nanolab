"""Qualify selected CLI launchers against one owned JVM Kubernetes platform."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO, override
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.kubectl import owned_deployment_pods
from sonata_tasks.minikube import MinikubeTarget

from nanolab.assets.diagnostics.native_k8s_runtime import LOG_LIMIT
from nanolab.comparison.prepare import captured_source_state
from nanolab.tasks.cli import CliFunction, CliWorkflowRequest, add_cli_contract
from nanolab.tasks.cli_artifacts import (
    CLI_STREAM_LIMIT,
    CliArtifact,
    check_cli_diagnostics,
    cli_modes,
    verify_cli_artifact,
)
from nanolab.tasks.cli_function import (
    CliFunctionListTask,
    _json_stdout,
    parse_cli_list,
    runtime_config_patch_task,
    runtime_config_readback_task,
    runtime_config_tasks,
)
from nanolab.tasks.http_function import Endpoint
from nanolab.tasks.kubectl import k8s_function_resources_absent
from nanolab.tasks.recipes.kubernetes import (
    RecipeKubernetesImageCheckTask,
    _json_command,
)
from nanolab.tasks.recipes.validation import RecipeMetadataCheckTask
from nanolab.tasks.recipes.workflow import RecipeDistribution
from nanolab.workspace.recipe import RecipeRun

RECEIPT_LIMIT = 16 * 1024 * 1024
_MODULES = frozenset({"k8s-deployment-provider", "build-metadata", "runtime-config"})
_NAMESPACE = "control-plane"
_FIELD = "rateMaxPerSecond"


class _EvidenceExecutor:
    """Bound every contract/probe command and retain failures before verification."""

    def __init__(self, executor: CommandTaskExecutor, stream: TextIO) -> None:
        self.executor, self.stream = executor, stream
        self.written = 0
        self.results: dict[str, list[TaskResult]] = {}

    def binding_key(self, role: str) -> str:
        return self.executor.binding_key(role)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        deadline = min(task.options.timeout_seconds or 60, 60)
        result = self.executor.run(
            replace(
                task,
                argv=("timeout", "--kill-after=5s", f"{deadline:g}s", *task.argv),
                options=replace(task.options, timeout_seconds=None),
            ),
            dry_run=dry_run,
        )
        limit = (
            LOG_LIMIT
            if task.argv[0] == "kubectl" and "logs" in task.argv
            else CLI_STREAM_LIMIT
        )
        record = {
            "case": task.summary,
            "argv": task.argv,
            "returnCode": result.return_code,
            "stdout": result.stdout.encode()[:limit].decode(errors="replace"),
            "stderr": result.stderr.encode()[:limit].decode(errors="replace"),
            "truncated": max(len(result.stdout.encode()), len(result.stderr.encode()))
            > limit,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if self.written + len(line.encode()) > RECEIPT_LIMIT:
            raise RuntimeError("CLI pass receipts exceed their byte bound")
        self.stream.write(line)
        self.stream.flush()
        self.written += len(line.encode())
        self.results.setdefault(task.summary, []).append(result)
        check_cli_diagnostics(result.stdout, result.stderr, limit=limit)
        return result


def _runtime_snapshot(task: CommandTask, inputs: TaskInputs) -> dict[str, Any]:
    result = task.run(inputs).value
    if result is None:
        raise RuntimeError("CLI runtime snapshot returned no evidence")
    return _json_stdout(result)


def _required_cases(function: str) -> set[str]:
    return {
        f"Apply {function}",
        "List functions",
        "Control-plane info",
        "Control-plane contract",
        "Runtime config snapshot",
        "Validate runtime config",
        "Reject invalid runtime config",
        "Patch runtime config",
        "Runtime config readback",
        f"Get {function}",
        f"CLI config file for {function}",
        f"Update {function}",
        f"Get updated {function}",
        f"Scale {function}",
        f"Replicas of {function}",
        f"Invoke {function}",
        f"Refuse unreplaced change to {function}",
        f"Replace {function}",
        f"Get replaced {function}",
        f"Delete {function}",
        f"Wait for {function} resources to disappear",
        f"Wait for deployment/fn-{function}",
        f"Roll out deployment/fn-{function}",
        f"Scaled Wait for deployment/fn-{function}",
        f"Scaled Roll out deployment/fn-{function}",
    }


def _observations(
    results: dict[str, list[TaskResult]],
    *,
    function: str,
    namespace: str,
    report: RecipeDistribution,
    version: str,
) -> dict[str, object]:
    required = _required_cases(function)
    missing = required - results.keys()
    if missing:
        raise RuntimeError(f"CLI pass omitted required cases: {sorted(missing)}")
    observations: dict[str, object] = {"help": {"valid": True}, "version": version}
    json_titles = {
        "Control-plane info",
        "Runtime config snapshot",
        "Validate runtime config",
        "Patch runtime config",
        "Runtime config readback",
        f"Get {function}",
        f"CLI config file for {function}",
        f"Update {function}",
        f"Get updated {function}",
        f"Scale {function}",
        f"Replicas of {function}",
        f"Invoke {function}",
        f"Replace {function}",
        f"Get replaced {function}",
    }
    for title in sorted(required):
        entries = results[title]
        if len(entries) != 1:
            raise RuntimeError(f"CLI case is ambiguous: {title}")
        result = entries[0]
        if title == "List functions" or title == f"Delete {function}":
            observations[title] = parse_cli_list(result.stdout)
        elif title == "Control-plane contract":
            observations[title] = yaml.safe_load(result.stdout)
        elif title in json_titles:
            payload = _json_stdout(result)
            if title == "Control-plane info":
                metadata = payload.get("metadata")
                control = report.control_plane()
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("version") != version
                    or sorted(metadata.get("modules", [])) != sorted(report.modules)
                    or metadata.get("build", {}).get("type") != control.mode
                    or metadata.get("build", {}).get("variant") != control.variant
                    or metadata.get("build", {}).get("optimization")
                    != control.optimization
                ):
                    raise RuntimeError(
                        "CLI info metadata differs from the selected recipe"
                    )
                if any(
                    payload.get("capabilities", {}).get(key) is not True
                    for key in (
                        "functionUpdate",
                        "replicas",
                        "buildMetadata",
                        "runtimeConfig",
                    )
                ):
                    raise RuntimeError("CLI info lacks required capabilities")
            if "endpointUrl" in payload:
                endpoint = urlsplit(payload["endpointUrl"])
                if (
                    endpoint.scheme != "http"
                    or endpoint.hostname
                    != f"fn-{function}.{namespace}.svc.cluster.local"
                    or endpoint.path != "/invoke"
                ):
                    raise RuntimeError(
                        "CLI function endpoint does not belong to the owned namespace"
                    )
                payload.pop("endpointUrl")
            if title == f"Invoke {function}":
                identifier = payload.pop("executionId", None)
                if identifier is not None and (
                    not isinstance(identifier, str) or not identifier
                ):
                    raise RuntimeError("CLI invocation has an invalid execution ID")
            if title in ("Runtime config snapshot", "Runtime config readback"):
                payload.pop("revision", None)
            if title == "Patch runtime config":
                try:
                    UUID(payload.pop("changeId"))
                    applied = datetime.fromisoformat(payload.pop("appliedAt"))
                    if applied.tzinfo is None:
                        raise ValueError("missing timezone")
                except (KeyError, ValueError, TypeError, AttributeError) as error:
                    raise RuntimeError(
                        "CLI runtime patch metadata is invalid"
                    ) from error
                payload.pop("revision", None)
                payload.get("effectiveConfig", {}).pop("revision", None)
            observations[title] = payload
        else:
            # Negative diagnostics may quote launcher paths. Each shared verifier
            # already required a nonzero exit and the expected diagnostic.
            observations[title] = {"exit": result.return_code}
    return observations


class CliParityTask(Task[None]):
    """Run complete command passes and prove state, artifact and platform continuity."""

    def __init__(
        self,
        run: Resource[RecipeRun],
        distribution: Resource[RecipeDistribution],
        artifacts: tuple[Resource[CliArtifact], ...],
        *,
        runtime: str,
        endpoint: Endpoint,
        target: Resource[MinikubeTarget],
        namespace: str,
        function: str,
        executor: CommandTaskExecutor,
        evidence_dir: Path,
        function_resources: dict[str, Any] | None = None,
    ) -> None:
        """Bind the current attempt; artifact receipts live in sibling `artifacts`."""
        self.title = "Qualify CLI artifact contracts"
        self.run_resource, self.distribution, self.artifacts = (
            run,
            distribution,
            artifacts,
        )
        self.runtime, self.endpoint, self.target = runtime, endpoint, target
        self.function_resources = function_resources
        self.namespace, self.function, self.executor, self.evidence_dir = (
            namespace,
            function,
            executor,
            evidence_dir,
        )

    def _prefix(self, inputs: TaskInputs) -> tuple[str, ...]:
        return (
            "kubectl",
            "--context",
            inputs.resource(self.target).context,
            "-n",
            self.namespace,
        )

    def _snapshot(
        self, inputs: TaskInputs, executor: CommandTaskExecutor
    ) -> tuple[dict[str, str], str]:
        prefix = self._prefix(inputs)

        def fetch(kind: str, *args: str) -> dict[str, Any]:
            return _json_command(
                executor,
                inputs,
                f"Read CLI platform {kind}",
                (*prefix, "get", kind, *args, "-o", "json"),
            )

        deployment = fetch("deployment", "nanofaas-control-plane")
        owned = owned_deployment_pods(
            deployment,
            fetch("replicasets").get("items", []),
            fetch("pods").get("items", []),
        )
        if len(owned) != 1 or owned[0].get("status", {}).get("phase") != "Running":
            raise RuntimeError("CLI platform needs one owned running control-plane Pod")
        pod = owned[0]
        statuses = [
            row
            for row in pod.get("status", {}).get("containerStatuses", [])
            if row.get("name") == "control-plane"
        ]
        if len(statuses) != 1 or statuses[0].get("ready") is not True:
            raise RuntimeError("CLI control-plane container is missing or not ready")
        identity = {
            "deploymentUid": deployment["metadata"].get("uid"),
            "podUid": pod.get("metadata", {}).get("uid"),
            "containerId": statuses[0].get("containerID"),
            "imageId": statuses[0].get("imageID"),
            "node": pod.get("spec", {}).get("nodeName"),
        }
        if (
            not all(isinstance(value, str) and value for value in identity.values())
            or not re.fullmatch(
                r"(?:containerd|docker|cri-o)://[0-9a-f]{64}", identity["containerId"]
            )
            or identity["node"] not in inputs.resource(self.target).nodes
        ):
            raise RuntimeError(
                "CLI control-plane identity is incomplete or incompatible"
            )
        return identity, pod["metadata"]["name"]

    def _log_task(
        self,
        inputs: TaskInputs,
        executor: CommandTaskExecutor,
        pod: str,
        directory: Path,
        label: str,
    ) -> CommandTask:
        def verify(result: TaskResult) -> None:
            (directory / f"logs-{label}.txt").write_text(result.stdout)
            if not result.stdout.strip():
                raise RuntimeError("CLI control-plane logs are missing")

        return CommandTask(
            title=f"CLI control-plane logs {label}",
            argv=(
                *self._prefix(inputs),
                "logs",
                pod,
                "-c",
                "control-plane",
                f"--limit-bytes={LOG_LIMIT + 1}",
            ),
            executor=executor,
            role="host",
            semantic_key=f"cli-parity-logs:{label}:{pod}",
            verify=verify,
        )

    def _platform_checks(
        self, inputs: TaskInputs, executor: CommandTaskExecutor, directory: Path
    ) -> None:
        RecipeMetadataCheckTask(
            self.distribution,
            executor=executor,
            run_dir=directory,
            endpoint=self.endpoint,
        ).run(inputs)
        RecipeKubernetesImageCheckTask(
            self.distribution,
            namespace=self.namespace,
            deployment="nanofaas-control-plane",
            component=("control-plane", "control-plane", "java"),
            executor=executor,
            role="host",
            run_dir=directory,
            target=self.target,
        ).run(inputs)

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        if self.evidence_dir.exists():
            raise FileExistsError(self.evidence_dir)
        staged, report = (
            inputs.resource(self.run_resource),
            inputs.resource(self.distribution),
        )
        artifacts = tuple(inputs.resource(resource) for resource in self.artifacts)
        if tuple(artifact.mode for artifact in artifacts) != cli_modes(self.runtime):
            raise ValueError("CLI artifact modes differ from requested runtime")
        if (
            report.control_plane().mode != "jvm"
            or report.function("word-stats", "java").mode != "jvm"
            or not _MODULES.issubset(report.modules)
        ):
            raise ValueError("CLI parity requires the selected JVM recipe and modules")
        source = captured_source_state(staged.source_dir)
        recipe_hash = hashlib.sha256(staged.recipe.read_bytes()).hexdigest()
        for artifact in artifacts:
            verify_cli_artifact(staged.source_dir, artifact)
            receipt = json.loads(
                (
                    self.evidence_dir.parent / "artifacts" / f"{artifact.mode}.json"
                ).read_text()
            )
            if (
                receipt.get("mode") != artifact.mode
                or receipt.get("version") != artifact.version
                or receipt.get("files") != [list(row) for row in artifact.files]
                or receipt.get("source") != source
                or receipt.get("tag") != staged.tag
                or receipt.get("recipeSha256") != recipe_hash
            ):
                raise RuntimeError(
                    "CLI artifact receipt differs from the current frozen attempt"
                )
            for case in ("help", "versionCommand"):
                output = receipt.get(case, {})
                check_cli_diagnostics(
                    output.get("stdout", ""), output.get("stderr", "")
                )
                if (
                    output.get("returnCode") != 0
                    or (case == "help" and "Usage:" not in output.get("stdout", ""))
                    or (
                        case == "versionCommand"
                        and output.get("stdout", "").strip()
                        != f"nanofaas {artifact.version}"
                    )
                ):
                    raise RuntimeError(f"CLI artifact lacks verified {case} evidence")
        cases = json.loads(
            (
                staged.source_dir / "functions/test-data/word-stats/correctness.json"
            ).read_text()
        ).get("cases", [])
        case = next(
            (
                row
                for row in cases
                if isinstance(row, dict)
                and row.get("expectedStatus", 200) == 200
                and isinstance(row.get("expected"), dict)
                and "error" not in row["expected"]
            ),
            None,
        )
        if case is None or "input" not in case or not case["expected"]:
            raise ValueError(
                "CLI word-stats corpus lacks a deterministic successful case"
            )
        url = (
            inputs.resource(self.endpoint)
            if isinstance(self.endpoint, Resource)
            else self.endpoint
        )
        self.evidence_dir.mkdir(parents=True, exist_ok=False)
        observations: dict[str, dict[str, object]] = {}
        identity: dict[str, str] | None = None
        original: int | None = None
        for artifact in artifacts:
            directory = self.evidence_dir / artifact.mode
            directory.mkdir()
            config = directory / "cli-config.yaml"
            config.write_text(
                yaml.safe_dump(
                    {
                        "currentContext": "owned",
                        "contexts": {"owned": {"endpoint": url}},
                    }
                )
            )
            prefix = (str(artifact.binary), "--config", str(config), "--endpoint", url)
            with (directory / "commands.jsonl").open("x") as stream:
                executor = _EvidenceExecutor(self.executor, stream)
                snapshot_task = runtime_config_tasks(
                    _NAMESPACE,
                    patch={_FIELD: 999999},
                    invalid_patch={_FIELD: -1},
                    cli_argv=prefix,
                    executor=executor,
                    role="host",
                    cwd=staged.source_dir,
                )[0]
                snapshot_task.title = "Read CLI runtime baseline"
                pod: str | None = None
                try:
                    before, pod = self._snapshot(inputs, executor)
                    if identity is not None and before != identity:
                        raise RuntimeError(
                            "CLI control-plane instance was replaced between passes"
                        )
                    identity = before
                    self._platform_checks(inputs, executor, directory / "before")
                    self._log_task(inputs, executor, pod, directory, "before").run(
                        inputs
                    )
                    snapshot = _runtime_snapshot(snapshot_task, inputs)
                    rate = snapshot["namespaces"][_NAMESPACE].get(_FIELD)
                    if (
                        type(rate) is not int
                        or rate < 1
                        or (original is not None and rate != original)
                    ):
                        raise RuntimeError(
                            "CLI runtime baseline is invalid or was not restored"
                        )
                    original = rate
                    patch = 999998 if rate == 999999 else 999999
                    absence = CliFunctionListTask(
                        {},
                        absent=(self.function,),
                        cli_argv=prefix,
                        executor=executor,
                        role="host",
                        cwd=staged.source_dir,
                    )
                    absence.title = "Initial CLI function absence"
                    absence.run(inputs)
                    k8s_function_resources_absent(
                        function=self.function,
                        namespace=self.namespace,
                        executor=executor,
                        role="host",
                        timeout_seconds=45,
                    ).run(inputs)
                    request = CliWorkflowRequest(
                        functions=(
                            CliFunction(
                                self.function,
                                report.function("word-stats", "java").image.reference,
                                json.dumps(case["input"]),
                                resources=self.function_resources,
                                expected_output=case["expected"],
                            ),
                        ),
                        namespace=self.namespace,
                        endpoint=url,
                        binary=str(artifact.binary),
                        runtime_config_namespace=_NAMESPACE,
                        config_file=config,
                    )
                    workflow = Workflow(workflow_id=f"cli-{artifact.mode}")
                    resources = add_cli_contract(
                        workflow,
                        request,
                        executor=executor,
                        cwd=staged.source_dir,
                        readiness_timeout_seconds=45,
                        strict=True,
                        runtime_config_patch={_FIELD: patch},
                    )
                    workflow.add(
                        self._log_task(
                            inputs, executor, pod, directory, "after-requests"
                        ),
                        requires=resources,
                    )
                    workflow.run()
                    # First absence probe protects acquisition; the last proves release.
                    executor.results.pop(
                        f"Wait for {self.function} resources to disappear", None
                    )
                    k8s_function_resources_absent(
                        function=self.function,
                        namespace=self.namespace,
                        executor=executor,
                        role="host",
                        timeout_seconds=45,
                    ).run(inputs)
                    after, after_pod = self._snapshot(inputs, executor)
                    self._log_task(
                        inputs, executor, after_pod, directory, "after-release"
                    ).run(inputs)
                    if after != identity:
                        raise RuntimeError(
                            "CLI control-plane instance was replaced during a pass"
                        )
                    self._platform_checks(inputs, executor, directory / "after")
                    observations[artifact.mode] = _observations(
                        executor.results,
                        function=self.function,
                        namespace=self.namespace,
                        report=report,
                        version=artifact.version,
                    )
                    current = _runtime_snapshot(snapshot_task, inputs)
                    runtime_config_patch_task(
                        _NAMESPACE,
                        {_FIELD: original},
                        expected_revision=current["revision"],
                        cli_argv=prefix,
                        executor=executor,
                        role="host",
                        cwd=staged.source_dir,
                    ).run(inputs)
                    runtime_config_readback_task(
                        _NAMESPACE,
                        {_FIELD: original},
                        cli_argv=prefix,
                        executor=executor,
                        role="host",
                        cwd=staged.source_dir,
                    ).run(inputs)
                    restored_identity, restored_pod = self._snapshot(inputs, executor)
                    self._log_task(
                        inputs, executor, restored_pod, directory, "after-restore"
                    ).run(inputs)
                    if restored_identity != identity:
                        raise RuntimeError(
                            "CLI control-plane instance was replaced "
                            "during baseline restore"
                        )
                except Exception as error:
                    if pod is not None:
                        try:
                            self._log_task(
                                inputs, executor, pod, directory, "failure"
                            ).run(inputs)
                        except Exception as logs_error:
                            error.add_note(
                                f"CLI post-failure log capture failed: {logs_error}"
                            )
                    if original is not None:
                        try:
                            current = _runtime_snapshot(snapshot_task, inputs)
                            if current["namespaces"][_NAMESPACE][_FIELD] != original:
                                runtime_config_patch_task(
                                    _NAMESPACE,
                                    {_FIELD: original},
                                    expected_revision=current["revision"],
                                    cli_argv=prefix,
                                    executor=executor,
                                    role="host",
                                    cwd=staged.source_dir,
                                ).run(inputs)
                                runtime_config_readback_task(
                                    _NAMESPACE,
                                    {_FIELD: original},
                                    cli_argv=prefix,
                                    executor=executor,
                                    role="host",
                                    cwd=staged.source_dir,
                                ).run(inputs)
                        except Exception as cleanup:
                            error.add_note(
                                f"CLI runtime baseline restoration failed: {cleanup}"
                            )
                    (directory / "failure.json").write_text(
                        json.dumps(
                            {
                                "error": str(error),
                                "notes": getattr(error, "__notes__", []),
                            }
                        )
                        + "\n"
                    )
                    raise
            (directory / "observations.json").write_text(
                json.dumps(observations[artifact.mode], indent=2) + "\n"
            )
        for artifact in artifacts:
            verify_cli_artifact(staged.source_dir, artifact)
        if (
            source != captured_source_state(staged.source_dir)
            or recipe_hash != hashlib.sha256(staged.recipe.read_bytes()).hexdigest()
        ):
            raise RuntimeError("CLI qualification changed frozen tracked inputs")
        equal = len(observations) == 2 and observations["jvm"] == observations["native"]
        if self.runtime == "parity" and not equal:
            raise RuntimeError("Native and JVM CLI semantic observations differ")
        receipt_name = "parity.json" if self.runtime == "parity" else "contracts.json"
        with (self.evidence_dir / receipt_name).open("x") as stream:
            json.dump(
                {
                    "runtime": self.runtime,
                    "modes": list(observations),
                    "equal": equal,
                    "platform": identity,
                    "source": source,
                    "recipeSha256": recipe_hash,
                },
                stream,
                indent=2,
            )
            stream.write("\n")
        return TaskOutcome()

    @override
    def _fingerprint_payload(self) -> object:
        return {
            "runtime": self.runtime,
            "namespace": self.namespace,
            "function": self.function,
            "evidence": str(self.evidence_dir),
        }
