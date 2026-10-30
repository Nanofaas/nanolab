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


def test_payload_uses_the_synced_loadgen_repository(tmp_path):
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
