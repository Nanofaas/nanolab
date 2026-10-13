#!/usr/bin/env python3
"""Run one unqualified ORACLE observation through owned NanoLab infrastructure."""

import argparse
import copy
import json
import math
import shutil
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from importlib.resources import files
from pathlib import Path

if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "run":
    sys.path.insert(0, str(Path(sys.argv[2]) / "package"))

import yaml
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.one_shot import OperationalLimits
from nanolab.one_shot.supervision import (
    CampaignSupervision,
)
from nanolab.one_shot.vm_resources import (
    telemetry_policy,
)
from nanolab.plans.one_shot_observation import build_one_shot_observation_plan
from nanolab.tasks.one_shot.arrivals import (
    read_frozen_arrival_profile,
    validate_arrival_profile,
)
from nanolab.tasks.one_shot.artifacts import (
    package_fingerprint,
)
from nanolab.tasks.one_shot.experiment import (
    physical_attempts,
)
from nanolab.tasks.one_shot.observation import (
    event,
    oracle_lifetime_seconds,
    retain_offline_report,
    save,
    sha,
    source_file_names,
    verify_files,
)
from nanolab.tasks.one_shot.trace import (
    TraceArtifact,
)
from nanolab.tasks.recipes.workflow import read_distribution

NATIVE_MAX_TIMING_FRACTION = 1.0
SCHEDULE_SETUP_SECONDS = 90


def validate_operator_protocol(protocol):
    """Check the pinned native timing contract before entering the VM lifecycle."""
    for field in ("maxOperationalFraction", "maxAuctionFraction"):
        value = protocol.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= NATIVE_MAX_TIMING_FRACTION
        ):
            raise ValueError(f"{field} must be finite and within (0, 1.00]")
    if protocol["maxAuctionFraction"] != protocol["maxOperationalFraction"]:
        raise ValueError(
            "maxAuctionFraction must match the shared configuration epsilon"
        )
    budget = (
        protocol["protocol"]["auctionSeconds"]
        + protocol["protocol"]["preparationSeconds"]
    )
    if (
        budget > protocol["periodSeconds"] * protocol["maxOperationalFraction"]
        or budget > protocol["leadSeconds"]
    ):
        raise ValueError(
            "native operational budget must fit the period fraction and lead time"
        )


def bind_scenario(source, distribution):
    """Keep resource quotas and rates, bind the existing runtime."""
    result = copy.deepcopy(source)
    settings = result["oneShot"]
    settings["operationalLimits"] = OperationalLimits().model_dump(by_alias=True)
    settings["timing"].setdefault("protocol", {}).update(
        auctionSeconds=60,
        peerSeconds=5,
        solverSeconds=5,
        preparationSeconds=10,
        maxSolverStates=10000000,
    )
    settings["runtimeDistribution"] = {
        "path": str(distribution.resolve()),
        "sha256": sha(distribution),
    }
    settings["experiment"] = {
        "windows": [{**row, "epochs": 1} for row in settings["timing"]["matrix"]],
        "repetitions": 1,
        "warmupSeconds": 720,
        "warmupRate": 1,
        "generatorVus": 512,
        "maxArrivalLatenessSeconds": 3600,
    }
    return result


def historical_statistics(observed):
    """Calculate the missing deviation from the exact original deduplicated samples."""
    path = Path(observed["sourceDirectory"]) / "physical-window/physical.jsonl"
    if sha(path) != observed["sourcePhysicalSha256"]:
        raise ValueError("historical physical samples changed")
    start = datetime.fromisoformat(observed["measurementStartUtc"]).timestamp()
    values = {}
    for row in physical_attempts(path):
        if (
            start <= row.get("emittedAt", -1) < start + observed["measurementSeconds"]
            and row.get("state") == "RELEASED"
            and row.get("handlerStarted")
            and row.get("statusCode") == 200
            and str(row.get("responseStatus")).lower() == "success"
        ):
            values.setdefault((row["destination"], row["function"]), []).append(
                row["occupancySeconds"]
            )
    result = copy.deepcopy(observed["service"])
    for node, functions in result.items():
        for name, cell in functions.items():
            samples = values[node, name]
            if (
                len(samples) != cell["sampleCount"]
                or statistics.mean(samples) != cell["meanSeconds"]
            ):
                raise ValueError("historical statistics mismatch")
            cell["stddevSeconds"] = statistics.stdev(samples)
    return result


def prepare(args):
    """Capture the package, script and inputs without starting any VM."""
    arrival_input = getattr(args, "arrival_profile", None)
    arrival_document = None
    if arrival_input is not None:
        arrival_document = json.loads(arrival_input.read_text())
        input_trace = args.tuning / "trace.json"
        validate_arrival_profile(
            arrival_document,
            TraceArtifact.model_validate_json(input_trace.read_bytes()),
            trace_sha256=sha(input_trace),
            warmup_seconds=720,
        )
    args.output.mkdir(parents=True, exist_ok=False)
    if arrival_input is not None:
        shutil.copyfile(arrival_input, args.output / "arrival-profile.json")
    for name in [
        "trace.json",
        "selected.json",
        "input-context.json",
        "offload-coverage.json",
    ]:
        shutil.copyfile(args.tuning / name, args.output / name)
    shutil.copyfile(args.observations, args.output / "observations.json")
    shutil.copyfile(args.distribution, args.output / "distribution.json")
    shutil.copyfile(Path(__file__), args.output / "source-used.py")
    package = Path(str(files("nanolab")))
    shutil.copytree(
        package,
        args.output / "package/nanolab",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copyfile(
        package / "assets/presets/recipes/one-shot-multifunction-jvm.yaml",
        args.output / "recipe.yaml",
    )
    scenario = bind_scenario(
        yaml.safe_load((args.tuning / "load-scenario.yaml").read_text()),
        args.output / "distribution.json",
    )
    scenario["recipeProfile"] = str((args.output / "recipe.yaml").resolve())
    with (args.output / "scenario.yaml").open("x") as stream:
        yaml.safe_dump(scenario, stream, sort_keys=False)
    settings = ScenarioConfig.model_validate(scenario).one_shot
    assert (
        settings is not None
        and settings.timing is not None
        and settings.experiment is not None
    )
    trace = TraceArtifact.model_validate_json((args.output / "trace.json").read_text())
    if (
        len(trace.windows) != 9
        or trace.period_seconds != 360
        or len(trace.nodes) != 6
        or len(trace.functions) != 5
    ):
        raise ValueError("approved trace scope required")
    selected = json.loads((args.output / "selected.json").read_text())
    if [r["rates"] for r in selected] != trace.windows:
        raise ValueError("selected loads differ from trace")
    coverage = json.loads((args.output / "offload-coverage.json").read_text())
    if (
        coverage["intervalsWithPeerOffload"] != 9
        or coverage["intervalsWithMultipleNeighbors"] < 2
    ):
        raise ValueError("offload coverage missing")
    built = read_distribution(
        args.output / "distribution.json",
        recipe=args.output / "recipe.yaml",
        tag=json.loads(args.distribution.read_text())["tag"],
        published=False,
    )
    changed = subprocess.check_output(
        ["git", "-C", str(args.source), "diff", "--name-only", "HEAD"], text=True
    ).splitlines()
    if any(not n.endswith(".md") for n in changed):
        raise ValueError("unexported runtime changes exist")
    revision = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
    ).strip()
    if built.source != {"revision": revision, "dirty": False}:
        raise ValueError("runtime revision mismatch")
    observed = json.loads(args.observations.read_text())
    save(args.output / "statistics.json", historical_statistics(observed))
    protocol = {
        "qualified": False,
        "purpose": "operator-declared-protocol-not-timing-qualification",
        "periodSeconds": 360,
        "leadSeconds": settings.operational_limits.lead_seconds,
        "operationalLimits": settings.operational_limits.model_dump(by_alias=True),
        "maxOperationalFraction": NATIVE_MAX_TIMING_FRACTION,
        "maxAuctionFraction": NATIVE_MAX_TIMING_FRACTION,
        "protocol": settings.timing.protocol.model_dump(by_alias=True),
        "performanceAcceptanceGates": False,
        "maxArrivalLatenessSeconds": 3600,
        "requireNoSwap": False,
        "observerPrecheckSeconds": 60,
        "oracleMaxAgeSeconds": oracle_lifetime_seconds(
            trace,
            warmup_seconds=settings.experiment.warmup_seconds,
            limits=settings.operational_limits,
        ),
        "oracleLifetimeBasis": (
            "warmup + schedule setup + all epochs + existing generator drain"
        ),
    }
    validate_operator_protocol(protocol)
    save(args.output / "operator-protocol.json", protocol)
    deps = {
        n: metadata.version(n)
        for n in [
            "nanolab",
            "sonata-engine",
            "sonata-tasks",
            "multipass-vm-sdk",
            "subprocess-toolkit",
            "httpx",
            "pydantic",
            "plotly",
        ]
    }
    frozen = {
        "purpose": "first-functional-oracle-observation",
        "qualified": False,
        "source": str(args.source.resolve()),
        "sourceRevision": revision,
        "sourceInventoryComplete": True,
        "sourceFiles": {
            name: sha(args.source / name) for name in source_file_names(args.source)
        },
        "dependencyFiles": {
            str(path): sha(path)
            for package_name in (
                "sonata_engine",
                "sonata_tasks",
                "multipass_vm_sdk",
                "subprocess_toolkit",
            )
            for path in Path(str(files(package_name))).rglob("*.py")
        },
        "python": sys.version,
        "dependencies": deps,
        "hostBootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "nanolabPackageSha256": package_fingerprint(),
        "telemetry": telemetry_policy(),
        "serviceScope": {
            k: observed[k]
            for k in [
                "type",
                "independentlyQualified",
                "scope",
                "limitation",
                "sourcePhysicalSha256",
            ]
        },
        "profileAdaptation": (
            "native synthetic=true marks unqualified extrapolation; "
            "real historical means/statistics are retained"
        ),
        "files": {
            str(p.relative_to(args.output)): sha(p)
            for p in args.output.rglob("*")
            if p.is_file()
        },
    }
    if arrival_document is not None:
        frozen["arrivalProfile"] = {"sha256": sha(args.output / "arrival-profile.json")}
        read_frozen_arrival_profile(args.output, frozen)
    save(args.output / "freeze.json", frozen)
    print(
        json.dumps(
            {
                "prepared": str(args.output),
                "qualified": False,
                "fingerprint": sha(args.output / "freeze.json"),
            },
            indent=2,
        ),
        flush=True,
    )


def run_observation(args):
    """Rely on Sonata compensation; preserve raw and never retry a failed run."""
    frozen = json.loads((args.freeze / "freeze.json").read_text())
    verify_files(args.freeze, frozen)
    protocol = json.loads((args.freeze / "operator-protocol.json").read_text())
    validate_operator_protocol(protocol)
    read_frozen_arrival_profile(args.freeze, frozen)
    if (
        Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        != frozen["hostBootId"]
        or package_fingerprint() != frozen["nanolabPackageSha256"]
    ):
        raise ValueError("host boot or package drift")
    if sys.version != frozen["python"] or any(
        metadata.version(n) != v for n, v in frozen["dependencies"].items()
    ):
        raise ValueError("operator dependency drift")
    args.output.mkdir(parents=True, exist_ok=False)

    def inventory(name):
        result = subprocess.run(
            ["multipass", "list", "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(result.stdout)
        save(args.output / name, value)
        return value

    supervision = CampaignSupervision(args.output, archive=args.archive)

    def execute():
        primary = None
        try:
            inventory("vms-before.json")
            config = ScenarioConfig.model_validate(
                yaml.safe_load((args.freeze / "scenario.yaml").read_text())
            )
            workflow = build_one_shot_observation_plan(
                config,
                EnvironmentConfig.model_validate(
                    {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
                ),
                freeze_dir=args.freeze,
                run_dir=args.output,
                supervision=supervision,
            )
            event(args.output, "provisioning-started", qualified=False)
            workflow.run()
            event(args.output, "workflow-complete", qualified=False)
        except BaseException as error:
            primary = error
            try:
                save(
                    args.output / "failure.json",
                    {
                        "error": repr(error),
                        "notes": getattr(error, "__notes__", []),
                        "atUtc": datetime.now(UTC).isoformat(),
                    },
                )
            except Exception as reporting:
                error.add_note("failure receipt: " + str(reporting))
            try:
                event(args.output, "workflow-failed", error=repr(error))
            except Exception as reporting:
                error.add_note("failure event: " + str(reporting))
            raise
        finally:
            try:
                inventory("vms-after.json")
            except Exception as error:
                try:
                    save(args.output / "inventory-failure.json", {"error": repr(error)})
                except Exception as reporting:
                    (primary or error).add_note("inventory receipt: " + str(reporting))
                if primary is None:
                    raise
                primary.add_note("post-run inventory: " + str(error))
            try:
                retain_offline_report(args.output)
            except Exception as error:
                if primary is None:
                    raise
                primary.add_note("offline report: " + str(error))

    supervision.run(execute)


def main():
    """Keep preparation and execution as separate saved commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    for key in ["tuning", "observations", "distribution", "source", "output"]:
        prep.add_argument("--" + key, type=Path, required=True)
    prep.add_argument("--arrival-profile", type=Path)
    launch = commands.add_parser("run")
    launch.add_argument("freeze", type=Path)
    launch.add_argument("output", type=Path)
    launch.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    else:
        try:
            run_observation(args)
        except BaseException as error:
            print(
                json.dumps(
                    {"cause": str(error), "exitCode": getattr(error, "exit_code", 1)}
                ),
                flush=True,
            )
            raise SystemExit(getattr(error, "exit_code", 1)) from error


if __name__ == "__main__":
    main()
