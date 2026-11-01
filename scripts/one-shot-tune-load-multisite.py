#!/usr/bin/env python3
"""Tune a multi-site one-shot load with the original pinned Python helpers.

The five-edge tuning (`one-shot-tune-load.py`) is left untouched; this script
reuses its context binding, original solver cases, candidate selection and
recording, and adds what a multi-site graph needs: nodes copied from the
calibrated ones (same VM, limits and pool, so their service observations
apply), the phases and per-node base factors read from a design file, and a
cross-site requirement. No VM, deployment or benchmark is launched.
"""

import argparse
import copy
import json
import runpy
from datetime import UTC, datetime
from pathlib import Path

import yaml

ORIGINAL = runpy.run_path(str(Path(__file__).with_name("one-shot-tune-load.py")))
digest, write_json, execute = (
    ORIGINAL["digest"],
    ORIGINAL["write_json"],
    ORIGINAL["execute"],
)
FACTORS, OPTIONS = ORIGINAL["FACTORS"], ORIGINAL["OPTIONS"]
# Original relative function pressure; the per-node base comes from the design.
WEIGHTS = {
    "one-shot-arithmetic": 1.15,
    "one-shot-sort": 1.0,
    "one-shot-memory": 1.1,
    "one-shot-matrix": 0.9,
    "one-shot-mixed": 0.95,
}
PERIOD_SECONDS = 360


def expand_scenario(document, design):
    """Copy calibrated nodes onto the design's new ids and set its graph.

    Host CPU placement belongs to a deployment, not to the load model: it is
    dropped, like the runtime images, so the model only binds sizes and limits.
    """
    result = copy.deepcopy(document)
    settings = result["oneShot"]
    by_id = {node["id"]: node for node in settings["nodes"]}
    added = []
    for new, source in design["mirror"].items():
        if new in by_id:
            raise ValueError(f"{new} already exists in the scenario")
        added.append({**copy.deepcopy(by_id[source]), "id": new})
    cloud = [node for node in settings["nodes"] if node["kind"] == "cloud"]
    edges = [node for node in settings["nodes"] if node["kind"] == "edge"]
    settings["nodes"] = [*edges, *added, *cloud]
    settings["neighbors"] = design["neighbors"]
    settings.pop("hostCpuPlacement", None)
    settings.pop("runtimeDistribution", None)
    return result


def expand_observations(observations, design):
    """Give each copied node the service observations of its source node."""
    result = copy.deepcopy(observations)
    nodes = {node["id"]: node for node in result["settings"]["nodes"]}
    for new, source in design["mirror"].items():
        result["settings"]["nodes"].append({**copy.deepcopy(nodes[source]), "id": new})
        result["service"][new] = copy.deepcopy(result["service"][source])
    result["mirroredFrom"] = design["mirror"]
    return result


def site_of(design):
    """Map each edge to its site."""
    return {node: site for site, nodes in design["sites"].items() for node in nodes}


def candidate_rates(context, design, phase, factor):
    """The original pressure rule, with per-node bases from the design."""
    rounded_rate = ORIGINAL["rounded_rate"]
    targets = phase["targets"]
    rates = {}
    for node, functions in context["parameters"].items():
        rates[node] = {}
        for name, params in functions.items():
            count = context["initialReplicas"][node][name]
            capacity = count * params["utilization"] / params["serviceSeconds"]
            scale = 0.03 if node == context["cloud"] else design["baseFactors"][node]
            if node in targets:
                scale = factor * (1.5 if targets[node] == name else 0.65)
            if node == context["cloud"]:
                scale *= 1 + phase["index"] * 0.05
            rates[node][name] = rounded_rate(
                capacity * scale * WEIGHTS[name], context["flowQuantum"]
            )
    return rates


def cross_site(routes, sites):
    """Keep the routes whose source and destination are on different sites."""
    return [
        route
        for route in routes
        if sites[route["source"]] != sites[route["destination"]]
    ]


def select(candidates, *, require_cloud, require_cross, sites):
    """The original first-satisfying choice, optionally also across sites."""
    peer_routes = ORIGINAL["peer_routes"]
    for result in candidates:
        routes = peer_routes(result)
        if (
            routes
            and (not require_cloud or result["summary"]["cloudRequestedEdgeRps"] > 0)
            and (not require_cross or cross_site(routes, sites))
        ):
            return result
    raise ValueError("no candidate satisfies the requested offload pattern")


def coverage(selected, design, sites):
    """Check the design's requirements over the selected windows."""
    peer_routes = ORIGINAL["peer_routes"]
    required = design["requirements"]
    intervals = []
    for epoch, result in enumerate(selected):
        routes = peer_routes(result)
        counts = {
            source: sum(route["source"] == source for route in routes)
            for source in result["context"]["edges"]
        }
        intervals.append(
            {
                "epoch": epoch,
                "phase": result["id"],
                "routes": routes,
                "crossSiteRoutes": cross_site(routes, sites),
                "originsWithMultipleNeighbors": [
                    source for source, count in counts.items() if count >= 2
                ],
            }
        )
    totals = {
        "intervalsWithPeerOffload": sum(bool(row["routes"]) for row in intervals),
        "intervalsWithMultipleNeighbors": sum(
            bool(row["originsWithMultipleNeighbors"]) for row in intervals
        ),
        "intervalsWithCrossSiteOffload": sum(
            bool(row["crossSiteRoutes"]) for row in intervals
        ),
    }
    if totals["intervalsWithPeerOffload"] < required["peerOffloadIntervals"]:
        raise ValueError("peer offload is missing in some interval")
    if totals["intervalsWithMultipleNeighbors"] < required["multiReceiverIntervals"]:
        raise ValueError("too few intervals with a source offloading to two peers")
    if totals["intervalsWithCrossSiteOffload"] < required["crossSiteIntervals"]:
        raise ValueError("too few intervals with offload across sites")
    return {
        **totals,
        "requirements": required,
        "interpretation": "predicted one-hop peer requests, not auction messages",
        "intervals": intervals,
    }


def report(output, selected, context, sites):
    """A table of the predicted routing per interval."""
    peer_routes = ORIGINAL["peer_routes"]
    lines = [
        "# Multi-site one-shot load — Python reference predictions",
        "",
        (
            "Model predictions with the calibrated service times of the copied nodes; "
            "eight rounds with the original helpers; terminal cloud outside the "
            "auction. Not measurements."
        ),
        "",
        (
            "| Interval | Phase | Factor | Edge arrivals/s | Local/s | Peers/s "
            "| Cross-site/s | Cloud requested/s |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for epoch, row in enumerate(selected):
        summary = row["summary"]
        cross = sum(route["rateRps"] for route in cross_site(peer_routes(row), sites))
        lines.append(
            f"| {epoch} | {row['id']} | {row['factor']} "
            f"| {summary['offeredEdgeRps']:.2f} | {summary['localRps']:.2f} "
            f"| {summary['neighborRps']:.2f} | {cross:.2f} "
            f"| {summary['cloudRequestedEdgeRps']:.2f} |"
        )
    (output / "report.md").write_text("\n".join(lines) + "\n")


def tune(args):
    """Expand, solve every candidate, select, and write the shared trace."""
    from nanolab.tasks.one_shot.trace import TraceArtifact

    design = json.loads(args.design.read_text())
    phases = design["phases"]
    if len(phases) * PERIOD_SECONDS != design["plannedMeasurementSeconds"]:
        raise ValueError("phases do not cover the planned measurement")
    sites = site_of(design)
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        source_snapshot = args.output / "source-used.py"
        original_snapshot = args.output / "original-source-used.py"
        with source_snapshot.open("xb") as destination:
            destination.write(Path(__file__).read_bytes())
        with original_snapshot.open("xb") as destination:
            destination.write(
                Path(__file__).with_name("one-shot-tune-load.py").read_bytes()
            )
        reference = ORIGINAL["verify_reference"](args.reference)
        scenario = expand_scenario(yaml.safe_load(args.scenario.read_text()), design)
        observations = expand_observations(
            json.loads(args.observations.read_text()), design
        )
        with (args.output / "model-scenario.yaml").open("x") as stream:
            yaml.safe_dump(scenario, stream, sort_keys=False)
        write_json(args.output / "model-observations.json", observations)
        context = ORIGINAL["context_from_scenario"](scenario, observations)
        if sorted(sites) != context["edges"]:
            raise ValueError("design sites must list exactly the scenario's edges")
        if context["maxRounds"] != 8:
            raise ValueError("the agreed experiment uses exactly eight rounds")
        write_json(
            args.output / "input-context.json",
            {
                "reference": reference,
                "design": design,
                "designSha256": digest(args.design),
                "scenarioSha256": digest(args.scenario),
                "observationsSha256": digest(args.observations),
                "scriptSha256": digest(Path(__file__)),
                "context": context,
                "auctionOptions": OPTIONS,
                "candidateFactors": FACTORS,
            },
        )
        selected, summaries = [], []
        last = len(phases) - 1
        for index, phase in enumerate(phases):
            phase = {**phase, "index": index}
            candidates = []
            for factor in FACTORS:
                identity = phase["id"] + "-" + str(factor)
                source = args.output / (identity + ".input.json")
                destination = args.output / (identity + ".result.json")
                write_json(
                    source,
                    {
                        "id": phase["id"],
                        "factor": factor,
                        "context": context,
                        "rates": candidate_rates(context, design, phase, factor),
                    },
                )
                execute(
                    identity,
                    [
                        str(args.reference_python),
                        str(original_snapshot),
                        "case",
                        "--input",
                        str(source),
                        "--reference",
                        str(args.reference),
                        "--output",
                        str(destination),
                    ],
                    args.output,
                )
                result = json.loads(destination.read_text())
                candidates.append(result)
                summaries.append(
                    {"phase": phase["id"], "factor": factor, **result["summary"]}
                )
            selected.append(
                select(
                    candidates,
                    require_cloud=0 < index < last,
                    require_cross=phase.get("requireCrossSite", False),
                    sites=sites,
                )
            )
        write_json(args.output / "candidate-summaries.json", summaries)
        write_json(args.output / "selected.json", selected)
        write_json(
            args.output / "offload-coverage.json", coverage(selected, design, sites)
        )
        trace = TraceArtifact(
            nodes=[*context["edges"], context["cloud"]],
            functions=context["functions"],
            period_seconds=PERIOD_SECONDS,
            flow_quantum=context["flowQuantum"],
            seed=7,
            payloads={
                name: context["parameters"][context["cloud"]][name]["input"]
                for name in context["functions"]
            },
            windows=[row["rates"] for row in selected],
            phase_ids=[row["id"] for row in selected],
            node_roles={
                node: "cloud-direct" if node == context["cloud"] else "edge"
                for node in context["parameters"]
            },
        )
        write_json(args.output / "trace.json", trace.model_dump())
        updated = copy.deepcopy(scenario)
        updated["oneShot"]["timing"]["matrix"] = [
            {
                "id": row["id"],
                "network": "observed-local",
                "rates": {
                    node: {name + "-rust": rate for name, rate in functions.items()}
                    for node, functions in row["rates"].items()
                },
            }
            for row in selected
        ]
        with (args.output / "load-scenario.yaml").open("x") as stream:
            yaml.safe_dump(updated, stream, sort_keys=False)
        report(args.output, selected, context, sites)
        write_json(
            args.output / "manifest.json",
            {
                "qualified": False,
                "purpose": "multi-site-load-design",
                "referenceCommit": ORIGINAL["REFERENCE_COMMIT"],
                "periodSeconds": PERIOD_SECONDS,
                "intervals": len(phases),
                "plannedMeasurementSeconds": design["plannedMeasurementSeconds"],
                "files": {
                    path.name: digest(path)
                    for path in sorted(args.output.iterdir())
                    if path.is_file()
                },
            },
        )
        print(json.dumps({"output": str(args.output), "candidates": len(summaries)}))
    except BaseException as error:
        write_json(
            args.output / "failure.json",
            {"error": repr(error), "atUtc": datetime.now(UTC).isoformat()},
        )
        raise


def main(argv=None):
    """Parse the tuning inputs and run it."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in [
        "design",
        "scenario",
        "observations",
        "reference",
        "reference-python",
        "output",
    ]:
        parser.add_argument("--" + name, type=Path, required=True)
    tune(parser.parse_args(argv))


if __name__ == "__main__":
    main()
