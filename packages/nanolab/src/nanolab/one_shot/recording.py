"""Persist campaign evidence without rendering or operational decisions."""

import json
import os
import tempfile
from pathlib import Path


class CampaignRecorder:
    """Write campaign artifacts; callers serialize append-only event streams."""

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
        descriptor, name = tempfile.mkstemp(
            prefix=".status-", suffix=".json.tmp", dir=self.output
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_text(json.dumps(snapshot, indent=2) + "\n")
            temporary.replace(self.output / "status.json")
        finally:
            temporary.unlink(missing_ok=True)

    def write_result(self, snapshot: dict) -> None:
        """Write a terminal receipt independently of status persistence."""
        (self.output / "result.json").write_text(json.dumps(snapshot, indent=2) + "\n")
