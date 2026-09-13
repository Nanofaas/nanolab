"""Durable ownership and fail-closed cleanup of generated soak resources."""

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from sonata_engine import JournalConfig, Resource, TaskInputs
from yaml.tokens import AliasToken, AnchorToken, TagToken

from nanolab.tasks.compose import DockerComposeProject

CLEANUP_JOURNAL = "cleanup.jsonl"


class CleanupState:
    """Share one failure gate and authoritative ownership journal across adapters.

    Pass the same instance to Compose and every function adapter. ``journal``
    must be run_dir/cleanup.jsonl, distinct from Sonata's workflow journal.
    Acquisition successes are fsynced here even for non-kept workflows; only
    confirmed releases get tombstones. Standalone teardown replays this file.
    A new acquisition run must use a new directory, never reopen old ownership.

    This does not replace the runtime's crash/partial-acquisition manifest:
    unknown acquisition failures deliberately require authoritative recovery.
    """

    def __init__(self, journal: JournalConfig) -> None:
        """Configure ownership without creating files or invoking cleanup."""
        if journal.path.name != CLEANUP_JOURNAL or not journal.path.is_absolute():
            raise ValueError("cleanup journal must be absolute run_dir/cleanup.jsonl")
        self.journal = journal
        self.run_id = uuid4().hex
        self.failed = False
        self.records: dict[str, dict[str, Any]] = {}
        self._prepared = False
        self._titles: set[str] = set()
        self._replay = False

    @classmethod
    def restore(cls, journal: JournalConfig) -> "CleanupState":
        """Restore release-only state without reacquiring any runtime object."""
        from nanolab.tasks.soak.teardown import read_cleanup_records

        state = cls(journal)
        records = read_cleanup_records(journal.path)
        state.records = {record["resource"]: record for record in records}
        state._prepared = True
        state._replay = True
        return state

    def prepare(self) -> None:
        """Reserve a fresh journal before any resource acquisition side effect."""
        if self._replay:
            raise ValueError("restored cleanup state is release-only")
        if self._prepared:
            return
        self.journal.path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.path.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(self.journal.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._prepared = True

    def _append(self, record: dict[str, Any]) -> None:
        descriptor = os.open(
            self.journal.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def retain(self, title: str, value: dict[str, Any]) -> None:
        """Durably register one owned resource in reverse acquisition order."""
        self.prepare()
        if title in self._titles or len(self._titles) >= 1_000_000:
            self.failed = True
            raise ValueError("duplicate or excessive cleanup resource identity")
        record = {
            "schema_version": 3,
            "workflow_id": "soak-owned-cleanup",
            "run_id": self.run_id,
            "kind": "retained",
            "resource": title,
            "order": 1_000_000 - len(self._titles),
            "value": deepcopy(value),
        }
        self._titles.add(title)
        self.records[title] = record
        try:
            self._append(record)
        except BaseException:
            self.failed = True
            raise

    def released(self, title: str) -> None:
        """Record success only after cleanup and durable journal acknowledgement."""
        record = self.records[title]
        try:
            self._append({**record, "kind": "released"})
        except BaseException:
            self.failed = True
            raise
        del self.records[title]

    def unknown_acquisition(self, title: str, error: BaseException) -> None:
        """Preserve the platform without deleting an unproven function name."""
        self.failed = True
        try:
            self.retain(title, {"schema": "nanolab-soak-unknown-acquisition-v1"})
        except Exception as journal_error:
            error.add_note(f"Could not persist unknown acquisition: {journal_error}")
        error.add_note(
            "Acquisition ownership is unknown; authoritative recovery required"
        )

    def require_compose_release(self, title: str) -> None:
        """Keep CP and its catalog while any dependent cleanup is unconfirmed."""
        if self.failed or any(item != title for item in self.records):
            raise RuntimeError(
                "preserving platform because resource cleanup is unconfirmed"
            )


class _GeneratedLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("generated Compose requires unique string keys")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _validate_generated(body: bytes, root: Path, project_name: str) -> None:
    """Accept only self-contained Compose with project-scoped deletion targets."""
    if b"$" in body:
        raise ValueError("generated Compose must not contain interpolation")
    if any(
        isinstance(token, (AliasToken, AnchorToken, TagToken))
        for token in yaml.scan(body)
    ):
        raise ValueError("generated Compose must not contain YAML aliases or tags")
    value = yaml.load(body, Loader=_GeneratedLoader)
    if not isinstance(value, dict) or set(value) - {"services", "volumes", "networks"}:
        raise ValueError("generated Compose must be self-contained")
    for section in ("volumes", "networks"):
        entries = value.get(section, {})
        if not isinstance(entries, dict):
            raise ValueError(
                "generated Compose permits only project-scoped volumes/networks"
            )
        for name, options in entries.items():
            if isinstance(name, str) and options in ({}, None):
                continue
            if (
                section != "volumes"
                or not isinstance(options, dict)
                or re.fullmatch(r"diagnostic-tmp-[0-9]+", name) is None
                or set(options) != {"driver", "driver_opts", "labels"}
                or options["driver"] != "local"
                or options["labels"]
                != {"nanolab.run": project_name, "nanolab.diagnostic.tmp": "true"}
            ):
                raise ValueError(
                    "generated Compose permits only exact owned local diagnostic tmpfs"
                )
            driver = options["driver_opts"]
            if (
                not isinstance(driver, dict)
                or set(driver) != {"type", "device", "o"}
                or driver["type"] != "tmpfs"
                or driver["device"] != "tmpfs"
                or not isinstance(driver["o"], str)
            ):
                raise ValueError(
                    "generated Compose diagnostic tmpfs driver must be exact"
                )
            match = re.fullmatch(
                r"size=([1-9][0-9]*),uid=(0|[1-9][0-9]*),gid=(0|[1-9][0-9]*),mode=1777,noexec,nosuid,nodev",
                driver["o"],
            )
            if (
                match is None
                or not 4096 <= int(match[1]) <= 1024**3
                or int(match[1]) % 4096
            ):
                raise ValueError(
                    "generated Compose diagnostic tmpfs requires "
                    "finite page-aligned options"
                )
    services = value.get("services")
    if not isinstance(services, dict):
        raise ValueError("generated Compose requires services")
    allowed = {
        "image",
        "environment",
        "ports",
        "volumes",
        "networks",
        "depends_on",
        "command",
        "entrypoint",
        "restart",
        "healthcheck",
        "deploy",
        "mem_limit",
        "cpus",
        "read_only",
        "tmpfs",
        "stop_grace_period",
        "init",
        "user",
        "working_dir",
        "ulimits",
        "logging",
        "security_opt",
        "cap_drop",
        "cap_add",
    }
    diagnostic_mounts = {}
    for service in services.values():
        if not isinstance(service, dict) or set(service) - allowed:
            raise ValueError(
                "generated Compose contains unsupported service configuration"
            )
        if not isinstance(service.get("image"), str) or not service["image"]:
            raise ValueError("generated Compose services require explicit images")
        environment = service.get("environment", {})
        if not isinstance(environment, dict) or any(
            not isinstance(item, (str, int, float, bool))
            for item in environment.values()
        ):
            raise ValueError("generated Compose environment must be explicit")
        mounts = service.get("volumes", [])
        if not isinstance(mounts, list):
            raise ValueError("generated Compose mounts must be a list")
        for mount in mounts:
            if isinstance(mount, str):
                parts = mount.split(":")
                if len(parts) not in {2, 3}:
                    raise ValueError("anonymous or ambiguous Compose mount")
                source = parts[0]
            elif (
                isinstance(mount, dict)
                and set(mount)
                <= {
                    "type",
                    "source",
                    "target",
                    "read_only",
                    "volume",
                    "bind",
                }
                and mount.get("type") in {"bind", "volume"}
            ):
                source = mount.get("source")
            else:
                raise ValueError("unsupported generated Compose mount")
            if not isinstance(source, str) or not source:
                raise ValueError("generated Compose mounts require a source")
            if isinstance(mount, dict) and (
                (
                    "volume" in mount
                    and (
                        mount["type"] != "volume" or mount["volume"] != {"nocopy": True}
                    )
                )
                or (
                    "bind" in mount
                    and (
                        mount["type"] != "bind"
                        or mount["bind"] != {"create_host_path": False}
                    )
                )
            ):
                raise ValueError("unsupported generated Compose mount options")
            if source in value.get("volumes", {}):
                options = value["volumes"][source]
                if options:
                    match = re.fullmatch(
                        r"size=([1-9][0-9]*),uid=(0|[1-9][0-9]*),gid=(0|[1-9][0-9]*),mode=1777,noexec,nosuid,nodev",
                        options["driver_opts"]["o"],
                    )
                    if match is None:
                        raise ValueError("unsupported generated Compose tmpfs options")
                    if (
                        mount
                        != {
                            "type": "volume",
                            "source": source,
                            "target": "/tmp",
                            "volume": {"nocopy": True},
                        }
                        or service.get("user") != f"{match.group(2)}:{match.group(3)}"
                        or source in diagnostic_mounts
                        or sum(
                            (
                                m.get("target")
                                if isinstance(m, dict)
                                else m.split(":")[1]
                            )
                            == "/tmp"
                            for m in mounts
                        )
                        != 1
                    ):
                        raise ValueError(
                            "generated Compose tmpfs must have one exact "
                            "credential-bound /tmp mount"
                        )
                    diagnostic_mounts[source] = True
                continue
            if isinstance(mount, dict) and mount["type"] == "volume":
                raise ValueError("undeclared generated Compose volume")
            path = Path(source)
            if source != "/var/run/docker.sock" and (
                not path.is_absolute()
                or not path.resolve().is_relative_to(root.resolve())
            ):
                raise ValueError("Compose bind mount must belong to its run directory")
    if any(
        options and name not in diagnostic_mounts
        for name, options in value.get("volumes", {}).items()
    ):
        raise ValueError(
            "generated Compose diagnostic tmpfs must be mounted by its owned target"
        )


def compose_file_fingerprint(project: DockerComposeProject, cwd: Path) -> str:
    """Validate and hash a bounded, self-contained generated Compose file."""
    if (
        project.build
        or project.role != "host"
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or key.startswith(("DOCKER_", "COMPOSE_"))
            for key, value in project.env.items()
        )
    ):
        raise ValueError("generated Compose requires frozen local execution")
    path = project.file if project.file.is_absolute() else cwd / project.file
    if path.is_symlink() or not path.is_file():
        raise ValueError("owned Compose file must be a regular non-symlink file")
    with path.open("rb") as stream:
        body = stream.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("owned Compose file exceeds the evidence byte limit")
    _validate_generated(body, path.parent, project.name)
    return hashlib.sha256(body).hexdigest()


def compose_argv(project: DockerComposeProject) -> tuple[str, ...]:
    """Ignore ambient .env files and target only the explicit generated project."""
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


def journaled_compose_resource(
    resource: Resource[DockerComposeProject],
    *,
    project: DockerComposeProject,
    cwd: Path,
    cleanup_state: CleanupState,
    command: Callable[[tuple[str, ...], Path, Mapping[str, str]], bytes],
) -> Resource[dict[str, Any]]:
    """Persist Compose ownership and gate its release on dependent cleanup."""
    expected = {
        "schema": "nanolab-soak-owned-compose-v1",
        "cwd": str(cwd.absolute()),
        "project": {
            "name": project.name,
            "file": str(project.file),
            "ready_url": project.ready_url,
            "build": project.build,
            "role": project.role,
            "env": dict(project.env),
        },
    }

    def acquire(inputs: TaskInputs) -> dict[str, Any]:
        digest = compose_file_fingerprint(project, cwd)
        cleanup_state.prepare()
        try:
            resource.acquire(inputs)
        except BaseException as error:
            cleanup_state.unknown_acquisition(resource.title, error)
            raise
        value = {**deepcopy(expected), "compose_sha256": digest}
        cleanup_state.retain(resource.title, value)
        return value

    def release(inputs: TaskInputs, value: dict[str, Any]) -> None:
        cleanup_state.require_compose_release(resource.title)
        if not isinstance(value, dict) or set(value) != {*expected, "compose_sha256"}:
            raise ValueError("retained Compose identity differs from owned project")
        if {
            key: item for key, item in value.items() if key != "compose_sha256"
        } != expected:
            raise ValueError("retained Compose identity differs from owned project")
        if value["compose_sha256"] != compose_file_fingerprint(project, cwd):
            raise ValueError("owned Compose file changed since acquisition")
        try:
            command((*compose_argv(project), "down", "--volumes"), cwd, project.env)
            cleanup_state.released(resource.title)
        except BaseException:
            cleanup_state.failed = True
            raise

    return Resource(
        title=resource.title,
        acquire=acquire,
        release=release,
        requires=resource.requires,
        always_release=resource.always_release,
        acquire_idempotent=resource.acquire_idempotent,
    )
