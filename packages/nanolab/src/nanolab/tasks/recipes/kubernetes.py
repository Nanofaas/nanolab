"""Kubernetes resources and image evidence for recipe validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.imagetools import (
    select_platform_manifest_digest as _select_manifest_digest,
)
from sonata_tasks.kubectl import (
    kubectl_port_forward_resource,
    owned_namespace_resource,
)
from sonata_tasks.kubectl import (
    owned_deployment_pods as _owned_pods,
)
from sonata_tasks.kubectl import (
    pod_image_digest as _pod_image_digest,
)
from sonata_tasks.minikube import MinikubeTarget
from sonata_tasks.minikube import minikube_target_resource as _minikube_target_resource
from sonata_tasks.vm.logged import run_remote_logged
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.tasks.deployment import LOCAL_REGISTRY
from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.recipes.remote import RemoteRecipeRun
from nanolab.tasks.recipes.workflow import RecipeDistribution, RecipeImage
from nanolab.tasks.vm.models import VmRequest
from nanolab.workspace.recipe import RecipeRun


@dataclass(frozen=True, slots=True)
class RuntimeImageIdentity:
    """Image identity resolved from the node runtime, not from a mutable tag."""

    config_digest: str
    manifest_digests: tuple[str, ...]


def verify_runtime_image(
    expected: RecipeImage,
    actual: RuntimeImageIdentity,
    *,
    registry_config_digest: str | None = None,
) -> None:
    """Relate the Pod's configuration to its built or published artifact."""
    if expected.digest is not None and expected.digest not in actual.manifest_digests:
        raise ValueError(
            f"Runtime image manifest differs for {expected.reference}: "
            f"{expected.digest!r} not in {actual.manifest_digests!r}"
        )
    expected_config = registry_config_digest if expected.digest else expected.id
    if not expected_config or actual.config_digest != expected_config:
        raise ValueError(
            f"Runtime image config differs for {expected.reference}: "
            f"{actual.config_digest!r} != {expected_config!r}"
        )
    if expected.digest is not None and expected.id not in (
        expected.digest,
        expected_config,
    ):
        raise ValueError(
            f"Recipe image id for {expected.reference} is neither the "
            "published index/manifest nor its platform config"
        )


def _command(
    executor: CommandTaskExecutor,
    inputs: TaskInputs,
    title: str,
    argv: tuple[str, ...],
    *,
    role: ExecutionRole = "host",
    cwd: Path | None = None,
) -> str:
    result = (
        CommandTask(
            title=title,
            argv=argv,
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
        )
        .run(inputs)
        .value
    )
    if result is None:
        raise RuntimeError(f"{title} returned no result")
    return result.stdout


def _json_command(
    executor: CommandTaskExecutor,
    inputs: TaskInputs,
    title: str,
    argv: tuple[str, ...],
    *,
    role: ExecutionRole = "host",
) -> dict[str, Any]:
    data = json.loads(_command(executor, inputs, title, argv, role=role))
    if not isinstance(data, dict):
        raise ValueError(f"{title} returned no JSON object")
    return data


def minikube_target_resource(
    *, executor: CommandTaskExecutor, kubeconfig: Path | None = None
) -> Resource[MinikubeTarget]:
    """Apply NanoLab's local tool and Docker-driver requirements."""
    return _minikube_target_resource(
        executor=executor,
        kubeconfig=kubeconfig,
        required_tools=("minikube", "docker", "kubectl", "helm", "k6"),
        driver="docker",
    )


def _node_images(
    executor: CommandTaskExecutor, inputs: TaskInputs, target: MinikubeTarget, node: str
) -> list[dict[str, Any]]:
    data = _json_command(
        executor,
        inputs,
        f"Inspect Minikube images on {node}",
        (
            "minikube",
            "-p",
            target.profile,
            "ssh",
            "--node",
            node,
            "--",
            "sudo",
            "crictl",
            "images",
            "-o",
            "json",
        ),
    )
    images = data.get("images")
    if not isinstance(images, list):
        raise ValueError("CRI image list is malformed")
    return images


def _image_rows(images: list[dict[str, Any]], reference: str) -> list[dict[str, Any]]:
    return [row for row in images if reference in row.get("repoTags", ())]


def minikube_images_resource(
    *,
    target: Resource[MinikubeTarget],
    distribution: Resource[RecipeDistribution],
    probe_image: Resource[str] | None = None,
    executor: CommandTaskExecutor,
    run_dir: Path,
) -> Resource[tuple[str, ...]]:
    """Import exactly the run's images and remove only imports we acquired."""
    acquired: list[str] = []

    def release(inputs: TaskInputs, _value: tuple[str, ...]) -> None:
        selected = inputs.resource(target)
        for reference in reversed(acquired):
            _command(
                executor,
                inputs,
                f"Remove run image {reference}",
                ("minikube", "-p", selected.profile, "image", "rm", reference),
            )

    def acquire(inputs: TaskInputs) -> tuple[str, ...]:
        selected = inputs.resource(target)
        report = inputs.resource(distribution)
        images = [component.image for component in report.components]
        references = [
            *(image.reference for image in images),
            *((inputs.resource(probe_image),) if probe_image is not None else ()),
        ]
        try:
            for reference in references:
                host = json.loads(
                    _command(
                        executor,
                        inputs,
                        f"Inspect built image {reference}",
                        ("docker", "image", "inspect", reference),
                    )
                )
                expected = next(
                    (image.id for image in images if image.reference == reference), None
                )
                if (
                    not isinstance(host, list)
                    or len(host) != 1
                    or (expected is not None and host[0].get("Id") != expected)
                ):
                    raise ValueError(f"Host image identity changed: {reference}")
            for reference in references:
                if any(
                    _image_rows(
                        _node_images(executor, inputs, selected, node), reference
                    )
                    for node in selected.nodes
                ):
                    raise RuntimeError(
                        f"Refusing to own existing Minikube image: {reference}"
                    )
                acquired.append(reference)
                _command(
                    executor,
                    inputs,
                    f"Load run image {reference}",
                    (
                        "minikube",
                        "-p",
                        selected.profile,
                        "image",
                        "load",
                        "--daemon",
                        reference,
                    ),
                )
                expected = next(
                    (image for image in images if image.reference == reference), None
                )
                if expected is not None:
                    for node in selected.nodes:
                        rows = _image_rows(
                            _node_images(executor, inputs, selected, node), reference
                        )
                        if len(rows) != 1:
                            raise ValueError(
                                f"Loaded image missing or ambiguous on {node}: "
                                f"{reference}"
                            )
                        verify_runtime_image(
                            expected, RuntimeImageIdentity(rows[0].get("id", ""), ())
                        )
        except Exception:
            release(inputs, ())
            raise
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "loaded-images.json").write_text(
            json.dumps(references, indent=2) + "\n"
        )
        return tuple(references)

    return Resource(
        title="Load recipe images into selected Minikube",
        acquire=acquire,
        release=release,
        requires=(
            target,
            distribution,
            *((probe_image,) if probe_image is not None else ()),
        ),
    )


def queue_probe_image_resource(
    *,
    run: Resource[RecipeRun],
    tag: str,
    executor: CommandTaskExecutor,
    remote_run: Resource[RemoteRecipeRun] | None = None,
    provider: VmCommandProvider | None = None,
    vm_request: VmRequest | None = None,
) -> Resource[str]:
    """Build the auxiliary warm-echo image from the same staged source."""
    image = f"{LOCAL_REGISTRY}/nanofaas/java-warm-echo:{tag}"
    build_argv = (
        "docker",
        "build",
        "-t",
        image,
        "-f",
        "services/java/warm-echo/Dockerfile",
        "services/java/warm-echo",
    )

    def acquire(inputs: TaskInputs) -> str:
        if remote_run is None:
            source = inputs.resource(run).source_dir
            _command(
                executor,
                inputs,
                "Build queue probe artifact",
                ("./gradlew", ":services:java:warm-echo:bootJar", "--no-daemon"),
                cwd=source,
            )
            _command(
                executor, inputs, "Build queue probe image", build_argv, cwd=source
            )
        else:
            if provider is None or vm_request is None:
                raise ValueError("Remote queue probe needs a VM provider")
            remote = inputs.resource(remote_run)
            run_remote_logged(
                provider,
                vm_request,
                (
                    ("./gradlew", ":services:java:warm-echo:bootJar", "--no-daemon"),
                    build_argv,
                    ("docker", "push", image),
                ),
                remote_dir=remote.source,
                remote_log=remote.root / "queue-probe.log",
                local_log=remote.local.output_dir.parent / "queue-probe.log",
            )
        return image

    return Resource(
        title="Build staged Kubernetes queue probe",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=(run, *((remote_run,) if remote_run is not None else ())),
    )


def recipe_namespace_resource(
    *,
    namespace: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    target: Resource[MinikubeTarget] | None,
    requires: tuple[Resource[Any], ...],
) -> Resource[str]:
    """Own a recipe namespace in the selected cluster."""
    return replace(
        owned_namespace_resource(
            namespace,
            executor=executor,
            role=role,
            context=(lambda inputs: inputs.resource(target).context)
            if target
            else None,
            requires=(*requires, *((target,) if target is not None else ())),
        ),
        title=f"Own recipe namespace {namespace}",
    )


def kubernetes_api_endpoint_resource(
    *,
    release: Resource[str],
    namespace: str,
    target: Resource[MinikubeTarget],
    executor: CommandTaskExecutor,
    run_dir: Path,
) -> Resource[str]:
    """Forward the NanoFaaS API from the selected Minikube profile."""
    del executor
    return replace(
        kubectl_port_forward_resource(
            namespace=namespace,
            resource="service/control-plane",
            remote_port=8080,
            log_path=run_dir / "api-port-forward.log",
            context=lambda inputs: inputs.resource(target).context,
            requires=(release, target),
        ),
        title="Forward recipe API from Minikube to host loopback",
    )


class RecipeKubernetesImageCheckTask(Task[None]):
    """Prove each ready Pod uses the configuration and manifest from the report."""

    def __init__(
        self,
        distribution: Resource[RecipeDistribution],
        *,
        namespace: str,
        deployment: str,
        component: tuple[str, str, str],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        run_dir: Path,
        target: Resource[MinikubeTarget] | None = None,
    ) -> None:
        """Select one Deployment and its expected recipe component."""
        self.title = f"Verify Kubernetes recipe image {deployment}"
        self.distribution = distribution
        self.namespace = namespace
        self.deployment = deployment
        self.component = component
        self.executor = executor
        self.role: ExecutionRole = role
        self.run_dir = run_dir
        self.target = target

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Inspect all owned, ready Pods and retain raw runtime evidence."""
        report = inputs.resource(self.distribution)
        kind, name, sdk = self.component
        expected = (
            report.control_plane().image
            if kind == "control-plane"
            else report.function(name, sdk).image
        )
        prefix = (
            ("kubectl", "--context", inputs.resource(self.target).context)
            if self.target is not None
            else ("kubectl",)
        )

        def fetch(title: str, *args: str) -> dict[str, Any]:
            return _json_command(
                self.executor,
                inputs,
                title,
                (*prefix, "-n", self.namespace, "get", *args, "-o", "json"),
                role=self.role,
            )

        deployment = fetch("Read recipe Deployment", "deployment", self.deployment)
        replica_sets = fetch("Read recipe ReplicaSets", "replicasets").get("items", [])
        pods = fetch("Read recipe Pods", "pods").get("items", [])
        owned = _owned_pods(deployment, replica_sets, pods)
        evidence: dict[str, Any] = {
            "deployment": deployment,
            "replicaSets": replica_sets,
            "pods": owned,
            "nodeImages": {},
            "registryDescriptors": {},
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"k8s-image-{kind}-{name}.json"
        try:
            desired = deployment.get("spec", {}).get("replicas", 1)
            if not owned or len(owned) < desired:
                raise ValueError(f"Deployment {self.deployment} has too few owned Pods")
            for pod in owned:
                pod_name = pod.get("metadata", {}).get("name", "<unknown>")
                if pod.get("status", {}).get("phase") != "Running":
                    raise ValueError(f"Pod {pod_name} is not running")
                node = pod.get("spec", {}).get("nodeName")
                if not isinstance(node, str) or not node:
                    raise ValueError(f"Pod {pod_name} has no node")
                containers = [
                    row
                    for row in pod.get("spec", {}).get("containers", [])
                    if row.get("image") == expected.reference
                ]
                if not containers:
                    raise ValueError(
                        f"Pod {pod_name} does not declare {expected.reference}"
                    )
                statuses = {
                    row.get("name"): row
                    for row in pod.get("status", {}).get("containerStatuses", [])
                }
                if self.target is not None:
                    selected = inputs.resource(self.target)
                    if node not in selected.nodes:
                        raise ValueError(
                            f"Pod {pod_name} runs on an unknown Minikube node"
                        )
                    images = _node_images(self.executor, inputs, selected, node)
                else:
                    images = _json_command(
                        self.executor,
                        inputs,
                        f"Inspect k3s images on {node}",
                        ("sudo", "k3s", "crictl", "images", "-o", "json"),
                        role=self.role,
                    ).get("images", [])
                evidence["nodeImages"][node] = images
                rows = _image_rows(images, expected.reference)
                if len(rows) != 1:
                    raise ValueError(
                        f"CRI image for {expected.reference} is missing "
                        f"or ambiguous on {node}"
                    )
                row = rows[0]
                digests = tuple(
                    value.rsplit("@", 1)[-1] for value in row.get("repoDigests", [])
                )
                registry_config = None
                if expected.digest is not None:
                    node_info = fetch("Read recipe Pod node", "node", node)
                    node_runtime = node_info.get("status", {}).get("nodeInfo", {})
                    os = node_runtime.get("operatingSystem")
                    arch = node_runtime.get("architecture")
                    if not isinstance(os, str) or not isinstance(arch, str):
                        raise ValueError(f"Node {node} has no runtime platform")
                    repository = expected.reference.rsplit(":", 1)[0]
                    image = f"{repository}@{expected.digest}"
                    index = _json_command(
                        self.executor,
                        inputs,
                        "Inspect published recipe image",
                        ("docker", "buildx", "imagetools", "inspect", "--raw", image),
                        role=self.role,
                    )
                    descriptor = index
                    if "config" not in index:
                        manifest_digest = _select_manifest_digest(index, os, arch)
                        descriptor = _json_command(
                            self.executor,
                            inputs,
                            "Inspect published recipe platform manifest",
                            (
                                "docker",
                                "buildx",
                                "imagetools",
                                "inspect",
                                "--raw",
                                f"{repository}@{manifest_digest}",
                            ),
                            role=self.role,
                        )
                    registry_config = descriptor.get("config", {}).get("digest")
                    evidence["registryDescriptors"][node] = {
                        "index": index,
                        "manifest": descriptor,
                    }
                verify_runtime_image(
                    expected,
                    RuntimeImageIdentity(row.get("id", ""), digests),
                    registry_config_digest=registry_config,
                )
                for container in containers:
                    status = statuses.get(container.get("name"), {})
                    actual_id = status.get("imageID", "")
                    actual_digest = _pod_image_digest(actual_id)
                    if not status.get("ready") or actual_digest not in (
                        row.get("id"),
                        *digests,
                    ):
                        raise ValueError(
                            f"Pod {pod_name} container imageID does not "
                            "match CRI evidence"
                        )
        finally:
            path.write_text(json.dumps(evidence, indent=2) + "\n")
        return TaskOutcome()

    def _fingerprint_payload(self) -> object:
        return {
            "distribution": self.distribution.title,
            "namespace": self.namespace,
            "deployment": self.deployment,
            "component": self.component,
        }
