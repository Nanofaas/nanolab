"""Verify publication reports against captured inputs and published artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.comparison.manifest import ComparisonManifest
from nanolab.comparison.prepare import (
    ComparisonStage,
    comparison_remote_command,
    verify_comparison_source,
)
from nanolab.comparison.profiles import (
    COMPARISON_FUNCTIONS,
    COMPARISON_RECIPE_MODULES,
    declared_options,
)
from nanolab.tasks.recipe import (
    RecipeDistribution,
    RecipeImage,
    read_distribution,
    require_validation_distribution,
)
from nanolab.tasks.vm.models import VmRequest
from nanolab.tasks.vm.runners import VmFileFetcher

_FIXED_JVM = [
    "-XX:MaxRAMPercentage=70",
    "-Xss256k",
    "-Dspring.main.banner-mode=off",
    "-Dspring.jmx.enabled=false",
    "-Dspring.devtools.restart.enabled=false",
    "-Dmanagement.endpoints.enabled-by-default=false",
]
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("Comparison image has a malformed sha256 digest")
    return value


def verify_comparison_registry(
    image: RecipeImage, provider: VmCommandProvider, request: VmRequest
) -> dict[str, object]:
    """Verify a mutable tag and its single-platform registry configuration."""
    digest = _digest(image.digest)
    config = _digest(image.id)
    reported = comparison_remote_command(
        provider,
        request,
        (
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{.Manifest.Digest}}",
            image.reference,
        ),
    ).strip()
    if reported != digest:
        raise ValueError("Comparison image registry digest differs from report")
    reference = image.reference.rsplit(":", 1)[0] + "@" + digest
    raw = comparison_remote_command(
        provider,
        request,
        ("docker", "buildx", "imagetools", "inspect", "--raw", reference),
    )
    descriptor = json.loads(raw)
    if descriptor.get("config", {}).get("digest") != config:
        raise ValueError("Comparison image registry configuration differs from report")
    return {
        "reference": image.reference,
        "digest": digest,
        "config": config,
        "manifest": descriptor,
    }


def _inspect_jvm(
    image: RecipeImage,
    options: Mapping[str, Any],
    stage: ComparisonStage,
    provider: VmCommandProvider,
    request: VmRequest,
    evidence_dir: Path,
) -> None:
    reference = image.reference.rsplit(":", 1)[0] + "@" + _digest(image.digest)
    comparison_remote_command(provider, request, ("docker", "pull", reference))
    temporary = comparison_remote_command(
        provider, request, ("docker", "create", reference)
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", temporary):
        raise ValueError("Comparison JVM inspection container identity is malformed")
    remote_dir = stage.remote_root / "inspection" / evidence_dir.name
    fetcher = VmFileFetcher(provider, request)
    try:
        comparison_remote_command(provider, request, ("mkdir", "-p", str(remote_dir)))
        for name in ("jvm.options", "launch.args"):
            destination = remote_dir / name
            comparison_remote_command(
                provider,
                request,
                ("docker", "cp", f"{temporary}:/app/{name}", str(destination)),
            )
            fetcher.fetch_from(str(destination), evidence_dir / name)
        raw = comparison_remote_command(
            provider, request, ("docker", "image", "inspect", reference)
        )
        (evidence_dir / "launch-config.json").write_text(raw)
        image_config = json.loads(raw)
        config = image_config[0]["Config"]
        if (
            image_config[0]["Id"] != image.id
            or config.get("Entrypoint")
            != ["/opt/jre/bin/java", "@/app/jvm.options", "@/app/launch.args"]
            or config.get("Cmd")
            or config.get("WorkingDir") != "/app"
            or any(
                entry.split("=", 1)[0]
                in {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"}
                and entry.split("=", 1)[-1]
                for entry in config.get("Env", [])
            )
        ):
            raise ValueError("Comparison JVM image launch configuration differs")
        actual = shlex.split((evidence_dir / "jvm.options").read_text())
        if actual != [*_FIXED_JVM, *options["jvm_args"]]:
            raise ValueError("Comparison JVM image argument options differ")
        if shlex.split((evidence_dir / "launch.args").read_text()) != [
            "-jar",
            "app.jar",
        ]:
            raise ValueError("Comparison JVM image launch arguments differ")
    finally:
        comparison_remote_command(provider, request, ("docker", "rm", temporary))


def _descriptor(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def verify_comparison_publication(
    *,
    distribution: RecipeDistribution,
    stage: ComparisonStage,
    variant: str,
    inputs: Mapping[str, object],
    provider: VmCommandProvider,
    request: VmRequest,
    evidence_dir: Path,
) -> dict[str, object]:
    """Return a receipt only after source, report and image evidence agree."""
    captured = cast(dict[str, Any], inputs)
    run = stage.runs[variant]
    options = declared_options(run.recipe)
    profile = captured["profiles"][variant]
    if (
        hashlib.sha256(run.recipe.read_bytes()).hexdigest() != profile["sha256"]
        or options != profile["options"]
    ):
        raise ValueError("Comparison captured profile differs from immutable inputs")
    # Re-read instead of trusting a distribution object supplied by a caller.
    distribution = read_distribution(
        distribution.report, recipe=run.recipe, tag=run.tag, published=True
    )
    require_validation_distribution(
        distribution,
        functions=tuple(COMPARISON_FUNCTIONS.values()) if variant == "jvm" else (),
        required_modules=COMPARISON_RECIPE_MODULES,
        exact_modules=True,
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    state = verify_comparison_source(stage, provider, request)
    (evidence_dir / "source.json").write_text(json.dumps(state, indent=2) + "\n")
    source = captured["nanofaas"]
    if (
        state["revision"] != source["revision"]
        or state["patchSha256"] != source["patchSha256"]
    ):
        raise ValueError("Comparison staged source differs from immutable inputs")
    expected_source = {
        "revision": source["revision"],
        "dirty": source["patchSha256"] != hashlib.sha256(b"").hexdigest(),
    }
    if distribution.source != expected_source:
        raise ValueError("Comparison report source identity differs")
    cp = distribution.control_plane()
    if cp.native != options["native"]:
        raise ValueError("Comparison native options differ from captured profile")
    registry = {}
    images = {}
    for component in distribution.components:
        key = (
            "control-plane"
            if component.kind == "control-plane"
            else f"{component.name}/{component.sdk}"
        )
        registry[key] = verify_comparison_registry(component.image, provider, request)
        images[key] = {
            "reference": component.image.reference,
            "digest": component.image.digest,
            "id": component.image.id,
        }
    (evidence_dir / "registry.json").write_text(json.dumps(registry, indent=2) + "\n")
    if cp.mode == "jvm":
        _inspect_jvm(cp.image, options, stage, provider, request, evidence_dir)
    root = evidence_dir.parents[1]
    evidence = {
        path.name: _descriptor(path, root)
        for path in evidence_dir.iterdir()
        if path.is_file() and path.name not in {"gradle.log"}
    }
    return {
        "profile": _descriptor(run.recipe, root),
        "distribution": _descriptor(distribution.report, root),
        "evidence": evidence,
        "images": images,
        "source": source,
        "declaredOptions": options,
        "gradleProperties": captured["nativeProperties"]
        if cp.mode == "native"
        else {"buildMemory": None, "parallelism": None},
    }


def _verify_file(descriptor: Mapping[str, Any], root: Path) -> Path:
    path = root / descriptor["path"]
    if (
        not path.resolve().is_relative_to(root.resolve())
        or not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != descriptor["sha256"]
    ):
        raise ValueError("Comparison recorded artifact evidence is missing or changed")
    return path


def require_recorded_publications(
    *,
    manifest: ComparisonManifest,
    provider: VmCommandProvider,
    request: VmRequest,
    root: Path,
) -> None:
    """Require local receipts and published images without rebuilding anything."""
    inputs = cast(dict[str, Any], manifest.identity)
    for variant, raw_receipt in manifest.publications.items():
        receipt = cast(dict[str, Any], raw_receipt)
        expected_profile = inputs["profiles"].get(variant)
        if (
            not expected_profile
            or receipt["profile"]["sha256"] != expected_profile["sha256"]
            or receipt["declaredOptions"] != expected_profile["options"]
            or receipt["source"] != inputs["nanofaas"]
        ):
            raise ValueError(
                "Comparison recorded receipt differs from immutable inputs"
            )
        profile = _verify_file(receipt["profile"], root)
        report = _verify_file(receipt["distribution"], root)
        for descriptor in receipt["evidence"].values():
            _verify_file(descriptor, root)
        distribution = read_distribution(
            report, recipe=profile, tag=str(manifest.identity["tag"]), published=True
        )
        expected_source = {
            "revision": inputs["nanofaas"]["revision"],
            "dirty": inputs["nanofaas"]["patchSha256"]
            != hashlib.sha256(b"").hexdigest(),
        }
        if distribution.source != expected_source:
            raise ValueError("Comparison recorded report source differs")
        if distribution.control_plane().native != expected_profile["options"]["native"]:
            raise ValueError("Comparison recorded native options differ")
        if declared_options(profile) != receipt["declaredOptions"]:
            raise ValueError("Comparison recorded profile options differ")
        require_validation_distribution(
            distribution,
            functions=tuple(COMPARISON_FUNCTIONS.values()) if variant == "jvm" else (),
            required_modules=COMPARISON_RECIPE_MODULES,
            exact_modules=True,
        )
        for component in distribution.components:
            key = (
                "control-plane"
                if component.kind == "control-plane"
                else f"{component.name}/{component.sdk}"
            )
            expected = receipt["images"][key]
            if expected != {
                "reference": component.image.reference,
                "digest": component.image.digest,
                "id": component.image.id,
            }:
                raise ValueError("Comparison recorded image differs from report")
            verify_comparison_registry(component.image, provider, request)
