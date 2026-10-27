"""The paper's qr-code wrappers, built once per run on the stack VM.

Every baseline runs the one implementation in `nanofaas-paper/functions/qr-code`:
the computation in its `core` crate, a thin wrapper per platform. The source is
taken at a commit, never from a working tree with changes, and sent as that
commit's `git archive`, so what is built is what the identity records.

The build runs in a pinned Rust builder on the VM, for Linux (static, musl) and
the two WASI targets. Before any cell uses them, `qr-stdio` renders the
protocol's payload and the PNG must be the one the protocol froze, or, while it
is not frozen, the one the measured nanoFaaS image produced on Azure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from nanolab.comparison.prepare import comparison_remote_command, comparison_transfer_to
from nanolab.tasks.experiment_02.vm import Vm, exec_status
from nanolab.workspace.git import git_output

WORKLOADS_ENV = "NANOLAB_WORKLOADS_ROOT"
WORKLOAD = "qr-code"
# rust:1.99-alpine: musl is the host target, so native builds are static.
BUILDER = "rust@sha256:b1d5dd5a22a09404d3e798c0cf051b689077360374eb505fa5bb93c479693d87"
# The PNG the measured nanoFaaS image rendered for the experiments' payload.
MEASURED_PNG_SHA256 = "aa04cabc94ed3cecf89cabd027588b5c6ca19b1d3bdd2efa6d74c3af9b3086c1"
ARTIFACTS: dict[str, str] = {
    "qr-stdio": "target/release/qr-stdio",
    "qr-fn": "target/release/qr-fn",
    "qr-funless": "target/wasm32-wasip1/release/qr_funless.wasm",
    "qr-spin": "target/wasm32-wasip2/release/qr_spin.wasm",
}
_BUILD = (
    "rustup target add wasm32-wasip1 wasm32-wasip2 >/dev/null"
    " && cargo build -q --release --locked -p qr-stdio -p qr-fn"
    " && cargo build -q --release --locked -p qr-funless --target wasm32-wasip1"
    " && cargo build -q --release --locked -p qr-spin --target wasm32-wasip2"
)


def _git(root: Path, *args: str) -> str:
    return git_output(root, *args).decode().strip()


def capture_workloads(root: Path) -> dict[str, str]:
    """Return the commit and the tree of `root/qr-code`, refusing local changes."""
    if not (root / WORKLOAD).is_dir():
        raise ValueError(f"{root} has no {WORKLOAD} workload")
    if _git(root, "status", "--porcelain", "--", WORKLOAD):
        raise ValueError(f"{root / WORKLOAD} is dirty: commit it before measuring")
    return {
        "commit": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", f"HEAD:./{WORKLOAD}"),
    }


def build_workloads(
    stack: Vm,
    *,
    workloads: Path,
    remote_root: PurePosixPath,
    payload: bytes,
    png_sha256: str | None,
) -> dict[str, str]:
    """Build every wrapper on the stack; return each artifact's sha256 by key."""
    _ = capture_workloads(workloads)
    source = remote_root / WORKLOAD
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "qr-code.tar"
        archive.write_bytes(
            subprocess.check_output(
                ("git", "archive", "--format=tar", "HEAD", WORKLOAD), cwd=workloads
            )
        )
        request = Path(scratch) / "payload.json"
        request.write_bytes(payload)
        _ = comparison_remote_command(
            stack.provider, stack.request, ("mkdir", "-p", str(remote_root))
        )
        for local in (archive, request):
            comparison_transfer_to(
                stack.provider, stack.request, local, remote_root / local.name
            )
    _ = comparison_remote_command(
        stack.provider,
        stack.request,
        (
            "sh",
            "-c",
            f"rm -rf {source} && tar -xf {remote_root}/qr-code.tar -C {remote_root}",
        ),
    )
    _ = comparison_remote_command(
        stack.provider,
        stack.request,
        ("sudo", "docker", "run", "--rm", "-v", f"{source}:/src", "-w", "/src",
         BUILDER, "sh", "-c", _BUILD),
    )  # fmt: skip
    rendered = comparison_remote_command(
        stack.provider,
        stack.request,
        ("sh", "-c", f"{source}/{ARTIFACTS['qr-stdio']} < {remote_root}/payload.json"),
    )
    png = base64.b64decode(json.loads(rendered))
    expected = png_sha256 or MEASURED_PNG_SHA256
    if hashlib.sha256(png).hexdigest() != expected:
        raise ValueError(f"the built qr-stdio renders a png other than {expected}")
    paths = [str(source / path) for path in ARTIFACTS.values()]
    sums = comparison_remote_command(
        stack.provider, stack.request, ("sha256sum", *paths)
    ).split()
    return dict(zip(ARTIFACTS, sums[0::2], strict=True))


def ensure_workloads(
    stack: Vm,
    *,
    workloads: Path,
    remote_root: PurePosixPath,
    payload: bytes,
    png_sha256: str | None,
    record: Path,
) -> dict[str, str]:
    """Build the wrappers once per run; on a resume, require the ones recorded.

    `record` keeps the first build's sha256 per artifact. A resume rebuilds
    nothing and refuses artifacts that are missing or differ on the stack.
    """
    if not record.is_file():
        artifacts = build_workloads(
            stack,
            workloads=workloads,
            remote_root=remote_root,
            payload=payload,
            png_sha256=png_sha256,
        )
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(artifacts, indent=2) + "\n")
        return artifacts
    recorded = json.loads(record.read_text())
    paths = [str(remote_root / WORKLOAD / path) for path in ARTIFACTS.values()]
    listed = exec_status(stack, ("sha256sum", *paths))
    current = dict(zip(ARTIFACTS, listed.stdout.split()[0::2], strict=False))
    if listed.return_code or current != recorded:
        raise ValueError(
            "the workload artifacts on the stack changed since this run built them; "
            "use a new run directory"
        )
    return recorded
