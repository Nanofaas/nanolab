"""Checks that a workload's declared resource limits reached the running object."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.core.fingerprint import fingerprint_digest
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import TaskResult

from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.kubectl import KubectlTask
from nanolab.tasks.managed_containers import inspect_managed_container

ResourceSpec = Mapping[str, Any]


class ContainerdResourceCheckTask(Task[None]):
    """Read the OCI spec from containerd and compare declared resource limits."""

    def __init__(
        self,
        *,
        function: str,
        replica: int,
        resources: ResourceSpec | None,
        run: RootlessRun,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        """Record the expected resources and the test-owned containerd runtime."""
        self.title = f"Inspect resources of {function} replica {replica}"
        self._function = function
        self._replica = replica
        self._resources = resources
        self._run = run
        self._executor = executor
        self._role = role
        self._cwd = cwd

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Fail when the actual OCI CPU or memory values differ from the manifest."""
        subject = f"{self._function} replica {self._replica}"
        outcome = CommandTask(
            title=self.title,
            argv=(
                "bash",
                str(self._run.script),
                "inspect-owned",
                self._run.run_id,
                str(self._run.repo_root),
                self._function,
                str(self._replica),
            ),
            executor=self._executor,
            role=self._role,
            options=CommandOptions(cwd=self._cwd),
        ).run(inputs)
        if outcome.value is None:
            raise RuntimeError(f"{subject}: containerd inspect returned no result")
        payload = _payload(outcome.value, subject)
        identifier = payload.get("ID", payload.get("id"))
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError(f"{subject}: containerd inspect returned no ID")
        if self._resources is None:
            return TaskOutcome(value=None)
        spec = payload.get("Spec") or payload.get("spec") or {}
        actual = spec.get("linux", {}).get("resources", {})
        if not isinstance(actual, dict):
            raise RuntimeError(f"{identifier}: no OCI Linux resources")
        requests, limits = _halves(self._resources)
        cpu = actual.get("cpu") or {}
        memory = actual.get("memory") or {}
        expected: dict[str, int] = {}
        if requests.get("cpu") is not None:  # nosec B113: resource mapping, not HTTP
            expected["cpu.shares"] = max(
                2, int(Decimal(str(requests["cpu"])) * 1024 + Decimal("0.5"))
            )
        if limits.get("cpu") is not None:
            expected["cpu.quota"] = int(Decimal(str(limits["cpu"])) * 100000)
            expected["cpu.period"] = 100000
        if requests.get("memoryMiB") is not None:  # nosec B113: resource mapping, not HTTP
            expected["memory.reservation"] = int(requests["memoryMiB"]) * 1024 * 1024
        if limits.get("memoryMiB") is not None:
            expected["memory.limit"] = int(limits["memoryMiB"]) * 1024 * 1024
        _compare(
            {
                f"{scope}.{name}": (cpu if scope == "cpu" else memory).get(name)
                for scope, name in (key.split(".") for key in expected)
            },
            expected,
            identifier,
        )
        return TaskOutcome(value=None)


def _payload(result: TaskResult, subject: str) -> dict[str, Any]:
    try:
        parsed = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(
            f"{subject} was not JSON: {result.stdout[:200]!r}"
        ) from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{subject} was not a JSON object: {result.stdout[:200]!r}")
    return parsed


def _compare(
    actual: Mapping[str, Any], expected: Mapping[str, Any], subject: str
) -> None:
    """Report the first field that differs, by name.

    The shell version this replaces joined four values into one line and ran
    `test "$actual" = "$expected"`, so a failure told you only that something was
    wrong — never which limit the control plane had failed to apply.
    """
    for field, want in expected.items():
        got = actual.get(field)
        if got != want:
            raise RuntimeError(f"{subject}: {field} is {got!r}, expected {want!r}")


def _halves(resources: ResourceSpec) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    if not isinstance(requests, Mapping) or not isinstance(limits, Mapping):
        raise RuntimeError(f"resource spec must hold mappings, got {resources!r}")
    return requests, limits


class ContainerResourceCheckTask(Task[None]):
    """Read a container's host config and assert the declared limits landed on it.

    `role` has no default on purpose: running this on the host and running it
    inside a VM are different checks against different daemons, and a default
    would hide that decision at the call site.

    With no resource spec the task still verifies the selected running replica.
    """

    def __init__(
        self,
        *,
        function: str,
        replica: int,
        resources: ResourceSpec | None,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        """Translate the spec into Docker host-config fields and compare against them.

        With no spec the task checks the running replica identity.
        """
        self.title = f"Inspect resources of {function} replica {replica}"
        self._function = function
        self._replica = replica
        self._executor = executor
        self._role: ExecutionRole = role
        self._cwd = cwd
        self._expected: dict[str, int] | None = None
        if resources is not None:
            requests, limits = _halves(resources)
            # `requests` is the spec's resource-request dict, not the HTTP library.
            request_memory = int(requests.get("memoryMiB") or 0)  # nosec B113
            limit_memory = int(limits.get("memoryMiB") or 0)
            expected = {
                # Docker floors CPU shares at 2; the control plane rounds half up.
                # `requests` is the resource-request dict, not the HTTP library.
                "CpuShares": max(
                    2,
                    int(float(str(requests.get("cpu", 0))) * 1024 + 0.5),  # nosec B113
                ),
                "NanoCpus": int(float(str(limits.get("cpu", 0))) * 1_000_000_000),
                # A reservation equal to the limit is left unset rather than restated.
                "MemoryReservation": (
                    0
                    if request_memory == limit_memory
                    else request_memory * 1024 * 1024
                ),
                "Memory": limit_memory * 1024 * 1024,
            }

            self._expected = expected

    def _fingerprint_payload(self) -> object:
        return {
            "version": "nanolab.container-resources:v3",
            "function": self._function,
            "replica": self._replica,
            "expected": self._expected,
            "role": self._role,
            "binding_key": self._executor.binding_key(self._role),
            "cwd": str(self._cwd) if self._cwd else None,
        }

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Check the running replica's identity and its declared resource limits."""
        observed = inspect_managed_container(
            function=self._function,
            replica=self._replica,
            executor=self._executor,
            role=self._role,
            inputs=inputs,
            cwd=self._cwd,
        )
        if self._expected is not None:
            actual = observed.get("HostConfig")
            if not isinstance(actual, dict):
                raise RuntimeError(f"{observed['Id']}: no container host config")
            _compare(actual, self._expected, observed["Id"])
        return TaskOutcome(value=None)


def _k8s_cpu(value: object) -> str:
    number = float(str(value))
    return str(int(number)) if number.is_integer() else f"{int(number * 1000)}m"


class K8sResourceCheckTask(KubectlTask):
    """Read a Deployment and assert the declared limits reached its container.

    Kubernetes inspection uses the common KubectlTask primitive.
    """

    def __init__(
        self,
        *,
        deployment: str,
        namespace: str,
        resources: ResourceSpec | None,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        """Translate the spec into Kubernetes resource fields and compare against them.

        With no spec the task only checks that the Deployment exists.
        """
        verify: Callable[[TaskResult], None] | None = None
        # Set with the check below, for the same reason: one spec, one key.
        semantic_key: str | None = None
        if resources is not None:
            requests, limits = _halves(resources)
            # `requests`/`limits` are the spec's resource dicts, not the HTTP library.
            expected = {
                "requests.cpu": _k8s_cpu(requests.get("cpu", 0)),  # nosec B113
                "requests.memory": f"{requests.get('memoryMiB', 0)}Mi",  # nosec B113
                "limits.cpu": _k8s_cpu(limits.get("cpu", 0)),
                "limits.memory": f"{limits.get('memoryMiB', 0)}Mi",
            }

            def check_deployment(result: TaskResult) -> None:
                payload = _payload(result, "deployment")
                try:
                    container = payload["spec"]["template"]["spec"]["containers"][0]
                    declared = container["resources"]
                except (KeyError, IndexError, TypeError) as error:
                    raise RuntimeError(
                        f"{deployment}: no container resources "
                        "in the deployment payload"
                    ) from error
                actual = {
                    f"{half}.{key}": (declared.get(half) or {}).get(key)
                    for half in ("requests", "limits")
                    for key in ("cpu", "memory")
                }
                _compare(actual, expected, deployment)

            verify = check_deployment
            semantic_key = (
                f"nanolab.k8s-resources:v2:{fingerprint_digest({'expected': expected})}"
            )

        super().__init__(
            "get",
            "deployment",
            deployment,
            "-o=json",
            executor=executor,
            role=role,
            namespace=namespace,
            title=f"Inspect resources of {deployment}",
            options=CommandOptions(cwd=cwd),
            verify=verify,
            semantic_key=semantic_key,
        )
