"""Exercise the B4 builder and its evidence failure boundary."""

import re
import shutil
import subprocess

import pytest
import yaml

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.offload_loadtest import build_offload_loadtest_plan
from tests.config.test_offload_experiment import scenario
from tests.plans.test_offload_loadtest import (
    RecordingExecutor,
    _bindings,
    _UnusedFetcher,
)


def test_experiment_rejects_local_or_shared_generator(tmp_path):
    config = ScenarioConfig.model_validate(scenario())
    with pytest.raises(ValueError, match=r"distinct|remote"):
        build_offload_loadtest_plan(
            config,
            EnvironmentConfig(provider="local"),
            _bindings(RecordingExecutor()),
            run_dir=tmp_path,
        )


def test_experiment_rejects_previous_artifacts(tmp_path):
    (tmp_path / "protocol.json").write_text("{}")
    env = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "edge"},
                "cloud": {"host": "cloud"},
                "loadgen": {"host": "loadgen"},
            },
        }
    )
    with pytest.raises(ValueError, match="fresh"):
        build_offload_loadtest_plan(
            ScenarioConfig.model_validate(scenario()),
            env,
            _bindings(RecordingExecutor()),
            run_dir=tmp_path,
            fetcher=_UnusedFetcher(),
        )


@pytest.mark.parametrize("mode", ["disabled", "pressure", "cloud"])
def test_matched_arms_reuse_workflow_with_explicit_resources(
    tmp_path, monkeypatch, mode
):
    import nanolab.plans.offload_experiment as module
    from tests.conftest import source_contract_root

    captured = {}

    def build(request, bindings, **kwargs):
        captured.update(request=request, **kwargs)
        return "workflow"

    monkeypatch.setattr(module, "build_offload_loadtest_workflow", build)
    spec = scenario()
    spec["offloadExperiment"]["mode"] = mode
    env = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "edge"},
                "cloud": {"host": "cloud"},
                "loadgen": {"host": "loadgen"},
            },
        }
    )
    result = build_offload_loadtest_plan(
        ScenarioConfig.model_validate(spec),
        env,
        _bindings(RecordingExecutor()),
        run_dir=tmp_path,
        repo_root=source_contract_root(),
        fetcher=_UnusedFetcher(),
    )
    assert result == "workflow"
    request = captured["request"]
    edge, cloud = request.edge.functions[0], request.cloud.functions[0]
    assert edge.name == cloud.name == "word-stats-java"
    assert edge.max_retries == cloud.max_retries == 0
    assert edge.timeout_ms == cloud.timeout_ms == 30000
    assert edge.concurrency == 2 and cloud.concurrency == 20
    assert edge.queue_size == 20 and cloud.queue_size == 100
    assert edge.offload["enabled"] is (mode == "pressure")
    assert cloud.offload["enabled"] is False
    for platform in (request.edge, request.cloud):
        values = " ".join(platform.helm_values)
        assert "SYNC_QUEUE" in values
        assert "resources.limits.cpu=2" in values
        assert "resources.limits.memory=2048Mi" in values
        assert platform.additional_modules == ("offload", "sync-queue")

        indices = {
            int(match.group(1))
            for argument in platform.helm_values
            if (match := re.match(r"controlPlane\.extraEnv\[(\d+)\]\.name=", argument))
        }
        assert indices == set(range(len(indices))), (
            "Helm extraEnv must not contain null gaps"
        )
        if helm := shutil.which("helm"):
            rendered = subprocess.run(
                [
                    helm,
                    "template",
                    "nanofaas",
                    str(source_contract_root() / "deploy/helm/nanofaas"),
                    *platform.helm_values,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert rendered.returncode == 0, rendered.stderr
            deployment = next(
                document
                for document in yaml.safe_load_all(rendered.stdout)
                if document
                and document.get("kind") == "Deployment"
                and document["metadata"]["name"] == "nanofaas-control-plane"
            )
            environment = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
            assert all(
                isinstance(item, dict) and item.get("name") for item in environment
            )
            assert len({item["name"] for item in environment}) == len(environment)
            actual = {item["name"]: item.get("value") for item in environment}
            assert actual["NANOFAAS_ADMISSION_PROFILE"] == "SYNC_QUEUE"
            assert actual["SYNC_QUEUE_MAX_DEPTH"] == (
                "20" if platform is request.edge else "100"
            )
            if platform is request.edge:
                assert actual["NANOFAAS_OFFLOAD_TARGETURL"] == "http://cloud:30080"
            else:
                assert "NANOFAAS_OFFLOAD_TARGETURL" not in actual
