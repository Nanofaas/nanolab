"""B5 compares function runtimes, keeping scheduling and workload fixed."""

from pathlib import Path

import pytest
import yaml

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.loadtest import _default_stages, _resolve_functions, load_script_name

RUNTIMES = ("java", "go", "javascript", "python", "rust")
PRESETS = Path(__file__).parents[2] / "src/nanolab/assets/presets/scenarios"


def test_b5_presets_differ_only_by_function_runtime():
    common = None
    for runtime in RUNTIMES:
        value = yaml.safe_load(
            (PRESETS / f"function-runtime-{runtime}.yaml").read_text()
        )
        config = ScenarioConfig.model_validate(value)
        assert not config.concurrency_control and not config.autoscaling
        assert load_script_name(config) == "function-runtime.js"
        assert _default_stages(config) == ()
        assert config.payload_profile == "medium" and config.backend == "k8s"
        key = f"word-stats-{runtime}"
        assert config.functions == [key]
        value["functions"] = ["FUNCTION"]
        value["resources"]["FUNCTION"] = value["resources"].pop(key)
        assert common is None or common == value
        common = value


def test_b5_registration_has_fixed_identical_limits(nanofaas_root):
    for runtime in RUNTIMES:
        config = ScenarioConfig.model_validate(
            yaml.safe_load((PRESETS / f"function-runtime-{runtime}.yaml").read_text())
        )
        functions, _ = _resolve_functions(config, nanofaas_root, None, None, None)
        (fn,) = functions
        assert (fn.concurrency, fn.queue_size, fn.timeout_ms, fn.max_retries) == (
            2,
            20,
            30000,
            3,
        )
        assert fn.resources["limits"] == {"cpu": 2.0, "memoryMiB": 512}


@pytest.mark.parametrize(
    "override",
    [
        {"concurrencyControl": True},
        {"autoscaling": True},
        {"functions": ["word-stats-java", "word-stats-rust"]},
        {"payloadProfile": "small"},
        {"controlPlaneRuntime": "native"},
        {"backend": "container"},
    ],
)
def test_b5_rejects_confounded_or_incomplete_config(override):
    value = yaml.safe_load((PRESETS / "function-runtime-java.yaml").read_text())
    value.update(override)
    with pytest.raises(ValueError, match=r"function-runtime|concurrencyControl"):
        ScenarioConfig.model_validate(value)


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_b5_compiles_the_real_workflow_without_external_actions(
    runtime, nanofaas_checkout, tmp_path, monkeypatch
):
    from sonata_tasks.execution.bindings import RoleBindings

    from nanolab.config.environment import EnvironmentConfig
    from nanolab.metrics.catalogue import queries_for
    from nanolab.plans import loadtest

    from .test_loadtest import NoopPrometheus, RecordingExecutor

    requests = []
    loads = []
    platform_builder = loadtest._build_platform_request
    load_builder = loadtest.K6Task

    def platform(**kwargs):
        request = platform_builder(**kwargs)
        requests.append(request)
        return request

    def load(config, **kwargs):
        loads.append(config)
        return load_builder(config, **kwargs)

    monkeypatch.setattr(loadtest, "_build_platform_request", platform)
    monkeypatch.setattr(loadtest, "K6Task", load)
    config = ScenarioConfig.model_validate(
        yaml.safe_load((PRESETS / f"function-runtime-{runtime}.yaml").read_text())
    )
    executor = RecordingExecutor()
    workflow = loadtest.build_loadtest_plan(
        config,
        EnvironmentConfig.model_validate(
            {
                "provider": "external",
                "roles": {
                    "stack": {"host": "stack.example"},
                    "loadgen": {"host": "load.example"},
                },
            }
        ),
        RoleBindings({"host": executor, "stack": executor, "loadgen": executor}),
        control_plane_url="http://stack.example:30080",
        prometheus_client=NoopPrometheus(),
        repo_root=nanofaas_checkout,
        remote_repo_root=Path("/data/source"),
        remote_run_dir=Path("/home/ubuntu/nanofaas-release/b5/benchmarks/run-1"),
        run_dir=tmp_path,
    )
    assert workflow.compile().tasks
    assert not executor.seen
    (request,) = requests
    assert request.additional_modules == ("async-queue", "build-metadata")
    assert len(request.functions) == 1
    assert request.functions[0].resources["limits"] == {"cpu": 2.0, "memoryMiB": 512}
    args = " ".join(request.helm_values)
    assert "containerMetrics.enabled=true" in args
    assert "controlPlane.resources.limits.cpu=2" in args
    (task,) = loads
    assert not task.stages
    assert task.script_path.name == "function-runtime.js"
    assert task.payload_path == Path(
        "/home/ubuntu/nanofaas/functions/test-data/word-stats/performance-medium.json"
    )
    assert (
        task.env["NANOFAAS_CONTRACT"]
        == "/home/ubuntu/nanofaas/functions/test-data/word-stats/runtime-contract.json"
    )
    assert task.env["NANOFAAS_FUNCTION"] == f"word-stats-{runtime}"
    assert task.process_env["K6_CONSOLE_OUTPUT"].startswith(
        "/home/ubuntu/nanofaas-release/b5/benchmarks/run-1/"
    )
    queries = {
        q.name: q
        for q in queries_for(
            f"word-stats-{runtime}",
            modules=request.additional_modules,
            container_functions=(f"word-stats-{runtime}",),
            function_runtime=True,
        )
    }
    for name in (
        "container_memory_bytes",
        "container_rss_bytes",
        "container_cpu_seconds",
        "container_cpu_periods",
    ):
        assert queries[f"{name}@word-stats-{runtime}"].required
    assert (
        "container_memory_rss{"
        in queries[f"container_rss_bytes@word-stats-{runtime}"].expr
    )
