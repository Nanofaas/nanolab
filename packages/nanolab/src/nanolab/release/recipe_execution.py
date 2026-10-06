"""Assemble release recipes against guarded source and independently verified images."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

from sonata_engine import Evidence, TaskInputs
from sonata_tasks.compensation import best_effort
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import CommandTaskSpec
from sonata_tasks.vm.logged import VmFileFetcher
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.config.environment import ExecutionRole
from nanolab.images.plan import ImageArchitecture
from nanolab.release.build import _provider_exec, _require_result
from nanolab.release.model import digest_path
from nanolab.release.recipe import ReleaseRecipeGroup, read_release_distribution
from nanolab.tasks.recipes.multiarch import unique_json_object
from nanolab.tasks.recipes.workflow import recipe_command
from nanolab.tasks.vm.models import VmRequest

# Collect archive and VM entries without following links.
_INVENTORY_SCRIPT = r"""
import hashlib, json, os, re, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
entries = {}
for directory, dirs, files in os.walk(root, followlinks=False):
    for name in sorted(dirs + files):
        path = Path(directory) / name
        info = path.lstat()
        entry = {"mode": stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            entry.update(type="symlink", target=os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            entry.update(type="directory")
        elif stat.S_ISREG(info.st_mode):
            h = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024*1024), b""):
                    h.update(chunk)
            entry.update(type="file", sha256=h.hexdigest())
        else:
            raise ValueError("Unsupported source entry: " + str(path))
        entries[str(path.relative_to(root))] = entry
caches = [".gradle"]
projects = {"."}
for name in ("settings.gradle", "settings.gradle.kts"):
    settings = root / name
    if settings.is_file() and not settings.is_symlink():
        text = settings.read_text()
        includes = re.findall(r"\binclude\s*(?:\(([^)]*)\)|([^\n;]+))", text)
        for parenthesized, plain in includes:
            for project in re.findall(r"['\"]([^'\"]+)['\"]", parenthesized or plain):
                projects.add(project.strip(":").replace(":", "/"))
        mapping = (
            r"project\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
            r"\.projectDir\s*=\s*file\(\s*['\"]([^'\"]+)['\"]\s*\)"
        )
        for project, directory in re.findall(mapping, text):
            projects.discard(project.strip(":").replace(":", "/"))
            projects.add(directory)
        if "it.unimib.datai.nanofaas.control-plane-modules" in text:
            for path in entries:
                parts = Path(path).parts
                if len(parts) == 4 and parts[:2] == ("platform", "modules"):
                    if parts[-1] == "module.properties":
                        projects.add(str(Path(path).parent))
        expression = r"includeBuild\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
        for path in re.findall(expression, text):
            included = Path(path)
            if included.is_absolute() or ".." in included.parts:
                raise ValueError("Unsafe included Gradle build")
            names = ("settings.gradle", "settings.gradle.kts")
            if any((root / included / f).is_file() for f in names):
                caches.append(str(included / ".gradle"))
                projects.add(str(included))
for project in projects:
    if Path(project).is_absolute() or ".." in Path(project).parts:
        raise ValueError("Unsafe Gradle project path")
payload = {
    "schema": 1, "entries": entries, "gradleCaches": sorted(set(caches)),
    "gradleProjects": sorted(projects),
}
Path(sys.argv[2]).write_text(
    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
"""

_FILE_FACTS = r"""
import hashlib, json, sys
from pathlib import Path
p = Path(sys.argv[1])
if p.is_symlink() or not p.is_file():
    raise ValueError("Not a regular evidence file")
h = hashlib.sha256()
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(1024*1024), b""):
        h.update(chunk)
print(json.dumps({"size": p.stat().st_size, "digest": "sha256:"+h.hexdigest()}))
"""


def _fetch_verified(
    provider: object, request: object, remote: str, local: Path, *, limit: int
) -> None:
    facts = json.loads(
        str(
            getattr(
                _provider_exec(
                    provider, request, ("python3", "-c", _FILE_FACTS, remote)
                ),
                "stdout",
                "",
            )
        )
    )
    if type(facts.get("size")) is not int or not 0 < facts["size"] <= limit:
        raise ValueError(f"Evidence transfer size out of bounds: {remote}")
    local.parent.mkdir(parents=True, exist_ok=True)
    partial = local.with_suffix(local.suffix + ".partial")
    partial.unlink(missing_ok=True)
    try:
        VmFileFetcher(
            cast(VmCommandProvider, provider), cast(VmRequest, request)
        ).fetch_from(remote, partial)
        after = json.loads(
            str(
                getattr(
                    _provider_exec(
                        provider, request, ("python3", "-c", _FILE_FACTS, remote)
                    ),
                    "stdout",
                    "",
                )
            )
        )
        if (
            facts != after
            or partial.stat().st_size != facts["size"]
            or digest_path(partial) != facts["digest"]
        ):
            raise ValueError(f"Evidence transfer digest/size differs: {remote}")
        partial.replace(local)
    finally:
        partial.unlink(missing_ok=True)


def _verify_remote_digest(
    provider: object, request: object, remote: str, digest: str, label: str
) -> None:
    actual = str(
        getattr(_provider_exec(provider, request, ("sha256sum", remote)), "stdout", "")
    ).split()
    if not actual or "sha256:" + actual[0] != digest:
        raise ValueError(f"Staged release {label} digest differs")


def capture_release_inventory(source_tree: Path, destination: Path) -> Path:
    """Freeze every archive entry, including tracked files inside build directories."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        (sys.executable, "-c", _INVENTORY_SCRIPT, str(source_tree), str(destination)),
        check=True,
    )
    return destination


def verify_release_source(
    *, inventory_file: Path, provider: object, request: object, source_dir: str
) -> None:
    """Reject source changes and additions except source-derived build outputs."""
    source = Path(source_dir)
    remote_inventory = str(source.parent / "source-current.json")
    current_file = inventory_file.with_name("source-current.json")
    _provider_exec(
        provider,
        request,
        ("python3", "-c", _INVENTORY_SCRIPT, source_dir, remote_inventory),
    )
    _fetch_verified(
        provider, request, remote_inventory, current_file, limit=20 * 1024 * 1024
    )
    inventory = json.loads(
        inventory_file.read_text(), object_pairs_hook=unique_json_object
    )
    expected = inventory["entries"]
    current = json.loads(
        current_file.read_text(), object_pairs_hook=unique_json_object
    )["entries"]
    writable = set(inventory["gradleCaches"])
    writable.update(
        str(Path(project) / "build") for project in inventory["gradleProjects"]
    )
    for name, entry in expected.items():
        if current.get(name) != entry:
            raise ValueError(f"Release source inventory changed: {name}")
    for name, entry in current.items():
        if entry["type"] == "symlink":
            target = Path(entry["target"])
            # Even generated links must stay within their permitted output subtree.
            resolved = (source / name).parent / target
            normalized = Path(os.path.normpath(resolved))
            if target.is_absolute() or not normalized.is_relative_to(source):
                raise ValueError(f"Release source symlink escapes: {name}")
            if name not in expected and not any(
                normalized.is_relative_to(source / root)
                for root in writable
                if name == root or name.startswith(root + "/")
            ):
                raise ValueError(f"Release source output symlink escapes: {name}")
        if name not in expected and not any(
            name == root or name.startswith(root + "/") for root in writable
        ):
            raise ValueError(f"Unexpected release source entry: {name}")


def release_recipe_commands(
    groups: tuple[ReleaseRecipeGroup, ...],
    *,
    source_dir: str,
    remote_root: str,
    builder_name: str,
    architecture: ImageArchitecture = "amd64",
    role: ExecutionRole = "stack",
) -> tuple[CommandTaskSpec, ...]:
    """Return root Gradle assemblies with separate owned profile and output paths."""
    if (architecture, role) not in {("amd64", "stack"), ("arm64", "arm-builder")}:
        raise ValueError("Unsupported release recipe architecture/role pair")
    if not groups or any(
        not group.cells
        or any(cell.architecture != architecture for cell in group.cells)
        for group in groups
    ):
        raise ValueError("Release recipe cells differ from execution architecture")
    return tuple(
        CommandTaskSpec(
            task_id=f"release.{architecture}.recipe.{group.flavor}",
            summary=f"Assemble {architecture.upper()} {group.flavor} recipe",
            argv=recipe_command(
                "assembleRecipe",
                recipe=f"{remote_root}/recipe-inputs/{architecture}/{group.name}.yaml",
                output=f"{remote_root}/recipe-output/{architecture}/{group.flavor}",
                tag=group.tag,
            ),
            role=role,
            options=CommandOptions(
                remote_dir=source_dir,
                env={"DOCKER_BUILDKIT": "1", "BUILDX_BUILDER": builder_name},
            ),
        )
        for group in groups
    )


def run_release_recipe_steps(
    inputs: TaskInputs,
    *,
    groups: tuple[ReleaseRecipeGroup, ...],
    executor: RoleBoundCommandTaskExecutor,
    provider: object,
    request: object,
    source_dir: str,
    remote_root: str,
    evidence_dir: Path,
    inventory_file: Path,
    source_commit: str,
    archive_digest: str,
    builder_name: str,
    inputs_dir: Path | None = None,
    architecture: ImageArchitecture = "amd64",
    role: ExecutionRole = "stack",
) -> tuple[Evidence, ...]:
    """Require successful fresh reports, complete logs and matching local image IDs."""
    del inputs
    commands = release_recipe_commands(
        groups,
        source_dir=source_dir,
        remote_root=remote_root,
        builder_name=builder_name,
        architecture=architecture,
        role=role,
    )
    verify_release_source(
        inventory_file=inventory_file,
        provider=provider,
        request=request,
        source_dir=source_dir,
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if inputs_dir is not None:
        for name in (
            f"buildkitd-{architecture}.toml",
            *(f"{g.name}.yaml" for g in groups),
        ):
            shutil.copyfile(inputs_dir / name, evidence_dir / name)
    retained_inventory = inventory_file
    if inputs_dir is not None:
        retained_inventory = evidence_dir / "source-inventory.json"
        shutil.copyfile(inventory_file, retained_inventory)
    evidence = [
        Evidence(
            "file-digest", str(retained_inventory), digest_path(retained_inventory)
        )
    ]
    config = evidence_dir / f"buildkitd-{architecture}.toml"
    evidence.append(Evidence("file-digest", str(config), digest_path(config)))
    _verify_remote_digest(
        provider, request, f"{remote_root}/source.tar", archive_digest, "archive"
    )
    _verify_remote_digest(
        provider,
        request,
        f"{remote_root}/recipe-inputs/{architecture}/{config.name}",
        digest_path(config),
        "configuration",
    )
    facts = {
        "architecture": architecture,
        "role": role,
        "sourceCommit": source_commit,
        "archiveDigest": archive_digest,
        "inventoryDigest": digest_path(inventory_file),
        "builder": builder_name,
        "driver": "docker-container",
        "driverOptions": ["default-load=true"],
        "commands": [
            {"argv": c.argv, "env": dict(c.options.env), "cwd": c.options.remote_dir}
            for c in commands
        ],
    }
    for label, argv in (
        ("host", ("uname", "-m")),
        ("hostOS", ("uname", "-s")),
        ("daemon", ("docker", "info", "--format={{.OSType}}|{{.Architecture}}")),
        ("docker", ("docker", "version")),
        ("buildx", ("docker", "buildx", "version")),
        ("builderInspection", ("docker", "buildx", "inspect", builder_name)),
    ):
        facts[label] = str(
            getattr(_provider_exec(provider, request, argv), "stdout", "")
        ).strip()
    aliases = {"amd64": {"x86_64", "amd64"}, "arm64": {"aarch64", "arm64"}}[
        architecture
    ]
    if (
        facts["hostOS"] != "Linux"
        or facts["host"] not in aliases
        or facts["daemon"] not in {"linux|" + alias for alias in aliases}
    ):
        raise ValueError(
            f"Recipe builder requires native Linux {architecture.upper()} host/daemon"
        )
    facts_file = evidence_dir / "build-facts.json"
    facts_file.write_text(json.dumps(facts, sort_keys=True, indent=2) + "\n")
    evidence.append(Evidence("file-digest", str(facts_file), digest_path(facts_file)))
    for group, command in zip(groups, commands, strict=True):
        profile = evidence_dir / f"{group.name}.yaml"
        if profile.read_bytes() != group.profile_bytes:
            raise ValueError("Frozen recipe profile changed")
        remote_profile = f"{remote_root}/recipe-inputs/{architecture}/{profile.name}"
        staged_hash = str(
            getattr(
                _provider_exec(provider, request, ("sha256sum", remote_profile)),
                "stdout",
                "",
            )
        ).split()[0]
        if "sha256:" + staged_hash != group.profile_digest:
            raise ValueError("Staged recipe profile differs")
        evidence.append(Evidence("file-digest", str(profile), group.profile_digest))
        output = f"{remote_root}/recipe-output/{architecture}/{group.flavor}"
        local_output = evidence_dir / group.flavor
        report = local_output / "distribution.json"
        report.unlink(missing_ok=True)
        _provider_exec(provider, request, ("rm", "-rf", "--", output))
        _provider_exec(provider, request, ("mkdir", "-p", output))
        log = local_output / "gradle.log"
        log.unlink(missing_ok=True)
        # cleanRecipe claims/clears its output; logging must stay outside it.
        log_dir = f"{remote_root}/recipe-output/{architecture}/logs"
        log_path = f"{log_dir}/{group.flavor}.log"
        _provider_exec(provider, request, ("mkdir", "-p", log_dir))
        _provider_exec(provider, request, ("rm", "-f", "--", log_path))
        remote_log = shlex.quote(log_path)
        script = (
            f"{{ {shlex.join(command.argv)}; }} > {remote_log} 2>&1; "
            f'result=$?; tail -c 65536 {remote_log}; exit "$result"'
        )
        try:
            result = executor.run(replace(command, argv=("sh", "-c", script)))
            _require_result(result, "release recipe assembly")
        except BaseException as error:
            best_effort(
                error,
                lambda log_path=log_path, log=log: _fetch_verified(
                    provider,
                    request,
                    log_path,
                    log,
                    limit=100 * 1024 * 1024,
                ),
                what="recipe failure diagnostics",
            )
            raise
        _fetch_verified(provider, request, log_path, log, limit=100 * 1024 * 1024)
        _fetch_verified(
            provider, request, output + "/distribution.json", report, limit=1024 * 1024
        )
        _verify_remote_digest(
            provider, request, remote_profile, group.profile_digest, "profile"
        )
        _verify_remote_digest(
            provider, request, f"{remote_root}/source.tar", archive_digest, "archive"
        )
        components = read_release_distribution(
            report, group=group, source_commit=source_commit
        )
        inspect = _provider_exec(
            provider,
            request,
            (
                "docker",
                "image",
                "inspect",
                "--format={{.Os}}|{{.Architecture}}|{{.Id}}",
                *(c.image.reference for c in components),
            ),
        )
        rows = str(getattr(inspect, "stdout", "")).strip().splitlines()
        if len(rows) != len(components):
            raise ValueError("Daemon image inspection coverage differs")
        for row, component in zip(rows, components, strict=True):
            if row != f"linux|{architecture}|{component.image.id}":
                raise ValueError(
                    f"Daemon image/platform differs: {component.image.reference}"
                )
            evidence.append(
                Evidence(
                    "local-image-digest",
                    "docker-daemon:" + component.image.reference,
                    component.image.id,
                )
            )
        evidence.extend(
            Evidence("file-digest", str(p), digest_path(p)) for p in (report, log)
        )
        verify_release_source(
            inventory_file=inventory_file,
            provider=provider,
            request=request,
            source_dir=source_dir,
        )
    images = [item for item in evidence if item.kind == "local-image-digest"]
    if len(images) != len({item.reference for item in images}):
        raise ValueError("Duplicate release recipe image coverage")
    inspected = _provider_exec(
        provider,
        request,
        (
            "docker",
            "image",
            "inspect",
            "--format={{.Os}}|{{.Architecture}}|{{.Id}}",
            *(item.reference.removeprefix("docker-daemon:") for item in images),
        ),
    )
    rows = str(getattr(inspected, "stdout", "")).strip().splitlines()
    if rows != [f"linux|{architecture}|{item.digest}" for item in images]:
        raise ValueError("Final release image IDs/platforms differ from reports")
    return tuple(evidence)
