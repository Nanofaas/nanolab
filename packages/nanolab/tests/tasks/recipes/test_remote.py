from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import cast

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.tasks.recipes.remote import (
    RemoteRecipeRun,
    bundle_recipe_source,
    remote_recipe_distribution_resource,
)
from nanolab.tasks.vm.models import VmRequest
from nanolab.workspace.recipe import RecipeRun, prepare_recipe_run


def test_remote_bundle_retains_source_but_removes_git_remote_and_hooks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / ".git" / "hooks").mkdir(parents=True)
    (source / ".git" / "objects" / "info").mkdir(parents=True)
    (source / ".git" / "config").write_text(
        '[remote "origin"]\n url = https://secret.example/token\n'
    )
    (source / ".git" / "hooks" / "post-checkout").write_text("secret hook")
    (source / ".git" / "objects" / "info" / "alternates").write_text(
        "/host/private/repo"
    )
    (source / "hello.txt").write_text("tracked edit")
    archive = tmp_path / "source.tar.gz"

    bundle_recipe_source(source, archive)

    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
        assert "source/hello.txt" in names
        assert "source/.git/hooks/post-checkout" not in names
        assert "source/.git/objects/info/alternates" not in names
        config = bundle.extractfile("source/.git/config")
        assert config is not None
        assert b"secret.example" not in config.read()


def test_remote_bundle_preserves_tracked_diff_and_executable_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source with spaces"
    source.mkdir()

    def git(root: Path, *args: str) -> bytes:
        return subprocess.check_output(("git", *args), cwd=root)

    git(source, "init", "-q")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "old.txt").write_text("remove me")
    (source / "script.sh").write_text("exit 0\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "initial")
    (source / "old.txt").unlink()
    (source / "script.sh").write_text("exit 1\n")
    (source / "script.sh").chmod(0o755)
    recipe = tmp_path / "profile.yaml"
    recipe.write_text("schemaVersion: 2\n")
    staged = prepare_recipe_run(source, recipe, tmp_path / "run", "recipe-123")
    archive = tmp_path / "source.tar.gz"

    bundle_recipe_source(staged.source_dir, archive)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    remote = extracted / "source"

    assert git(remote, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert git(remote, "diff", "HEAD", "--binary") == git(
        source, "diff", "HEAD", "--binary"
    )
    assert not (remote / "old.txt").exists()
    assert (remote / "script.sh").stat().st_mode & 0o111


def test_failed_vm_gradle_fetches_log_and_rejects_stale_report(tmp_path: Path) -> None:
    local = RecipeRun(
        tmp_path / "source",
        tmp_path / "recipe.yaml",
        tmp_path / "distribution",
        "recipe-123abc",
    )
    local.output_dir.mkdir()
    (local.output_dir / "distribution.json").write_text('{"stale":true}')
    remote = RemoteRecipeRun(local, PurePosixPath("/home/ubuntu/nanolab-recipe-123abc"))
    resource = Resource(
        title="remote run",
        acquire=lambda _inputs: remote,
        release=lambda _inputs, _value: None,
    )

    class Provider:
        def __init__(self) -> None:
            self.fetched: list[str] = []

        def exec_argv(self, _request, argv, **_kwargs):
            return SimpleNamespace(
                return_code=1 if argv[0] == "sh" else 0,
                stdout="failed",
                stderr="build error",
            )

        def transfer_from(self, _request, *, source, destination):
            self.fetched.append(source)
            destination.write_text("remote log")
            return SimpleNamespace(return_code=0)

    provider = Provider()
    distribution = remote_recipe_distribution_resource(
        run=resource,
        provider=cast(VmCommandProvider, provider),
        request=VmRequest(lifecycle="multipass", name="test"),
        functions=(),
        required_modules=frozenset(),
    )
    inputs = TaskInputs._for_resources({resource: remote}, {resource})

    with pytest.raises(RuntimeError, match="VM command failed"):
        distribution.acquire(inputs)
    assert str(remote.root / "gradle.log") in provider.fetched
    assert (local.output_dir.parent / "gradle.log").read_text() == "remote log"
