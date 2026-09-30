from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from nanolab.tasks.recipe import read_distribution

PLATFORMS = ["linux/amd64", "linux/arm64"]
SOURCE = {"revision": "123", "dirty": False}
INDEX = "sha256:" + "a" * 64
MANIFESTS = {"linux/amd64": "sha256:" + "b" * 64, "linux/arm64": "sha256:" + "c" * 64}


def multiarch_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    recipe = tmp_path / "recipe.yaml"
    profile = {
        "schemaVersion": 2,
        "name": "multiarch",
        "registry": {
            "repository": "127.0.0.1:5000/nanofaas",
            "tag": "base",
            "platforms": PLATFORMS,
            "provenance": False,
        },
        "controlPlane": {
            "modules": ["build-metadata", "container-deployment-provider"],
            "build": {"mode": "jvm", "variant": "multiarch"},
            "container": {"image": "control-plane"},
        },
        "functions": [
            {
                "name": "word-stats",
                "sdk": "java",
                "build": {"mode": "jvm"},
                "container": {"image": "word-stats-java"},
            }
        ],
    }
    recipe.write_text(yaml.safe_dump(profile))
    data: dict[str, Any] = {
        "schemaVersion": 2,
        "recipe": {
            "name": "multiarch",
            "sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
        },
        "tag": "run-1",
        "source": dict(SOURCE),
        "modules": profile["controlPlane"]["modules"],
        "components": [],
    }
    for kind, name, image in [
        ("control-plane", "control-plane", "control-plane"),
        ("function", "word-stats", "word-stats-java"),
    ]:
        item = {
            "kind": kind,
            "name": name,
            "sdk": "java",
            "mode": "jvm",
            "image": {
                "reference": f"127.0.0.1:5000/nanofaas/{image}:run-1",
                "status": "published",
                "digest": INDEX,
                "platforms": PLATFORMS.copy(),
                "manifests": dict(MANIFESTS),
                "provenance": False,
            },
        }
        if kind == "control-plane":
            item.update(variant="multiarch", optimization="c2")
        data["components"].append(item)
    report = tmp_path / "distribution.json"
    report.write_text(json.dumps(data))
    return recipe, report, data


def test_read_multiarch_distribution_without_local_id(tmp_path: Path) -> None:
    from nanolab.tasks.recipe_multiarch import read_multiarch_distribution

    recipe, report, _ = multiarch_fixture(tmp_path)
    result = read_multiarch_distribution(
        report, recipe=recipe, tag="run-1", expected_source=SOURCE
    )
    assert result.source == SOURCE
    assert len(result.components) == 2
    for component in result.components:
        assert component.image.platforms == ("linux/amd64", "linux/arm64")
        assert component.image.digest == INDEX
        assert component.image.manifests == MANIFESTS
        assert not hasattr(component.image, "id")


@pytest.mark.parametrize(
    "failure",
    [
        "hash",
        "tag",
        "source",
        "dirty-type",
        "modules",
        "kind",
        "sdk",
        "mode",
        "variant",
        "optimization",
        "duplicate",
        "reference",
        "platform-duplicate",
        "platform-missing",
        "platform-extra",
        "manifest-missing",
        "manifest-extra",
        "digest",
        "child-digest",
        "provenance",
        "status",
        "schema",
        "empty",
    ],
)
def test_reject_inconsistent_multiarch_report(tmp_path: Path, failure: str) -> None:
    from nanolab.tasks.recipe_multiarch import read_multiarch_distribution

    recipe, report, data = multiarch_fixture(tmp_path)
    component = data["components"][0]
    image = component["image"]
    if failure == "hash":
        data["recipe"]["sha256"] = "wrong"
    elif failure == "tag":
        data["tag"] = "other"
    elif failure == "source":
        data["source"]["revision"] = "other"
    elif failure == "dirty-type":
        data["source"]["dirty"] = 0
    elif failure == "modules":
        data["modules"].append("unexpected")
    elif failure in {"kind", "sdk", "mode", "variant", "optimization"}:
        component[failure] = "wrong"
    elif failure == "duplicate":
        data["components"].append(component)
    elif failure == "reference":
        data["components"][1]["image"]["reference"] = image["reference"]
    elif failure == "platform-duplicate":
        image["platforms"].append("linux/amd64")
    elif failure == "platform-missing":
        image["platforms"].pop()
    elif failure == "platform-extra":
        image["platforms"].append("linux/s390x")
    elif failure == "manifest-missing":
        image["manifests"].pop("linux/amd64")
    elif failure == "manifest-extra":
        image["manifests"]["linux/s390x"] = INDEX
    elif failure == "digest":
        image["digest"] = "sha256:abc"
    elif failure == "child-digest":
        image["manifests"]["linux/amd64"] = "sha256:abc"
    elif failure == "provenance":
        image["provenance"] = True
    elif failure == "status":
        image["status"] = "published-unverified"
    elif failure == "schema":
        data["schemaVersion"] = True
    elif failure == "empty":
        data["components"] = []
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=r"Multiarch|multiarch|Recipe|recipe|sha256"):
        read_multiarch_distribution(
            report, recipe=recipe, tag="run-1", expected_source=SOURCE
        )


def test_reject_duplicate_json_platform_key(tmp_path: Path) -> None:
    from nanolab.tasks.recipe_multiarch import read_multiarch_distribution

    recipe, report, _ = multiarch_fixture(tmp_path)
    report.write_text(
        report.read_text().replace(
            '"linux/amd64":',
            '"linux/amd64": "sha256:' + "d" * 64 + '", "linux/amd64":',
            1,
        )
    )
    with pytest.raises(ValueError, match="Duplicate"):
        read_multiarch_distribution(
            report, recipe=recipe, tag="run-1", expected_source=SOURCE
        )


def test_local_reader_still_rejects_multiarch(tmp_path: Path) -> None:
    recipe, report, _ = multiarch_fixture(tmp_path)
    with pytest.raises(ValueError, match="Multi-platform"):
        read_distribution(report, recipe=recipe, tag="run-1", published=True)


def test_builder_property_is_forwarded_only_when_selected() -> None:
    from nanolab.tasks.recipe import recipe_command

    base = {"recipe": "recipe.yaml", "output": "distribution", "tag": "run-1"}
    original = recipe_command("publishRecipe", **base)
    selected = recipe_command("publishRecipe", **base, builder="owned-builder")
    assert "-PrecipeBuilder=owned-builder" in selected
    assert (
        tuple(arg for arg in selected if not arg.startswith("-PrecipeBuilder="))
        == original
    )
    assert not any(arg.startswith("-PrecipeBuilder=") for arg in original)


def publication_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None = None
):
    from sonata_engine import Resource, TaskInputs
    from sonata_tasks.execution.models import TaskResult

    from nanolab.tasks.recipe_builder import RecipeBuilder
    from nanolab.tasks.recipe_multiarch import multiarch_recipe_distribution_resource
    from tests.tasks.test_recipe_registry import fixture_fetch, registry_fixture
    from tests.workspace.test_recipe import _git

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    distribution, blobs = registry_fixture(fixture)
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "tracked").write_text("initial")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "initial")
    run_dir = tmp_path / "run"
    commands = []

    class Executor:
        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            commands.append(task)
            if task.argv[0] == "./gradlew":
                output = Path(
                    next(
                        arg.split("=", 1)[1]
                        for arg in task.argv
                        if arg.startswith("-PrecipeOutput=")
                    )
                )
                output.mkdir(parents=True, exist_ok=True)
                data = json.loads(distribution.report.read_bytes())
                data["source"] = {
                    "revision": _git(task.options.cwd, "rev-parse", "HEAD"),
                    "dirty": False,
                }
                if failure == "parser":
                    data["tag"] = "incorrect"
                (output / "distribution.json").write_text(json.dumps(data))
            return TaskResult(
                task_id="",
                status="failed" if failure == "command" else "passed",
                return_code=1 if failure == "command" else 0,
                stdout="publication output",
                stderr="publication failure" if failure == "command" else "",
            )

    def fetch(repository, kind, reference):
        assert not (run_dir / "runtime-images.json").exists()
        if failure == "registry":
            raise ValueError("registry verification failed")
        return fixture_fetch(blobs)(repository, kind, reference)

    monkeypatch.setattr("nanolab.tasks.recipe_registry.fetch_local_registry", fetch)
    builder = Resource(
        title="builder",
        acquire=lambda _: RecipeBuilder("owned-builder", "linux/arm64"),
        release=lambda *_: None,
    )
    value = RecipeBuilder("owned-builder", "linux/arm64")
    inputs = TaskInputs._for_resources({builder: value}, {builder})
    publication = multiarch_recipe_distribution_resource(
        source=source,
        recipe=fixture / "recipe.yaml",
        run_dir=run_dir,
        tag="run-1",
        executor=Executor(),
        functions=(("word-stats", "java"),),
        builder=builder,
    )
    return publication, inputs, commands, run_dir


def test_multiarch_publication_runs_once_before_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication, inputs, commands, run_dir = publication_fixture(tmp_path, monkeypatch)
    result = publication.acquire(inputs)
    gradle = [task for task in commands if task.argv[0] == "./gradlew"]
    assert len(gradle) == 1
    assert gradle[0].argv[1] == "publishRecipe"
    assert "-PrecipeBuilder=owned-builder" in gradle[0].argv
    assert gradle[0].options.cwd == run_dir / "recipe/source"
    assert (run_dir / "recipe/gradle.log").read_text().startswith("publication output")
    assert (run_dir / "recipe/recipe-inputs.json").is_file()
    assert all("@sha256:" in item.image.reference for item in result.components)
    assert len([task for task in commands if task.argv[:2] == ("docker", "pull")]) == 2
    mapping = json.loads((run_dir / "runtime-images.json").read_text())
    assert mapping["platform"] == "linux/arm64"
    assert len(mapping["images"]) == 2
    assert "id" not in json.loads(result.report.read_text())["components"][0]["image"]


@pytest.mark.parametrize("failure", ["command", "parser", "registry"])
def test_failed_publication_invalidates_previous_runtime_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    publication, inputs, _, run_dir = publication_fixture(
        tmp_path, monkeypatch, failure
    )
    run_dir.mkdir()
    mapping = run_dir / "runtime-images.json"
    mapping.write_text('{"old": true}')
    with pytest.raises(
        (ValueError, RuntimeError), match=r"publication|hash|tag|registry"
    ):
        publication.acquire(inputs)
    assert not mapping.exists()
    assert (run_dir / "recipe/gradle.log").exists()


def test_multiarch_source_change_fails_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication, inputs, commands, run_dir = publication_fixture(tmp_path, monkeypatch)
    publication.acquire(inputs)
    (run_dir / "recipe/source/tracked").write_text("tampered")
    commands.clear()
    with pytest.raises(ValueError, match="staged inputs"):
        publication.acquire(inputs)
    assert not commands
    assert not (run_dir / "runtime-images.json").exists()
