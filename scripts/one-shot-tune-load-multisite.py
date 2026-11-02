#!/usr/bin/env python3
"""Tune a multi-site one-shot load with the original pinned Python helpers.

The five-edge tuning (`one-shot-tune-load.py`) is left untouched; this script
reuses its context binding, original solver cases, candidate selection and
recording, and adds what a multi-site graph needs: nodes copied from the
calibrated ones (same VM, limits and pool, so their service observations
apply), the phases and per-node base factors read from a design file, and a
cross-site requirement. No VM, deployment or benchmark is launched.

A design with `hotspots` instead of `phases` ramps the load: each hotspot rises
to its peak and falls back geometrically over `widthIntervals`, hotspots of
different edges overlap, and one factor is searched for all intervals at once,
refusing any whose edge totals change between consecutive intervals by more
than `maxStepRatio`. The one-shot plan follows load with an epoch of delay, so
a step makes the first epoch after it reject what a ramp lets it shed. A ramp
design may set `periodSeconds` (one-minute intervals are the epochs the
runtime plans) and count the intervals with a cloud request
(`requirements.cloudIntervals`) instead of requiring one in each.
"""

import argparse
import copy
import itertools
import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
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
# Rates below this count as this in a step ratio, so that the flow quantum's
# rounding of a near-idle function does not read as a jump.
STEP_FLOOR_RPS = 0.5


def expand_scenario(document, design):
    """Copy calibrated nodes onto the design's new ids and set its graph.

    A copied node also takes its source's host CPUs, on its own site: the
    edges' hosts are the design's sites (the observation rebinds them from the
    environment, which must agree); the runtime
    images are dropped, so the model only binds sizes and limits.
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
    placement = settings.get("hostCpuPlacement")
    if placement is not None:
        for new, source in design["mirror"].items():
            placement["nodes"][new] = copy.deepcopy(placement["nodes"][source])
        placement["hosts"] = site_of(design)
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


def ramp_label(design, interval):
    """Name an interval after the hotspots peaking in it (centres may be halves)."""
    peaks = [
        spot["edge"]
        for spot in design["hotspots"]
        if abs(spot["center"] - interval) <= 0.5
    ]
    return f"i{interval}-" + ("-".join(peaks) if peaks else "between")


def ramp_rates(context, design, interval, factor):
    """The original pressure rule, ramped geometrically around each hotspot.

    A function sits at its base at a hotspot's width and at the original peak
    (factor x 1.5 for the hotspot's function, x 0.65 for the others) at its
    centre; between them its scale is base x (peak / base) ** share, so each
    interval multiplies it by the same ratio. Overlapping hotspots of one edge
    take the higher scale.
    """
    rounded_rate = ORIGINAL["rounded_rate"]
    width = design["profile"]["widthIntervals"]
    rates = {}
    for node, functions in context["parameters"].items():
        rates[node] = {}
        for name, params in functions.items():
            count = context["initialReplicas"][node][name]
            capacity = count * params["utilization"] / params["serviceSeconds"]
            if node == context["cloud"]:
                scale = 0.03 * (1 + interval * 0.05)
            else:
                base = scale = design["baseFactors"][node]
                for spot in design["hotspots"]:
                    if spot["edge"] != node:
                        continue
                    share = max(0.0, 1 - abs(interval - spot["center"]) / width)
                    peak = (
                        factor
                        * spot.get("amplitude", 1)
                        * (1.5 if spot["function"] == name else 0.65)
                    )
                    if peak > base:
                        scale = max(scale, base * (peak / base) ** share)
            rates[node][name] = rounded_rate(
                capacity * scale * WEIGHTS[name], context["flowQuantum"]
            )
    return rates


def step_ratios(windows, edges):
    """Each edge function's largest rate change between consecutive intervals.

    Per function, as the runtime forecasts; rates are floored at
    STEP_FLOOR_RPS first.
    """
    ratios = {}
    for edge in edges:
        for name in windows[0][edge]:
            rates = [max(window[edge][name], STEP_FLOOR_RPS) for window in windows]
            ratios[edge + "/" + name] = max(
                (max(a, b) / min(a, b) for a, b in itertools.pairwise(rates)),
                default=1.0,
            )
    return ratios


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
        "intervalsWithCloudRequest": sum(
            result["summary"]["cloudRequestedEdgeRps"] > 0 for result in selected
        ),
    }
    if totals["intervalsWithPeerOffload"] < required["peerOffloadIntervals"]:
        raise ValueError("peer offload is missing in some interval")
    if totals["intervalsWithMultipleNeighbors"] < required["multiReceiverIntervals"]:
        raise ValueError("too few intervals with a source offloading to two peers")
    if totals["intervalsWithCrossSiteOffload"] < required["crossSiteIntervals"]:
        raise ValueError("too few intervals with offload across sites")
    if totals["intervalsWithCloudRequest"] < required.get("cloudIntervals", 0):
        raise ValueError("too few intervals with a cloud request")
    return {
        **totals,
        "requirements": required,
        "interpretation": "predicted one-hop peer requests, not auction messages",
        "intervals": intervals,
    }


def select_phases(solve, context, design, sites):
    """Per phase, the first factor whose solution has the required offloads."""
    phases = design["phases"]
    selected, summaries = [], []
    last = len(phases) - 1
    for index, phase in enumerate(phases):
        phase = {**phase, "index": index}
        candidates = []
        for factor in FACTORS:
            result = solve(
                phase["id"] + "-" + str(factor),
                phase["id"],
                factor,
                candidate_rates(context, design, phase, factor),
            )
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
    return selected, summaries, None


def select_ramp(solve, context, design, sites, jobs=1):
    """The first factor whose ramped intervals all meet the requirements."""
    peer_routes = ORIGINAL["peer_routes"]
    profile = design["profile"]
    intervals = design["intervals"]
    summaries, search = [], []
    for factor in profile.get("factors", FACTORS):
        windows = [ramp_rates(context, design, i, factor) for i in range(intervals)]
        ratios = step_ratios(windows, context["edges"])
        if max(ratios.values()) > profile["maxStepRatio"]:
            # A higher factor only raises the peaks: every later one is steeper.
            search.append({"factor": factor, "stepRatios": ratios, "steep": True})
            break
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(
                pool.map(
                    lambda item, factor=factor: solve(
                        f"{ramp_label(design, item[0])}-{factor}",
                        ramp_label(design, item[0]),
                        factor,
                        item[1],
                    ),
                    enumerate(windows),
                )
            )
        summaries += [
            {"phase": result["id"], "factor": factor, **result["summary"]}
            for result in results
        ]
        failures = []
        for index, result in enumerate(results):
            if not peer_routes(result):
                failures.append(f"interval {index}: no peer offload")
            if (
                "cloudIntervals" not in design["requirements"]
                and 0 < index < intervals - 1
                and result["summary"]["cloudRequestedEdgeRps"] <= 0
            ):
                failures.append(f"interval {index}: no cloud request")
        try:
            coverage(results, design, sites)
        except ValueError as error:
            failures.append(str(error))
        routes = [ORIGINAL["peer_routes"](result) for result in results]
        search.append(
            {
                "factor": factor,
                "stepRatios": ratios,
                "failures": failures,
                "intervalsWithPeerOffload": sum(map(bool, routes)),
                "intervalsWithCrossSiteOffload": sum(
                    bool(cross_site(found, sites)) for found in routes
                ),
                "intervalsWithCloudRequest": sum(
                    result["summary"]["cloudRequestedEdgeRps"] > 0 for result in results
                ),
                "peakEdgeRps": max(
                    result["summary"]["offeredEdgeRps"] for result in results
                ),
                "peakCloudRequestedRps": max(
                    result["summary"]["cloudRequestedEdgeRps"] for result in results
                ),
                "edgeRequests": sum(
                    result["summary"]["offeredEdgeRps"] for result in results
                )
                * design.get("periodSeconds", PERIOD_SECONDS),
            }
        )
        if not failures:
            return results, summaries, search
    raise RampSearchError(search)


class RampSearchError(ValueError):
    """No factor satisfies the ramp; carries the search for the record."""

    def __init__(self, search):
        """Keep every factor's step ratios and failures."""
        super().__init__("no factor satisfies the ramped requirements")
        self.search = search


def solve_case(args, context, identity, phase_id, factor, rates):
    """Solve one candidate with the pinned original helpers."""
    source = args.output / (identity + ".input.json")
    destination = args.output / (identity + ".result.json")
    write_json(
        source,
        {"id": phase_id, "factor": factor, "context": context, "rates": rates},
    )
    execute(
        identity,
        [
            str(args.reference_python),
            str(args.output / "original-source-used.py"),
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
    return json.loads(destination.read_text())


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
    ramp = "hotspots" in design
    intervals = design["intervals"] if ramp else len(design["phases"])
    period = design.get("periodSeconds", PERIOD_SECONDS)
    if intervals * period != design["plannedMeasurementSeconds"]:
        raise ValueError("intervals do not cover the planned measurement")
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
        solve = partial(solve_case, args, context)
        try:
            if ramp:
                selected, summaries, search = select_ramp(
                    solve, context, design, sites, jobs=args.jobs
                )
            else:
                selected, summaries, search = select_phases(
                    solve, context, design, sites
                )
        except RampSearchError as error:
            write_json(args.output / "ramp-search.json", error.search)
            raise
        if search is not None:
            write_json(args.output / "ramp-search.json", search)
        write_json(args.output / "candidate-summaries.json", summaries)
        write_json(args.output / "selected.json", selected)
        write_json(
            args.output / "offload-coverage.json", coverage(selected, design, sites)
        )
        trace = TraceArtifact(
            nodes=[*context["edges"], context["cloud"]],
            functions=context["functions"],
            period_seconds=period,
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
                "periodSeconds": period,
                "intervals": intervals,
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
    parser.add_argument(
        "--jobs", type=int, default=1, help="ramp cases solved concurrently"
    )
    tune(parser.parse_args(argv))


if __name__ == "__main__":
    main()
