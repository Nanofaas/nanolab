#!/usr/bin/env python3
"""Prove the pinned diagnostic helper's readings through the production path.

The unit tests drive a fake helper and a fake `jcmd`. This drives what a run
drives -- `build_helper_image`, then `LocalDockerDiagnosticProvisioner.prepare`,
then `adapter.capture` -- against a real JDK as the container's init process, so
the gates, the worker's dispatch and the artifact transport are all the ones a
run uses. It is what found that `histogram` was advertised by the type system
while no run path could reach it, and that every Native Memory Tracking command
exits 0 on the answer that means no reading was taken.

It needs the operator preconditions `run` documents and does not start any of
them: a local container environment, a `docker buildx` builder whose driver
publishes the attestations the helper build requires, and the registry those
images are pushed to. Each is checked before anything is built.

Usage:
    scripts/validate-diagnostic-helper.py [--rm] [--dry-run]
                                          [--target-image IMAGE] [--builder NAME]

Exit status is 0 only when every case in the table below behaved as it says.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH = REPO_ROOT / "build" / "diagnostic-helper-validation"
RUN_ID = "helper-validation"
VOLUME_LABEL = "nanolab.diagnostic.tmp"
OWNER_LABEL = "nanolab.run"
CONTAINER = "nanolab-helper-validation-target"
VOLUME = "nanolab-helper-validation-tmp"
QUOTA = 16 * 1024 * 1024
# `_RECEIPT_BYTES` in diagnostics.py: the reservation each capture takes on top
# of its artifact budget, so the pool must carry one per capture.
_RECEIPT_BYTES = 65536
TIMEOUT_S = 180.0
JAVA = "/opt/java/openjdk/bin/java"

# A program that holds a heap and a few threads open and keeps the JVM alive: a
# reading needs a target that is running, not one that has finished.
PROGRAM = (
    "public class Target { public static void main(String[] a) throws Exception {"
    " byte[] held = new byte[8 * 1024 * 1024];"
    " for (int i = 0; i < held.length; i += 4096) held[i] = 1;"
    " java.util.concurrent.Executors.newFixedThreadPool(4).submit("
    " () -> { try { Thread.sleep(900000); } catch (Exception e) { } });"
    ' System.out.println("ready"); Thread.sleep(900000); } }'
)


@dataclass(frozen=True)
class Case:
    """One reading to take, and what its receipt and artifact must say."""

    checkpoint: str
    operation: str
    status: str
    contains: str


# `native_memory` needs `-XX:NativeMemoryTracking=summary` in the target's JVM.
# Without it the reading is refused rather than recorded, which is the last row:
# the diff is read after the baseline the same target's earlier checkpoint took.
FLAGGED = (
    Case("baseline", "native_memory_baseline", "PASS", "Baseline taken"),
    Case("drain", "native_memory_diff", "PASS", "Native Memory Tracking:"),
    Case("drain", "native_memory", "PASS", "Total: reserved="),
    Case("drain", "histogram", "PASS", "#instances"),
)
UNFLAGGED = (Case("drain", "native_memory", "INCONCLUSIVE", "not a complete reading"),)


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run one command, returning its result rather than raising."""
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one docker command against the local daemon."""
    return run(["docker", *args])


def checked(args: list[str], *, what: str) -> str:
    """Run a command that must succeed, or name the step that did not."""
    result = run(args)
    if result.returncode != 0:
        raise SystemExit(f"{what} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def preconditions(registry: str, builder: str) -> None:
    """Refuse to build anything without what the build and the run both need."""
    if shutil.which("docker") is None:
        raise SystemExit("docker not found on PATH")
    host = registry.split("/", 1)[0]
    try:
        with urllib.request.urlopen(f"http://{host}/v2/", timeout=5) as answer:
            if answer.status != 200:
                raise SystemExit(f"registry {host} answered {answer.status}")
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit(
            f"registry {host} is not answering: {error}\n"
            "A run needs it too, and does not start it: see docs/soak.md."
        ) from error
    inspected = docker("buildx", "inspect", builder)
    if inspected.returncode != 0:
        raise SystemExit(
            f"builder {builder} does not exist: {inspected.stderr.strip()}\n"
            "The helper build publishes attestations, so it needs a "
            "docker-container builder; create one before validating."
        )


def target_program(flagged: bool) -> str:
    """Stage the target program in the volume and hand the JVM its place.

    The published application images are distroless with a JRE and no shell, so
    the shipped default is the helper's own JDK. The program is written into the
    mounted volume because that image has no shell to stage it with at run time,
    and `exec` makes the JVM the container's init process, which is the identity
    the provisioner binds to.
    """
    flags = "-XX:+UseSerialGC -XX:TieredStopAtLevel=1"
    if flagged:
        flags = "-XX:NativeMemoryTracking=summary " + flags
    return (
        f"cat > /tmp/Target.java <<'JAVA'\n{PROGRAM}\nJAVA\n"
        f"exec {JAVA} {flags} /tmp/Target.java"
    )


def start_target(image: str, *, flagged: bool, uid: int) -> None:
    """Run a JVM as the container's init process, with the owned tmpfs at /tmp.

    The mount options are the exact shape the provisioner verifies: a local
    tmpfs, page-aligned, owned by the run's uid, sticky, and carrying both the
    run's label and its own.
    """
    if docker("image", "inspect", image).returncode != 0:
        raise SystemExit(f"target image {image} is not present locally")
    docker(
        "volume",
        "create",
        "--driver",
        "local",
        "--opt",
        "type=tmpfs",
        "--opt",
        "device=tmpfs",
        "--opt",
        f"o=size={QUOTA},uid={uid},gid={uid},mode=1777,noexec,nosuid,nodev",
        "--label",
        f"{OWNER_LABEL}={RUN_ID}",
        "--label",
        f"{VOLUME_LABEL}=true",
        VOLUME,
    )
    checked(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER,
            "--label",
            f"{OWNER_LABEL}={RUN_ID}",
            "--user",
            f"{uid}:{uid}",
            "-v",
            f"{VOLUME}:/tmp",  # nosec B108 - isolated container path
            "--entrypoint",
            "sh",
            image,
            "-c",
            target_program(flagged),
        ],
        what="starting the target JVM",
    )
    for _ in range(120):
        if "ready" in docker("logs", CONTAINER).stdout:
            return
        time.sleep(0.5)
    raise SystemExit(f"target JVM never started:\n{docker('logs', CONTAINER).stderr}")


def stop_target() -> None:
    """Remove the target and its volume, tolerating either being absent."""
    docker("rm", "-f", CONTAINER)
    docker("volume", "rm", "-f", VOLUME)


def natural_checkpoint(root: Path, target: Any, phase: str) -> Path:
    """Write the checkpoint receipt a capture requires before it will run.

    A soak writes this itself. It is the one precondition this script stands in
    for, built the way `runtime.py` builds it; nothing else is simulated.
    """
    samples = root / f"natural-samples-{phase}.json"
    samples.write_text(json.dumps({"schema": "nanolab-soak-v1", "rows": []}))
    body = samples.read_bytes()
    receipt = root / f"natural-{phase}.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "nanolab-soak-v1",
                "kind": "natural_checkpoint",
                "phase": phase,
                "completed": True,
                "target": asdict(target),
                "ended_s": time.monotonic() - 0.5,
                "artifacts": [
                    {
                        "path": samples.name,
                        "size_bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                ],
            }
        )
    )
    return receipt


def read_artifacts(receipt: dict[str, Any], output: Path) -> str:
    """Return the captured text, so a passing receipt can still be read."""
    bodies = []
    for artifact in receipt.get("artifacts", []):
        path = output / artifact["path"]
        if path.is_file() and artifact.get("size_bytes"):
            bodies.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(bodies)


def capture(
    prepared: Any, target: Any, budget: Any, root: Path, case: Case
) -> tuple[dict[str, Any], str]:
    """Take one reading at one checkpoint through the real adapter."""
    adapter = prepared.adapter(
        budget=budget,
        natural_checkpoint=natural_checkpoint(root, target, case.checkpoint),
        max_capture_bytes=prepared.quota_bytes,
        natural_phase=case.checkpoint,
    )
    output = root / f"{case.checkpoint}-{case.operation}"
    shutil.rmtree(output, ignore_errors=True)
    receipt = json.loads(
        adapter.capture(target, case.operation, output, TIMEOUT_S).read_text()
    )
    return receipt, read_artifacts(receipt, output)


def validate(
    image: str, *, flagged: bool, registry: str, builder: str, uid: int, helper: str
) -> list[str]:
    """Provision against a real target and report every case that misbehaved."""
    from nanolab.tasks.soak.diagnostic_helper import (
        DockerHelperSpec,
        LocalDockerDiagnosticProvisioner,
    )
    from nanolab.tasks.soak.diagnostics import DiagnosticBudget
    from nanolab.tasks.soak.models import Target
    from nanolab.workspace.paths import bundled_assets_root

    run_dir = SCRATCH / "run"
    shutil.rmtree(run_dir, ignore_errors=True)
    root = SCRATCH / ("flagged" if flagged else "unflagged")
    shutil.rmtree(root, ignore_errors=True)
    # The supervisor opens its logs exclusively, so the root is fresh each time.
    run_dir.mkdir(parents=True)
    root.mkdir(parents=True)

    start_target(image, flagged=flagged, uid=uid)
    observed = json.loads(
        checked(["docker", "inspect", CONTAINER], what="inspecting the target")
    )[0]
    target = Target(
        "control-plane",
        observed["Id"],
        observed["State"]["Pid"],
        observed["State"]["StartedAt"],
        _digest(image),
        "jvm",
    )
    spec = DockerHelperSpec(
        target=target,
        helper_image=helper,
        owner_label=OWNER_LABEL,
        owner_value=RUN_ID,
        uid=uid,
        gid=uid,
        output_root=root,
        quota_bytes=QUOTA,
        helper_memory_bytes=QUOTA + 64 * 1024 * 1024,
        allow_target_stop_on_cancel=True,
        target_tmp_volume=VOLUME,
    )
    failures: list[str] = []
    assets = bundled_assets_root() / "soak"
    prepared = LocalDockerDiagnosticProvisioner(assets_dir=assets).prepare(
        spec, timeout_s=TIMEOUT_S
    )
    try:
        print(
            "provisioned operations:",
            json.loads(prepared.receipt.read_text())["operations"],
        )
        cases = FLAGGED if flagged else UNFLAGGED
        # One reservation per capture, never refunded, plus the receipt each one
        # writes: the pool a run sizes is `available - receipt * captures`, and
        # a pool one receipt short fails the last case rather than the first.
        budget = DiagnosticBudget(0, 0, len(cases) * (QUOTA + _RECEIPT_BYTES))
        for case in cases:
            receipt, body = capture(prepared, target, budget, root, case)
            claimed = receipt.get("reason", "") + body
            placed = all(
                item["path"].startswith("artifacts/")
                for item in receipt.get("artifacts", [])
            )
            outcome = (
                "ok"
                if receipt["status"] == case.status
                and case.contains in claimed
                and placed
                else "BAD"
            )
            print(
                f"  [{outcome}] {case.checkpoint}/{case.operation}: "
                f"{receipt['status']} -- {receipt.get('reason', '')[:160]}"
            )
            for line in body.splitlines()[:8]:
                print(f"        | {line}")
            if outcome == "BAD":
                failures.append(
                    f"{case.operation} at {case.checkpoint}: expected "
                    f"{case.status} carrying {case.contains!r}, got "
                    f"{receipt['status']} with {receipt.get('reason', '')!r}"
                )
                # A capture whose completion the helper could not confirm stops
                # the owned target, deliberately: a `jcmd` that may still be
                # running inside the measured JVM must not be left there. So the
                # rest of this target's cases have nothing to run against.
                print(
                    "  that capture stopped the owned target, so this target's "
                    "remaining cases cannot run"
                )
                break
    finally:
        prepared.close()
    return failures


def _digest(image: str) -> str:
    """Resolve a local image to its repository digest, which the spec requires."""
    if "@sha256:" in image:
        return image
    observed = json.loads(
        checked(["docker", "image", "inspect", image], what="inspecting the image")
    )[0]
    digests = observed.get("RepoDigests") or []
    if not digests:
        raise SystemExit(
            f"target image {image} has no repository digest; push it to the "
            "registry first, because the spec binds an immutable reference"
        )
    return digests[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rm",
        action="store_true",
        help="remove the target container, its volume and the built image copies",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="check preconditions and stop"
    )
    parser.add_argument(
        "--target-image",
        default=None,
        help="run this image as the target instead of the helper's own JDK",
    )
    parser.add_argument("--builder", default="nanolab-heap-analysis")
    parser.add_argument("--registry", default="localhost:5000/nanolab")
    parser.add_argument(
        "--helper-image",
        default=None,
        help="a published helper digest to validate, instead of building one",
    )
    parser.add_argument("--uid", type=int, default=None)
    arguments = parser.parse_args()

    preconditions(arguments.registry, arguments.builder)
    uid = arguments.uid if arguments.uid is not None else os.getuid()
    print(
        f"registry {arguments.registry} is answering; builder "
        f"{arguments.builder} exists; uid {uid}"
    )
    if arguments.dry_run:
        print("dry run: nothing built, no container started")
        return 0

    helper = arguments.helper_image
    if helper is None:
        from nanolab.tasks.soak.helper_build import (
            HelperImageRequest,
            build_helper_image,
        )

        helper = build_helper_image(
            HelperImageRequest(
                run_dir=_fresh_run_dir(),
                run_id=RUN_ID,
                registry=arguments.registry,
                builder=arguments.builder,
            )
        )
        print(f"helper: {helper}")
    # The helper image is a JDK with a shell, which the published application
    # images are not; validating against it keeps the check self-contained.
    image = arguments.target_image or helper
    failures = validate(
        image,
        flagged=True,
        registry=arguments.registry,
        builder=arguments.builder,
        uid=uid,
        helper=helper,
    )
    stop_target()
    # Last, because a target without the flag is a second container and a second
    # provisioning: the reading must be refused, not recorded as an empty one.
    failures += validate(
        image,
        flagged=False,
        registry=arguments.registry,
        builder=arguments.builder,
        uid=uid,
        helper=helper,
    )
    stop_target()
    if arguments.rm:
        docker("rmi", image)
    if failures:
        print("\n".join(f"FAILED: {item}" for item in failures), file=sys.stderr)
        return 1
    print("every case behaved as declared")
    return 0


def _fresh_run_dir() -> Path:
    """Return an empty run root; the supervisor refuses to reuse its log files."""
    run_dir = SCRATCH / "run"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    return run_dir


if __name__ == "__main__":
    raise SystemExit(main())
