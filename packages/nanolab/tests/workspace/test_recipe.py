from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nanolab.workspace.recipe import prepare_recipe_run


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def test_prepare_recipe_run_preserves_tracked_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "name with spaces").write_text("before")
    executable = source / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (source / "deleted.txt").write_text("delete")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "initial")
    (source / "name with spaces").write_text("after")
    (source / "deleted.txt").unlink()
    executable.write_text("#!/bin/sh\nexit 1\n")
    recipe = tmp_path / "profile.yaml"
    recipe.write_text("schemaVersion: 2\n")
    before = _git(source, "status", "--porcelain")

    run = prepare_recipe_run(source, recipe, tmp_path / "run", "run-1")

    assert (run.source_dir / "name with spaces").read_text() == "after"
    assert not (run.source_dir / "deleted.txt").exists()
    assert os.access(run.source_dir / "run.sh", os.X_OK)
    assert run.recipe.read_bytes() == recipe.read_bytes()
    assert _git(source, "status", "--porcelain") == before
    assert _git(run.source_dir, "rev-parse", "HEAD") == _git(
        source, "rev-parse", "HEAD"
    )


def test_prepare_recipe_run_rejects_different_inputs_on_retry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "tracked").write_text("one")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "initial")
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("schemaVersion: 2\n")
    run_dir = tmp_path / "run"
    first = prepare_recipe_run(source, recipe, run_dir, "run-1")
    assert prepare_recipe_run(source, recipe, run_dir, "run-1") == first
    recipe.write_text("schemaVersion: 1\n")
    with pytest.raises(ValueError, match="different inputs"):
        prepare_recipe_run(source, recipe, run_dir, "run-1")


def test_prepare_recipe_run_rejects_symlink_outside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "external").symlink_to(tmp_path / "outside", target_is_directory=True)
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "initial")
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("schemaVersion: 2\n")
    with pytest.raises(ValueError, match="symlink"):
        prepare_recipe_run(source, recipe, tmp_path / "run", "run-1")
