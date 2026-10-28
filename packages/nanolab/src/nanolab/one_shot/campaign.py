"""Frozen paired designs and whole-block statistical summaries for local one-shot."""

from __future__ import annotations

import copy
import json
import math
import random
import statistics
from collections.abc import Sequence
from itertools import pairwise, permutations
from pathlib import Path
from typing import Any, cast

import yaml

from nanolab.config import ScenarioConfig
from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.models import CampaignManifest, TimingQualification
from nanolab.one_shot.vm_resources import telemetry_policy
from nanolab.tasks.one_shot.artifacts import (
    canonical_bytes,
    content_hash,
    package_fingerprint,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.conservation import RunEvidence
from nanolab.tasks.one_shot.statistics import quantile
from nanolab.tasks.one_shot.trace import TraceArtifact

MODES = ("baseline", "oracle", "ewma")


def plan_repetitions(paired_cloud_differences: Sequence[float]) -> int:
    """Freeze a balanced sample size from six pilot block differences."""
    values = list(paired_cloud_differences)
    if len(values) < 6 or any(not math.isfinite(v) or abs(v) > 1 for v in values):
        raise ValueError("at least six finite paired pilot differences are required")
    planned = math.ceil(((1.959964 + 0.841621) * statistics.stdev(values) / 0.05) ** 2)
    planned = math.ceil(max(12, planned) / 6) * 6
    if planned > 36:
        raise ValueError(
            "pilot requires more than 36 blocks; revise the campaign budget"
        )
    return planned


def _scope(manifest: CampaignManifest) -> str:
    document = manifest.model_dump(
        exclude={"run_id", "anchor", "mode", "repetition", "seed"}
    )
    document["modes"] = sorted(document["modes"])
    parameters = copy.deepcopy(document["parameters"])
    parameters.pop("nodeEndpoints", None)
    parameters.get("campaign", {}).pop("block", None)
    parameters.get("campaign", {}).pop("seed", None)
    parameters.get("generator", {}).pop("campaignBlock", None)
    document["parameters"] = parameters
    return content_hash(canonical_bytes(document))


def analysis_epochs(trace: TraceArtifact) -> list[int]:
    """Keep the versioned denominator explicit, including all synchronized rounds."""
    if trace.phase_ids is None or trace.node_roles is None:
        raise ValueError("scientific trace requires phase and origin roles")
    if trace.schema_version == 2:
        if (
            trace.period_seconds != 360
            or len(trace.windows) != 9
            or len(trace.functions) != 5
            or len(trace.nodes) != 6
            or len(set(trace.phase_ids)) != 9
            or list(trace.node_roles.values()).count("edge") != 5
            or list(trace.node_roles.values()).count("cloud-direct") != 1
        ):
            raise ValueError("synchronized analysis scope mismatch")
        return list(range(9))
    if trace.schema_version != 1:
        raise ValueError("unsupported scientific trace version")
    positions = {}
    steady = []
    for epoch, phase in enumerate(trace.phase_ids):
        positions[phase] = positions.get(phase, 0) + 1
        if positions[phase] > 2:
            steady.append(epoch)
    if any(count != 6 for count in positions.values()):
        raise ValueError("scientific phases require six epochs")
    return steady


def analysis_denominator(trace: TraceArtifact) -> str:
    """Name the denominator without changing interpretation of legacy evidence."""
    analysis_epochs(trace)
    return (
        "planned-edge-originals-all-nine-synchronized-epochs"
        if trace.schema_version == 2
        else "planned-edge-originals-last-four-epochs-per-phase"
    )


def edge_outcomes(evidence: RunEvidence, trace: TraceArtifact) -> dict[str, int]:
    """Count physical outcomes against all declared planned edge originals."""
    steady = set(analysis_epochs(trace))
    planned = {
        (epoch, node, name): round(rate * trace.period_seconds)
        for epoch, window in enumerate(trace.windows)
        for node, rates in window.items()
        for name, rate in rates.items()
    }
    cells = cast(list[dict[str, Any]], evidence.cells)
    actual = {(cell["epoch"], cell["origin"], cell["function"]): cell for cell in cells}
    if len(actual) != len(cells) or set(actual) != set(planned):
        raise ValueError("planned original cells are missing or duplicated")
    if any(
        sum(cell[field] for cell in cells) != getattr(evidence, field)
        for field in ("planned", "completed", "errors")
    ):
        raise ValueError("planned cell totals disagree with global conservation totals")
    if trace.node_roles is None:
        raise ValueError("scientific trace requires origin roles")
    cloud = {node for node, role in trace.node_roles.items() if role == "cloud-direct"}
    result = {"planned": 0, "completed": 0, "cloud": 0, "errors": 0}
    for key, count in planned.items():
        cell = actual[key]
        if cell["planned"] != count or cell["completed"] + cell["errors"] != count:
            raise ValueError("planned originals disagree with accounted outcomes")
        destinations = cell["destinations"]
        if (
            sum(destinations.values()) != cell["completed"]
            or set(destinations) - set(trace.nodes)
            or any(
                not isinstance(v, int) or isinstance(v, bool) or v < 0
                for v in destinations.values()
            )
        ):
            raise ValueError("planned physical destination counts are inconsistent")
        if key[0] not in steady or key[1] in cloud:
            continue
        for field in ("planned", "completed", "errors"):
            result[field] += cell[field]
        result["cloud"] += sum(destinations.get(node, 0) for node in cloud)
    if not result["planned"]:
        raise ValueError("planned steady edge denominator is empty")
    return result


def _paired_block(block: dict) -> dict | None:
    runs = block.get("runs", {})
    if set(runs) != set(MODES):
        return None
    scope = None
    outcomes = {}
    for mode in MODES:
        run = runs[mode]
        manifest = CampaignManifest.model_validate(run["manifest"])
        evidence = RunEvidence.model_validate(run["evidence"])
        trace = TraceArtifact.model_validate(run["trace"])
        if manifest.mode != mode or manifest.trace_sha256 != trace.sha256:
            raise ValueError("trace/mode differs from frozen manifest")
        current = _scope(manifest)
        if scope is not None and scope != current:
            raise ValueError("paired modes have different frozen scope")
        scope = current
        campaign = cast(dict[str, Any], manifest.parameters).get("campaign", {})
        if any(
            campaign.get(k) != block[k]
            for k in ("block", "role", "plannedBlocks", "protocolSha256")
        ):
            raise ValueError("block differs from frozen campaign scope")
        if not (
            evidence.valid
            and evidence.planned > 0
            and evidence.emitted == evidence.planned
            and evidence.completed + evidence.errors == evidence.planned
            and not evidence.censored
            and not evidence.generator_deficit
            and evidence.observations.get("loadExecuted") is not False
        ):
            return None
        outcomes[mode] = edge_outcomes(evidence, trace)
    if len({row["planned"] for row in outcomes.values()}) != 1:
        raise ValueError("paired modes disagree on planned edge denominator")
    baseline = outcomes["baseline"]
    denominator = baseline["planned"]
    return {
        "block": block["block"],
        "scope": scope,
        "outcomes": outcomes,
        "primary": (outcomes["ewma"]["cloud"] - baseline["cloud"]) / denominator,
        "completion": (outcomes["ewma"]["completed"] - baseline["completed"])
        / denominator,
        "oracle": (outcomes["oracle"]["cloud"] - baseline["cloud"]) / denominator,
    }


def paired_summary(
    blocks: Sequence[dict],
    *,
    seed: int,
    draws: int = 10000,
    protocol: dict | None = None,
) -> dict:
    """Resample whole paired blocks; unavailable blocks never become zero outcomes."""
    if draws < 1:
        raise ValueError("positive bootstrap draw count required")
    main = [block for block in blocks if block["role"] == "main"]
    pilot = [block for block in blocks if block["role"] == "pilot"]
    seen = [(block["role"], block["block"]) for block in blocks]
    if len(seen) != len(set(seen)):
        raise ValueError("duplicate campaign block")
    plans = {(block["plannedBlocks"], block["protocolSha256"]) for block in main}
    if len(plans) != 1:
        raise ValueError("main blocks disagree on frozen scope/sample size")
    planned, _protocol = plans.pop()
    if planned < 12 or planned > 36 or planned % 6:
        raise ValueError("main planned block count must be balanced and within budget")
    observed = {block["block"] for block in main}
    replacements = {b["block"]: b for b in protocol["replacements"]} if protocol else {}
    if protocol and (
        protocol["plannedBlocks"] != planned
        or len(replacements) > math.floor(0.2 * planned)
    ):
        raise ValueError("replacement registry differs from frozen budget")
    if observed - (set(range(planned)) | set(replacements)):
        raise ValueError("block outside frozen planned registry")
    pairs, excluded, replaced = [], [], []
    originals = {b["block"]: b for b in main if b["block"] < planned}
    replaced_ids = set()
    for block in sorted(main, key=lambda b: b["block"]):
        pair = _paired_block(block)
        if block["block"] >= planned:
            original_id = block.get("replacementFor")
            original = originals.get(original_id)
            descriptor = replacements[block["block"]]
            if original_id in replaced_ids or original is None:
                raise ValueError("replacement requires one retained original failure")
            if any(
                run["manifest"]["seed"] != descriptor["seed"]
                or run["manifest"]["modes"] != descriptor["modes"]
                for run in block.get("runs", {}).values()
            ):
                raise ValueError("replacement differs from frozen seed/order")
            if not (
                original.get("invalidity") == "external-technical"
                and original.get("failureEvidence")
                and _paired_block(original) is None
            ):
                pair = None
            elif pair is not None:
                replaced_ids.add(original_id)
                replaced.append(
                    {"original": original_id, "replacement": block["block"]}
                )
        if pair is None:
            excluded.append(
                {
                    "block": block["block"],
                    "reason": block.get("invalidity", "missing-or-nonconforming-mode"),
                    "artifacts": block.get("artifacts"),
                    "failureEvidence": block.get("failureEvidence"),
                }
            )
        else:
            pairs.append(pair)
    if len({pair["scope"] for pair in pairs}) > 1:
        raise ValueError("main blocks have different frozen scope")
    complete = (
        len(pairs) == planned
        and set(originals) == set(range(planned))
        and all(
            item["block"] in replaced_ids
            for item in excluded
            if item["block"] < planned
        )
    )
    samples = []
    if complete:
        rng = random.Random(seed)  # nosec B311: frozen statistical bootstrap
        samples = [
            [pairs[i] for i in rng.choices(range(len(pairs)), k=len(pairs))]
            for _ in range(draws)
        ]

    def metric(key):
        values = [pair[key] for pair in pairs]
        means = [statistics.fmean(pair[key] for pair in sample) for sample in samples]
        return {
            "estimate": statistics.fmean(values) if values else None,
            "confidence95": [quantile(means, 0.025), quantile(means, 0.975)]
            if means
            else None,
            "blockCount": len(values),
        }

    primary, completion, oracle = (
        metric("primary"),
        metric("completion"),
        metric("oracle"),
    )
    traces = [
        TraceArtifact.model_validate(run["trace"])
        for block in main
        for run in block.get("runs", {}).values()
    ]
    policies = {
        (analysis_denominator(trace), tuple(analysis_epochs(trace))) for trace in traces
    }
    if len(policies) != 1:
        raise ValueError("main blocks disagree on analysis scope")
    denominator, epochs = policies.pop()
    primary["denominator"] = denominator
    completion.update(
        threshold=-0.02,
        passed=bool(complete and completion["confidence95"][0] >= -0.02),
    )
    return {
        "complete": complete,
        "analysisEpochIndices": list(epochs),
        "confirmatory": False,
        "inference": "conditional-on-independent-blocks"
        if complete
        else "inconclusive",
        "primary": primary,
        "completionGuard": completion,
        "secondaryOracle": oracle,
        "pilotBlockCount": len(pilot),
        "plannedBlocks": planned,
        "missingBlocks": sorted(set(range(planned)) - observed),
        "excludedBlocks": excluded,
        "blocks": pairs,
        "replacedBlocks": replaced,
        "serialDiagnostics": {
            "blockOrder": [pair["block"] for pair in pairs],
            "primarySequence": [pair["primary"] for pair in pairs],
            "adjacentProductMean": statistics.fmean(
                (a["primary"] - primary["estimate"])
                * (b["primary"] - primary["estimate"])
                for a, b in pairwise(pairs)
            )
            if len(pairs) > 1
            else None,
            "interpretation": (
                "descriptive only; lack of serial signal does not prove independence"
            ),
        },
        "bootstrap": {
            "unit": "paired-whole-block",
            "seed": seed,
            "draws": draws,
            "method": "percentile-95",
        },
        "independence": (
            "fresh VMs alone do not establish block independence; "
            "inspect time order and drift"
        ),
    }


def freeze_campaign_protocol(*, profiles, qualification, pilot_rows, settings) -> dict:
    """Freeze pilot order or main N only after immutable prerequisite verification."""
    from nanolab.one_shot.profiles import read_profiles, validate_trace_capacity
    from nanolab.tasks.one_shot.experiment import (
        read_qualification,
        verify_qualified_load,
    )
    from nanolab.tasks.one_shot.trace import capacity_trace

    if (
        settings.purpose != "scientific-experiment"
        or settings.profile is None
        or settings.qualification is None
    ):
        raise ValueError("scientific profile and qualification are required")
    verified = read_profiles(settings)
    timing = read_qualification(settings)
    if (
        verified.sha256 != profiles.sha256
        or timing != qualification
        or verified.environment_fingerprint != timing.environment_fingerprint
    ):
        raise ValueError("profile/timing frozen scope mismatch")
    if (
        timing.minimum_samples < 50
        or timing.quantile < 0.95
        or timing.epsilon > 0.05
        or any(
            TimingQualification.model_validate(cell["result"]).sample_count < 50
            for cell in timing.matrix
        )
    ):
        raise ValueError(
            "scientific timing requires fifty complete tail samples per cell"
        )
    if verified.concurrent_capacities is not None:
        from nanolab.tasks.one_shot.trace import concurrent_trace

        names = {
            str(row["function"])
            for row in next(iter(verified.by_node.values())).functions
        }
        aliases = {
            key: key if key in names else key.removesuffix("-rust")
            for key in settings.functions
        }
        trace = concurrent_trace(
            verified.concurrent_capacities,
            settings,
            period=timing.period_seconds,
            seed=settings.seed,
            aliases=aliases,
        )
    else:
        if verified.capacities is None or len(settings.functions) != 1:
            raise ValueError("measured single-function capacities required")
        original = next(iter(settings.functions))
        name = cast(
            str, verified.by_node[next(iter(verified.by_node))].functions[0]["function"]
        )
        relative = capacity_trace(
            verified.capacities, settings, timing.period_seconds, settings.seed
        )

        def canonical_window(window):
            return {node: {name: rates[original]} for node, rates in window.items()}

        if relative.requested_windows is None:
            raise ValueError("capacity trace requires requested rates")
        trace = TraceArtifact.model_validate(
            {
                **relative.model_dump(),
                "functions": [name],
                "payloads": {name: relative.payloads[original]},
                "windows": [canonical_window(window) for window in relative.windows],
                "requested_windows": [
                    canonical_window(window) for window in relative.requested_windows
                ],
            }
        )
        aliases = {original: name}
    verify_qualified_load(trace, timing, aliases=aliases)
    validate_trace_capacity(trace, verified, settings)
    role, n, pilot = "pilot", 6, None
    if pilot_rows:
        if (
            len(pilot_rows) != 6
            or {row["block"] for row in pilot_rows} != set(range(6))
            or any(row["role"] != "pilot" for row in pilot_rows)
        ):
            raise ValueError("main freeze requires six complete pilot triplets")
        pairs = [
            _paired_block(row)
            for row in sorted(pilot_rows, key=lambda row: row["block"])
        ]
        if any(pair is None for pair in pairs):
            raise ValueError("main freeze requires complete conforming pilot triplets")
        registered = freeze_campaign_protocol(
            profiles=profiles,
            qualification=qualification,
            pilot_rows=[],
            settings=settings,
        )
        registered_sha = content_hash(canonical_bytes(registered))
        for row in pilot_rows:
            descriptor = registered["blocks"][row["block"]]
            if row["protocolSha256"] != registered_sha:
                raise ValueError("pilot differs from registered protocol")
            for run in row["runs"].values():
                manifest = CampaignManifest.model_validate(run["manifest"])
                if (
                    manifest.seed != descriptor["seed"]
                    or manifest.modes != descriptor["modes"]
                ):
                    raise ValueError(
                        "pilot seed/order differs from registered protocol"
                    )
                if (
                    manifest.profile_sha256 != verified.sha256
                    or manifest.qualification_sha256 != settings.qualification.sha256
                    or manifest.environment_fingerprint
                    != verified.environment_fingerprint
                    or manifest.trace_sha256 != trace.sha256
                ):
                    raise ValueError("pilot prerequisite/trace scope mismatch")
        differences = [pair["primary"] for pair in pairs if pair is not None]
        n = plan_repetitions(differences)
        role = "main"
        pilot = {
            "blockCount": 6,
            "differences": differences,
            "sampleStandardDeviation": statistics.stdev(differences),
            "planningApproximation": (
                "normal-paired-5pp-80-percent-power; six-block uncertainty remains"
            ),
            "registrySha256": content_hash(canonical_bytes(pilot_rows)),
        }
    order = list(permutations(MODES))
    random.Random(settings.seed).shuffle(order)  # nosec B311: frozen balanced mode order
    blocks = [
        {"block": i, "seed": settings.seed + i, "modes": list(order[i % 6])}
        for i in range(n)
    ]
    replacements = (
        [
            {"block": n + i, "seed": settings.seed + n + i, "modes": list(order[i % 6])}
            for i in range(math.floor(0.20 * n))
        ]
        if role == "main"
        else []
    )
    frozen_settings = settings.model_dump(mode="json", by_alias=True, exclude_none=True)
    frozen_settings.pop("experiment", None)
    if settings.runtime_distribution is not None:
        settings.runtime_distribution.read_verified()
    return {
        "schemaVersion": 2 if trace.schema_version == 2 else 1,
        "artifactType": "paired-one-shot-campaign",
        "nanolabPackageSha256": package_fingerprint(),
        "role": role,
        "seed": settings.seed,
        "plannedBlocks": n,
        "blocks": blocks,
        "replacements": replacements,
        "settings": frozen_settings,
        "trace": trace.model_dump(),
        "traceSha256": trace.sha256,
        "profileSha256": verified.sha256,
        "qualificationSha256": settings.qualification.sha256,
        "environmentFingerprint": verified.environment_fingerprint,
        "pilot": pilot,
        "analysis": {
            "primary": "ewma-minus-baseline-edge-cloud-completion-share",
            "denominator": analysis_denominator(trace),
            **(
                {
                    "vmTelemetry": telemetry_policy(),
                    "analysisEpochIndices": analysis_epochs(trace),
                    "functionAliases": aliases,
                }
                if trace.schema_version == 2
                else {}
            ),
            "completionGuard": -0.02,
            "completionGuardBasis": "lower-95-percentile-bound",
            "bootstrapDraws": 10000,
            "bootstrapSeed": settings.seed,
            "unit": "paired-whole-block",
            "secondary": "oracle-minus-baseline; oracle is not an upper bound",
            "replacementLimit": math.floor(0.20 * n),
            "replacementAllowed": "identified-external-technical-only",
            "independence": (
                "inspect time order, host contention, service drift "
                "and serial contrasts; fresh VMs do not prove independence"
            ),
        },
    }


def write_campaign_scenarios(protocol: dict, output) -> list:
    """Emit immutable standard public scenarios, including preset replacement seeds."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    content = canonical_bytes(protocol)
    reference = output / "protocol.json"
    write_immutable_artifact(reference, content)
    trace = TraceArtifact.model_validate(protocol["trace"])
    settings = protocol["settings"]
    if trace.phase_ids is None:
        raise ValueError("campaign trace requires phase identifiers")
    if protocol["schemaVersion"] == 2:
        aliases = protocol["analysis"]["functionAliases"]
        step = 1
    else:
        aliases = {next(iter(settings["functions"])): trace.functions[0]}
        step = 6
    if len(set(aliases.values())) != len(aliases) or set(aliases.values()) != set(
        trace.functions
    ):
        raise ValueError("campaign function aliases must be biunivocal")
    windows = [
        {
            "id": trace.phase_ids[i],
            "epochs": step,
            "network": "observed-local",
            "rates": {
                node: {key: rates[name] for key, name in aliases.items()}
                for node, rates in trace.windows[i].items()
            },
        }
        for i in range(0, len(trace.windows), step)
    ]
    paths = []
    for block in protocol["blocks"] + protocol["replacements"]:
        node_settings = copy.deepcopy(settings)
        node_settings["seed"] = block["seed"]
        node_settings["experiment"] = {
            "windows": windows,
            "repetitions": 1,
            "warmupSeconds": math.ceil(2 * trace.period_seconds),
            "generatorVus": 512,
            "maxArrivalLatenessSeconds": 0.25,
            "campaignProtocol": {
                "path": str(reference.resolve()),
                "sha256": content_hash(content),
            },
            "campaignBlock": block["block"],
        }
        scenario = {
            "workflow": "one-shot-experiment",
            "backend": "container",
            "functions": list(settings["functions"]),
            "oneShot": node_settings,
        }
        ScenarioConfig.model_validate(scenario)
        path = output / f"block-{block['block']:03d}.yaml"
        write_immutable_artifact(
            path, yaml.safe_dump(scenario, sort_keys=False).encode()
        )
        paths.append(path)
    return paths


def read_campaign_binding(settings) -> dict | None:
    """Reject scenario changes before any owned VM or generator can be acquired."""
    from nanolab.one_shot.models import CampaignProtocol
    from nanolab.tasks.one_shot.trace import experiment_trace

    experiment = settings.experiment
    if experiment is None or experiment.campaign_protocol is None:
        return None
    reference = experiment.campaign_protocol
    protocol = CampaignProtocol.model_validate(json.loads(reference.read_verified()))
    if protocol.nanolab_package_sha256 != package_fingerprint():
        raise ValueError("installed implementation differs from frozen campaign")
    frozen = OneShotConfig.model_validate(protocol.settings)
    descriptor = next(
        (
            b
            for b in protocol.blocks + protocol.replacements
            if b.block == experiment.campaign_block
        ),
        None,
    )
    if descriptor is None or settings.seed != descriptor.seed:
        raise ValueError("scenario seed/block differs from frozen campaign")
    current = settings.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude={"seed", "experiment"}
    )
    expected = frozen.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude={"seed", "experiment"}
    )
    if current != expected:
        raise ValueError("scenario settings differ from frozen campaign scope")
    trace = TraceArtifact.model_validate(protocol.trace)
    aliases = (
        cast(dict[str, str], protocol.analysis["functionAliases"])
        if protocol.schema_version == 2
        else {next(iter(settings.functions)): trace.functions[0]}
    )
    declared = experiment_trace(settings, period=trace.period_seconds, aliases=aliases)
    if (
        declared.windows != trace.windows
        or declared.phase_ids != trace.phase_ids
        or experiment.warmup_seconds != math.ceil(2 * trace.period_seconds)
        or experiment.generator_vus != 512
        or experiment.max_arrival_lateness != 0.25
    ):
        raise ValueError("scenario load/generator differs from frozen campaign")
    return {
        "protocol": protocol.model_dump(by_alias=True),
        "block": descriptor.model_dump(),
        "trace": trace,
        "sha256": reference.sha256,
    }
