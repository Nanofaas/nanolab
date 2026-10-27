"""Run one experiment-02 cell on the stack and loadgen VMs.

The order is the protocol's, and each step guards the next:

1. remove what an earlier cell left, or stop: a leftover container would sit in
   the budget and in the idle measurement;
2. apply the budget, restart the engine inside it, and read the budget back
   from the cgroup files, stopping on any difference;
3. start the sampler, outside the budget;
4. deploy and wait for the first correct response: a platform that never
   gives one failed to start, which is a result, not an error;
5. stop if any engine or container process escaped the budget;
6. record what runs and mark the load as started;
7. run the calendar from the loadgen;
8. always stop the sampler, fetch its samples, and tear the platform down.

An OOM kill anywhere in the budget, before or during the load, makes the cell
`oom` whatever else happened. The receipt is written by the caller.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from nanolab.comparison.experiment_01 import DIAGNOSTICS, Outcome, mark_load_started
from nanolab.comparison.experiment_02 import SAMPLES, Experiment02Cell
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.tasks.experiment_02.budget import (
    INFRA_SLICE,
    SUT_CGROUP,
    apply_budget_argv,
    parse_budget,
    process_listing_argv,
    processes_outside,
    read_budget_argv,
    restart_engine_argv,
)
from nanolab.tasks.experiment_02.generator import generator_environment, run_generator
from nanolab.tasks.experiment_02.platforms import Platform, leftover_containers
from nanolab.tasks.experiment_02.samples import (
    SAMPLE_INTERVAL_SECONDS,
    SAMPLER_SCRIPT,
    oom_kills,
    read_cgroup_samples,
)
from nanolab.tasks.experiment_02.vm import Vm

SAMPLER_UNIT = "nanolab-e02-sampler"


def _clear_leftovers(stack: Vm) -> None:
    leftovers = leftover_containers(stack)
    if leftovers:
        _ = stack.run(("docker", "rm", "-f", *leftovers))
    remaining = leftover_containers(stack)
    if remaining:
        raise ValueError(f"containers from an earlier cell remain: {remaining}")


def _apply_budget(stack: Vm, cell: Experiment02Cell, run_dir: Path) -> None:
    _ = stack.run(apply_budget_argv(cell.budget))
    _ = stack.run(restart_engine_argv())
    observed = parse_budget(stack.run(read_budget_argv()))
    (run_dir / "budget.json").write_text(
        json.dumps(
            {
                "applied": cell.budget.model_dump(mode="json", by_alias=True),
                "observed": observed.model_dump(mode="json", by_alias=True),
            },
            indent=2,
        )
        + "\n"
    )
    if observed != cell.budget:
        raise ValueError(
            f"the measured slice enforces {observed.label}, not {cell.budget.label}"
        )


def _start_sampler(stack: Vm, remote_root: PurePosixPath) -> PurePosixPath:
    remote_script = remote_root / SAMPLER_SCRIPT.name
    remote_samples = remote_root / "cgroup-samples.jsonl"
    _ = stack.run(("mkdir", "-p", str(remote_root)))
    stack.put(SAMPLER_SCRIPT, remote_script)
    _ = stack.probe(("sudo", "systemctl", "stop", SAMPLER_UNIT))
    _ = stack.run(("rm", "-f", str(remote_samples)))
    _ = stack.run(
        (
            "sudo", "systemd-run", "--quiet", "--collect", "--unit", SAMPLER_UNIT,
            "--slice", INFRA_SLICE, "python3", "-I", str(remote_script),
            "--root", str(SUT_CGROUP), "--interval", f"{SAMPLE_INTERVAL_SECONDS:g}",
            "--out", str(remote_samples),
        )
    )  # fmt: skip
    return remote_samples


def _diagnose(stack: Vm, diagnostics: Path, reason: str) -> None:
    diagnostics.mkdir(parents=True, exist_ok=True)
    (diagnostics / "error.txt").write_text(reason + "\n")
    _code, listing, _err = stack.probe(("docker", "ps", "-a"))
    (diagnostics / "containers.txt").write_text(listing)
    for name in leftover_containers(stack):
        _code, logs, errors = stack.probe(("docker", "logs", "--tail", "200", name))
        (diagnostics / f"logs-{name}.txt").write_text(logs + errors)


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
    it, a generator that left no summary. The caller decides on retries.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = run_dir / DIAGNOSTICS
    _clear_leftovers(stack)
    _apply_budget(stack, cell, run_dir)
    remote_samples = _start_sampler(stack, remote_root)
    outcome: Outcome = "measured"
    try:
        timings = platform.start(
            stack, protocol=protocol, payload=payload, evidence=run_dir
        )
        if timings is None:
            _diagnose(stack, diagnostics, "no correct response before the timeout")
            outcome = "startup_failed"
        else:
            outside = processes_outside(stack.run(process_listing_argv()))
            if outside:
                diagnostics.mkdir(parents=True, exist_ok=True)
                (diagnostics / "processes-outside.txt").write_text(
                    "\n".join(outside) + "\n"
                )
                raise ValueError(
                    f"deployment processes outside the measured slice: {outside}"
                )
            platform.record(stack, run_dir)
            mark_load_started(run_dir)
            environment = generator_environment(
                protocol,
                payload=payload,
                body=platform.body(payload),
                target=platform.target(host),
            )
            code = run_generator(
                loadgen,
                script=script,
                env=environment,
                remote_dir=remote_root,
                local_dir=run_dir,
            )
            if code and not (run_dir / "k6-summary.json").is_file():
                raise RuntimeError(f"k6 exited {code} without a summary")
    finally:
        _ = stack.probe(("sudo", "systemctl", "stop", SAMPLER_UNIT))
        _ = stack.get(remote_samples, run_dir / SAMPLES)
        platform.teardown(stack)
    kills = oom_kills(read_cgroup_samples(run_dir / SAMPLES).samples)
    if kills:
        diagnostics.mkdir(parents=True, exist_ok=True)
        (diagnostics / "oom.txt").write_text(
            f"{kills} OOM kill(s) in the measured slice\n"
        )
        return "oom"
    return outcome
