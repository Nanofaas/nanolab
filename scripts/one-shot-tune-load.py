#!/usr/bin/env python3
"""Tune nine load windows using the original pinned one-shot Python helpers.

The coordinator runs in NanoLab's environment; individual cases run in the
isolated reference environment. No VM, deployment or benchmark is launched.
"""

import argparse
import copy
import csv
import hashlib
import importlib
import json
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

REFERENCE_COMMIT = "71899f720a4ffffd070ebf7ddc5b74afddfde9c5"
FACTORS = [1.5, 2, 3, 4, 6, 8, 12, 16]
OPTIONS = {
    "epsilon": 0.01,
    "eta": 0.0,
    "zeta": 0.0,
    "latency_weight": 0.0,
    "fairness_weight": 0.0,
    "unit_bids": False,
}


def digest(path):
    """Hash the exact saved bytes without loading large raw files into memory."""
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path, value):
    """Preserve prior results and reject nonfinite JSON numbers."""
    with Path(path).open("x") as output:
        output.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def verify_reference(reference):
    """Check every Python source against the previously pinned git archive."""
    provenance = json.loads(
        (reference.parent / "reference-provenance.json").read_text()
    )
    archive = reference.parent / "reference.tar"
    if (
        provenance["commit"] != REFERENCE_COMMIT
        or digest(archive) != provenance["archiveSha256"]
    ):
        raise ValueError("reference revision/archive differs from the pinned original")
    sources = {}
    with tarfile.open(archive) as bundle:
        for member in bundle:
            if member.isfile() and member.name.endswith(".py"):
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ValueError("missing source in pinned archive: " + member.name)
                with stream:
                    expected = hashlib.sha256(stream.read()).hexdigest()
                actual = digest(reference / member.name)
                if actual != expected:
                    raise ValueError("reference source changed: " + member.name)
                sources[member.name] = actual
    return {
        "commit": REFERENCE_COMMIT,
        "archiveSha256": digest(archive),
        "sources": sources,
    }


def simulate(document, reference):
    """Apply original LSP, bid, grant, LSPr_x and memory-start transitions."""
    sys.path.insert(0, str(reference))
    import numpy as np

    try_solve_local = importlib.import_module("models.local_sp").try_solve_local
    models = importlib.import_module("models.sp")
    auction = importlib.import_module("run_faasmadea")
    local_model, restricted_model = models.LSP, models.LSPr_x
    compute_residual_capacity = auction.compute_residual_capacity
    define_bids, evaluate_bids = auction.define_bids, auction.evaluate_bids
    start_additional_replicas = auction.start_additional_replicas

    context, rates = document["context"], document["rates"]
    nodes, names = context["edges"], context["functions"]
    quantum = Decimal(str(context["flowQuantum"]))
    nn, nf = len(nodes), len(names)
    load = np.zeros((nn, nf), dtype=int)
    for i, node in enumerate(nodes):
        for f, name in enumerate(names):
            units = Decimal(str(rates[node][name])) / quantum
            if not units.is_finite() or units < 0 or units != units.to_integral_value():
                raise ValueError("rate is not an exact multiple of flowQuantum")
            load[i, f] = int(units)
    parameters = context["parameters"]
    v = {
        "Nn": {None: nn},
        "Nf": {None: nf},
        "memory_capacity": {
            i + 1: context["pools"][node] for i, node in enumerate(nodes)
        },
        "memory_requirement": {
            f + 1: parameters[nodes[0]][name]["memoryMiB"]
            for f, name in enumerate(names)
        },
        "max_utilization": {
            f + 1: parameters[nodes[0]][name]["utilization"]
            for f, name in enumerate(names)
        },
        "pi": {f + 1: 0 for f in range(nf)},
        "beta": {
            (i + 1, j + 1, f + 1): 1
            for i in range(nn)
            for j in range(nn)
            for f in range(nf)
        },
    }
    for key in ["alpha", "delta", "gamma"]:
        v[key] = {
            (i + 1, f + 1): parameters[node][name][key]
            for i, node in enumerate(nodes)
            for f, name in enumerate(names)
        }
    v["demand"] = {
        (i + 1, f + 1): parameters[node][name]["serviceSeconds"] * float(quantum)
        for i, node in enumerate(nodes)
        for f, name in enumerate(names)
    }
    v["incoming_load"] = {
        (i + 1, f + 1): int(load[i, f]) for i in range(nn) for f in range(nf)
    }
    memory = np.array([v["memory_requirement"][f + 1] for f in range(nf)])
    started = time.monotonic()
    x, r, initial_omega = (np.zeros((nn, nf)) for _ in range(3))
    initial_objectives = []
    for i in range(nn):
        v["whoami"] = {None: i + 1}
        solution = try_solve_local(local_model(), {None: v})
        if solution is None:
            raise ValueError("original local solver returned no supported LSP solution")
        x[i], r[i], initial_omega[i] = solution["x"], solution["r"], solution["omega"]
        initial_objectives.append(solution["obj"])
    rho = np.array([context["pools"][node] for node in nodes]) - r @ memory
    neighborhood = np.array(
        [[int(peer in context["neighbors"][node]) for peer in nodes] for node in nodes]
    )
    y = np.zeros((nn, nn, nf))
    prices, fairness = np.zeros((nn, nf)), np.zeros((nn, nf))
    omega = initial_omega.copy()
    rounds = []
    for iteration in range(context["maxRounds"]):
        capacity, blackboard, ell = compute_residual_capacity(x, y, r, {None: v})
        bids, memory_bids, _ = define_bids(
            omega,
            blackboard,
            prices,
            {None: v},
            neighborhood,
            rho,
            OPTIONS,
            np.zeros((nn, nn)),
            fairness,
            False,
        )
        additional = np.zeros((nn, nf))
        grant = np.zeros((nn, nn, nf))
        if len(bids):
            grant, prices, _, _ = evaluate_bids(
                bids,
                blackboard,
                {None: v},
                previous_y=y,
                ell=ell,
                p=prices,
                capacity=capacity,
                u0=np.ones((nn, nf)) * 0.9,
                auction_options=OPTIONS,
                tentatively_start_replicas=False,
                may_replace_existing_assignments=False,
            )
            y += grant
            outbound = y.sum(axis=1)
            fairness[outbound > 0] += 1
            restricted = dict(v)
            restricted["y_bar"] = {
                (i + 1, j + 1, f + 1): y[i, j, f]
                for i in range(nn)
                for j in range(nn)
                for f in range(nf)
            }
            restricted["omega_bar"] = {
                (i + 1, f + 1): outbound[i, f] for i in range(nn) for f in range(nf)
            }
            restricted["x_bar"] = {
                (i + 1, f + 1): x[i, f] for i in range(nn) for f in range(nf)
            }
            for i in range(nn):
                restricted["whoami"] = {None: i + 1}
                solution = try_solve_local(restricted_model(), {None: restricted})
                if solution is None:
                    raise ValueError(
                        "original restricted solver could not preserve grants"
                    )
                r[i] = solution["r"]
            rho = np.array([context["pools"][node] for node in nodes]) - r @ memory
            omega = initial_omega - outbound
        else:
            additional, rho = start_additional_replicas(memory_bids, r, {None: v}, rho)
            r += additional
        if (rho < 0).any() or (y < 0).any() or (omega < 0).any():
            raise ValueError("original reference violated RAM/flow nonnegativity")
        if (y * (1 - neighborhood[:, :, None])).any():
            raise ValueError("original reference assigned a non-neighbor")
        rounds.append(
            {
                "round": iteration,
                "offersUnits": blackboard.tolist(),
                "bids": bids.to_dict(orient="records"),
                "memoryBids": memory_bids.to_dict(orient="records"),
                "grantsUnits": grant.tolist(),
                "additionalReplicas": additional.tolist(),
                "replicas": r.tolist(),
                "memoryUsedMiB": (r @ memory).tolist(),
                "remainingUnits": omega.tolist(),
                "prices": prices.tolist(),
            }
        )
    pending = load - x - y.sum(axis=1)
    if (pending < 0).any():
        raise ValueError("original reference overassigned external arrivals")
    cap = r * np.array(
        [
            [v["max_utilization"][f + 1] / v["demand"][i + 1, f + 1] for f in range(nf)]
            for i in range(nn)
        ]
    )
    if (x + y.sum(axis=0) > cap + 1e-8).any():
        raise ValueError("original reference exceeded modeled handler capacity")
    cloud = context["cloud"]
    cloud_assessment = {}
    for f, name in enumerate(names):
        params = parameters[cloud][name]
        wanted = float(pending[:, f].sum() * float(quantum)) + rates[cloud][name]
        capacity = (
            context["cloudReplicas"][name]
            * params["utilization"]
            / params["serviceSeconds"]
        )
        cloud_assessment[name] = {
            "directRps": rates[cloud][name],
            "requestedRps": wanted,
            "modeledCapacityRps": capacity,
            "modelUtilization": wanted / capacity,
            "capacityExcessRps": max(0, wanted - capacity),
        }
    return {
        "id": document["id"],
        "factor": document["factor"],
        "context": context,
        "rates": rates,
        "rounds": rounds,
        "localUnits": x.tolist(),
        "neighborUnits": y.tolist(),
        "cloudRequestedUnits": pending.tolist(),
        "replicas": r.tolist(),
        "initialObjectives": initial_objectives,
        "memoryUsedMiB": (r @ memory).tolist(),
        "cloudAssessment": cloud_assessment,
        "summary": {
            "offeredEdgeRps": float(load.sum() * float(quantum)),
            "localRps": float(x.sum() * float(quantum)),
            "neighborRps": float(y.sum() * float(quantum)),
            "cloudRequestedEdgeRps": float(pending.sum() * float(quantum)),
            "referenceWallSeconds": time.monotonic() - started,
        },
    }


def rounded_rate(rate, quantum):
    """Use an explicit positive decimal grid, not float multiplication tails."""
    units = (Decimal(str(rate)) / Decimal(str(quantum))).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return float(max(Decimal(1), units) * Decimal(str(quantum)))


def context_from_scenario(document, observations):
    """Bind original service observations to the prospective resource layout."""
    from nanolab.config.one_shot import resolve_node_functions
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.one_shot.capacity import comparison_replica_vectors

    settings = ScenarioConfig.model_validate(document).one_shot
    if settings is None:
        raise ValueError("one-shot scenario required")
    if settings.timing is None:
        raise ValueError("explicit timing protocol required")
    aliases = {name: name.removesuffix("-rust") for name in settings.functions}
    initial = comparison_replica_vectors(settings, aliases)
    parameters = {}
    for node in settings.nodes:
        parameters[node.id] = {}
        old_node = next(
            row for row in observations["settings"]["nodes"] if row["id"] == node.id
        )
        if node.cpus != old_node["cpus"] or node.memory_mib != old_node["memoryMiB"]:
            raise ValueError("VM size changed since the service observations")
        for name, fn in resolve_node_functions(settings, node.id).items():
            old_fn = observations["settings"]["functions"][name]
            old_limit = old_node.get("functionLimits", {}).get(name, old_fn)
            if old_limit["cpu"] != fn.cpu or old_fn["input"] != fn.input:
                raise ValueError(
                    "CPU quota/workload changed since the service observations"
                )
            params = fn.model_dump(by_alias=True)
            params["serviceSeconds"] = observations["service"][node.id][aliases[name]][
                "meanSeconds"
            ]
            parameters[node.id][aliases[name]] = params
    cloud = next(node.id for node in settings.nodes if node.kind == "cloud")
    return {
        "edges": sorted(node.id for node in settings.nodes if node.kind == "edge"),
        "cloud": cloud,
        "functions": sorted(aliases.values()),
        "parameters": parameters,
        "pools": {node.id: node.memory_capacity_mib for node in settings.nodes},
        "neighbors": settings.neighbors,
        "cloudReplicas": initial[cloud],
        "initialReplicas": initial,
        "flowQuantum": settings.flow_quantum,
        "maxRounds": settings.timing.protocol.max_rounds,
    }


def candidate_rates(context, phase, factor):
    """Explore pressure relative to the original balanced replica capacities."""
    weights = {
        "one-shot-arithmetic": 1.15,
        "one-shot-sort": 1.0,
        "one-shot-memory": 1.1,
        "one-shot-matrix": 0.9,
        "one-shot-mixed": 0.95,
    }
    base_factors = dict(
        zip(context["edges"], [0.28, 0.31, 0.34, 0.30, 0.36], strict=True)
    )
    targets = phase["targets"]
    rates = {}
    for node, functions in context["parameters"].items():
        rates[node] = {}
        for name, params in functions.items():
            count = context["initialReplicas"][node][name]
            capacity = count * params["utilization"] / params["serviceSeconds"]
            weight = weights[name]
            scale = 0.03 if node == context["cloud"] else base_factors[node]
            if node in targets:
                scale = factor * (1.5 if targets[node] == name else 0.65)
            if node == context["cloud"]:
                scale *= 1 + phase["index"] * 0.05
            rates[node][name] = rounded_rate(
                capacity * scale * weight, context["flowQuantum"]
            )
    return rates


def peer_routes(result):
    """Count distinct receiving edges from actual grants, across all functions."""
    context = result["context"]
    routes = []
    for i, source in enumerate(context["edges"]):
        for j, destination in enumerate(context["edges"]):
            functions = {
                name: result["neighborUnits"][i][j][f] * context["flowQuantum"]
                for f, name in enumerate(context["functions"])
                if result["neighborUnits"][i][j][f] > 0
            }
            if functions:
                routes.append(
                    {
                        "source": source,
                        "destination": destination,
                        "rateRps": sum(functions.values()),
                        "functions": functions,
                    }
                )
    return routes


def select_candidate(candidates, *, require_cloud):
    """Select the first tested load with the requested observable routing."""
    for result in candidates:
        if peer_routes(result) and (
            not require_cloud or result["summary"]["cloudRequestedEdgeRps"] > 0
        ):
            return result
    raise ValueError("no candidate satisfies the requested offload pattern")


def offload_coverage(selected):
    """Require peer offload everywhere and at least two multi-receiver windows."""
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
                "originsWithMultipleNeighbors": [
                    source for source, count in counts.items() if count >= 2
                ],
            }
        )
    active = sum(bool(row["routes"]) for row in intervals)
    multiple = sum(bool(row["originsWithMultipleNeighbors"]) for row in intervals)
    if len(intervals) != 9 or active != 9:
        raise ValueError("peer offload is required in every interval (nine windows)")
    if multiple < 2:
        raise ValueError("one source must offload to two neighbors in two intervals")
    return {
        "intervalsWithPeerOffload": active,
        "intervalsWithMultipleNeighbors": multiple,
        "interpretation": "predicted one-hop peer requests, not auction messages",
        "intervals": intervals,
    }


def execute(name, command, output):
    """Record every external step and stop on its first failure, without retries."""
    started = datetime.now(UTC).isoformat()
    print(started, name, "started", flush=True)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as error:
        write_json(
            output / (name + ".command.json"),
            {
                "command": command,
                "startedAtUtc": started,
                "finishedAtUtc": datetime.now(UTC).isoformat(),
                "timeoutSeconds": 300,
                "stdout": str(error.stdout),
                "stderr": str(error.stderr),
            },
        )
        raise
    write_json(
        output / (name + ".command.json"),
        {
            "command": command,
            "startedAtUtc": started,
            "finishedAtUtc": datetime.now(UTC).isoformat(),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
    if result.returncode:
        raise RuntimeError(name + " failed; diagnostics preserved")
    return result


def tune(args):
    """Save all candidates, select nine windows and validate the shared trace."""
    import yaml

    from nanolab.tasks.one_shot.trace import TraceArtifact

    args.output.mkdir(parents=True, exist_ok=False)
    try:
        source_snapshot = args.output / "source-used.py"
        with source_snapshot.open("xb") as destination:
            destination.write(Path(__file__).read_bytes())
        reference = verify_reference(args.reference)
        environment = execute(
            "reference-environment",
            [
                str(args.reference_python),
                "-c",
                "import sys,json,importlib.metadata as m; "
                "print(json.dumps({'python':sys.version,'packages':"
                "{name:m.version(name) for name in ['numpy','pandas','scipy','pyomo',"
                "'matplotlib','networkx','seaborn','parse','tqdm']}}))",
            ],
            args.output,
        )
        scenario = yaml.safe_load(args.scenario.read_text())
        observations = json.loads(args.observations.read_text())
        context = context_from_scenario(scenario, observations)
        if context["maxRounds"] != 8:
            raise ValueError("the agreed experiment uses exactly eight rounds")
        phases = [
            {
                "id": "opening-edge-2",
                "targets": {"edge-2": "one-shot-arithmetic"},
            },
            {"id": "hotspot-edge-5", "targets": {"edge-5": "one-shot-mixed"}},
            {"id": "hotspot-edge-1", "targets": {"edge-1": "one-shot-arithmetic"}},
            {"id": "hotspot-edge-2", "targets": {"edge-2": "one-shot-sort"}},
            {"id": "hotspot-edge-3", "targets": {"edge-3": "one-shot-memory"}},
            {"id": "hotspot-edge-4", "targets": {"edge-4": "one-shot-matrix"}},
            {
                "id": "two-triangles",
                "targets": {"edge-2": "one-shot-sort", "edge-4": "one-shot-matrix"},
            },
            {
                "id": "diffuse-subset",
                "targets": {
                    "edge-1": "one-shot-arithmetic",
                    "edge-3": "one-shot-memory",
                    "edge-5": "one-shot-mixed",
                },
            },
            {
                "id": "closing-edge-5",
                "targets": {"edge-5": "one-shot-sort"},
            },
        ]
        write_json(
            args.output / "input-context.json",
            {
                "reference": reference,
                "referenceEnvironment": json.loads(environment.stdout),
                "scenarioSha256": digest(args.scenario),
                "observationsSha256": digest(args.observations),
                "scriptSha256": digest(Path(__file__)),
                "context": context,
                "auctionOptions": OPTIONS,
                "candidateFactors": FACTORS,
                "selectionRequirements": {
                    "peerOffloadIntervals": 9,
                    "sameSourceToMultiplePeersIntervals": 2,
                    "cloudRequestedInCentralIntervals": True,
                    "choose": "first tested factor satisfying phase requirements",
                    "scope": "workload design, not measured-performance gates",
                },
                "serviceScope": {
                    key: observations[key]
                    for key in [
                        "type",
                        "independentlyQualified",
                        "scope",
                        "limitation",
                        "sourcePhysicalSha256",
                    ]
                },
            },
        )
        selected, summaries = [], []
        for index, phase in enumerate(phases):
            phase["index"] = index
            candidates = []
            for factor in FACTORS:
                identity = phase["id"] + "-" + str(factor)
                source, destination = (
                    args.output / (identity + ".input.json"),
                    args.output / (identity + ".result.json"),
                )
                write_json(
                    source,
                    {
                        "id": phase["id"],
                        "factor": factor,
                        "context": context,
                        "rates": candidate_rates(context, phase, factor),
                    },
                )
                execute(
                    identity,
                    [
                        str(args.reference_python),
                        str(source_snapshot),
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
            selected.append(select_candidate(candidates, require_cloud=0 < index < 8))
        write_json(args.output / "candidate-summaries.json", summaries)
        write_json(args.output / "selected.json", selected)
        write_json(args.output / "offload-coverage.json", offload_coverage(selected))
        trace = TraceArtifact(
            nodes=[*context["edges"], context["cloud"]],
            functions=context["functions"],
            period_seconds=360,
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
        windows = [
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
        updated["oneShot"]["timing"]["matrix"] = windows
        with (args.output / "load-scenario.yaml").open("x") as output:
            yaml.safe_dump(updated, output, sort_keys=False)
        report(args.output, selected, context)
        execute(
            "plot",
            [
                str(args.reference_python),
                str(source_snapshot),
                "plot",
                "--input",
                str(args.output / "selected.json"),
                "--output",
                str(args.output),
            ],
            args.output,
        )
        write_json(
            args.output / "manifest.json",
            {
                "qualified": False,
                "purpose": "first-functional-experiment-load-tuning",
                "referenceCommit": REFERENCE_COMMIT,
                "periodSeconds": 360,
                "intervals": 9,
                "plannedMeasurementSeconds": 3240,
                "interpretation": (
                    "Model predictions with historical64MiB service estimates; "
                    "not measured cloud acceptance or VM saturation"
                ),
                "files": {
                    path.name: digest(path)
                    for path in args.output.iterdir()
                    if path.is_file()
                },
            },
        )
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "candidates": len(summaries),
                    "intervals": 9,
                }
            ),
            flush=True,
        )
    except BaseException as error:
        write_json(
            args.output / "failure.json",
            {"error": repr(error), "atUtc": datetime.now(UTC).isoformat()},
        )
        raise


def report(output, selected, context):
    """Publish the selected rates and routing predictions with honest provenance."""
    lines = [
        "# Taratura del carico one-shot — riferimento Python",
        "",
        "Previsioni con tempi storici a64MiB e quote CPU originali; "
        "RAM prospettica64/128/256/128/256MiB.",
        "Otto round fissi con helper originali, bid aggregate, nessuna sostituzione "
        "o gerarchia. Cloud terminale con7 repliche per funzione, fuori dall'asta.",
        "I fattori esplorati definiscono il carico, "
        "non soglie di accettazione delle prestazioni.",
        "Scelta: primo fattore con inoltri ai vicini in ogni intervallo; "
        "nei sette intervalli centrali anche richiesta al cloud. "
        "Almeno due intervalli devono avere uno stesso nodo che inoltra a "
        "due vicini distinti. Nessun fallback se il carico non soddisfa questi "
        "requisiti; conteggi verificati in offload-coverage.json.",
        "La capacità cloud è una stima fluida: richieste al cloud ed eccesso "
        "stimato non sono completamenti o reject osservati.",
        "",
        "| Intervallo | Condizione | Fattore | Arrivi edge/s | Locale/s | "
        "Vicini/s | Cloud richiesto/s | Eccesso cloud/s |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    with (output / "rates.csv").open("x") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["epoch", "condition", "node", "function", "rateRps", "requestsPer360s"]
        )
        for epoch, row in enumerate(selected):
            stats = row["summary"]
            excess = sum(
                fn["capacityExcessRps"] for fn in row["cloudAssessment"].values()
            )
            lines.append(
                f"| {epoch} | {row['id']} | {row['factor']} | "
                f"{stats['offeredEdgeRps']:.2f} | {stats['localRps']:.2f} | "
                f"{stats['neighborRps']:.2f} | "
                f"{stats['cloudRequestedEdgeRps']:.2f} | {excess:.2f} |"
            )
            for node, functions in row["rates"].items():
                for name, rate in functions.items():
                    writer.writerow(
                        [
                            epoch,
                            row["id"],
                            node,
                            name,
                            rate,
                            int(Decimal(str(rate)) * 360),
                        ]
                    )
    (output / "report.md").write_text("\n".join(lines) + "\n")


def plot(args):
    """Export scientific figures of offered rates and predicted destinations."""
    matplotlib = importlib.import_module("matplotlib")

    matplotlib.use("Agg")
    plt = importlib.import_module("matplotlib.pyplot")

    selected = json.loads(args.input.read_text())
    context = selected[0]["context"]
    boundaries = [6 * i for i in range(10)]
    figure, axes = plt.subplots(2, 3, figsize=(15, 8), sharex=True)
    for axis, node in zip(
        axes.flat, [*context["edges"], context["cloud"]], strict=True
    ):
        for name in context["functions"]:
            axis.stairs(
                [row["rates"][node][name] for row in selected],
                boundaries,
                label=name.removeprefix("one-shot-"),
            )
        axis.set(title=node, ylabel="richieste/s", xlabel="minuti", xlim=(0, 54))
        axis.grid(alpha=0.2)
    axes[0, 0].legend(fontsize=8)
    figure.suptitle("Carico proposto — nove intervalli simultanei da360s")
    figure.tight_layout()
    figure.savefig(args.output / "offered-rates.png", dpi=180)
    plt.close(figure)
    figure, axis = plt.subplots(figsize=(12, 5))
    for name, label in [
        ("localRps", "locale"),
        ("neighborRps", "vicini"),
        ("cloudRequestedEdgeRps", "cloud richiesto"),
    ]:
        axis.stairs([row["summary"][name] for row in selected], boundaries, label=label)
    axis.set(
        title="Previsioni Python, non misure — otto round e tempi storici",
        xlabel="minuti",
        ylabel="richieste edge/s",
        xlim=(0, 54),
    )
    axis.legend()
    axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(args.output / "predicted-routing.png", dpi=180)
    plt.close(figure)


def main():
    """Keep coordinator dependencies separate from the original solver runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tuning = commands.add_parser("tune")
    for name in ["scenario", "observations", "reference", "reference-python", "output"]:
        tuning.add_argument("--" + name, type=Path, required=True)
    for command in ["case", "plot"]:
        worker = commands.add_parser(command)
        for name in ["input", "output"]:
            worker.add_argument("--" + name, type=Path, required=True)
        if command == "case":
            worker.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "tune":
        tune(args)
    elif args.command == "plot":
        plot(args)
    else:
        write_json(
            args.output, simulate(json.loads(args.input.read_text()), args.reference)
        )


if __name__ == "__main__":
    main()
