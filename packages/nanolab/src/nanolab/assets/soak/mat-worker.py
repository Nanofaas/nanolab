"""Pinned container worker: runs the eight fixed MAT report invocations.

Exactly four positional arguments: baseline HPROF, final HPROF, an owned
read-write output directory, and the MAT ParseHeapDump.sh launcher. Inputs are
copied into a bounded work directory (never mutated in place), the eight
reports are generated with the launcher's own naming, only ZIP/TXT/log
outputs are copied to the mounted result directory, and the work copies are
always removed, even on failure.
"""

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - owned, fixed-argv MAT launcher invocation
import sys
import time
from pathlib import Path

REPORTS = (
    ("baseline", (), "org.eclipse.mat.api:overview"),
    ("baseline", (), "org.eclipse.mat.api:suspects"),
    ("baseline", (), "org.eclipse.mat.api:top_components"),
    ("final", (), "org.eclipse.mat.api:overview"),
    ("final", (), "org.eclipse.mat.api:suspects"),
    ("final", (), "org.eclipse.mat.api:top_components"),
    ("final", ("-snapshot2=baseline.hprof",), "org.eclipse.mat.api:compare"),
    ("final", ("-baseline=baseline.hprof",), "org.eclipse.mat.api:suspects2"),
)
OUTPUT_SUFFIXES = {".zip", ".txt", ".log"}
# The container always mounts a bounded tmpfs at /work; the override exists
# only so this script can be exercised by a real subprocess in tests.
WORK = Path(os.environ.get("NANOLAB_MAT_WORK_DIR", "/work"))


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    """Run the eight fixed MAT reports and always leave a worker receipt."""
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: mat-worker.py <baseline.hprof> <final.hprof> "
            "<output-dir> <ParseHeapDump.sh>"
        )
    baseline_src, final_src, output_dir, launcher = (Path(a) for a in sys.argv[1:5])
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    baseline = WORK / "baseline.hprof"
    final = WORK / "final.hprof"
    dumps = {"baseline": baseline, "final": final}
    invocations = []
    started = time.monotonic()
    try:
        shutil.copyfile(baseline_src, baseline)
        shutil.copyfile(final_src, final)
        for dump, extra, report_id in REPORTS:
            argv = [str(launcher), dumps[dump].name, *extra, report_id]
            before = {path.name for path in WORK.iterdir()}
            entry = {"dump": dump, "report_id": report_id, "argv": argv, "outputs": []}
            invocation_started = time.monotonic()
            try:
                result = subprocess.run(  # nosec B603 - fixed launcher/report argv
                    argv, cwd=WORK, check=True, capture_output=True, text=True
                )
                entry["returncode"] = result.returncode
                entry["stderr_tail"] = result.stderr[-2048:]
            except subprocess.CalledProcessError as error:
                entry["returncode"] = error.returncode
                entry["stderr_tail"] = (error.stderr or "")[-2048:]
                invocations.append(entry)
                raise
            finally:
                entry["duration_s"] = time.monotonic() - invocation_started
            produced = sorted(
                path
                for path in WORK.iterdir()
                if path.name not in before and path.suffix.lower() in OUTPUT_SUFFIXES
            )
            for path in produced:
                destination = reports_dir / path.name
                shutil.move(str(path), destination)
                entry["outputs"].append(
                    {
                        "name": destination.name,
                        "sha256": _hash(destination),
                        "size_bytes": destination.stat().st_size,
                    }
                )
            invocations.append(entry)
    finally:
        for path in (baseline, final):
            path.unlink(missing_ok=True)
        for leftover in WORK.iterdir():
            if leftover.is_file():
                leftover.unlink()
        receipt = {
            "schema": "nanolab-heap-analysis-mat-worker-v1",
            "invocations": invocations,
            "duration_s": time.monotonic() - started,
        }
        (output_dir / "mat-worker-receipt.json").write_text(json.dumps(receipt))


if __name__ == "__main__":
    main()
