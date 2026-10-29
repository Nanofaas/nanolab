#!/usr/bin/env python3
"""Build once, freeze and run step ORACLE, continuous ORACLE and continuous ARI."""

import argparse
import hashlib
import json
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

RUNS = ("step-oracle", "continuous-oracle", "continuous-ari")
INPUTS = (
    "runtimeSource",
    "stepTuning",
    "continuousTuning",
    "continuousParentFreeze",
    "observations",
    "physicalSamples",
)


def read_json(path):
    """Read an input without rewriting historical evidence."""
    return json.loads(path.read_text())


def digest(path):
    """Stream potentially large physical sample files."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inputs(path):
    """Resolve explicit paths and reject missing or mismatched historical inputs."""
    raw = read_json(path)
    if set(raw) != set(INPUTS):
        raise ValueError("config must contain exactly: " + ", ".join(INPUTS))
    result = {
        key: (path.parent / Path(value).expanduser()).resolve()
        for key, value in raw.items()
    }
    for item in result.values():
        if not item.exists():
            raise ValueError("missing input: " + str(item))
    for name in ("stepTuning", "continuousTuning"):
        for file in ("trace.json", "selected.json", "input-context.json"):
            if not (result[name] / file).is_file():
                raise ValueError("missing tuning input: " + str(result[name] / file))
    step = read_json(result["stepTuning"] / "trace.json")
    continuous = read_json(result["continuousTuning"] / "trace.json")
    if (step["period_seconds"], len(step["windows"])) not in ((360, 9), (60, 54)):
        raise ValueError("expected the existing 54-minute step load")
    if (continuous["period_seconds"], len(continuous["windows"])) != (60, 54):
        raise ValueError("expected the existing 54-minute continuous load")
    observations = read_json(result["observations"])
    if digest(result["physicalSamples"]) != observations["sourcePhysicalSha256"]:
        raise ValueError("historical physical sample hash mismatch")
    if result["physicalSamples"].parent.name != "physical-window":
        raise ValueError(
            "physical samples must be in their original physical-window directory"
        )
    return result


def run_freezes(jobs, output, python):
    """Wait for each owned run and archive before starting the next; stop on error."""
    status_path = output / "sequence-status.json"
    completed = []
    for name, freeze in jobs:
        status = {
            "state": "running",
            "current": name,
            "completed": completed.copy(),
            "updatedAtUtc": datetime.now(UTC).isoformat(),
        }
        status_path.write_text(json.dumps(status, indent=2) + "\n")
        print(f"Starting {name}", flush=True)
        try:
            subprocess.run(
                [
                    python,
                    str(freeze / "source-used.py"),
                    "run",
                    str(freeze),
                    str(output / (name + "-run")),
                    "--archive",
                    str(output / (name + "-archive")),
                ],
                check=True,
            )
        except BaseException:
            status["state"] = "failed"
            status_path.write_text(json.dumps(status, indent=2) + "\n")
            raise
        completed.append(name)
    status_path.write_text(
        json.dumps(
            {
                "state": "complete",
                "completed": completed,
                "updatedAtUtc": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
        + "\n"
    )


def main(argv=None):
    """Only --check is read-only; a normal invocation prepares and runs all three."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="validate inputs only; no build, VM or run"
    )
    args = parser.parse_args(argv)
    config = inputs(args.config.resolve())
    output = args.output.resolve()
    if output.exists():
        raise ValueError("output directory already exists: " + str(output))
    lab = Path(__file__).resolve().parents[1]
    observer = runpy.run_path(str(lab / "scripts/one-shot-observe.py"))
    arrival = config["continuousTuning"] / "arrival-profile.json"
    profile = read_json(arrival)
    observer["verify_parent_archive"](
        config["continuousParentFreeze"], profile["parent"]["freezeSha256"]
    )
    source = config["runtimeSource"]
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD"], check=True)
    # Validate the minute grid and archive checks before spending time building images.
    step = observer["TraceArtifact"].model_validate_json(
        (config["stepTuning"] / "trace.json").read_bytes()
    )
    observer["step_decision_grid"](
        step, read_json(config["stepTuning"] / "selected.json"), 60
    )
    observer["validate_arrival_profile"](
        profile,
        observer["TraceArtifact"].model_validate_json(
            (config["continuousTuning"] / "trace.json").read_bytes()
        ),
        trace_sha256=digest(config["continuousTuning"] / "trace.json"),
        warmup_seconds=720,
    )
    for name, file in read_json(config["continuousTuning"] / "provenance.json")[
        "files"
    ].items():
        if digest(config["continuousTuning"] / name) != file["sha256"]:
            raise ValueError("continuous preparation hash mismatch: " + name)
    summary = {
        "runtimeRevision": revision,
        "order": list(RUNS),
        "decisionPeriodSeconds": 60,
        "loadSecondsPerRun": 3240,
        "warmupSecondsPerRun": 720,
        "inputs": {key: str(value) for key, value in config.items()},
    }
    print(json.dumps(summary, indent=2), flush=True)
    if args.check:
        return
    output.mkdir(parents=True, exist_ok=False)
    (output / "sequence.json").write_text(json.dumps(summary, indent=2) + "\n")
    observations = read_json(config["observations"])
    observations["sourceDirectory"] = str(config["physicalSamples"].parent.parent)
    observed_path = output / "observations.json"
    observed_path.write_text(json.dumps(observations, indent=2) + "\n")
    recipe = (
        lab
        / "packages/nanolab/src/nanolab/assets/presets/recipes/one-shot-multifunction-jvm.yaml"
    )
    distribution = output / "distribution"
    subprocess.run(
        [
            str(source / "gradlew"),
            "--max-workers=2",
            "assembleRecipe",
            "-Precipe=" + str(recipe),
            "-PrecipeOutput=" + str(distribution),
            "-PrecipeTag=sequence-"
            + revision[:8]
            + "-"
            + datetime.now(UTC).strftime("%Y%m%d%H%M%S"),
        ],
        cwd=source,
        check=True,
    )
    jobs = []
    for name in RUNS:
        continuous = name != "step-oracle"
        freeze = output / (name + "-freeze")
        observer["prepare"](
            SimpleNamespace(
                forecast="ari" if name == "continuous-ari" else "oracle",
                arrival_profile=arrival if continuous else None,
                parent_freeze=config["continuousParentFreeze"] if continuous else None,
                tuning=config["continuousTuning"]
                if continuous
                else config["stepTuning"],
                observations=observed_path,
                distribution=distribution / "distribution.json",
                source=source,
                output=freeze,
                decision_period_seconds=None if continuous else 60,
            )
        )
        jobs.append((name, freeze))
    run_freezes(jobs, output, sys.executable)


if __name__ == "__main__":
    main()
