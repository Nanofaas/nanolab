from __future__ import annotations

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.models import TaskResult
from sonata_tasks.minikube import MinikubeTarget

from nanolab.tasks.recipes.kubernetes import (
    RuntimeImageIdentity,
    _node_images,
    _owned_pods,
    _pod_image_digest,
    _select_manifest_digest,
    minikube_target_resource,
    recipe_namespace_resource,
    verify_runtime_image,
)
from nanolab.tasks.recipes.workflow import RecipeImage


@pytest.mark.parametrize("response", ["{}", '{"images": null}', '{"images": {}}'])
def test_missing_or_malformed_cri_inventory_is_not_an_empty_node(response):
    executor = FakeExecutor(
        {
            (
                "minikube",
                "-p",
                "minikube",
                "ssh",
                "--node",
                "node",
                "--",
                "sudo",
                "crictl",
                "images",
                "-o",
                "json",
            ): response,
        }
    )
    target = MinikubeTarget(profile="minikube", context="minikube", nodes=("node",))
    with pytest.raises(ValueError, match="CRI image list is malformed"):
        _node_images(executor, TaskInputs.empty(), target, "node")


class FakeExecutor:
    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task, *, dry_run: bool = False) -> TaskResult:
        self.calls.append(tuple(task.argv))
        return TaskResult(
            task_id="",
            status="passed",
            return_code=0,
            stdout=self.responses[tuple(task.argv)],
        )


def test_built_image_requires_config_identity() -> None:
    expected = RecipeImage("registry/example:run", "sha256:config", "built", None)
    verify_runtime_image(expected, RuntimeImageIdentity("sha256:config", ()))
    with pytest.raises(ValueError, match="config"):
        verify_runtime_image(expected, RuntimeImageIdentity("sha256:other", ()))


def test_published_manifest_and_config_are_distinct_requirements() -> None:
    expected = RecipeImage(
        "registry/example:run", "sha256:config", "published", "sha256:manifest"
    )
    verify_runtime_image(
        expected,
        RuntimeImageIdentity("sha256:config", ("sha256:manifest",)),
        registry_config_digest="sha256:config",
    )
    with pytest.raises(ValueError, match="manifest"):
        verify_runtime_image(
            expected,
            RuntimeImageIdentity("sha256:config", ("sha256:other",)),
            registry_config_digest="sha256:config",
        )


def test_published_index_resolves_platform_config() -> None:
    expected = RecipeImage(
        "registry/example:run", "sha256:index", "published", "sha256:index"
    )
    actual = RuntimeImageIdentity("sha256:config", ("sha256:index",))
    verify_runtime_image(expected, actual, registry_config_digest="sha256:config")
    with pytest.raises(ValueError, match="config"):
        verify_runtime_image(expected, actual, registry_config_digest="sha256:other")


def test_published_index_selects_only_node_platform() -> None:
    index = {
        "manifests": [
            {
                "digest": "sha256:arm64",
                "platform": {"os": "linux", "architecture": "arm64"},
            },
            {
                "digest": "sha256:attestation",
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ]
    }
    assert _select_manifest_digest(index, "linux", "arm64") == "sha256:arm64"
    with pytest.raises(ValueError, match="platform"):
        _select_manifest_digest(index, "linux", "amd64")


def test_unknown_runtime_identity_fails_closed() -> None:
    expected = RecipeImage("registry/example:run", "sha256:config", "built", None)
    with pytest.raises(ValueError, match="config"):
        verify_runtime_image(expected, RuntimeImageIdentity("", ()))


@pytest.mark.parametrize(
    ("image_id", "digest"),
    [
        ("sha256:config", "sha256:config"),
        ("containerd://sha256:config", "sha256:config"),
        ("docker-pullable://registry/example@sha256:manifest", "sha256:manifest"),
        ("registry/example@sha256:manifest", "sha256:manifest"),
    ],
)
def test_pod_image_id_resolves_known_runtime_forms(image_id: str, digest: str) -> None:
    assert _pod_image_digest(image_id) == digest


def test_pod_image_id_rejects_unknown_form() -> None:
    with pytest.raises(ValueError, match="imageID"):
        _pod_image_digest("opaque-id")


def test_non_minikube_context_fails_before_build(monkeypatch) -> None:
    monkeypatch.setattr(
        "sonata_tasks.minikube.shutil.which", lambda _name: "/usr/bin/tool"
    )
    executor = FakeExecutor(
        {
            ("kubectl", "config", "current-context"): "other-cluster\n",
            ("minikube", "profile", "list", "-o", "json"): '{"valid":[]}',
        }
    )
    resource = minikube_target_resource(executor=executor)

    with pytest.raises(RuntimeError, match="not an active Minikube"):
        resource.acquire(TaskInputs.empty())
    assert all("build" not in argv for argv in executor.calls)


def test_namespace_collision_never_deletes_existing_namespace() -> None:
    executor = FakeExecutor(
        {
            (
                "kubectl",
                "get",
                "namespaces",
                "-o",
                "json",
            ): '{"items":[{"metadata":{"name":"nanofaas-recipe-existing"}}]}',
        }
    )
    resource = recipe_namespace_resource(
        namespace="nanofaas-recipe-existing",
        executor=executor,
        role="stack",
        target=None,
        requires=(),
    )

    with pytest.raises(RuntimeError, match="existing namespace"):
        resource.acquire(TaskInputs.empty())
    assert len(executor.calls) == 1


def test_pod_selection_follows_deployment_ownership() -> None:
    def owned(name: str, kind: str, uid: str, own_uid: str) -> dict:
        return {
            "metadata": {
                "name": name,
                "uid": uid,
                "ownerReferences": [{"kind": kind, "uid": own_uid}],
            }
        }

    deployment = {"metadata": {"uid": "deployment-1"}}
    replica_sets = [
        owned("rs-1", "Deployment", "rs-1", "deployment-1"),
        owned("rs-2", "Deployment", "rs-2", "other"),
    ]
    pods = [
        owned("owned", "ReplicaSet", "pod-1", "rs-1"),
        owned("unrelated", "ReplicaSet", "pod-2", "rs-2"),
    ]

    selected = _owned_pods(deployment, replica_sets, pods)
    assert [pod["metadata"]["name"] for pod in selected] == ["owned"]


@pytest.mark.parametrize("fault", [None, "foreign", "load", "release"])
def test_cli_recipe_import_owns_only_recipe_images_without_queue_probe(tmp_path, fault):
    from pathlib import Path

    from sonata_engine import Resource

    from nanolab.tasks.recipes.kubernetes import minikube_images_resource
    from nanolab.tasks.recipes.workflow import RecipeComponent, RecipeDistribution

    reference = "127.0.0.1:5000/nanofaas/control-plane:unique-cli"
    image = RecipeImage(reference, "sha256:" + "a" * 64, "built", None)
    value = RecipeDistribution(
        Path("report.json"),
        "recipe-sha",
        "unique-cli",
        None,
        ("build-metadata",),
        (
            RecipeComponent(
                "control-plane", "control-plane", "java", "jvm", image, "cli", "c2"
            ),
        ),
    )
    target_value = MinikubeTarget("owned", "owned", ("node",))
    target = Resource(
        title="target",
        acquire=lambda _inputs: target_value,
        release=lambda *_args: None,
    )
    distribution = Resource(
        title="distribution", acquire=lambda _inputs: value, release=lambda *_args: None
    )

    class ImageBoundary:
        def __init__(self):
            self.loaded = fault == "foreign"
            self.calls = []

        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            import json

            self.calls.append(task.argv)
            code = 0
            if task.argv[:3] == ("docker", "image", "inspect"):
                body = [{"Id": image.id}]
            elif "ssh" in task.argv:
                body = {
                    "images": [{"id": image.id, "repoTags": [reference]}]
                    if self.loaded
                    else []
                }
            elif "load" in task.argv:
                assert task.argv[-1] == reference
                self.loaded = True
                code = 1 if fault == "load" else 0
                body = {}
            elif "rm" in task.argv:
                assert task.argv[-1] == reference
                self.loaded = False
                code = 1 if fault == "release" else 0
                body = {}
            else:
                raise AssertionError(task.argv)
            return TaskResult(
                task_id=task.task_id,
                status="failed" if code else "passed",
                return_code=code,
                stdout=json.dumps(body),
                stderr="image command failed" if code else "",
            )

    executor = ImageBoundary()
    resource = minikube_images_resource(
        target=target,
        distribution=distribution,
        executor=executor,
        run_dir=tmp_path / "evidence",
    )
    assert resource.requires == (target, distribution)
    inputs = TaskInputs._for_resources(
        {target: target_value, distribution: value}, {target, distribution}
    )
    if fault in ("foreign", "load"):
        with pytest.raises(RuntimeError):
            resource.acquire(inputs)
        if fault == "foreign":
            assert not any("rm" in argv for argv in executor.calls)
        else:
            assert any("rm" in argv for argv in executor.calls)
    else:
        assert resource.acquire(inputs) == (reference,)
        if fault == "release":
            with pytest.raises(RuntimeError, match="image command failed"):
                resource.release(inputs, (reference,))
        else:
            resource.release(inputs, (reference,))
            assert executor.loaded is False
