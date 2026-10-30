"""Comparison orchestration preserves immutable experiments across resumes."""

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from nanolab.application import experiment_02
from nanolab.cli import comparison, product
from nanolab.comparison.manifest import write_comparison_manifest
from nanolab.comparison.profiles import PreparedComparison, declared_options
from nanolab.tasks.recipes.workflow import read_distribution
from nanolab.workspace.paths import ToolPaths, discover_tool_root


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
    paths = ToolPaths.from_roots(nanofaas_checkout, discover_tool_root())
    monkeypatch.setattr(product, "default_tool_paths", lambda: paths)
    monkeypatch.setattr(comparison, "default_tool_paths", lambda: paths)
    monkeypatch.setattr(experiment_02, "default_tool_paths", lambda: paths)

    @contextmanager
    def provision(*args):
        events.append("provision")
        assert args[-1] is True
        assert (
            json.loads((root / "comparison-manifest.json").read_text())["target"]
            is None
        )
        yield

    monkeypatch.setattr(comparison, "provisioning_context", provision)
    monkeypatch.setattr(experiment_02, "provisioning_context", provision)

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
                stdout = json.dumps({"digest": "sha256:" + "a" * 64})
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
            image_name = yaml.safe_load(profile.read_text())["controlPlane"][
                "container"
            ]["image"]
            components = [
                {
                    "kind": "control-plane",
                    "name": "control-plane",
                    "sdk": "java",
                    "mode": options["mode"],
                    "variant": options["variant"],
                    "optimization": options["native"]["optimization"]
                    if options["native"]
                    else (
                        "c1"
                        if "-XX:TieredStopAtLevel=1" in options["jvm_args"]
                        else "c2"
                    ),
                    "image": {
                        "reference": (
                            f"127.0.0.1:5000/nanofaas/{image_name}:"
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
        from nanolab.tasks.recipes.remote import remote_recipe_root

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
    monkeypatch.setattr(comparison, "prepare_run", lambda *args, **kwargs: (None,))

    def execute(**kwargs):
        assert kwargs["prepared_comparison"] is prepared
        assert kwargs["provision"] is False
        assert kwargs["keep"] is True
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise ConnectionError("transport lost")

    monkeypatch.setattr(comparison, "execute_workflow", execute)
    comparison._run_cell(
        build_matrix(resolve_variants(("jvm",)), 1)[0],
        scenario=tmp_path / "scenario.yaml",
        scenario_config=ScenarioConfig(
            workflow="loadtest",
            backend="k8s",
            functions=["word-stats-java", "word-stats-javascript"],
        ),
        environment=tmp_path / "environment.yaml",
        environment_config=EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "original"}}}
        ),
        paths=object(),
        root=tmp_path,
        prepared=prepared,
        before_attempt=lambda: checks.append("check"),
    )
    assert len(attempts) == len(checks) == 2


def _experiment_case(command_case, *, azure: bool):
    from nanolab.workspace.paths import discover_tool_root

    invoke, events, root, scenario, environment, _ = command_case
    scenario.write_text(
        (discover_tool_root() / "scenarios" / "experiment-01-pilot.yaml").read_text()
    )
    if azure:
        environment.write_text(
            "provider: azure\nroles:\n"
            "  stack: {name: s, disk: 128G, user: azureuser}\n"
            "  loadgen: {name: l, disk: 30G, user: azureuser}\n"
            "azure:\n  resource_group: rg\n  location: westeurope\n"
            "  vm_size: Standard_D8s_v5\n  loadgen_vm_size: Standard_D2s_v5\n"
            "  image_urn: Canonical:ubuntu-24_04-lts:server:24.04.202607140\n"
            "  operator_source_cidr: 203.0.113.7/32\n"
            "  ssh_key_path: /tmp/id.pub\n"
        )
    return invoke, events, root


def test_experiment_01_requires_azure_before_side_effects(command_case):
    invoke, events, root = _experiment_case(command_case, azure=False)
    result = invoke("--variants", "experiment-01-core-k8s-jvm-jit")
    assert result.exit_code != 0
    assert "Azure" in result.output
    assert events == [] and not root.exists()


def test_experiment_01_refuses_non_campaign_variants(command_case):
    invoke, events, root = _experiment_case(command_case, azure=True)
    result = invoke("--variants", "jvm-loop1")
    assert result.exit_code != 0
    assert events == [] and not root.exists()


def _experiment_02_case(command_case, *, azure: bool, loadgen: bool = True):
    from nanolab.workspace.paths import discover_tool_root

    invoke, events, root, scenario, environment, _ = command_case
    scenario.write_text(
        (discover_tool_root() / "scenarios" / "experiment-02-pilot.yaml").read_text()
    )
    if azure:
        environment.write_text(
            "provider: azure\nroles:\n"
            "  stack: {name: s, disk: 128G, user: azureuser}\n"
            + ("  loadgen: {name: l, disk: 30G, user: azureuser}\n" if loadgen else "")
            + "azure:\n  resource_group: rg\n  location: westeurope\n"
            "  vm_size: Standard_D8s_v5\n  loadgen_vm_size: Standard_D2s_v5\n"
            "  image_urn: Canonical:ubuntu-24_04-lts:server:24.04.202607140\n"
            "  operator_source_cidr: 203.0.113.7/32\n"
            "  ssh_key_path: /tmp/id.pub\n"
        )
    return invoke, events, root


@pytest.mark.parametrize(("azure", "loadgen"), [(False, True), (True, False)])
def test_experiment_02_requires_azure_with_a_loadgen_before_side_effects(
    command_case, azure, loadgen
):
    invoke, events, root = _experiment_02_case(
        command_case, azure=azure, loadgen=loadgen
    )
    result = invoke("--variants", "nanofaas-p1-jvm")
    assert result.exit_code != 0
    assert "Azure" in result.output
    assert "loadgen" in result.output
    assert events == [] and not root.exists()


def test_experiment_02_refuses_unknown_configurations(command_case):
    invoke, events, root = _experiment_02_case(command_case, azure=True)
    result = invoke("--variants", "nanofaas-p2-jvm")
    assert result.exit_code != 0
    assert "unknown" in result.output
    assert events == [] and not root.exists()


def test_experiment_02_refuses_a_kubernetes_scenario(command_case):
    from nanolab.workspace.paths import discover_tool_root

    invoke, events, root, scenario, _environment, _ = command_case
    pilot = discover_tool_root() / "scenarios" / "experiment-02-pilot.yaml"
    scenario.write_text(pilot.read_text().replace("backend: container", "backend: k8s"))
    result = invoke("--variants", "nanofaas-p1-jvm")
    assert result.exit_code != 0
    assert events == [] and not root.exists()


def test_compare_direct_and_p1_with_two_budgets_can_resume(command_case, monkeypatch):
    """The whole experiment-02 path with fake VMs: four cells, then a resume."""
    from nanolab.comparison.cells import CELL_RESULT
    from nanolab.comparison.experiment_02 import (
        EVENTS,
        SAMPLES,
        SCHEDULER,
        SUMMARY,
    )

    invoke, events, root = _experiment_02_case(command_case, azure=True)
    scenario = command_case[3]
    scenario.write_text(
        scenario.read_text().replace(
            "  budgets:\n    - {cpus: 2, memoryMiB: 2048}\n",
            "  budgets:\n    - {cpus: 2, memoryMiB: 2048}\n"
            "    - {cpus: 1, memoryMiB: 1024}\n",
        )
    )
    assert "cpus: 1, memoryMiB: 1024" in scenario.read_text()

    class Provider:
        def exec_argv(self, request, argv, **kwargs):
            if argv[:2] == ("sudo", "sh"):
                events.append("slices")
            elif argv[:2] == ("rm", "-rf"):
                events.append(("cleanup", argv[-1]))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        def connection_host(self, request):
            return "20.0.0.4"

    monkeypatch.setattr(
        experiment_02, "provider_for_environment", lambda *a: Provider()
    )
    machine = {
        "machineId": "a" * 32,
        "productUuid": "11111111-2222-3333-4444-555555555555",
    }
    target = {
        "machines": {"stack": machine, "loadgen": machine},
        "engine": {"dockerId": "docker-1"},
    }
    monkeypatch.setattr(
        experiment_02, "read_comparison_target", lambda *a, **k: dict(target)
    )
    monkeypatch.setattr(experiment_02, "read_host_facts", lambda *a: {})
    monkeypatch.setattr(
        experiment_02,
        "require_recorded_publications",
        lambda **kwargs: events.append("publications-checked"),
    )

    def image(reference):
        return SimpleNamespace(image=SimpleNamespace(reference=reference))

    def prepare(**kwargs):
        events.append(("prepare", tuple(kwargs["profiles"])))
        distribution = SimpleNamespace(
            control_plane=lambda: image("registry/control-plane:p1"),
            function=lambda *names: image("registry/rust-qr-code:e02"),
        )
        from nanolab.tasks.recipes.remote import remote_recipe_root

        owned = remote_recipe_root(
            kwargs["request"], str(kwargs["manifest"].identity["tag"])
        )
        return SimpleNamespace(
            distributions={"nanofaas-p1-jvm": distribution},
            remote_source=owned / "source",
        )

    monkeypatch.setattr(comparison, "_run_prepare", prepare)

    def cell(cell, **kwargs):
        events.append(("cell", cell.key, cell.budget.label))
        run_dir = kwargs["run_dir"]
        (run_dir / "metrics").mkdir(parents=True, exist_ok=True)
        for name in (SUMMARY, EVENTS, SAMPLES, SCHEDULER):
            (run_dir / name).write_text("{}\n")
        (run_dir / EVENTS).write_text('{"t":"origin","ts":1000000}\n')
        return "measured"

    monkeypatch.setattr(experiment_02, "run_cell", cell)
    monkeypatch.setattr(
        experiment_02,
        "write_experiment_02_report",
        lambda root, cells, protocol: events.append("report") or root / "r.csv",
    )

    result = invoke(
        "--variants", "nanofaas-p1-jvm,direct-reference", "--repetitions", "1"
    )
    assert result.exit_code == 0, (result.output, result.exception)
    cells = sorted(event[1:] for event in events if event[0] == "cell")
    assert cells == [
        ("direct-reference", "cpu1-mem1024"),
        ("direct-reference", "cpu2-mem2048"),
        ("nanofaas-p1-jvm", "cpu1-mem1024"),
        ("nanofaas-p1-jvm", "cpu2-mem2048"),
    ]
    assert events.count("provision") == events.count("slices") == 1
    # Only the configuration with a recipe is prepared.
    assert ("prepare", ("nanofaas-p1-jvm",)) in events
    receipts = sorted(path.parent.relative_to(root).as_posix()
                      for path in root.rglob(CELL_RESULT))  # fmt: skip
    assert len(receipts) == 4

    events.clear()
    result = invoke(
        "--variants", "nanofaas-p1-jvm,direct-reference", "--repetitions", "1"
    )
    assert result.exit_code == 0, (result.output, result.exception)
    assert "resuming: 4 of 4" in result.output
    assert not [event for event in events if event[0] == "cell"]
    assert "provision" not in events and "slices" not in events
    assert "publications-checked" in events


def _workloads_repository(tmp_path):
    import subprocess

    root = tmp_path / "paper"
    (root / "functions" / "qr-code").mkdir(parents=True)
    (root / "functions" / "qr-code" / "Cargo.toml").write_text("[workspace]\n")
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("add", "."),
        ("commit", "-qm", "w"),
    ):
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    return root / "functions"


def test_compare_runs_a_baseline_cell_end_to_end(command_case, monkeypatch, tmp_path):
    from nanolab.tasks.experiment_02 import baselines
    from nanolab.tasks.experiment_02.baselines import Spin

    invoke, events, _root = _experiment_02_case(command_case, azure=True)

    class Provider:
        def exec_argv(self, request, argv, **kwargs):
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        def connection_host(self, request):
            return "20.0.0.4"

    machine = {
        "machineId": "a" * 32,
        "productUuid": "11111111-2222-3333-4444-555555555555",
    }
    monkeypatch.setattr(
        experiment_02, "provider_for_environment", lambda *a: Provider()
    )
    monkeypatch.setattr(
        experiment_02,
        "read_comparison_target",
        lambda *a, **k: {"machines": {"stack": machine}, "engine": {"dockerId": "d"}},
    )
    monkeypatch.setattr(experiment_02, "read_host_facts", lambda *a: {})
    monkeypatch.setattr(
        experiment_02, "require_recorded_publications", lambda **k: None
    )
    image = SimpleNamespace(image=SimpleNamespace(reference="registry/qr:e02"))

    def prepare(**kwargs):
        from nanolab.tasks.recipes.remote import remote_recipe_root

        owned = remote_recipe_root(
            kwargs["request"], str(kwargs["manifest"].identity["tag"])
        )
        distribution = SimpleNamespace(
            function=lambda *a: image, control_plane=lambda: image
        )
        return SimpleNamespace(
            distributions={"nanofaas-p1-jvm": distribution},
            remote_source=owned / "source",
        )

    monkeypatch.setattr(comparison, "_run_prepare", prepare)
    monkeypatch.setattr(
        experiment_02,
        "ensure_workloads",
        lambda stack, **kwargs: (
            events.append(("workloads", kwargs["record"].name)) or {"qr-spin": "a" * 64}
        ),
    )
    observed = {"version": "spin 4.2.5"}
    identity = {"spin": "id"}
    monkeypatch.setattr(
        baselines, "baseline_identity", lambda key, **kwargs: identity[key]
    )
    monkeypatch.setattr(
        baselines,
        "install_baselines",
        lambda stack, keys, **kwargs: (
            events.append(("install", tuple(keys)))
            or {
                key: {
                    "identity": identity[key],
                    "root": f"/opt/nanolab-e02/{key}/id",
                    "image": "",
                    **observed,
                }
                for key in keys
            }
        ),
    )

    def cell(cell, **kwargs):
        from nanolab.comparison.experiment_02 import EVENTS, SAMPLES, SCHEDULER, SUMMARY

        platform = kwargs["platform"]
        events.append(("cell", cell.key, type(platform).__name__))
        if cell.key == "spin":  # the cell runs what this run installed
            assert str(platform.root) == "/opt/nanolab-e02/spin/id"
        (kwargs["run_dir"] / "metrics").mkdir(parents=True, exist_ok=True)
        for name in (SUMMARY, EVENTS, SAMPLES, SCHEDULER):
            (kwargs["run_dir"] / name).write_text("{}\n")
        (kwargs["run_dir"] / EVENTS).write_text('{"t":"origin","ts":1000000}\n')
        return "measured"

    monkeypatch.setattr(experiment_02, "run_cell", cell)
    monkeypatch.setattr(
        experiment_02,
        "write_experiment_02_report",
        lambda root, cells, p: root / "r.csv",
    )
    arguments = ("--variants", "spin,direct-reference", "--repetitions", "1")
    result = invoke(*arguments)
    assert result.exit_code != 0 and "NANOLAB_WORKLOADS_ROOT" in result.output
    monkeypatch.setenv("NANOLAB_WORKLOADS_ROOT", str(_workloads_repository(tmp_path)))
    result = invoke(*arguments)
    assert result.exit_code == 0, (result.output, result.exception)
    assert ("install", ("spin",)) in events and (
        "workloads",
        "workloads.json",
    ) in events
    assert ("cell", "spin", Spin.__name__) in events
    events.clear()
    result = invoke(*arguments)
    assert result.exit_code == 0, (result.output, result.exception)
    assert not [event for event in events if event[0] == "cell"]
    # The resume checks the recorded artifacts again rather than trusting them.
    assert ("workloads", "workloads.json") in events
    # An installation that no longer matches what was recorded stops the run.
    observed["version"] = "spin 4.3.0"
    result = invoke(*arguments, "--fresh")
    assert result.exit_code != 0 and "changed" in str(result.exception)
    # Inputs that make another identity stop it before anything is installed.
    observed["version"] = "spin 4.2.5"
    identity["spin"] = "other"
    events.clear()
    result = invoke(*arguments, "--fresh")
    assert result.exit_code != 0 and "changed" in str(result.exception)
    assert not [event for event in events if event[0] == "install"]


def test_runtime_gc_subset_prepares_qr_publisher_and_resumes(command_case, monkeypatch):
    from datetime import UTC, datetime

    from nanolab.comparison.cells import attempt_dir, write_cell_result

    invoke, events, root = _experiment_case(command_case, azure=True)
    monkeypatch.setattr(comparison, "read_host_facts", lambda *args: {})

    def measured(cell, *, root, identity, before_attempt, **kwargs):
        before_attempt()
        events.append(("runtime-cell", cell.key, cell.repetition))
        folder = attempt_dir(cell.run_dir(root), 1)
        (folder / "metrics").mkdir(parents=True)
        (folder / "k6-summary.json").write_text('{"metrics":{}}')
        (folder / "k6-events.jsonl").write_text('{"t":"origin","ts":1000000}\n')
        (folder / "metrics/prometheus-snapshot.json").write_text(
            '{"start":"1970-01-01T00:16:40+00:00","queries":{}}'
        )
        write_cell_result(
            cell,
            root,
            identity=identity,
            attempt=1,
            outcome="measured",
            reason="fixture",
            started_at=datetime.now(UTC),
        )
        return "measured"

    monkeypatch.setattr(comparison, "_run_experiment_cell", measured)
    result = invoke("--variants", "native-o3")
    assert result.exit_code == 0, (result.output, result.exception)
    manifest = json.loads((root / "comparison-manifest.json").read_text())
    assert set(manifest["identity"]["profiles"]) == {"jvm", "native-o3"}
    functions = manifest["identity"]["profiles"]["jvm"]["options"]["functions"]
    assert [(f["name"], f["sdk"]) for f in functions] == [("qr-code", "rust")]
    assert [("runtime-cell", "native-o3", n) for n in (1, 2)] == [
        event
        for event in events
        if isinstance(event, tuple) and event[0] == "runtime-cell"
    ]
    result = invoke("--variants", "native-o3")
    assert result.exit_code == 0, (result.output, result.exception)
    assert "2 of 2 cells" in result.output


def test_august_interpreter_reuses_publisher_and_resumes(command_case):
    invoke, events, root, *_ = command_case
    result = invoke("--variants", "jvm-int", "--repetitions", "1")
    assert result.exit_code == 0, (result.output, result.exception)
    manifest = json.loads((root / "comparison-manifest.json").read_text())
    profiles = manifest["identity"]["profiles"]
    assert set(profiles) == {"jvm", "jvm-int"}
    assert profiles["jvm-int"]["options"]["jvm_args"] == [
        "-XX:+UseSerialGC",
        "-Xint",
    ]
    assert profiles["jvm-int"]["options"]["functions"] == []
    assert [f["sdk"] for f in profiles["jvm"]["options"]["functions"]] == [
        "java",
        "javascript",
    ]
    events.clear()
    result = invoke("--variants", "jvm-int", "--repetitions", "1")
    assert result.exit_code == 0, (result.output, result.exception)
    assert "provision" not in events
    assert not any(
        isinstance(event, tuple) and event[0] in {"publication", "cell"}
        for event in events
    )
