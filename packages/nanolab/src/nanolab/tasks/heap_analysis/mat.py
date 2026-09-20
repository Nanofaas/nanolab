"""Digest-pinned Eclipse MAT report runner.

Runs the eight fixed report invocations over a baseline/final HPROF pair
inside a single-shot Docker container (no network, read-only root, dropped
capabilities, finite CPU/memory/time/output), then binds every produced report
to its input dump's SHA-256 and writes an atomic manifest. MAT itself is only
ever invoked after the application deployment has been released; this module
does not hold or assume any deployment resource.

There is no dump-size budget here, by explicit operator decision: dumps that
were captured successfully are always analysable. Refusing them after capture
throws away the whole run and protects nothing, because the bytes are already
on disk. For the same reason MAT's /work area is an unbounded disk-backed
directory owned by the run rather than a sized tmpfs - a tmpfs would charge
its pages to the container's memory cgroup and be OOM-killed against
``--memory`` long before any quota could bind.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from nanolab.tasks.soak.artifacts import describe_artifact, measure_tree
from nanolab.tasks.soak.processes import run_owned_command
from nanolab.workspace.paths import bundled_assets_root

_DIGEST = re.compile(r"[^\s@]+@sha256:[a-f0-9]{64}")

CONTAINER_BASELINE = "/dumps/baseline.hprof"
CONTAINER_FINAL = "/dumps/final.hprof"
CONTAINER_OUT = "/out"
CONTAINER_LAUNCHER = "/opt/mat/ParseHeapDump.sh"
CONTAINER_WORKER = "/opt/nanolab/mat-worker.py"
CONTAINER_PYTHON = "/usr/local/bin/python3"

# Every produced report is bound to exactly one of these (dump, report_id)
# pairs. Kept in sync by hand with the eight invocations assets/soak/mat-worker.py
# runs - the report set the brief fixes, not a configurable surface.
REQUIRED_REPORTS = (
    ("baseline", "org.eclipse.mat.api:overview"),
    ("baseline", "org.eclipse.mat.api:suspects"),
    ("baseline", "org.eclipse.mat.api:top_components"),
    ("final", "org.eclipse.mat.api:overview"),
    ("final", "org.eclipse.mat.api:suspects"),
    ("final", "org.eclipse.mat.api:top_components"),
    ("final", "org.eclipse.mat.api:compare"),
    ("final", "org.eclipse.mat.api:suspects2"),
)

_PIDS_LIMIT = "512"
_REAP_TIMEOUT_S = 30
_DOCKER_LOG_LIMIT_BYTES = 1024 * 1024

# The lock lives beside this package: the same bundled-asset lookup runtime.py
# and workload.py already use for assets/soak/*, and the only one that still
# resolves once nanolab is installed rather than run from a checkout.
_LOCK_PATH = bundled_assets_root() / "soak/mat.lock.json"
_UNKNOWN_MAT_VERSION = "unknown"


def _read_mat_version(lock_path: Path) -> str:
    """Read the frozen MAT version from mat.lock.json, never raising."""
    try:
        data = json.loads(lock_path.read_text())
    except (OSError, ValueError):
        return _UNKNOWN_MAT_VERSION
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else _UNKNOWN_MAT_VERSION


@dataclass(frozen=True)
class MatAnalysisRequest:
    """One bounded MAT analysis of a baseline/final HPROF pair.

    output_dir must already exist and be owned by the caller. MatAnalyzer
    creates a fresh "analysis" subdirectory under it and refuses to reuse one.
    """

    baseline_hprof: Path
    final_hprof: Path
    output_dir: Path
    helper_image: str
    artifact_limit_bytes: int
    mat_memory_mib: int
    mat_cpus: float
    mat_timeout_s: int
    uid: int
    gid: int
    docker: str = "/usr/bin/docker"
    docker_host: str = "unix:///var/run/docker.sock"

    def __post_init__(self) -> None:
        """Reject a tag, a missing or symlinked dump, or a bad output root.

        Dump SIZE is deliberately not checked: see the module docstring.
        """
        if _DIGEST.fullmatch(self.helper_image) is None:
            raise ValueError("helper_image must be digest-pinned")
        for dump in (self.baseline_hprof, self.final_hprof):
            if dump.is_symlink():
                raise ValueError(f"heap dump must not be a symlink: {dump}")
            if not dump.is_file():
                raise ValueError(f"heap dump is missing: {dump}")
        if (
            not self.output_dir.is_absolute()
            or self.output_dir.is_symlink()
            or not self.output_dir.is_dir()
        ):
            raise ValueError(
                "output_dir must be an owned, existing, non-symlink directory"
            )
        if type(self.artifact_limit_bytes) is not int or self.artifact_limit_bytes <= 0:
            raise ValueError("artifact_limit_bytes must be a positive integer")
        if type(self.mat_memory_mib) is not int or self.mat_memory_mib <= 0:
            raise ValueError("mat_memory_mib must be a positive integer")
        if (
            isinstance(self.mat_cpus, bool)
            or not isinstance(self.mat_cpus, (int, float))
            or self.mat_cpus <= 0
        ):
            raise ValueError("mat_cpus must be a positive number")
        if type(self.mat_timeout_s) is not int or self.mat_timeout_s <= 0:
            raise ValueError("mat_timeout_s must be a positive integer")
        if any(type(value) is not int or value < 0 for value in (self.uid, self.gid)):
            raise ValueError("uid and gid must be non-negative integers")
        if not Path(self.docker).is_absolute() or not self.docker_host.startswith(
            "unix:///"
        ):
            raise ValueError("docker must be an absolute path with a unix socket host")

    @property
    def analysis_dir(self) -> Path:
        """Fresh evidence root this run owns exclusively."""
        return self.output_dir / "analysis"

    @property
    def work_dir(self) -> Path:
        """Unbounded disk-backed MAT work area this run owns exclusively."""
        return self.output_dir / "mat-work"

    @property
    def container_name(self) -> str:
        """The exact owned container name, so a stuck MAT is always reapable."""
        return f"nanolab-mat-{self.output_dir.name}"


def mat_run_argv(request: MatAnalysisRequest) -> tuple[str, ...]:
    """Build a single-shot `docker run` invocation with an exact owned name.

    No network, no writable root, a dropped-capability non-root user, finite
    CPU/memory/time, read-only dump mounts, and two caller-owned writable
    mounts: the analysis directory and the unbounded disk-backed work area.
    The name and owner label exist so the container can always be found and
    removed, including on the mat_timeout_s path where SIGKILLing the docker
    CLI leaves the container running.
    """
    return (
        request.docker,
        "--host",
        request.docker_host,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        request.container_name,
        "--label",
        f"nanolab.run={request.output_dir.name}",
        "--label",
        "nanolab.mat=true",
        "--network",
        "none",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--read-only",
        "--user",
        f"{request.uid}:{request.gid}",
        "--cpus",
        str(request.mat_cpus),
        "--memory",
        f"{request.mat_memory_mib}m",
        "--memory-swap",
        f"{request.mat_memory_mib}m",
        "--pids-limit",
        _PIDS_LIMIT,
        # Eclipse's launcher writes its configuration area under $HOME by
        # default; unset, that resolves under the read-only root and every
        # invocation fails with "Invalid Configuration Location" (exit 15).
        "--env",
        "HOME=/work",
        "--mount",
        f"type=bind,source={request.baseline_hprof},"
        f"target={CONTAINER_BASELINE},readonly",
        "--mount",
        f"type=bind,source={request.final_hprof},target={CONTAINER_FINAL},readonly",
        "--mount",
        f"type=bind,source={request.analysis_dir},target={CONTAINER_OUT}",
        # Disk-backed and uncapped on purpose. MAT writes working indices
        # several times the dump size; on a tmpfs those pages charge to this
        # container's memory cgroup and are OOM-killed against --memory.
        "--mount",
        f"type=bind,source={request.work_dir},target=/work",
        # MAT's report renderer stages each report via
        # java.io.File.createTempFile against Java's default java.io.tmpdir
        # (/tmp), which otherwise doesn't exist as a writable mount and
        # fails with "IOException: Read-only file system".
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=268435456,mode=1777",  # nosec B108 - a docker --tmpfs mount argument, not a host temp path
        "--entrypoint",
        CONTAINER_PYTHON,
        request.helper_image,
        CONTAINER_WORKER,
        CONTAINER_BASELINE,
        CONTAINER_FINAL,
        CONTAINER_OUT,
        CONTAINER_LAUNCHER,
    )


def _reap_container(request: MatAnalysisRequest, analysis: Path) -> None:
    """Remove the exact owned MAT container. Best effort, never raises."""
    try:
        run_owned_command(
            (
                request.docker,
                "--host",
                request.docker_host,
                "rm",
                "--force",
                request.container_name,
            ),
            cwd=analysis,
            env=_docker_env(),
            log_path=analysis / "docker-rm.log",
            timeout_s=_REAP_TIMEOUT_S,
            cancelled=Event(),
            output_limit_bytes=_DOCKER_LOG_LIMIT_BYTES,
        )
    except Exception:  # teardown must not mask the real failure
        return


def _docker_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DOCKER_", "PYTHON", "LD_"))
    }


def _load_receipt(analysis: Path) -> dict | None:
    path = analysis / "mat-worker-receipt.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _build_manifest(
    request: MatAnalysisRequest,
    analysis: Path,
    docker_result,
    started_s: float,
    ended_s: float,
    mat_version: str,
) -> dict:
    errors: list[str] = []
    reports_dir = analysis / "reports"
    dumps = {
        "baseline": describe_artifact(request.baseline_hprof),
        "final": describe_artifact(request.final_hprof),
    }
    if (
        docker_result.returncode != 0
        or not docker_result.reaped
        or docker_result.errors
        or docker_result.cancelled
        or docker_result.timed_out
        or docker_result.forced_stop
        or docker_result.quota_exceeded
    ):
        errors.append("MAT container did not complete cleanly")
    receipt = _load_receipt(analysis)
    reports: dict[str, dict] = {}
    if receipt is None:
        errors.append("mat-worker-receipt.json is missing or unreadable")
    else:
        by_label = {
            (item.get("dump"), item.get("report_id")): item
            for item in receipt.get("invocations", [])
            if isinstance(item, dict)
        }
        for dump, report_id in REQUIRED_REPORTS:
            label = f"{dump}:{report_id}"
            item = by_label.get((dump, report_id))
            if item is None or item.get("returncode") != 0 or not item.get("outputs"):
                errors.append(f"report not produced: {label}")
                continue
            files = []
            for output in item["outputs"]:
                path = reports_dir / output["name"]
                try:
                    if not path.is_file() or path.stat().st_size == 0:
                        errors.append(
                            f"report output missing or empty: {output['name']}"
                        )
                        continue
                    observed = describe_artifact(path)
                except OSError as error:
                    errors.append(
                        f"report output unreadable: {output['name']}: {error}"
                    )
                    continue
                if observed["sha256"] != output.get("sha256"):
                    errors.append(f"report output hash mismatch: {output['name']}")
                    continue
                files.append(observed)
            if not files:
                errors.append(f"report produced no valid output: {label}")
                continue
            reports[label] = {
                "dump": dump,
                "report_id": report_id,
                "argv": item.get("argv"),
                "duration_s": item.get("duration_s"),
                "files": files,
            }
    try:
        artifact_bytes = measure_tree(analysis)
    except OSError as error:
        artifact_bytes = None
        errors.append(f"artifact size measurement failed: {error}")
    else:
        if artifact_bytes > request.artifact_limit_bytes:
            errors.append(
                f"cumulative run artifacts {artifact_bytes} exceed the "
                f"{request.artifact_limit_bytes} byte budget"
            )
    status = "PASS" if not errors and len(reports) == len(REQUIRED_REPORTS) else "FAIL"
    return {
        "schema": "nanolab-heap-analysis-mat-manifest-v1",
        "status": status,
        "helper_image": request.helper_image,
        "mat_version": mat_version,
        "commands": {"docker_run": list(mat_run_argv(request))},
        "dumps": dumps,
        "reports": reports,
        "artifact_bytes": artifact_bytes,
        "started_s": started_s,
        "ended_s": ended_s,
        "duration_s": ended_s - started_s,
        "errors": errors,
    }


def _write_manifest(analysis: Path, manifest: dict) -> Path:
    target = analysis / "manifest.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, sort_keys=True, indent=2))
    temporary.replace(target)
    return target


class MatAnalyzer:
    """Run the fixed MAT report set for a single baseline/final dump pair."""

    def __init__(self, lock_path: Path = _LOCK_PATH) -> None:
        """Freeze the MAT version this analyzer records, read once at construction."""
        self._mat_version = _read_mat_version(lock_path)

    def run(self, request: MatAnalysisRequest) -> Path:
        """Execute the MAT container and publish the run's manifest.

        The container carries an exact owned name, so the `finally` can always
        reap it. Without that, a MAT still alive when the supervisor SIGKILLs
        the `docker` CLI (most likely on the mat_timeout_s path) survives the
        run holding its CPU and memory reservation with nothing able to find
        it: SIGKILL on the client never reaches the container.
        """
        analysis = request.analysis_dir
        if analysis.is_symlink() or analysis.exists():
            raise FileExistsError(f"analysis directory already exists: {analysis}")
        work = request.work_dir
        if work.is_symlink() or work.exists():
            raise FileExistsError(f"work directory already exists: {work}")
        analysis.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        started = time.time()
        try:
            docker_result = run_owned_command(
                mat_run_argv(request),
                cwd=analysis,
                env=_docker_env(),
                log_path=analysis / "docker-run.log",
                timeout_s=request.mat_timeout_s,
                cancelled=Event(),
                output_limit_bytes=_DOCKER_LOG_LIMIT_BYTES,
            )
        finally:
            _reap_container(request, analysis)
            shutil.rmtree(work, ignore_errors=True)
        ended = time.time()
        manifest = _build_manifest(
            request, analysis, docker_result, started, ended, self._mat_version
        )
        return _write_manifest(analysis, manifest)
