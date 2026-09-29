from __future__ import annotations

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.models import TaskResult

from nanolab.tasks.recipe import RecipeImage
from nanolab.tasks.recipe_kubernetes import (
    RuntimeImageIdentity,
    _owned_pods,
    _pod_image_digest,
    _select_manifest_digest,
    minikube_target_resource,
    recipe_namespace_resource,
    verify_runtime_image,
)


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
