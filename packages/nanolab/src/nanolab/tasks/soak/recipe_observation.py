"""Bind one recipe publication to actual compiler and BuildKit observations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import yaml
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec
from sonata_tasks.sources import source_entry

from nanolab.tasks.recipes.multiarch import (
    MultiarchDistribution,
    read_buildx_distribution,
    unique_json_object,
)
from nanolab.tasks.recipes.workflow import _object, recipe_command
from nanolab.tasks.soak.artifacts import (
    ArtifactWriter,
    describe_artifact,
    enforce_limit,
    fingerprint,
)
from nanolab.tasks.soak.build_executor import (
    BuildCommandError,
    OwnedBuildCommandExecutor,
)
from nanolab.tasks.soak.build_observation import (
    _GRADLE_INIT,
    _GRADLE_OPTIONS,
    _NODE_PRELOAD,
    BuildObservationCapture,
)
from nanolab.tasks.soak.build_provenance import (
    BuildProvenanceCollector,
    _materials,
    _predicate,
)
from nanolab.tasks.soak.builds import ObservedBuild
from nanolab.tasks.soak.images import BuildRecipe
from nanolab.tasks.soak.sources import SourceSnapshot, verify_snapshot
from nanolab.workspace.recipe import RecipeRun


def verify_recipe_source(
    snapshot: SourceSnapshot,
    workspace: Path,
    *,
    generated_inputs: frozenset[str] = frozenset(),
) -> None:
    """Check original inputs independently of generated build files."""
    verify_snapshot(snapshot)
    paths = {entry.path for entry in snapshot.entries}
    if any(
        source_entry(workspace, entry.path, paths) != entry
        for entry in snapshot.entries
    ):
        raise ValueError("Recipe source inputs changed across publication")
    outputs = {".git", ".gradle", "build"}
    outputs.update(
        (Path(entry.path).parent / output).as_posix()
        for entry in snapshot.entries
        if Path(entry.path).name in {"build.gradle", "build.gradle.kts"}
        for output in ("build", ".gradle")
    )
    for parent, directories, files in os.walk(workspace, followlinks=False):
        base = Path(parent)
        for name in [*directories, *files]:
            item = base / name
            relative = item.relative_to(workspace).as_posix()
            if relative in outputs:
                if name in directories:
                    directories.remove(name)
                continue
            if (
                item.is_file() or item.is_symlink()
            ) and relative not in paths | generated_inputs:
                raise ValueError("Recipe source inputs added across publication")


def verify_recipe_provenance(local: dict, published: dict) -> None:
    """Apply the collector's material and invocation binding to saved predicates."""
    if _materials(local) != _materials(published):
        raise ValueError("Recipe observed base materials differ from registry")
    local_id = local.get("metadata", {}).get("buildInvocationID")
    if local_id is not None and local_id != published.get("metadata", {}).get(
        "buildInvocationID"
    ):
        raise ValueError("Recipe metadata and published build invocation differ")


def _read_record(root: Path, descriptor: dict[str, object], limit: int) -> bytes:
    name = descriptor.get("path")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("Recipe observation log path escapes its owner")
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Recipe observation log is unavailable or oversized")
    body = path.read_bytes()
    if descriptor.get("sha256") != hashlib.sha256(body).hexdigest() or descriptor.get(
        "size_bytes"
    ) != len(body):
        raise ValueError("Recipe observation log identity differs from record")
    return body


def _build_inputs(argv: list[str]) -> list[str]:
    """Compare compilation inputs independently of publication/export metadata."""
    output = []
    index = 0
    while index < len(argv):
        if argv[index] == "--metadata-file":
            index += 2
        elif argv[index] == "--push":
            index += 1
        else:
            output.append(argv[index])
            index += 1
    return output


def observe_soak_recipe(
    run: RecipeRun,
    snapshot: SourceSnapshot,
    *,
    executor: OwnedBuildCommandExecutor,
    builder: str,
    artifact_limit_bytes: int,
    budget_root: Path,
) -> tuple[MultiarchDistribution, dict[str, ObservedBuild]]:
    """Instrument disposable inputs, publish once and validate observations."""
    root = run.recipe.parent
    directory = root / "observer"
    writer = ArtifactWriter(directory, artifact_limit_bytes, budget_root=budget_root)
    token = uuid4().hex
    marker = "NANOLAB_BUILD_OBSERVATION_" + token + ":"
    profile_bytes = run.recipe.read_bytes()
    profile = _object(yaml.safe_load(profile_bytes), "recipe")
    platform = _object(profile.get("registry"), "registry")["platforms"][0]
    docker = shutil.which("docker")
    if docker is None:
        writer.close()
        raise ValueError("Recipe observation requires Docker")
    try:
        verify_recipe_source(snapshot, run.source_dir)
        init = writer.write_blob(
            "files",
            "observe.gradle",
            _GRADLE_INIT.replace("__MARKER__", json.dumps(marker))
            .replace("__STAGE__", "null")
            .encode(),
        )
        generated = run.source_dir / (".nanolab-recipe-" + token)
        generated.mkdir()
        hook_body = (
            _NODE_PRELOAD.replace("__MARKER__", json.dumps(marker))
            .replace("__STAGE__", '"build"')
            .encode()
        )
        hook = writer.write_blob("files", "observe-node.cjs", hook_body)
        (generated / hook.name).write_bytes(hook_body)
        node_source = run.source_dir / "functions/javascript/word-stats/Dockerfile"
        lines = []
        stage = None
        inserted = False
        for line in node_source.read_text().splitlines():
            if re.match(r"(?i)^\s*(SHELL|ONBUILD)\s", line):
                raise ValueError("Unsupported recipe Node build shell observation")
            if line.upper().startswith("FROM "):
                match = re.search(r"(?i)\sAS\s+(\S+)\s*$", line)
                stage = match.group(1) if match else None
            if line.startswith("RUN npm "):
                if stage != "build":
                    raise ValueError(
                        "Recipe Node compilation requires the observed build stage"
                    )
                if not inserted:
                    lines.append(
                        f"COPY {generated.name}/{hook.name} /nanolab-observe-node.cjs"
                    )
                    inserted = True
                line = (
                    'RUN NODE_OPTIONS="--require=/nanolab-observe-node.cjs '
                    '${NODE_OPTIONS:-}" ' + line[4:]
                )
            lines.append(line)
        if not inserted:
            raise ValueError("Recipe Node compilation observation is unavailable")
        node_dockerfile = writer.write_blob(
            "files", "Dockerfile.node", ("\n".join(lines) + "\n").encode()
        )
        request = {
            "request_id": token,
            "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
            "source_fingerprint": snapshot.fingerprint,
            "workspace": str(run.source_dir),
            "directory": str(directory),
            "docker": docker,
            "node_source": str(node_source),
            "node_dockerfile": str(node_dockerfile),
            "artifact_limit_bytes": artifact_limit_bytes
            - min(4096, artifact_limit_bytes // 8),
            "budget_root": str(budget_root),
        }
        request_path = writer.write_json("request.json", request)
        asset = (
            Path(__file__).resolve().parents[2]
            / "assets/diagnostics/recipe-docker-observer.py"
        )
        observer = writer.write_blob("files", "docker-observer.py", asset.read_bytes())
        launcher = writer.write_blob(
            "files",
            "docker-observe",
            (
                "#!/bin/sh\nexec "
                + " ".join(
                    shlex.quote(str(value))
                    for value in (sys.executable, observer, request_path)
                )
                + ' "$@"\n'
            ).encode(),
        )
        launcher.chmod(0o700)
        instrumentation = tuple(
            describe_artifact(path)
            for path in (
                init,
                hook,
                generated / hook.name,
                node_dockerfile,
                observer,
                launcher,
            )
        )
        argv = (
            *recipe_command(
                "publishRecipe",
                recipe=str(run.recipe),
                output=str(root / "workspace-distribution"),
                tag=run.tag,
                builder=builder,
            ),
            f"-PrecipeDocker={launcher}",
            "--init-script",
            str(init),
            *_GRADLE_OPTIONS,
        )
        revision = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=run.source_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ("git", "status", "--porcelain"),
                cwd=run.source_dir,
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
        )
        publication_request = writer.write_json(
            "publication-request.json",
            {
                **request,
                "argv": list(argv),
                "builder": builder,
                "instrumentation": instrumentation,
                "original_node_dockerfile": describe_artifact(node_source),
            },
        )
        result = executor.run(
            CommandTaskSpec(
                "publish-soak-recipe",
                "Publish all soak application images",
                argv,
                options=CommandOptions(
                    cwd=run.source_dir, env={"BUILDX_METADATA_PROVENANCE": "max"}
                ),
            )
        )
        if (
            result.status != "passed"
            or executor.last_log_path is None
            or executor.last_result is None
        ):
            raise BuildCommandError(f"Recipe publication failed: {result.stderr}")
        state = executor.last_result
        if (
            state.returncode != 0
            or not state.reaped
            or state.forced_stop
            or state.errors
        ):
            raise BuildCommandError("Recipe publication lacks complete owned execution")
        publication_log = writer.write_blob(
            "execution", "publication.log", executor.last_log_path.read_bytes()
        )
        verify_recipe_source(
            snapshot,
            run.source_dir,
            generated_inputs=frozenset(
                {(generated / hook.name).relative_to(run.source_dir).as_posix()}
            ),
        )
        if any(
            describe_artifact(Path(str(item["path"]))) != item
            for item in instrumentation
        ):
            raise ValueError(
                "Recipe observation instrumentation changed across publication"
            )
        if run.recipe.read_bytes() != profile_bytes:
            raise ValueError("Recipe profile changed across publication")
        output_writer = ArtifactWriter(
            run.output_dir, artifact_limit_bytes, budget_root=budget_root
        )
        try:
            output_writer.write_file(
                "distribution.json",
                (root / "workspace-distribution/distribution.json").read_bytes(),
            )
        finally:
            output_writer.close()
        distribution = read_buildx_distribution(
            run.output_dir / "distribution.json",
            recipe=run.recipe,
            tag=run.tag,
            expected_source={"revision": revision, "dirty": dirty},
            platforms=frozenset({platform}),
            provenance=True,
        )
        records = []
        for line in (directory / "commands.jsonl").read_text().splitlines():
            record = _object(
                json.loads(line, object_pairs_hook=unique_json_object), "observation"
            )
            if (
                any(
                    record.get(key) != request[key]
                    for key in (
                        "request_id",
                        "profile_sha256",
                        "source_fingerprint",
                        "workspace",
                    )
                )
                or record.get("cwd") != str(run.source_dir)
                or record.get("exit_code") != 0
            ):
                raise ValueError("Recipe command observation is unbound or failed")
            records.append(record)
        builder_log = executor.observe(("docker", "buildx", "inspect", builder), 30)
        builder_path = writer.write_blob("execution", "builder.log", builder_log)
        outputs = {}
        publication_text = publication_log.read_text()
        java_lines = []
        seen_tasks = set()
        for line in publication_text.splitlines():
            if line.startswith(marker):
                frame = _object(
                    json.loads(base64.b64decode(line[len(marker) :], validate=True)),
                    "compiler frame",
                )
                if frame.get("toolchain") in {"java", "gradle"}:
                    java_lines.append(line)
                    if frame.get("toolchain") == "java":
                        seen_tasks.add(frame.get("task"))
        host_log = writer.write_blob(
            "execution", "host-compilers.log", ("\n".join(java_lines) + "\n").encode()
        )
        for component in distribution.components:
            role = (
                "control-plane"
                if component.kind == "control-plane"
                else f"{component.name}-{component.sdk}"
            )
            matches = [
                record
                for record in records
                if "--push" in record.get("actual", [])
                and component.image.reference in record["actual"]
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Recipe image publication observation is absent or ambiguous"
                )
            record = matches[0]
            actual = record["actual"]
            if (
                actual[:2] != ["buildx", "build"]
                or actual[actual.index("--builder") + 1] != builder
                or "--provenance=mode=max" not in actual
            ):
                raise ValueError(
                    "Recipe publication builder/provenance differs from request"
                )
            metadata = _object(
                json.loads(
                    _read_record(
                        directory,
                        _object(record.get("metadata"), "metadata observation"),
                        artifact_limit_bytes,
                    ),
                    object_pairs_hook=unique_json_object,
                ),
                "metadata",
            )
            if (
                metadata.get("containerimage.digest") != component.image.digest
                or metadata.get("image.name") != component.image.reference
                or _object(
                    metadata.get("containerimage.descriptor"), "image descriptor"
                ).get("digest")
                != component.image.digest
            ):
                raise ValueError("Recipe image metadata differs from publication")
            build_ref = metadata.get("buildx.build.ref")
            if (
                not isinstance(build_ref, str)
                or len(build_ref.split("/")) != 3
                or build_ref.split("/")[0] != builder
            ):
                raise ValueError("Recipe builder metadata differs from publication")
            predicate = _predicate(
                metadata.get("buildx.build.provenance"),
                platform,
                component.image.manifests[platform],
            )
            bases = _materials(predicate)
            jvm = component.sdk == "java"
            publication_record = record
            if not jvm:
                assembly = [
                    candidate
                    for candidate in records
                    if "--push" not in candidate["actual"]
                    and _build_inputs(candidate["actual"]) == _build_inputs(actual)
                ]
                if len(assembly) != 1 or records.index(assembly[0]) >= records.index(
                    record
                ):
                    raise ValueError(
                        "Recipe compiler build is not bound to publication inputs"
                    )
                record = assembly[0]
                actual = record["actual"]
            recipe = BuildRecipe(
                role,
                "build",
                "jvm" if jvm else "default",
                platform,
                component.image.reference,
                argv if jvm else None,
                None,
                fingerprint(
                    {
                        "profile": request["profile_sha256"],
                        "source": snapshot.fingerprint,
                        "command": actual,
                        "publication_command": publication_record["actual"],
                        "instrumentation": instrumentation,
                    }
                ),
                None,
            )
            capture = BuildObservationCapture(
                recipe,
                root / (role + "-metadata.json"),
                run.source_dir,
                source_fingerprint=snapshot.fingerprint,
                artifact_limit_bytes=artifact_limit_bytes,
                budget_root=budget_root,
            )
            capture.marker = marker
            descriptor = _object(record.get("log"), "command log")
            body = _read_record(directory, descriptor, artifact_limit_bytes)
            command_log = writer.write_blob("execution", role + "-build.log", body)
            capture.commands = [
                {
                    "kind": "build",
                    "argv": actual,
                    "cwd": str(run.source_dir),
                    "exit_code": 0,
                    "log": describe_artifact(command_log),
                }
            ]
            if jvm:
                task = (
                    ":control-plane:compileJava"
                    if role == "control-plane"
                    else ":functions:java:word-stats:compileJava"
                )
                if task not in seen_tasks:
                    raise ValueError(
                        f"Recipe compiler observation is missing for {role}"
                    )
                capture.commands.append(
                    {
                        "kind": "prerequisite",
                        "argv": list(argv),
                        "cwd": str(run.source_dir),
                        "exit_code": 0,
                        "log": describe_artifact(host_log),
                    }
                )
                capture.capture_toolchains("prerequisite")
            else:
                capture.capture_toolchains("build", materials=bases)
            node = build_ref.split("/")[1]
            sections = re.split(r"(?m)(?=^\s*Name:\s*\S+\s*$)", builder_log.decode())
            selected = [
                section
                for section in sections
                if re.match(r"\s*Name:\s*" + re.escape(node) + r"\s*\n", section)
                and re.search(r"(?m)^\s*BuildKit(?: version)?:", section)
            ]
            if len(selected) != 1:
                raise ValueError("Recipe builder node observation is missing")
            node_log = writer.write_blob(
                "execution", role + "-builder.log", selected[0].encode()
            )
            capture.commands.append(
                {
                    "kind": "toolchain",
                    "toolchain": "buildkit",
                    "argv": ["docker", "buildx", "inspect", builder],
                    "cwd": str(run.source_dir),
                    "scope": "builder",
                    "exit_code": 0,
                    "build_ref": build_ref,
                    "log": describe_artifact(node_log),
                }
            )
            request_commands = {
                "workspace": str(run.source_dir),
                "build_argv": actual,
                "prerequisite_argv": list(argv) if jvm else [],
            }
            # Share the collector's strict internal command validation.
            tools, logs = BuildProvenanceCollector(executor.observe)._commands(  # noqa: SLF001
                {"commands": capture.commands},
                request_commands,
                recipe,
                build_ref,
                bases,
                root,
            )
            observation = writer.write_json(
                role + "-observations.json",
                {
                    "source_fingerprint": snapshot.fingerprint,
                    "request_id": token,
                    "profile_sha256": request["profile_sha256"],
                    "effective_recipe_fingerprint": recipe.recipe_fingerprint,
                    "image_digest": component.image.digest,
                    "commands": capture.commands,
                    "publication": publication_record,
                    "recipe": asdict(recipe),
                },
            )
            outputs[role] = ObservedBuild(
                component.image.digest,
                tools,
                bases,
                tuple(
                    dict.fromkeys(
                        (
                            *logs,
                            publication_log,
                            observation,
                            request_path,
                            publication_request,
                            builder_path,
                            directory / publication_record["metadata"]["path"],
                            directory / publication_record["log"]["path"],
                            init,
                            hook,
                            node_dockerfile,
                            observer,
                            launcher,
                            *capture.evidence_paths,
                        )
                    )
                ),
            )
        enforce_limit(root, artifact_limit_bytes)
        return distribution, outputs
    finally:
        writer.close()
