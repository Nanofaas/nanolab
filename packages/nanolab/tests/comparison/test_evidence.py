"""Publication evidence fixes the source and image identity before measurement."""

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.comparison.evidence import verify_comparison_publication
from nanolab.comparison.prepare import SOURCE_PROBE, ComparisonStage
from nanolab.comparison.profiles import comparison_profiles, declared_options
from nanolab.tasks.recipe import read_distribution
from nanolab.tasks.vm.models import VmRequest
from nanolab.workspace.recipe import RecipeRun

FIXED = [
    "-XX:MaxRAMPercentage=70",
    "-Xss256k",
    "-Dspring.main.banner-mode=off",
    "-Dspring.jmx.enabled=false",
    "-Dspring.devtools.restart.enabled=false",
    "-Dmanagement.endpoints.enabled-by-default=false",
]


class Publisher:
    def __init__(self, source, options):
        self.source = source
        self.options = options
        self.calls = []
        self.files = {}
        self.digest = "sha256:" + "a" * 64
        self.config = "sha256:" + "b" * 64
        self.entrypoint = [
            "/opt/jre/bin/java",
            "@/app/jvm.options",
            "@/app/launch.args",
        ]
        self.patch_override = None
        self.launch = "-jar\napp.jar\n"

    def exec_argv(self, request, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ("python3", "-c"):
            stdout = subprocess.check_output(
                ("python3", "-c", SOURCE_PROBE), cwd=self.source, text=True
            )
            if self.patch_override:
                state = json.loads(stdout)
                state["patchSha256"] = self.patch_override
                stdout = json.dumps(state)
        elif argv[:5] == ("docker", "buildx", "imagetools", "inspect", "--format"):
            stdout = json.dumps({"digest": self.digest})
        elif argv[:5] == ("docker", "buildx", "imagetools", "inspect", "--raw"):
            stdout = json.dumps(
                {"schemaVersion": 2, "config": {"digest": self.config}, "layers": []}
            )
        elif argv[:3] == ("docker", "image", "inspect"):
            stdout = json.dumps(
                [
                    {
                        "Id": self.config,
                        "Config": {
                            "Entrypoint": self.entrypoint,
                            "Cmd": None,
                            "WorkingDir": "/app",
                            "Env": [],
                        },
                    }
                ]
            )
        elif argv[:2] == ("docker", "create"):
            stdout = "c" * 64
        elif argv[:2] == ("docker", "cp"):
            self.files[argv[3]] = (
                "\n".join(self.options)
                if argv[2].endswith("jvm.options")
                else self.launch
            )
            stdout = ""
        elif argv[0] in ("mkdir", "rm") or argv[:2] in (
            ("docker", "rm"),
            ("docker", "pull"),
        ):
            stdout = ""
        elif argv[0] == "sha256sum":
            profile = self.source.parent / "profiles" / Path(argv[1]).name
            stdout = hashlib.sha256(profile.read_bytes()).hexdigest()
        else:
            pytest.fail(f"unexpected publication verification command {argv}")
        return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

    def transfer_from(self, request, *, source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.files[source])
        return SimpleNamespace(return_code=0, stdout="", stderr="")


@pytest.fixture
def publication(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(("git", *args), cwd=source)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Tests")
    (source / "tracked").write_text("original")
    git("add", ".")
    git("commit", "-qm", "initial")
    profiles = comparison_profiles(
        Path(__file__).resolve().parents[2], ("jvm", "native-o3-g1", "native-o3")
    )
    copied = {}
    (tmp_path / "profiles").mkdir()
    for key, profile in profiles.items():
        copy = tmp_path / "profiles" / f"{key}.yaml"
        copy.write_bytes(profile.read_bytes())
        copied[key] = copy
    profiles = copied
    runs = {
        key: RecipeRun(
            source, profile, tmp_path / "prepare" / key / "distribution", "recipe-test"
        )
        for key, profile in profiles.items()
    }
    stage = ComparisonStage(source, PurePosixPath("/remote/nanolab-recipe-test"), runs)
    inputs: dict[str, Any] = {
        "nanofaas": {
            "revision": git("rev-parse", "HEAD").decode().strip(),
            "patchSha256": hashlib.sha256(b"").hexdigest(),
        },
        "tag": "recipe-test",
        "profiles": {
            key: {
                "sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
                "options": declared_options(profile),
            }
            for key, profile in profiles.items()
        },
        "nativeProperties": {"buildMemory": None, "parallelism": None},
    }
    provider = Publisher(
        source, [*FIXED, "-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1"]
    )

    def verify(variant="jvm", mutation=None):
        run = runs[variant]
        options: dict[str, Any] = declared_options(run.recipe)
        components = [
            {
                "kind": "control-plane",
                "name": "control-plane",
                "sdk": "java",
                "mode": options["mode"],
                "variant": variant,
                "optimization": "c1" if variant == "jvm" else "3",
                "image": {
                    "reference": (
                        f"127.0.0.1:5000/nanofaas/control-plane-{variant}:recipe-test"
                    ),
                    "id": "sha256:" + "b" * 64,
                    "digest": "sha256:" + "a" * 64,
                    "status": "published",
                },
            }
        ]
        if options["native"]:
            components[0]["native"] = options["native"]
        for function in options["functions"]:
            components.append(  # noqa: PERF401 - explicit complete report rows
                {
                    "kind": "function",
                    "name": function["name"],
                    "sdk": function["sdk"],
                    "mode": function["mode"] or "container",
                    "image": {
                        "reference": (
                            f"127.0.0.1:5000/nanofaas/{function['image']}:recipe-test"
                        ),
                        "id": "sha256:" + "b" * 64,
                        "digest": "sha256:" + "a" * 64,
                        "status": "published",
                    },
                }
            )
        data = {
            "schemaVersion": 2,
            "recipe": {"sha256": hashlib.sha256(run.recipe.read_bytes()).hexdigest()},
            "tag": run.tag,
            "source": {"revision": inputs["nanofaas"]["revision"], "dirty": False},
            "modules": options["modules"],
            "components": components,
        }
        if mutation:
            mutation(data)
        run.output_dir.mkdir(parents=True, exist_ok=True)
        report = run.output_dir / "distribution.json"
        report.write_text(json.dumps(data))
        distribution = read_distribution(
            report, recipe=run.recipe, tag=run.tag, published=True
        )
        return verify_comparison_publication(
            distribution=distribution,
            stage=stage,
            variant=variant,
            inputs=inputs,
            provider=cast(VmCommandProvider, provider),
            request=VmRequest(lifecycle="multipass", name="test"),
            evidence_dir=tmp_path / "prepare" / variant,
        )

    return verify, provider, inputs, stage


def test_verified_jvm_receipt_records_declared_and_inspected_options(publication):
    verify, provider, _, _ = publication
    receipt = verify()
    assert set(receipt["images"]) == {
        "control-plane",
        "word-stats/java",
        "word-stats/javascript",
    }
    assert receipt["declaredOptions"]["jvm_args"] == [
        "-XX:+UseSerialGC",
        "-XX:TieredStopAtLevel=1",
    ]
    assert receipt["images"]["control-plane"]["digest"] == "sha256:" + "a" * 64
    assert any(argv[:2] == ("docker", "rm") for argv in provider.calls)
    assert not any(
        argv[:2] in (("docker", "run"), ("docker", "start")) for argv in provider.calls
    )


@pytest.mark.parametrize(
    "change",
    ["revision", "dirty", "digest", "config", "args", "launch", "entrypoint", "patch"],
)
def test_plausible_report_cannot_hide_another_artifact(publication, change):
    verify, provider, _, _ = publication
    mutation: Callable[[Any], None] | None = None
    if change in ("revision", "dirty", "digest"):

        def mutate(data):
            if change == "digest":
                data["components"][0]["image"]["digest"] = "sha256:short"
            else:
                data["source"][change] = "another" if change == "revision" else True

        mutation = mutate
    elif change == "config":
        provider.config = "sha256:" + "e" * 64
    elif change == "args":
        provider.options.append("-XX:TieredStopAtLevel=4")
    elif change == "entrypoint":
        provider.entrypoint = ["/bin/sh", "-c", "java"]
    elif change == "launch":
        provider.launch = "-jar\nother.jar\n"
    else:
        provider.patch_override = "f" * 64
    with pytest.raises(ValueError, match=r"Comparison|comparison|JVM|source|image"):
        verify(mutation=mutation)
    if change in ("args", "entrypoint", "launch"):
        assert any(argv[:2] == ("docker", "rm") for argv in provider.calls)


def test_native_g1_requires_effective_jfr_evidence(publication):
    verify, _, _, _ = publication
    receipt = verify("native-o3-g1")
    assert receipt["declaredOptions"]["native"]["monitoring"] == ["jfr"]

    def mutation(data):
        data["components"][0]["native"]["monitoring"] = []

    with pytest.raises(ValueError, match="native"):
        verify("native-o3-g1", mutation)


@pytest.fixture
def resume_case(publication, tmp_path):
    from dataclasses import asdict

    from nanolab.comparison.manifest import (
        ComparisonManifest,
        write_comparison_manifest,
    )
    from nanolab.plans.functions import ResolvedFunction

    verify, provider, inputs, stage = publication
    stage = ComparisonStage(
        stage.source,
        stage.remote_root,
        {key: stage.runs[key] for key in ("jvm", "native-o3")},
    )
    inputs["profiles"] = {key: inputs["profiles"][key] for key in stage.runs}
    jvm_receipt = verify("jvm")
    inputs["nativeProperties"] = {"buildMemory": "4g", "parallelism": 2}
    verify("native-o3")
    for key, run in stage.runs.items():
        provider.files[
            str(stage.remote_root / "distributions" / key / "distribution.json")
        ] = (run.output_dir / "distribution.json").read_text()
    empty_hash = hashlib.sha256(b"{}").hexdigest()
    identity = {
        **inputs,
        "nanolab": inputs["nanofaas"],
        "variants": ["native-o3"],
        "repetitions": 1,
        "scenario": {"values": {}, "sha256": empty_hash},
        "environment": {"values": {}, "sha256": empty_hash},
        "roles": {
            "stack": VmRequest(lifecycle="multipass", name="test").model_dump(
                mode="json", exclude={"proxmox_password"}
            )
        },
        "functions": {
            "word-stats-java": asdict(
                ResolvedFunction(
                    key="word-stats-java",
                    name="word-stats-java",
                    image="test/java",
                    build_argv=(),
                    payload="{}",
                )
            )
        },
        "scheduler": {
            "engine": "unified",
            "strategy": "per-function",
            "runtimeSwitching": False,
        },
    }
    manifest = ComparisonManifest(
        identity=identity,
        started_at="original",
        functions=["word-stats-java", "word-stats-javascript"],
        repetitions=1,
        order=["native-o3 run 1"],
        variants=[],
        regime={},
        publications={"jvm": jvm_receipt},
    )
    write_comparison_manifest(tmp_path, manifest)
    original_exec = provider.exec_argv
    publications = []

    def execute(request, argv, **kwargs):
        if argv[:2] == ("sh", "-c"):
            assert "publishRecipe" in argv[2]
            assert f"{stage.remote_root}/distributions/" not in argv[2].split(" > ")[-1]
            key = "jvm" if "profiles/jvm.yaml" in argv[2] else "native-o3"
            assert "bootJar" not in argv[2]
            if key == "jvm":
                assert "-PnativeBuildMemory" not in argv[2]
                assert "-PnativeParallelism" not in argv[2]
            else:
                assert "-PnativeBuildMemory=4g" in argv[2]
                assert "-PnativeParallelism=2" in argv[2]
            publications.append(key)
            provider.files[str(stage.remote_root / f"{key}.gradle.log")] = "published\n"
            return SimpleNamespace(return_code=0, stdout="", stderr="")
        return original_exec(request, argv, **kwargs)

    provider.exec_argv = execute
    return (
        {
            "stage": stage,
            "manifest": manifest,
            "root": tmp_path,
            "provider": cast(VmCommandProvider, provider),
            "request": VmRequest(lifecycle="multipass", name="test"),
        },
        publications,
        provider,
    )


def test_resume_reuses_committed_jvm_and_publishes_only_remaining_variant(resume_case):
    from nanolab.comparison.prepare import prepare_comparison

    kwargs, publications, _ = resume_case
    manifest = kwargs["manifest"]
    original = json.loads(json.dumps(manifest.publications["jvm"]))
    prepared = prepare_comparison(**kwargs)
    assert publications == ["native-o3"]
    assert set(prepared.distributions) == {"jvm", "native-o3"}
    assert manifest.publications["jvm"] == original
    assert (
        json.loads((kwargs["root"] / "comparison-manifest.json").read_text())[
            "started_at"
        ]
        == "original"
    )


def test_new_matrix_publishes_shared_functions_before_native(resume_case):
    from nanolab.comparison.prepare import prepare_comparison

    kwargs, publications, _ = resume_case
    kwargs["manifest"].publications = {}
    prepared = prepare_comparison(**kwargs)
    assert publications == ["jvm", "native-o3"]
    assert len(prepared.distributions["jvm"].components) == 3
    assert len(prepared.distributions["native-o3"].components) == 1


def test_missing_committed_registry_artifact_stops_without_republication(resume_case):
    from nanolab.comparison.prepare import prepare_comparison

    kwargs, publications, provider = resume_case
    before = (kwargs["root"] / "comparison-manifest.json").read_bytes()
    provider.digest = "sha256:" + "e" * 64
    with pytest.raises(ValueError, match="registry digest"):
        prepare_comparison(**kwargs)
    assert publications == []
    assert (kwargs["root"] / "comparison-manifest.json").read_bytes() == before


def test_failed_receipt_commit_preserves_prior_publications(resume_case, monkeypatch):
    from nanolab.comparison.prepare import prepare_comparison

    kwargs, publications, _ = resume_case
    before = (kwargs["root"] / "comparison-manifest.json").read_bytes()

    def fail(*args):
        raise OSError("crash before receipt commit")

    monkeypatch.setattr("nanolab.comparison.manifest.Path.replace", fail)
    with pytest.raises(OSError, match="crash"):
        prepare_comparison(**kwargs)
    assert publications == ["native-o3"]
    assert set(kwargs["manifest"].publications) == {"jvm"}
    assert (kwargs["root"] / "comparison-manifest.json").read_bytes() == before
    assert (kwargs["root"] / "prepare/native-o3/gradle.log").is_file()


@pytest.mark.parametrize("field", ["optimization", "gc", "builder", "monitoring"])
def test_native_report_cannot_change_declared_options(publication, field):
    verify, _, _, _ = publication

    def mutate(data):
        data["components"][0]["native"][field] = (
            [] if field == "monitoring" else "other"
        )

    with pytest.raises(ValueError, match="native"):
        verify("native-o3-g1", mutate)


@pytest.mark.parametrize(
    "change", ["profile-identity", "source-identity", "options-identity"]
)
def test_recorded_receipt_must_match_immutable_inputs(resume_case, change):
    from nanolab.comparison.prepare import prepare_comparison

    kwargs, publications, _ = resume_case
    identity = kwargs["manifest"].identity
    if change == "profile-identity":
        identity["profiles"]["jvm"]["sha256"] = "f" * 64
    elif change == "source-identity":
        identity["nanofaas"]["patchSha256"] = "e" * 64
    else:
        identity["profiles"]["jvm"]["options"]["jvm_args"] = ["-XX:+UseG1GC"]
    with pytest.raises(ValueError, match=r"Comparison|comparison"):
        prepare_comparison(**kwargs)
    assert publications == []


@pytest.mark.parametrize("indexed_id", [False, True])
@pytest.mark.parametrize("multi_platform", [False, True])
def test_single_platform_index_with_attestation_has_verified_config(
    indexed_id, multi_platform
):
    from nanolab.comparison.evidence import _registry_identity
    from nanolab.tasks.recipe import RecipeImage

    digest = "sha256:" + "a" * 64
    config = "sha256:" + "b" * 64
    platform = "sha256:" + "c" * 64
    index = {
        "manifests": [
            {"digest": platform, "platform": {"os": "linux", "architecture": "arm64"}},
            {
                "digest": "sha256:" + "d" * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": platform,
                },
            },
        ]
    }

    def command(argv):
        if "--format" in argv:
            assert argv[-2] == "{{json .Manifest}}"
            return json.dumps({"digest": digest})
        if argv[-1].endswith(platform):
            return json.dumps({"config": {"digest": config}})
        return json.dumps(index)

    if multi_platform:
        index["manifests"].append(
            {
                "digest": "sha256:" + "e" * 64,
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        )
    image = RecipeImage(
        reference="registry/image:tag",
        id=digest if indexed_id else config,
        digest=digest,
        status="published",
    )
    if multi_platform:
        with pytest.raises(ValueError, match="single-platform"):
            _registry_identity(image, command)
        return
    result = _registry_identity(image, command)
    assert result["config"] == config
    assert result["digest"] == digest
