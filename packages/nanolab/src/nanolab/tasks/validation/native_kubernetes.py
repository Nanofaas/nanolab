"""Qualify native Kubernetes artifacts through their actual API and runtime."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from sonata_engine import TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.http import endpoint_argv

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
from nanolab.tasks.recipes.kubernetes import _command, _json_command

QUOTA_ENV = "NANOFAAS_INVOCATION_CAPACITY_EXECUTIONS_PER_FUNCTION"


class _ApiCommands:
    """Bound existing curl tasks and retain their real responses as evidence."""

    def __init__(self, executor: CommandTaskExecutor, stream: TextIO | None) -> None:
        self.executor = executor
        self.stream = stream
        self.written = 0

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
                "1048576",
                *argv[1:],
            )
        result = self.executor.run(
            replace(task, argv=("timeout", "--kill-after=5s", "60s", *argv)),
            dry_run=dry_run,
        )
        if len(result.stdout.encode()) > 1024 * 1024:
            raise RuntimeError("native API response exceeds its byte bound")
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
            status.get("function") != name
            or status.get("desiredReplicas") != 1
            or status.get("readyReplicas") != 1
        ):
            raise RuntimeError("native GET replicas response differs from request")
        HttpFunctionContractTask(
            name,
            payload=function.payload,
            endpoint=endpoint,
            executor=bounded,
            role=role,
            cwd=cwd,
            expectation=HttpFunctionExpectation(
                status=200, api_status="success", status_code=200
            ),
        ).run(inputs)
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
