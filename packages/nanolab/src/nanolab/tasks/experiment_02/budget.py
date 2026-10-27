"""One cgroup holds the deployment under measurement, and its budget is the cell's.

The budget of experiment 02 is a whole deployment's: management services,
gateways, function instances and the execution backend that runs them. On a
Docker host that backend is dockerd and containerd, so they live in the budget
too, in `nanolab-sut.slice`. Docker's `cgroup-parent` puts every container it
creates there as well, including those a platform creates on its own.

What must not count is the measurement: the registry the images come from and
the sampler that reads the cgroup. They live in `nanolab-infra.slice`, beside
the budget rather than inside it; the agents in its own child,
`nanolab-infra-observers.slice`, so their cost is measured without the
registry's. The slices nest as systemd derives from the dashes in their names.

The budget is applied as a CPU quota and a memory limit on the slice, with swap
forbidden, and read back from the cgroup files before a cell measures anything:
a slice that is missing or unbounded must stop the cell, not let it measure an
unbudgeted deployment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from nanolab.config.experiment_02 import Budget
from nanolab.tasks.components.operations import RemoteCommandOperation
from nanolab.tasks.experiment_02.refusal import CellRefusedError

SUT_SLICE = "nanolab-sut.slice"
INFRA_SLICE = "nanolab-infra.slice"
OBSERVER_SLICE = "nanolab-infra-observers.slice"
SUT_CGROUP = PurePosixPath("/sys/fs/cgroup/nanolab.slice/nanolab-sut.slice")
OBSERVER_CGROUP = PurePosixPath(
    "/sys/fs/cgroup/nanolab.slice/nanolab-infra.slice/nanolab-infra-observers.slice"
)
_INFRA_CGROUP = "/nanolab.slice/nanolab-infra.slice"
_SUT_CGROUP = "/nanolab.slice/nanolab-sut.slice"
# The engine's own cgroups inside the slice, reported apart from the platform.
ENGINE_CGROUPS = ("docker.service", "containerd.service")
# The registry `ensure-registry.yml` starts; recreated outside the budget.
REGISTRY_CONTAINER = "nanofaas-e2e-registry"
_MIB = 1024 * 1024
# Processes that belong to the execution backend wherever they run.
_ENGINE_COMMANDS = frozenset(
    {
        "dockerd",
        "containerd",
        "containerd-shim",
        "containerd-shim-runc-v2",
        "runc",
        "docker-proxy",
    }
)

# `NANOLAB_SLICE_ROOT` exists for the tests, which run the script against a
# scratch directory with fake systemctl and docker; on a VM it is empty.
SLICE_SETUP_SCRIPT = f"""set -eu
root="${{NANOLAB_SLICE_ROOT:-}}"
units="$root/etc/systemd/system"
mkdir -p "$units/docker.service.d" "$units/containerd.service.d" "$root/etc/docker"
printf '[Unit]\\nDescription=NanoLab experiment 02\\n\\n[Slice]\\n' \\
  > "$units/nanolab.slice"
printf '[Unit]\\nDescription=Deployment under measurement\\n\\n[Slice]\\n' \\
  > "$units/{SUT_SLICE}"
printf '[Unit]\\nDescription=Measurement infrastructure\\n\\n[Slice]\\n' \\
  > "$units/{INFRA_SLICE}"
printf '[Unit]\\nDescription=Measurement agents\\n\\n[Slice]\\n' \\
  > "$units/{OBSERVER_SLICE}"
for service in docker containerd; do
  printf '[Service]\\nSlice={SUT_SLICE}\\n' \\
    > "$units/$service.service.d/nanolab-slice.conf"
done
python3 -I -c '
import json, os, sys
path = sys.argv[1]
settings = {{}}
if os.path.exists(path) and os.path.getsize(path):
    with open(path) as stream:
        settings = json.load(stream)
settings["cgroup-parent"] = "{SUT_SLICE}"
with open(path, "w") as stream:
    stream.write(json.dumps(settings, indent=2, sort_keys=True) + "\\n")
' "$root/etc/docker/daemon.json"
swapoff -a
systemctl daemon-reload
systemctl restart containerd docker
docker rm -f {REGISTRY_CONTAINER} >/dev/null 2>&1 || true
docker run -d --restart unless-stopped --name {REGISTRY_CONTAINER} \\
  --cgroup-parent={INFRA_SLICE} -p 5000:5000 registry:2 >/dev/null
"""


def slice_setup_operation() -> RemoteCommandOperation:
    """Return the provisioning step that creates the slices and moves the engine."""
    return RemoteCommandOperation(
        operation_id="experiment02.slices",
        summary="Put the Docker engine in the measured slice, the registry outside",
        argv=("sudo", "sh", "-c", SLICE_SETUP_SCRIPT),
        execution_target="vm",
    )


def apply_budget_argv(budget: Budget) -> tuple[str, ...]:
    """Return the command that sets the slice's CPU quota and memory limit."""
    return (
        "sudo",
        "systemctl",
        "set-property",
        "--runtime",
        SUT_SLICE,
        f"CPUQuota={budget.cpus * 100:g}%",
        f"MemoryMax={budget.memory_mib}M",
        "MemorySwapMax=0",
    )


def restart_engine_argv() -> tuple[str, ...]:
    """Return the command that restarts the engine, so every cell starts it fresh."""
    return ("sudo", "systemctl", "restart", "containerd", "docker")


def read_budget_argv() -> tuple[str, ...]:
    """Return the command that prints the slice's cpu.max, memory.max and swap."""
    return (
        "cat",
        str(SUT_CGROUP / "cpu.max"),
        str(SUT_CGROUP / "memory.max"),
        str(SUT_CGROUP / "memory.swap.max"),
    )


def parse_budget(text: str) -> Budget:
    """Read the budget the kernel enforces from `read_budget_argv`'s output."""
    lines = text.split()
    if len(lines) != 4:
        raise CellRefusedError(f"unexpected cgroup budget files: {text!r}")
    quota, period, memory, swap = lines
    if quota == "max" or memory == "max":
        raise CellRefusedError("the measured slice is unbounded")
    if swap != "0":
        raise CellRefusedError("the measured slice may swap")
    memory_bytes = int(memory)
    if memory_bytes % _MIB:
        raise CellRefusedError(f"memory.max is not a whole number of MiB: {memory}")
    return Budget(cpus=int(quota) / int(period), memoryMiB=memory_bytes // _MIB)


PLATFORM_UNIT = "nanolab-e02-platform"


def host_service(
    argv: Sequence[str], *, env: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Return the command that runs a platform's host process inside the budget.

    One transient unit in the measured slice; stopping it stops every process
    the platform started outside Docker.
    """
    variables = [
        part for key, value in (env or {}).items() for part in ("-E", f"{key}={value}")
    ]
    return (
        "sudo", "systemd-run", "--quiet", "--collect", "--unit", PLATFORM_UNIT,
        "--slice", SUT_SLICE, "--property=KillMode=control-group",
        *variables, "--", *argv,
    )  # fmt: skip


def process_listing_argv() -> tuple[str, ...]:
    """Return the command that prints `pid comm cgroup` for every process."""
    script = (
        "for d in /proc/[0-9]*; do "
        'c=$(cat "$d/comm" 2>/dev/null) || continue; '
        "g=$(sed -n 's/^0:://p' \"$d/cgroup\" 2>/dev/null) || continue; "
        'echo "${d#/proc/} $c $g"; done'
    )
    return ("sh", "-c", script)


def processes_outside(listing: str, host_commands: Sequence[str] = ()) -> list[str]:
    """Return the engine, container and platform processes that escaped the budget.

    The engine's processes, any process in a Docker scope and the platform's
    own host processes (`host_commands`, as `comm` shows them) must be in the
    measured slice. The only exception is the infrastructure slice, which holds
    the registry by design.
    """
    outside: list[str] = []
    for line in listing.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        _pid, command, cgroup = parts
        engine = (
            command in _ENGINE_COMMANDS
            or command in host_commands
            or "/docker-" in cgroup
        )
        if not engine or cgroup.startswith((_SUT_CGROUP, _INFRA_CGROUP)):
            continue
        outside.append(line.strip())
    return outside
