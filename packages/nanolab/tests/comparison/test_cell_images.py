"""Every prepared cell verifies the active strategy before generating traffic."""

from typing import cast

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.models import TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.comparison.evidence import (
    require_comparison_scheduler,
    verify_comparison_scheduler,
)


@pytest.mark.parametrize(
    "metrics",
    [
        'scheduler_active{strategy="per-function"} 1\n',
        "# HELP scheduler_active Current strategy\nother 99\n"
        'scheduler_active{pod="cp",strategy="per-function"} 1.0\n',
        'scheduler_active{strategy="per-function",label="escaped\\"quote"} 1e0\n',
    ],
)
def test_single_active_per_function_strategy_is_accepted(metrics):
    require_comparison_scheduler(metrics)


@pytest.mark.parametrize(
    "metrics",
    [
        "",
        "other 1\n",
        "scheduler_active 1\n",
        'scheduler_active{strategy="per-function"} 0\n',
        'scheduler_active{strategy="per-function"} NaN\n',
        'scheduler_active{strategy="per-function"} +Inf\n',
        'scheduler_active{strategy="shared-queue"} 1\n',
        'scheduler_active{strategy="per-function"} 1\n'
        'scheduler_active{strategy="shared-queue"} 0\n',
        'scheduler_active{strategy="per-function"} 1\n'
        'scheduler_active{strategy="per-function"} 1\n',
        'scheduler_active{strategy="per-function",strategy="per-function"} 1\n',
        "scheduler_active{strategy=per-function} 1\n",
        'scheduler_active{strategy="per-function"} garbage\n',
    ],
)
def test_unknown_inactive_or_ambiguous_scheduler_is_rejected(metrics):
    with pytest.raises(ValueError, match="scheduler"):
        require_comparison_scheduler(metrics)


@pytest.mark.parametrize("code", [0, 7])
def test_scheduler_strategy_mismatch_retains_response(tmp_path, code):
    response = 'scheduler_active{strategy="shared-queue"} 1\n'

    class Executor:
        def __init__(self):
            self.calls = []

        def run(self, task, **kwargs):
            self.calls.append(task)
            return TaskResult(
                task_id="",
                status="passed" if code == 0 else "failed",
                return_code=code,
                stdout=response,
                stderr="error" if code else "",
            )

    executor = Executor()
    with pytest.raises((ValueError, RuntimeError), match="scheduler"):
        verify_comparison_scheduler(
            endpoint="http://10.43.0.7:8080",
            executor=cast(CommandTaskExecutor, executor),
            inputs=TaskInputs.empty(),
            run_dir=tmp_path,
        )
    assert (tmp_path / "scheduler-metrics.txt").read_text() == response
    assert executor.calls[0].argv == (
        "curl",
        "-fsS",
        "--max-time",
        "15",
        "http://10.43.0.7:8081/actuator/prometheus",
    )
    assert executor.calls[0].role == "stack"


@pytest.mark.parametrize("failure", ["scheduler", "pod", "metadata", "registry", None])
def test_prepared_cell_verifies_all_identities_before_k6(
    tmp_path, nanofaas_root, failure
):
    import json

    from sonata_engine import Resource, Workflow
    from sonata_tasks.command import CommandTask
    from sonata_tasks.execution.bindings import RoleBindings

    from nanolab.config.scenario import ScenarioConfig
    from nanolab.plans.runtime_comparison import _prepared_checks
    from nanolab.tasks.platform import Platform
    from nanolab.tasks.recipe import RecipeComponent, RecipeDistribution, RecipeImage

    digest = "sha256:" + "a" * 64
    config_id = "sha256:" + "b" * 64
    components = tuple(
        RecipeComponent(
            kind,
            name,
            sdk,
            mode,
            RecipeImage(
                f"127.0.0.1:5000/nanofaas/{image}:recipe-test",
                config_id,
                "published",
                digest,
            ),
            "jvm" if kind == "control-plane" else None,
            "c1" if kind == "control-plane" else None,
        )
        for kind, name, sdk, image, mode in [
            ("control-plane", "control-plane", "java", "control-plane-jvm", "jvm"),
            ("function", "word-stats", "java", "java-word-stats", "jvm"),
            (
                "function",
                "word-stats",
                "javascript",
                "javascript-word-stats",
                "container",
            ),
        ]
    )
    distribution = RecipeDistribution(
        tmp_path / "report.json",
        "hash",
        "recipe-test",
        None,
        ("k8s-deployment-provider", "async-queue", "build-metadata"),
        components,
    )
    scenario = ScenarioConfig(
        workflow="loadtest",
        backend="k8s",
        loadProfile="comparison",
        controlPlaneVariant="jvm",
        functions=["word-stats-java", "word-stats-javascript"],
    )
    events = []

    class Executor:
        def __init__(self):
            self.k6_calls = []
            self.calls = []

        def binding_key(self, role):
            return role

        def run(self, task, **kwargs):
            argv = task.argv
            self.calls.append(argv)
            if argv[0] == "k6":
                self.k6_calls.append(argv)
                stdout = ""
            elif "--format" in argv:
                stdout = json.dumps(
                    {
                        "digest": "sha256:" + "e" * 64
                        if failure == "registry"
                        else digest
                    }
                )
            elif "--raw" in argv:
                stdout = json.dumps({"config": {"digest": config_id}, "layers": []})
            elif argv[0] == "curl" and argv[-1].endswith("build-metadata"):
                stdout = json.dumps(
                    {
                        "modules": list(distribution.modules),
                        "build": {
                            "type": "jvm",
                            "variant": "wrong" if failure == "metadata" else "jvm",
                            "optimization": "c1",
                        },
                    }
                )
            elif argv[0] == "curl":
                stdout = (
                    'scheduler_active{strategy="shared-queue"} 1\n'
                    if failure == "scheduler"
                    else 'scheduler_active{strategy="per-function"} 1\n'
                )
            elif "deployment" in argv:
                stdout = json.dumps(
                    {"metadata": {"uid": "deployment"}, "spec": {"replicas": 1}}
                )
            elif "replicasets" in argv:
                stdout = json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "uid": "rs",
                                    "ownerReferences": [
                                        {"kind": "Deployment", "uid": "deployment"}
                                    ],
                                }
                            }
                        ]
                    }
                )
            elif "pods" in argv:
                # One Pod fixture per selected Deployment; all component images
                # are present so the real check must select the expected one.
                stdout = json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "ready",
                                    "uid": "pod",
                                    "ownerReferences": [
                                        {"kind": "ReplicaSet", "uid": "rs"}
                                    ],
                                },
                                "spec": {
                                    "nodeName": "node",
                                    "containers": [
                                        {"name": str(i), "image": c.image.reference}
                                        for i, c in enumerate(components)
                                    ],
                                },
                                "status": {
                                    "phase": "Running",
                                    "containerStatuses": [
                                        {
                                            "name": str(i),
                                            "ready": True,
                                            "imageID": (
                                                "docker-pullable://"
                                                f"{c.image.reference.rsplit(':', 1)[0]}"
                                                f"@{digest}"
                                            ),
                                        }
                                        for i, c in enumerate(components)
                                    ],
                                },
                            }
                        ]
                    }
                )
            elif "crictl" in argv:
                observed = "sha256:" + "f" * 64 if failure == "pod" else digest
                stdout = json.dumps(
                    {
                        "images": [
                            {
                                "id": config_id,
                                "repoTags": [c.image.reference],
                                "repoDigests": [
                                    c.image.reference.rsplit(":", 1)[0] + "@" + observed
                                ],
                            }
                            for c in components
                        ]
                    }
                )
            elif "node" in argv:
                stdout = json.dumps(
                    {
                        "status": {
                            "nodeInfo": {
                                "operatingSystem": "linux",
                                "architecture": "arm64",
                            }
                        }
                    }
                )
            else:
                pytest.fail(f"unexpected cell command: {argv}")
            return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)

    executor = Executor()
    ready = Resource(
        title="Ready platform",
        acquire=lambda inputs: events.append("ready"),
        release=lambda inputs, value: events.append("released"),
    )
    platform = Platform(
        endpoint="http://10.43.0.7:8080", resources=(ready,), functions=()
    )
    check = _prepared_checks(
        scenario,
        distribution,
        distribution,
        RoleBindings({"stack": executor}),
        tmp_path,
        nanofaas_root,
        None,
    )(platform)
    workflow = Workflow(workflow_id="cell")
    _ = workflow.add(
        CommandTask(title="k6", argv=("k6", "run"), executor=executor, role="stack"),
        requires=(check,),
    )
    if failure:
        with pytest.raises(
            (ValueError, RuntimeError),
            match=r"scheduler|manifest differs|metadata|registry digest",
        ):
            workflow.run()
        assert executor.k6_calls == []
    else:
        workflow.run()
        assert executor.k6_calls == [("k6", "run")]
        assert (tmp_path / "images/java/k8s-image-function-word-stats.json").is_file()
        assert (
            tmp_path / "images/javascript/k8s-image-function-word-stats.json"
        ).is_file()
    assert events == ["ready", "released"]
    if failure == "scheduler":
        assert (
            tmp_path / "scheduler-metrics.txt"
        ).read_text() == 'scheduler_active{strategy="shared-queue"} 1\n'
    if failure == "pod":
        assert (
            tmp_path / "images/control-plane/k8s-image-control-plane-control-plane.json"
        ).is_file()
