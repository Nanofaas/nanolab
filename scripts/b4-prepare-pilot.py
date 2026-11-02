#!/usr/bin/env python3
"""Prepare matched B4 pilot arms; the normal nanolab workflow owns all VM lifecycles."""

import argparse
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.environment import require_experiment_environment


def prepare(
    output_root: Path, ssh_key: Path, *, rates: tuple[int, ...] = (20, 100, 300)
) -> Path:
    """Write unique, validated inputs and commands without accessing cloud services."""
    if not output_root.is_absolute() or not ssh_key.is_absolute():
        raise ValueError("output root and controller SSH key path must be absolute")
    if (
        not rates
        or any(type(rate) is not int or rate <= 0 for rate in rates)
        or len(set(rates)) != len(rates)
    ):
        raise ValueError("rates must be distinct positive integers")
    token = uuid4().hex[:10]
    created = datetime.now(UTC)
    root = output_root.resolve() / f"pilot-{created:%Y%m%dT%H%M%SZ}-{token}"
    arms = []
    configurations = []
    for rate_index, rate in enumerate(rates):
        modes = ("disabled", "pressure", "cloud")
        if rate_index % 2:
            modes = ("pressure", "disabled", "cloud")
        for mode in modes:
            number = len(arms) + 1
            identity = f"b4-{token}-{number:02}"
            directory = root / f"{number:02}-{mode}-r{rate}"
            scenario = {
                "workflow": "offload-loadtest",
                "backend": "k8s",
                "build": "docker",
                "functions": ["word-stats-java"],
                "resources": {
                    "control-plane": {
                        "requests": {"cpu": 0.25, "memoryMiB": 512},
                        "limits": {"cpu": 2, "memoryMiB": 2048},
                    },
                    "word-stats-java": {
                        "requests": {"cpu": 1, "memoryMiB": 256},
                        "limits": {"cpu": 2, "memoryMiB": 512},
                    },
                },
                "offloadExperiment": {
                    "mode": mode,
                    "rate": rate,
                    "durationSeconds": 180,
                    "warmupSeconds": 60,
                    "vus": 512,
                },
            }
            environment = {
                "provider": "multipass",
                "roles": {
                    "stack": {
                        "name": identity + "-edge",
                        "cpus": 4,
                        "memory": "12G",
                        "disk": "64G",
                    },
                    "loadgen": {
                        "name": identity + "-loadgen",
                        "cpus": 2,
                        "memory": "4G",
                        "disk": "30G",
                    },
                    "cloud": {
                        "name": identity + "-cloud",
                        "provider": "azure",
                        "user": "azureuser",
                        "disk": "64G",
                    },
                },
                "azure": {
                    "resource_group": "maurinoRicerca-rg",
                    "location": "westeurope",
                    "vm_size": "Standard_D8s_v5",
                    "priority": "Spot",
                    "eviction_policy": "Delete",
                    "max_bid_price": -1,
                    "image_urn": "Canonical:ubuntu-24_04-lts:server:24.04.202607140",
                    "operator_source_cidr": "auto",
                    "ssh_key_path": str(ssh_key),
                },
            }
            ScenarioConfig.model_validate(scenario)
            require_experiment_environment(
                EnvironmentConfig.model_validate(environment)
            )
            scenario_path = directory / "scenario.yaml"
            environment_path = directory / "environment.yaml"
            run_dir = directory / "results"
            command = shlex.join(
                [
                    "nanolab",
                    "run",
                    str(scenario_path),
                    "--environment",
                    str(environment_path),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            arms.append(
                {
                    "id": identity,
                    "mode": mode,
                    "rate": rate,
                    "purpose": "cloud-capacity-diagnostic"
                    if mode == "cloud"
                    else "edge-comparison",
                    "scenario": str(scenario_path),
                    "environment": str(environment_path),
                    "runDir": str(run_dir),
                    "command": command,
                }
            )
            configurations.append((directory, scenario, environment))
    root.mkdir(parents=True, exist_ok=False)
    for directory, scenario, environment in configurations:
        directory.mkdir()
        for name, data in (("scenario", scenario), ("environment", environment)):
            with (directory / f"{name}.yaml").open("x") as stream:
                yaml.safe_dump(data, stream, sort_keys=False)
    with (root / "manifest.json").open("x") as stream:
        json.dump(
            {
                "schemaVersion": 1,
                "kind": "b4-pilot",
                "createdAtUtc": created.isoformat(),
                "ratesProvisional": True,
                "controller": "DGX Spark",
                "input": "word-stats-java/performance-medium.json",
                "arms": arms,
            },
            stream,
            indent=2,
            allow_nan=False,
        )
        stream.write("\n")
    with (root / "commands.sh").open("x") as stream:
        stream.write(
            "#!/bin/sh\nset -eu\n# Run on the DGX controller; stop at the first failed arm.\n"
        )
        stream.write("\n".join(arm["command"] for arm in arms) + "\n")
    return root


def main() -> None:
    """Print the prepared directory; never execute the generated commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("/data/nanofaas-b4"))
    parser.add_argument(
        "--ssh-key",
        type=Path,
        required=True,
        help="Absolute Azure SSH public-key path on the DGX controller",
    )
    parser.add_argument("--rates", type=int, nargs="+", default=[20, 100, 300])
    args = parser.parse_args()
    try:
        output = prepare(args.output_root, args.ssh_key, rates=tuple(args.rates))
    except ValueError as error:
        parser.error(str(error))
    print(output)


if __name__ == "__main__":
    main()
