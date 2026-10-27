"""Versioned, immutable experiment inputs and atomic publication receipts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nanolab.application.functions import ResolvedFunction, resolve_function
from nanolab.application.vm_provider import vm_request_for_role
from nanolab.comparison.experiment_02 import EXPERIMENT_02_PUBLISHER
from nanolab.comparison.matrix import ComparisonCell, MatrixCell
from nanolab.comparison.profiles import COMPARISON_SCHEDULER_STRATEGY, declared_options
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.vm.models import VmRequest
from nanolab.workspace.paths import discover_tool_root


class _Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nanofaas: dict[str, object]
    nanolab: dict[str, object]
    profiles: dict[str, dict[str, object]]
    tag: str
    variants: list[str]
    repetitions: int
    nativeProperties: dict[str, object]  # noqa: N815 - manifest JSON contract
    scenario: dict[str, object]
    environment: dict[str, object]
    roles: dict[str, dict[str, object]]
    functions: dict[str, dict[str, object]]
    scheduler: dict[str, object]
    # Absent from every historical manifest, which keeps the historical rules.
    experiment: dict[str, object] | None = None

    @model_validator(mode="after")
    def complete_identity(self) -> _Identity:
        for source in (self.nanofaas, self.nanolab):
            if not source.get("revision") or not _sha(source.get("patchSha256")):
                raise ValueError("incomplete Git identity")
        expected_scheduler = (
            EXPERIMENT_01_SCHEDULER
            if self.experiment is not None
            else {
                "engine": "unified",
                "strategy": COMPARISON_SCHEDULER_STRATEGY,
                "runtimeSwitching": False,
            }
        )
        if self.scheduler != expected_scheduler:
            raise ValueError("missing or conflicting scheduler identity")
        if self.experiment is not None:
            _complete_experiment(self.experiment, self.variants, self.repetitions)
        if (
            not self.variants
            or len(set(self.variants)) != len(self.variants)
            or self.repetitions < 1
        ):
            raise ValueError("invalid matrix identity")
        for config in (self.scenario, self.environment):
            if not isinstance(config.get("values"), dict) or config.get(
                "sha256"
            ) != _hash(config["values"]):
                raise ValueError("incomplete configuration identity")
        for key in _profiled_keys(self.experiment, self.variants):
            profile = self.profiles.get(key, {})
            if not _sha(profile.get("sha256")) or not isinstance(
                profile.get("options"), dict
            ):
                raise ValueError("incomplete profile identity")
            options = profile["options"]
            assert isinstance(options, dict)  # nosec B101 - validated above
            if not {
                "mode",
                "variant",
                "modules",
                "jvm_args",
                "native",
                "functions",
            }.issubset(options):
                raise ValueError("incomplete profile options identity")
        required_function_fields = {field.name for field in fields(ResolvedFunction)}
        for function in self.functions.values():
            if not required_function_fields.issubset(function):
                raise ValueError("incomplete resolved function identity")
        required_request_fields = set(VmRequest.model_fields) - {"proxmox_password"}
        for request in self.roles.values():
            if not required_request_fields.issubset(request):
                raise ValueError("incomplete VM request identity")
        if not self.roles or not self.functions or not self.tag:
            raise ValueError("incomplete resolved inputs")
        return self


# Experiment 01 pins no strategy: the baseline has no queue to schedule, and
# the queue composition keeps the engine's own derivation from its modules. The
# strategy that actually ran is observed per cell, not asserted here.
EXPERIMENT_01_SCHEDULER: dict[str, object] = {
    "engine": "unified",
    "strategy": None,
    "runtimeSwitching": False,
}
_EXPERIMENT_FIELDS = {
    "profile",
    "orderPolicy",
    "order",
    "protocol",
    "protocolSha256",
    "payload",
    "script",
    "builds",
    "subset",
}


_EXPERIMENT_02_FIELDS = {
    "profile",
    "orderPolicy",
    "seed",
    "order",
    "protocol",
    "protocolSha256",
    "payload",
    "script",
    "configurations",
    "budgets",
    "subset",
    "workloads",
}


def _profiled_keys(
    experiment: Mapping[str, object] | None, variants: list[str]
) -> tuple[str, ...]:
    """Return the keys whose recipe must be in the identity's profiles.

    Experiment 02 has configurations without a recipe, such as the function
    alone; every configuration that has one is profiled, and so is the recipe
    that publishes the shared function, selected or not.
    """
    if experiment is None:
        return tuple(dict.fromkeys(("jvm", *variants)))
    if experiment.get("profile") != "experiment-02":
        return tuple(variants)
    configurations = experiment["configurations"]
    assert isinstance(configurations, dict)  # nosec B101 - validated before
    recipes = [
        key
        for key, configuration in configurations.items()
        if isinstance(configuration, dict) and configuration.get("recipe")
    ]
    return tuple(dict.fromkeys((EXPERIMENT_02_PUBLISHER, *recipes)))


def _complete_experiment_02(
    experiment: Mapping[str, object], variants: list[str], repetitions: int
) -> None:
    if set(experiment) != _EXPERIMENT_02_FIELDS:
        raise ValueError("incomplete experiment identity")
    if experiment["protocolSha256"] != _hash(experiment["protocol"]):
        raise ValueError("incomplete experiment protocol identity")
    payload = experiment["payload"]
    script = experiment["script"]
    configurations = experiment["configurations"]
    budgets = experiment["budgets"]
    order = experiment["order"]
    if (
        not isinstance(payload, dict)
        or not _sha(payload.get("sha256"))
        or not isinstance(script, dict)
        or not _sha(script.get("sha256"))
        or not isinstance(configurations, dict)
        or set(configurations) != set(variants)
        or not isinstance(budgets, list)
        or not budgets
        or not isinstance(order, list)
        or len(order) != len(variants) * len(budgets) * repetitions
        # One cell per (configuration, budget, repetition): no label twice.
        or len(set(order)) != len(order)
    ):
        raise ValueError("incomplete experiment artifact identity")


def _complete_experiment(
    experiment: Mapping[str, object], variants: list[str], repetitions: int
) -> None:
    if experiment.get("profile") == "experiment-02":
        _complete_experiment_02(experiment, variants, repetitions)
        return
    if set(experiment) != _EXPERIMENT_FIELDS:
        raise ValueError("incomplete experiment identity")
    if experiment["profile"] != "experiment-01" or experiment["protocolSha256"] != (
        _hash(experiment["protocol"])
    ):
        raise ValueError("incomplete experiment protocol identity")
    payload = experiment["payload"]
    script = experiment["script"]
    builds = experiment["builds"]
    order = experiment["order"]
    if (
        not isinstance(payload, dict)
        or not _sha(payload.get("sha256"))
        or not _sha(payload.get("bodySha256"))
        or not isinstance(script, dict)
        or not _sha(script.get("sha256"))
        or not isinstance(builds, dict)
        or set(builds) != set(variants)
        or not isinstance(order, list)
        or len(order) != len(variants) * repetitions
    ):
        raise ValueError("incomplete experiment artifact identity")


def _sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ("git", *args), cwd=root, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"Cannot establish Git identity: {root}") from error


def _source_identity(root: Path) -> dict[str, object]:
    return {
        "revision": _git(root, "rev-parse", "HEAD").decode().strip(),
        "patchSha256": hashlib.sha256(
            _git(root, "diff", "HEAD", "--binary", "--no-ext-diff")
        ).hexdigest(),
    }


def capture_comparison_inputs(
    *,
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    nanofaas_root: Path,
    nanolab_root: Path,
    profiles: Mapping[str, Path],
    variants: tuple[str, ...],
    repetitions: int,
    tag: str,
    build_memory: str | None,
    parallelism: int | None,
    experiment: Mapping[str, object] | None = None,
    profile_options: Callable[[str, Path], dict[str, object]] = (
        lambda _key, path: declared_options(path)
    ),
) -> dict[str, object]:
    """Capture tracked Git state and complete effective experiment settings."""
    scenario_values = scenario.model_dump(mode="json")
    environment_values = environment.model_dump(mode="json")
    roles = {
        role: vm_request_for_role(environment, role, loadtest=True).model_dump(
            mode="json", exclude={"proxmox_password"}
        )
        for role in ("stack", "loadgen")
    }
    tool_root = discover_tool_root()
    inputs: dict[str, object] = {
        "nanofaas": _source_identity(nanofaas_root),
        "nanolab": _source_identity(nanolab_root),
        "profiles": {
            key: {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "options": profile_options(key, path),
            }
            for key, path in profiles.items()
        },
        "tag": tag,
        "variants": list(variants),
        "repetitions": repetitions,
        "nativeProperties": {"buildMemory": build_memory, "parallelism": parallelism},
        "scenario": {"values": scenario_values, "sha256": _hash(scenario_values)},
        "environment": {
            "values": environment_values,
            "sha256": _hash(environment_values),
        },
        "roles": roles,
        "functions": {
            key: asdict(
                resolve_function(
                    scenario, key, source_root=nanofaas_root, tool_root=tool_root
                )
            )
            for key in scenario.functions
        },
        "scheduler": dict(EXPERIMENT_01_SCHEDULER)
        if experiment is not None
        else {
            "engine": "unified",
            "strategy": COMPARISON_SCHEDULER_STRATEGY,
            "runtimeSwitching": False,
        },
    }
    if experiment is not None:
        inputs["experiment"] = dict(experiment)
    # Normalize tuples in resolved functions to JSON's array representation.
    normalized = json.loads(json.dumps(inputs))
    _Identity.model_validate(normalized)
    return normalized


class ComparisonManifest(BaseModel):
    """An experiment's original inputs, infrastructure and verified artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1] = 1  # noqa: N815 - manifest JSON contract
    identity: dict[str, object]
    target: dict[str, object] | None = None
    publications: dict[str, dict[str, object]] = Field(default_factory=dict)
    started_at: str
    functions: list[str]
    repetitions: int
    order: list[str]
    variants: list[dict[str, object]]
    regime: dict[str, object]

    @model_validator(mode="after")
    def complete_manifest(self) -> ComparisonManifest:
        """Reject incomplete identities and publication receipts."""
        _Identity.model_validate(self.identity)
        for receipt in self.publications.values():
            for field in (
                "profile",
                "distribution",
                "evidence",
                "images",
                "declaredOptions",
                "source",
                "gradleProperties",
            ):
                if not isinstance(receipt.get(field), dict):
                    raise ValueError(f"incomplete publication receipt: {field}")
            for field in ("profile", "distribution"):
                descriptor = receipt[field]
                assert isinstance(descriptor, dict)  # nosec B101 - validated above
                if not isinstance(descriptor.get("path"), str) or not _sha(
                    descriptor.get("sha256")
                ):
                    raise ValueError(f"incomplete publication receipt: {field}")
            if not receipt["evidence"] or not receipt["images"]:
                raise ValueError("incomplete publication receipt: evidence/images")
            evidence = receipt["evidence"]
            assert isinstance(evidence, dict)  # nosec B101 - validated above
            for descriptor in evidence.values():
                if (
                    not isinstance(descriptor, dict)
                    or not isinstance(descriptor.get("path"), str)
                    or not _sha(descriptor.get("sha256"))
                ):
                    raise ValueError("incomplete publication receipt: evidence")
            images = receipt["images"]
            assert isinstance(images, dict)  # nosec B101 - validated above
            for image in images.values():
                if (
                    not isinstance(image, dict)
                    or not image.get("reference")
                    or not isinstance(image.get("digest"), str)
                    or not _sha(image["digest"].removeprefix("sha256:"))
                ):
                    raise ValueError("incomplete publication receipt: image")
            if receipt["source"] != self.identity["nanofaas"]:
                raise ValueError("incomplete publication receipt: source")
        return self


def read_comparison_manifest(root: Path) -> ComparisonManifest | None:
    """Read supported evidence, refusing nonempty roots without a valid manifest."""
    if not root.exists() or not any(root.iterdir()):
        return None
    try:
        return ComparisonManifest.model_validate_json(
            (root / "comparison-manifest.json").read_bytes()
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid comparison manifest in {root}: {error}") from error


def require_matching_inputs(
    manifest: ComparisonManifest, inputs: Mapping[str, object]
) -> None:
    """Refuse changed inputs without modifying the original evidence."""

    def comparable(value: Mapping[str, object]) -> dict[str, object]:
        result = json.loads(json.dumps(dict(value)))
        for profile in result["profiles"].values():
            profile.pop("path", None)
        return result

    _Identity.model_validate(dict(inputs))
    if comparable(manifest.identity) != comparable(inputs):
        raise ValueError("Comparison inputs differ; use a new run directory")


def new_comparison_manifest(
    inputs: Mapping[str, object],
    cells: tuple[MatrixCell, ...],
    *,
    variants: list[dict[str, object]] | None = None,
) -> ComparisonManifest:
    """Create initial evidence while retaining the report's existing fields.

    The historical matrix derives each variant's record from its build; a
    matrix whose cells are not builds, like experiment 02's, passes `variants`.
    """
    identity = _Identity.model_validate(dict(inputs))
    records: list[dict[str, object]]
    if variants is not None:
        records = variants
    else:
        builds = [cell for cell in cells if isinstance(cell, ComparisonCell)]
        if len(builds) != len(cells):
            raise ValueError("cells that are not builds need explicit variants")
        by_key: dict[str, dict[str, object]] = {
            cell.variant.key: {
                "key": cell.variant.key,
                "label": cell.variant.label,
                "rationale": cell.variant.rationale,
                "build_env": dict(cell.variant.build_env),
                "image": (
                    f"127.0.0.1:5000/nanofaas/control-plane-{cell.variant.key}"
                    f":{identity.tag}"
                ),
            }
            for cell in builds
        }
        records = list(by_key.values())
    return ComparisonManifest(
        identity=dict(inputs),
        started_at=datetime.now(UTC).isoformat(),
        functions=list(identity.functions),
        repetitions=max(cell.repetition for cell in cells),
        order=[cell.label for cell in cells],
        variants=records,
        regime=identity.scenario,
    )


def write_comparison_manifest(root: Path, manifest: ComparisonManifest) -> None:
    """Atomically replace complete evidence using a same-directory temporary file."""
    validated = ComparisonManifest.model_validate(manifest.model_dump())
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=root, prefix=".comparison-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(validated.model_dump_json(indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(root / "comparison-manifest.json")
    finally:
        temporary.unlink(missing_ok=True)
