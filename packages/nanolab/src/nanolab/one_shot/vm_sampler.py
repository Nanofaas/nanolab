"""Standalone Linux/cgroupv2 sampler transferred to an owned VM (stdlib only)."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import resource
import signal
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def cpu_delta(before: str, after: str) -> dict:
    """Return fractions of total VM CPU time without double-counting guest time."""
    old = [int(x) for x in before.splitlines()[0].split()[1:9]]
    new = [int(x) for x in after.splitlines()[0].split()[1:9]]
    count = sum(
        line.startswith("cpu") and line[3:4].isdigit() for line in after.splitlines()
    )
    old_count = sum(
        line.startswith("cpu") and line[3:4].isdigit() for line in before.splitlines()
    )
    delta = [b - a for a, b in zip(old, new, strict=True)]
    if (
        len(delta) != 8
        or count < 1
        or count != old_count
        or min(delta) < 0
        or sum(delta) <= 0
    ):
        raise ValueError("invalid/nonadvancing VM CPU counters or vCPU count changed")
    total = sum(delta)
    busy = total - delta[3] - delta[4] - delta[7]
    return {
        "vcpuCount": count,
        "cpuUtilization": busy / total,
        "cpuUsedCores": count * busy / total,
        "cpuIowaitFraction": delta[4] / total,
        "cpuStealFraction": delta[7] / total,
    }


def memory_values(text: str) -> dict:
    """Linux MemAvailable includes reclaimable caches; use byte units everywhere."""
    values = {
        row.split(":")[0]: int(row.split()[1]) * 1024
        for row in text.splitlines()
        if ":" in row
    }
    try:
        total, available, swap, free = (
            values[k] for k in ["MemTotal", "MemAvailable", "SwapTotal", "SwapFree"]
        )
    except KeyError as error:
        raise ValueError("VM memory counters unavailable") from error
    if not 0 <= available <= total or not 0 <= free <= swap or total <= 0:
        raise ValueError("invalid VM memory counters")
    return {
        "memoryTotalBytes": total,
        "memoryAvailableBytes": available,
        "memoryUsedBytes": total - available,
        "swapTotalBytes": swap,
        "swapFreeBytes": free,
        "swapUsedBytes": swap - free,
    }


def container_delta(
    previous: dict, current: dict, *, elapsed: float, vcpus: int, quota: float | None
) -> dict:
    """CPU usage is core-seconds; throttle duration need not be <= wall duration."""
    keys = ["cpuUsageUsec", "nrPeriods", "nrThrottled", "throttledUsec"]
    values = {key: current[key] - previous[key] for key in keys}
    if elapsed <= 0 or vcpus <= 0 or min(values.values()) < 0:
        raise ValueError("container counters reset/nonadvancing clock")
    cores = values["cpuUsageUsec"] / (elapsed * 1e6)
    return {
        "cpuUsedCores": cores,
        "cpuVmFraction": cores / vcpus,
        "cpuQuotaFraction": cores / quota if quota else None,
        "periodsDelta": values["nrPeriods"],
        "throttledPeriodsDelta": values["nrThrottled"],
        "throttledUsecDelta": values["throttledUsec"],
    }


def container_identity(inspect: dict) -> dict:
    """Identify each physical replica separately from CP and Fluent Bit."""
    name = inspect["Name"].lstrip("/")
    labels = inspect["Config"].get("Labels") or {}
    role = "other"
    function = None
    replica = None
    if name == "nanolab-one-shot-control-plane":
        role = "control-plane"
    elif name.startswith("nanolab-logs-"):
        role = "collector"
    elif labels.get("io.nanofaas.managed") == "true":
        role = "function"
        function = labels["io.nanofaas.function"].split("/", 1)[-1]
        replica = int(labels["io.nanofaas.replica"])
    return {
        "containerId": inspect["Id"],
        "containerName": name,
        "role": role,
        "function": function,
        "replica": replica,
    }


def correlate_sample(
    sample: dict,
    *,
    anchor: datetime,
    warmup: int,
    period: int,
    epochs: int,
    mode: str,
    functions: list[str],
    run_id: str,
) -> dict:
    """Correlate UTC directly to the frozen boundaries, including warmup."""
    timestamp = datetime.fromisoformat(sample["timestampUtc"])
    if timestamp.tzinfo is None or anchor.tzinfo is None:
        raise ValueError("resource timestamps must have UTC timezone")
    offset = (timestamp - anchor).total_seconds()
    phase, epoch = ("before", None)
    if -warmup <= offset < 0:
        phase, epoch = "warmup", -1
    elif 0 <= offset < period * epochs:
        phase, epoch = "measurement", int(offset // period)
    elif offset >= period * epochs:
        phase = "after"
    return {
        **sample,
        "runId": run_id,
        "mode": mode,
        "phase": phase,
        "epoch": epoch,
        "functions": functions,
    }


class DockerSocket(http.client.HTTPConnection):
    """Reuse the existing local Docker daemon without spawning docker per sample."""

    def __init__(self):
        """Open a bounded Unix socket connection."""
        super().__init__("localhost", timeout=2)

    def connect(self):
        """Connect only to the VM-local Docker daemon."""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")

    def get_json(self, path: str):
        """Read a complete successful inventory response."""
        self.request("GET", path)
        response = self.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"Docker inventory HTTP {response.status}")
        return json.loads(body)


def process_identity(pid: int) -> str:
    """PID plus kernel start ticks prevents counter reuse across incarnations."""
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return str(pid) + ":" + stat[19]


def read_container(inspect: dict) -> dict:
    """Read physical cgroup counters with PID-incarnation checks on both sides."""
    pid = inspect["State"]["Pid"]
    if pid <= 0 or not inspect["State"]["Running"]:
        raise ValueError("container is not running")
    incarnation = process_identity(pid)
    if inspect.get("_samplerIncarnation", incarnation) != incarnation:
        raise ValueError("cached Docker PID incarnation changed")
    relative = next(
        line[3:]
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    if ".." in Path(relative).parts or not relative.startswith("/"):
        raise ValueError("invalid cgroup path")
    directory = Path("/sys/fs/cgroup") / relative.lstrip("/")
    cpu = {
        key: int(value)
        for key, value in (
            line.split() for line in (directory / "cpu.stat").read_text().splitlines()
        )
    }
    maximum, period = (directory / "cpu.max").read_text().split()
    quota = None if maximum == "max" else int(maximum) / int(period)

    def value(filename):
        text = (directory / filename).read_text().strip()
        return None if text == "max" else int(text)

    row = {
        **container_identity(inspect),
        "incarnation": incarnation,
        "cpuQuotaCores": quota,
        "cpuUsageUsec": cpu["usage_usec"],
        "nrPeriods": cpu.get("nr_periods", 0),
        "nrThrottled": cpu.get("nr_throttled", 0),
        "throttledUsec": cpu.get("throttled_usec", 0),
        "memoryCurrentBytes": value("memory.current"),
        "memoryLimitBytes": value("memory.max"),
        "memoryPeakBytes": value("memory.peak"),
        "swapCurrentBytes": value("memory.swap.current"),
        "swapLimitBytes": value("memory.swap.max"),
        "memoryEvents": {
            k: int(v)
            for k, v in (
                line.split()
                for line in (directory / "memory.events").read_text().splitlines()
            )
        },
    }
    if process_identity(pid) != incarnation:
        raise ValueError("container incarnation changed during sample")
    return row


def sample_loop(node: str, *, duration: float, context: dict | None = None) -> None:
    """One local sample per second; skipped slots/errors remain explicit evidence."""
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    previous_cpu = None
    previous_time = None
    previous_containers = {}
    inventory = {}
    connection = DockerSocket()
    started = time.monotonic()
    target = started
    previous_agent_cpu = None
    while running and time.monotonic() - started < duration:
        now = time.monotonic()
        wall = datetime.now(UTC).isoformat()
        row = {
            "schemaVersion": 1,
            "timestampUtc": wall,
            "monotonicSeconds": now,
            "node": node,
            "errors": [],
            "containers": [],
        }
        try:
            text = Path("/proc/stat").read_text()
            row.update(memory_values(Path("/proc/meminfo").read_text()))
            count = sum(
                line.startswith("cpu") and line[3:4].isdigit()
                for line in text.splitlines()
            )
            row["vcpuCount"] = count
            row["cpuCountersTicks"] = [
                int(value) for value in text.splitlines()[0].split()[1:9]
            ]
            row["clockTicksPerSecond"] = os.sysconf("SC_CLK_TCK")
            row["cpuUtilization"] = None
            if previous_cpu is not None:
                row.update(cpu_delta(previous_cpu, text))
            row["elapsedSeconds"] = (
                now - previous_time if previous_time is not None else None
            )
            previous_cpu = text
            containers = connection.get_json("/containers/json")
            identifiers = {item["Id"] for item in containers}
            row["expectedContainerIds"] = sorted(identifiers)
            inventory = {
                key: value for key, value in inventory.items() if key in identifiers
            }
            observed = {}
            for identifier in sorted(identifiers):
                try:
                    if identifier not in inventory:
                        inventory[identifier] = connection.get_json(
                            "/containers/" + identifier + "/json"
                        )
                        inventory[identifier]["_samplerIncarnation"] = process_identity(
                            inventory[identifier]["State"]["Pid"]
                        )
                    current = read_container(inventory[identifier])
                    prior = previous_containers.get(identifier)
                    current.update(
                        {
                            "cpuUsedCores": None,
                            "cpuVmFraction": None,
                            "cpuQuotaFraction": None,
                            "periodsDelta": None,
                            "throttledPeriodsDelta": None,
                            "throttledUsecDelta": None,
                        }
                    )
                    if prior and prior["incarnation"] == current["incarnation"]:
                        current.update(
                            container_delta(
                                prior,
                                current,
                                elapsed=now - prior["sampleMonotonic"],
                                vcpus=count,
                                quota=current["cpuQuotaCores"],
                            )
                        )
                    current["sampleMonotonic"] = now
                    observed[identifier] = current
                    row["containers"].append(current)
                except Exception as error:
                    inventory.pop(identifier, None)
                    row["errors"].append(
                        {"containerId": identifier, "error": str(error)}
                    )
            previous_containers = observed
        except Exception as error:
            row["errors"].append({"error": str(error)})
            connection.close()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        own_cpu = usage.ru_utime + usage.ru_stime
        row["sampler"] = {
            "cpuSeconds": own_cpu,
            "memoryRssPeakBytes": usage.ru_maxrss * 1024,
            "cpuUsedCores": (own_cpu - previous_agent_cpu) / (now - previous_time)
            if previous_agent_cpu is not None
            and previous_time is not None
            and now > previous_time
            else None,
            "collectionSeconds": time.monotonic() - now,
        }
        previous_agent_cpu, previous_time = own_cpu, now
        if context is not None:
            row = correlate_sample(
                row,
                anchor=datetime.fromisoformat(context["anchor"]),
                warmup=context["warmup"],
                period=context["period"],
                epochs=context["epochs"],
                mode=context["mode"],
                functions=context["functions"],
                run_id=context["run_id"],
            )
        print(json.dumps(row, separators=(",", ":"), allow_nan=False), flush=True)
        target += 1
        if target <= time.monotonic():
            target = started + int(time.monotonic() - started) + 1
        time.sleep(max(0, target - time.monotonic()))
    connection.close()


def stop_owned_sampler(script: str) -> None:
    """Signal only Python processes executing this unique uploaded script."""
    owned = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            args = (path / "cmdline").read_bytes().split(b"\0")
            if (
                len(args) > 2
                and args[0].rsplit(b"/", 1)[-1] == b"python3"
                and args[1:3] == [b"-u", script.encode()]
            ):
                owned.append((int(path.name), process_identity(int(path.name))))
        except (FileNotFoundError, ProcessLookupError):
            continue
    for pid, identity in owned:
        try:
            if process_identity(pid) == identity:
                os.kill(pid, signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError):
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        alive = []
        for pid, identity in owned:
            try:
                if process_identity(pid) == identity:
                    # A zombie has exited and can safely be reaped by Multipass.
                    state = (
                        Path(f"/proc/{pid}/stat")
                        .read_text()
                        .rsplit(")", 1)[1]
                        .split()[0]
                    )
                    if state != "Z":
                        alive.append(pid)
            except (FileNotFoundError, ProcessLookupError):
                pass
        if not alive:
            return
        time.sleep(0.1)
    raise RuntimeError("owned VM sampler process did not stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--context", default="null")
    options = parser.parse_args()
    if options.stop:
        stop_owned_sampler(str(Path(__file__).resolve()))
        sys.exit(0)
    if not options.node or options.duration is None or options.duration <= 0:
        parser.error("positive --duration and --node are required")
    sample_loop(
        options.node, duration=options.duration, context=json.loads(options.context)
    )
