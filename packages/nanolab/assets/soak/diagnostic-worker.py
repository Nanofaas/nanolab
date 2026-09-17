"""Pinned container worker. All child commands use NanoLab's owned supervisor.

The host must explicitly stop the owned target if this worker cannot acknowledge
completion. Stopping jcmd does not cancel an in-JVM operation.
"""

import base64
import ctypes
import datetime
import fcntl
import json
import math
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4


def full_gc_events(recording, started_epoch_s, ended_epoch_s):
    """Require actual completed full-GC JFR events caused by diagnostic command."""
    selected = []
    for event in recording["recording"]["events"]:
        values = event.get("values", {})
        if (
            event.get("type") != "jdk.GarbageCollection"
            or values.get("name") not in {"G1Full", "SerialOld", "ParallelOld"}
            or values.get("cause") != "Diagnostic Command"
            or type(values.get("gcId")) is not int
        ):
            continue
        start = datetime.datetime.fromisoformat(
            values["startTime"].replace("Z", "+00:00")
        ).timestamp()
        match = re.fullmatch(r"PT([0-9]+(?:\.[0-9]+)?)S", values["duration"])
        if match is None:
            continue
        end = start + float(match[1])
        if started_epoch_s <= start <= end <= ended_epoch_s:
            selected.append(event)
    if not selected:
        raise ValueError("no request-window JFR full GC caused by Diagnostic Command")
    return selected


def heap_dump_completed(output):
    """Jcmd may exit zero while reporting ENOSPC or another dump failure."""
    return (
        re.search(r"(?m)^Heap dump file created \[[0-9]+ bytes in ", output) is not None
    )


def validate_request(message):
    """Only fixed diagnostic methods are dispatchable, never caller argv."""
    if message.get("kind") not in {"inspect", "execute"}:
        raise ValueError("unsupported request")
    if message["kind"] == "inspect":
        return
    if (
        message.get("operation") not in {"gc", "heap_dump"}
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message.get("request_id", ""))
        or type(message.get("max_bytes")) is not int
        or message["max_bytes"] <= 0
        or type(message.get("timeout_s")) not in (int, float)
        or not math.isfinite(message["timeout_s"])
        or not 0 < message["timeout_s"] <= 3600
    ):
        raise ValueError("unprovisioned or unbounded diagnostic request")


def identity(cfg, *, require_shared_tmp=True):
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/1/status").read_text().splitlines()
        if ":" in line
    )
    ticks = Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19]
    if ticks != cfg["target_start_ticks"]:
        raise ValueError("target process incarnation changed")
    if require_shared_tmp:
        helper_tmp, target_tmp = os.stat("/tmp"), os.stat("/proc/1/root/tmp")
        if (helper_tmp.st_dev, helper_tmp.st_ino) != (
            target_tmp.st_dev,
            target_tmp.st_ino,
        ):
            raise ValueError(
                "target attach/control directory is not shared with helper"
            )
    return {
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "target_uid": int(status["Uid"].split()[1]),
        "target_gid": int(status["Gid"].split()[1]),
        "target_pid": 1,
        "target_start_ticks": ticks,
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "target_pid_namespace": os.readlink("/proc/1/ns/pid"),
        "mount_namespace": os.readlink("/proc/self/ns/mnt"),
        "target_mount_namespace": os.readlink("/proc/1/ns/mnt"),
        "helper_pid": cfg["helper_pid"],
    }


def read_proc(path, limit):
    """Read a complete bounded procfs record without inventing unavailable data."""
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError("procfs evidence exceeds read bound")
    return body.decode("utf-8")


def memory(cfg):
    include_heap = bool(cfg.get("include_heap_info"))
    opt_in = bool(cfg.get("include_smaps") or include_heap)
    result = {
        "schema": "nanolab-soak-memory-helper-v1",
        "target": cfg["target"],
        "source": "docker-exec:owned-pid-namespace:/proc/1",
        "started_s": time.monotonic(),
        "errors": {},
    }
    deadline = cfg.get("memory_deadline_s", result["started_s"] + 5.0)
    if opt_in:
        result["intervals"], result["completion"] = {}, {}
    result["before"] = identity(cfg, require_shared_tmp=include_heap)
    reads = [("status", 65536), ("smaps_rollup", 262144)]
    if cfg.get("include_smaps"):
        reads.append(("smaps", 8388608))
    for name, limit in reads:
        begin = time.monotonic() if opt_in else None
        state = "not_started"
        try:
            if opt_in and time.monotonic() >= deadline:
                raise TimeoutError("memory collection deadline exhausted")
            state = "failed"
            result[name] = read_proc(Path("/proc/1") / name, limit)
            state = "completed"
        except (OSError, ValueError) as error:
            result[name] = None
            message = f"{type(error).__name__}: {error}"
            result["errors"][name] = message[:1024] if opt_in else message
        if opt_in:
            result["completion"][name] = state
            result["intervals"][name] = {
                "started_s": begin,
                "ended_s": time.monotonic(),
            }
    if include_heap:
        begin = time.monotonic()
        result["heap_info"] = None
        state = "not_started"
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("memory collection deadline exhausted")
            with TemporaryDirectory(dir="/work") as scratch:
                state = "unresolved"
                result["heap_info"] = jcmd(
                    ("GC.heap_info",),
                    deadline,
                    Path(scratch),
                    require_completion=True,
                )
                state = "completed"
        except CommandCompletionUnresolved as error:
            result["errors"]["heap_info"] = str(error)[:1024]
        except CommandCompletedError as error:
            state = "completed"
            result["errors"]["heap_info"] = str(error)[:1024]
        except CommandNotStartedError as error:
            # The runner failed before launching anything: the target is
            # untouched, so this is not_started and the host must not treat it
            # as an unacknowledged in-JVM command.
            state = "not_started"
            result["errors"]["heap_info"] = str(error)[:1024]
        except (OSError, ValueError, RuntimeError) as error:
            # A failure before the scratch directory exists leaves this
            # not_started; once a command was launched it stays unresolved,
            # unless jcmd has already acknowledged completion.
            result["errors"]["heap_info"] = str(error)[:1024]
        result["completion"]["heap_info"] = state
        result["intervals"]["heap_info"] = {
            "started_s": begin,
            "ended_s": time.monotonic(),
        }
    result["after"] = identity(cfg, require_shared_tmp=include_heap)
    result["ended_s"] = time.monotonic()
    if opt_in:
        raw_keys = {"status", "smaps_rollup", "smaps", "heap_info"}
        metadata = {key: value for key, value in result.items() if key not in raw_keys}
        if len(json.dumps(metadata).encode("utf-8")) > 65536:
            raise ValueError("memory response metadata exceeds its bound")
    return result


def quota(path, maximum, *, mountpoint="/out"):
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    entry = next((line for line in mounts if line.split()[4] == mountpoint), None)
    if entry is None or entry.split(" - ", 1)[1].split()[0] != "tmpfs":
        raise ValueError(f"{mountpoint} must be a separate tmpfs")
    stat = os.statvfs(path)
    capacity = stat.f_blocks * stat.f_frsize
    if not 0 < capacity <= maximum:
        raise ValueError("output filesystem capacity exceeds reservation")
    return {"type": "tmpfs", "capacity_bytes": capacity}


class CommandCompletionUnresolved(RuntimeError):  # noqa: N818
    """The command runner cannot acknowledge in-JVM completion."""


class CommandNotStartedError(RuntimeError):
    """The runner failed before any command reached the target process."""


class CommandCompletedError(RuntimeError):
    """A normally completed command reported an error."""


def command(
    argv, deadline, scratch, limit=2 * 1024 * 1024, *, require_completion=False
):
    from processes import OwnedCommandRunner

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CommandNotStartedError("remote operation deadline exhausted")
    log = scratch / (uuid4().hex + ".log")
    runner: OwnedCommandRunner | None = None
    try:
        runner = OwnedCommandRunner(
            argv,
            cwd=scratch,
            env=dict(os.environ),
            log_path=log,
            timeout_s=remaining,
            cancelled=Event(),
            output_limit_bytes=limit,
            stop_timeout_s=1.0,
        )
        result = runner.run()
    except (OSError, ValueError, RuntimeError) as error:
        # The launch point is the owned runner's own child: a failure without
        # one provably left the target untouched, so it is not_started rather
        # than an unresolved in-JVM command. Never inferred from message text.
        if runner is not None and runner.launched:
            raise
        raise CommandNotStartedError(
            f"{type(error).__name__}: {error}"[:1024]
        ) from error
    if require_completion and (
        not result.reaped
        or result.ended_s is None
        or result.forced_stop
        or result.errors
        or result.cancelled
        or result.timed_out
        or result.quota_exceeded
        # A missing returncode means the child never reported an exit status,
        # which is no proof that the in-JVM command finished.
        or result.returncode is None
        # A negative returncode means the child died from a signal (a killed
        # helper container, the kernel OOM killer), which is no proof at all
        # that the in-JVM command finished.
        or result.returncode < 0
    ):
        raise CommandCompletionUnresolved("remote command completion unresolved")
    if require_completion and result.returncode != 0:
        # Same bounded excerpt as the sibling failure below: an acknowledged
        # error exit and a bare constant tell an operator nothing about why.
        raise CommandCompletedError(
            "remote command exited with an error: "
            + log.read_text(errors="replace")[:1024]
        )
    if (
        result.returncode != 0
        or not result.reaped
        or result.ended_s is None
        or result.forced_stop
        or result.errors
        or result.timed_out
        or result.cancelled
        or result.quota_exceeded
    ):
        raise RuntimeError("remote owned child failed: " + log.read_text()[:1024])
    return log.read_text()


def jcmd(args, deadline, scratch, *, require_completion=False):
    argv = ("/opt/java/openjdk/bin/jcmd", "1", *args)
    if require_completion:
        return command(argv, deadline, scratch, require_completion=True)
    return command(argv, deadline, scratch)


def node_call(cfg, request, deadline):
    payload = (json.dumps(request) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.connect(cfg["node_socket"])
        connection.sendall(payload)
        body = bytearray()
        while b"\n" not in body:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            part = connection.recv(65536)
            if not part:
                raise OSError("private Node control closed without completion")
            body.extend(part)
            if len(body) > 1024 * 1024:
                raise ValueError("private Node response exceeds bound")
        result = json.loads(body)
    if result.get("ok") is not True or result.get("pid") != 1:
        raise ValueError("Node control rejected operation: " + str(result)[:1024])
    return result


def target_path(cfg, local):
    return f"/proc/{cfg['helper_pid']}/root" + str(local)


def dump_jfr(cfg, name, destination, deadline, scratch):
    """Write JFR at a canonical shared path with a verified target-side quota.

    JFR's WriteablePath creates a file and then calls toRealPath. A proc-root
    magic link can therefore leave a zero-byte placeholder before JFR rejects
    the resolved path. Shared /tmp preserves the same path in both namespaces.
    """
    bound = quota(Path("/tmp"), cfg["quota_bytes"], mountpoint="/tmp")
    with TemporaryDirectory(prefix="nanolab-jfr-", dir="/tmp") as temp:
        path = Path(temp) / "capture.jfr"
        stop_output = jcmd(
            ("JFR.stop", f"name={name}", f"filename={path}"), deadline, scratch
        )
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                header = stream.read(4)
        except OSError as error:
            raise ValueError(
                f"JFR target file unavailable; JFR.stop output: {stop_output[:4096]}"
            ) from error
        if not 4 < size <= cfg["quota_bytes"] or header != b"FLR\x00":
            raise ValueError(
                f"JFR target file empty/invalid ({size} bytes); JFR.stop output: "
                f"{stop_output[:4096]}"
            )
        summary = command(
            ("/opt/java/openjdk/bin/jfr", "summary", str(path)), deadline, scratch
        )
        with path.open("rb") as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=65536)
        if destination.stat().st_size != size:
            raise ValueError("JFR artifact copy differs from observed target file")
        return {
            **bound,
            "target_write_bytes": size,
            "stop_output": stop_output,
            "summary_output": summary,
        }


def dump_heap_jvm(cfg, request, output, deadline, scratch):
    """Stage target writes on shared bounded /tmp, not a proc-root alias.

    Emit command evidence before returning or raising. Its bytes are part of
    the same artifact reservation; incomplete dumps are never transported.
    """
    identity(cfg)
    maximum = min(cfg["quota_bytes"], request["max_bytes"])
    bound = quota(Path("/tmp"), maximum, mountpoint="/tmp")
    with TemporaryDirectory(prefix="nanolab-heapdump-", dir="/tmp") as temp:
        path = Path(temp) / "capture.hprof"
        args = ("GC.heap_dump", str(path))
        before = os.statvfs(temp)
        evidence = {
            "schema": "nanolab-soak-jvm-heapdump-command-v1",
            "request_id": request["request_id"],
            "target": cfg["target"],
            "argv": ["/opt/java/openjdk/bin/jcmd", "1", *args],
            "target_filesystem": bound,
            "available_bytes_before": before.f_bavail * before.f_frsize,
            "started_s": time.monotonic(),
        }
        primary_error = None
        try:
            text = jcmd(args, deadline, scratch)
            evidence["command_output"] = text[:4096]
            evidence["command_output_truncated"] = len(text) > 4096
            if not heap_dump_completed(text):
                raise ValueError(
                    "JVM did not report a completed heap dump: " + text[:1024]
                )
            size = path.stat().st_size
            if not 0 < size <= maximum:
                raise ValueError("empty or over-budget JVM heap dump")
            with path.open("rb") as stream:
                if not stream.read(32).startswith(b"JAVA PROFILE 1.0."):
                    raise ValueError("invalid HPROF header")
            evidence["target_write_bytes"] = size
        except Exception as error:
            primary_error = error
            # Supervised-command failures already include a bounded log excerpt.
            # Do not label that exception text as complete command output.
            evidence["error"] = str(error)[:1024]
            raise
        finally:
            evidence["ended_s"] = time.monotonic()
            try:
                after = os.statvfs(temp)
                evidence["available_bytes_after"] = after.f_bavail * after.f_frsize
            except OSError as error:
                evidence["space_observation_error"] = str(error)[:256]
            try:
                body = json.dumps(
                    evidence, ensure_ascii=False, allow_nan=False
                ).encode()
                if len(body) > min(16384, request["max_bytes"]):
                    raise ValueError(
                        "heapdump command evidence exceeds artifact budget"
                    )
                emit(
                    {
                        "kind": "artifact",
                        "name": "heapdump-command.json",
                        "data_base64": base64.b64encode(body).decode(),
                    }
                )
            except Exception as error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    f"heapdump command evidence unavailable: {error}"
                )
        # Both the target filesystem and the transport have finite bounds.
        # Copy only after command completion/header validation, with no retry or
        # quota growth if target writes, merge, or artifact reservation fail.
        if size + len(body) > request["max_bytes"]:
            raise ValueError("heapdump plus command evidence exceeds artifact budget")
        copied = 0
        with path.open("rb") as source, (output / "capture.hprof").open("xb") as dest:
            while chunk := source.read(65536):
                if time.monotonic() >= deadline:
                    raise TimeoutError("heapdump artifact copy deadline exceeded")
                copied += len(chunk)
                if copied > size:
                    raise ValueError("JVM heap dump changed during artifact copy")
                dest.write(chunk)
        if copied != size:
            raise ValueError("JVM heap dump copy differs from observed target file")
        return len(body)


def probe(cfg):
    evidence = identity(cfg)
    alias = os.stat(cfg["host_output_root"])
    evidence["output_alias"] = [alias.st_dev, alias.st_ino]
    with TemporaryDirectory(prefix="probe-", dir="/work") as temp:
        scratch = Path(temp)
        output = Path("/out") / uuid4().hex
        output.mkdir(mode=0o700)
        deadline = time.monotonic() + cfg.get("timeout_s", 40)
        evidence["quota"] = quota(output, cfg["quota_bytes"])
        runtime = cfg["target"]["runtime"]
        evidence["runtime"] = runtime
        if runtime == "jvm":
            version = jcmd(("VM.version",), deadline, scratch)
            helper_version = command(
                ("/opt/java/openjdk/bin/java", "-version"), deadline, scratch
            )
            target_match = re.search(r"(?:version |JDK )(25\.\d+(?:\.\d+)?)", version)
            helper_match = re.search(r'"(25\.\d+(?:\.\d+)?)', helper_version)
            if not target_match or not helper_match:
                raise ValueError("JDK25 attachment required")
            evidence.update(
                version=target_match[1],
                helper_version=helper_match[1],
                attach_output=version,
                private_control=False,
            )
            name = "nanolab_probe_" + uuid4().hex
            settings = f"/proc/{cfg['helper_pid']}/root/opt/nanolab/full-gc.jfc"
            evidence["jfr_start_output"] = jcmd(
                ("JFR.start", f"name={name}", f"settings={settings}", "disk=false"),
                deadline,
                scratch,
            )
            evidence["jfr_write"] = dump_jfr(
                cfg, name, output / "probe.jfr", deadline, scratch
            )
            evidence["quota"]["target_write_bytes"] = evidence["jfr_write"][
                "target_write_bytes"
            ]
        else:
            result = node_call(
                cfg,
                {
                    "operation": "probe",
                    "path": target_path(cfg, output / "probe.bin"),
                    "max_bytes": cfg["quota_bytes"],
                },
                deadline,
            )
            if result["quota"] != evidence["quota"]:
                raise ValueError("target and helper disagree on output filesystem")
            evidence.update(
                version=result["version"],
                private_control=True,
                inspector_url=result["inspector_url"],
                attach_output=result["attach_output"],
            )
            evidence["quota"]["target_write_bytes"] = (
                (output / "probe.bin").stat().st_size
            )
        for path in output.iterdir():
            path.unlink()
        output.rmdir()
        return evidence


def gc_jvm(cfg, request, output, deadline, scratch):
    name = "nanolab_gc_" + uuid4().hex
    settings = f"/proc/{cfg['helper_pid']}/root/opt/nanolab/full-gc.jfc"
    jcmd(
        ("JFR.start", f"name={name}", f"settings={settings}", "disk=false"),
        deadline,
        scratch,
    )
    start = time.time()
    jcmd(("GC.run",), deadline, scratch)
    end = time.time()
    path = output / "capture.jfr"
    dump_jfr(cfg, name, path, deadline, scratch)
    raw = command(
        (
            "/opt/java/openjdk/bin/jfr",
            "print",
            "--json",
            "--events",
            "jdk.GarbageCollection",
            str(path),
        ),
        deadline,
        scratch,
    )
    recording = json.loads(raw)
    events = full_gc_events(recording, start, end)
    (output / "runtime-gc.json").write_text(raw)
    # This is a count of observed events in this fresh request recording, not a
    # guessed VM counter and not evidence derived from jcmd's exit status.
    return {
        "before_count": 0,
        "after_count": len(events),
        "source": "jdk.GarbageCollection",
        "runtime_event_ids": [event["values"]["gcId"] for event in events],
    }


def gc_node(cfg, output, deadline):
    result = node_call(cfg, {"operation": "gc"}, deadline)
    events = result.get("events", [])
    if (
        not events
        or type(result.get("before_count")) is not int
        or type(result.get("after_count")) is not int
        or not 0 <= result["before_count"] < result["after_count"]
        or any(
            event.get("kind") != result.get("major_kind")
            or not result["started_ms"] <= event["startTime"]
            or event["startTime"] + event["duration"] > result["ended_ms"]
            for event in events
        )
    ):
        raise ValueError("no actual request-window Node major GC event")
    (output / "runtime-gc.json").write_text(json.dumps(result))
    return {
        "before_count": result["before_count"],
        "after_count": result["after_count"],
        "source": "node:perf_hooks:major-gc",
    }


def emit(value):
    sys.stdout.write(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def exchange(cfg, request):
    validate_request(request)
    if (
        request.get("schema") != "nanolab-soak-diagnostic-helper-v1"
        or request["target"] != cfg["target"]
    ):
        raise ValueError("unprovisioned schema or target")
    identity(cfg)
    if request["kind"] == "inspect":
        emit(
            {
                "kind": "result",
                "target": cfg["target"],
                "running": True,
                "remote_finished": True,
            }
        )
        return
    if request["max_bytes"] < cfg["quota_bytes"]:
        raise ValueError("request reservation smaller than target-side quota")
    with open("/work/operation.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with TemporaryDirectory(prefix="operation-", dir="/work") as temp:
            scratch = Path(temp)
            output = Path("/out") / uuid4().hex
            output.mkdir(mode=0o700)
            quota(output, cfg["quota_bytes"])
            started = time.monotonic()
            deadline = started + request["timeout_s"]
            operation, runtime = request["operation"], cfg["target"]["runtime"]
            emitted_bytes = 0
            result = {
                "kind": "result",
                "target": cfg["target"],
                "exit_code": 0,
                "completed": True,
                "descendants_reaped": True,
                "remote_finished": True,
            }
            if operation == "gc":
                observed = (
                    gc_jvm(cfg, request, output, deadline, scratch)
                    if runtime == "jvm"
                    else gc_node(cfg, output, deadline)
                )
                result.update(
                    before_count=observed["before_count"],
                    after_count=observed["after_count"],
                    full_gc_event="full-gc.json",
                )
                event = {
                    "schema": "nanolab-soak-v1",
                    "kind": "full_gc_completed",
                    "target": cfg["target"],
                    "request_id": request["request_id"],
                    "started_s": started,
                    "ended_s": time.monotonic(),
                    "raw_evidence": "runtime-gc.json",
                    **observed,
                }
                (output / "full-gc.json").write_text(json.dumps(event))
            elif runtime == "jvm":
                emitted_bytes = dump_heap_jvm(cfg, request, output, deadline, scratch)
            else:
                node_call(
                    cfg,
                    {
                        "operation": "heap_dump",
                        "path": target_path(cfg, output / "capture.heapsnapshot"),
                        "max_bytes": cfg["quota_bytes"],
                    },
                    deadline,
                )
                if (output / "capture.heapsnapshot").stat().st_size <= 0:
                    raise ValueError("empty Node snapshot")
            identity(cfg)
            total = emitted_bytes
            for path in sorted(output.iterdir()):
                with path.open("rb") as stream:
                    while chunk := stream.read(48 * 1024):
                        total += len(chunk)
                        if total > request["max_bytes"] or time.monotonic() >= deadline:
                            raise ValueError("artifact or time budget exhausted")
                        emit(
                            {
                                "kind": "artifact",
                                "name": path.name,
                                "data_base64": base64.b64encode(chunk).decode(),
                            }
                        )
                path.unlink()
            output.rmdir()
            emit(result)


def main():
    if sys.argv[1] == "hold":
        # /proc/<holder>/root access uses a ptrace read check. Same UID and
        # dumpability are necessary; capability escalation is never attempted.
        if ctypes.CDLL(None).prctl(4, 1, 0, 0, 0) != 0:
            raise OSError("cannot establish same-UID proc-root access")
        while True:
            time.sleep(60)
    value = json.loads(base64.b64decode(sys.argv[2], validate=True))
    if sys.argv[1] == "probe":
        print(json.dumps(probe(value)))
    elif sys.argv[1] == "memory":
        print(json.dumps(memory(value)))
    elif sys.argv[1] == "exchange":
        try:
            exchange(value["config"], value["request"])
        except Exception as error:
            emit(
                {
                    "kind": "result",
                    "target": value["config"]["target"],
                    "exit_code": 1,
                    "completed": False,
                    "descendants_reaped": False,
                    "remote_finished": False,
                    "error": str(error)[:1024],
                }
            )
    else:
        raise ValueError("unsupported worker mode")


if __name__ == "__main__":
    main()
