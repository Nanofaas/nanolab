#!/usr/bin/env python3
"""Measure a finished observation's forecast errors per interval, node and function."""

import argparse
import json
from pathlib import Path

from nanolab.tasks.one_shot.forecast_errors import record_forecast_errors


def main(argv=None):
    """Read a run or archive directory; write only into a new output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="run or archive with manifest.json")
    parser.add_argument(
        "--output", type=Path, required=True, help="new directory for the results"
    )
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    summary = record_forecast_errors(args.run, args.output)
    print(
        json.dumps(
            {"output": str(args.output), "overall": summary["overall"]}, indent=2
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
