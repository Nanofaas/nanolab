"""Deferred preparation contracts; all source/build effects are injected."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.images import BuildReceipt, BuildRecipe
from nanolab.tasks.soak.preparation import (
    PreparationOptions,
    check_preparation_support,
    prepare_soak,
)


def policy():
    path = (
        Path(__file__).resolve().parents[2]
        / "scenarios-v2/memory-soak-smoke-container.yaml"
    )
    return SoakConfig.model_validate(yaml.safe_load(path.read_text())["soak"])


def options(**changes):
    return PreparationOptions(
        generator_command=(sys.executable,),
        support_check=lambda config: None,
        **changes,
    )


def test_missing_diagnostics_rejected_before_build():
    config = policy()
    config.diagnostics.operations = {"control-plane": ["histogram"]}
    with pytest.raises(ValueError, match="diagnostic adapter unavailable"):
        check_preparation_support(config, options())


def test_provider_declaration_is_not_an_adapter_or_capability_receipt():
    config = policy()
    config.diagnostics.operations = {"control-plane": ["gc"]}
    declared = options(diagnostic_provider_available=True)
    assert declared.diagnostic_adapter is None
    check_preparation_support(config, declared)
    # Availability of wiring cannot bypass the independent prerequisite gate.
    config.prerequisites.required_coverage = ["sync"]
    with pytest.raises(ValueError, match=r"isolated prerequisite runner"):
        check_preparation_support(config, declared)


def test_missing_prerequisite_runner_rejected_before_build():
    config = policy()
    config.prerequisites.required_coverage = ["sync"]
    with pytest.raises(ValueError, match=r"isolated prerequisite runner"):
        check_preparation_support(config, options())


def test_missing_generator_rejected_before_build(monkeypatch):
    monkeypatch.setattr("nanolab.tasks.soak.preparation.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match=r"generator executable unavailable"):
        check_preparation_support(policy(), options())


def test_unsupported_prebuilt_rejected_before_build():
    config = policy()
    config.images["control-plane"].mode = "prebuilt"
    with pytest.raises(ValueError, match=r"prebuilt provenance import is"):
        check_preparation_support(config, options())


@pytest.mark.parametrize("automatic_diagnostics", [False, True])
def test_single_source_and_observer_injection(
    tmp_path, monkeypatch, automatic_diagnostics
):
    from sonata_engine import Task, TaskOutcome

    import nanolab.tasks.soak.preparation as module

    config = policy()
    if automatic_diagnostics:
        config.diagnostics.operations = {"control-plane": ["gc"]}
        config.diagnostics.gc_completion_evidence = {
            "control-plane": "jdk.GarbageCollection"
        }
    seen = []
    source = SimpleNamespace(root=tmp_path / "source", fingerprint="single-source")
    recipes = tuple(
        BuildRecipe(
            role,
            "build",
            "jvm",
            "linux/amd64",
            "registry/" + role + ":run",
            None,
            {},
            role + "-recipe",
            None,
        )
        for role in config.roles
    )
    receipts = tuple(
        BuildReceipt(
            role,
            "registry/" + role + "@sha256:" + "a" * 64,
            source.fingerprint,
            role + "-recipe",
            role + "-build",
            "linux/amd64",
            (),
            (),
            (),
        )
        for role in config.roles
    )
    observer = object()

    def capture(*args, **kwargs):
        seen.append("snapshot")
        return source

    class Build(Task):
        title = "Synthetic build"

        def __init__(self, actual, **kwargs):
            assert actual == recipes
            assert kwargs["collect"] is observer
            self.snapshot = kwargs["snapshot"]

        def run(self, inputs):
            assert inputs.resource(self.snapshot) is source
            seen.extend(item.role for item in receipts)
            return TaskOutcome(value=receipts)

    monkeypatch.setattr(module, "capture_source_snapshot", capture)
    monkeypatch.setattr(module, "plan_images", lambda *a, **k: recipes)
    monkeypatch.setattr(module, "BuildImagesTask", Build)
    prepared = prepare_soak(
        config,
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
        options=options(diagnostic_provider_available=automatic_diagnostics),
        build_observer=observer,
    )
    assert seen == ["snapshot", *config.roles]
    assert prepared.images == {item.role: item.image_digest for item in receipts}
    assert prepared.builds_finished_s <= prepared.frozen_at_s
    prepared.writer.close()


def test_unknown_payload_stops_before_source_snapshot(tmp_path, monkeypatch):
    import nanolab.tasks.soak.preparation as module

    config = policy()
    config.workload.rates = {"unknown-function": 1.0}
    monkeypatch.setattr(
        module,
        "capture_source_snapshot",
        lambda *a, **k: pytest.fail("source captured"),
    )
    with pytest.raises(ValueError, match="validation error for SoakConfig"):
        prepare_soak(
            config,
            run_dir=tmp_path / "run",
            repo_root=tmp_path,
            tool_root=tmp_path,
            options=options(),
            build_observer=object(),
        )
    assert not (tmp_path / "run").exists()


def test_explicit_cases_have_exact_expected_output():
    from nanolab.tasks.soak.preparation import _payloads

    values = _payloads(policy(), options())
    expected = next(iter(values.values()))[0]["expected"]
    assert expected == {
        "wordCount": 3,
        "uniqueWords": 2,
        "topWords": [{"word": "hello", "count": 2}, {"word": "world", "count": 1}],
        "averageWordLength": 5.0,
    }
