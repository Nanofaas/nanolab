"""Resource ownership and isolation survive partial provisioning failure."""

from dataclasses import dataclass, field
from typing import Any

import pytest
from sonata_engine import Task, TaskInputs, TaskOutcome, Workflow

from nanolab.config import EnvironmentConfig
from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.infrastructure import build_one_shot_resources

pytestmark = pytest.mark.usefixtures("simulated_one_shot_host")


def config() -> OneShotConfig:
    return OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"f": {"input": {}}},
        }
    )


@dataclass
class Result:
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Provider:
    acquired: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def vm_exists(self, request):
        return False

    def ensure_running(self, request):
        self.acquired.append(request.name)
        if len(self.acquired) == 3:
            raise RuntimeError("third VM failure")
        return Result()

    def connection_host(self, request):
        return "10.0.0." + str(len(self.acquired))

    def teardown(self, request):
        self.released.append(request.name)
        return Result()


class Observe(Task[None]):
    title = "Observe nodes"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome(value=None)


def test_third_acquire_failure_releases_already_acquired_in_reverse(
    tmp_path, monkeypatch
):
    provider = Provider()
    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.provider_for", lambda *args: provider
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    resources = build_one_shot_resources(
        config(), environment, run_id="test", repo_root=tmp_path
    )
    workflow = Workflow("partial-failure")
    workflow.add(Observe(), requires=resources.vms)
    with pytest.raises(Exception, match="third VM failure"):
        workflow.run()
    assert provider.released == list(reversed(provider.acquired))


def test_node_names_are_distinct_and_scoped_to_each_run(tmp_path):
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    first = build_one_shot_resources(
        config(), environment, run_id="first", repo_root=tmp_path
    )
    second = build_one_shot_resources(
        config(), environment, run_id="second", repo_root=tmp_path
    )
    a = {node.request.name for node in first.nodes.values()}
    b = {node.request.name for node in second.nodes.values()}
    assert len(a) == 3 and a.isdisjoint(b)
    assert first.generator_location == "operator-host"


def test_existing_vm_is_neither_adopted_nor_deleted(tmp_path, monkeypatch):
    provider = Provider()
    monkeypatch.setattr(provider, "vm_exists", lambda request: True)
    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.provider_for", lambda *args: provider
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    resources = build_one_shot_resources(
        config(), environment, run_id="test", repo_root=tmp_path
    )
    workflow = Workflow("existing-vm")
    workflow.add(Observe(), requires=resources.vms)
    with pytest.raises(RuntimeError, match="refusing to adopt"):
        workflow.run()
    assert provider.acquired == []
    assert provider.released == []


def test_runtime_plan_keeps_each_registration_on_its_own_vm(tmp_path, monkeypatch):
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.plans.one_shot_common import add_one_shot_platforms

    scenario = ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-calibration",
            "backend": "container",
            "functions": ["one-shot-workload-rust"],
            "oneShot": {
                "provider": "multipass",
                "purpose": "workflow-validation",
                "nodes": [
                    {"id": "edge-0", "kind": "edge"},
                    {"id": "edge-1", "kind": "edge"},
                    {"id": "cloud", "kind": "cloud"},
                ],
                "functions": {
                    "one-shot-workload-rust": {
                        "input": {
                            "iterations": 1000,
                            "working_set_bytes": 4096,
                            "seed": 7,
                        }
                    }
                },
            },
        }
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    (tmp_path / "functions/rust/one-shot-workload").mkdir(parents=True)
    workflow = Workflow("runtime-preview")
    topology = add_one_shot_platforms(
        workflow, scenario, environment, run_dir=tmp_path, repo_root=tmp_path
    )
    assert len(topology.platforms) == 3
    assert len({node.request.name for node in topology.resources.nodes.values()}) == 3
    assert all(len(platform.functions) == 1 for platform in topology.platforms.values())
    workflow.add(Observe(), requires=topology.requires)
    assert workflow.compile().tasks


def test_platform_builder_watches_owned_services_without_acquiring(
    tmp_path, monkeypatch
):
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.one_shot.supervision import CampaignSupervision
    from nanolab.plans.one_shot_common import add_one_shot_platforms

    settings = config().model_dump(by_alias=True)
    settings["functions"] = {"one-shot-workload-rust": {"input": {}}}
    settings["observability"] = {
        "logs": {
            "enabled": True,
            "backendOwned": True,
            "backendBind": "192.168.64.1",
            "endpoint": "http://192.168.64.1:3100/loki/api/v1/push",
        }
    }
    scenario = ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-calibration",
            "backend": "container",
            "functions": ["one-shot-workload-rust"],
            "oneShot": settings,
        }
    )
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {"stack": {"name": "one-shot"}},
        }
    )
    (tmp_path / "functions/rust/one-shot-workload").mkdir(parents=True)
    supervisor = CampaignSupervision(tmp_path / "run")
    assets = tmp_path / "deploy/observability"
    assets.mkdir(parents=True)
    for name in ("fluent-bit.conf", "parsers.conf"):
        (assets / name).write_text("# fixture\n")
    original = supervisor.service_resource
    wrapped = {}

    def watch(resource, name, probe, **kwargs):
        result = original(resource, name, probe, **kwargs)
        wrapped[name] = result
        return result

    monkeypatch.setattr(supervisor, "service_resource", watch)
    workflow = Workflow("watch-wiring")
    topology = add_one_shot_platforms(
        workflow,
        scenario,
        environment,
        run_dir=tmp_path,
        repo_root=tmp_path,
        supervision=supervisor,
    )
    assert set(wrapped) == {"logging-backend"} | {
        f"{node}:{role}"
        for node in ("edge-0", "edge-1", "cloud")
        for role in ("collector", "control-plane")
    }
    for node in topology.resources.nodes:
        assert topology.collectors[node] is wrapped[f"{node}:collector"]
        assert topology.endpoints[node] is wrapped[f"{node}:control-plane"]
        assert topology.collectors[node] in topology.endpoints[node].requires
        assert wrapped["logging-backend"] in topology.collectors[node].requires
    workflow.add(Observe(), requires=topology.requires)
    assert workflow.compile().tasks
    assert not supervisor.checks  # constructing/compiling never acquires services


def test_bootstrap_uses_acquired_vm_address(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sonata_engine import TaskInputs

    from nanolab.plans.one_shot_common import _bootstrap
    from nanolab.tasks.components.operations import RemoteCommandOperation
    from nanolab.tasks.vm.models import VmInfo

    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    resources = build_one_shot_resources(
        config(), environment, run_id="test", repo_root=tmp_path
    )
    node = resources.nodes["edge-0"]
    cloud_init_calls = []

    def cloud_init(argv, **_kwargs):
        cloud_init_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"status":"done"}', stderr="")

    monkeypatch.setattr("nanolab.plans.one_shot_common.subprocess.run", cloud_init)
    operation = RemoteCommandOperation(
        operation_id="vm.provision_base",
        summary="bootstrap",
        argv=("ansible-playbook", "base.yml", "-i", "<multipass-ip:fake>,"),
    )
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.plan_vm_provision_base",
        lambda context: (operation,),
    )
    observed = []
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.run_bootstrap_operations",
        lambda provider, operations, **kwargs: observed.extend(operations),
    )
    resource = _bootstrap(node, tmp_path)
    inputs = TaskInputs._for_resources(
        {
            node.vm: VmInfo(
                name=node.request.name or "missing",
                host="10.0.0.9",
                user="ubuntu",
                home="/home/ubuntu",
            )
        },
        {node.vm},
    )
    resource.acquire(inputs)
    assert cloud_init_calls[0][:3] == ("multipass", "exec", node.request.name)
    assert observed[0].argv[-1] == "10.0.0.9,"


@pytest.mark.parametrize("fault", [None, "inspect-timeout", "save-timeout"])
def test_image_archive_keeps_all_named_images_for_oci_import(
    tmp_path, monkeypatch, fault
):
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    from sonata_engine import TaskInputs

    from nanolab.config.scenario import ScenarioConfig
    from nanolab.plans.one_shot_common import add_one_shot_platforms

    scenario = ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-calibration",
            "backend": "container",
            "functions": ["one-shot-workload-rust"],
            "oneShot": {
                "provider": "multipass",
                "purpose": "workflow-validation",
                "nodes": [
                    {"id": "edge-0", "kind": "edge"},
                    {"id": "edge-1", "kind": "edge"},
                    {"id": "cloud", "kind": "cloud"},
                ],
                "functions": {"one-shot-workload-rust": {"input": {}}},
            },
        }
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    (tmp_path / "functions/rust/one-shot-workload").mkdir(parents=True)
    workflow = Workflow("archive")
    topology = add_one_shot_platforms(
        workflow, scenario, environment, run_dir=tmp_path, repo_root=tmp_path
    )
    workflow.add(Observe(), requires=topology.requires)
    archive = next(
        task.resource
        for task in workflow.compile().tasks
        if task.kind == "acquire"
        and task.resource is not None
        and task.resource.title.startswith("Archive digest")
    )
    report = tmp_path / "distribution.json"
    report.write_text("{}")
    images = [
        SimpleNamespace(id="sha256:" + "a" * 64, reference="cp:tag"),
        SimpleNamespace(id="sha256:" + "b" * 64, reference="fn:tag"),
    ]
    built = SimpleNamespace(
        report=report, components=[SimpleNamespace(image=image) for image in images]
    )
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        assert kwargs.get("timeout", 0) > 0
        if argv[:2] == ("docker", "save"):
            Path(argv[argv.index("--output") + 1]).write_bytes(b"saved-images")
            if fault == "save-timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        elif fault == "inspect-timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        reference = argv[-1]
        return SimpleNamespace(
            stdout=(images[0].id if reference == "cp:tag" else images[1].id).encode()
        )

    monkeypatch.setattr("nanolab.plans.one_shot_common.subprocess.run", execute)
    original = archive.requires[0]
    inputs = TaskInputs._for_resources({original: built}, {original})
    if fault:
        with pytest.raises(subprocess.TimeoutExpired):
            archive.acquire(inputs)
        assert not (tmp_path / "images.tar").exists()
        assert not (tmp_path / "images.tar.partial").exists()
        assert not (tmp_path / "runtime-distribution.json").exists()
        return
    assert archive.acquire(inputs).read_bytes() == b"saved-images"
    saved = next(argv for argv in calls if argv[:2] == ("docker", "save"))
    assert saved[-2:] == ("cp:tag", "fn:tag")


@pytest.mark.parametrize("failure", [None, "transfer", "image", "socket"])
@pytest.mark.parametrize("node_id", ["edge-0", "cloud"])
@pytest.mark.parametrize("terminal_queueing", [False, True])
@pytest.mark.parametrize("baseline", [False, True])
@pytest.mark.parametrize("scientific", [False, True])
@pytest.mark.parametrize("forecast_max_age_seconds", [None, 4110])
@pytest.mark.parametrize(
    "forecaster", [None, (10, {"history": "360", "ari.order": "6"})]
)
def test_control_plane_namespace_and_compensated_cleanup(
    tmp_path,
    monkeypatch,
    failure,
    node_id,
    terminal_queueing,
    baseline,
    scientific,
    forecast_max_age_seconds,
    forecaster,
):
    from types import SimpleNamespace

    import httpx
    from sonata_engine import Resource, TaskInputs

    from nanolab.plans.one_shot_common import _control_plane
    from nanolab.tasks.vm.models import VmInfo

    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    resources = build_one_shot_resources(
        config(), environment, run_id="socket", repo_root=tmp_path
    )
    node = resources.nodes[node_id]
    monkeypatch.setattr(
        node.provider,
        "transfer_to",
        lambda *args, **kwargs: Result(return_code=1 if failure == "transfer" else 0),
    )
    calls = []

    def command(self, argv, **kwargs):
        calls.append(argv)
        stdout = (
            ("bad" if failure == "socket" else "999")
            if argv[0] == "stat"
            else "sha256:" + ("b" if failure == "image" else "a") * 64
            if argv[:3] == ("docker", "image", "inspect")
            else ""
        )
        return Result(stdout=stdout)

    monkeypatch.setattr("nanolab.one_shot.infrastructure.NodeResource.command", command)
    bootstrap = Resource(
        title="bootstrap", acquire=lambda _: None, release=lambda *args: None
    )
    distribution: Resource[Any] = Resource(
        title="distribution", acquire=lambda _: None, release=lambda *args: None
    )
    archive: Resource[Any] = Resource(
        title="archive", acquire=lambda _: None, release=lambda *args: None
    )
    image = SimpleNamespace(id="sha256:" + "a" * 64, reference="cp:tag")
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.archive_image_ids",
        lambda *args: {"cp:tag": {image.id}},
    )
    built = SimpleNamespace(
        components=[SimpleNamespace(image=image)],
        control_plane=lambda: SimpleNamespace(image=image),
    )
    values: dict[Resource[Any], Any] = {
        other.vm: VmInfo(
            name=other.request.name or "missing",
            host="10.0.0.1",
            user="ubuntu",
            home="/home/ubuntu",
        )
        for other in resources.nodes.values()
    }
    values.update({distribution: built, archive: tmp_path / "images.tar"})
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    )
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.httpx.Client", lambda **kwargs: http
    )
    resource = _control_plane(
        node,
        resources,
        bootstrap=bootstrap,
        distribution=distribution,
        archive=archive,
        run_dir=tmp_path,
        terminal_queueing=terminal_queueing,
        collector=bootstrap if terminal_queueing else None,
        scientific=scientific,
        forecast_max_age_seconds=forecast_max_age_seconds,
        **(
            {
                "forecast_window_seconds": forecaster[0],
                "forecast_properties": forecaster[1],
            }
            if forecaster
            else {}
        ),
        baseline_depth=2 if baseline else None,
    )

    def release_command(_request, argv, **_kwargs):
        calls.append(argv)
        return Result(stdout="retained control-plane log")

    monkeypatch.setattr(node.provider, "exec_argv", release_command)
    inputs = TaskInputs._for_resources(values, set(values))
    if failure:
        with pytest.raises((RuntimeError, ValueError), match=r"transfer|image|socket"):
            resource.acquire(inputs)
    else:
        url = resource.acquire(inputs)
        launch = next(argv for argv in calls if argv[:2] == ("docker", "run"))
        if scientific:
            assert launch[launch.index("--cpus") + 1] == "0.35"
            assert launch[launch.index("--memory") + 1] == "512m"
        else:
            assert "--cpus" not in launch
        assert "--group-add" in launch
        assert launch[launch.index("--group-add") + 1] == "999"
        queued = terminal_queueing or (baseline and node_id != "cloud")
        assert f"--sync-queue.enabled={str(queued).lower()}" in launch
        assert "--nanofaas.admission.profile=SYNC_QUEUE" in launch
        assert "--nanofaas.metrics.profile=advanced" in launch
        queue_logger = (
            "--logging.level.it.unimib.datai.nanofaas.execution.QueueObservation=INFO"
        )
        assert (queue_logger in launch) == terminal_queueing
        ages = [
            argument
            for argument in launch
            if argument.startswith("--nanofaas.forecasting.max-age=")
        ]
        assert ages == (
            ["--nanofaas.forecasting.max-age=4110s"] if forecast_max_age_seconds else []
        )
        forecasting = [
            argument
            for argument in launch
            if argument.startswith(
                (
                    "--nanofaas.forecasting.window=",
                    "--nanofaas.forecasting.history=",
                    "--nanofaas.forecasting.ari.",
                )
            )
        ]
        assert forecasting == (
            [
                "--nanofaas.forecasting.window=10s",
                "--nanofaas.forecasting.history=360",
                "--nanofaas.forecasting.ari.order=6",
            ]
            if forecaster
            else ["--nanofaas.forecasting.window=1s"]
        )
        if baseline and node_id != "cloud":
            assert "--nanofaas.offload.target-url=http://10.0.0.1:8080" in launch
            assert "--sync-queue.max-depth=2" in launch
        resource.release(inputs, url)
    assert (
        tmp_path / f"{node.config.id}-control-plane.log"
    ).read_text() == "retained control-plane log"
    assert any(argv[:2] == ("docker", "rm") for argv in calls)


@pytest.mark.parametrize(
    ("capacity", "reason"), [((2, 100000), "CPU"), ((128, 1024), "memory")]
)
def test_topology_rejects_host_resource_overcommit(
    tmp_path, monkeypatch, capacity, reason
):
    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.host_capacity", lambda: capacity
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    with pytest.raises(ValueError, match=reason):
        build_one_shot_resources(
            config(), environment, run_id="capacity", repo_root=tmp_path
        )


def test_runtime_lookup_uses_function_label_scoped_to_owned_vm(tmp_path, monkeypatch):
    from nanolab.tasks.one_shot.preflight import runtime_endpoint
    from nanolab.tasks.vm.models import VmInfo

    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    resources = build_one_shot_resources(
        config(), environment, run_id="labels", repo_root=tmp_path
    )
    node = resources.nodes["edge-0"]
    observed = []

    def inspect(**kwargs):
        observed.append(kwargs["function"])
        return {"NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "18080"}]}}}

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.inspect_managed_container", inspect
    )
    inputs = TaskInputs._for_resources(
        {
            node.vm: VmInfo(
                name=node.request.name or "missing",
                host="10.0.0.9",
                user="ubuntu",
                home="/home/ubuntu",
            )
        },
        {node.vm},
    )
    endpoint, _ = runtime_endpoint(node, "f", inputs=inputs)
    assert endpoint == "http://10.0.0.9:18080"
    assert observed == [f"{node.request.name}/f"]


def test_node_transport_errors_and_executor_results_remain_distinct(
    tmp_path, monkeypatch
):
    from sonata_tasks.execution.models import CommandTaskSpec

    from nanolab.one_shot.infrastructure import NodeExecutor

    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    node = build_one_shot_resources(
        config(), environment, run_id="transport", repo_root=tmp_path
    ).nodes["edge-0"]
    monkeypatch.setattr(
        node.provider,
        "exec_argv",
        lambda *_args, **_kwargs: Result(return_code=1, stderr="VM transport failure"),
    )
    with pytest.raises(RuntimeError, match="edge-0: VM transport failure"):
        node.command(("docker", "ps"))
    executor = NodeExecutor(node)
    assert executor.binding_key("host") == "multipass:" + str(node.request.name)
    task = CommandTaskSpec(
        task_id="inspect", summary="Inspect runtime", argv=("docker", "ps"), role="host"
    )
    assert executor.run(task).status == "failed"


def test_bundled_comparison_recipe_contains_real_sync_queue_module():
    from importlib.resources import files

    import yaml

    recipe = yaml.safe_load(
        files("nanolab")
        .joinpath("assets/presets/recipes/one-shot-jvm.yaml")
        .read_text()
    )
    assert "sync-queue" in recipe["controlPlane"]["modules"]
