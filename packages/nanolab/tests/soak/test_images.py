"""Soak recipes reuse the image catalogue without replacing native SDKs by JVMs."""

import pytest

from nanolab.config.soak import ImageBuildSpec
from nanolab.workspace.paths import default_tool_paths


def specs(native=False):
    variant = "native-o3" if native else "jvm"
    return {
        "control-plane": ImageBuildSpec(
            variant=variant,
            platform="linux/amd64",
            modules=["container-deployment-provider", "async-queue"],
        ),
        "word-stats-java": ImageBuildSpec(variant=variant, platform="linux/amd64"),
        "word-stats-javascript": ImageBuildSpec(
            variant="default", platform="linux/amd64"
        ),
    }


def plan(native=False, images=None, run_id="run-1"):
    from nanolab.tasks.soak.images import plan_images

    runtime = "native" if native else "jvm"
    return plan_images(
        default_tool_paths().nanofaas_root,
        specs(native) if images is None else images,
        {
            "control-plane": runtime,
            "word-stats-java": runtime,
            "word-stats-javascript": "node",
        },
        registry="registry.test:5000/nanofaas",
        run_id=run_id,
    )


def test_build_key_changes_with_each_input():
    from nanolab.tasks.soak.images import build_key

    key = build_key("sdk-a", "recipe-a", "linux/amd64")
    assert key != build_key("sdk-b", "recipe-a", "linux/amd64")
    assert key != build_key("sdk-a", "recipe-b", "linux/amd64")
    assert key != build_key("sdk-a", "recipe-a", "linux/arm64")


def test_all_required_images_have_recipes_without_existing_tags():
    recipes = {recipe.role: recipe for recipe in plan()}
    assert set(recipes) == {"control-plane", "word-stats-java", "word-stats-javascript"}
    cp = recipes["control-plane"]
    assert (
        "-PcontrolPlaneModules=container-deployment-provider,async-queue"
        in cp.prerequisite_argv
    )
    assert "-PcontrolPlaneModules=all" not in cp.prerequisite_argv
    assert (
        ":functions:java:word-stats:bootJar"
        in recipes["word-stats-java"].prerequisite_argv
    )
    js = recipes["word-stats-javascript"]
    target = next(iter(js.bake["target"].values()))
    assert target["context"] == "."
    assert target["dockerfile"] == "functions/javascript/word-stats/Dockerfile"


def test_native_function_is_compiled_not_replaced_with_jvm():
    recipes = {recipe.role: recipe for recipe in plan(native=True)}
    java = recipes["word-stats-java"]
    target = next(iter(java.bake["target"].values()))
    assert java.prerequisite_argv is None
    assert target["dockerfile"] == "deploy/native-java/Dockerfile"
    assert target["args"]["NATIVE_TASK"] == ":functions:java:word-stats:nativeCompile"
    assert target["args"]["GRAALVM_DISTRIBUTION"] == "community"
    assert "-PnativeOptimization=3" in target["args"]["GRADLE_ARGS"]
    assert "-PnativeGc=G1" not in target["args"]["GRADLE_ARGS"]


def test_native_g1_preserves_its_explicit_distribution():
    images = specs(True)
    images["word-stats-java"] = ImageBuildSpec(
        variant="native-o3-g1", platform="linux/amd64"
    )
    java = next(
        recipe for recipe in plan(True, images) if recipe.role == "word-stats-java"
    )
    args = next(iter(java.bake["target"].values()))["args"]
    assert args["GRAALVM_DISTRIBUTION"] == "oracle"
    assert "-PnativeGc=G1" in args["GRADLE_ARGS"]


def test_different_run_tags_do_not_change_build_input_identity():
    first = plan(run_id="run-1")
    second = plan(run_id="run-2")
    assert first[0].image != second[0].image
    assert first[0].recipe_fingerprint == second[0].recipe_fingerprint


def test_modules_are_part_of_the_recipe_identity():
    images = specs()
    images["control-plane"] = ImageBuildSpec(
        variant="jvm", platform="linux/amd64", modules=[]
    )
    assert plan()[0].recipe_fingerprint != plan(images=images)[0].recipe_fingerprint


def test_prebuilt_is_explicit_and_has_no_build_recipe():
    images = specs()
    image = "registry.test:5000/fn@sha256:" + "a" * 64
    images["word-stats-java"] = ImageBuildSpec(
        mode="prebuilt",
        variant="jvm",
        platform="linux/amd64",
        digest=image,
        provenance_receipt="java-receipt.json",
    )
    java = next(
        recipe for recipe in plan(images=images) if recipe.role == "word-stats-java"
    )
    assert java.image == image
    assert java.bake is None
    assert java.prerequisite_argv is None


def test_native_request_cannot_use_a_jvm_recipe():
    with pytest.raises(ValueError, match=r"runtime and variant disagree for"):
        plan(native=True, images=specs(False))


def test_unsupported_options_are_not_silently_ignored():
    images = specs()
    images["control-plane"] = ImageBuildSpec(
        variant="jvm", platform="linux/amd64", build_options={"NATIVE_TASK": ":wrong"}
    )
    with pytest.raises(ValueError, match="unsupported build options"):
        plan(images=images)


def test_receipt_requires_real_base_and_toolchain_evidence(tmp_path):
    from nanolab.tasks.soak.images import freeze_build_receipt

    recipe = plan()[0]
    log = tmp_path / "build.log"
    log.write_text("build output")
    with pytest.raises(ValueError, match="build provenance requires source"):
        freeze_build_receipt(
            recipe,
            "source-fingerprint",
            "sha256:" + "a" * 64,
            toolchains={},
            base_images={},
            logs=(log,),
        )
    receipt = freeze_build_receipt(
        recipe,
        "source-fingerprint",
        "sha256:" + "a" * 64,
        toolchains={"java": "25", "buildkit": "identified-builder"},
        base_images={"runtime": "runtime@sha256:" + "b" * 64},
        logs=(log,),
    )
    assert receipt.image_digest.endswith("@sha256:" + "a" * 64)
    assert ":soak-" not in receipt.image_digest
    assert receipt.source_fingerprint == "source-fingerprint"


def test_prebuilt_digest_mismatch_cannot_be_frozen(tmp_path):
    from nanolab.tasks.soak.images import freeze_build_receipt

    images = specs()
    images["word-stats-java"] = ImageBuildSpec(
        mode="prebuilt",
        variant="jvm",
        platform="linux/amd64",
        digest="fn@sha256:" + "a" * 64,
        provenance_receipt="receipt.json",
    )
    recipe = next(
        item for item in plan(images=images) if item.role == "word-stats-java"
    )
    log = tmp_path / "log"
    log.write_text("inspect")
    with pytest.raises(ValueError, match="prebuilt image digest differs from the r"):
        freeze_build_receipt(
            recipe,
            "source",
            "sha256:" + "b" * 64,
            toolchains={"java": "25"},
            base_images={"runtime": "runtime@sha256:" + "c" * 64},
            logs=(log,),
        )
