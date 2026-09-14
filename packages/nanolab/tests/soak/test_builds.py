import base64
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from threading import Event

import pytest
from sonata_engine import Resource, Workflow

from nanolab.tasks.soak.builds import BuildImagesTask, ObservedBuild
from nanolab.tasks.soak.images import BuildRecipe, freeze_build_receipt


def synthetic_build(
    tmp_path, monkeypatch, variant, *, fail=False, platform="linux/amd64"
):
    """Use the real executor/task/collector, replacing only process execution."""
    from nanolab.tasks.soak.artifacts import fingerprint
    from nanolab.tasks.soak.build_executor import OwnedBuildCommandExecutor
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector
    from nanolab.tasks.soak.processes import OwnedCommandResult
    from nanolab.tasks.soak.sources import SourceEntry, SourceSnapshot

    root = tmp_path / "frozen"
    root.mkdir()
    text = (
        "FROM node:20-alpine AS build\nWORKDIR /src\nRUN npm run build\n"
        "FROM node:20-alpine\nCOPY --from=build /src/dist /app\n"
        if variant == "default"
        else "FROM oraclelinux:9-slim AS builder\nWORKDIR /workspace\n"
        'RUN ./gradlew "$NATIVE_TASK" $GRADLE_ARGS --no-daemon\n'
        "FROM scratch\nCOPY --from=builder /tmp/application /app\n"
    )
    (root / "Dockerfile").write_text(text)
    entry = SourceEntry(
        "Dockerfile",
        "file",
        (root / "Dockerfile").stat().st_mode & 0o777,
        len(text.encode()),
        hashlib.sha256(text.encode()).hexdigest(),
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(asdict(entry)))
    source = SourceSnapshot(
        root,
        fingerprint({"entries": [asdict(entry)]}),
        "revision",
        True,
        (entry,),
        manifest,
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    snapshot = Resource(
        title="Synthetic snapshot",
        acquire=lambda _: source,
        release=lambda _inputs, _value: None,
    )
    digest, child, base = ("sha256:" + char * 64 for char in "abc")
    base_name = "node" if variant == "default" else "oraclelinux"
    image = "localhost:5000/example:run"
    architecture = platform.split("/")[1]
    recipe = BuildRecipe(
        "example",
        "build",
        variant,
        platform,
        image,
        ("./gradlew", "bootJar") if variant == "jvm" else None,
        {
            "target": {
                "example": {
                    "context": ".",
                    "dockerfile": "Dockerfile",
                    "tags": [image],
                    "platforms": [platform],
                }
            }
        },
        "planned-recipe",
        None,
    )
    output = tmp_path / "output"
    executor = OwnedBuildCommandExecutor(
        cwd=tmp_path,
        log_dir=tmp_path / "raw-logs",
        cancelled=Event(),
        timeout_cap_s=30,
        artifact_limit_bytes=1024 * 1024,
    )
    predicate = {
        "buildType": "https://mobyproject.org/buildkit@v1",
        "materials": [
            {
                "uri": f"pkg:docker/library/{base_name}@latest",
                "digest": {"sha256": base.removeprefix("sha256:")},
            }
        ],
    }
    observed_commands = []

    def run(argv, *, cwd, log_path, **kwargs):
        observed_commands.append(argv)
        code = 0
        if argv[0] == "./gradlew" or argv[:3] == ("docker", "buildx", "bake"):
            work = Path(cwd)
            assert work != root
            request = json.loads((output / "metadata-0.json.request.json").read_text())
            assert request["workspace"] == str(work)
            scripts = tuple(work.glob(".nanolab-observation-*/*"))
            marker = re.search(
                r"NANOLAB_BUILD_OBSERVATION_[a-f0-9]+:",
                "\n".join(path.read_text() for path in scripts),
            ).group()  # pyright: ignore[reportOptionalMemberAccess]

            def evidence(name, version, **extra):
                record = {
                    "toolchain": name,
                    "argv": ["/used/bin/" + name, "--version"],
                    "output": version,
                    "exit_code": 0,
                    "execution_cwd": str(work),
                    **extra,
                }
                return (
                    marker
                    + base64.b64encode(json.dumps(record).encode()).decode()
                    + "\n"
                )

            if argv[0] == "./gradlew":
                assert request["prerequisite_argv"] == list(argv)
                body = evidence(
                    "java", 'openjdk version "25.0.1"\n', compiler="/used/bin/javac"
                )
                body += evidence(
                    "gradle", "Gradle 9.7.1\n", effective_gradle_version="9.7.1"
                )
                (work / "build").mkdir()
                (work / "build" / "compiled.jar").write_bytes(
                    b"synthetic compile output"
                )
            else:
                assert request["build_argv"] == list(argv)
                assert kwargs["env"]["BUILDX_METADATA_PROVENANCE"] == "max"
                assert "--progress=plain" in argv
                metadata = Path(argv[argv.index("--metadata-file") + 1])
                metadata.write_text(
                    json.dumps(
                        {
                            "example": {
                                "containerimage.digest": digest,
                                "buildx.build.ref": "builder/node0/id",
                                "buildx.build.provenance": predicate,
                            }
                        }
                    )
                )
                body = "published\n"
                if variant != "jvm":
                    stage = "build" if variant == "default" else "builder"
                    body += (
                        f"#5 [{stage} 1/4] FROM "
                        f"docker.io/library/{base_name}:latest@{base}\n"
                    )
                    name, version = (
                        ("node", "v20.19.0\n")
                        if variant == "default"
                        else ("native-image", "native-image 25.0.1\n")
                    )
                    body += "#7 1.234 " + evidence(
                        name,
                        version,
                        stage=stage,
                        compiler_command="native-image -jar app.jar",
                    )
            if fail:
                code = 7
        elif argv[:3] == ("docker", "buildx", "inspect"):
            assert Path(cwd) == output / "workspace-0"
            body = "Name: builder\nNodes:\nName: node0\nBuildKit version: v0.25.0\n"
            body += "Name: unrelated\nBuildKit version: v0.20.0\n"
        elif argv[:4] == ("docker", "buildx", "imagetools", "inspect"):
            assert "@sha256:" in argv[4]
            responses = {
                "{{json .Manifest}}": {
                    "digest": digest,
                    "manifests": [
                        {
                            "digest": child,
                            "platform": {"os": "linux", "architecture": architecture},
                        },
                        {
                            "digest": "sha256:" + "d" * 64,
                            "platform": {"os": "unknown", "architecture": "unknown"},
                            "annotations": {
                                "vnd.docker.reference.type": "attestation-manifest",
                                "vnd.docker.reference.digest": child,
                            },
                        },
                    ],
                },
                "{{json .Image}}": {"os": "linux", "architecture": architecture},
                "{{json .Provenance}}": {"SLSA": predicate},
            }
            body = json.dumps(responses[argv[-1]])
        else:
            raise AssertionError(f"unexpected external command: {argv}")
        encoded = body.encode()
        log_path.write_bytes(encoded)
        return OwnedCommandResult(code, False, True, log_bytes=len(encoded))

    monkeypatch.setattr("nanolab.tasks.soak.build_executor.run_owned_command", run)
    collector = BuildProvenanceCollector(executor.observe)
    task = BuildImagesTask(
        (recipe,),
        snapshot=snapshot,
        executor=executor,
        output_dir=output,
        collect=collector.collect,
        artifact_limit_bytes=1024 * 1024,
    )
    workflow = Workflow(workflow_id="synthetic-observation")
    workflow.add(task, requires=(snapshot,))
    return workflow, output, source, observed_commands


@pytest.mark.parametrize(
    ("variant", "expected"),
    [("jvm", "java"), ("default", "node"), ("native-o3", "native-image")],
)
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_real_task_producer_and_collector_freeze_observed_build(
    tmp_path, monkeypatch, variant, expected, platform
):
    from nanolab.tasks.soak.sources import verify_snapshot

    workflow, output, source, _commands = synthetic_build(
        tmp_path, monkeypatch, variant, platform=platform
    )
    workflow.run()
    receipt = json.loads((output / "build-0.json").read_text())
    assert receipt["source_fingerprint"] == source.fingerprint
    assert receipt["platform"] == platform
    assert receipt["recipe_fingerprint"] != "planned-recipe"
    assert receipt["image_digest"] == "localhost:5000/example@sha256:" + "a" * 64
    assert expected in dict(receipt["toolchains"])
    assert dict(receipt["toolchains"])["buildkit"] == "v0.25.0"
    observed = json.loads((output / "metadata-0.json.observations.json").read_text())
    assert observed["status"] == "complete"
    assert observed["source_fingerprint"] == source.fingerprint
    assert any(item["kind"] == "build" for item in observed["commands"])
    verify_snapshot(source)
    assert source.dirty is True
    assert not (source.root / "build").exists()


def test_failed_command_retains_observations_and_never_collects_or_freezes(
    tmp_path, monkeypatch
):
    workflow, output, _source, commands = synthetic_build(
        tmp_path, monkeypatch, "jvm", fail=True
    )
    with pytest.raises(RuntimeError, match="prerequisite example failed"):
        workflow.run()
    observed = json.loads((output / "metadata-0.json.observations.json").read_text())
    assert observed["status"] == "incomplete"
    assert observed["commands"][0]["exit_code"] == 7
    assert not (output / "frozen-images.json").exists()
    assert len(commands) == 1


def test_build_executor_rejects_prebuilt_fallback(tmp_path):
    recipe = BuildRecipe(
        "role",
        "prebuilt",
        "default",
        "linux/amd64",
        "image",
        None,
        None,
        "recipe",
        "receipt",
    )
    snapshot = Resource(
        title="Snapshot",
        acquire=lambda inputs: None,
        release=lambda inputs, value: None,
    )
    with pytest.raises(ValueError, match=r"build execution requires build"):
        BuildImagesTask(
            (recipe,),
            snapshot=snapshot,  # pyright: ignore[reportArgumentType]
            executor=None,  # pyright: ignore[reportArgumentType]
            output_dir=tmp_path,
            collect=lambda *args: None,  # pyright: ignore[reportArgumentType]
            artifact_limit_bytes=10000,
        )


def test_receipts_require_observed_effective_identities(tmp_path):
    recipe = BuildRecipe(
        "role",
        "build",
        "default",
        "linux/amd64",
        "registry/role:run",
        None,
        {},
        "recipe",
        None,
    )
    log = tmp_path / "build.log"
    log.write_text("observed build output")
    observed = ObservedBuild(
        "sha256:" + "a" * 64,
        {"java": "25.0.1"},
        {"runtime": "runtime@sha256:" + "b" * 64},
        (log,),
    )
    receipt = freeze_build_receipt(
        recipe,
        "source",
        observed.digest,
        toolchains=observed.toolchains,
        base_images=observed.base_images,
        logs=observed.logs,
    )
    assert receipt.image_digest == "registry/role@" + observed.digest
    with pytest.raises(ValueError, match="build provenance requires source"):
        freeze_build_receipt(
            recipe,
            "source",
            observed.digest,
            toolchains={},
            base_images=observed.base_images,
            logs=observed.logs,
        )


def test_receipt_binds_the_recipe_that_was_requested_and_the_one_built(tmp_path):
    """Build observation rewrites the recipe, so the two fingerprints differ.

    The receipt has to carry both, or nothing offline can tie the image back
    to the recipe the scenario actually asked for.
    """
    from nanolab.tasks.soak.images import BuildReceipt

    receipt = BuildReceipt(
        "control-plane",
        "localhost:5000/x@sha256:" + "0" * 64,
        "source",
        "effective-fingerprint",
        "build",
        "linux/arm64",
        (),
        (),
        (),
        original_recipe_fingerprint="requested-fingerprint",
    )

    assert receipt.recipe_fingerprint == "effective-fingerprint"
    assert receipt.original_recipe_fingerprint == "requested-fingerprint"
    # A build with no instrumentation is its own original.
    plain = BuildReceipt(
        "f", "i@sha256:" + "0" * 64, "s", "same", "b", "linux/arm64", (), (), ()
    )
    assert plain.original_recipe_fingerprint is None
