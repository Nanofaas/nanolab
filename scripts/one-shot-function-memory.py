#!/usr/bin/env python3
"""Apply the agreed function RAM sizes while preserving initial replica counts.

Write a new scenario and provenance receipt; never provision VMs or overwrite
an existing scenario. CPU quotas and workload inputs remain user-configured.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path

import yaml
from nanolab.config.one_shot import resolve_node_functions
from nanolab.config.scenario import ScenarioConfig
from nanolab.one_shot.capacity import comparison_replica_vectors
from nanolab.tasks.one_shot.artifacts import package_fingerprint

MEMORY_MIB = {
    "one-shot-arithmetic-rust": 64,
    "one-shot-sort-rust": 128,
    "one-shot-memory-rust": 256,
    "one-shot-matrix-rust": 128,
    "one-shot-mixed-rust": 256,
}


def configure(document):
    """Resize RAM pools to preserve the existing balanced initial allocation."""
    scenario = ScenarioConfig.model_validate(document)
    settings = scenario.one_shot
    if settings is None or not settings.strict_memory:
        raise ValueError("strict-memory one-shot scenario required")
    if set(settings.functions) != set(MEMORY_MIB):
        raise ValueError("scenario must contain the five agreed Rust functions")
    initial = comparison_replica_vectors(settings, {name: name for name in MEMORY_MIB})
    updated = copy.deepcopy(document)
    for node in updated["oneShot"]["nodes"]:
        functions = resolve_node_functions(settings, node["id"])
        pool = sum(
            initial[node["id"]][name] * size for name, size in MEMORY_MIB.items()
        )
        node["memoryCapacityMiB"] = pool
        node["functionLimits"] = {
            name: {
                "cpu": functions[name].cpu,
                "memoryMiB": MEMORY_MIB[name],
                "maxReplicas": pool // MEMORY_MIB[name],
            }
            for name in settings.functions
        }
    minimum_pool = min(
        node["memoryCapacityMiB"] for node in updated["oneShot"]["nodes"]
    )
    for name, fn in updated["oneShot"]["functions"].items():
        fn["memoryMiB"] = MEMORY_MIB[name]
        fn["maxReplicas"] = minimum_pool // MEMORY_MIB[name]
    ScenarioConfig.model_validate(updated)
    return updated, initial


def main():
    """Validate and persist a prospective configuration with source hashes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipt_path = args.output.with_suffix(".memory.json")
    if args.output.exists() or receipt_path.exists():
        raise FileExistsError("output scenario or memory receipt already exists")
    original = args.input.read_bytes()
    updated, initial = configure(yaml.safe_load(original))
    scenario_bytes = yaml.safe_dump(updated, sort_keys=False).encode()
    receipt = {
        "qualified": False,
        "loadStatus": "pending-python-tuning",
        "inputPath": str(args.input.resolve()),
        "inputSha256": hashlib.sha256(original).hexdigest(),
        "scenarioSha256": hashlib.sha256(scenario_bytes).hexdigest(),
        "scriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "nanolabPackageSha256": package_fingerprint(),
        "functionMemoryMiB": MEMORY_MIB,
        "initialReplicaVectors": initial,
        "measuredServiceConfiguration": (
            "historical 64-MiB observations; no new measurements"
        ),
        "nodes": updated["oneShot"]["nodes"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as destination:
        destination.write(scenario_bytes)
    with receipt_path.open("x") as destination:
        destination.write(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"scenario": str(args.output), "receipt": str(receipt_path)}))


if __name__ == "__main__":
    main()
