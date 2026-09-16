"""Reject soak protocols that cannot support their declared conclusion."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig


@pytest.fixture
def scenario_data() -> dict:
    roles = ("control-plane", "word-stats-java")
    return {
        "workflow": "soak",
        "backend": "container",
        "functions": ["word-stats-java"],
        "soak": {
            "purpose": "p24",
            "phases": {
                "warmup_s": 60,
                "baseline_drain_s": 2100,
                "baseline_window_s": 30,
                "steady_s": 5400,
                "drain_s": 2100,
                "cleanup_margin_s": 300,
            },
            "retention_s": {"outcomes": 30, "keys": 300, "inflight": 1800},
            "roles": {
                role: {
                    "runtime": "jvm",
                    "expected_cpu": 2.0,
                    "memory_limit_bytes": 1073741824,
                    "required_metrics": [
                        "process_rss_bytes",
                        "cgroup_memory_usage_bytes",
                        "execution_in_flight_records",
                    ],
                    "required_capabilities": ["rss", "cgroup", "retained_state"],
                    "runtime_options": ["-Xmx512m"],
                    "collection_sources": ["procfs", "cgroup", "prometheus"],
                }
                for role in roles
            },
            "images": {
                role: {
                    "variant": "jvm",
                    "platform": "linux/amd64",
                    "modules": ["container-deployment-provider", "async-queue"]
                    if role == "control-plane"
                    else [],
                }
                for role in roles
            },
            "criteria": [
                criterion
                for role in roles
                for criterion in (
                    {
                        "id": f"{role}.budget",
                        "role": role,
                        "metric": "cgroup_memory_usage_bytes",
                        "unit": "bytes",
                        "operation": "maximum",
                        "phase": "steady",
                        "window_s": 5400,
                        "threshold": 1073741824,
                        "rationale": "Declared container memory budget",
                    },
                    {
                        "id": f"{role}.rss",
                        "role": role,
                        "metric": "process_rss_bytes",
                        "unit": "bytes",
                        "operation": "return_to_reference",
                        "phase": "drain",
                        "window_s": 30,
                        "deadline_s": 2100,
                        "absolute_tolerance": 10485760,
                        "relative_tolerance": 0.05,
                        "rationale": "Test-only explicit RSS recovery policy",
                    },
                    {
                        "id": f"{role}.live",
                        "role": role,
                        "metric": "execution_in_flight_records",
                        "unit": "count",
                        "operation": "expected_zero",
                        "phase": "drain",
                        "window_s": 30,
                        "deadline_s": 2100,
                        "rationale": "Physical work must have settled after expiry",
                    },
                )
            ],
            "diagnostics": {
                "operations": {role: ["gc", "histogram"] for role in roles},
                "timeout_s": 30,
                "max_dumps": 2,
                "max_dump_bytes": 2147483648,
                "gc_completion_evidence": dict.fromkeys(roles, "full-gc-event"),
            },
            "prerequisites": {
                "mode": "run",
                "required_coverage": ["sync"],
                "relevant_config_keys": {"sync": ["images", "roles", "workload"]},
            },
            "sample_interval_s": 5,
            "scrape_timeout_s": 2,
            "max_observation_gap_s": 15,
            "artifact_limit_bytes": 4294967296,
            "cancellation_timeout_s": 60,
            "workload": {
                "rates": {"word-stats-java": 100},
                "preallocated_vus": 200,
                "max_vus": 200,
                "max_error_ratio": 0,
                "max_dropped_iterations": 0,
            },
        },
    }


def parse_soak(data):
    from nanolab.config.soak import SoakConfig

    return SoakConfig.model_validate(data)


def test_single_function_soak_is_not_forced_into_comparison(scenario_data):
    config = ScenarioConfig.model_validate(scenario_data)
    soak = config.soak
    assert soak is not None
    assert soak.images["control-plane"].mode == "build"
    assert soak.images["word-stats-java"].mode == "build"
    assert soak.phases.steady_s == 5400


def test_native_recipes_are_preserved_without_prebuilt_tags(scenario_data):
    for role in scenario_data["soak"]["roles"]:
        scenario_data["soak"]["roles"][role]["runtime"] = "native"
        scenario_data["soak"]["images"][role]["variant"] = "native-o3-serial"
    config = ScenarioConfig.model_validate(scenario_data)
    soak = config.soak
    assert soak is not None
    assert soak.images["word-stats-java"].variant == "native-o3-serial"
    assert soak.images["word-stats-java"].digest is None


def test_historical_schedule_covers_three_cycles():
    from nanolab.config.soak import validate_schedule

    validate_schedule(5400, 2100, (30, 300, 1800), 300)


@pytest.mark.parametrize(
    ("steady", "drain", "retention", "margin"),
    [
        (5399, 2100, (1800,), 300),
        (5400, 2099, (1800,), 300),
        (5400, 2100, (), 300),
        (5400, 2100, (0,), 300),
        (5400, 2100, (1800,), 0),
        (5400, 2100, (1800,), -1),
    ],
)
def test_incomplete_or_invalid_schedule_is_rejected(steady, drain, retention, margin):
    from nanolab.config.soak import validate_schedule

    with pytest.raises(
        ValueError,
        match=r"drain must include|schedule requires positive|steady must cover",
    ):
        validate_schedule(steady, drain, retention, margin)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("phases", "steady_s", 5399),
        ("phases", "drain_s", 2099),
        ("phases", "baseline_drain_s", 30),
        ("phases", "baseline_window_s", 2),
        ("phases", "warmup_s", 0),
        ("phases", "drain_s", True),
        ("phases", "steady_s", "5400"),
        ("phases", "warmup_s", float("nan")),
        ("workload", "max_vus", 199),
        ("workload", "rates", {"word-stats-java": 0}),
        ("workload", "max_error_ratio", float("inf")),
        ("prerequisites", "required_coverage", []),
        ("prerequisites", "mode", "receipts"),
    ],
)
def test_protocol_rejects_invalid_nested_values(scenario_data, section, key, value):
    data = scenario_data["soak"]
    data[section][key] = value
    with pytest.raises(ValidationError, match="validation error for SoakConfig"):
        parse_soak(data)


def test_diagnostic_purpose_carries_no_criteria_or_retention(scenario_data):
    data = scenario_data["soak"]
    data.update(purpose="diagnostic", criteria=[], retention_s={})
    protocol = parse_soak(data)
    assert protocol.criteria == []
    assert protocol.retention_s == {}


def test_soak_scenario_refuses_the_internal_diagnostic_purpose(scenario_data):
    scenario_data["soak"].update(purpose="diagnostic", criteria=[], retention_s={})
    with pytest.raises(ValidationError, match="p24 or smoke purpose"):
        ScenarioConfig.model_validate(scenario_data)


@pytest.mark.parametrize("purpose", ["p24", "smoke"])
@pytest.mark.parametrize("key", ["criteria", "retention_s"])
def test_measured_purposes_still_require_criteria_and_retention(
    scenario_data, purpose, key
):
    data = scenario_data["soak"]
    data["purpose"] = purpose
    data[key] = [] if key == "criteria" else {}
    with pytest.raises(ValidationError, match="acceptance criteria and retention"):
        parse_soak(data)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sample_interval_s", 16),
        ("scrape_timeout_s", 16),
        ("max_observation_gap_s", 1),
        ("artifact_limit_bytes", 1024),
        ("cancellation_timeout_s", 0),
        ("sample_interval_s", float("nan")),
        ("purpose", "benchmark"),
        ("retention_s", {}),
        ("criteria", []),
    ],
)
def test_protocol_rejects_unmeasurable_policy(scenario_data, key, value):
    data = scenario_data["soak"]
    data[key] = value
    with pytest.raises(ValidationError, match="validation error for SoakConfig"):
        parse_soak(data)


@pytest.mark.parametrize(
    "change",
    [
        "missing_image",
        "extra_image",
        "missing_role",
        "extra_rate",
        "missing_rate",
        "missing_metric",
        "unknown_criterion_role",
        "duplicate_criterion",
        "missing_threshold",
        "missing_rss_tolerance",
        "deadline_after_drain",
        "window_after_deadline",
        "unknown_diagnostic_role",
        "unverified_gc",
        "no_rss_policy",
        "no_cgroup_budget",
        "budget_over_limit",
        "rss_wrong_unit",
        "unused_threshold",
        "extra_field",
        "blank_rationale",
        "duplicate_capability",
    ],
)
def test_cross_field_contract_cannot_hide_missing_evidence(scenario_data, change):
    data = scenario_data["soak"]
    if change == "missing_image":
        del data["images"]["word-stats-java"]
    elif change == "extra_image":
        data["images"]["unknown"] = deepcopy(data["images"]["control-plane"])
    elif change == "missing_role":
        del data["roles"]["word-stats-java"]
    elif change == "extra_rate":
        data["workload"]["rates"]["unknown"] = 1
    elif change == "missing_rate":
        data["workload"]["rates"] = {}
    elif change == "missing_metric":
        data["roles"]["control-plane"]["required_metrics"].remove("process_rss_bytes")
    elif change == "unknown_criterion_role":
        data["criteria"][0]["role"] = "unknown"
    elif change == "duplicate_criterion":
        data["criteria"].append(deepcopy(data["criteria"][0]))
    elif change == "missing_threshold":
        del data["criteria"][0]["threshold"]
    elif change == "missing_rss_tolerance":
        del data["criteria"][1]["absolute_tolerance"]
    elif change == "deadline_after_drain":
        data["criteria"][1]["deadline_s"] = 2101
    elif change == "window_after_deadline":
        data["criteria"][1]["window_s"] = 31
        data["criteria"][1]["deadline_s"] = 30
    elif change == "unknown_diagnostic_role":
        data["diagnostics"]["operations"]["unknown"] = ["histogram"]
    elif change == "unverified_gc":
        del data["diagnostics"]["gc_completion_evidence"]["control-plane"]
    elif change == "no_rss_policy":
        data["criteria"] = [
            c for c in data["criteria"] if c["operation"] != "return_to_reference"
        ]
    elif change == "no_cgroup_budget":
        data["criteria"] = [c for c in data["criteria"] if c["operation"] != "maximum"]
    elif change == "budget_over_limit":
        data["criteria"][0]["threshold"] = 2147483648
    elif change == "rss_wrong_unit":
        data["criteria"][1]["unit"] = "count"
    elif change == "unused_threshold":
        data["criteria"][1]["threshold"] = 1
    elif change == "extra_field":
        data["images"]["control-plane"]["silent_fallback"] = True
    elif change == "blank_rationale":
        data["criteria"][0]["rationale"] = "  "
    elif change == "duplicate_capability":
        data["roles"]["control-plane"]["required_capabilities"].append("rss")
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(scenario_data)


def test_smoke_is_explicitly_short_and_not_p24(scenario_data):
    data = scenario_data["soak"]
    data["purpose"] = "smoke"
    data["phases"].update(steady_s=60, drain_s=60, baseline_drain_s=60)
    data["prerequisites"] = {
        "mode": "run",
        "required_coverage": [],
        "relevant_config_keys": {},
    }
    for criterion in data["criteria"]:
        criterion["window_s"] = 30
        if "deadline_s" in criterion:
            criterion["deadline_s"] = 60
    config = ScenarioConfig.model_validate(scenario_data)
    soak = config.soak
    assert soak is not None
    assert soak.purpose == "smoke"
    data["purpose"] = "p24"
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(scenario_data)


def test_prebuilt_mode_requires_digest_and_provenance(scenario_data):
    data = scenario_data["soak"]
    image = data["images"]["word-stats-java"]
    image["mode"] = "prebuilt"
    with pytest.raises(ValidationError, match="validation error for SoakConfig"):
        parse_soak(data)
    image["digest"] = "registry.example/fn@sha256:" + "a" * 64
    image["provenance_receipt"] = "receipts/java.json"
    assert parse_soak(data).images["word-stats-java"].mode == "prebuilt"


@pytest.mark.parametrize("digest", ["fn:latest", "sha256:" + "a" * 64, "fn@sha256:abc"])
def test_prebuilt_tag_is_not_an_immutable_reference(scenario_data, digest):
    image = scenario_data["soak"]["images"]["word-stats-java"]
    image.update(mode="prebuilt", digest=digest, provenance_receipt="receipt.json")
    with pytest.raises(ValidationError, match="validation error for SoakConfig"):
        parse_soak(scenario_data["soak"])


def test_build_mode_cannot_silently_use_a_prebuilt_digest(scenario_data):
    scenario_data["soak"]["images"]["word-stats-java"]["digest"] = (
        "fn@sha256:" + "a" * 64
    )
    with pytest.raises(ValidationError, match="validation error for SoakConfig"):
        parse_soak(scenario_data["soak"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("backend", "k8s"),
        ("autoscaling", True),
        ("concurrencyControl", True),
        ("loadProfile", "mixed"),
        ("soakMinutes", 90),
        ("drainMinutes", 35),
        ("controlPlaneImage", "cp:latest"),
        ("functionImages", {"word-stats-java": "fn:latest"}),
        ("controlPlaneRuntime", "native"),
        ("controlPlaneVariant", "jvm"),
        ("loadScale", 2),
        ("loadVus", 100),
        ("asyncShare", 0),
        ("idemShare", 0),
    ],
)
def test_soak_does_not_silently_accept_legacy_workload_knobs(scenario_data, key, value):
    scenario_data[key] = value
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(scenario_data)


def test_soak_block_cannot_be_ignored_by_a_legacy_workflow(scenario_data):
    scenario_data["workflow"] = "loadtest"
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(scenario_data)


def test_soak_workflow_requires_its_protocol_block(scenario_data):
    del scenario_data["soak"]
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(scenario_data)


def test_top_level_resources_cannot_disagree_with_measured_limits(scenario_data):
    scenario_data["resources"] = {
        "control-plane": {"limits": {"cpu": 2, "memoryMiB": 512}}
    }
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(scenario_data)


def test_existing_loadtest_keeps_its_original_contract():
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "container",
            "functions": ["word-stats-java"],
        }
    )
    assert config.workflow == "loadtest"
