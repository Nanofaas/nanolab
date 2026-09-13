"""Collect published BuildKit provenance and request-bound build-stage evidence.

Wiring contract:
1. Write the bake file, then call ``write_observation_request`` before execution.
2. The build-stage executor writes ``<metadata>.observations.json``. It records
   schema ``nanolab-soak-build-observations-v1``, request_id, recipe_fingerprint,
   workspace, image_digest and commands. Each successful command records kind
   (build/prerequisite/toolchain), argv, cwd, exit_code and log {path, sha256,
   size_bytes}. Paths are relative to the metadata directory or absolute within it.
3. Toolchain entries additionally record toolchain and scope. JVM java/gradle
   evidence has scope host; native-image/node has scope build and an immutable
   material reference present in the attestation. BuildKit has scope builder and
   build_ref matching Buildx metadata. Versions are parsed from log bytes.
4. Pass ``BuildProvenanceCollector(runner).collect`` to BuildImagesTask.

The executor is the trusted observer: it must capture the actual compiler used
by Gradle, not an unrelated PATH java, and run version captures in the same
build context. The request helper observes no commands and certifies nothing.
No Dockerfile, tag text, requested build arg, or caller-authored version is used
as effective provenance. The collector does not verify publisher signatures.

Docker's public interfaces are documented at:
https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/
https://docs.docker.com/reference/cli/docker/buildx/build/
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from nanolab.tasks.soak.artifacts import fingerprint
from nanolab.tasks.soak.builds import ObservedBuild
from nanolab.tasks.soak.images import BuildRecipe

CommandRunner = Callable[[tuple[str, ...], float], bytes]
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_LIMIT = 16 * 1024 * 1024
_BUILD_TYPE = "https://mobyproject.org/buildkit@v1"
_VERSIONS = {
    "java": re.compile(r'(?m)^(?:openjdk|java) (?:version )?"?([0-9][^\s"]*)'),
    "gradle": re.compile(r"(?m)^Gradle ([0-9][^\s]*)\s*$"),
    "native-image": re.compile(r"(?m)^native-image ([0-9][^\s]*)"),
    "node": re.compile(r"(?m)^v([0-9]+\.[0-9]+\.[0-9]+[^\s]*)\s*$"),
    "buildkit": re.compile(r"(?m)^\s*BuildKit(?: version)?:\s*(v?[0-9][^\s]*)\s*$"),
}


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _decode(body: bytes, limit: int = _LIMIT) -> dict[str, Any]:
    if not isinstance(body, bytes) or len(body) > limit:
        raise ValueError("provenance output exceeds byte limit or is not bytes")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate provenance JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"nonfinite provenance JSON: {value}")

    return _mapping(
        json.loads(body, object_pairs_hook=pairs, parse_constant=invalid), "provenance"
    )


def _safe_file(path: Path, root: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("parent traversal in evidence path is unsupported")
    path = path.absolute()
    root = root.resolve()
    if not path.is_relative_to(root):
        raise ValueError("evidence path escapes metadata directory")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("symlink evidence is unsupported")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("evidence must be a regular file")
    return path


def _read(path: Path, root: Path, limit: int = _LIMIT) -> bytes:
    path = _safe_file(path, root)
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError("provenance file exceeds byte limit")
    return body


def _persist(path: Path, body: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def _sidecar(metadata: Path, suffix: str) -> Path:
    return metadata.with_name(metadata.name + suffix)


def _recipe_target(recipe: BuildRecipe) -> str:
    if recipe.mode != "build" or recipe.bake is None:
        raise ValueError("collector requires a build recipe, not prebuilt")
    targets = _mapping(recipe.bake.get("target"), "bake targets")
    if len(targets) != 1:
        raise ValueError("one unambiguous bake target is required")
    name, raw = next(iter(targets.items()))
    target = _mapping(raw, "bake target")
    if target.get("tags") != [recipe.image] or target.get("platforms") != [
        recipe.platform
    ]:
        raise ValueError("bake target image/platform differs from recipe")
    if (
        not recipe.image
        or re.search(r"\s|@", recipe.image)
        or recipe.image.startswith("-")
    ):
        raise ValueError("invalid build image reference")
    return name


def _option(argv: tuple[str, ...], option: str) -> str:
    if argv.count(option) != 1:
        raise ValueError(f"build command requires exactly one {option}")
    index = argv.index(option)
    if index + 1 == len(argv):
        raise ValueError(f"build command lacks {option} value")
    return argv[index + 1]


def write_observation_request(
    recipe: BuildRecipe,
    metadata: Path,
    workspace: Path,
    *,
    build_argv: tuple[str, ...],
) -> Path:
    """Exclusively bind an upcoming build command; execute no tools or builds."""
    _recipe_target(recipe)
    metadata, workspace = metadata.absolute(), workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("build workspace must be a directory")
    if build_argv[:3] != ("docker", "buildx", "bake"):
        raise ValueError("observed build command must be docker buildx bake")
    if "--push" not in build_argv or "--provenance=mode=max" not in build_argv:
        raise ValueError("build must publish maximum provenance")
    if Path(_option(build_argv, "--metadata-file")).absolute() != metadata:
        raise ValueError("build command metadata path differs from request")
    bake = Path(_option(build_argv, "-f"))
    if _decode(_read(bake, metadata.parent)) != recipe.bake:
        raise ValueError("actual bake file differs from requested recipe")
    request = {
        "schema": "nanolab-soak-build-request-v1",
        "request_id": uuid4().hex,
        "role": recipe.role,
        "recipe_fingerprint": recipe.recipe_fingerprint,
        "bake_fingerprint": fingerprint(recipe.bake or {}),
        "platform": recipe.platform,
        "image": recipe.image,
        "metadata": str(metadata),
        "workspace": str(workspace),
        "build_argv": list(build_argv),
        "prerequisite_argv": list(recipe.prerequisite_argv or ()),
    }
    path = _sidecar(metadata, ".request.json")
    _persist(path, json.dumps(request, sort_keys=True, allow_nan=False).encode())
    return path


def _platform(value: object) -> str:
    platform = _mapping(value, "image platform")
    parts = [platform.get("os"), platform.get("architecture")]
    variant = platform.get("variant")
    if variant:
        parts.append(variant)
    if any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("effective platform is unavailable")
    result = "/".join(str(part) for part in parts)
    return "linux/arm64" if result == "linux/arm64/v8" else result


def _expected_platform(platform: str) -> str:
    return "linux/arm64" if platform == "linux/arm64/v8" else platform


def _predicate(value: object, platform: str, subject: str) -> dict[str, Any]:
    data = _mapping(value, "published provenance")
    if platform in data:
        data = _mapping(data[platform], "platform provenance")
    elif platform == "linux/arm64/v8" and "linux/arm64" in data:
        data = _mapping(data["linux/arm64"], "platform provenance")
    if "SLSA" in data:
        data = _mapping(data["SLSA"], "SLSA provenance")
    if "predicate" in data:
        if data.get("predicateType") not in {
            "https://slsa.dev/provenance/v0.2",
            "https://slsa.dev/provenance/v1",
        }:
            raise ValueError("unsupported provenance predicate type")
        subjects = data.get("subject")
        if not isinstance(subjects, list) or not any(
            isinstance(item, dict)
            and item.get("digest", {}).get("sha256") == subject.removeprefix("sha256:")
            for item in subjects
        ):
            raise ValueError(
                "provenance subject differs from published platform digest"
            )
        data = _mapping(data["predicate"], "SLSA predicate")
    definition = _mapping(data.get("buildDefinition", data), "build definition")
    if definition.get("buildType") != _BUILD_TYPE:
        raise ValueError("published BuildKit provenance is missing or unsupported")
    return data


def _materials(predicate: dict[str, Any]) -> dict[str, str]:
    definition = predicate.get("buildDefinition")
    materials = (
        definition.get("resolvedDependencies")
        if isinstance(definition, dict)
        else predicate.get("materials")
    )
    if not isinstance(materials, list) or len(materials) > 4096:
        raise ValueError("bounded provenance materials are required")
    bases = {}
    for raw in materials:
        material = _mapping(raw, "material")
        uri = material.get("uri")
        if not isinstance(uri, str):
            raise ValueError("material URI is missing")
        if not uri.startswith("pkg:docker/"):
            continue
        digest = _mapping(material.get("digest"), "Docker material digest").get(
            "sha256"
        )
        if not isinstance(digest, str) or _HEX.fullmatch(digest) is None:
            raise ValueError("Docker material requires an observed SHA-256 digest")
        name = unquote(
            uri.removeprefix("pkg:docker/").split("?", 1)[0].split("#", 1)[0]
        ).rsplit("@", 1)[0]
        if not name or re.search(r"\s|@|://", name) or name.startswith(("/", "-")):
            raise ValueError("invalid Docker material image identity")
        value = name + "@sha256:" + digest
        if uri in bases and bases[uri] != value:
            raise ValueError("conflicting Docker material identities")
        bases[uri] = value
    if not bases:
        raise ValueError("no observed Docker base materials")
    return bases


class BuildProvenanceCollector:
    """Read digest-pinned registry evidence using a bounded injected runner.

    The runner must enforce timeout, reap its process on cancellation and bound
    capture while reading. This class additionally rejects oversized returned
    bytes. It never runs a build, pulls an image, or invokes a shell.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        timeout_s: float = 30,
        max_output_bytes: int = _LIMIT,
        max_log_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        """Configure the injected runner and finite collection budgets."""
        if (
            isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("positive finite command timeout is required")
        if any(
            type(value) is not int or value <= 0
            for value in (max_output_bytes, max_log_bytes)
        ):
            raise ValueError("positive byte limits are required")
        self.runner = runner
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self.max_log_bytes = max_log_bytes

    def _inspect(self, reference: str, field: str, directory: Path) -> dict[str, Any]:
        argv = (
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json ." + field + "}}",
        )
        body = self.runner(argv, self.timeout_s)
        if not isinstance(body, bytes) or len(body) > self.max_output_bytes:
            raise ValueError("registry provenance output exceeds byte limit")
        _persist(directory / (field.lower() + ".json"), body)
        return _decode(body, self.max_output_bytes)

    def _log(self, item: dict[str, Any], root: Path) -> tuple[Path, bytes]:
        descriptor = _mapping(item.get("log"), "command log")
        raw = descriptor.get("path")
        if not isinstance(raw, str) or not raw:
            raise ValueError("command log path is required")
        path = _safe_file(root / raw, root)
        digest = hashlib.sha256()
        size = 0
        captured = bytearray()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            while chunk := stream.read(65536):
                size += len(chunk)
                if size > self.max_log_bytes:
                    raise ValueError("command log exceeds byte limit")
                digest.update(chunk)
                if item.get("kind") == "toolchain":
                    if size > self.max_output_bytes:
                        raise ValueError("toolchain log exceeds byte limit")
                    captured.extend(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("command log changed during collection")
        if (
            type(descriptor.get("size_bytes")) is not int
            or descriptor["size_bytes"] != size
            or descriptor.get("sha256") != digest.hexdigest()
        ):
            raise ValueError("command log size/hash mismatch")
        return path, bytes(captured)

    def _commands(
        self,
        observed: dict[str, Any],
        request: dict[str, Any],
        recipe: BuildRecipe,
        build_ref: str,
        bases: dict[str, str],
        root: Path,
    ) -> tuple[dict[str, str], tuple[Path, ...]]:
        commands = observed.get("commands")
        if not isinstance(commands, list) or not 1 <= len(commands) <= 64:
            raise ValueError("bounded build-stage command observations are required")
        counts = {"build": 0, "prerequisite": 0}
        toolchains, logs = {}, []
        for raw in commands:
            item = _mapping(raw, "command observation")
            if (
                item.get("cwd") != request["workspace"]
                or type(item.get("exit_code")) is not int
                or item["exit_code"] != 0
            ):
                raise ValueError("command failed or ran outside the bound workspace")
            argv = item.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(arg, str) or not arg for arg in argv)
            ):
                raise ValueError("observed command argv is required")
            log, body = self._log(item, root)
            logs.append(log)
            kind = item.get("kind")
            if kind in counts:
                counts[kind] += 1
                if argv != request[kind + "_argv"]:
                    raise ValueError("observed command differs from bound argv")
                continue
            if kind != "toolchain":
                raise ValueError("unknown observed command kind")
            name = item.get("toolchain")
            if not isinstance(name, str) or name not in _VERSIONS or name in toolchains:
                raise ValueError("unknown or duplicate observed toolchain")
            text = body.decode("utf-8")
            if name == "buildkit":
                parts = build_ref.split("/")
                if (
                    len(parts) != 3
                    or not all(parts)
                    or item.get("scope") != "builder"
                    or item.get("build_ref") != build_ref
                ):
                    raise ValueError(
                        "BuildKit observation lacks matching build reference"
                    )
                if argv != ["docker", "buildx", "inspect", parts[0]]:
                    raise ValueError(
                        "BuildKit identity must come from the observed builder"
                    )
                nodes = re.findall(r"(?m)^\s*Name:\s*(\S+)\s*$", text)
                if parts[1] not in nodes:
                    raise ValueError("BuildKit log lacks the actual builder node")
            else:
                expected_scope = (
                    "host"
                    if recipe.prerequisite_argv and name in {"java", "gradle"}
                    else "build"
                )
                if item.get("scope") != expected_scope:
                    raise ValueError(
                        "toolchain observation scope differs from build context"
                    )
                if (
                    expected_scope == "build"
                    and item.get("material") not in bases.values()
                ):
                    raise ValueError(
                        "toolchain is not bound to an observed build material"
                    )
                executables = {
                    "java": {"java"},
                    "gradle": {"gradle", "gradlew"},
                    "node": {"node"},
                    "native-image": {"native-image"},
                }
                if (
                    Path(argv[0]).name not in executables[name]
                    or len(argv) != 2
                    or argv[1] not in {"--version", "-version"}
                ):
                    raise ValueError(
                        "toolchain version requires an observed version command"
                    )
            versions = _VERSIONS[name].findall(text)
            if len(versions) != 1:
                raise ValueError(
                    "toolchain version is missing or ambiguous in observed log"
                )
            toolchains[name] = versions[0]
        if counts != {
            "build": 1,
            "prerequisite": int(recipe.prerequisite_argv is not None),
        }:
            raise ValueError("build/prerequisite command coverage is incomplete")
        required = {"buildkit"}
        if recipe.prerequisite_argv is not None:
            required.update(("java", "gradle"))
        elif recipe.variant.startswith("native-"):
            required.add("native-image")
        elif recipe.variant == "default":
            required.add("node")
        else:
            raise ValueError("unsupported toolchain observation requirements")
        if not required.issubset(toolchains):
            raise ValueError(
                "missing required observed toolchains: "
                + ", ".join(sorted(required - toolchains.keys()))
            )
        return toolchains, tuple(logs)

    def collect(
        self, recipe: BuildRecipe, metadata: Path, workspace: Path
    ) -> ObservedBuild:
        """Return observed identities, or raise before any measurement can begin."""
        target = _recipe_target(recipe)
        metadata, workspace = metadata.absolute(), workspace.resolve(strict=True)
        root = metadata.parent.resolve()
        request_path = _sidecar(metadata, ".request.json")
        observation_path = _sidecar(metadata, ".observations.json")
        request = _decode(_read(request_path, root))
        expected = {
            "schema": "nanolab-soak-build-request-v1",
            "role": recipe.role,
            "recipe_fingerprint": recipe.recipe_fingerprint,
            "bake_fingerprint": fingerprint(recipe.bake or {}),
            "platform": recipe.platform,
            "image": recipe.image,
            "metadata": str(metadata),
            "workspace": str(workspace),
            "prerequisite_argv": list(recipe.prerequisite_argv or ()),
        }
        if any(
            request.get(key) != value for key, value in expected.items()
        ) or not request.get("request_id"):
            raise ValueError("build request differs from recipe/workspace")
        record = _decode(_read(metadata, root, self.max_output_bytes))
        if "containerimage.digest" not in record:
            record = _mapping(record.get(target), "bake target metadata")
        digest = record.get("containerimage.digest")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("BuildKit output digest is missing or invalid")
        descriptor = record.get("containerimage.descriptor")
        if (
            descriptor is not None
            and _mapping(descriptor, "output descriptor").get("digest") != digest
        ):
            raise ValueError("metadata descriptor digest mismatch")
        observed = _decode(_read(observation_path, root))
        binding = {
            "schema": "nanolab-soak-build-observations-v1",
            "request_id": request["request_id"],
            "recipe_fingerprint": recipe.recipe_fingerprint,
            "workspace": str(workspace),
            "image_digest": digest,
        }
        if any(observed.get(key) != value for key, value in binding.items()):
            raise ValueError(
                "build observations differ from request/recipe/workspace/digest"
            )
        collection = root / (metadata.name + ".provenance-" + uuid4().hex)
        collection.mkdir()
        last = recipe.image.rsplit("/", 1)[-1]
        repository = recipe.image.rsplit(":", 1)[0] if ":" in last else recipe.image
        reference = repository + "@" + digest
        manifest = self._inspect(reference, "Manifest", collection)
        if manifest.get("digest") != digest:
            raise ValueError("published digest differs from build output")
        descriptors = manifest.get("manifests")
        if not isinstance(descriptors, list):
            raise ValueError("published index with attached attestation is required")
        platform = _expected_platform(recipe.platform)
        matching = [
            item
            for item in descriptors
            if isinstance(item, dict) and _platform(item.get("platform")) == platform
        ]
        if len(matching) != 1:
            raise ValueError("published platform is absent or ambiguous")
        child = matching[0].get("digest")
        if not isinstance(child, str) or _DIGEST.fullmatch(child) is None:
            raise ValueError("published platform digest is invalid")
        attached = [
            item
            for item in descriptors
            if isinstance(item, dict)
            and isinstance(item.get("annotations"), dict)
            and item["annotations"].get("vnd.docker.reference.type")
            == "attestation-manifest"
            and item["annotations"].get("vnd.docker.reference.digest") == child
        ]
        if not attached:
            raise ValueError("published platform lacks a bound attestation")
        image = self._inspect(repository + "@" + child, "Image", collection)
        if _platform(image) != platform:
            raise ValueError("effective image platform differs from requested platform")
        published = self._inspect(reference, "Provenance", collection)
        predicate = _predicate(published, recipe.platform, child)
        bases = _materials(predicate)
        local = record.get("buildx.build.provenance")
        if local:
            local_predicate = _predicate(local, recipe.platform, child)
            if _materials(local_predicate) != bases:
                raise ValueError("metadata and published materials disagree")
            local_id = local_predicate.get("metadata", {}).get("buildInvocationID")
            published_id = predicate.get("metadata", {}).get("buildInvocationID")
            if local_id is not None and local_id != published_id:
                raise ValueError("metadata and published build invocation differ")
        build_ref = record.get("buildx.build.ref")
        if not isinstance(build_ref, str) or not build_ref:
            raise ValueError("observed BuildKit build reference is missing")
        toolchains, logs = self._commands(
            observed, request, recipe, build_ref, bases, root
        )
        return ObservedBuild(
            digest,
            toolchains,
            bases,
            (
                metadata,
                request_path,
                observation_path,
                *logs,
                collection / "manifest.json",
                collection / "image.json",
                collection / "provenance.json",
            ),
        )
