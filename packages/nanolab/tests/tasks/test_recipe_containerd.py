import json
from pathlib import Path

from sonata_engine import Resource, TaskInputs
from sonata_tasks.tasks.models import TaskResult
from sonata_tasks.testing import RecordingExecutor

from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.recipe import (
    RecipeComponent,
    RecipeDistribution,
    RecipeImage,
)
from nanolab.tasks.recipe_containerd import RecipeContainerdImageCheckTask


def test_recipe_containerd_writes_evidence_and_checks_published_digest(tmp_path: Path):
    reference = "127.0.0.1:5000/nanofaas/word-stats-java:recipe-a"
    digest = "sha256:" + "a" * 64
    distribution = RecipeDistribution(
        report=tmp_path / "distribution.json",
        recipe_sha256="recipe",
        tag="recipe-a",
        source=None,
        modules=("containerd-deployment-provider",),
        components=(
            RecipeComponent(
                kind="function",
                name="word-stats",
                sdk="java",
                mode="jvm",
                image=RecipeImage(reference, "sha256:local", "published", digest),
                variant=None,
                optimization=None,
            ),
        ),
    )
    resource = Resource(
        title="distribution",
        acquire=lambda _inputs: distribution,
        release=lambda _inputs, _value: None,
    )
    executor = RecordingExecutor(
        results=[
            TaskResult(
                task_id="owned",
                status="passed",
                return_code=0,
                stdout=json.dumps({"ID": "owned-1", "Image": reference}),
            ),
            TaskResult(
                task_id="image",
                status="passed",
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "sha256:different-local-id",
                            "RepoDigests": [
                                "127.0.0.1:5000/nanofaas/word-stats-java@" + digest
                            ],
                        }
                    ]
                ),
            ),
        ]
    )
    task = RecipeContainerdImageCheckTask(
        resource,
        function=("word-stats-java", "word-stats", "java"),
        run=RootlessRun("run123", Path("/repo"), Path("/session.sh")),
        executor=executor,
        role="stack",
        run_dir=tmp_path,
    )
    inputs = TaskInputs._for_resources({resource: distribution}, {resource})

    task.run(inputs)

    assert (tmp_path / "containerd-word-stats-java-image.json").is_file()
    evidence = json.loads(
        (tmp_path / "containerd-word-stats-java-image-identity.json").read_text()
    )
    assert evidence["RepoDigests"] == [
        "127.0.0.1:5000/nanofaas/word-stats-java@" + digest
    ]
    other_target = RecipeContainerdImageCheckTask(
        resource,
        function=("word-stats-java", "word-stats", "java"),
        run=RootlessRun("run123", Path("/repo"), Path("/session.sh")),
        executor=RecordingExecutor(target_key="other-vm"),
        role="stack",
        run_dir=tmp_path,
    )
    assert task._fingerprint_payload() != other_target._fingerprint_payload()
