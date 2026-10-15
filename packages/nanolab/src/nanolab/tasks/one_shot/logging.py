"""Optional node collectors owned by existing Sonata compensation/lifetimes."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import compensated_resource

from nanolab.config.one_shot import OneShotLogs, operational_limit
from nanolab.one_shot.infrastructure import NodeResource
from nanolab.tasks.loadtest.soak import parse_prometheus


def query_url(endpoint: str) -> str:
    """Derive the query API beside the configured push endpoint."""
    return endpoint.removesuffix("/push") + "/query_range"


def backend_resource(
    logs: OneShotLogs,
    *,
    root: Path,
    run_dir: Path,
    host_cpus: list[int] | None = None,
    operator: Resource | None = None,
    settings=None,
    supervision=None,
) -> Resource[str]:
    """Preserve external endpoints and own local backends only when requested."""
    endpoint_url = logs.endpoint
    if not endpoint_url:
        raise ValueError("logging backend endpoint is required")
    if not logs.backend_owned:
        return Resource(
            title="Use external logging backend",
            acquire=lambda _: endpoint_url,
            release=lambda _inputs, _value: None,
        )
    project = "nanolab-logs-" + uuid4().hex[:12]
    endpoint = urlsplit(endpoint_url)
    environment = dict(
        os.environ,
        LOKI_BIND=logs.backend_bind,
        LOKI_LISTEN_PORT=str(endpoint.port or 80),
        GRAFANA_PORT="0",
    )
    directory = run_dir / "logging"
    override = directory / "backend-compose.override.json"
    started = False
    argv = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(root / "deploy/observability/compose.yaml"),
        "-f",
        str(override),
        "--profile",
        "backend",
    ]

    def command(*args, check=True, timeout=None):
        return subprocess.run(
            argv + list(args),
            env=environment,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout or operational_limit(settings, "compose_seconds", 90),
        )

    def release(_inputs, _value=None):
        if not started and _value is None:
            return
        directory = run_dir / "logging"
        archive_error = None
        raw_export_failed = False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            containers = command("ps", "-aq", "loki").stdout.split()
            if containers:
                if len(containers) != 1:
                    raise RuntimeError("owned Loki identity is ambiguous")
                # Collector dependencies release first. Stop the final writer
                # before copying the entire store, including already sent chunks.
                command("stop", "loki")
                destination = directory / "backend-data"
                destination.mkdir(exist_ok=False)
                subprocess.run(
                    ["docker", "cp", containers[0] + ":/loki/.", str(destination)],
                    check=True,
                    capture_output=True,
                    timeout=operational_limit(settings, "compose_seconds", 90),
                )
                hashes = {}
                for path in destination.rglob("*"):
                    if path.is_file():
                        with path.open("rb") as stream:
                            hashes[str(path.relative_to(destination))] = (
                                hashlib.file_digest(stream, "sha256").hexdigest()
                            )
                (directory / "backend-data-sha256.json").write_text(
                    json.dumps(hashes, indent=2) + "\n"
                )
        except Exception as error:
            archive_error = error
            raw_export_failed = True
        try:
            directory.mkdir(parents=True, exist_ok=True)
            captured = command("logs", "--no-color", check=False)
            (directory / "backend.log").write_text(captured.stdout + captured.stderr)
        except Exception as error:
            archive_error = archive_error or error
            logging.getLogger(__name__).warning("Backend log export failed: %s", error)
        result = command(
            "down",
            *(() if raw_export_failed else ("-v",)),
            "--remove-orphans",
            check=False,
        )
        if raw_export_failed:
            recovery = {
                "atUtc": datetime.now(UTC).isoformat(),
                "project": project,
                "retained": "owned Docker volumes; services stopped",
                "reason": str(archive_error),
                "cleanupComplete": False,
            }
            logging.getLogger(__name__).error("Raw recovery required: %s", recovery)
            try:
                (directory / "backend-recovery.json").write_text(
                    json.dumps(recovery, indent=2) + "\n"
                )
            except OSError as error:
                if archive_error is not None:
                    archive_error.add_note("recovery receipt: " + str(error))
        try:
            (directory / "backend-cleanup.log").write_text(
                result.stdout + result.stderr
            )
        except OSError as error:
            archive_error = archive_error or error
            logging.getLogger(__name__).warning(
                "Backend cleanup receipt failed: %s", error
            )
        remaining = subprocess.check_output(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ],
            text=True,
        ).strip()
        volumes = subprocess.check_output(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ],
            text=True,
        ).strip()
        if result.returncode or remaining or (volumes and not raw_export_failed):
            raise RuntimeError("owned logging backend cleanup unconfirmed")
        if archive_error is not None:
            raise RuntimeError(
                "owned Loki raw archive failed before cleanup"
            ) from archive_error

    def verify_ingestion():
        timestamp = str(time.time_ns())
        message = "nanolab backend probe " + timestamp
        labels = {"nanolab_backend_probe": project}
        row: dict[str, str | int | float | None] = {
            "atUtc": datetime.now(UTC).isoformat(),
            "state": "checking",
        }
        timeout = operational_limit(settings, "collection_http_seconds", 10)
        with httpx.Client(trust_env=False) as http:
            metrics = None
            metrics_attempted = False
            metrics_url = endpoint_url.split("/loki/api/v1/push")[0] + "/metrics"
            try:
                response = http.post(
                    endpoint_url,
                    json={
                        "streams": [
                            {"stream": labels, "values": [[timestamp, message]]}
                        ]
                    },
                    timeout=timeout,
                )
                row["pushStatus"] = response.status_code
                if response.status_code != 204:
                    raise RuntimeError(
                        f"Loki push HTTP {response.status_code}: {response.text}"
                    )
                response = http.get(
                    query_url(endpoint_url),
                    params={
                        "query": '{nanolab_backend_probe="' + project + '"}',
                        "start": timestamp,
                        "end": str(int(timestamp) + 1),
                        "limit": 1,
                    },
                    timeout=timeout,
                )
                row["queryStatus"] = response.status_code
                response.raise_for_status()
                body = response.json()
                if body.get("status") != "success" or not any(
                    value == [timestamp, message]
                    for stream in body.get("data", {}).get("result", [])
                    for value in stream.get("values", [])
                ):
                    raise RuntimeError("Loki probe write is not queryable")
                # Loki deliberately accepts ENOSPC writes into memory. A visible
                # canary must not hide a failed WAL persistence operation.
                metrics_attempted = True
                metrics = http.get(metrics_url, timeout=timeout)
                metrics.raise_for_status()
                counters = parse_prometheus(
                    metrics.text, ("loki_ingester_wal_disk_full_failures_total",)
                )
                failures = counters.get("loki_ingester_wal_disk_full_failures_total")
                if failures is None or not math.isfinite(failures) or failures < 0:
                    row["walDiskFullFailures"] = None
                    raise RuntimeError(
                        "Loki WAL failure counter unavailable or invalid"
                    )
                row["walDiskFullFailures"] = failures
                if failures:
                    raise RuntimeError(
                        f"Loki WAL disk-full write failures recorded: {failures}"
                    )
                row["state"] = "passed"
            except Exception as error:
                row.update(state="failed", error=str(error))
                try:
                    if not metrics_attempted:
                        metrics = http.get(metrics_url, timeout=timeout)
                        metrics.raise_for_status()
                    if metrics is not None:
                        (directory / "backend-metrics-on-failure.txt").write_text(
                            metrics.text
                        )
                except Exception as metrics_error:
                    row["metricsError"] = str(metrics_error)
                raise RuntimeError(
                    "owned Loki ingestion probe failed: " + str(error)
                ) from error
            finally:
                with (directory / "backend-probes.jsonl").open("a") as stream:
                    stream.write(json.dumps(row) + "\n")

    def acquire(_inputs):
        nonlocal started
        directory.mkdir(parents=True, exist_ok=True)
        # Campaigns share the host filesystem with VM images. A percent-full cutoff
        # can reject every log despite ample free bytes; retain WAL and check real IO.
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "loki": {
                            "command": [
                                "-config.file=/etc/loki/config.yaml",
                                "-ingester.wal-disk-full-threshold=0",
                                "-ingester.wal-enabled=true",
                            ]
                        }
                    }
                },
                indent=2,
            )
            + "\n"
        )
        started = True
        command("up", "-d")
        if host_cpus is not None:
            from nanolab.one_shot.host_cpu import pin_logging_project

            facts = pin_logging_project(project, host_cpus)
            (run_dir / "logging-host-placement.json").write_text(
                json.dumps(facts, indent=2) + "\n"
            )
        deadline = time.monotonic() + operational_limit(
            settings, "service_start_seconds", 90
        )
        with httpx.Client(trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    http.get(
                        endpoint_url.split("/loki/api/v1/push")[0] + "/ready",
                        timeout=operational_limit(
                            settings, "collection_http_seconds", 2
                        ),
                    ).raise_for_status()
                except httpx.HTTPError:
                    time.sleep(0.5)
                    continue
                verify_ingestion()
                return endpoint_url
        raise RuntimeError("owned logging backend readiness failed")

    resource = compensated_resource(
        title="Own local Loki/Grafana logging backend",
        acquire=acquire,
        release=release,
        compensate=release,
        requires=(operator,) if operator is not None else (),
    )
    if supervision is not None:

        def probe(_value):
            result = command(
                "ps",
                "--all",
                "--format",
                "json",
                "loki",
                timeout=operational_limit(settings, "collection_http_seconds", 10),
            )
            raw = result.stdout.strip()
            rows = (
                json.loads(raw)
                if raw.startswith("[")
                else [json.loads(line) for line in raw.splitlines()]
            )
            if len(rows) != 1 or rows[0].get("State") != "running":
                raise RuntimeError("owned Loki backend stopped")
            verify_ingestion()

        return supervision.service_resource(
            resource,
            "logging-backend",
            probe,
            join_seconds=operational_limit(settings, "worker_join_seconds", 40),
        )
    return resource


@dataclass(frozen=True)
class LogSession:
    """Retain the measured collector identity and export interval."""

    node_id: str
    container: str
    volume: str
    started_ns: int
    identity: dict


def collection_evidence(metrics: str) -> dict:
    """Conservatively distinguish delivery counters from query transport success."""
    totals: dict[str, float] = {}
    for line in metrics.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        key = parts[0].split("{", 1)[0]
        try:
            totals[key] = totals.get(key, 0) + float(parts[1])
        except (ValueError, IndexError):
            continue
    required = [
        "fluentbit_input_records_total",
        "fluentbit_output_proc_records_total",
        "fluentbit_output_dropped_records_total",
    ]
    if any(key not in totals for key in required):
        return {"status": "unverified", "reason": "delivery counters unavailable"}
    received, delivered, dropped = (totals[key] for key in required)
    dropped += sum(
        value
        for key, value in totals.items()
        if key
        in {
            "fluentbit_filter_drop_records_total",
            "fluentbit_input_long_line_skipped_total",
            "fluentbit_input_long_line_truncated_total",
            "fluentbit_input_multiline_truncated_total",
        }
    )
    pending = max(0, received - delivered - dropped)
    return {
        "status": "loss" if dropped else "pending" if pending else "drained",
        "received": received,
        "delivered": delivered,
        "dropped": dropped,
        "pending": pending,
    }


def collector_resource(
    node: NodeResource,
    logs: OneShotLogs,
    *,
    bootstrap: Resource,
    backend: Resource[str],
    root: Path,
    run_dir: Path,
    mode: str,
    settings=None,
) -> Resource[LogSession]:
    """Own persistent buffering and export independently of SDK accounting."""
    token = uuid4().hex[:12]
    name = "nanolab-logs-" + node.config.id + "-" + token
    volume = name + "-state"
    assets = {
        filename: root / "deploy/observability" / filename
        for filename in ["fluent-bit.conf", "parsers.conf"]
    }
    hashes = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in assets.items()
    }
    created = False
    session = None
    final_metrics = ""
    archive_issue = None

    def metrics_snapshot():
        return node.command(
            (
                "python3",
                "-c",
                "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:2020/api/v1/metrics/prometheus',timeout="
                + str(operational_limit(settings, "collection_http_seconds", 3))
                + ").read().decode())",
            )
        ).stdout

    def archive_state():
        directory = run_dir / "logging" / node.config.id
        directory.mkdir(parents=True, exist_ok=True)
        mountpoint = node.command(
            ("docker", "volume", "inspect", "--format={{.Mountpoint}}", volume)
        ).stdout.strip()
        if not mountpoint.startswith("/"):
            raise RuntimeError("collector state mountpoint unavailable")
        private = node.command(("mktemp", "-d")).stdout.strip()
        if not private.startswith("/"):
            raise RuntimeError("collector archive private directory unavailable")
        remote = private + "/state.tar.gz"
        node.command(("sudo", "tar", "-czf", remote, "-C", mountpoint, "."))
        node.command(("sudo", "chmod", "644", remote))
        archive = directory / "collector-state.tar.gz"
        copied = node.provider.transfer_from(
            node.request, source=remote, destination=archive
        )
        if copied.return_code or not archive.is_file() or not archive.stat().st_size:
            raise RuntimeError("collector state archive transfer failed")
        remote_hash = node.command(("sha256sum", remote)).stdout.split()[0]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != remote_hash:
            raise RuntimeError("collector state archive checksum mismatch")
        node.command(("rm", "-f", remote))
        node.command(("rmdir", private))

    def export():
        directory = run_dir / "logging" / node.config.id
        directory.mkdir(parents=True, exist_ok=True)
        status = {
            "status": "complete",
            "exportStatus": "complete",
            "nodeId": node.config.id,
            "issues": [],
            "truncated": False,
            "collection": collection_evidence(final_metrics),
            "stateArchive": "verified" if archive_issue is None else "failed",
        }
        if status["collection"]["status"] != "drained":
            status["issues"].append(
                "collection delivery is " + status["collection"]["status"]
            )
        if archive_issue:
            status["issues"].append(
                "recoverable state archive failed: " + archive_issue
            )
        write_error = None
        try:
            (directory / "collector-metrics.txt").write_text(final_metrics)
            stats = node.command(
                ("docker", "stats", "--no-stream", "--format={{json .}}", name)
            )
            (directory / "collector-stats.json").write_text(stats.stdout)
            observed = node.command(("docker", "logs", name))
            (directory / "collector.log").write_text(observed.stdout + observed.stderr)
        except OSError as error:
            write_error = write_error or error
            status["issues"].append(str(error))
        except Exception as error:
            status["issues"].append(str(error))
        try:
            if session is None or logs.endpoint is None:
                raise RuntimeError(
                    "collector identity or endpoint unavailable during export"
                )
            params = {
                "query": '{job="nanofaas",node='
                + json.dumps(node.config.id)
                + "} | json | runId="
                + json.dumps(run_dir.name),
                "start": str(session.started_ns),
                "end": str(time.time_ns()),
                "limit": str(logs.export_limit),
            }
            with httpx.Client(trust_env=False) as http:
                response = http.get(query_url(logs.endpoint), params=params, timeout=10)
                response.raise_for_status()
                result = response.json()
            (directory / "loki-query.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            count = sum(len(stream["values"]) for stream in result["data"]["result"])
            status["truncated"] = count >= logs.export_limit
            if status["truncated"]:
                status["issues"].append("Loki export limit reached; query is partial")
                status["exportStatus"] = "partial"
            if count < status["collection"].get("delivered", 0):
                status["issues"].append(
                    "query contains fewer records than collector delivery counter"
                )
                status["exportStatus"] = "partial"
        except OSError as error:
            write_error = write_error or error
            status["issues"].append(str(error))
        except Exception as error:
            status["issues"].append(str(error))
            status["exportStatus"] = "partial"
        if status["issues"]:
            status["status"] = "partial"
        (directory / "status.json").write_text(json.dumps(status, indent=2) + "\n")
        (directory / "sha256.json").write_text(
            json.dumps(
                {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in directory.iterdir()
                    if p.is_file() and p.name != "sha256.json"
                },
                indent=2,
            )
            + "\n"
        )
        if write_error is not None:
            raise RuntimeError("collector export write failed") from write_error

    def release(_inputs, _value=None):
        nonlocal created, final_metrics, archive_issue
        if not created:
            return
        # The platform/function resources depend on this collector: they stop first.
        # Bound tail/flush/retry, then stop before archiving SQLite/chunks.
        for _ in range(5):
            time.sleep(2)
            try:
                final_metrics = metrics_snapshot()
                if collection_evidence(final_metrics)["status"] == "drained":
                    break
            except Exception:
                break
        stopped = node.provider.exec_argv(
            node.request,
            (
                "docker",
                "stop",
                "-t",
                str(int(operational_limit(settings, "process_drain_seconds", 15))),
                name,
            ),
        )
        try:
            if stopped.return_code:
                raise RuntimeError("collector graceful stop failed")
            archive_state()
        except Exception as error:
            archive_issue = str(error)
            node.recovery.append(f"collector volume {volume}: {archive_issue}")
            logging.getLogger(__name__).error(
                "Recoverable collector state archive failed for %s: %s",
                node.config.id,
                error,
            )
        export_error = None
        try:
            export()
        except Exception as error:
            export_error = error
            logging.getLogger(__name__).warning(
                "Log export failed for %s: %s", node.config.id, error
            )
        removed = node.provider.exec_argv(node.request, ("docker", "rm", "-f", name))
        if (
            removed.return_code
            and "No such container" not in removed.stdout + removed.stderr
        ):
            raise RuntimeError("collector cleanup unconfirmed")
        if archive_issue is None:
            deleted = node.provider.exec_argv(
                node.request, ("docker", "volume", "rm", volume)
            )
            if (
                deleted.return_code
                and "no such volume" not in (deleted.stdout + deleted.stderr).lower()
            ):
                raise RuntimeError("collector volume cleanup unconfirmed")
        created = False
        if archive_issue:
            try:
                (run_dir / "logging" / node.config.id / "recovery.json").write_text(
                    json.dumps(
                        {
                            "node": node.config.id,
                            "vm": getattr(node.request, "name", None),
                            "volume": volume,
                            "reason": archive_issue,
                            "cleanupComplete": False,
                        },
                        indent=2,
                    )
                    + "\n"
                )
            except OSError as error:
                logging.getLogger(__name__).error(
                    "Recovery receipt unavailable: %s", error
                )
            raise RuntimeError(
                "collector state archive failed; volume and containing VM retained: "
                + archive_issue
            )
        if export_error is not None:
            raise RuntimeError(
                "collector export failed before owned cleanup"
            ) from export_error

    def acquire(inputs: TaskInputs):
        nonlocal created, session
        vm = inputs.resource(node.vm)
        endpoint = urlsplit(inputs.resource(backend))
        remote = vm.home + "/nanolab-logging-" + token
        node.command(("mkdir", "-p", remote))
        for filename, path in assets.items():
            staged = node.provider.transfer_to(
                node.request, source=path, destination=remote + "/" + filename
            )
            if staged.return_code:
                raise RuntimeError("collector config transfer failed")
        verified = node.command(("sha256sum", *(remote + "/" + key for key in assets)))
        if [line.split()[0] for line in verified.stdout.splitlines()] != list(
            hashes.values()
        ):
            raise ValueError("collector config hash mismatch")
        driver = node.command(
            ("docker", "info", "--format={{.LoggingDriver}}")
        ).stdout.strip()
        if driver != "json-file":
            raise ValueError(
                "managed function collection requires Docker json-file driver"
            )
        root_dir = node.command(
            ("docker", "info", "--format={{.DockerRootDir}}")
        ).stdout.strip()
        if not root_dir.startswith("/"):
            raise ValueError("Docker log root unavailable")
        existing = node.provider.exec_argv(
            node.request, ("docker", "volume", "inspect", volume)
        )
        if not existing.return_code:
            raise RuntimeError("refuse pre-existing collector state volume")
        node.command(
            (
                "docker",
                "volume",
                "create",
                "--label",
                "nanolab.logging=" + token,
                volume,
            )
        )
        created = True
        values = {
            "NANOFAAS_NODE_ID": node.config.id,
            "NANOFAAS_ENVIRONMENT": logs.environment,
            "NANOFAAS_CAMPAIGN_ID": logs.campaign_id,
            "NANOFAAS_RUN_ID": run_dir.name,
            "NANOFAAS_MODE": mode,
            "NANOFAAS_NODE_ROLE": node.config.kind,
            "NANOFAAS_LOG_BUFFER_SIZE": str(logs.buffer_mib) + "M",
            "LOKI_HOST": endpoint.hostname,
            "LOKI_PORT": str(
                endpoint.port or (443 if endpoint.scheme == "https" else 80)
            ),
            "LOKI_URI": endpoint.path,
            "LOKI_TLS": "On" if endpoint.scheme == "https" else "Off",
        }
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "host",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(logs.cpu),
            "--memory",
            str(logs.memory_mib) + "m",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=10m",
            "--log-opt",
            "max-file=2",
            "-v",
            root_dir + "/containers:/var/log/containers:ro",
            "-v",
            volume + ":/var/lib/fluent-bit",
            "-v",
            remote + "/fluent-bit.conf:/fluent-bit/etc/fluent-bit.conf:ro",
            "-v",
            remote + "/parsers.conf:/fluent-bit/etc/parsers.conf:ro",
        ]
        for key, value in values.items():
            argv += ["-e", key + "=" + str(value)]
        started = time.time_ns()
        node.command((*argv, logs.image))
        observed = json.loads(node.command(("docker", "inspect", name)).stdout)[0]
        if not observed["State"]["Running"]:
            raise RuntimeError("owned collector is not running")
        budget = observed["HostConfig"]
        if (
            budget["NanoCpus"] != round(logs.cpu * 1e9)
            or budget["Memory"] != logs.memory_mib * 1024 * 1024
        ):
            raise ValueError("collector resource budget mismatch")
        session = LogSession(
            node.config.id,
            name,
            volume,
            started,
            {
                "image": observed["Image"],
                "configSha256": hashes,
                "cpu": logs.cpu,
                "memoryMiB": logs.memory_mib,
                "bufferMiB": logs.buffer_mib,
            },
        )
        deadline = time.monotonic() + operational_limit(
            settings, "worker_start_seconds", 30
        )
        while time.monotonic() < deadline:
            result = node.provider.exec_argv(
                node.request,
                (
                    "python3",
                    "-c",
                    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:2020/api/v1/metrics/prometheus',timeout="
                    + str(operational_limit(settings, "collection_http_seconds", 2))
                    + ").read()",
                ),
            )
            if not result.return_code:
                return session
            time.sleep(0.5)
        raise RuntimeError("collector readiness failed")

    return compensated_resource(
        title="Own " + node.config.id + " Fluent Bit collector",
        acquire=acquire,
        release=release,
        compensate=release,
        requires=(node.vm, bootstrap, backend),
    )
