"""Explicit Linux host CPU placement for controlled, run-owned Multipass VMs."""

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from nanolab.config.one_shot import HostCpuPlacement


def validate_host_placement(
    policy: HostCpuPlacement, *, cpu_root: Path = Path("/sys/devices/system/cpu")
) -> dict:
    """Refuse unsupported hardware or changed CPU families before acquisition."""
    selected = set(policy.operator_cpus + policy.logging_cpus)
    selected.update(cpu for target in policy.nodes.values() for cpu in target.cpus)
    facts = {}
    for cpu in sorted(selected):
        directory = cpu_root / f"cpu{cpu}"
        try:
            online = directory / "online"
            if online.exists() and online.read_text().strip() != "1":
                raise ValueError(f"host CPU {cpu} must be online")
            frequency = int((directory / "cpufreq/cpuinfo_max_freq").read_text())
            governor = (directory / "cpufreq/scaling_governor").read_text().strip()
        except (OSError, ValueError) as error:
            if "must be online" in str(error):
                raise
            raise ValueError(f"host CPU {cpu} evidence unavailable") from error
        if frequency <= 0:
            raise ValueError(f"host CPU {cpu} frequency unavailable")
        if governor != "performance":
            raise ValueError(f"host CPU {cpu} governor must remain performance")
        facts[cpu] = {"maxFrequencyKHz": frequency, "governor": governor}
    for node, target in policy.nodes.items():
        if any(
            facts[cpu]["maxFrequencyKHz"] != target.max_frequency_khz
            for cpu in target.cpus
        ):
            raise ValueError(f"{node} host CPU family differs from frozen placement")
    return {
        "nodes": {
            node: target.model_dump(by_alias=True)
            for node, target in policy.nodes.items()
        },
        "operatorCpus": policy.operator_cpus,
        "loggingCpus": policy.logging_cpus,
        "hostCpus": facts,
    }


def owned_qemu_pid(vm_name: str, *, proc_root: Path = Path("/proc")) -> int:
    """Require one QEMU process with an exact owned VM disk path component."""
    if not re.fullmatch(r"nl-os-[a-z0-9][a-z0-9-]{0,95}", vm_name):
        raise ValueError("invalid owned VM name")
    matches = []
    for directory in proc_root.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if not (directory / "comm").read_text().strip().startswith("qemu-system"):
                continue
            args = (directory / "cmdline").read_bytes().split(b"\0")
            disks = [
                arg.decode().partition("file=")[2].split(",")[0]
                for arg in args
                if b"file=" in arg
            ]
        except (OSError, UnicodeError):
            continue
        if any(vm_name in PurePosixPath(disk).parts for disk in disks):
            matches.append(int(directory.name))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one owned QEMU process for {vm_name}")
    return matches[0]


def inspect_vm_placement(
    vm_name: str, cpus: list[int], *, proc_root: Path = Path("/proc")
) -> dict:
    """Check every QEMU thread; missing evidence is a failed placement check."""
    pid = owned_qemu_pid(vm_name, proc_root=proc_root)
    try:
        threads = list((proc_root / str(pid) / "task").iterdir())
        masks = {
            thread.name: sorted(os.sched_getaffinity(int(thread.name)))
            for thread in threads
        }
    except OSError as error:
        raise ValueError(f"{vm_name} thread affinity evidence unavailable") from error
    if not masks:
        raise ValueError(f"{vm_name} thread evidence missing")
    if any(set(mask) != set(cpus) for mask in masks.values()):
        raise ValueError(f"{vm_name} thread affinity differs from frozen placement")
    return {
        "vmName": vm_name,
        "qemuPid": pid,
        "cpus": sorted(cpus),
        "threadMasks": masks,
    }


def pin_vm(vm_name: str, cpus: list[int], *, proc_root: Path = Path("/proc")) -> dict:
    """Pin only an identified newly owned VM and verify the resulting masks."""
    pid = owned_qemu_pid(vm_name, proc_root=proc_root)
    result = subprocess.run(
        [
            "sudo",
            "-n",
            "taskset",
            "-apc",
            ",".join(str(cpu) for cpu in sorted(cpus)),
            str(pid),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if owned_qemu_pid(vm_name, proc_root=proc_root) != pid:
        raise ValueError("owned QEMU process identity changed during placement")
    facts = inspect_vm_placement(vm_name, cpus, proc_root=proc_root)
    return {**facts, "tasksetStdout": result.stdout, "tasksetStderr": result.stderr}


def restore_operator(original: dict, *, proc_root: Path = Path("/proc")) -> None:
    """Restore old masks and the initial main-thread mask for new workers."""
    errors = []
    for thread in (proc_root / "self/task").iterdir():
        try:
            os.sched_setaffinity(
                int(thread.name),
                original["threadMasks"].get(thread.name, original["defaultCpus"]),
            )
        except ProcessLookupError:
            continue
        except OSError as error:
            errors.append(error)
    if errors:
        raise RuntimeError("operator CPU affinity restore incomplete") from errors[0]


def pin_operator(cpus: list[int], *, proc_root: Path = Path("/proc")) -> dict:
    """Pin this owned process and compensate any partially applied masks."""
    threads = list((proc_root / "self/task").iterdir())
    original = {
        "defaultCpus": sorted(os.sched_getaffinity(os.getpid())),
        "threadMasks": {
            t.name: sorted(os.sched_getaffinity(int(t.name))) for t in threads
        },
    }
    if not original["threadMasks"]:
        raise ValueError("operator thread evidence missing")
    try:
        for thread in threads:
            os.sched_setaffinity(int(thread.name), cpus)
    except BaseException as error:
        try:
            restore_operator(original, proc_root=proc_root)
        except Exception as cleanup:
            error.add_note(f"operator affinity compensation: {cleanup}")
        raise
    return original


def pin_logging_project(project: str, cpus: list[int]) -> list[dict]:
    """Pin only containers whose inspected labels match the owned project."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    identifiers = result.stdout.split()
    if not identifiers:
        raise ValueError("owned logging container evidence missing")
    template = (
        '{"id":{{json .Id}},'
        '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"cpuset":{{json .HostConfig.CpusetCpus}}}'
    )

    def inspect():
        result = subprocess.run(
            ["docker", "inspect", "--format=" + template, *identifiers],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        if len(rows) != len(identifiers) or any(
            row["project"] != project for row in rows
        ):
            raise ValueError("logging container ownership mismatch")
        return rows

    inspect()
    mask = ",".join(str(cpu) for cpu in sorted(cpus))
    subprocess.run(
        ["docker", "update", "--cpuset-cpus", mask, *identifiers],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = inspect()
    if any(row["cpuset"] != mask for row in rows):
        raise ValueError("owned logging CPU affinity differs from placement")
    return rows


def collect_host_placement(
    policy: HostCpuPlacement,
    vm_names: dict[str, str],
    *,
    proc_root: Path = Path("/proc"),
    cpu_root: Path = Path("/sys/devices/system/cpu"),
) -> dict:
    """Recheck hardware and all owned process masks immediately before load."""
    if set(vm_names) != set(policy.nodes):
        raise ValueError("host placement VM name coverage must be exact")
    hardware = validate_host_placement(policy, cpu_root=cpu_root)
    operator = {
        thread.name: sorted(os.sched_getaffinity(int(thread.name)))
        for thread in (proc_root / "self/task").iterdir()
    }
    if not operator or any(
        set(mask) != set(policy.operator_cpus) for mask in operator.values()
    ):
        raise ValueError("operator thread affinity differs from placement")
    nodes = {
        node: inspect_vm_placement(vm_names[node], target.cpus, proc_root=proc_root)
        for node, target in policy.nodes.items()
    }
    return {
        "policy": policy.model_dump(by_alias=True),
        "hardware": hardware,
        "nodes": nodes,
        "operatorThreadMasks": operator,
    }
