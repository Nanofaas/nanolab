"""The B1 controller comparison holds workload and capacity constant."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from nanolab.application.loadtest_policy import controller_settings
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.loadtest import _build_platform_request, concurrency_budget
from nanolab.tasks.platform import PlatformFunction

ASSETS = Path(__file__).parents[2] / "src/nanolab/assets"
MODES = ("ADAPTIVE_PER_POD", "SOJOURN", "BUDGETED")


def test_b1_presets_differ_only_by_controller():
    common = None
    for suffix, mode in zip(("adaptive", "sojourn", "budgeted"), MODES, strict=True):
        payload = yaml.safe_load(
            (
                ASSETS / f"presets/scenarios/concurrency-comparison-{suffix}.yaml"
            ).read_text()
        )
        scenario = ScenarioConfig.model_validate(payload)
        assert scenario.concurrency_mode == mode
        assert concurrency_budget(scenario) == "16"
        payload.pop("concurrencyMode")
        assert common is None or payload == common
        common = payload


@pytest.mark.parametrize("value", [0, -1])
def test_budget_must_be_positive(value):
    with pytest.raises(ValueError, match="greater than 0"):
        ScenarioConfig.model_validate(
            {
                "workflow": "loadtest",
                "functions": ["word-stats-java"],
                "concurrencyControl": True,
                "concurrencyTotalBudget": value,
            }
        )


def test_budget_requires_governor():
    with pytest.raises(ValueError, match="concurrencyTotalBudget"):
        ScenarioConfig.model_validate(
            {
                "workflow": "loadtest",
                "functions": ["word-stats-java"],
                "concurrencyTotalBudget": 16,
            }
        )


def test_kubernetes_receives_explicit_budget(monkeypatch):
    monkeypatch.setattr(
        "nanolab.plans.loadtest.source_fingerprint", lambda root: "frozen"
    )
    nanofaas_root = Path("/repo")
    request = _build_platform_request(
        backend="k8s",
        build="docker",
        functions=(
            PlatformFunction(
                name="word-stats-java", image="i:test", payload="{}", build_argv=("x",)
            ),
        ),
        additional_modules=("concurrency-control", "async-queue"),
        functions_prebuilt=False,
        prebuilt_control_plane_image="cp:test",
        root=nanofaas_root,
        remote_repo_root=nanofaas_root,
        hpa=False,
        concurrency_total_budget="16",
    )
    assert (
        "controlPlane.extraEnv[0].name=NANOFAAS_CONCURRENCY_CONTROL_TOTAL_BUDGET"
        in request.helm_values
    )
    i = request.helm_values.index("controlPlane.extraEnv[0].value=16")
    assert request.helm_values[i - 1] == "--set-string"


def test_sojourn_uses_payload_specific_latency_budget():
    scenario = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "functions": ["word-stats-java"],
            "concurrencyControl": True,
            "concurrencyMode": "SOJOURN",
            "payloadProfile": "medium",
        }
    )
    assert controller_settings(scenario)["targetLatencyMs"] == 120


def test_openloop_uses_selected_corpus(tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps({"cases": [{"input": {"text": "corpus-only", "topN": 3}}]})
    )
    source = (ASSETS / "k6/open-loop.js").read_text()
    source = "\n".join(
        line for line in source.splitlines() if not line.startswith("import ")
    ).replace("export ", "")
    harness = """const fs=require('fs'); const __ENV={NANOFAAS_PAYLOAD:process.argv[1]};
    const __ITER=0; const open=p=>fs.readFileSync(p,'utf8');
    class SharedArray {constructor(name,factory){return factory();}}
    const check=()=>{};
    const http={post:(url,body)=>{console.log(body);return {status:200};}};
    """
    result = subprocess.run(
        ["node", "-e", harness + source + "\nsteady();burst();", str(corpus)],
        capture_output=True,
        text=True,
        check=True,
    )
    bodies = [json.loads(line) for line in result.stdout.splitlines()]
    assert bodies == [{"input": {"text": "corpus-only", "topN": 3}}] * 2


def test_payload_uses_the_synced_loadgen_repository(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "nanolab.application.loadtest_policy.resolve_function_definition",
        lambda function: SimpleNamespace(family="word-stats"),
    )
    from nanolab.application.loadtest_policy import payload_corpus_path

    corpus = tmp_path / "functions/test-data/word-stats/performance-medium.json"
    corpus.parent.mkdir(parents=True)
    corpus.write_text("{}")
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "functions": ["word-stats-java"],
            "payloadProfile": "medium",
        }
    )
    assert payload_corpus_path(
        config,
        tmp_path,
        "word-stats-java",
        destination_root=Path("/home/azureuser/nanofaas"),
    ) == Path(
        "/home/azureuser/nanofaas/functions/test-data/word-stats/performance-medium.json"
    )


@pytest.mark.parametrize("limit", [1, 2, 4, 8])
def test_b11_fixed_baselines_keep_the_common_protocol(limit):
    from nanolab.application.loadtest_policy import concurrency_control_setup

    common = yaml.safe_load(
        (ASSETS / "presets/scenarios/concurrency-comparison-adaptive.yaml").read_text()
    )
    fixed = yaml.safe_load(
        (
            ASSETS / f"presets/scenarios/concurrency-comparison-fixed{limit}.yaml"
        ).read_text()
    )
    config = ScenarioConfig.model_validate(fixed)
    assert config.fixed_concurrency == limit
    assert concurrency_control_setup(config)["concurrencyControl"] == {"mode": "FIXED"}
    fixed.pop("fixedConcurrency")
    fixed["concurrencyMode"] = "ADAPTIVE_PER_POD"
    assert fixed == common


def test_fixed_mode_requires_explicit_valid_limit():
    for limit in (None, 0, 9):
        with pytest.raises(ValueError, match="fixedConcurrency"):
            ScenarioConfig.model_validate(
                {
                    "workflow": "loadtest",
                    "functions": ["word-stats-java"],
                    "concurrencyControl": True,
                    "concurrencyMode": "FIXED",
                    "fixedConcurrency": limit,
                }
            )


def test_fixed_observations_do_not_require_adaptive_response():
    from nanolab.tasks.loadtest.concurrency import (
        ConcurrencySample,
        ConcurrencySummary,
        verify_fixed_concurrency,
    )

    summary = ConcurrencySummary(
        function_name="fn",
        samples=(
            ConcurrencySample(elapsed_seconds=0, effective=1, in_flight=0),
            ConcurrencySample(elapsed_seconds=1, effective=1, in_flight=1),
        ),
    )
    verify_fixed_concurrency(summary, 1)
    with pytest.raises(RuntimeError, match="fixed"):
        verify_fixed_concurrency(summary, 2)
    with pytest.raises(RuntimeError):
        verify_fixed_concurrency(ConcurrencySummary(function_name="fn"), 1)


def test_fixed_task_records_a_constant_single_slot_series(monkeypatch):
    from types import SimpleNamespace

    from sonata_engine import TaskInputs

    from nanolab.tasks.loadtest import LoadtestOutcome, ReportCoTenancyTask
    from nanolab.tasks.loadtest.concurrency import ConcurrencySample, ConcurrencySummary

    summary = ConcurrencySummary(
        function_name="fn",
        samples=(ConcurrencySample(elapsed_seconds=1, effective=1, in_flight=1),),
    )
    outcome = LoadtestOutcome(k6=object())
    monkeypatch.setattr(
        "nanolab.tasks.loadtest.load_outcome", lambda inputs, title: outcome
    )
    task = ReportCoTenancyTask(
        watchers=SimpleNamespace(summaries=lambda: {"fn": summary}), fixed_concurrency=1
    )
    assert task.run(TaskInputs.empty()).value.concurrency is summary


@pytest.mark.parametrize(("api_port", "metrics_port"), [(8080, 8081), (30080, 30081)])
def test_concurrency_watchers_use_management_port(api_port, metrics_port):
    from types import SimpleNamespace

    from nanolab.plans.loadtest import (
        _build_concurrency_watcher,
        _build_concurrency_watchers,
    )

    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "concurrencyControl": True,
            "functions": ["word-stats-java", "word-stats-java-lite"],
        }
    )
    functions = tuple(SimpleNamespace(name=name) for name in config.functions)
    url = f"http://20.224.195.10:{api_port}"
    watcher = _build_concurrency_watcher(
        config=config,
        control_plane_url=url,
        target=functions[0],
    )
    group = _build_concurrency_watchers(
        config=config,
        control_plane_url=url,
        functions=functions,
    )
    assert watcher._probe.management_url == f"http://20.224.195.10:{metrics_port}"
    assert all(
        w._probe.management_url == f"http://20.224.195.10:{metrics_port}"
        for w in group._watchers.values()
    )
