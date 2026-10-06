"""Observe live process identity, launcher options and effective configuration."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanolab.tasks.soak.collector import _docker_get, collect_procfs
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.preparation import PreparedSoak

if TYPE_CHECKING:
    from nanolab.tasks.soak.containerd_runtime import PreparedContainerdSoak


def _duration_seconds(value: object) -> float:
    """Parse observed Spring Duration values without assuming an absent default."""
    import re

    if not isinstance(value, str):
        raise ValueError("effective duration must be an observed string")
    match = re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value
    )
    if match and any(match.groups()):
        return sum(
            float(part or 0) * factor
            for part, factor in zip(match.groups(), (3600, 60, 1), strict=True)
        )
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if match:
        return float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]
    raise ValueError("effective duration format is unsupported")


def retention_from_info(document: dict) -> dict[str, float]:
    """Read the control plane's normalized execution-store retention."""
    properties = document.get("executionStore")
    if not isinstance(properties, dict):
        raise ValueError("effective execution-store info is absent")
    return {
        "unkeyed-sync-outcome": _duration_seconds(properties.get("syncTtl")),
        "terminal-key-and-readable-outcome": _duration_seconds(properties.get("ttl")),
        "live-key-and-execution": _duration_seconds(properties.get("maxLifetime")),
    }


def _read_owned_argfile(root: Path, name: str, limit: int) -> tuple[bytes, dict]:
    """Read a bounded regular file beneath the target root without symlink traversal."""
    path = Path(name)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise ValueError(
            "JVM argfile requires an absolute container path without traversal"
        )
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)
    with os.fdopen(file_descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("JVM argfile exceeds its regular-file observation bound")
        body = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if len(body) > limit or any(
        getattr(before, key) != getattr(after, key) for key in fields
    ):
        raise ValueError("JVM argfile changed during bounded observation")
    return body, {key: getattr(after, key) for key in fields}


def _observed_java_options(
    directory: Path, argv: list[str], env: dict, proof: dict
) -> list[str]:
    """Expand a conservative launcher grammar, preserving every option and override.

    Evidence describes observed launch inputs, not a live VM.flags query. The
    current argfile bytes and metadata are retained for audit, not claimed to
    prove that mutable files have been unchanged since process startup.
    """

    def environment(name):
        value = env.get(name, "")
        if not value.isascii() or any(char in value for char in "'\"\\"):
            raise ValueError("unsupported JVM environment quoting or encoding")
        return value.split()

    options = environment("JAVA_TOOL_OPTIONS")
    trailing = environment("_JAVA_OPTIONS")
    if any(not item.startswith("-") for item in (*options, *trailing)):
        raise ValueError("unsupported JVM environment argument")
    pending = environment("JDK_JAVA_OPTIONS") + argv[1:]
    used = 0
    index = 0
    while index < len(pending):
        item = pending[index]
        if item.startswith("@"):
            if len(proof["argfiles"]) >= 16:
                raise ValueError("JVM argfile count exceeds observation bound")
            body, metadata = _read_owned_argfile(
                directory / "root", item[1:], 65536 - used
            )
            used += len(body)
            proof["argfiles"].append(
                {
                    "path": item[1:],
                    "bytes_base64": base64.b64encode(body).decode(),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "stat": metadata,
                }
            )
            text = body.decode("ascii")
            content = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
            if any(char in content for char in "'\\") or any(
                ord(char) < 32 and char not in " \t\r\n\f" for char in text
            ):
                raise ValueError(
                    "unsupported JVM argfile quoting, escape or control character"
                )
            expanded = content.split()
            for position, token in enumerate(expanded):
                # Recipe JVM launch files quote each complete, unescaped token.
                # Embedded quotes, concatenation and quoted spaces stay unsupported.
                if (
                    len(token) > 2
                    and token.startswith('"')
                    and token.endswith('"')
                    and token.count('"') == 2
                ):
                    expanded[position] = token[1:-1]
                elif '"' in token:
                    raise ValueError("unsupported JVM argfile quoting")
            if any(token.startswith("@") for token in expanded):
                raise ValueError("nested JVM argfiles are unsupported")
            pending[index : index + 1] = expanded
            continue
        if item == "-jar" or not item.startswith("-"):
            break
        if (
            item in {"-cp", "-classpath", "-p", "-m", "--disable-@files"}
            or (
                item.startswith("--") and "=" not in item and item != "--enable-preview"
            )
            or item.startswith(("-XX:Flags=", "-XX:VMOptionsFile="))
        ):
            raise ValueError("unsupported JVM launcher option or indirect options file")
        options.append(item)
        index += 1
    options.extend(trailing)
    if any(item.startswith(("-XX:Flags=", "-XX:VMOptionsFile=")) for item in options):
        raise ValueError("indirect JVM options files are unsupported")
    return options


def observe_local_process(target: Target) -> dict[str, Any]:
    """Observe local PID identity, cgroup-v2 limits and actual runtime arguments."""
    import shlex

    from nanolab.tasks.soak.preflight import effective_cpu_limit

    before = collect_procfs(target.process_id, target.container_id)
    directory = Path("/proc") / str(target.process_id)

    def read(path):
        with path.open("rb") as stream:
            body = stream.read(65536 + 1)
        if len(body) > 65536:
            raise ValueError("process configuration exceeds observation bound")
        return body.decode()

    cgroup = directory / "root/sys/fs/cgroup"
    raw_cpu = read(cgroup / "cpu.max").split()
    if len(raw_cpu) != 2:
        raise ValueError("effective cgroup CPU quota is malformed")
    quota = None if raw_cpu[0] == "max" else int(raw_cpu[0])
    cpuset = read(cgroup / "cpuset.cpus.effective").strip()
    memory = read(cgroup / "memory.max").strip()
    argv = [arg for arg in read(directory / "cmdline").split("\0") if arg]
    env = dict(
        item.split("=", 1)
        for item in read(directory / "environ").split("\0")
        if "=" in item
    )
    executable = (directory / "exe").readlink().name
    runtime = (
        "jvm" if executable == "java" else "node" if executable == "node" else None
    )
    options = []
    argument_evidence = {}
    if runtime == "jvm":
        argument_evidence = {
            "target": asdict(target),
            "process_start_ticks": before["start_ticks"],
            "source": "observed launch inputs; not live VM flags",
            "argv": argv,
            "environment": {
                name: env.get(name, "")
                for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")
            },
            "argfiles": [],
        }

        def owned_container():
            container = _docker_get(
                f"/containers/{target.container_id}/json",
                "/var/run/docker.sock",
                5,
            )
            state = container.get("State", {})
            if (
                container.get("Id") != target.container_id
                or state.get("Pid") != target.process_id
                or state.get("StartedAt") != target.process_started_at
                or state.get("Running") is not True
                or container.get("Config", {}).get("Image") != target.image_digest
            ):
                raise ValueError("owned JVM container identity changed")

        try:
            owned_container()
            options = _observed_java_options(directory, argv, env, argument_evidence)
            owned_container()
        except (OSError, ValueError) as error:
            options = None
            argument_evidence["unavailable"] = str(error)
    elif runtime == "node":
        options.extend(shlex.split(env.get("NODE_OPTIONS", "")))
        for arg in argv[1:]:
            if not arg.startswith("-"):
                break
            options.append(arg)
    after = collect_procfs(target.process_id, target.container_id)
    if before["start_ticks"] != after["start_ticks"]:
        raise ValueError(
            "process identity changed while observing effective configuration"
        )
    return {
        "cpu": effective_cpu_limit(quota, int(raw_cpu[1]), cpuset),
        "memory_bytes": None if memory == "max" else int(memory),
        "limit_sources": {
            "cpu": str(cgroup / "cpu.max"),
            "memory_bytes": str(cgroup / "memory.max"),
        },
        "runtime": runtime,
        "runtime_options": options if runtime is not None else None,
        "runtime_source": "procfs/exe,cmdline,environ",
        "runtime_argument_evidence": argument_evidence,
        "capabilities": ["procfs", "docker-engine"],
        "collection_sources": ["procfs", "docker-engine"],
    }


def observe_local_configuration(
    prepared: PreparedSoak | PreparedContainerdSoak,
    management_url: str,
    api_url: str | None = None,
) -> dict[str, Any]:
    """Query bounded actuator documents; missing bound values stay unavailable."""
    from urllib.request import ProxyHandler, Request, build_opener

    from nanolab.tasks.soak.collector import _NoRedirect, read_bounded

    result: dict[str, Any] = {"unavailable": {}}
    # Build metadata still comes from its module on the application port.
    sources = {"info": management_url + "/actuator/info"}
    if api_url is not None:
        sources["build-metadata"] = api_url + "/modules/build-metadata"
    for name, url in sources.items():
        try:
            request = Request(url, headers={"Accept": "application/json"})
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            with opener.open(
                request, timeout=prepared.config.scrape_timeout_s
            ) as response:
                document = json.loads(read_bounded(response))
            if not isinstance(document, dict):
                raise ValueError("actuator observation must be an object")
            prepared.writer.write_json("effective-" + name + ".json", document)
            if name == "info":
                result["retention_s"] = retention_from_info(document)
                result["retention_source"] = "effective-info.json:executionStore"
            else:
                raw = document.get("modules")
                if not isinstance(raw, (list, str)):
                    raise ValueError(
                        "effective module information is absent or ambiguous"
                    )
                result["modules"] = (
                    [item.strip() for item in raw.split(",") if item.strip()]
                    if isinstance(raw, str)
                    else raw
                )
                result["modules_source"] = "effective-build-metadata.json:modules"
        except (OSError, ValueError, TypeError, KeyError) as error:
            result["unavailable"][name] = str(error)
    return result
