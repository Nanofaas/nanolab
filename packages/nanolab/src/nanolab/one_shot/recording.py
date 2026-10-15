"""Persist campaign evidence without rendering or operational decisions."""

import json
from pathlib import Path


class CampaignRecorder:
    """Write the existing campaign artifact formats; callers serialize access."""

    def __init__(self, output: Path):
        """Use an already owned output directory."""
        self.output = output

    def append_event(self, row: dict) -> None:
        """Append one complete supervision event."""
        with (self.output / "supervision.jsonl").open("a") as stream:
            stream.write(json.dumps(row, default=str) + "\n")

    def append_progress(self, row: dict) -> None:
        """Append one observation stage without changing its timestamp."""
        with (self.output / "progress.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    def write_status(self, snapshot: dict) -> None:
        """Replace the current status atomically."""
        temporary = self.output / "status.json.tmp"
        temporary.write_text(json.dumps(snapshot, indent=2) + "\n")
        temporary.replace(self.output / "status.json")

    def write_result(self, snapshot: dict) -> None:
        """Write a terminal receipt independently of status persistence."""
        (self.output / "result.json").write_text(json.dumps(snapshot, indent=2) + "\n")
