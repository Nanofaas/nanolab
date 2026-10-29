#!/usr/bin/env python3
"""Run one unqualified ORACLE, EWMA, Holt or ARI observation on owned NanoLab VMs."""

# Frozen-package selection must precede all third-party/application imports.
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


def parse_args(argv=None):
    """Parse with the standard library before selecting any frozen package."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    for key in ["tuning", "observations", "distribution", "source", "output"]:
        prep.add_argument("--" + key, type=Path, required=True)
    prep.add_argument("--arrival-profile", type=Path)
    prep.add_argument(
        "--decision-period-seconds",
        type=int,
        choices=(60,),
        help="subdivide uniform step decisions, preserving load duration and rates",
    )
    prep.add_argument("--parent-freeze", type=Path)
    prep.add_argument(
        "--forecast",
        choices=("oracle", "ewma", "holt", "ari"),
        default="oracle",
        help="ORACLE uploads the trace; the others forecast each node's arrivals",
    )
    prep.add_argument(
        "--forecast-window-seconds",
        type=int,
        help="not ORACLE; default 60 for EWMA, 10 for Holt and ARI",
    )
    prep.add_argument("--forecast-alpha", type=float, help="EWMA only; default 0.5")
    prep.add_argument("--holt-level", type=float, help="Holt only; default 0.4")
    prep.add_argument("--holt-trend", type=float, help="Holt only; default 0.4")
    prep.add_argument("--holt-damping", type=float, help="Holt only; default 0.95")
    prep.add_argument("--ari-order", type=int, help="ARI only; default 6")
    launch = commands.add_parser("run")
    launch.add_argument("freeze", type=Path)
    launch.add_argument("output", type=Path)
    launch.add_argument("--archive", type=Path)
    launch.add_argument(
        "--display", choices=("console", "tui", "json", "none"), default="console"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    _bootstrap_args = parse_args()
    if _bootstrap_args.command == "run":
        sys.path.insert(0, str(_bootstrap_args.freeze / "package"))

import yaml
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.one_shot import OperationalLimits
from nanolab.one_shot.supervision import (
    CampaignSupervision,
)
from nanolab.one_shot.views import select_presentation, show_launch_failure
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
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.experiment import (
    physical_attempts,
)
from nanolab.tasks.one_shot.observation import (
    event,
    observation_forecast,
    observation_purpose,
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
    period, lead = protocol["periodSeconds"], protocol["leadSeconds"]
    preparation = protocol["protocol"]["preparationSeconds"]
    budget = protocol["protocol"]["auctionSeconds"] + preparation
    # The checks NanoFaaS makes when the nodes are configured, before any VM.
    if (
        budget > min(lead, period * protocol["maxOperationalFraction"])
        or not preparation < lead < period
    ):
        raise ValueError(
            "native operational budget must fit the period fraction and lead time, "
            "and the lead must be shorter than the period"
        )
    if "forecast" in protocol:
        observation_forecast(protocol)


# Option, frozen key and default of each forecaster's own parameters.
FORECAST_OPTIONS = {
    "ewma": {"forecast_alpha": ("alpha", 0.5)},
    "holt": {
        "holt_level": ("level", 0.4),
        "holt_trend": ("trend", 0.4),
        "holt_damping": ("damping", 0.95),
    },
    "ari": {"ari_order": ("order", 6)},
}
FORECAST_WINDOW_SECONDS = {"ewma": 60, "holt": 10, "ari": 10}
# NanoFaaS's default number of complete windows kept for Holt and ARI.
FORECAST_HISTORY_WINDOWS = 360


def forecast_protocol(args):
    """Freeze the forecaster's settings; ORACLE keeps its historical protocol."""
    provider = getattr(args, "forecast", "oracle")
    given = {
        name
        for options in FORECAST_OPTIONS.values()
        for name in options
        if getattr(args, name, None) is not None
    }
    window = getattr(args, "forecast_window_seconds", None)
    if given - set(FORECAST_OPTIONS.get(provider, {})) or (
        provider == "oracle" and window is not None
    ):
        raise ValueError("forecast options must match the --forecast provider")
    if provider == "oracle":
        return {}
    window = FORECAST_WINDOW_SECONDS[provider] if window is None else window
    values = {
        key: default if getattr(args, name, None) is None else getattr(args, name)
        for name, (key, default) in FORECAST_OPTIONS[provider].items()
    }
    forecast = {
        "provider": provider,
        "windowSeconds": window,
        # NanoFaaS dates a forecast at its current window's start.
        "maxAgeSeconds": 2 * window,
    }
    if provider == "ewma":
        forecast["alpha"] = values["alpha"]
    else:
        forecast["historyWindows"] = FORECAST_HISTORY_WINDOWS
        forecast |= {"holt": values} if provider == "holt" else values
    protocol = {"forecast": forecast}
    observation_forecast(protocol)
    return protocol


def bind_scenario(source, distribution, *, trace=None):
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
    if trace is not None:
        settings["timing"]["matrix"] = [
            {
                "id": trace.phase_ids[i] if trace.phase_ids else f"epoch-{i}",
                "rates": window,
            }
            for i, window in enumerate(trace.windows)
        ]
        settings["timing"]["periodCandidatesSeconds"] = [trace.period_seconds]
        settings["timing"]["maxTraceResolutionSeconds"] = trace.period_seconds
        # One-minute decisions: NanoFaaS wants the lead shorter than the period
        # and auction plus preparation within both. On 14 October negotiation
        # took at most 1.4 s and activation 4.8 s.
        settings["timing"]["protocol"].update(auctionSeconds=10, preparationSeconds=10)
        settings["operationalLimits"]["leadSeconds"] = 30
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


def verify_parent_archive(root: Path, expected_fingerprint: str) -> None:
    """Check archived provenance without requiring a retired source/environment."""
    if sha(root / "freeze.json") != expected_fingerprint:
        raise ValueError("parent archive fingerprint mismatch")
    frozen = json.loads((root / "freeze.json").read_text())
    for name, expected in frozen["files"].items():
        path = (root / name).resolve()
        path.relative_to(root.resolve())
        if sha(path) != expected:
            raise ValueError("parent archive bytes changed: " + name)


def dependency_files():
    """Fingerprint the actual engine and presentation code used by future freezes."""
    return {
        str(path): sha(path)
        for package_name in (
            "sonata_engine",
            "sonata_tasks",
            "multipass_vm_sdk",
            "subprocess_toolkit",
            "rich",
            "tui_toolkit",
            "typer",
        )
        for path in Path(str(files(package_name))).rglob("*.py")
    }


def step_decision_grid(trace, selected, period):
    """Change decision cadence without changing the piecewise-constant load."""
    factor = trace.period_seconds / period if period > 0 else 0
    if (
        factor < 1
        or not factor.is_integer()
        or [r["rates"] for r in selected] != trace.windows
    ):
        raise ValueError("step decision period or reference scope mismatch")
    factor = int(factor)
    data = trace.model_dump()
    data["period_seconds"] = period
    for name in ("windows", "requested_windows"):
        if data.get(name) is not None:
            data[name] = [
                copy.deepcopy(row) for row in data[name] for _ in range(factor)
            ]
    data["phase_ids"] = [
        f"step-{i}-decision-{j}"
        for i in range(len(trace.windows))
        for j in range(factor)
    ]
    minute = TraceArtifact.model_validate(data)
    decisions = [copy.deepcopy(row) for row in selected for _ in range(factor)]
    for row, identity in zip(decisions, minute.phase_ids, strict=True):
        row["id"] = identity
    return minute, decisions


def prepare(args):
    """Capture the package, script and inputs without starting any VM."""
    forecast = forecast_protocol(args)
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
    derived = arrival_document is not None and arrival_document["schemaVersion"] == 2
    parent = getattr(args, "parent_freeze", None)
    if derived:
        if parent is None:
            raise ValueError("derived replay requires --parent-freeze")
        verify_parent_archive(parent, arrival_document["parent"]["freezeSha256"])
        provenance = json.loads((args.tuning / "provenance.json").read_text())
        for name, item in provenance["files"].items():
            if sha(args.tuning / name) != item["sha256"]:
                raise ValueError("derived preparation bytes changed: " + name)
    args.output.mkdir(parents=True, exist_ok=False)
    if arrival_input is not None:
        shutil.copyfile(arrival_input, args.output / "arrival-profile.json")
    for name in [
        "trace.json",
        "selected.json",
        "input-context.json",
        *([] if derived else ["offload-coverage.json"]),
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
    step_period = getattr(args, "decision_period_seconds", None)
    if step_period is not None:
        if arrival_input is not None:
            raise ValueError("decision-period override is only for uniform step load")
        original = TraceArtifact.model_validate_json(
            (args.output / "trace.json").read_bytes()
        )
        reference = json.loads((args.output / "selected.json").read_bytes())
        minute, decisions = step_decision_grid(original, reference, step_period)
        for name in ("trace.json", "selected.json"):
            (args.output / name).rename(args.output / ("source-" + name))
        save(args.output / "trace.json", minute.model_dump())
        save(args.output / "selected.json", decisions)
        save(
            args.output / "decision-grid.json",
            {
                "sourcePeriodSeconds": original.period_seconds,
                "periodSeconds": step_period,
                "sourceEpochs": len(original.windows),
                "decisionEpochs": len(minute.windows),
                "loadDurationSeconds": original.period_seconds * len(original.windows),
                "coverageScope": "offload-coverage.json describes the original load steps",
            },
        )
    scenario = bind_scenario(
        yaml.safe_load(
            (
                (parent / "scenario.yaml")
                if derived
                else (args.tuning / "load-scenario.yaml")
            ).read_text()
        ),
        args.output / "distribution.json",
        **(
            {
                "trace": TraceArtifact.model_validate_json(
                    (args.output / "trace.json").read_bytes()
                )
            }
            if derived or step_period is not None
            else {}
        ),
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
        (
            not derived
            and step_period is None
            and (len(trace.windows) != 9 or trace.period_seconds != 360)
        )
        or (
            step_period is not None
            and (len(trace.windows) != 54 or trace.period_seconds != 60)
        )
        or (derived and (len(trace.windows) != 54 or trace.period_seconds != 60))
        or len(trace.nodes) != 6
        or len(trace.functions) != 5
    ):
        raise ValueError("approved trace scope required")
    selected = json.loads((args.output / "selected.json").read_text())
    if [r["rates"] for r in selected] != trace.windows:
        raise ValueError("selected loads differ from trace")
    if derived:
        save(
            args.output / "offload-coverage.json",
            {
                "observationOnly": True,
                "epochs": len(selected),
                "intervalsWithPeerOffload": sum(
                    row["summary"]["neighborRps"] > 0 for row in selected
                ),
                "note": (
                    "Predicted coverage is recorded; "
                    "no load selection or exclusion gate"
                ),
            },
        )
        for name in (
            "means.json",
            "means.csv",
            "oracle-verification.json",
            "reference-environment.json",
            "provenance.json",
        ):
            shutil.copyfile(
                args.tuning / name,
                args.output
                / (
                    "preparation-provenance.json" if name == "provenance.json" else name
                ),
            )
    coverage = json.loads((args.output / "offload-coverage.json").read_text())
    if not derived and (
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
        "periodSeconds": trace.period_seconds,
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
        **forecast,
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
            "rich",
            "tui-toolkit",
            "typer",
        ]
    }
    frozen = {
        "purpose": observation_purpose(observation_forecast(protocol)["provider"]),
        "qualified": False,
        "source": str(args.source.resolve()),
        "sourceRevision": revision,
        "sourceInventoryComplete": True,
        "sourceFiles": {
            name: sha(args.source / name) for name in source_file_names(args.source)
        },
        "dependencyFiles": dependency_files(),
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
    entrypoint_sha = frozen.get("files", {}).get("source-used.py")
    if entrypoint_sha is not None and sha(Path(__file__)) != entrypoint_sha:
        raise ValueError("observation entrypoint drift")
    protocol = json.loads((args.freeze / "operator-protocol.json").read_text())
    validate_operator_protocol(protocol)
    arrival_document = read_frozen_arrival_profile(args.freeze, frozen)
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

    supervision = CampaignSupervision(
        args.output,
        archive=args.archive,
        presentation_factory=lambda fail: select_presentation(
            getattr(args, "display", "console"), on_failure=fail
        ),
    )

    def execute():
        primary = None
        try:
            if arrival_document is not None:
                write_immutable_artifact(
                    args.output / "arrival-profile.json",
                    (args.freeze / "arrival-profile.json").read_bytes(),
                )
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
        except BaseException as error:  # noqa: BLE001 - defer until diagnostics finish
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
            except Exception as reporting:  # noqa: BLE001 - retain the first cause
                error.add_note("failure receipt: " + str(reporting))
            try:
                event(args.output, "workflow-failed", error=repr(error))
            except Exception as reporting:  # noqa: BLE001 - retain the first cause
                error.add_note("failure event: " + str(reporting))
        finally:
            try:
                inventory("vms-after.json")
            except Exception as error:  # noqa: BLE001 - complete independent diagnostics
                if primary is None:
                    primary = error
                else:
                    primary.add_note("post-run inventory: " + str(error))
                try:
                    save(args.output / "inventory-failure.json", {"error": repr(error)})
                except Exception as reporting:  # noqa: BLE001 - retain the first cause
                    primary.add_note("inventory receipt: " + str(reporting))
            try:
                retain_offline_report(args.output)
            except Exception as error:  # noqa: BLE001 - complete independent diagnostics
                if primary is None:
                    primary = error
                else:
                    primary.add_note("offline report: " + str(error))
        if primary is not None:
            raise primary

    args.supervised = True
    supervision.run(execute)


def main(args=None):
    """Format pre-supervisor errors once, preserving selected output and exits."""
    args = args or parse_args()
    if args.command == "prepare":
        prepare(args)
    else:
        args.supervised = False
        try:
            run_observation(args)
        except BaseException as error:
            if not args.supervised:
                show_launch_failure(args.display, error)
            raise SystemExit(getattr(error, "exit_code", 1)) from error


if __name__ == "__main__":
    main(_bootstrap_args)
