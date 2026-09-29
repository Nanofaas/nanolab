"""Transfer a captured recipe run and publish it inside an explicit stack VM."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.vm.logged import run_remote_logged
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.tasks.recipe import (
    RecipeDistribution,
    read_distribution,
    recipe_command,
    require_validation_distribution,
)
from nanolab.tasks.vm.models import VmRequest, vm_remote_home
from nanolab.tasks.vm.runners import VmFileFetcher
from nanolab.workspace.recipe import RecipeRun


@dataclass(frozen=True, slots=True)
class RemoteRecipeRun:
    """The host evidence and owned source/output paths inside one VM."""

    local: RecipeRun
    root: PurePosixPath

    @property
    def source(self) -> PurePosixPath:
        """Return the VM source checkout path."""
        return self.root / "source"

    @property
    def recipe(self) -> PurePosixPath:
        """Return the VM profile path."""
        return self.root / "recipe.yaml"

    @property
    def output(self) -> PurePosixPath:
        """Return the VM distribution directory."""
        return self.root / "distribution"


def remote_recipe_root(request: VmRequest, tag: str) -> PurePosixPath:
    """Return the sole VM directory this run may create and later remove."""
    if not tag.startswith("recipe-") or not tag.removeprefix("recipe-").isalnum():
        raise ValueError("Invalid recipe run tag")
    return PurePosixPath(vm_remote_home(request)) / f"nanolab-{tag}"


def _remote(
    provider: VmCommandProvider,
    request: VmRequest,
    argv: tuple[str, ...],
    *,
    remote_dir: str | None = None,
) -> object:
    result = provider.exec_argv(
        request, argv, env=None, remote_dir=remote_dir, dry_run=False
    )
    code = getattr(result, "return_code", 0)
    if code != 0:
        raise RuntimeError(
            f"VM command failed ({code}): {argv[0]}: "
            f"{getattr(result, 'stderr', '') or getattr(result, 'stdout', '')}"
        )
    return result


def _bundle_source(source: Path, destination: Path) -> None:
    """Archive staged inputs with self-contained Git metadata and no hooks or caches."""
    excluded = {
        ".gradle",
        "build",
        "__pycache__",
        ".git/hooks",
        ".git/logs",
        ".git/info",
        ".git/config",
        ".git/description",
        ".git/FETCH_HEAD",
        ".git/ORIG_HEAD",
    }

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = info.name.removeprefix("source/")
        parts = name.split("/")
        if any(part in {".gradle", "build", "__pycache__"} for part in parts):
            return None
        if any(name == entry or name.startswith(entry + "/") for entry in excluded):
            return None
        if name == ".git/objects/info/alternates":
            return None
        return info

    with tarfile.open(destination, "w:gz") as archive:
        archive.add(source, arcname="source", filter=keep)
        config = (
            b"[core]\n\trepositoryformatversion = 0\n"
            b"\tbare = false\n\tfilemode = true\n"
        )
        metadata = tarfile.TarInfo("source/.git/config")
        metadata.size = len(config)
        metadata.mode = 0o644
        archive.addfile(metadata, io.BytesIO(config))


def remote_recipe_run_resource(
    *,
    run: Resource[RecipeRun],
    provider: VmCommandProvider,
    request: VmRequest,
    run_dir: Path,
    tag: str,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RemoteRecipeRun]:
    """Upload only the captured source and profile into an owned VM directory."""

    def acquire(inputs: TaskInputs) -> RemoteRecipeRun:
        local = inputs.resource(run)
        root = remote_recipe_root(request, tag)
        remote = RemoteRecipeRun(local, root)
        run_dir.mkdir(parents=True, exist_ok=True)
        archive = run_dir / "source.tar.gz"
        _bundle_source(local.source_dir, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        recipe_digest = hashlib.sha256(local.recipe.read_bytes()).hexdigest()
        _remote(provider, request, ("mkdir", "-p", str(root)))
        for source, destination in (
            (archive, str(root / "source.tar.gz")),
            (local.recipe, str(remote.recipe)),
        ):
            result = provider.transfer_to(
                request, source=source, destination=destination
            )
            if getattr(result, "return_code", 0) != 0:
                raise RuntimeError(f"VM recipe transfer failed: {destination}")
        for path, expected in (
            (root / "source.tar.gz", digest),
            (remote.recipe, recipe_digest),
        ):
            result = _remote(provider, request, ("sha256sum", str(path)))
            if str(getattr(result, "stdout", "")).split()[0] != expected:
                raise RuntimeError(f"VM staged input hash differs: {path}")
        _remote(
            provider,
            request,
            ("tar", "-xzf", str(root / "source.tar.gz"), "-C", str(root)),
        )
        identity = json.loads(
            (local.output_dir.parent / "recipe-inputs.json").read_text()
        )
        revision = _remote(
            provider,
            request,
            ("git", "rev-parse", "HEAD"),
            remote_dir=str(remote.source),
        )
        if str(getattr(revision, "stdout", "")).strip() != identity["revision"]:
            raise RuntimeError("VM staged revision differs from captured source")
        patch = _remote(
            provider,
            request,
            ("git", "diff", "HEAD", "--binary", "--no-ext-diff"),
            remote_dir=str(remote.source),
        )
        if (
            hashlib.sha256(str(getattr(patch, "stdout", "")).encode()).hexdigest()
            != identity["patchSha256"]
        ):
            raise RuntimeError("VM staged tracked changes differ from captured source")
        return remote

    return Resource(
        title="Stage recipe in stack VM",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=(run, *requires),
    )


def remote_recipe_distribution_resource(
    *,
    run: Resource[RemoteRecipeRun],
    provider: VmCommandProvider,
    request: VmRequest,
    functions: tuple[tuple[str, str], ...],
    required_modules: frozenset[str],
    containerd_maven_repository: Path | None = None,
) -> Resource[RecipeDistribution]:
    """Publish in the VM, fetch evidence, then validate the host-side report."""

    def acquire(inputs: TaskInputs) -> RecipeDistribution:
        remote = inputs.resource(run)
        local = remote.local
        report = local.output_dir / "distribution.json"
        log = local.output_dir.parent / "gradle.log"
        remote_log = remote.root / "gradle.log"
        report.unlink(missing_ok=True)
        _remote(provider, request, ("mkdir", "-p", str(remote.output)))
        _remote(
            provider,
            request,
            ("rm", "-f", str(remote.output / "distribution.json")),
        )
        command = recipe_command(
            "publishRecipe",
            recipe=str(remote.recipe),
            output=str(remote.output),
            tag=local.tag,
            containerd_maven_repository=containerd_maven_repository,
        )
        try:
            run_remote_logged(
                provider,
                request,
                (command,),
                remote_dir=remote.source,
                remote_log=remote_log,
                local_log=log,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                VmFileFetcher(provider, request).fetch_from(
                    str(remote.output / "distribution.json"), report
                )
            raise
        VmFileFetcher(provider, request).fetch_from(
            str(remote.output / "distribution.json"), report
        )
        distribution = read_distribution(
            report, recipe=local.recipe, tag=local.tag, published=True
        )
        require_validation_distribution(
            distribution, functions=functions, required_modules=required_modules
        )
        return distribution

    return Resource(
        title="Publish staged recipe in stack VM",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=(run,),
    )


def cleanup_remote_recipe_run(
    run: RemoteRecipeRun, *, provider: VmCommandProvider, request: VmRequest
) -> None:
    """Delete only this run's VM staging directory after successful validation."""
    if run.root != remote_recipe_root(request, run.local.tag):
        raise ValueError("Remote recipe cleanup path does not match run")
    _remote(provider, request, ("rm", "-rf", "--", str(run.root)))
