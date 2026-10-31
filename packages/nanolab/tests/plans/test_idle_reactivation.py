import importlib

import pytest
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from tests.config.test_idle_reactivation import idle_scenario

from .test_loadtest import NoopPrometheus, RecordingExecutor


@pytest.mark.nanofaas
def test_pair_builds_once_and_releases_before_next_arm(tmp_path, nanofaas_root):
    module = importlib.import_module("nanolab.plans.idle_reactivation")
    executor = RecordingExecutor()
    config = ScenarioConfig.model_validate(idle_scenario())
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "stack.example"},
                "loadgen": {"host": "load.example"},
            },
        }
    )
    workflow = module.build_idle_reactivation_plan(
        config,
        environment,
        RoleBindings({"host": executor, "stack": executor, "loadgen": executor}),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path / "b6",
        repo_root=nanofaas_root,
        fetcher=None,
    )
    compiled = workflow.compile()
    titles = [task.task.title for task in compiled.tasks]
    assert titles[0] == "Freeze B6 protocol and workload inputs"
    assert sum("Build control plane" in title for title in titles) == 1
    first = next(
        i
        for i, title in enumerate(titles)
        if title == "Measure idle reactivation: keep-ready"
    )
    second = next(
        i
        for i, title in enumerate(titles)
        if title == "Measure idle reactivation: scale-to-zero"
    )
    assert any(
        "release" in title.lower() or "Release" in title
        for title in titles[first + 1 : second]
    )
    assert not any("k6" in title.lower() for title in titles)
    assert titles[-1] == "Verify paired idle-reactivation evidence"


def test_b6_requires_a_dedicated_generator_before_provisioning(tmp_path):
    module = importlib.import_module("nanolab.plans.idle_reactivation")
    config = ScenarioConfig.model_validate(idle_scenario())
    environment = EnvironmentConfig.model_validate(
        {"provider": "external", "roles": {"stack": {"host": "stack"}}}
    )
    with pytest.raises(ValueError, match="loadgen"):
        module.require_idle_environment(environment)


@pytest.mark.nanofaas
@pytest.mark.parametrize("fail_measurement", [False, True])
def test_pair_registers_fixed_budgets_and_reuses_images_with_cleanup(
    tmp_path, nanofaas_root, monkeypatch, fail_measurement
):
    import json

    from sonata_engine import TaskOutcome

    module = importlib.import_module("nanolab.plans.idle_reactivation")
    executor = RecordingExecutor()
    config = ScenarioConfig.model_validate(idle_scenario())
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {"stack": {"host": "stack"}, "loadgen": {"host": "loadgen"}},
        }
    )
    if fail_measurement:
        from nanolab.tasks.loadtest.tasks import CapturePrometheusSnapshot

        def fail(self, watcher, report):
            raise RuntimeError("loadgen lost")

        monkeypatch.setattr(module.RunIdleReactivation, "_measure", fail)
        monkeypatch.setattr(CapturePrometheusSnapshot, "_WINDOW_MARGIN_S", 0)
    else:
        monkeypatch.setattr(
            module.RunIdleReactivation,
            "run",
            lambda self, inputs: TaskOutcome(value={}),
        )
        monkeypatch.setattr(
            module.VerifyIdlePair, "run", lambda self, inputs: TaskOutcome(value={})
        )
    workflow = module.build_idle_reactivation_plan(
        config,
        environment,
        RoleBindings({"host": executor, "stack": executor, "loadgen": executor}),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path / "b6",
        repo_root=nanofaas_root,
        fetcher=None,
    )
    if fail_measurement:
        with pytest.raises(RuntimeError, match="paired evidence"):
            workflow.run()
        report = json.loads((tmp_path / "b6/paired-summary.json").read_text())
        assert report["passed"] is False
        assert all(not arm["complete"] for arm in report["arms"].values())
        for arm in ("keep-ready", "scale-to-zero"):
            assert (tmp_path / "b6" / arm / "control-plane.log").is_file()
        log_positions = [
            i
            for i, task in enumerate(executor.seen)
            if "Collect" in task.summary and "control-plane log" in task.summary
        ]
        releases = [
            i
            for i, task in enumerate(executor.seen)
            if task.argv[:2] == ("helm", "uninstall")
        ]
        assert len(log_positions) == len(releases) == 2
        assert log_positions[0] < releases[0] < log_positions[1] < releases[1]
    else:
        workflow.run()
    registrations = [
        json.loads(t.argv[t.argv.index("--data") + 1])
        for t in executor.seen
        if t.summary.startswith("Register ")
    ]
    assert [m["scalingConfig"]["minReplicas"] for m in registrations] == [1, 0]
    for manifest in registrations:
        assert manifest["scalingConfig"]["maxReplicas"] == 1
        assert manifest["concurrency"] == 2
        assert manifest["queueSize"] == 20
        assert manifest["timeoutMs"] == 30000
        assert manifest["resources"]["limits"] == {"cpu": 2, "memoryMiB": 512}
    assert registrations[0]["image"] == registrations[1]["image"]
    helm = [t.argv for t in executor.seen if t.argv[0] == "helm"]
    actions = [argv[1] for argv in helm]
    assert actions == ["upgrade", "uninstall", "upgrade", "uninstall"]
    assert any("NANOFAAS_K8S_IMAGEPULLPOLICY" in arg for arg in helm[0])


def test_b6_rejects_a_generator_bound_to_the_stack_machine():
    from nanolab.plans.idle_reactivation import require_idle_environment

    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "same"},
                "loadgen": {"host": "same"},
            },
        }
    )
    with pytest.raises(ValueError, match="distinct"):
        require_idle_environment(environment)


@pytest.mark.nanofaas
def test_azure_probe_uses_the_same_resolved_home_as_provisioning(
    tmp_path, nanofaas_root
):
    from nanolab.plans.idle_reactivation import build_idle_reactivation_plan
    from nanolab.tasks.idle_reactivation_runtime import RunIdleReactivation

    environment = EnvironmentConfig.model_validate(
        {
            "provider": "azure",
            "roles": {"stack": {"name": "stack"}, "loadgen": {"name": "loadgen"}},
            "azure": {"resource_group": "test", "location": "westeurope"},
        }
    )
    executor = RecordingExecutor()
    workflow = build_idle_reactivation_plan(
        ScenarioConfig.model_validate(idle_scenario()),
        environment,
        RoleBindings({"host": executor, "stack": executor, "loadgen": executor}),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path / "b6",
        repo_root=nanofaas_root,
    )
    tasks = [
        t.task
        for t in workflow.compile().tasks
        if isinstance(t.task, RunIdleReactivation)
    ]
    assert all(str(t.remote_home) == "/home/azureuser" for t in tasks)


@pytest.mark.nanofaas
def test_platform_commands_translate_controller_checkout_through_real_vm_bindings(
    tmp_path, nanofaas_root, monkeypatch
):
    from sonata_engine import TaskOutcome

    from nanolab.application.execution import build_role_bindings
    from nanolab.plans.idle_reactivation import build_idle_reactivation_plan
    from nanolab.tasks.idle_reactivation_runtime import (
        RunIdleReactivation,
        VerifyIdlePair,
    )
    from tests.cli.test_execution_bindings import RecordingVmProvider

    class Provider(RecordingVmProvider):
        def exec_argv(self, request, argv, **kwargs):
            result = super().exec_argv(request, argv, **kwargs)
            result.stdout = (
                "10.43.0.7" if "get service control-plane" in " ".join(argv) else ""
            )
            return result

    provider = Provider()

    environment = EnvironmentConfig.model_validate(
        {
            "provider": "azure",
            "roles": {"stack": {"name": "stack"}, "loadgen": {"name": "loadgen"}},
            "azure": {"resource_group": "test", "location": "westeurope"},
        }
    )
    bindings, _ = build_role_bindings(
        environment, vm_provider=provider, repo_root=nanofaas_root
    )
    monkeypatch.setattr(
        RunIdleReactivation, "run", lambda self, inputs: TaskOutcome(value={})
    )
    monkeypatch.setattr(
        VerifyIdlePair, "run", lambda self, inputs: TaskOutcome(value={})
    )
    build_idle_reactivation_plan(
        ScenarioConfig.model_validate(idle_scenario()),
        environment,
        bindings,
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path / "b6",
        repo_root=nanofaas_root,
    ).run()
    calls = [(argv, directory) for _, argv, _, directory, _ in provider.exec_calls]
    assert calls
    assert all(directory == "/home/azureuser/nanofaas" for _, directory in calls)
    assert any(argv[0] == "./gradlew" for argv, _ in calls)
    assert [argv[1] for argv, _ in calls if argv[0] == "helm"] == [
        "upgrade",
        "uninstall",
        "upgrade",
        "uninstall",
    ]
