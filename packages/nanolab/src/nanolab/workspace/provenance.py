"""What the nanoFaaS checkout looks like right now.

The run metadata records it so a result can be traced back to a source state,
and the image tags derive from it so a changed source cannot reach the cluster
under a name the cluster already has.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def git_commit(repo_root: Path) -> str | None:
    """Return the checkout's HEAD commit hash, or None if git cannot say.

    None when git is missing, the directory is not a repository, or the query
    fails for any other reason.
    """
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_provenance(repo_root: Path) -> dict[str, object]:
    """Summarise the checkout's working state for the run metadata.

    Returns the HEAD commit, whether the tree is dirty, a SHA-256 over the
    tracked diff plus the contents of every untracked file, and the raw
    ``git status`` lines. When git is missing or a query fails, the commit is
    still reported but the dirty flag and fingerprint are None and the status
    list is empty.
    """
    commit = git_commit(repo_root)
    try:
        status = subprocess.run(
            ("git", "status", "--porcelain=v1"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        diff = subprocess.run(
            ("git", "diff", "--binary", "HEAD"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        untracked = subprocess.run(
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return {
            "git_commit": commit,
            "git_dirty": None,
            "git_diff_sha256": None,
            "git_status": [],
        }
    if status.returncode != 0 or diff.returncode != 0 or untracked.returncode != 0:
        return {
            "git_commit": commit,
            "git_dirty": None,
            "git_diff_sha256": None,
            "git_status": [],
        }
    digest = hashlib.sha256((status.stdout + "\0" + diff.stdout).encode("utf-8"))
    for relative_path in filter(None, untracked.stdout.split("\0")):
        digest.update(b"\0untracked\0")
        digest.update(relative_path.encode("utf-8"))
        try:
            digest.update((repo_root / relative_path).read_bytes())
        except OSError:
            digest.update(b"\0unavailable")
    return {
        "git_commit": commit,
        "git_dirty": bool(status.stdout.strip()),
        "git_diff_sha256": digest.hexdigest(),
        "git_status": status.stdout.splitlines(),
    }


def source_fingerprint(repo_root: Path) -> str | None:
    """Fingerprint the checkout's current contents as one hash.

    Image tags are built from this. A fixed tag like `:e2e` is a name, not a
    content: rebuilding under it leaves every Deployment manifest byte-identical,
    so Kubernetes has nothing to react to and keeps the pod it already had. The
    rebuilt image reaches the registry and never reaches the cluster — silently,
    with every task green. A fingerprinted tag makes a source change a manifest
    change, which is what actually triggers a rollout.

    None when there is no git to ask: the caller then keeps the fixed tag rather
    than inventing a random one that would rebuild the world every run.
    """
    provenance = git_provenance(repo_root)
    commit = provenance["git_commit"]
    if not isinstance(commit, str):
        return None
    diff_sha = provenance["git_diff_sha256"]
    return hashlib.sha256(
        f"{commit}\0{diff_sha if isinstance(diff_sha, str) else ''}".encode()
    ).hexdigest()
