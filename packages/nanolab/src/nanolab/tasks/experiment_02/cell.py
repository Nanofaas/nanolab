"""Run one experiment-02 cell on the stack and loadgen VMs.

The order is the protocol's, and each step guards the next:

1. remove what an earlier cell left, and stop if a container or the sampler
   survives; then record the slice's OOM counter before anything changes it;
2. start the sampler, outside the budget, and wait for its first sample, so it
   sees the engine restart that follows;
3. apply the budget, restart the engine inside it, and read the budget back
   from the cgroup files, stopping on any difference;
4. start the platform and wait for its first correct response; a platform
   that never gives one failed to start, which is a result, not an error;
5. stop if a deployment process escaped the budget, record what runs, mark the
   load as started, and run the calendar from the loadgen;
6. whatever happened, collect the evidence and clean up, each operation on
   its own: the OOM counter again, the containers' state and logs, the
   sampler's files, the generator's ledger, the teardown. A failed cleanup
   step is written to `diagnostics/cleanup-errors.json` and never replaces
   the error that came first.

An OOM kill in the budget during the attempt, documented by the counter or by
a container's `OOMKilled`, makes the cell `oom` whatever else happened, the
engine restart and a failed generator included. Missing OOM evidence is
unknown, not zero, and stops the matrix. Without an OOM: a readiness timeout
is `startup_failed`, any other error is raised, and only a calendar that ran
to the end is `measured`. The receipt is written by the caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from traceback import format_exception_only
from typing import Any

from nanolab.comparison.experiment_01 import (
    DIAGNOSTICS,
    EVENTS,
    LOAD_STARTED,
    SUMMARY,
    Outcome,
    mark_load_started,
)
from nanolab.comparison.experiment_02 import SAMPLES, SCHEDULER, Experiment02Cell
from nanolab.comparison.prepare import comparison_remote_command, comparison_transfer_to
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.tasks.experiment_02.budget import (
    OBSERVER_CGROUP,
    OBSERVER_SLICE,
    PLATFORM_UNIT,
    SUT_CGROUP,
    apply_budget_argv,
    parse_budget,
    process_listing_argv,
    processes_outside,
    read_budget_argv,
    restart_engine_argv,
)
from nanolab.tasks.experiment_02.generator import generator_environment, run_generator
from nanolab.tasks.experiment_02.platforms import (
    ContainersRemainError,
    Platform,
    baseline_teardown,
    leftover_containers,
)
from nanolab.tasks.experiment_02.refusal import CellRefusedError
from nanolab.tasks.experiment_02.samples import (
    SAMPLE_INTERVAL_SECONDS,
    SAMPLER_SCRIPT,
    oom_kills,
    read_cgroup_samples,
)
from nanolab.tasks.experiment_02.vm import Vm, exec_status, fetch_if_present

SAMPLER_UNIT = "nanolab-e02-sampler"
CLEANUP_ERRORS = "cleanup-errors.json"
# Each diagnostic probe is bounded, so one hung command cannot hold the rest.
_PROBE_TIMEOUT = ("timeout", "30")


def _run(vm: Vm, argv: tuple[str, ...]) -> str:
    return comparison_remote_command(vm.provider, vm.request, argv)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _sampler_running(stack: Vm) -> bool:
    status = exec_status(stack, ("systemctl", "is-active", "--quiet", SAMPLER_UNIT))
    return status.return_code == 0


def _require_clean(stack: Vm) -> None:
    # An interrupted cell may have left a baseline's unit, project or networks.
    try:
        baseline_teardown(stack)
    except ContainersRemainError as error:
        raise CellRefusedError(
            f"containers from an earlier cell remain: {error.names}"
        ) from error
    remaining = leftover_containers(stack)
    if remaining:
        raise CellRefusedError(f"containers from an earlier cell remain: {remaining}")
    _ = exec_status(stack, ("sudo", "systemctl", "stop", SAMPLER_UNIT))
    if _sampler_running(stack):
        raise CellRefusedError(f"an earlier cell's sampler still runs: {SAMPLER_UNIT}")
    unit = ("systemctl", "is-active", "--quiet", PLATFORM_UNIT)
    if exec_status(stack, unit).return_code == 0:
        raise CellRefusedError(
            f"an earlier cell's platform still runs: {PLATFORM_UNIT}"
        )


def _stop_sampler(stack: Vm) -> None:
    _ = exec_status(stack, ("sudo", "systemctl", "stop", SAMPLER_UNIT))
    if _sampler_running(stack):
        raise RuntimeError(f"{SAMPLER_UNIT} is still running")


def _cgroup_state(stack: Vm) -> dict[str, Any]:
    """Return the slice's identity and its `memory.events`, read without Docker."""
    output = _run(
        stack,
        ("sh", "-c", f"stat -c %i {SUT_CGROUP} && cat {SUT_CGROUP}/memory.events"),
    )
    inode, *lines = output.splitlines()
    events = {
        key: int(value)
        for key, _, value in (line.partition(" ") for line in lines)
        if value.strip().isdigit()
    }
    return {"cgroup": str(SUT_CGROUP), "inode": inode.strip(), "memoryEvents": events}


def _start_sampler(
    stack: Vm, remote_root: PurePosixPath, metrics_url: str | None, run_dir: Path
) -> PurePosixPath | None:
    """Start the sampler; return where its scheduler scrapes go, if anywhere."""
    remote_script = remote_root / SAMPLER_SCRIPT.name
    remote_samples = remote_root / "cgroup-samples.jsonl"
    _ = _run(stack, ("mkdir", "-p", str(remote_root)))
    comparison_transfer_to(stack.provider, stack.request, SAMPLER_SCRIPT, remote_script)
    remote_scheduler = None
    scheduler: tuple[str, ...] = ()
    if metrics_url is not None:
        remote_scheduler = remote_root / "scheduler-samples.jsonl"
        scheduler = (
            "--metrics-url", metrics_url, "--scheduler-out", str(remote_scheduler)
        )  # fmt: skip
    _ = _run(
        stack,
        (
            "sudo", "systemd-run", "--quiet", "--collect", "--unit", SAMPLER_UNIT,
            "--slice", OBSERVER_SLICE, "python3", "-I", str(remote_script),
            "--root", str(SUT_CGROUP), "--observer-root", str(OBSERVER_CGROUP),
            "--interval", f"{SAMPLE_INTERVAL_SECONDS:g}",
            "--out", str(remote_samples), *scheduler,
        ),
    )  # fmt: skip
    _ = _run(
        stack,
        (
            "sh",
            "-c",
            f"for i in $(seq 50); do [ -s {remote_samples} ] && exit 0; "
            "sleep 0.2; done; echo 'no first sample' >&2; exit 1",
        ),
    )
    agents = _run(
        stack,
        (
            "systemctl",
            "show",
            "-p",
            "Id,MainPID,ControlGroup,ActiveState",
            SAMPLER_UNIT,
        ),
    )
    (run_dir / "agents.txt").write_text(agents)
    return remote_scheduler


def _apply_budget(stack: Vm, cell: Experiment02Cell, run_dir: Path) -> None:
    _ = _run(stack, apply_budget_argv(cell.budget))
    _ = _run(stack, restart_engine_argv())
    observed = parse_budget(_run(stack, read_budget_argv()))
    _write_json(
        run_dir / "budget.json",
        {
            "applied": cell.budget.model_dump(mode="json", by_alias=True),
            "observed": observed.model_dump(mode="json", by_alias=True),
        },
    )
    if observed != cell.budget:
        raise CellRefusedError(
            f"the measured slice enforces {observed.label}, not {cell.budget.label}"
        )


def _container_evidence(stack: Vm, diagnostics: Path) -> list[str]:
    """Save the containers' listing, state and logs; return the OOM-killed ones."""
    listing = exec_status(stack, (*_PROBE_TIMEOUT, "docker", "ps", "-a"))
    (diagnostics / "containers.txt").write_text(listing.stdout + listing.stderr)
    names = leftover_containers(stack)
    killed: list[str] = []
    if names:
        # Containers a platform removes meanwhile are missing, not an error.
        state = exec_status(
            stack,
            (
                *_PROBE_TIMEOUT, "docker", "inspect", "--format",
                "{{.Name}} {{.State.OOMKilled}}", *names,
            ),
        ).stdout  # fmt: skip
        (diagnostics / "containers-state.txt").write_text(state)
        killed = [
            line.split()[0].lstrip("/")
            for line in state.splitlines()
            if line.split()[-1:] == ["true"]
        ]
    for name in names:
        logs = exec_status(
            stack, (*_PROBE_TIMEOUT, "docker", "logs", "--tail", "200", name)
        )
        (diagnostics / f"logs-{name}.txt").write_text(logs.stdout + logs.stderr)
    return killed


def _oom(
    before: Mapping[str, Any],
    after: Mapping[str, Any] | None,
    sampled: int | None,
    killed: list[str] | None,
) -> tuple[bool | None, str]:
    """Decide whether the attempt lost a process to the OOM killer; `None` is unknown.

    The slice's counter, read over SSH before and after, is the primary
    evidence; the samples and the containers' state confirm it.
    """
    counted: int | None = None
    if after is not None and before["inode"] == after["inode"]:
        start = before["memoryEvents"].get("oom_kill")
        end = after["memoryEvents"].get("oom_kill")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            counted = end - start
    if (counted or 0) > 0 or (sampled or 0) > 0 or killed:
        return True, (
            f"oom_kill increase: {counted} by the cgroup counter, {sampled} in the "
            f"samples; OOMKilled containers: {killed or []}"
        )
    if counted == 0 or (counted is None and sampled == 0 and killed is not None):
        return False, ""
    return None, "the slice's OOM counter could not be compared across the attempt"


def run_cell(
    cell: Experiment02Cell,
    *,
    stack: Vm,
    loadgen: Vm,
    platform: Platform,
    protocol: Experiment02Protocol,
    payload: bytes,
    script: Path,
    run_dir: Path,
    remote_root: PurePosixPath,
    host: str,
) -> Outcome:
    """Run `cell` into `run_dir` and return its outcome.

    Raises instead of returning when the cell cannot be measured as the
    protocol requires: a budget the kernel does not enforce, a process outside
    it, a generator that did not finish, OOM evidence that is missing. A
    `CellRefusedError` is a refusal. The caller decides on retries and checks
    `diagnostics/cleanup-errors.json` before any other cell.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = run_dir / DIAGNOSTICS
    diagnostics.mkdir(exist_ok=True)
    _require_clean(stack)
    before = _cgroup_state(stack)
    _write_json(diagnostics / "cgroup-before.json", before)
    remote_samples = remote_root / "cgroup-samples.jsonl"
    # The scheduler file is looked for only where the platform has metrics.
    remote_scheduler = (
        remote_root / "scheduler-samples.jsonl" if platform.metrics_url else None
    )
    error: BaseException | None = None
    timed_out = False
    try:
        _ = _start_sampler(stack, remote_root, platform.metrics_url, run_dir)
        _apply_budget(stack, cell, run_dir)
        timings = platform.start(
            stack, protocol=protocol, payload=payload, evidence=run_dir
        )
        if timings is None:
            timed_out = True
        else:
            outside = processes_outside(
                _run(stack, process_listing_argv()), platform.host_commands
            )
            if outside:
                (diagnostics / "processes-outside.txt").write_text(
                    "\n".join(outside) + "\n"
                )
                raise CellRefusedError(
                    f"deployment processes outside the measured slice: {outside}"
                )
            platform.record(stack, run_dir)
            mark_load_started(run_dir)
            code = run_generator(
                loadgen,
                script=script,
                env=generator_environment(
                    protocol,
                    payload=payload,
                    body=platform.body(payload),
                    target=platform.target(host),
                    secret_headers=platform.secret_headers(stack),
                ),
                remote_dir=remote_root,
                local_dir=run_dir,
            )
            if code:
                raise RuntimeError(f"k6 exited {code}")
    except BaseException as caught:  # classified below, after the evidence
        error = caught
    cleanup_errors: list[dict[str, str]] = []

    def attempt(name: str, step: Callable[[], object]) -> object:
        try:
            return step()
        except Exception as failure:
            cleanup_errors.append(
                {"step": name, "error": "".join(format_exception_only(failure))}
            )
            return None

    after = attempt("cgroup-after", lambda: _cgroup_state(stack))
    if isinstance(after, dict):
        _write_json(diagnostics / "cgroup-after.json", after)
    killed = attempt("containers", lambda: _container_evidence(stack, diagnostics))
    _ = attempt("stop-sampler", lambda: _stop_sampler(stack))
    _ = attempt(
        "fetch-samples",
        lambda: fetch_if_present(stack, remote_samples, run_dir / SAMPLES),
    )
    if remote_scheduler is not None:
        scheduler = remote_scheduler
        _ = attempt(
            "fetch-scheduler",
            lambda: fetch_if_present(stack, scheduler, run_dir / SCHEDULER),
        )
    for name in (EVENTS, SUMMARY):
        if (run_dir / LOAD_STARTED).is_file() and not (run_dir / name).is_file():
            _ = attempt(
                f"fetch-{name}",
                lambda name=name: fetch_if_present(
                    loadgen, remote_root / name, run_dir / name
                ),
            )
    _ = attempt("teardown", lambda: platform.teardown(stack))
    if cleanup_errors:
        _write_json(diagnostics / CLEANUP_ERRORS, cleanup_errors)
    samples = run_dir / SAMPLES
    sampled = (
        oom_kills(read_cgroup_samples(samples).samples) if samples.is_file() else None
    )
    oom, reason = _oom(
        before,
        after if isinstance(after, dict) else None,
        sampled,
        killed if isinstance(killed, list) else None,
    )
    if oom:
        (diagnostics / "oom.txt").write_text(reason + "\n")
        return "oom"
    if isinstance(error, KeyboardInterrupt):
        raise error
    if oom is None:
        raise RuntimeError(f"OOM evidence is missing: {reason}") from error
    if error is not None:
        raise error
    if timed_out:
        (diagnostics / "error.txt").write_text(
            "no correct response before the readiness timeout; see readiness.json\n"
        )
        return "startup_failed"
    return "measured"
