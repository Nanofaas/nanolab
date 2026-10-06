"""Immutable experiment inputs protect resumed comparison results."""

import hashlib
import json
import subprocess
from typing import Any

import pytest

from nanolab.comparison.manifest import (
    ComparisonManifest,
    capture_comparison_inputs,
    new_comparison_manifest,
    read_comparison_manifest,
    require_matching_inputs,
    write_comparison_manifest,
)
from nanolab.comparison.matrix import build_matrix
from nanolab.comparison.profiles import comparison_profiles
from nanolab.config.environment import EnvironmentConfig, RoleTarget
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.control_plane_variants import resolve_variants
from nanolab.workspace.paths import discover_tool_root


def git(root, *args):
    return subprocess.check_output(("git", *args), cwd=root)


@pytest.fixture
def captured(tmp_path):
    roots = {}
    for name in ("nanofaas", "nanolab"):
        root = tmp_path / name
        root.mkdir()
        git(root, "init", "-q")
        git(root, "config", "user.email", "tests@example.com")
        git(root, "config", "user.name", "Tests")
        (root / "tracked").write_text("initial")
        if name == "nanofaas":
            for sdk in ("java", "javascript"):
                function = root / "functions" / sdk / "word-stats"
                function.mkdir(parents=True)
                (function / "function.yaml").write_text(
                    f"name: word-stats-{sdk}\ncatalog:\n"
                    f"  defaultImage: test/{sdk}:latest\n"
                )
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial")
        roots[name] = root
    kwargs: dict[str, Any] = {
        "scenario": ScenarioConfig(
            workflow="loadtest",
            backend="k8s",
            loadProfile="comparison",
            functions=["word-stats-java", "word-stats-javascript"],
        ),
        "environment": EnvironmentConfig(
            provider="multipass", roles={"stack": RoleTarget(name="experiment")}
        ),
        "nanofaas_root": roots["nanofaas"],
        "nanolab_root": roots["nanolab"],
        "profiles": comparison_profiles(discover_tool_root(), ("jvm",)),
        "variants": ("jvm",),
        "repetitions": 1,
        "tag": "recipe-test",
        "build_memory": None,
        "parallelism": None,
    }
    return kwargs, capture_comparison_inputs(**kwargs)


def test_changed_load_scale_refuses_resume_without_rewriting(tmp_path, captured):
    kwargs, inputs = captured
    manifest = new_comparison_manifest(
        inputs, build_matrix(resolve_variants(("jvm",)), 1)
    )
    root = tmp_path / "run"
    write_comparison_manifest(root, manifest)
    before = (root / "comparison-manifest.json").read_bytes()
    kwargs["scenario"] = kwargs["scenario"].model_copy(update={"load_scale": 2.0})
    with pytest.raises(ValueError, match="inputs"):
        require_matching_inputs(manifest, capture_comparison_inputs(**kwargs))
    assert (root / "comparison-manifest.json").read_bytes() == before


def test_capture_tracks_both_repositories_and_effective_defaults(captured):
    kwargs, inputs = captured
    for name in ("nanofaas", "nanolab"):
        assert (
            inputs[name]["revision"]
            == git(kwargs[f"{name}_root"], "rev-parse", "HEAD").decode().strip()
        )
        assert inputs[name]["patchSha256"] == hashlib.sha256(b"").hexdigest()
    assert inputs["scheduler"] == {
        "engine": "unified",
        "strategy": "per-function",
        "runtimeSwitching": False,
    }
    assert inputs["scenario"]["values"]["load_scale"] == 1.0
    assert inputs["roles"]["stack"]["cpus"] == 4
    assert inputs["functions"]["word-stats-java"]["max_retries"] == 3
    payload = json.loads(
        (discover_tool_root() / "scenarios/payloads/word-stats-sample.json").read_text()
    )
    assert inputs["functions"]["word-stats-java"]["payload"] == json.dumps(
        {"input": payload}, separators=(",", ":")
    )
    assert (
        inputs["profiles"]["jvm"]["sha256"]
        == hashlib.sha256(kwargs["profiles"]["jvm"].read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "change",
    [
        "nanofaas",
        "nanolab",
        "profile",
        "repetitions",
        "variants",
        "parallelism",
        "memory",
        "resources",
        "vm",
        "function",
        "mixed",
    ],
)
def test_changed_effective_input_refuses_resume(captured, tmp_path, change):
    kwargs, inputs = captured
    manifest = new_comparison_manifest(
        inputs, build_matrix(resolve_variants(("jvm",)), 1)
    )
    if change in ("nanofaas", "nanolab"):
        (kwargs[f"{change}_root"] / "tracked").write_text("changed")
    elif change == "profile":
        profile = tmp_path / "copy.yaml"
        profile.write_bytes(kwargs["profiles"]["jvm"].read_bytes() + b"\n# changed\n")
        kwargs["profiles"] = {"jvm": profile}
    elif change == "memory":
        kwargs["build_memory"] = "4g"
    elif change == "resources":
        kwargs["scenario"] = kwargs["scenario"].model_copy(update={"load_vus": 200})
    elif change == "vm":
        kwargs["environment"] = EnvironmentConfig(
            provider="multipass",
            roles={"stack": RoleTarget(name="experiment", memory="16G")},
        )
    elif change == "function":
        path = kwargs["nanofaas_root"] / "functions/java/word-stats/function.yaml"
        path.write_text(
            path.read_text().replace("name: word-stats-java", "name: changed")
        )
    elif change == "mixed":
        kwargs["scenario"] = kwargs["scenario"].model_copy(update={"async_share": 0.5})
    else:
        kwargs[change] = {
            "repetitions": 2,
            "variants": ("jvm", "native-o3"),
            "parallelism": 2,
        }[change]
    if change == "variants":
        kwargs["profiles"] = comparison_profiles(
            discover_tool_root(), kwargs["variants"]
        )
    with pytest.raises(ValueError, match="inputs"):
        require_matching_inputs(manifest, capture_comparison_inputs(**kwargs))


def test_profile_path_does_not_change_experiment(captured, tmp_path):
    kwargs, inputs = captured
    copy = tmp_path / "same.yaml"
    copy.write_bytes(kwargs["profiles"]["jvm"].read_bytes())
    kwargs["profiles"] = {"jvm": copy}
    manifest = new_comparison_manifest(
        inputs, build_matrix(resolve_variants(("jvm",)), 1)
    )
    require_matching_inputs(manifest, capture_comparison_inputs(**kwargs))


@pytest.mark.parametrize(
    "payload",
    [None, "{", "{}", '{"schemaVersion":2}', '{"schemaVersion":1,"identity":{}}'],
)
def test_nonempty_or_legacy_root_cannot_be_overwritten(tmp_path, payload):
    root = tmp_path / "run"
    root.mkdir()
    path = root / "comparison-manifest.json"
    if payload is None:
        (root / "existing-result").write_text("preserve")
    else:
        path.write_text(payload)
    with pytest.raises(ValueError, match="manifest"):
        read_comparison_manifest(root)
    if payload is not None:
        assert path.read_text() == payload


def test_missing_scheduler_identity_invalidates_manifest(captured):
    _, inputs = captured
    del inputs["scheduler"]
    with pytest.raises(ValueError, match="scheduler"):
        ComparisonManifest(
            identity=inputs,
            started_at="now",
            functions=[],
            repetitions=1,
            order=[],
            variants=[],
            regime={},
        )


def test_atomic_failure_preserves_original_manifest(captured, tmp_path, monkeypatch):
    _, inputs = captured
    manifest = new_comparison_manifest(
        inputs, build_matrix(resolve_variants(("jvm",)), 1)
    )
    write_comparison_manifest(tmp_path, manifest)
    before = (tmp_path / "comparison-manifest.json").read_bytes()

    def fail(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr("nanolab.comparison.manifest.Path.replace", fail)
    with pytest.raises(OSError, match="replacement"):
        write_comparison_manifest(tmp_path, manifest)
    assert (tmp_path / "comparison-manifest.json").read_bytes() == before
    assert json.loads(before)["order"] == ["jvm run 1"]


def test_git_failure_does_not_create_identity(captured, tmp_path):
    kwargs, _ = captured
    kwargs["nanolab_root"] = tmp_path
    with pytest.raises(ValueError, match="Git"):
        capture_comparison_inputs(**kwargs)


@pytest.mark.parametrize(
    "field",
    [
        "profile",
        "distribution",
        "evidence",
        "images",
        "source",
        "gradleProperties",
        "declaredOptions",
    ],
)
def test_incomplete_publication_receipt_is_rejected(captured, field):
    _, inputs = captured
    manifest = new_comparison_manifest(
        inputs, build_matrix(resolve_variants(("jvm",)), 1)
    )
    value = manifest.model_dump()
    receipt: dict[str, object] = {
        "profile": {"path": "profiles/jvm.yaml", "sha256": "a" * 64},
        "distribution": {"path": "prepare/jvm/distribution.json", "sha256": "b" * 64},
        "evidence": {
            "registry": {"path": "prepare/jvm/registry.json", "sha256": "c" * 64}
        },
        "images": {
            "control-plane": {
                "reference": "registry/cp:tag",
                "digest": "sha256:" + "d" * 64,
            }
        },
        "source": inputs["nanofaas"],
        "gradleProperties": inputs["nativeProperties"],
        "declaredOptions": inputs["profiles"]["jvm"]["options"],
    }
    receipt[field] = None
    value["publications"] = {"jvm": receipt}
    with pytest.raises(ValueError, match="receipt"):
        ComparisonManifest.model_validate(value)


def test_supported_manifest_roundtrip_preserves_report_fields(captured, tmp_path):
    _, inputs = captured
    original = new_comparison_manifest(
        inputs, build_matrix(resolve_variants(("jvm",)), 1)
    )
    write_comparison_manifest(tmp_path, original)
    restored = read_comparison_manifest(tmp_path)
    assert restored == original
    assert restored is not None
    assert restored.variants[0]["key"] == "jvm"
    assert restored.functions == ["word-stats-java", "word-stats-javascript"]


@pytest.mark.parametrize(
    "section", ["profile-options", "function-settings", "vm-request"]
)
def test_partial_nested_identity_cannot_resume(captured, section):
    _, inputs = captured
    manifest = new_comparison_manifest(
        inputs, build_matrix(resolve_variants(("jvm",)), 1)
    )
    value = manifest.model_dump()
    if section == "profile-options":
        value["identity"]["profiles"]["jvm"]["options"] = {}
    elif section == "function-settings":
        value["identity"]["functions"]["word-stats-java"] = {"name": "word-stats-java"}
    else:
        value["identity"]["roles"]["stack"] = {"name": "experiment"}
    with pytest.raises(ValueError, match="identity"):
        ComparisonManifest.model_validate(value)
