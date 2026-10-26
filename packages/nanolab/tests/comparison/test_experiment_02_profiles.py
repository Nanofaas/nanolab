"""Experiment 02 recipes build exactly the composition each configuration names."""

from typing import Any

import pytest
import yaml

from nanolab.comparison.experiment_02 import CONFIGURATIONS_BY_KEY
from nanolab.comparison.profiles import (
    EXPERIMENT_01_CONTRACT,
    EXPERIMENT_02_CONTRACT,
    contract_for,
    experiment_02_options,
    experiment_02_profiles,
)
from nanolab.workspace.paths import discover_tool_root

TOOL_ROOT = discover_tool_root()
RECIPES = TOOL_ROOT / "recipes"
P1 = ["async-queue", "build-metadata", "container-deployment-provider"]
EXPECTED = {
    "nanofaas-p1-jvm": (P1, "jvm"),
    "nanofaas-p1-native": (P1, "native"),
    "nanofaas-p0-jvm": (["build-metadata"], "jvm"),
    "nanofaas-p0-native": (["build-metadata"], "native"),
}


@pytest.mark.parametrize("key", list(EXPECTED))
def test_each_recipe_declares_exactly_its_composition_and_runtime(key):
    configuration = CONFIGURATIONS_BY_KEY[key]
    options: dict[str, Any] = experiment_02_options(
        RECIPES / f"{configuration.recipe}.yaml",
        configuration,
        shares=key == "nanofaas-p1-jvm",
    )
    modules, mode = EXPECTED[key]
    assert options["modules"] == modules
    assert options["mode"] == mode
    if mode == "native":
        assert options["jvm_args"] == []
        assert options["native"]["gc"] == "serial"
        assert options["native"]["optimization"] == "3"
        assert options["native"]["maxHeap"] == "64m"
    else:
        assert options["jvm_args"] == ["-XX:+UseSerialGC", "-Xmx64m"]
    assert options["functions"] == (
        [{"name": "qr-code", "sdk": "rust", "mode": None, "image": "rust-qr-code"}]
        if key == "nanofaas-p1-jvm"
        else []
    )


def test_profiles_put_the_function_publisher_first():
    profiles = experiment_02_profiles(
        TOOL_ROOT, ("nanofaas-p0-native", "direct-reference")
    )
    assert list(profiles) == ["nanofaas-p1-jvm", "nanofaas-p0-native"]
    assert profiles["nanofaas-p0-native"] == RECIPES / "experiment-02-p0-native.yaml"


def test_profiles_refuse_unknown_or_repeated_keys():
    with pytest.raises(ValueError, match="unknown"):
        experiment_02_profiles(TOOL_ROOT, ("nanofaas-p2-jvm",))
    with pytest.raises(ValueError, match="unique"):
        experiment_02_profiles(TOOL_ROOT, ("nanofaas-p0-jvm", "nanofaas-p0-jvm"))


def test_only_the_publisher_may_declare_the_function():
    with pytest.raises(ValueError, match="publish"):
        experiment_02_options(
            RECIPES / "experiment-02-p1-jvm.yaml",
            CONFIGURATIONS_BY_KEY["nanofaas-p1-jvm"],
            shares=False,
        )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda r: r["controlPlane"]["modules"].append("autoscaler"), "modules"),
        (lambda r: r["controlPlane"]["jvm"]["args"].append("-Xmx128m"), "64m"),
        (lambda r: r["controlPlane"].update(config={"nanofaas": {}}), "configuration"),
    ],
)
def test_options_reject_a_recipe_that_drifts(tmp_path, edit, message):
    recipe = yaml.safe_load((RECIPES / "experiment-02-p0-jvm.yaml").read_text())
    edit(recipe)
    path = tmp_path / "experiment-02-p0-jvm.yaml"
    path.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError, match=message):
        experiment_02_options(
            path, CONFIGURATIONS_BY_KEY["nanofaas-p0-jvm"], shares=False
        )


def test_contract_is_chosen_from_the_identity_profile():
    assert contract_for({"experiment": {"profile": "experiment-02"}}) is (
        EXPERIMENT_02_CONTRACT
    )
    assert contract_for({"experiment": {"profile": "experiment-01"}}) is (
        EXPERIMENT_01_CONTRACT
    )
    assert EXPERIMENT_02_CONTRACT.modules("nanofaas-p0-jvm") == frozenset(
        {"build-metadata"}
    )
