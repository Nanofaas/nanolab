"""Small module-cost catalogue and preparation; normal NanoLab owns execution."""

import argparse
import random
import shlex
from pathlib import Path
from uuid import uuid4

import yaml

from nanolab.comparison.cells import identity_sha256, write_json_atomically
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.module_cost import ARMS, BASE


def module_cost_cells(block: str) -> dict[str, dict]:
    """Return exact recipe inputs; only the first recipe builds the function."""
    cells = {}
    for key in ARMS:
        runtime, composition = key.split("-")
        modules: list[str] = list(BASE)
        if composition in ("c", "cs"):
            modules.append("concurrency-control")
        if composition in ("s", "cs"):
            modules.append("autoscaler")
        control = {
            "modules": modules,
            "build": {"mode": runtime},
            "container": {"image": key},
            "config": {"nanofaas": {"metrics": {"profile": "advanced"}}},
        }
        if runtime == "jvm":
            control["jvm"] = {"args": ["-XX:+UseSerialGC", "-Xmx64m"]}
        else:
            control["build"].update(
                builder="container",
                native={"optimization": "3", "gc": "serial", "maxHeap": "64m"},
            )
        recipe = {
            "schemaVersion": 2,
            "name": f"module-cost-{key}",
            "registry": {"repository": "127.0.0.1:5000/nanofaas", "tag": block},
            "controlPlane": control,
        }
        if key == ARMS[0]:
            recipe["functions"] = [
                {
                    "name": "word-stats",
                    "sdk": "java",
                    "build": {"mode": "jvm"},
                    "container": {"image": "word-stats-java"},
                }
            ]
        cells[key] = recipe
    return cells


def prepare_campaign(output: Path, ssh_key: Path, seed: int) -> Path:
    """Emit three independent blocks; never authenticate or allocate."""
    if not output.is_absolute() or not ssh_key.is_absolute():
        raise ValueError("output and SSH public key paths must be absolute")
    token = "mc-" + uuid4().hex[:12]
    root = output / token
    order = list(ARMS)
    random.Random(seed).shuffle(order)  # nosec B311: reproducible experimental order
    blocks, commands = [], []
    root.mkdir(parents=True, exist_ok=False)
    for repeat in range(3):
        block = f"{token}-{repeat + 1}"
        block_order = (
            order
            if repeat == 0
            else list(reversed(order))
            if repeat == 1
            else order[4:] + order[:4]
        )
        directory = root / block
        directory.mkdir()
        spec = {
            "workflow": "module-cost",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "moduleCost": {"blockId": block, "order": block_order},
        }
        env = {
            "provider": "azure",
            "roles": {
                "stack": {
                    "name": block + "-stack",
                    "disk": "128G",
                    "user": "azureuser",
                },
                "loadgen": {
                    "name": block + "-loadgen",
                    "disk": "30G",
                    "user": "azureuser",
                },
            },
            "azure": {
                "resource_group": "maurinoRicerca-rg",
                "location": "westeurope",
                "vm_size": "Standard_D8s_v5",
                "loadgen_vm_size": "Standard_D8s_v5",
                "priority": "Spot",
                "eviction_policy": "Delete",
                "max_bid_price": -1,
                "image_urn": "Canonical:ubuntu-24_04-lts:server:24.04.202607140",
                "operator_source_cidr": "auto",
                "ssh_key_path": str(ssh_key),
            },
        }
        ScenarioConfig.model_validate(spec)
        EnvironmentConfig.model_validate(env)
        for name, value in [("scenario", spec), ("environment", env)]:
            (directory / f"{name}.yaml").write_text(
                yaml.safe_dump(value, sort_keys=False)
            )
        recipes = module_cost_cells(block)
        (directory / "recipes").mkdir()
        for name, value in recipes.items():
            (directory / "recipes" / f"{name}.yaml").write_text(
                yaml.safe_dump(value, sort_keys=False)
            )
        write_json_atomically(
            directory / "inputs.json",
            {
                "identity": identity_sha256(
                    {"scenario": spec, "environment": env, "recipes": recipes}
                ),
                "recipes": recipes,
            },
        )
        commands.append(
            shlex.join(
                [
                    "nanolab",
                    "run",
                    str(directory / "scenario.yaml"),
                    "--environment",
                    str(directory / "environment.yaml"),
                    "--run-dir",
                    str(directory / "results"),
                ]
            )
        )
        blocks.append(block)
    write_json_atomically(
        root / "manifest.json",
        {"schemaVersion": 1, "seed": seed, "blocks": blocks, "commands": commands},
    )
    (root / "commands.sh").write_text(
        "#!/bin/sh\nset -eu\n" + "\n".join(commands) + "\n"
    )
    return root


def main() -> None:
    """Prepare inputs under /data by default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("/data/module-cost"))
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261101)
    args = parser.parse_args()
    print(prepare_campaign(args.output_root, args.ssh_key, args.seed))


if __name__ == "__main__":
    main()
