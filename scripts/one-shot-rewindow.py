#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy==2.2.6", "scipy==1.15.3"]
# ///
"""Derive a continuous replay and optional Python oracle, without starting a VM."""

import argparse
import csv
import hashlib
import json
import math
import os
import runpy
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

sys.dont_write_bytecode = True


def digest(path):
    """Hash saved bytes without changing an archived input."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    """Never overwrite an earlier preparation artifact."""
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def verify_parent(root):
    """Check the full frozen inventory and the identities used by this derivation."""
    frozen = json.loads((root / "freeze.json").read_text())
    for name, expected in frozen["files"].items():
        path = (root / name).resolve()
        path.relative_to(root.resolve())
        if digest(path) != expected:
            raise ValueError("parent file hash mismatch: " + name)
    profile = json.loads((root / "arrival-profile.json").read_text())
    if frozen["arrivalProfile"]["sha256"] != digest(
        root / "arrival-profile.json"
    ) or profile["traceSha256"] != digest(root / "trace.json"):
        raise ValueError("parent profile identity mismatch")
    return frozen, profile


def reference_environment(python: Path, packages: list[str]) -> dict:
    """Ask the interpreter that actually runs the model for its own environment."""
    code = (
        "import json,sys; from importlib import metadata; "
        "print(json.dumps({'executable':sys.executable,'python':sys.version,"
        "'packages':{n:metadata.version(n) for n in sys.argv[1:]}}))"
    )
    return json.loads(
        subprocess.check_output([str(python), "-B", "-c", code, *packages], text=True)
    )


def prepare(args):
    """Integrate the saved spline in physical seconds and preserve every arrival."""
    import numpy
    import scipy
    from scipy.interpolate import BSpline

    helper = Path(__file__).with_name("rewindow.py")
    if not helper.exists():
        helper = (
            Path(__file__).resolve().parents[1]
            / "packages/nanolab/src/nanolab/tasks/one_shot/rewindow.py"
        )
    functions = runpy.run_path(str(helper))
    parent_hash = digest(args.freeze / "freeze.json")
    _, original = verify_parent(args.freeze)
    trace = json.loads((args.freeze / "trace.json").read_text())
    context = json.loads((args.freeze / "input-context.json").read_text())
    if (
        original["periodSeconds"] != trace["period_seconds"]
        or original["epochs"] != len(trace["windows"])
        or set(original["measurementCurves"])
        != {n + "/" + f for n in trace["nodes"] for f in trace["functions"]}
        or context["context"]["flowQuantum"] != trace["flow_quantum"]
    ):
        raise ValueError("parent curve, trace and model context differ")
    curves = {}
    scale = original["periodSeconds"]

    def integral(definition, start, stop):
        identity = id(definition)
        if identity not in curves:
            curves[identity] = BSpline(
                definition["knotsInIntervals"],
                definition["coefficientsRps"],
                definition["degree"],
                extrapolate=False,
            )
        return float(curves[identity].integrate(start / scale, stop / scale) * scale)

    document, means = functions["rewindow_profile"](
        original, period_seconds=args.period_seconds, integral=integral
    )
    windows, audit = functions["quantize_means"](
        means, quantum=trace["flow_quantum"], period_seconds=args.period_seconds
    )
    for node in trace["nodes"]:
        for function in trace["functions"]:
            key = node + "/" + function
            for epoch in range(len(trace["windows"])):
                start, stop = epoch * scale, (epoch + 1) * scale
                mass = integral(original["measurementCurves"][key], start, stop)
                derived_mass = sum(
                    row[node][function] * args.period_seconds
                    for k, row in enumerate(means)
                    if start <= k * args.period_seconds < stop
                )
                if not math.isclose(mass, derived_mass, rel_tol=1e-10, abs_tol=1e-9):
                    raise ValueError("integral conservation failed: " + key)

    # The comparison below checks the actual offset bytes-as-numbers, not just totals.
    def calendars(profile):
        return {
            (phase, node, function): [
                offset
                for c in profile["cells"]
                if (c["phase"], c["origin"], c["function"]) == (phase, node, function)
                for offset in c["offsetsSeconds"]
            ]
            for phase in ("warmup", "campaign")
            for node in trace["nodes"]
            for function in trace["functions"]
        }

    if calendars(document) != calendars(original):
        raise ValueError("source arrival calendar changed")
    trace.update(period_seconds=args.period_seconds, windows=windows)
    trace.pop("requested_windows", None)
    trace["phase_ids"] = ["minute-" + str(k + 1).zfill(2) for k in range(len(means))]
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output / "trace.json", trace)
    document["traceSha256"] = digest(args.output / "trace.json")
    document["parent"] = {
        "freezeSha256": parent_hash,
        "arrivalProfileSha256": digest(args.freeze / "arrival-profile.json"),
        "periodSeconds": original["periodSeconds"],
        "epochs": original["epochs"],
    }
    document["oracleRepresentation"] = {
        "quantumRps": trace["flow_quantum"],
        "rounding": "nearest; ties upward",
        "meansFile": "means.json",
        "auditFile": "means.csv",
    }
    write(args.output / "arrival-profile.json", document)
    write(args.output / "means.json", means)
    cells = {
        (c["epoch"], c["origin"], c["function"]): len(c["offsetsSeconds"])
        for c in document["cells"]
        if c["phase"] == "campaign"
    }
    for row in audit:
        row["calendarRequests"] = cells[row["epoch"], row["node"], row["function"]]
    with (args.output / "means.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(audit[0]))
        writer.writeheader()
        writer.writerows(audit)
    shutil.copyfile(
        args.freeze / "input-context.json", args.output / "input-context.json"
    )
    tools = args.output / "tools"
    tools.mkdir()
    shutil.copyfile(__file__, tools / Path(__file__).name)
    shutil.copyfile(helper, tools / "rewindow.py")
    runner = Path(__file__).with_name("one-shot-tune-load.py")
    shutil.copyfile(runner, tools / runner.name)
    verification = {
        "runtimeReady": False,
        "scope": "offline-inputs-and-python-oracle-only",
        "periodSeconds": args.period_seconds,
        "epochs": len(means),
        "measurementSeconds": len(means) * args.period_seconds,
        "warmupSeconds": original["warmupSeconds"],
        "measurementRequests": sum(
            len(c["offsetsSeconds"])
            for c in document["cells"]
            if c["phase"] == "campaign"
        ),
        "warmupRequests": sum(
            len(c["offsetsSeconds"])
            for c in document["cells"]
            if c["phase"] == "warmup"
        ),
        "calendarOffsetsIdentical": True,
        "integralsConserved": True,
        "quantumRps": trace["flow_quantum"],
        "rounding": "nearest; ties upward",
        "maxAbsoluteQuantizationErrorRps": max(
            abs(row["quantizationErrorRps"]) for row in audit
        ),
        "pythonOracleCases": 0,
        "parentFreezeSha256": parent_hash,
        "numpyVersion": numpy.__version__,
        "scipyVersion": scipy.__version__,
    }
    write(args.output / "verification.json", verification)
    if args.reference:
        reference = context["reference"]
        environment = reference_environment(
            args.reference_python, list(context["referenceEnvironment"]["packages"])
        )
        write(args.output / "reference-environment.json", environment)
        for name, expected in reference["sources"].items():
            if digest(args.reference / name) != expected:
                raise ValueError("Python reference hash mismatch: " + name)
        cases = args.output / "python"
        cases.mkdir()
        selected = []
        for epoch, rates in enumerate(windows):
            case = {
                "id": trace["phase_ids"][epoch],
                "factor": 1,
                "context": context["context"],
                "rates": rates,
            }
            source, target = (
                cases / f"{epoch:02d}-input.json",
                cases / f"{epoch:02d}-result.json",
            )
            write(source, case)
            with (cases / f"{epoch:02d}.log").open("x") as stream:
                subprocess.run(
                    [
                        str(args.reference_python),
                        "-B",
                        str(tools / runner.name),
                        "case",
                        "--input",
                        str(source),
                        "--reference",
                        str(args.reference),
                        "--output",
                        str(target),
                    ],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                    check=True,
                )
            result = json.loads(target.read_text())
            selected.append(result)
            print(
                json.dumps(
                    {
                        "atUtc": datetime.now(UTC).isoformat(),
                        "kind": "oracle.epoch-completed",
                        "epoch": epoch,
                        "epochs": len(means),
                    }
                ),
                flush=True,
            )
        write(args.output / "selected.json", selected)
        verification["pythonOracleCases"] = len(selected)
        verification["pythonReferenceCommit"] = reference["commit"]
        verification["pythonReferenceSourcesVerified"] = len(reference["sources"])
        verification["oracleStateConvention"] = (
            "independent per-epoch LSP and fixed-round auction; same simulate wrapper as the previous campaign"
        )
        write(args.output / "oracle-verification.json", verification)
    # Freeze provenance last: no writer remains after this inventory is created.
    verify_parent(args.freeze)
    if parent_hash != digest(args.freeze / "freeze.json"):
        raise ValueError("parent freeze changed during preparation")
    write(
        args.output / "provenance.json",
        {
            "sourceFreeze": str(args.freeze.resolve()),
            "sourceReference": str(args.reference.resolve())
            if args.reference
            else None,
            "parentFreezeSha256": parent_hash,
            "python": sys.version,
            "packages": {
                n: metadata.version(n)
                for n in ("numpy", "scipy", "pyomo", "pandas")
                if n in metadata.packages_distributions()
            },
            "files": {
                str(p.relative_to(args.output)): {
                    "sha256": digest(p),
                    "bytes": p.stat().st_size,
                }
                for p in sorted(args.output.rglob("*"))
                if p.is_file()
            },
        },
    )
    print(json.dumps(verification, indent=2), flush=True)


def main():
    """Explicit preparation command; no runtime launch or timeout overrides."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--period-seconds", type=int, default=60)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--reference-python", type=Path)
    args = parser.parse_args()
    if bool(args.reference) != bool(args.reference_python):
        parser.error("--reference and --reference-python must be supplied together")
    prepare(args)


if __name__ == "__main__":
    main()
