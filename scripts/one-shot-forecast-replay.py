#!/usr/bin/env python3
"""Replay a finished observation's arrivals through the NanoFaaS forecasters."""

import argparse
import json
from pathlib import Path

from nanolab.tasks.one_shot.forecast_replay import (
    parse_forecaster,
    record_forecast_replay,
)

DEFAULT_FORECASTERS = (
    "ewma:window=60,alpha=0.5",
    "ewma:window=10,alpha=0.7",
    "holt:window=10",
    "ari:window=10,order=6",
)


def main(argv=None):
    """Read a run or archive directory; write only into a new output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="run or archive with manifest.json")
    parser.add_argument(
        "--output", type=Path, required=True, help="new directory for the results"
    )
    parser.add_argument(
        "--forecaster",
        action="append",
        help="provider:key=value,... e.g. holt:window=10,trend=0.2 (repeatable; "
        "default: " + " ".join(DEFAULT_FORECASTERS) + ")",
    )
    parser.add_argument(
        "--lead-seconds",
        type=float,
        help="query each interval this long before it starts (default: the run's)",
    )
    args = parser.parse_args(argv)
    forecasters = [
        parse_forecaster(spec) for spec in args.forecaster or DEFAULT_FORECASTERS
    ]
    args.output.mkdir(parents=True, exist_ok=False)
    summary = record_forecast_replay(
        args.run, args.output, forecasters=forecasters, lead=args.lead_seconds
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "leadSeconds": summary["leadSeconds"],
                "meanAbsoluteError": {
                    label: model["overall"]["meanAbsoluteError"]
                    for label, model in summary["models"].items()
                },
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
