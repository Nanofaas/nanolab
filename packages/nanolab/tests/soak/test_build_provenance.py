"""Published BuildKit observations, with no Docker or network execution."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from nanolab.tasks.soak.images import BuildRecipe

DIGEST = "sha256:" + "a" * 64
CHILD = "sha256:" + "b" * 64
BASE = "c" * 64


def fixture(tmp_path, variant="jvm", nested=True):
    from nanolab.tasks.soak.build_provenance import write_observation_request

    work = tmp_path / "workspace"
    work.mkdir()
    metadata = tmp_path / "metadata.json"
    image = "localhost:5000/nanofaas/example:run-1"
    bake = {"target": {"example": {"tags": [image], "platforms": ["linux/amd64"]}}}
    recipe = BuildRecipe(
        "example",
        "build",
        variant,
        "linux/amd64",
        image,
        ("./gradlew", "bootJar", "--no-daemon") if variant == "jvm" else None,
        bake,
        "recipe-identity",
        None,
    )
    bake_path = tmp_path / "bake.json"
    bake_path.write_text(json.dumps(bake))
    build_argv = (
        "docker",
        "buildx",
        "bake",
        "-f",
        str(bake_path),
        "--push",
        "--provenance=mode=max",
        "--metadata-file",
        str(metadata),
    )
    request_path = write_observation_request(
        recipe,
        metadata,
        work,
        build_argv=build_argv,
    )
    request = json.loads(request_path.read_text())
    predicate = {
        "buildType": "https://mobyproject.org/buildkit@v1",
        "builder": {"id": ""},
        "materials": [
            {
                "uri": "pkg:docker/library/node@22?platform=linux%2Famd64",
                "digest": {"sha256": BASE},
            }
        ],
        "invocation": {"environment": {"platform": "linux/amd64"}},
        "metadata": {"buildInvocationID": "invocation-1"},
    }
    record = {
        "containerimage.digest": DIGEST,
        "containerimage.descriptor": {"digest": DIGEST},
        "buildx.build.ref": "builder/node0/invocation-1",
        "buildx.build.provenance": deepcopy(predicate),
    }
    metadata.write_text(json.dumps({"example": record} if nested else record))
    manifest = {
        "digest": DIGEST,
        "manifests": [
            {"digest": CHILD, "platform": {"os": "linux", "architecture": "amd64"}},
            {
                "digest": "sha256:" + "d" * 64,
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": CHILD,
                },
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ],
    }
    responses = {
        "{{json .Manifest}}": manifest,
        "{{json .Image}}": {"os": "linux", "architecture": "amd64"},
        "{{json .Provenance}}": {"SLSA": deepcopy(predicate)},
    }
    commands = []

    def command(kind, argv, text, **extra):
        log = tmp_path / f"command-{len(commands)}.log"
        body = text.encode()
        log.write_bytes(body)
        commands.append(
            {
                "kind": kind,
                "argv": list(argv),
                "cwd": str(work),
                "exit_code": 0,
                "log": {
                    "path": log.name,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size_bytes": len(body),
                },
                **extra,
            }
        )

    command("build", build_argv, "Build completed and published\n")
    command(
        "toolchain",
        ("docker", "buildx", "inspect", "builder"),
        "Name: builder\nName: node0\nBuildKit version: v0.25.0\n",
        toolchain="buildkit",
        scope="builder",
        build_ref=record["buildx.build.ref"],
    )
    if variant == "jvm":
        command("prerequisite", recipe.prerequisite_argv, "BUILD SUCCESSFUL\n")
        command(
            "toolchain",
            ("/jdk/bin/java", "-version"),
            'openjdk version "25.0.1" 2025-10-21\n',
            toolchain="java",
            scope="host",
        )
        command(
            "toolchain",
            ("./gradlew", "--version"),
            "Gradle 9.1.0\n",
            toolchain="gradle",
            scope="host",
        )
    else:
        name, text = (
            ("native-image", "native-image 25.0.1 2025-10-21\n")
            if variant.startswith("native-")
            else ("node", "v22.20.0\n")
        )
        command(
            "toolchain",
            (name, "--version"),
            text,
            toolchain=name,
            scope="build",
            material="library/node@sha256:" + BASE,
        )
    observation_path = metadata.with_name(metadata.name + ".observations.json")
    observation = {
        "schema": "nanolab-soak-build-observations-v1",
        "request_id": request["request_id"],
        "recipe_fingerprint": recipe.recipe_fingerprint,
        "workspace": str(work),
        "image_digest": DIGEST,
        "commands": commands,
    }
    observation_path.write_text(json.dumps(observation))
    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        assert argv[:4] == ("docker", "buildx", "imagetools", "inspect")
        assert "@sha256:" in argv[4]
        return json.dumps(responses[argv[-1]]).encode()

    return (
        recipe,
        metadata,
        work,
        runner,
        responses,
        observation,
        observation_path,
        calls,
    )


@pytest.mark.parametrize("variant", ["jvm", "native-o3", "default"])
@pytest.mark.parametrize("nested", [True, False])
def test_collects_published_observed_identities_and_evidence(tmp_path, variant, nested):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector
    from nanolab.tasks.soak.builds import ObservedBuild

    recipe, metadata, work, runner, _, _, _, calls = fixture(tmp_path, variant, nested)
    result = BuildProvenanceCollector(runner, timeout_s=7).collect(
        recipe, metadata, work
    )
    assert isinstance(result, ObservedBuild)
    assert result.digest == DIGEST
    assert result.base_images == {
        "pkg:docker/library/node@22?platform=linux%2Famd64": "library/node@sha256:"
        + BASE
    }
    assert result.toolchains["buildkit"] == "v0.25.0"
    if variant == "jvm":
        assert result.toolchains["java"] == "25.0.1"
        assert result.toolchains["gradle"] == "9.1.0"
    assert metadata in result.logs
    assert all(path.is_file() for path in result.logs)
    assert len(calls) == 3
    assert all(timeout == 7 for _, timeout in calls)
    assert calls[1][0][4].endswith("@" + CHILD)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("digest", "digest"),
        ("platform", "platform"),
        ("attestation", "attestation"),
        ("materials", "material"),
        ("local-material", "material"),
        ("missing-provenance", "provenance"),
        ("wrong-subject", "subject"),
    ],
)
def test_rejects_unbound_or_incomplete_published_evidence(tmp_path, change, match):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, runner, responses, _, _, _ = fixture(tmp_path)
    if change == "digest":
        responses["{{json .Manifest}}"]["digest"] = CHILD
    elif change == "platform":
        responses["{{json .Image}}"]["architecture"] = "arm64"
    elif change == "attestation":
        responses["{{json .Manifest}}"]["manifests"].pop()
    elif change == "materials":
        responses["{{json .Provenance}}"]["SLSA"]["materials"] = []
    elif change == "local-material":
        responses["{{json .Provenance}}"]["SLSA"]["materials"][0]["digest"][
            "sha256"
        ] = "e" * 64
    elif change == "missing-provenance":
        responses["{{json .Provenance}}"] = {}
    else:
        predicate = responses["{{json .Provenance}}"]["SLSA"]
        responses["{{json .Provenance}}"]["SLSA"] = {
            "predicateType": "https://slsa.dev/provenance/v0.2",
            "predicate": predicate,
            "subject": [{"digest": {"sha256": "f" * 64}}],
        }
    with pytest.raises(ValueError, match=match):
        BuildProvenanceCollector(runner).collect(recipe, metadata, work)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "request",
        "recipe",
        "workspace",
        "digest",
        "failed",
        "hash",
        "size",
        "argv",
        "missing-java",
        "missing-gradle",
        "fake-version",
        "scope",
    ],
)
def test_rejects_invalid_build_stage_observations(tmp_path, change):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, runner, _, observed, path, _ = fixture(tmp_path)
    if change == "missing":
        path.unlink()
    else:
        if change in {"request", "recipe", "workspace", "digest"}:
            key = {
                "request": "request_id",
                "recipe": "recipe_fingerprint",
                "workspace": "workspace",
                "digest": "image_digest",
            }[change]
            observed[key] = "different"
        elif change == "failed":
            observed["commands"][0]["exit_code"] = 1
        elif change == "hash":
            observed["commands"][0]["log"]["sha256"] = "f" * 64
        elif change == "size":
            observed["commands"][0]["log"]["size_bytes"] = 0
        elif change == "argv":
            observed["commands"][0]["argv"] = ["true"]
        elif change.startswith("missing-"):
            observed["commands"] = [
                item
                for item in observed["commands"]
                if item.get("toolchain") != change.removeprefix("missing-")
            ]
        elif change == "fake-version":
            item = next(
                item for item in observed["commands"] if item.get("toolchain") == "java"
            )
            item["toolchain"] = "invented"
        else:
            next(
                item for item in observed["commands"] if item.get("toolchain") == "java"
            )["scope"] = "build"
        path.write_text(json.dumps(observed))
    with pytest.raises((ValueError, OSError)):
        BuildProvenanceCollector(runner).collect(recipe, metadata, work)


def test_metadata_provenance_optional_but_published_provenance_required(tmp_path):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, runner, _, _, _, _ = fixture(tmp_path)
    data = json.loads(metadata.read_text())
    data["example"].pop("buildx.build.provenance")
    metadata.write_text(json.dumps(data))
    assert (
        BuildProvenanceCollector(runner).collect(recipe, metadata, work).digest
        == DIGEST
    )


def test_selects_matching_platform_provenance(tmp_path):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, runner, responses, _, _, _ = fixture(tmp_path)
    responses["{{json .Provenance}}"] = {
        "linux/amd64": responses["{{json .Provenance}}"],
        "linux/arm64": {},
    }
    assert (
        BuildProvenanceCollector(runner).collect(recipe, metadata, work).digest
        == DIGEST
    )


def test_timeout_and_oversized_output_fail_without_fallback(tmp_path):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, _, _, _, _, _ = fixture(tmp_path)

    def timeout(argv, timeout):
        raise TimeoutError("registry unavailable")

    with pytest.raises(TimeoutError, match="registry unavailable"):
        BuildProvenanceCollector(timeout).collect(recipe, metadata, work)
    with pytest.raises(ValueError, match=r"registry provenance output exceeds"):
        BuildProvenanceCollector(
            lambda argv, timeout: b" " * 4097, max_output_bytes=4096
        ).collect(recipe, metadata, work)


def test_prebuilt_and_changed_recipe_cannot_reuse_build_observations(tmp_path):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, runner, _, _, _, _ = fixture(tmp_path)
    for changed in (
        replace(recipe, mode="prebuilt"),
        replace(recipe, recipe_fingerprint="other"),
    ):
        # Invalid bindings intentionally exercise different validation messages.
        with pytest.raises(
            ValueError,
            match=(
                r"build request differs from recipe|collector requires a build recipe"
            ),
        ):
            BuildProvenanceCollector(runner).collect(changed, metadata, work)


def test_symlink_log_cannot_escape_evidence_directory(tmp_path):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, runner, _, observed, _path, _ = fixture(tmp_path)
    log = tmp_path / observed["commands"][0]["log"]["path"]
    moved = tmp_path / "moved.log"
    log.rename(moved)
    log.symlink_to(moved)
    with pytest.raises(ValueError, match="symlink evidence is unsupported"):
        BuildProvenanceCollector(runner).collect(recipe, metadata, work)


def test_request_is_exclusive_and_requires_actual_bake_definition(tmp_path):
    from nanolab.tasks.soak.build_provenance import write_observation_request

    recipe, metadata, work, _, _, observed, _, _ = fixture(tmp_path)
    argv = tuple(observed["commands"][0]["argv"])
    with pytest.raises(FileExistsError, match="Errno 17"):
        write_observation_request(recipe, metadata, work, build_argv=argv)
    changed = replace(recipe, bake={"target": {"wrong": {}}})
    # Reject the invalid definition without prescribing validation order.
    with pytest.raises(ValueError, match="bake target image"):
        write_observation_request(changed, metadata, work, build_argv=argv)


def test_parent_traversal_is_rejected_even_with_matching_log_hash(tmp_path):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    recipe, metadata, work, runner, _, observed, path, _ = fixture(tmp_path)
    log = observed["commands"][0]["log"]
    log["path"] = "../" + tmp_path.name + "/" + log["path"]
    path.write_text(json.dumps(observed))
    with pytest.raises(ValueError, match=r"parent traversal in evidence path is"):
        BuildProvenanceCollector(runner).collect(recipe, metadata, work)
