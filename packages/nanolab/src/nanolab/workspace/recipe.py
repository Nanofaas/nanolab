"""Create a writable, reproducible NanoFaaS source for one recipe run."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RecipeRun:
    """Paths and tag for an isolated recipe build."""

    source_dir: Path
    recipe: Path
    output_dir: Path
    tag: str


def _git(source: Path, *args: str) -> bytes:
    return subprocess.run(
        ("git", *args), cwd=source, check=True, capture_output=True
    ).stdout


def _verify_staged_inputs(source: Path, staged: Path) -> None:
    """Check every tracked source path, including additions absent from HEAD."""
    for raw_name in _git(source, "ls-files", "-z").split(b"\0"):
        if not raw_name:
            continue
        name = os.fsdecode(raw_name)
        original = source / name
        copy = staged / name
        for root, path in ((source, original), (staged, copy)):
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                raise ValueError(f"Recipe source symlink escapes checkout: {name}")
        if original.is_symlink():
            same = copy.is_symlink() and original.readlink() == copy.readlink()
        elif original.is_file():
            same = (
                copy.is_file()
                and not copy.is_symlink()
                and original.read_bytes() == copy.read_bytes()
                and bool(original.stat().st_mode & 0o111)
                == bool(copy.stat().st_mode & 0o111)
            )
        else:
            same = not copy.exists() and not copy.is_symlink()
        if not same:
            raise ValueError(f"Recipe staged inputs differ from source: {name}")


def prepare_recipe_run(
    source: Path, recipe: Path, run_dir: Path, tag: str
) -> RecipeRun:
    """Clone tracked source and overlay its tracked edits without touching it."""
    source = source.resolve()
    recipe = recipe.resolve()
    run_dir = run_dir.resolve()
    revision = _git(source, "rev-parse", "HEAD").decode().strip()
    patch = _git(source, "diff", "HEAD", "--binary", "--no-ext-diff")
    profile = recipe.read_bytes()
    identity = {
        "source": str(source),
        "revision": revision,
        "patchSha256": hashlib.sha256(patch).hexdigest(),
        "recipeSha256": hashlib.sha256(profile).hexdigest(),
        "tag": tag,
    }
    metadata = run_dir / "recipe-inputs.json"
    result = RecipeRun(
        run_dir / "source", run_dir / "recipe.yaml", run_dir / "distribution", tag
    )
    if run_dir.exists():
        if not metadata.is_file() or json.loads(metadata.read_text()) != identity:
            raise ValueError(f"Recipe run directory has different inputs: {run_dir}")
        if result.recipe.read_bytes() != profile:
            raise ValueError("Recipe staged inputs differ from source: recipe.yaml")
        _verify_staged_inputs(source, result.source_dir)
        return result
    run_dir.mkdir(parents=True)
    subprocess.run(
        (
            "git",
            "clone",
            "--no-hardlinks",
            "--no-checkout",
            "-q",
            str(source),
            str(result.source_dir),
        ),
        check=True,
        capture_output=True,
    )
    _git(result.source_dir, "checkout", "-q", "--detach", revision)
    if patch:
        subprocess.run(
            ("git", "apply", "--index", "--binary", "-"),
            cwd=result.source_dir,
            input=patch,
            check=True,
            capture_output=True,
        )
    _verify_staged_inputs(source, result.source_dir)
    if (
        _git(source, "rev-parse", "HEAD").decode().strip() != revision
        or _git(source, "diff", "HEAD", "--binary", "--no-ext-diff") != patch
    ):
        raise RuntimeError("NanoFaaS source changed while preparing recipe run")
    result.recipe.write_bytes(profile)
    metadata.write_text(json.dumps(identity, indent=2) + "\n")
    return result
