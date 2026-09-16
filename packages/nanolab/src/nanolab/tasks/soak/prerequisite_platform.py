"""Fresh local prerequisite platforms, using the existing owned soak workflow.

Integration (parent, after preparation; no builds occur here)::

    factory = make_prerequisite_platform_factory(
        prepared=prepared, ownership_root=root, bindings=bindings)
    runner = make_live_runner(inputs=frozen_inputs, factory=factory)
    # Install runner in PreparationOptions.prerequisite_runner.
    # After EACH worker has exited and been reaped, including failed acquisition:
    result = factory.recover(lifetime_id, worker_reaped=True)
    # Do not claim cleanup unless result.cleanup_confirmed is True.

Every relevant_config profile must include effective_config, produced by
freeze_effective_config with DECLARED retention settings. These are expectations,
not observations. Runtime settings, function manifests and bound retention are
independently observed before returning that configuration to LivePlatform.
Recipe fields remain frozen test inputs and are explicitly recorded as such.

Recovery reuses the retained-resource teardown, then checks actual project
absence. A kill before the complete-acquisition marker is UNSUPPORTED: existing
resources cannot prove ownership of every create-before-receipt side effect.
This module does not promise recovery on parent death, arbitrary journals,
remote Docker, managed function deployments, or missing authoritative metrics.
The parent must retain this factory instance and enforce its worker reap gate.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import httpx
from sonata_engine import JournalConfig, Task, TaskInputs, TaskOutcome

from nanolab.plans.soak import compose_frozen_soak_workflow
from nanolab.tasks.soak.artifacts import ArtifactWriter, describe_artifact, fingerprint
from nanolab.tasks.soak.collector import _docker_get
from nanolab.tasks.soak.prerequisite_runtime import (
    LivePlatform,
    UnsupportedPreflightError,
)
from nanolab.tasks.soak.runtime import (
    create_local_deployment,
    observe_local_process,
    retention_from_configprops,
)
from nanolab.tasks.soak.teardown import (
    LocalCleanupCommands,
    TeardownSoakTask,
    read_cleanup_records,
)

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DIGEST = re.compile(r"[^\s@]+@sha256:[a-f0-9]{64}")
_RETENTION = {
    "unkeyed-sync-outcome",
    "terminal-key-and-readable-outcome",
    "live-key-and-execution",
}
_RECIPE = {
    "function",
    "role",
    "request",
    "expected_output",
    "expected_error_code",
    "headers",
    "request_timeout_s",
    "exercise_timeout_s",
    "poll_interval_s",
    "callback_result",
    "function_spec",
    "churn_count",
    "effective_config",
}
_LIMIT = 1024 * 1024


def _roles(config):
    return {
        name: {
            "runtime": role.runtime,
            "runtime_options": list(role.runtime_options),
            "cpu": role.expected_cpu,
            "memory_bytes": role.memory_limit_bytes,
        }
        for name, role in config.roles.items()
    }


def freeze_effective_config(prepared, *, retention_s: dict[str, float]) -> dict:
    """Make expected settings from policy, not a fabricated observed receipt."""
    if set(retention_s) != _RETENTION or any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
        for value in retention_s.values()
    ):
        raise UnsupportedPreflightError(
            "all three declared retention settings are required"
        )
    return {"roles": _roles(prepared.config), "retention_s": deepcopy(retention_s)}


def _publish(path: Path, value: dict) -> None:
    body = (json.dumps(value, allow_nan=False, sort_keys=True) + "\n").encode()
    if len(body) > _LIMIT:
        raise ValueError("platform ownership record exceeds bound")
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("symlink ownership record is unsupported")
    with path.open("rb") as stream:
        body = stream.read(_LIMIT + 1)
    if len(body) > _LIMIT:
        raise ValueError("platform ownership record exceeds bound")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("platform ownership record is not an object")
    return result


def _process_start(pid: int) -> str | None:
    try:
        body = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return body.rsplit(")", 1)[1].split()[19]


async def _wait(event: Event, timeout_s: float) -> None:
    async with asyncio.timeout(timeout_s):
        while not event.is_set():
            await asyncio.sleep(0.02)


async def _json(client, url: str) -> dict:
    async with client.stream(
        "GET", url, timeout=10, follow_redirects=False
    ) as response:
        if response.status_code != 200:
            raise UnsupportedPreflightError(
                f"effective configuration {url}: HTTP {response.status_code}"
            )
        body = bytearray()
        async for part in response.aiter_bytes():
            body.extend(part)
            if len(body) > _LIMIT:
                raise UnsupportedPreflightError(
                    f"effective configuration {url}: response too large"
                )
    result = json.loads(body)
    if not isinstance(result, dict):
        raise UnsupportedPreflightError(
            f"effective configuration {url}: expected object"
        )
    return result


def _matches_owned_function_manifest(observed: dict, manifest: dict) -> bool:
    mode = manifest.get("executionMode")
    return all(
        observed.get(key) == value
        for key, value in manifest.items()
        if key != "executionMode"
    ) and (
        "executionMode" not in manifest
        or (
            observed.get("requestedExecutionMode") == mode
            and observed.get("effectiveExecutionMode") == mode
        )
    )


@dataclass(frozen=True)
class RecoveryResult:
    """Report whether parent recovery confirmed all owned resources absent."""

    lifetime_id: str
    cleanup_confirmed: bool
    status: str
    reason: str
    evidence: str | None = None


class PrerequisitePlatformFactory:
    """Inert parent-owned factory; each context owns one complete local workflow.

    Worker acquisition/release bounds must also be configured on the strict
    runner. Its release deadline should exceed release_timeout_s. Timed-out
    workflow threads are never interpreted as stopped Docker resources.
    """

    _lifetime_budgets: dict[str, tuple[int, int]]

    def __init__(
        self,
        *,
        prepared,
        ownership_root: Path,
        bindings,
        docker_socket: str = "/var/run/docker.sock",
        acquire_timeout_s: float = 60,
        release_timeout_s: float = 30,
    ):
        """Freeze factory settings without acquiring platform resources."""
        if docker_socket != "/var/run/docker.sock":
            raise UnsupportedPreflightError(
                "owned workflow/recovery supports only the local default Docker socket"
            )
        for timeout in (acquire_timeout_s, release_timeout_s):
            if (
                type(timeout) not in (int, float)
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError("platform timeouts must be finite and positive")
        self.prepared = replace(prepared, config=prepared.config.model_copy(deep=True))
        self.images = dict(prepared.images)
        if set(self.images) != set(prepared.config.roles) or any(
            not isinstance(image, str) or _DIGEST.fullmatch(image) is None
            for image in self.images.values()
        ):
            raise UnsupportedPreflightError(
                "prepared role images must be immutable RepoDigests"
            )
        if "control-plane" not in self.images or len(self.images) < 2:
            raise UnsupportedPreflightError("a control-plane and SDK are required")
        if any(
            role.runtime not in {"jvm", "node"}
            for role in prepared.config.roles.values()
        ):
            raise UnsupportedPreflightError(
                "local process configuration requires JVM or Node"
            )
        self.root = Path(ownership_root).absolute()
        self.bindings = bindings
        self.docker_socket = docker_socket
        self.acquire_timeout_s = acquire_timeout_s
        self.release_timeout_s = release_timeout_s
        self.parent_pid = os.getpid()
        self.token = uuid4().hex
        self._lifetime_budgets = {}

    def validate_inputs(self, inputs: dict) -> None:
        """Validate frozen expectations; no resource or observation is fabricated."""
        if inputs.get("images") != self.images:
            raise UnsupportedPreflightError(
                "prerequisite images differ from prepared receipts"
            )
        configs = inputs.get("relevant_config")
        if not isinstance(configs, dict) or not configs:
            raise UnsupportedPreflightError("frozen prerequisite profiles are missing")
        settings = set()
        for coverage, profile in configs.items():
            if not isinstance(profile, dict) or set(profile) - _RECIPE:
                raise UnsupportedPreflightError(
                    f"{coverage}: unsupported relevant configuration fields"
                )
            role = profile.get("role")
            if (
                role == "control-plane"
                or role not in self.images
                or profile.get("function") != role
            ):
                raise UnsupportedPreflightError(
                    f"{coverage}: local deployment requires its SDK role function"
                )
            effective = profile.get("effective_config")
            if not isinstance(effective, dict) or set(effective) != {
                "roles",
                "retention_s",
            }:
                raise UnsupportedPreflightError(
                    f"{coverage}/effective_config: "
                    "frozen roles and retention_s required"
                )
            expected = freeze_effective_config(
                self.prepared, retention_s=effective["retention_s"]
            )
            if effective != expected:
                raise UnsupportedPreflightError(
                    f"{coverage}/effective_config: settings differ from prepared policy"
                )
            settings.add(fingerprint(effective))
        if len(settings) != 1:
            raise UnsupportedPreflightError(
                "profiles require the same frozen platform settings"
            )

    def _directory(self, lifetime_id: str) -> Path:
        if not isinstance(lifetime_id, str) or _NAME.fullmatch(lifetime_id) is None:
            raise ValueError("invalid prerequisite lifetime identity")
        directory = self.root / lifetime_id
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise ValueError("prerequisite ownership path contains a symlink")
        return directory

    def assign_lifetime_budget(
        self,
        lifetime_id: str,
        *,
        artifact_limit_bytes: int,
        recovery_limit_bytes: int,
    ) -> None:
        """Reserve bounded artifact and recovery storage for one lifetime."""
        if not lifetime_id:
            raise ValueError("lifetime_id must not be empty")
        limits = (artifact_limit_bytes, recovery_limit_bytes)
        if any(
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
            for limit in limits
        ):
            raise ValueError("lifetime budgets must be positive integers")
        global_limit = self.prepared.config.artifact_limit_bytes
        if any(limit > global_limit for limit in limits):
            raise ValueError(
                "lifetime budgets must not exceed the global artifact limit"
            )
        if lifetime_id in self._lifetime_budgets:
            raise ValueError(f"budget already assigned for lifetime {lifetime_id!r}")
        self._lifetime_budgets[lifetime_id] = limits

    @asynccontextmanager
    async def __call__(self, coverage_id: str, lifetime_id: str, inputs: dict):
        """Acquire and release one isolated platform for a frozen profile."""
        self.validate_inputs(inputs)
        if coverage_id not in inputs["relevant_config"]:
            raise UnsupportedPreflightError("coverage absent from frozen inputs")
        frozen = deepcopy(inputs)
        directory = self._directory(lifetime_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        project_name = "soak-" + uuid4().hex
        intent = {
            "schema": "nanolab-prerequisite-platform-v1",
            "factory_token": self.token,
            "lifetime_id": lifetime_id,
            "coverage_id": coverage_id,
            "project_name": project_name,
            "worker_pid": os.getpid(),
            "worker_start": _process_start(os.getpid()),
            "inputs_sha256": fingerprint(frozen),
            "images": self.images,
        }
        _publish(directory / "intent.json", intent)
        writer = ArtifactWriter(
            directory / "evidence",
            self._lifetime_budgets.get(
                lifetime_id,
                (
                    self.prepared.config.artifact_limit_bytes,
                    8 * _LIMIT,
                ),
            )[0],
        )
        isolated = replace(
            self.prepared, run_id=project_name, evidence_dir=writer.root, writer=writer
        )
        ready, finish, done = Event(), Event(), Event()
        failures = []
        thread = None
        body_error = None
        try:
            writer.write_json("frozen-inputs.json", frozen)
            deployment = create_local_deployment(
                isolated,
                directory,
                docker_socket=self.docker_socket,
                allow_diagnostic_target_stop_on_cancel=True,
            )
            manifests = {
                function.name: function.manifest().body()
                for function in deployment.request.functions
            }
            for profile in frozen["relevant_config"].values():
                manifest = manifests[profile["function"]]
                if manifest.get("executionMode") != "EXTERNAL":
                    raise UnsupportedPreflightError(
                        "prerequisites cannot own managed function containers"
                    )
                spec = profile.get("function_spec")
                if spec is not None and any(
                    spec.get(key) != manifest.get(key)
                    for key in ("executionMode", "image", "endpointUrl")
                ):
                    raise UnsupportedPreflightError(
                        "churn must remain on the same owned EXTERNAL SDK"
                    )

            class HoldPlatform(Task):
                title = "Hold isolated prerequisite platform"
                observer = None

                def stop_observer(self):
                    """No background measurement observer is created by this task."""

                def run(self, inputs):
                    _publish(directory / "acquired.json", intent)
                    ready.set()
                    finish.wait()
                    return TaskOutcome(value=lifetime_id)

            workflow = compose_frozen_soak_workflow(
                deployment.request,
                self.bindings,
                project=deployment.project,
                measurement=HoldPlatform(),
                api_endpoint=deployment.api_endpoint,
                ownership=deployment.ownership,
                cwd=directory,
                workflow_id="prerequisite-" + lifetime_id,
            )

            def run_workflow():
                try:
                    workflow.run(
                        journal=JournalConfig(path=directory / "workflow.jsonl")
                    )
                except BaseException as error:
                    failures.append(error)
                finally:
                    ready.set()
                    done.set()

            _publish(directory / "workflow-starting.json", intent)
            thread = Thread(
                target=run_workflow, name="prerequisite-platform", daemon=True
            )
            thread.start()
            await _wait(ready, self.acquire_timeout_s)
            if failures:
                raise failures[0]
            if not (directory / "acquired.json").is_file():
                raise UnsupportedPreflightError(
                    "platform workflow did not acquire its complete resources"
                )
            targets = await asyncio.to_thread(deployment.discover)

            def identities():
                actual = deployment.discover()
                if actual != targets:
                    raise UnsupportedPreflightError(
                        "prerequisite process identity changed"
                    )
                return {target.role: target.image_digest for target in actual}

            async with httpx.AsyncClient(
                base_url=deployment.api_endpoint, trust_env=False
            ) as client:

                async def observe_identities():
                    return await asyncio.to_thread(identities)

                async def observe_config():
                    await observe_identities()
                    roles = {}
                    for target in targets:
                        actual = await asyncio.to_thread(observe_local_process, target)
                        roles[target.role] = {
                            key: actual.get(key)
                            for key in _roles(isolated.config)[target.role]
                        }
                        decision = (
                            (deployment.diagnostic_inputs or {})
                            .get("roles", {})
                            .get(target.role, {})
                        )
                        controller = decision.get("controller")
                        if controller:
                            path = Path(controller["path"])
                            container = await asyncio.to_thread(
                                _docker_get,
                                f"/containers/{target.container_id}/json",
                                self.docker_socket,
                                5,
                            )
                            mounts = [
                                m
                                for m in container.get("Mounts", [])
                                if m.get("Destination")
                                == "/opt/nanolab/node-diagnostic-control.cjs"
                            ]
                            if (
                                path.is_symlink()
                                or describe_artifact(path) != controller
                                or len(mounts) != 1
                                or any(
                                    mounts[0].get(key) != value
                                    for key, value in {
                                        "Type": "bind",
                                        "Source": str(path),
                                        "RW": False,
                                    }.items()
                                )
                            ):
                                raise UnsupportedPreflightError(
                                    f"{target.role}/controller: "
                                    "owned preload identity differs"
                                )
                        writer.append(
                            "configuration-observations",
                            {"role": target.role, "observed": actual},
                        )
                    endpoint = deployment.metrics_endpoints["control-plane"]
                    if not endpoint or not endpoint.endswith("/actuator/prometheus"):
                        raise UnsupportedPreflightError(
                            "control-plane/configprops: management endpoint unavailable"
                        )
                    document = await _json(
                        client, endpoint.removesuffix("prometheus") + "configprops"
                    )
                    writer.append(
                        "configuration-observations",
                        {"source": "actuator/configprops", "document": document},
                    )
                    effective = {
                        "roles": roles,
                        "retention_s": retention_from_configprops(document),
                    }
                    result = {}
                    for coverage, recipe in frozen["relevant_config"].items():
                        name = recipe["function"]
                        observed = await _json(client, "/v1/functions/" + name)
                        if not _matches_owned_function_manifest(
                            observed, manifests[name]
                        ):
                            raise UnsupportedPreflightError(
                                f"{coverage}/function: actual manifest differs "
                                "from owned deployment"
                            )
                        writer.append(
                            "configuration-observations",
                            {"source": "function-api", "observed": observed},
                        )
                        result[coverage] = {
                            **deepcopy(recipe),
                            "function": observed["name"],
                            "effective_config": deepcopy(effective),
                        }
                    writer.append(
                        "configuration-observations",
                        {
                            "frozen_recipe_fields_not_observations": sorted(
                                _RECIPE - {"effective_config", "function"}
                            )
                        },
                    )
                    return result

                yield LivePlatform(
                    lifetime_id,
                    client,
                    {
                        role: endpoint
                        for role, endpoint in deployment.metrics_endpoints.items()
                        if endpoint is not None
                    },
                    observe_identities,
                    observe_config,
                    {
                        target.role: "control-plane"
                        if target.role == "control-plane"
                        else {"jvm": "java", "node": "javascript"}[target.runtime]
                        for target in targets
                    },
                    record=lambda event: writer.append("runtime-observations", event),
                )
        except BaseException as error:
            body_error = error
            raise
        finally:
            finish.set()
            try:
                if thread is not None:
                    await _wait(done, self.release_timeout_s)
                    thread.join(timeout=0)
                    if failures:
                        raise RuntimeError(
                            "owned workflow release/acquisition unconfirmed: "
                            f"{failures[0]}"
                        )
                writer.close()
            except BaseException as error:
                if body_error is not None:
                    body_error.add_note(f"parent recovery required: {error}")
                else:
                    raise RuntimeError(f"parent recovery required: {error}") from error

    def recover(
        self, lifetime_id: str, *, worker_reaped: bool = False
    ) -> RecoveryResult:
        """Parent-only cleanup audit/replay AFTER reap; no implicit release success."""
        if os.getpid() != self.parent_pid or worker_reaped is not True:
            return RecoveryResult(
                lifetime_id,
                False,
                "unsupported",
                "parent must reap the worker before recovery",
            )
        directory = self._directory(lifetime_id)
        try:
            intent = _read(directory / "intent.json")
            if (
                intent.get("factory_token") != self.token
                or intent.get("lifetime_id") != lifetime_id
                or intent.get("images") != self.images
            ):
                raise ValueError("ownership intent belongs to another factory/lifetime")
            pid = intent.get("worker_pid")
            if type(pid) is not int or pid <= 0 or not intent.get("worker_start"):
                raise ValueError("worker identity is unavailable")
            if _process_start(pid) == intent["worker_start"]:
                raise ValueError("worker is still present; reap is unconfirmed")
            if not (directory / "workflow-starting.json").exists():
                result = RecoveryResult(
                    lifetime_id,
                    True,
                    "confirmed",
                    "workflow acquisition was never started",
                )
            elif not (directory / "acquired.json").exists():
                result = RecoveryResult(
                    lifetime_id,
                    False,
                    "unsupported",
                    "partial acquisition/create-before-receipt "
                    "cannot be authoritatively recovered",
                )
            else:
                if _read(directory / "acquired.json") != intent:
                    raise ValueError(
                        "complete-acquisition identity differs from intent"
                    )
                history = read_cleanup_records(
                    directory / "cleanup.jsonl", include_released=True
                )
                projects = [
                    entry["value"]
                    for entry in history
                    if entry.get("value", {}).get("schema")
                    == "nanolab-soak-owned-compose-v1"
                ]
                if (
                    len(projects) != 1
                    or projects[0]["project"]["name"] != intent["project_name"]
                ):
                    raise ValueError(
                        "journal does not establish this exact owned project"
                    )
                command = LocalCleanupCommands(
                    directory,
                    timeout_s=10,
                    artifact_limit=self._lifetime_budgets.get(
                        lifetime_id,
                        (
                            self.prepared.config.artifact_limit_bytes,
                            8 * _LIMIT,
                        ),
                    )[1],
                )
                TeardownSoakTask(directory, command=command).run(TaskInputs.empty())
                if read_cleanup_records(directory / "cleanup.jsonl"):
                    raise ValueError("retained resource cleanup remains outstanding")
                for argv in (
                    ("docker", "ps", "-aq"),
                    ("docker", "network", "ls", "-q"),
                    ("docker", "volume", "ls", "-q"),
                ):
                    remaining = command(
                        (
                            *argv,
                            "--filter",
                            "label=com.docker.compose.project="
                            + intent["project_name"],
                        ),
                        directory,
                        {},
                    )
                    if remaining.strip():
                        raise ValueError("owned Docker resources remain after teardown")
                result = RecoveryResult(
                    lifetime_id,
                    True,
                    "confirmed",
                    "retained releases and actual project absence confirmed",
                )
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            result = RecoveryResult(lifetime_id, False, "unconfirmed", str(error))
        if directory.is_dir():
            path = directory / ("recovery-" + uuid4().hex + ".json")
            _publish(
                path,
                {
                    "lifetime_id": result.lifetime_id,
                    "cleanup_confirmed": result.cleanup_confirmed,
                    "status": result.status,
                    "reason": result.reason,
                },
            )
            result = replace(result, evidence=str(path))
        return result


def make_prerequisite_platform_factory(**kwargs) -> PrerequisitePlatformFactory:
    """Build an inert factory; wire its recover method into the parent reap path."""
    return PrerequisitePlatformFactory(**kwargs)
