"""Host CPU placement on a remote Multipass site.

The local checks in `host_cpu` read /sys and /proc and ask the kernel for
thread masks. On a site the same checks run unchanged against a mirror of the
few files they need; thread masks come from each task's `Cpus_allowed_list`,
the kernel's own rendering of the same affinity.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from nanolab.config.one_shot import HostCpuPlacement, HostCpuTarget
from nanolab.one_shot import host_cpu
from nanolab.one_shot.sites import SiteHost

CPU_ROOT = "/sys/devices/system/cpu"


def parse_cpu_list(text: str) -> list[int]:
    """Parse a kernel CPU list such as ``0-2,5``."""
    cpus: set[int] = set()
    for part in text.strip().split(","):
        if not part:
            continue
        first, _, last = part.partition("-")
        cpus.update(range(int(first), int(last or first) + 1))
    return sorted(cpus)


def status_affinity(thread: Path) -> list[int]:
    """Read a thread's CPU mask from its mirrored /proc status file."""
    for line in (thread / "status").read_text().splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return parse_cpu_list(line.partition(":")[2])
    raise ValueError(f"no Cpus_allowed_list for thread {thread.name}")


def mirror_qemu_processes(host: SiteHost, root: Path) -> Path:
    """Mirror what QEMU discovery and mask inspection read; return the /proc root."""
    host.mirror(["/proc/[0-9]*/comm"], root)
    proc = root / "proc"
    qemu = [
        directory.name
        for directory in proc.iterdir()
        if (directory / "comm").read_text().startswith("qemu-system")
    ]
    if qemu:
        host.mirror(
            [
                *(f"/proc/{pid}/cmdline" for pid in qemu),
                *(f"/proc/{pid}/task/[0-9]*/status" for pid in qemu),
            ],
            root,
        )
    return proc


def validate_site_placement(
    host: SiteHost, nodes: dict[str, HostCpuTarget], logging_cpus: list[int]
) -> dict:
    """Run the local hardware checks against the site's CPUs."""
    cpus = sorted({*logging_cpus, *(c for t in nodes.values() for c in t.cpus)})
    with tempfile.TemporaryDirectory(prefix="nl-site-cpu-") as directory:
        root = host.mirror(
            [
                f"{CPU_ROOT}/cpu{cpu}/{name}"
                for cpu in cpus
                for name in (
                    "online",
                    "cpufreq/cpuinfo_max_freq",
                    "cpufreq/scaling_governor",
                )
            ],
            Path(directory),
        )
        # Operator CPUs belong to the operator host and are validated there.
        policy = HostCpuPlacement.model_construct(
            nodes=nodes, operator_cpus=[], logging_cpus=list(logging_cpus)
        )
        return host_cpu.validate_host_placement(
            policy, cpu_root=root / CPU_ROOT.lstrip("/")
        )


def inspect_site_vm(host: SiteHost, vm_name: str, cpus: list[int]) -> dict:
    """Check every thread of a site's owned QEMU process against `cpus`."""
    with tempfile.TemporaryDirectory(prefix="nl-site-proc-") as directory:
        proc = mirror_qemu_processes(host, Path(directory))
        return host_cpu.inspect_vm_placement(
            vm_name, cpus, proc_root=proc, read_affinity=status_affinity
        )


def pin_site_vm(host: SiteHost, vm_name: str, cpus: list[int]) -> dict:
    """Pin only an identified newly owned VM on the site and verify its masks."""
    with tempfile.TemporaryDirectory(prefix="nl-site-proc-") as directory:
        pid = host_cpu.owned_qemu_pid(
            vm_name, proc_root=mirror_qemu_processes(host, Path(directory))
        )
    result = host.run(
        (
            "sudo",
            "-n",
            "taskset",
            "-apc",
            ",".join(str(cpu) for cpu in sorted(cpus)),
            str(pid),
        ),
        timeout=30,
    )
    facts = inspect_site_vm(host, vm_name, cpus)
    if facts["qemuPid"] != pid:
        raise ValueError("owned QEMU process identity changed during placement")
    return {
        **facts,
        "site": host.site.ssh,
        "tasksetStdout": result.stdout,
        "tasksetStderr": result.stderr,
    }
