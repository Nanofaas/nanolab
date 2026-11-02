import pytest

from nanolab.tasks.module_cost_report import contrasts, cpu_window


def test_cpu_counter_delta_and_reset_are_not_missing_as_zero():
    points = [
        {"timestamp": t, "value": v, "labels": {"pod": "cp"}}
        for t, v in ((0, 10), (5, 11), (10, 13))
    ]
    assert cpu_window(points, "cp", 0, 10) == pytest.approx(0.3)
    points[1]["value"] = 9
    with pytest.raises(ValueError, match="reset"):
        cpu_window(points, "cp", 0, 10)
    with pytest.raises(ValueError, match="coverage"):
        cpu_window([], "cp", 0, 10)


def test_within_runtime_contrast_signs():
    assert contrasts({"base": 10, "c": 13, "s": 12, "cs": 16}) == {
        "c-base": 3,
        "s-base": 2,
        "cs-base": 6,
        "interaction": 1,
    }


def arm_fixture(root, monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import nanolab.tasks.module_cost_report as module
    from nanolab.config.module_cost import BUDGETS
    from nanolab.tasks.module_cost import FUNCTION, fixed_function

    arm = root / "arms/native-c"
    arm.mkdir(parents=True)
    import yaml

    from nanolab.comparison.module_cost import module_cost_cells

    recipe = root / "build/native-c/recipe.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(yaml.safe_dump(module_cost_cells("test")["native-c"]))

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    response = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/module-cost/function-response.json"
        ).read_text()
    )
    digest = response["image"]
    cp_digest = "registry/cp@sha256:" + "b" * 64
    write(root / "build/jvm-base/images.json", {"function": {"digest": digest}})
    write(
        root / "build/native-c/images.json",
        {"control-plane": {"uncompressed_image_bytes": 123}},
    )
    write(root / "build/native-c/artifact-files.json", {"application": 99})
    (arm / "boot-id.txt").write_text("boot-1")
    (arm / "control-plane.log").write_text(
        "ConcurrencyGovernor starting with poll interval 5000ms"
    )
    write(
        arm / "deployment.json",
        {"images": {"function": digest, "control-plane": cp_digest}},
    )
    body = fixed_function(digest).manifest().body()
    write(arm / "registration.json", body)
    for name in ("spec-before", "spec-after"):
        write(arm / (name + ".json"), response)
    write(
        arm / "measurement.json",
        {
            "status": "collected",
            "start": 0,
            "end": 420,
            "idle_start": 60,
            "idle_end": 240,
            "load_window": [240, 420],
        },
    )
    items = [
        {
            "kind": "Deployment",
            "metadata": {"name": "fn-" + FUNCTION},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"fn": "word"}}},
            "status": {"readyReplicas": 1},
        }
    ]
    for name, image in (("control-plane", cp_digest), ("function", digest)):
        budget = BUDGETS["control-plane" if name == "control-plane" else FUNCTION]
        resources = {
            k: {"cpu": str(v["cpu"]), "memory": str(v["memoryMiB"]) + "Mi"}
            for k, v in budget.items()
        }
        items.append(
            {
                "kind": "Pod",
                "metadata": {
                    "uid": name + "-uid",
                    "name": name + "-pod",
                    "labels": {"fn": "word"} if name == "function" else {},
                },
                "spec": {"containers": [{"name": name, "resources": resources}]},
                "status": {
                    "containerStatuses": [
                        {
                            "name": name,
                            "ready": True,
                            "restartCount": 0,
                            "imageID": image,
                            "state": {
                                "running": {"startedAt": "1970-01-01T00:00:00+00:00"}
                            },
                        }
                    ],
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True",
                            "lastTransitionTime": "1970-01-01T00:00:02+00:00",
                        }
                    ],
                },
            }
        )
    (arm / "lifecycle.jsonl").write_text(
        "\n".join(
            json.dumps({"time": t, "raw": {"items": items}}) for t in range(0, 421, 2)
        )
    )
    queries = {
        "scrape_health": {
            "points": [
                {"timestamp": t, "value": 1, "labels": {"job": "kubernetes-cadvisor"}}
                for t in range(0, 451, 5)
            ]
        },
        "function_effective_concurrency": {
            "points": [{"timestamp": t, "value": 2} for t in range(0, 451, 5)]
        },
    }
    queries["function_effective_concurrency_timestamp"] = {
        "points": [{"timestamp": t, "value": t} for t in range(0, 451, 5)]
    }
    for metric in (
        "container_memory_bytes",
        "container_rss_bytes",
        "container_cpu_seconds",
    ):
        for name, pod in (
            ("control-plane", "control-plane-pod"),
            (FUNCTION, "function-pod"),
        ):
            queries[metric + "@" + name] = {
                "points": [
                    {
                        "timestamp": t,
                        "value": t * 0.2
                        if metric == "container_cpu_seconds"
                        else (10 + t / 10) * 1048576,
                        "labels": {"pod": pod},
                    }
                    for t in range(0, 451, 5)
                ]
            }
    write(
        arm / "metrics/prometheus-snapshot.json",
        {"start": "1970-01-01T00:00:00+00:00", "queries": queries},
    )
    monkeypatch.setattr(
        module,
        "read_distribution",
        lambda *a, **kw: SimpleNamespace(modules=("concurrency-control",)),
    )
    monkeypatch.setattr(
        module,
        "read_load",
        lambda *a: {
            "valid": 100,
            "emitted": 100,
            "dropped": 0,
            "first_send_ms": 240000 if a[-1] == "measurement" else 0,
            "last_terminal_ms": 420000 if a[-1] == "measurement" else 60000,
        },
    )
    write(
        arm / "warmup/clock.json",
        [
            {"sent": -1, "received": 1, "remote": 0},
            {"sent": 60, "received": 62, "remote": 61},
        ],
    )
    write(
        arm / "measurement/clock.json",
        [
            {"sent": 239, "received": 241, "remote": 240},
            {"sent": 420, "received": 422, "remote": 421},
        ],
    )
    return arm, {"host": {"boot_id": "boot-1"}, "protocol": {"blockId": "test"}}


def test_arm_integrals_and_readiness_use_raw_evidence(tmp_path, monkeypatch):
    from nanolab.tasks.module_cost_report import analyze_arm

    _, protocol = arm_fixture(tmp_path, monkeypatch)
    result = analyze_arm(tmp_path, "native-c", protocol)
    # Primary window is [120,240]: a linear 22->34 MiB signal averages 28.
    assert result["idle"]["rss"]["control_plane_mean_mib"] == pytest.approx(28)
    assert result["idle_cpu_cores"]["control_plane"] == pytest.approx(0.2)
    assert result["readiness_seconds"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-memory",
        "reset-cpu",
        "host",
        "policy",
        "module",
        "pod",
        "limits",
        "stale-concurrency",
    ],
)
def test_arm_rejects_unmatched_evidence(tmp_path, monkeypatch, mutation):
    import json

    from nanolab.tasks.module_cost_report import analyze_arm

    arm, protocol = arm_fixture(tmp_path, monkeypatch)
    if mutation in ("missing-memory", "reset-cpu", "stale-concurrency"):
        path = arm / "metrics/prometheus-snapshot.json"
        data = json.loads(path.read_text())
        if mutation == "stale-concurrency":
            data["queries"]["function_effective_concurrency_timestamp"] = {
                "points": [{"timestamp": t, "value": 0} for t in range(0, 451, 5)]
            }
        elif mutation == "missing-memory":
            data["queries"]["container_rss_bytes@control-plane"]["points"] = []
        else:
            data["queries"]["container_cpu_seconds@control-plane"]["points"][30][
                "value"
            ] = 0
        path.write_text(json.dumps(data))
    elif mutation == "host":
        (arm / "boot-id.txt").write_text("replacement")
    elif mutation == "module":
        (arm / "control-plane.log").write_text("Started application")
    elif mutation == "policy":
        (arm / "spec-after.json").write_text("{}")
    else:
        path = arm / "lifecycle.jsonl"
        data = [json.loads(line) for line in path.read_text().splitlines()]
        if mutation == "pod":
            data[50]["raw"]["items"][1]["metadata"]["uid"] = "replacement"
        else:
            data[50]["raw"]["items"][1]["spec"]["containers"][0]["resources"]["limits"][
                "cpu"
            ] = "1"
        path.write_text("\n".join(json.dumps(row) for row in data))
    with pytest.raises(
        ValueError,
        match=r"coverage|reset|host|policy|initialization|lifecycle|budget|stale",
    ):
        analyze_arm(tmp_path, "native-c", protocol)


def test_one_concurrency_sample_cannot_qualify_whole_arm(tmp_path, monkeypatch):
    import json

    from nanolab.tasks.module_cost_report import analyze_arm

    arm, protocol = arm_fixture(tmp_path, monkeypatch)
    path = arm / "metrics/prometheus-snapshot.json"
    snapshot = json.loads(path.read_text())
    snapshot["queries"]["function_effective_concurrency"]["points"] = [
        {"timestamp": 120, "value": 2}
    ]
    path.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError, match=r"concurrency.*coverage"):
        analyze_arm(tmp_path, "native-c", protocol)


def test_cohort_window_ignores_staging_and_fetch_delays():
    from nanolab.tasks.module_cost_report import cohort_window

    # Generator is 20 s ahead. Network and collection can finish much later.
    load = {"first_send_ms": 120000, "last_terminal_ms": 300000}
    anchors = [
        {"sent": 98, "received": 100, "remote": 119},
        {"sent": 350, "received": 352, "remote": 371},
    ]
    assert cohort_window(load, anchors) == (100, 280)
    anchors[1]["remote"] = 381
    with pytest.raises(ValueError, match="clock"):
        cohort_window(load, anchors)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requestedExecutionMode", "EXTERNAL"),
        ("effectiveExecutionMode", "EXTERNAL"),
        ("effectiveExecutionMode", None),
        ("deploymentBackend", "docker"),
        ("concurrency", 3),
        ("queueSize", 21),
        ("maxRetries", 1),
    ],
)
def test_effective_policy_rejects_missing_or_changed_response_fields(
    tmp_path, monkeypatch, when, field, value
):
    import json

    from nanolab.tasks.module_cost_report import analyze_arm

    arm, protocol = arm_fixture(tmp_path, monkeypatch)
    path = arm / f"spec-{when}.json"
    response = json.loads(path.read_text())
    if value is None:
        del response[field]
    else:
        response[field] = value
    path.write_text(json.dumps(response))
    with pytest.raises(ValueError, match="effective function policy drift"):
        analyze_arm(tmp_path, "native-c", protocol)
