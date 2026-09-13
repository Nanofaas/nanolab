"""Release retained soak resources from a validated local Sonata journal."""

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sonata_engine import (
    JournalConfig,
    Resource,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
    release_retained,
)
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.soak.build_executor import OwnedBuildCommandExecutor
from nanolab.tasks.soak.owned_functions import FunctionOwnership, delete_owned_function
from nanolab.tasks.soak.retention import CLEANUP_JOURNAL, compose_file_fingerprint

Command = Callable[[tuple[str, ...], Path, Mapping[str, str]], bytes]
_LIMIT = 8 * 1024 * 1024


class LocalCleanupCommands:
    """Run bounded local cleanup commands, pinning the local Docker daemon."""

    def __init__(self, run_dir: Path, timeout_s: float, artifact_limit: int) -> None:
        """Bind command limits without opening files or contacting Docker."""
        self.executor = OwnedBuildCommandExecutor(
            cwd=run_dir.absolute(),
            log_dir=run_dir.absolute() / ("teardown-" + uuid4().hex),
            cancelled=Event(),
            timeout_cap_s=timeout_s,
            artifact_limit_bytes=artifact_limit,
            command_output_limit_bytes=min(1024 * 1024, artifact_limit),
            env={"DOCKER_CONTEXT": "", "DOCKER_HOST": "unix:///var/run/docker.sock"},
        )

    def __call__(
        self, argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]
    ) -> bytes:
        """Return bounded captured output only after successful owned cleanup."""
        if argv[0] != "docker":
            raise ValueError("cleanup command transport accepts Docker only")
        actual = ("docker", "--host", "unix:///var/run/docker.sock", *argv[1:])
        result = self.executor.run(
            CommandTaskSpec(
                task_id="soak-owned-cleanup",
                summary="Release owned soak resources",
                argv=actual,
                options=CommandOptions(
                    cwd=cwd,
                    env={
                        **env,
                        "COMPOSE_REMOVE_ORPHANS": "0",
                        "COMPOSE_PROFILES": "",
                        "COMPOSE_DISABLE_ENV_FILE": "1",
                    },
                ),
            )
        )
        if not result.ok or self.executor.last_log_path is None:
            raise RuntimeError(f"owned cleanup command failed: {result.stderr}")
        with self.executor.last_log_path.open("rb") as stream:
            body = stream.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("owned cleanup output exceeds its byte bound")
        return body


def _json(body: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate cleanup JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"invalid cleanup JSON number: {value}")

    return json.loads(body, object_pairs_hook=pairs, parse_constant=invalid)


def read_cleanup_records(
    journal: Path,
    *,
    include_released: bool = False,
) -> list[dict[str, Any]]:
    """Validate the whole bounded journal before selecting outstanding ownership."""
    if journal.is_symlink() or not journal.is_file():
        raise ValueError("soak teardown requires an existing regular journal")
    with journal.open("rb") as stream:
        body = stream.read(_LIMIT + 1)
    if len(body) > _LIMIT:
        raise ValueError("soak journal exceeds the cleanup byte bound")
    retained = {}
    history = {}
    released = set()
    for line in body.splitlines():
        if not line.strip():
            continue
        record = _json(line)
        if not isinstance(record, dict) or record.get("schema_version") != 3:
            raise ValueError("unsupported Sonata journal record")
        kind = record.get("kind")
        if kind not in {"retained", "released"}:
            continue
        title = record.get("resource")
        if not isinstance(title, str) or not title:
            raise ValueError("retained resource has no identity")
        if kind == "released":
            previous = retained.get(title)
            if previous is not None and any(
                record.get(key) != previous.get(key)
                for key in ("workflow_id", "run_id")
            ):
                raise ValueError("release belongs to another journal run")
            released.add(title)
            retained.pop(title, None)
        elif title in released:
            raise ValueError("historical resource title re-retention is unsupported")
        elif title in retained:
            raise ValueError("ambiguous duplicate retained resource")
        else:
            if type(record.get("order")) is not int or record["order"] < 0:
                raise ValueError("invalid retained resource order")
            retained[title] = record
            history[title] = record
    records = list((history if include_released else retained).values())
    if records:
        runs = {(item.get("workflow_id"), item.get("run_id")) for item in records}
        if len(runs) != 1 or any(
            not isinstance(part, str) or not part for part in next(iter(runs))
        ):
            raise ValueError("retained resources do not belong to a single journal run")
        orders = [item["order"] for item in records]
        if len(orders) != len(set(orders)):
            raise ValueError("ambiguous retained release order")
    return sorted(records, key=lambda item: item["order"])


def _compose(value: object, run_dir: Path) -> tuple[DockerComposeProject, Path]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "cwd",
            "project",
            "compose_sha256",
        }
        or value["schema"] != "nanolab-soak-owned-compose-v1"
    ):
        raise ValueError("unsupported retained Compose record; no resources removed")
    raw = value["project"]
    if not isinstance(raw, dict) or set(raw) != {
        "name",
        "file",
        "ready_url",
        "build",
        "role",
        "env",
    }:
        raise ValueError("invalid retained Compose project fields")
    if raw["build"] is not False or raw["role"] != "host":
        raise ValueError("teardown requires a frozen local Compose project")
    if (
        not isinstance(raw["name"], str)
        or re.fullmatch(r"soak-[a-f0-9]{32}", raw["name"]) is None
    ):
        raise ValueError("retained Compose project lacks a unique soak identity")
    if not isinstance(value["cwd"], str) or not Path(value["cwd"]).is_absolute():
        raise ValueError("retained Compose cwd must be absolute")
    cwd = Path(value["cwd"])
    if not isinstance(raw["file"], str):
        raise ValueError("retained Compose file must be a path")
    path = Path(raw["file"])
    path = path if path.is_absolute() else cwd / path
    root = run_dir.resolve()
    if ".." in path.parts or not path.resolve().is_relative_to(root):
        raise ValueError("retained Compose file escapes its run directory")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("retained Compose path contains a symlink")
    env = raw["env"]
    if not isinstance(env, dict) or any(
        not isinstance(key, str)
        or not isinstance(item, str)
        or key.startswith("DOCKER_")
        or key.startswith("COMPOSE_")
        for key, item in env.items()
    ):
        raise ValueError("retained Compose environment contains unsafe overrides")
    project = DockerComposeProject(
        name=raw["name"],
        file=path,
        ready_url=raw["ready_url"],
        build=False,
        role="host",
        env=env,
    )
    if value["compose_sha256"] != compose_file_fingerprint(project, cwd):
        raise ValueError("owned Compose file changed since acquisition")
    return project, cwd


def _compose_argv(project: DockerComposeProject) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-f",
        str(project.file),
        "-p",
        project.name,
    )


def verify_function_owner(ownership: FunctionOwnership, *, command: Command) -> None:
    """Prove the exact continuously running CP before accepting API absence.

    The runtime must capture ID and StartedAt after deployment and catalog
    readiness, before registration. A restart requires authoritative recovery;
    this verifier deliberately cannot certify a restored/replaced catalog.
    """
    inspected = _json(
        command(
            ("docker", "inspect", ownership.control_plane_container_id),
            Path(ownership.cwd),
            {},
        )
    )
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise ValueError("owned control-plane inspection is incomplete")
    target = inspected[0]
    labels = target.get("Config", {}).get("Labels", {})
    state = target.get("State", {})
    if (
        target.get("Id") != ownership.control_plane_container_id
        or state.get("StartedAt") != ownership.control_plane_started_at
        or state.get("Running") is not True
        or state.get("Restarting") is True
        or labels.get("com.docker.compose.project") != ownership.project_name
        or labels.get("com.docker.compose.service") != "control-plane"
        or target.get("Config", {}).get("Image") != ownership.control_plane_image
    ):
        raise ValueError("control-plane identity or catalog continuity is unconfirmed")
    endpoint = urlsplit(ownership.api_endpoint)
    ports = target.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp", []) or []
    if not any(
        port.get("HostIp") == endpoint.hostname
        and port.get("HostPort") == str(endpoint.port)
        for port in ports
    ):
        raise ValueError("retained API endpoint is not owned by this control plane")


def _guard_platform(
    command: Command,
    project: DockerComposeProject,
    cwd: Path,
    identities: tuple[FunctionOwnership, ...],
) -> None:
    if not identities:
        return
    raw = command(
        (*_compose_argv(project), "ps", "-q", "control-plane"), cwd, project.env
    )
    identifiers = raw.decode().split()
    if (
        len(identifiers) != 1
        or re.fullmatch(r"[a-f0-9]{12,64}", identifiers[0]) is None
    ):
        raise ValueError("owned control plane is unavailable or ambiguous")
    inspected = _json(command(("docker", "inspect", identifiers[0]), cwd, project.env))
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise ValueError("owned control-plane inspection is incomplete")
    target = inspected[0]
    labels = target.get("Config", {}).get("Labels", {})
    if (
        labels.get("com.docker.compose.project") != project.name
        or labels.get("com.docker.compose.service") != "control-plane"
        or target.get("State", {}).get("Running") is not True
    ):
        raise ValueError("control plane no longer matches retained ownership")
    ports = target.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp", []) or []
    for identity in identities:
        if not identity.control_plane_container_id.startswith(identifiers[0]):
            raise ValueError("Compose selected a different control-plane container")
        verify_function_owner(identity, command=command)
        endpoint = urlsplit(identity.api_endpoint)
        if target.get("Config", {}).get("Image") != identity.control_plane_image:
            raise ValueError("control-plane image differs from retained function owner")
        if not any(
            port.get("HostIp") == endpoint.hostname
            and port.get("HostPort") == str(endpoint.port)
            for port in ports
        ):
            raise ValueError("retained API endpoint is not owned by this control plane")


class TeardownSoakTask(Task[tuple[str, ...]]):
    """Validate all ownership first, then replay the journal's release order."""

    title = "Release retained soak resources"
    idempotent = False

    def __init__(
        self,
        run_dir: Path,
        *,
        command: Command,
        delete_function: Callable[[FunctionOwnership], None] | None = None,
    ) -> None:
        """Bind a retained run and injectable cleanup transports."""
        self.run_dir = run_dir.absolute()
        self.command = command
        self.delete_function = delete_function

    def run(self, inputs: TaskInputs) -> TaskOutcome[tuple[str, ...]]:
        """Validate retained ownership before attempting any release."""
        owned_journal = self.run_dir / CLEANUP_JOURNAL
        journal = JournalConfig(
            path=(
                owned_journal
                if owned_journal.exists() or owned_journal.is_symlink()
                else self.run_dir / "journal.jsonl"
            )
        )
        records = read_cleanup_records(journal.path)
        workflow_journal = self.run_dir / "journal.jsonl"
        if journal.path == owned_journal and (
            workflow_journal.exists() or workflow_journal.is_symlink()
        ):
            history = {
                item["resource"]: item["value"]
                for item in read_cleanup_records(journal.path, include_released=True)
            }
            for item in read_cleanup_records(workflow_journal):
                if (
                    item["resource"] not in history
                    or item.get("value") != history[item["resource"]]
                ):
                    raise ValueError("workflow journal contains untracked ownership")
        if not records:
            return TaskOutcome(value=())
        compose_records = [
            item
            for item in records
            if isinstance(item.get("value"), dict)
            and item["value"].get("schema") == "nanolab-soak-owned-compose-v1"
        ]
        if len(compose_records) != 1:
            raise ValueError(
                "teardown requires exactly one retained owned Compose project"
            )
        compose_record = compose_records[0]
        project, cwd = _compose(compose_record["value"], self.run_dir)
        identities = {}
        for item in records:
            if item is compose_record:
                continue
            identity = FunctionOwnership.from_record(item.get("value"))
            if identity.project_name != project.name or identity.cwd != str(cwd):
                raise ValueError("retained function belongs to another platform")
            if item["order"] >= compose_record["order"]:
                raise ValueError("retained function would outlive its control plane")
            identities[item["resource"]] = identity
        if len({identity.name for identity in identities.values()}) != len(identities):
            raise ValueError("duplicate retained function identity")
        _guard_platform(self.command, project, cwd, tuple(identities.values()))
        failures = []
        resources = {}
        for title, identity in identities.items():

            def release_function(_inputs, value, identity=identity):
                try:
                    if value != identity.record():
                        raise ValueError("function ownership changed during teardown")
                    if self.delete_function is not None:
                        self.delete_function(identity)
                    else:
                        delete_owned_function(
                            identity,
                            verify_owner=lambda owner: verify_function_owner(
                                owner,
                                command=self.command,
                            ),
                        )
                except BaseException as error:
                    failures.append(error)
                    raise

            resources[title] = Resource(
                title=title,
                acquire=lambda _inputs: None,
                release=release_function,
            )

        def release_compose(_inputs, value):
            if failures:
                raise RuntimeError(
                    "preserving platform because function cleanup is unconfirmed"
                )
            current, current_cwd = _compose(value, self.run_dir)
            if current != project or current_cwd != cwd:
                raise ValueError("Compose ownership changed during teardown")
            self.command(
                (*_compose_argv(project), "down", "--volumes"),
                cwd,
                project.env,
            )

        resources[compose_record["resource"]] = Resource(
            title=compose_record["resource"],
            acquire=lambda _inputs: None,
            release=release_compose,
        )
        return TaskOutcome(value=release_retained(resources, journal))


# Removing the containers, network and volumes after the last one stops.
_CLEANUP_REMOVAL_MARGIN_S = 30.0


def cleanup_timeout_s(cancellation_timeout_s: float) -> float:
    """Return how long `compose down` may take, given the stop grace.

    The soak declares the stop grace as its cancellation budget, so every
    container may take that long to stop before Docker kills it, and the
    removals still follow. Using the bare budget timed out mid-stop and left
    the platform running under a "cleanup unconfirmed" failure.
    """
    return float(cancellation_timeout_s) + _CLEANUP_REMOVAL_MARGIN_S


def build_teardown_workflow(
    config,
    environment,
    bindings,
    *,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path,
) -> Workflow:
    """Construct cleanup without Docker calls, source builds or journal reads."""
    if config.workflow != "soak" or config.soak is None:
        raise ValueError("soak teardown requires a soak scenario")
    if environment.provider != "local":
        raise ValueError("soak teardown requires the local environment")
    commands = LocalCleanupCommands(
        run_dir,
        timeout_s=cleanup_timeout_s(config.soak.cancellation_timeout_s),
        artifact_limit=min(config.soak.artifact_limit_bytes, _LIMIT),
    )
    return Workflow(workflow_id="soak-teardown").add(
        TeardownSoakTask(run_dir, command=commands),
    )
