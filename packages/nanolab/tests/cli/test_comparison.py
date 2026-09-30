"""Comparison orchestration preserves immutable experiments across resumes."""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from nanolab.cli import comparison, product
from nanolab.comparison.manifest import write_comparison_manifest
from nanolab.comparison.profiles import PreparedComparison, declared_options
from nanolab.tasks.recipe import read_distribution
from nanolab.workspace.paths import ToolPaths

PACKAGE = Path(__file__).resolve().parents[2]


@pytest.fixture
def command_case(tmp_path, monkeypatch, nanofaas_checkout):
    scenario = tmp_path / "scenario.yaml"
    environment = tmp_path / "environment.yaml"
    scenario.write_text(
        "workflow: loadtest\nbackend: k8s\nloadProfile: comparison\n"
        "functions: [word-stats-java, word-stats-javascript]\n"
    )
    environment.write_text("provider: multipass\nroles:\n  stack: {name: original}\n")
    root = tmp_path / "run"
    events = []
    state = {"cluster": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    paths = ToolPaths.from_roots(nanofaas_checkout, PACKAGE)
    monkeypatch.setattr(product, "default_tool_paths", lambda: paths)

    @contextmanager
    def provision(*args):
        events.append("provision")
        assert args[-1] is True
        assert (
            json.loads((root / "comparison-manifest.json").read_text())["target"]
            is None
        )
        yield

    monkeypatch.setattr(product, "_provisioning_context", provision)

    class Provider:
        def exec_argv(self, request, argv, **kwargs):
            if argv[0] == "cat":
                stdout = "a" * 32
            elif argv[:2] == ("sudo", "cat"):
                stdout = "11111111-2222-3333-4444-555555555555"
            elif "namespace" in argv:
                stdout = json.dumps({"metadata": {"uid": state["cluster"]}})
            elif "nodes" in argv:
                stdout = json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "node",
                                    "uid": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                                }
                            }
                        ]
                    }
                )
            elif "--format" in argv:
                stdout = "sha256:" + "a" * 64
            elif "--raw" in argv:
                stdout = json.dumps({"config": {"digest": "sha256:" + "b" * 64}})
            elif argv[:2] == ("rm", "-rf"):
                events.append(("cleanup", argv[-1]))
                stdout = ""
            elif argv[:2] == ("sh", "-c"):
                events.append("leftovers")
                stdout = ""
            else:
                pytest.fail(f"unexpected command: {argv}")
            return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

    provider = Provider()
    monkeypatch.setattr(
        comparison, "provider_for_environment", lambda *args: provider, raising=False
    )

    def prepare(*args, **kwargs):
        events.append("prepare")
        manifest = kwargs["manifest"]
        assert manifest.target is not None
        profiles = kwargs["profiles"]
        distributions = {}

        def descriptor(path):
            return {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        for key, original_profile in profiles.items():
            profile = root / "profiles" / f"{key}.yaml"
            profile.parent.mkdir(parents=True, exist_ok=True)
            profile.write_bytes(original_profile.read_bytes())
            options: dict[str, Any] = declared_options(profile)
            components = [
                {
                    "kind": "control-plane",
                    "name": "control-plane",
                    "sdk": "java",
                    "mode": options["mode"],
                    "variant": key,
                    "optimization": "c1" if key == "jvm" else "3",
                    "image": {
                        "reference": (
                            f"127.0.0.1:5000/nanofaas/control-plane-{key}:"
                            f"{manifest.identity['tag']}"
                        ),
                        "id": "sha256:" + "b" * 64,
                        "digest": "sha256:" + "a" * 64,
                        "status": "published",
                    },
                }
            ]
            if options["native"]:
                components[0]["native"] = options["native"]
            components.extend(
                {
                    "kind": "function",
                    "name": function["name"],
                    "sdk": function["sdk"],
                    "mode": function["mode"] or "container",
                    "image": {
                        "reference": (
                            f"127.0.0.1:5000/nanofaas/{function['image']}:"
                            f"{manifest.identity['tag']}"
                        ),
                        "id": "sha256:" + "b" * 64,
                        "digest": "sha256:" + "a" * 64,
                        "status": "published",
                    },
                }
                for function in options["functions"]
            )
            output = root / "prepare" / key / "distribution"
            output.mkdir(parents=True, exist_ok=True)
            report = output / "distribution.json"
            report.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "recipe": {
                            "sha256": hashlib.sha256(profile.read_bytes()).hexdigest()
                        },
                        "tag": manifest.identity["tag"],
                        "source": {
                            "revision": manifest.identity["nanofaas"]["revision"],
                            "dirty": False,
                        },
                        "modules": options["modules"],
                        "components": components,
                    }
                )
            )
            evidence = output.parent / "registry.json"
            evidence.write_text("{}")
            if key not in manifest.publications:
                events.append(("publication", key))
                manifest.publications[key] = {
                    "profile": descriptor(profile),
                    "distribution": descriptor(report),
                    "evidence": {"registry": descriptor(evidence)},
                    "images": {
                        (
                            "control-plane"
                            if component["kind"] == "control-plane"
                            else f"{component['name']}/{component['sdk']}"
                        ): {
                            name: component["image"][name]
                            for name in ("reference", "digest", "id")
                        }
                        for component in components
                    },
                    "source": manifest.identity["nanofaas"],
                    "declaredOptions": options,
                    "gradleProperties": {"buildMemory": None, "parallelism": None},
                }
            distributions[key] = read_distribution(
                report,
                recipe=profile,
                tag=str(manifest.identity["tag"]),
                published=True,
            )
        write_comparison_manifest(root, manifest)
        from nanolab.tasks.recipe_remote import remote_recipe_root

        return PreparedComparison(
            remote_recipe_root(kwargs["request"], str(manifest.identity["tag"]))
            / "source",
            distributions,
        )

    monkeypatch.setattr(comparison, "_run_prepare", prepare)

    def cell(cell, **kwargs):
        kwargs["before_attempt"]()
        events.append(("cell", cell.variant.key, cell.repetition))
        assert kwargs["prepared"].remote_source.name == "source"
        folder = cell.run_dir(root)
        (folder / "metrics").mkdir(parents=True, exist_ok=True)
        (folder / "k6-summary.json").write_text("{}")
        (folder / "metrics/prometheus-snapshot.json").write_text("{}")

    monkeypatch.setattr(comparison, "_run_cell", cell)

    class Report:
        def __init__(self, **kwargs):
            pass

        def run(self):
            events.append("report")
            return root / "comparison-report.html"

    monkeypatch.setattr(comparison, "WriteComparisonReport", Report)
    app = typer.Typer()

    @app.callback()
    def cli():
        pass

    comparison.register(app)

    def invoke(*extra):
        return CliRunner().invoke(
            app,
            [
                "compare",
                str(scenario),
                "--environment",
                str(environment),
                "--variants",
                "jvm,native-o3",
                "--repetitions",
                "2",
                "--run-dir",
                str(root),
                *extra,
            ],
        )

    return invoke, events, root, scenario, environment, state


def test_new_comparison_provisions_once_and_interleaves_cells(command_case):
    invoke, events, root, *_ = command_case
    result = invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert events.count("provision") == 1
    assert [
        event for event in events if isinstance(event, tuple) and event[0] == "cell"
    ] == [
        ("cell", "jvm", 1),
        ("cell", "native-o3", 1),
        ("cell", "jvm", 2),
        ("cell", "native-o3", 2),
    ]
    assert (
        json.loads((root / "comparison-manifest.json").read_text())["schemaVersion"]
        == 1
    )
    assert events[-1] == "report"


def test_resume_skips_provisioning_publications_and_completed_cells(command_case):
    invoke, events, root, *_ = command_case
    assert invoke().exit_code == 0
    before = (root / "comparison-manifest.json").read_bytes()
    events.clear()
    result = invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert "provision" not in events
    assert not any(
        isinstance(event, tuple) and event[0] in {"publication", "cell"}
        for event in events
    )
    assert (root / "comparison-manifest.json").read_bytes() == before


@pytest.mark.parametrize("fresh", [False, True])
def test_replaced_cluster_cannot_resume_even_with_fresh(command_case, fresh):
    invoke, events, root, _, _, state = command_case
    assert invoke().exit_code == 0
    before = (root / "comparison-manifest.json").read_bytes()
    events.clear()
    state["cluster"] = "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"
    result = invoke(*(["--fresh"] if fresh else []))
    assert result.exit_code != 0
    assert events == []
    assert (root / "comparison-manifest.json").read_bytes() == before


@pytest.mark.parametrize("fresh", [False, True])
def test_changed_workload_cannot_rewrite_original_manifest(command_case, fresh):
    invoke, events, root, scenario, *_ = command_case
    assert invoke().exit_code == 0
    before = (root / "comparison-manifest.json").read_bytes()
    events.clear()
    scenario.write_text(scenario.read_text() + "loadScale: 2\n")
    assert invoke(*(["--fresh"] if fresh else [])).exit_code != 0
    assert events == []
    assert (root / "comparison-manifest.json").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "local",
        "backend",
        "functions",
        "image",
        "empty",
        "duplicate",
        "repetitions",
        "parallelism",
    ],
)
def test_invalid_comparison_is_rejected_before_side_effects(command_case, change):
    invoke, events, root, scenario, environment, _ = command_case
    extra = []
    if change == "local":
        environment.write_text("provider: local\n")
    elif change == "backend":
        scenario.write_text(
            scenario.read_text().replace("backend: k8s", "backend: container")
        )
    elif change == "functions":
        scenario.write_text(
            scenario.read_text().replace("word-stats-javascript", "warm-echo-java")
        )
    elif change == "image":
        scenario.write_text(scenario.read_text() + "controlPlaneImage: override:test\n")
    else:
        extra = {
            "empty": ["--variants", ""],
            "duplicate": ["--variants", "jvm,jvm"],
            "repetitions": ["--repetitions", "0"],
            "parallelism": ["--native-parallelism", "0"],
        }[change]
    result = invoke(*extra)
    assert result.exit_code != 0
    assert events == []
    assert not root.exists()


def test_cell_retry_preserves_prepared_artifacts_and_checks_each_attempt(
    tmp_path, monkeypatch
):
    from pathlib import PurePosixPath

    from nanolab.comparison.matrix import build_matrix
    from nanolab.config.environment import EnvironmentConfig
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.images.control_plane_variants import resolve_variants

    prepared = PreparedComparison(PurePosixPath("/owned/source"), {})
    checks = []
    attempts = []
    monkeypatch.setattr(product, "_prepare_run", lambda *args, **kwargs: (None,))

    def execute(**kwargs):
        assert kwargs["prepared_comparison"] is prepared
        assert kwargs["provision"] is False
        assert kwargs["keep"] is True
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise ConnectionError("transport lost")

    monkeypatch.setattr(product, "_execute_workflow", execute)
    comparison._run_cell(
        build_matrix(resolve_variants(("jvm",)), 1)[0],
        scenario=tmp_path / "scenario.yaml",
        scenario_config=ScenarioConfig(
            workflow="loadtest",
            backend="k8s",
            functions=["word-stats-java", "word-stats-javascript"],
        ),
        environment=tmp_path / "environment.yaml",
        environment_config=EnvironmentConfig(provider="multipass"),
        paths=object(),
        root=tmp_path,
        prepared=prepared,
        before_attempt=lambda: checks.append("check"),
    )
    assert len(attempts) == len(checks) == 2
