"""Qualify native Kubernetes artifacts through their actual API and runtime."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, TextIO
from uuid import uuid4

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.http import endpoint_argv
from sonata_tasks.kubectl import kubectl_port_forward_resource, owned_deployment_pods
from sonata_tasks.minikube import MinikubeTarget

from nanolab.assets.diagnostics.native_k8s_runtime import (
    LOG_LIMIT,
    NATIVE_ERRORS,
    PROCFS_SCRIPT,
    RUNTIME_LIMIT,
    cri_pid,
    parse_procfs,
    verify_native_runtime,
)
from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.http_function import (
    Endpoint,
    HttpFunctionContractTask,
    HttpFunctionExpectation,
    HttpFunctionReplicaStatusTask,
    HttpFunctionSetReplicasTask,
    _parse_contract_response,
    _split_final_response,
)
from nanolab.tasks.platform import PlatformFunction
from nanolab.tasks.recipes.kubernetes import (
    RecipeKubernetesImageCheckTask,
    _command,
    _json_command,
)
from nanolab.tasks.recipes.validation import RecipeMetadataCheckTask
from nanolab.tasks.recipes.workflow import RecipeDistribution
from nanolab.workspace.paths import bundled_assets_root

QUOTA_ENV = "NANOFAAS_INVOCATION_CAPACITY_EXECUTIONS_PER_FUNCTION"


class _ApiCommands:
    """Bound existing curl tasks and retain their real responses as evidence."""

    def __init__(
        self,
        executor: CommandTaskExecutor,
        stream: TextIO | None,
        *,
        max_bytes: int = RUNTIME_LIMIT,
    ) -> None:
        self.executor = executor
        self.stream = stream
        self.written = 0
        self.max_bytes = max_bytes

    def binding_key(self, role: str) -> str:
        return self.executor.binding_key(role)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        argv = task.argv
        if argv[0] == "curl":
            argv = (
                "curl",
                "--connect-timeout",
                "5",
                "--max-time",
                "30",
                "--max-filesize",
                str(self.max_bytes),
                *argv[1:],
            )
        result = self.executor.run(
            replace(task, argv=("timeout", "--kill-after=5s", "60s", *argv)),
            dry_run=dry_run,
        )
        if (
            max(len(result.stdout.encode()), len(result.stderr.encode()))
            > self.max_bytes
        ):
            raise RuntimeError("native API response exceeds its byte bound")
        if self.stream is None:
            return result
        record = (
            json.dumps(
                {
                    "title": task.summary,
                    "argv": argv,
                    "returnCode": result.return_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            + "\n"
        )
        self.written += len(record.encode())
        if self.written > 8 * 1024 * 1024:
            raise RuntimeError("native API evidence exceeds its byte bound")
        if self.stream is not None:
            self.stream.write(record)
            self.stream.flush()
        return result


def check_native_api(
    inputs: TaskInputs,
    *,
    function: PlatformFunction,
    endpoint: Endpoint,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None,
    evidence_file: Path | None = None,
) -> None:
    """Validate fresh registration, patch, replicas, invocation and deletion."""
    stream = evidence_file.open("x", encoding="utf-8") if evidence_file else None
    bounded = _ApiCommands(executor, stream)
    name = function.name

    def request(
        method: str, path: str, expected_status: int, payload: str | None = None
    ) -> dict[str, object]:
        result = (
            CommandTask(
                title=f"Native API {method} {path}",
                semantic_key=f"native-api:v1:{method}:{path}:{expected_status}",
                argv=endpoint_argv(
                    endpoint,
                    lambda base: (
                        "curl",
                        "-isS",
                        "-X",
                        method,
                        "-H",
                        "Content-Type: application/json",
                        *(("--data", payload) if payload is not None else ()),
                        f"{base}/v1/{path}",
                    ),
                ),
                executor=bounded,
                role=role,
                options=CommandOptions(cwd=cwd),
            )
            .run(inputs)
            .value
        )
        if result is None:
            raise RuntimeError("native API returned no response")
        if expected_status == 204:
            headers, body = _split_final_response(result.stdout)
            if (
                not headers.splitlines()
                or headers.splitlines()[0].split()[1] != "204"
                or body.strip()
            ):
                raise RuntimeError("native DELETE did not return an empty HTTP 204")
            return {}
        status, _, response = _parse_contract_response(name, result.stdout)
        if status != expected_status:
            raise RuntimeError(
                f"native API {method} returned {status}, expected {expected_status}"
            )
        return response

    def verify_function(response: dict[str, object]) -> None:
        if (
            response.get("name") != name
            or response.get("image") != function.image
            or response.get("requestedExecutionMode") != "DEPLOYMENT"
        ):
            raise RuntimeError("native function response differs from owned manifest")

    def register() -> None:
        verify_function(request("POST", "functions", 201, function.manifest().json()))

    try:
        verify_function(request("GET", f"functions/{name}", 200))
        request("DELETE", f"functions/{name}", 204)
        register()
        patched = request(
            "PATCH", f"functions/{name}", 200, '{"concurrency":1,"timeoutMs":5000}'
        )
        verify_function(patched)
        if patched.get("concurrency") != 1 or patched.get("timeoutMs") != 5000:
            raise RuntimeError("native PATCH response differs from request")
        HttpFunctionSetReplicasTask(
            name, replicas=1, endpoint=endpoint, executor=bounded, role=role, cwd=cwd
        ).run(inputs)
        HttpFunctionReplicaStatusTask(
            name, replicas=1, endpoint=endpoint, executor=bounded, role=role, cwd=cwd
        ).run(inputs)
        status = request("GET", f"functions/{name}/replicas", 200)
        if (
            status.get("name") != name
            or status.get("desiredReplicas") != 1
            or status.get("readyReplicas") != 1
        ):
            raise RuntimeError("native GET replicas response differs from request")
        invocation = (
            HttpFunctionContractTask(
                name,
                payload=function.payload,
                endpoint=endpoint,
                executor=bounded,
                role=role,
                cwd=cwd,
                expectation=HttpFunctionExpectation(status=200, api_status="success"),
            )
            .run(inputs)
            .value
        )
        if invocation is None:
            raise RuntimeError("native invocation returned no response")
        _, _, body = _parse_contract_response(name, invocation.stdout)
        if body.get("error") is not None:
            raise RuntimeError("native invocation returned an error envelope")
        status_code = body.get("statusCode")
        if status_code is not None and (
            type(status_code) is not int or status_code != 200
        ):
            raise RuntimeError("native invocation statusCode differs from HTTP success")
        request("DELETE", f"functions/{name}", 204)
        register()
        HttpFunctionReplicaStatusTask(
            name, replicas=1, endpoint=endpoint, executor=bounded, role=role, cwd=cwd
        ).run(inputs)
    finally:
        if stream is not None:
            stream.close()


def configure_native_quota(
    inputs: TaskInputs,
    *,
    deployment: dict[str, Any],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    prefix: tuple[str, ...],
) -> dict[str, Any]:
    """Change only the owned control plane's quota, with an atomic UID guard."""
    name = deployment["metadata"]["name"]
    current = _json_command(
        executor,
        inputs,
        "Verify native quota Deployment owner",
        (*prefix, "get", "deployment", name, "-o", "json"),
        role=role,
    )
    uid = deployment["metadata"].get("uid")
    if not uid or current.get("metadata", {}).get("uid") != uid:
        raise RuntimeError("native quota Deployment was replaced")
    version = current["metadata"].get("resourceVersion")
    if not isinstance(version, str) or not version:
        raise RuntimeError("native quota Deployment has no resource version")
    containers = current["spec"]["template"]["spec"]["containers"]
    indices = [
        index
        for index, row in enumerate(containers)
        if row.get("name") == "control-plane"
    ]
    if len(indices) != 1:
        raise RuntimeError("native quota control-plane container is ambiguous")
    index = indices[0]
    env = [
        row for row in containers[index].get("env", []) if row.get("name") != QUOTA_ENV
    ]
    env.append({"name": QUOTA_ENV, "value": "1"})
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": version},
        {
            "op": "add",
            "path": f"/spec/template/spec/containers/{index}/env",
            "value": env,
        },
    ]
    updated = _json_command(
        executor,
        inputs,
        "Set owned native invocation quota",
        (
            *prefix,
            "patch",
            "deployment",
            name,
            "--type=json",
            "-p",
            json.dumps(patch),
            "-o",
            "json",
        ),
        role=role,
    )
    if updated.get("metadata", {}).get("uid") != uid:
        raise RuntimeError("native quota Deployment was replaced during patch")
    _command(
        executor,
        inputs,
        "Await owned native quota rollout",
        (*prefix, "rollout", "status", f"deployment/{name}", "--timeout=50s"),
        role=role,
    )
    return updated


def _native_attempt_directory(run_dir: Path) -> Path:
    native = run_dir / "native"
    if native.is_symlink():
        raise RuntimeError("native evidence directory must not be a symlink")
    if native.exists():
        if not native.is_dir():
            raise RuntimeError("native evidence path is not a directory")
        attempts = run_dir / "native-attempts"
        if attempts.is_symlink():
            raise RuntimeError("native attempt archive must not be a symlink")
        attempts.mkdir(parents=True, exist_ok=True)
        native.rename(attempts / uuid4().hex)
    native.mkdir(parents=True)
    return native


class NativeKubernetesLifecycleTask(Task[None]):
    """Qualify the validated native distribution after ordinary queue checks."""

    def __init__(
        self,
        distribution: Resource[RecipeDistribution],
        *,
        namespace: str,
        function: PlatformFunction,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        run_dir: Path,
        function_component: tuple[str, str],
        target: Resource[MinikubeTarget] | None = None,
        assets: Path | None = None,
        remote_root: PurePosixPath | None = None,
    ) -> None:
        """Bind gates to the run's distribution and Kubernetes resources."""
        self.title = "Qualify native Kubernetes lifecycle"
        self.distribution = distribution
        self.namespace = namespace
        self.function = function
        self.endpoint = endpoint
        self.executor = executor
        self.role: ExecutionRole = role
        self.run_dir = run_dir
        self.function_component = function_component
        self.target = target
        self.assets = assets or bundled_assets_root() / "diagnostics"
        self.remote_root = remote_root

    def _prefix(self, inputs: TaskInputs) -> tuple[str, ...]:
        context = (
            ("--context", inputs.resource(self.target).context) if self.target else ()
        )
        return ("kubectl", *context, "-n", self.namespace)

    def _snapshot(
        self, inputs: TaskInputs, uid: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        executor = _ApiCommands(self.executor, None)
        prefix = self._prefix(inputs)
        deployment = _json_command(
            executor,
            inputs,
            "Read owned native Deployment",
            (*prefix, "get", "deployment", "nanofaas-control-plane", "-o", "json"),
            role=self.role,
        )
        if deployment.get("metadata", {}).get("uid") != uid:
            raise RuntimeError("native Deployment was replaced")
        sets = _json_command(
            executor,
            inputs,
            "Read native ReplicaSets",
            (*prefix, "get", "replicasets", "-o", "json"),
            role=self.role,
        ).get("items", [])
        pods = _json_command(
            executor,
            inputs,
            "Read native Pods",
            (*prefix, "get", "pods", "-o", "json"),
            role=self.role,
        ).get("items", [])
        owned = [
            pod
            for pod in owned_deployment_pods(deployment, sets, pods)
            if not pod.get("metadata", {}).get("deletionTimestamp")
        ]
        if len(owned) != 1 or owned[0].get("status", {}).get("phase") != "Running":
            raise RuntimeError("native Deployment must have one running owned Pod")
        return deployment, owned[0]

    def _logs(self, inputs: TaskInputs, pod: dict[str, Any], label: str) -> str:
        text = _command(
            _ApiCommands(self.executor, None, max_bytes=LOG_LIMIT),
            inputs,
            f"Read native {label} logs",
            (
                *self._prefix(inputs),
                "logs",
                pod["metadata"]["name"],
                "-c",
                "control-plane",
                f"--limit-bytes={LOG_LIMIT + 1}",
            ),
            role=self.role,
        )
        (self.run_dir / "native" / f"logs-{label}.txt").write_text(text)
        if not text or any(error in text for error in NATIVE_ERRORS):
            raise RuntimeError(
                "native logs are missing or contain native registration errors"
            )
        return text

    def _node_argv(
        self, inputs: TaskInputs, node: str, argv: tuple[str, ...]
    ) -> tuple[str, ...]:
        if self.target:
            target = inputs.resource(self.target)
            if node not in target.nodes:
                raise RuntimeError("native Pod runs on an unknown Minikube node")
            return (
                "minikube",
                "-p",
                target.profile,
                "ssh",
                "--node",
                node,
                "--",
                shlex.join(argv),
            )
        return argv

    @staticmethod
    def _identity(pod: dict[str, Any]) -> dict[str, Any]:
        statuses = [
            row
            for row in pod.get("status", {}).get("containerStatuses", [])
            if row.get("name") == "control-plane"
        ]
        if len(statuses) != 1 or not statuses[0].get("ready"):
            raise RuntimeError("native container is missing, ambiguous or not ready")
        container_id = statuses[0].get("containerID", "")
        if not re.fullmatch(r"containerd://[0-9a-f]{64}", container_id):
            raise RuntimeError("native CRI container ID is missing or not immutable")
        identity = {
            "podUid": pod.get("metadata", {}).get("uid"),
            "containerId": container_id,
            "node": pod.get("spec", {}).get("nodeName"),
        }
        if not all(isinstance(value, str) and value for value in identity.values()):
            raise RuntimeError("native Pod identity is incomplete")
        return identity

    def _probe(
        self,
        inputs: TaskInputs,
        pod: dict[str, Any],
        uid: str,
        *,
        logs_before: str,
        logs_after: str,
    ) -> None:
        before = self._identity(pod)
        node, container_id = (
            before["node"],
            before["containerId"].removeprefix("containerd://"),
        )
        executor = _ApiCommands(self.executor, None)
        cri = ("sudo", "crictl") if self.target else ("sudo", "k3s", "crictl")
        inspect_argv = self._node_argv(
            inputs, node, (*cri, "inspect", "-o", "json", container_id)
        )
        inspect_before = _json_command(
            executor, inputs, "Inspect native CRI process", inspect_argv, role=self.role
        )
        pid = cri_pid(
            inspect_before, container_id=container_id, pod_uid=before["podUid"]
        )
        raw = _command(
            executor,
            inputs,
            "Read native procfs threads",
            self._node_argv(
                inputs,
                node,
                ("sudo", "sh", "-c", PROCFS_SCRIPT, "native-procfs", str(pid)),
            ),
            role=self.role,
        )
        process = parse_procfs(raw)
        inspect_after = _json_command(
            executor, inputs, "Recheck native CRI process", inspect_argv, role=self.role
        )
        after_pid = cri_pid(
            inspect_after, container_id=container_id, pod_uid=before["podUid"]
        )
        _, after_pod = self._snapshot(inputs, uid)
        after = self._identity(after_pod)
        containers = [
            row
            for row in pod["spec"]["containers"]
            if row.get("name") == "control-plane"
        ]
        if len(containers) != 1:
            raise RuntimeError("native Pod container is ambiguous")
        container = containers[0]
        node_info = _json_command(
            executor,
            inputs,
            "Read native Pod architecture",
            (*self._prefix(inputs), "get", "node", node, "-o", "json"),
            role=self.role,
        )
        architecture = (
            node_info.get("status", {}).get("nodeInfo", {}).get("architecture")
        )
        if architecture not in ("arm64", "amd64"):
            raise RuntimeError("native Pod architecture is unknown")
        observation = {
            "before": {**before, "pid": pid, "startTime": process["before"]},
            "after": {**after, "pid": after_pid, "startTime": process["after"]},
            "commandLine": process["commandLine"],
            "threads": process["threads"],
            "cpuLimit": container.get("resources", {}).get("limits", {}).get("cpu"),
            "logsBefore": logs_before,
            "logsAfter": logs_after,
            "architecture": architecture,
            "pod": pod,
            "criBefore": inspect_before,
            "criAfter": inspect_after,
        }
        # Logs have their own byte bound and are saved separately from runtime JSON.
        runtime = {
            key: value
            for key, value in observation.items()
            if not key.startswith("logs")
        }
        encoded = json.dumps(runtime, indent=2)
        if len(encoded.encode()) > RUNTIME_LIMIT:
            raise RuntimeError("native runtime JSON exceeds byte bound")
        (self.run_dir / "native/runtime.json").write_text(encoded + "\n")
        verify_native_runtime(
            observation, expected_workers=1, require_epoll=architecture == "arm64"
        )

    def _quota(self, inputs: TaskInputs, endpoint: Endpoint) -> None:
        local = self.run_dir / "native/quota.json"
        output = (
            Path(str(self.remote_root / "native-quota.json"))
            if self.remote_root
            else local
        )
        task = CommandTask(
            title="Burst native invocation quota",
            semantic_key=f"native-invocation-quota:v1:{self.function.name}",
            argv=endpoint_argv(
                endpoint,
                lambda url: (
                    "python3",
                    str(self.assets / "native_quota_burst.py"),
                    "--url",
                    url,
                    "--function",
                    self.function.name,
                    "--out",
                    str(output),
                ),
            ),
            executor=_ApiCommands(self.executor, None),
            role=self.role,
        )
        try:
            task.run(inputs)
        finally:
            if self.remote_root:
                text = _command(
                    _ApiCommands(self.executor, None, max_bytes=4 * RUNTIME_LIMIT),
                    inputs,
                    "Retain native quota receipt",
                    ("cat", str(output)),
                    role=self.role,
                )
                local.write_text(text)

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Execute native gates and release the replacement API forward on failure."""
        report = inputs.resource(self.distribution)
        if report.control_plane().mode != "native":
            return TaskOutcome()
        initial = self.run_dir / "k8s-image-control-plane-control-plane.json"
        if not initial.is_file() or initial.stat().st_size > LOG_LIMIT:
            raise RuntimeError("native initial image evidence is missing or oversized")
        uid = (
            json.loads(initial.read_text())
            .get("deployment", {})
            .get("metadata", {})
            .get("uid")
        )
        if not isinstance(uid, str) or not uid:
            raise RuntimeError("native initial image evidence has no Deployment UID")
        family, sdk = self.function_component
        component = report.function(family, sdk)
        if (family, sdk, component.mode) != ("word-stats", "java", "jvm"):
            raise RuntimeError(
                "native qualification requires the ordinary JVM word-stats function"
            )
        function = replace(self.function, image=component.image.reference)
        native_dir = _native_attempt_directory(self.run_dir)
        deployment, before_pod = self._snapshot(inputs, uid)
        self._identity(before_pod)
        logs_before = self._logs(inputs, before_pod, "before-quota")
        check_native_api(
            inputs,
            function=function,
            endpoint=self.endpoint,
            executor=self.executor,
            role=self.role,
            cwd=None,
            evidence_file=native_dir / "api.jsonl",
        )
        configure_native_quota(
            inputs,
            deployment=deployment,
            executor=_ApiCommands(self.executor, None),
            role=self.role,
            prefix=self._prefix(inputs),
        )
        updated, after_pod = self._snapshot(inputs, uid)
        (native_dir / "quota-rollout.json").write_text(
            json.dumps(
                {
                    "before": before_pod,
                    "after": after_pod,
                    "deployment": updated,
                    "setting": {QUOTA_ENV: "1"},
                },
                indent=2,
            )
            + "\n"
        )
        target = self.target
        forward = (
            kubectl_port_forward_resource(
                namespace=self.namespace,
                resource="service/control-plane",
                remote_port=8080,
                log_path=native_dir / "api-port-forward.log",
                context=lambda current: current.resource(target).context,
            )
            if target
            else None
        )
        forward_endpoint = forward.acquire(inputs) if forward else None
        endpoint = forward_endpoint if forward_endpoint is not None else self.endpoint
        try:
            bounded = _ApiCommands(self.executor, None)
            RecipeMetadataCheckTask(
                self.distribution,
                executor=bounded,
                run_dir=native_dir,
                endpoint=endpoint,
                role=self.role,
            ).run(inputs)
            RecipeKubernetesImageCheckTask(
                self.distribution,
                namespace=self.namespace,
                deployment="nanofaas-control-plane",
                component=("control-plane", "control-plane", "java"),
                executor=bounded,
                role=self.role,
                run_dir=native_dir,
                target=self.target,
            ).run(inputs)
            self._quota(inputs, endpoint)
            logs_after = self._logs(inputs, after_pod, "after-quota")
            self._probe(
                inputs, after_pod, uid, logs_before=logs_before, logs_after=logs_after
            )
            (native_dir / "qualified.json").write_text(
                json.dumps(
                    {
                        "mode": "native",
                        "deploymentUid": uid,
                        "functionImage": function.image,
                        "source": report.source,
                    }
                )
                + "\n"
            )
        finally:
            if forward and forward_endpoint is not None:
                forward.release(inputs, forward_endpoint)
        return TaskOutcome()

    def _fingerprint_payload(self) -> object:
        return {
            "namespace": self.namespace,
            "function": self.function.name,
            "component": self.function_component,
            "distribution": self.distribution.title,
            "contract": "native-k8s:v1",
        }
