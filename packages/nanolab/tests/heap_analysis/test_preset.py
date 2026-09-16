"""The shipped heap-analysis preset resolves to the exact frozen protocol.

Not a P24 measurement: no soakPolicyFile, no criteria, no retention, no
prerequisites. Only checks that shape and the numbers the spec pins.
"""

from pathlib import Path

import yaml

from nanolab.config.scenario import ScenarioConfig

PRESET = (
    Path(__file__).resolve().parents[2]
    / "scenarios-v2"
    / "memory-heap-analysis-control-plane-container.yaml"
)


def _load() -> ScenarioConfig:
    raw = yaml.safe_load(PRESET.read_text())
    return ScenarioConfig.model_validate(raw)


def test_preset_resolves_to_the_frozen_heap_analysis_protocol():
    config = _load()
    assert config.workflow == "heap-analysis"
    assert config.heap_analysis is not None
    assert config.heap_analysis.target == "control-plane"
    assert config.heap_analysis.warmup_s == 120
    assert config.heap_analysis.steady_s == 900
    assert config.heap_analysis.drain_s == 420
    assert sum(config.heap_analysis.workload.rates.values()) == 200
    assert config.heap_analysis.max_dumps == 2


def test_preset_has_no_p24_policy_criteria_retention_or_prerequisites():
    raw = yaml.safe_load(PRESET.read_text())
    assert "soakPolicyFile" not in raw
    heap_analysis = raw["heapAnalysis"]
    assert "criteria" not in heap_analysis
    assert "retention_s" not in heap_analysis
    assert "prerequisites" not in heap_analysis


def test_preset_helper_image_is_digest_pinned_not_a_mutable_tag():
    config = _load()
    assert config.heap_analysis is not None
    helper_image = config.heap_analysis.helper_image
    assert "@sha256:" in helper_image
    assert ":" not in helper_image.split("@sha256:", 1)[0].split("/")[-1]


def test_preset_max_dump_bytes_is_at_or_below_one_gib():
    config = _load()
    assert config.heap_analysis is not None
    assert config.heap_analysis.max_dump_bytes <= 1024**3


def test_preset_only_declares_the_node_preload_for_a_provisioned_role():
    """A real run demonstrated a crash caused by an inconsistent preset.

    The preset declared the node diagnostic preload on
    word-stats-javascript, but heap-analysis only ever diagnoses
    its single `target` role (control-plane here). Because
    `_diagnostic_resource_inputs` only writes and mounts
    `node-diagnostic-control.cjs` for roles in its diagnostics operations,
    setting `NODE_OPTIONS=--require=...` on a role that is never a
    diagnostics target makes Node fail to find the module and the container
    exits immediately, crash-looping the whole deployment.
    """
    from nanolab.tasks.soak.runtime import _NODE_PRELOAD

    config = _load()
    assert config.heap_analysis is not None
    target = config.heap_analysis.target
    for role, policy in config.heap_analysis.roles.items():
        if role == target:
            continue
        assert _NODE_PRELOAD not in policy.runtime_options, (
            f"role {role!r} is not the diagnostics target {target!r} but "
            "declares the node preload, which is never provisioned for it"
        )
