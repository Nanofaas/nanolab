from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.models import TaskResult

from nanolab.config.contract import ContractConfig
from nanolab.images.plan import ImageArchitecture, ImageCell, ImageTarget
from nanolab.workspace.recipe import RecipeRun
from tests.functions.test_contracts import frozen_fixture as frozen_fixture

EMPTY = TaskInputs._for_resources({}, set())
ARCH: ImageArchitecture = "arm64" if platform.machine() == "aarch64" else "amd64"


class DockerBoundary:
    """Model only Docker/Gradle commands; real ownership/build assembly stays active."""

    def __init__(self, failure=None):
        self.failure = failure
        self.commands = []
        self.objects = {}
        self.builder = ""
        self.node = ""
        self.remove_calls = []

    def binding_key(self, role):
        return role

    def run(self, task, *, dry_run=False):
        config = json.loads(task.argv[-1])
        argv = tuple(config["argv"])
        self.commands.append(config)
        stdout, code = self.dispatch(argv, config)
        Path(config["log_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(config["log_path"]).write_text(stdout)
        meta = {
            "returncode": code,
            "reaped": True,
            "forced_stop": False,
            "cancelled": False,
            "timed_out": False,
            "quota_exceeded": False,
            "errors": [],
        }
        return TaskResult(
            task_id=task.task_id,
            status="passed",
            return_code=0,
            stdout=json.dumps(meta),
        )

    def dispatch(self, argv, config):
        if argv[:3] == ("docker", "buildx", "ls"):
            return self.builder, 0
        if argv[:3] == ("docker", "buildx", "create"):
            self.builder = argv[argv.index("--name") + 1]
            self.node = argv[argv.index("--node") + 1]
            self.objects[f"buildx_buildkit_{self.node}"] = {
                "Id": "builder-container",
                "HostConfig": {
                    "Memory": 17179869184,
                    "CpuQuota": 400000,
                    "CpuPeriod": 100000,
                },
            }
            if self.failure == "limits":
                self.objects[f"buildx_buildkit_{self.node}"]["HostConfig"]["Memory"] = (
                    1024
                )
            return self.builder, 0
        if argv[:3] == ("docker", "buildx", "inspect"):
            if not self.builder:
                return "no builder found", 1
            return (
                f"Name: {self.builder}\nDriver: docker-container\nNodes:\n"
                f"Name: {self.node}\nPlatforms: linux/{ARCH}\n",
                0,
            )
        if argv[:3] == ("docker", "buildx", "rm"):
            self.builder = ""
            self.objects.pop(f"buildx_buildkit_{self.node}", None)
            return "", 0
        if argv[:3] == ("docker", "image", "inspect"):
            value = self.objects.get(argv[-1]) or next(
                (v for v in self.objects.values() if v["Id"] == argv[-1]), None
            )
            return (json.dumps([value]), 0) if value else ("No such image", 1)
        if argv[:2] == ("docker", "inspect") or argv[:3] == (
            "docker",
            "network",
            "inspect",
        ):
            value = self.objects.get(argv[-1]) or next(
                (v for v in self.objects.values() if v["Id"] == argv[-1]), None
            )
            return (json.dumps([value]), 0) if value else ("No such object", 1)
        if argv[:3] == ("docker", "network", "create") or argv[:2] == (
            "docker",
            "create",
        ):
            name = argv[-1] if argv[1] == "network" else argv[argv.index("--name") + 1]
            label = argv[argv.index("--label") + 1].split("=", 1)[1]
            value = {
                "Id": name,
                "Labels": {"nanolab.contract.owner": label},
                "Config": {"Labels": {"nanolab.contract.owner": label}},
                "Image": "image-id",
                "HostConfig": {"PortBindings": {}},
                "State": {"ExitCode": 0},
            }
            self.objects[name] = value
            if self.failure == "create-after":
                return "created then failed", 1
            return name, 0
        if argv[:2] == ("docker", "start"):
            return ("start failed", 1) if self.failure == "start" else (argv[-1], 0)
        if argv[:2] == ("docker", "stop"):
            return "", 0
        if argv[:2] == ("docker", "logs"):
            return "ready", 0
        if argv[:2] == ("docker", "cp"):
            header = bytearray(64)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = (62 if ARCH == "amd64" else 183).to_bytes(2, "little")
            Path(argv[-1]).write_bytes(header)
            return "", 0
        if argv[0] == "./gradlew":
            return "built JVM", 0
        if argv[:2] == ("docker", "rm") or argv[:3] in (
            ("docker", "network", "rm"),
            ("docker", "image", "rm"),
        ):
            self.remove_calls.append(argv[-1])
            if self.failure != "remove-lies":
                for key in list(self.objects):
                    if key == argv[-1] or self.objects[key]["Id"] == argv[-1]:
                        self.objects.pop(key)
            return "", 0
        if argv[:3] == ("docker", "buildx", "bake"):
            bake = json.loads(Path(argv[argv.index("--file") + 1]).read_text())
            for target in bake["target"].values():
                ref = target["tags"][0]
                self.objects[ref] = {
                    "Id": f"image-{len(self.objects)}",
                    "Architecture": ARCH,
                    "Config": {
                        "Entrypoint": ["/app/function"],
                        "Labels": target["labels"],
                    },
                }
            if self.failure == "source-change":
                (Path(config["cwd"]) / "build.gradle").write_text("version = '9.0.0'\n")
            return "built", 0
        if argv[:3] == ("docker", "buildx", "build"):
            tag = argv[argv.index("--tag") + 1]
            owner = argv[argv.index("--label") + 1].split("=", 1)[1]
            self.objects[tag] = {
                "Id": "helper-id",
                "Architecture": ARCH,
                "Config": {
                    "Labels": {"nanolab.contract.owner": owner},
                    "Entrypoint": ["python3"],
                },
            }
            return "built helper", 0
        raise AssertionError(argv)


def artifact(tmp_path, boundary):
    from nanolab.tasks.validation.contract_resources import (
        ContractImage,
        ContractRuntime,
        artifact_container_resource,
    )

    cell = ImageCell(
        ImageTarget("python-word-stats", ("default",), Path("Dockerfile"), Path()),
        ARCH,
        "default",
        "tag",
        "test/image",
    )
    image = ContractImage(cell, "image-id", ARCH, ("/app/function",), None)
    runtime = ContractRuntime(
        "private-network", "capture-id", "probe-image", "owner-test"
    )
    return artifact_container_resource(
        image,
        runtime,
        mode="sdk",
        execution_id=None,
        payload=None,
        executor=boundary,
        run_dir=tmp_path,
    )


def test_replacement_container_is_preserved(tmp_path):
    boundary = DockerBoundary()
    resource = artifact(tmp_path, boundary)
    value = resource.acquire(EMPTY)
    boundary.objects[value]["Config"]["Labels"]["nanolab.contract.owner"] = "operator"
    with pytest.raises(RuntimeError, match="ownership"):
        resource.release(EMPTY, value)
    assert value in boundary.objects
    assert value not in boundary.remove_calls


def test_partial_acquisition_compensates(tmp_path):
    boundary = DockerBoundary("start")
    with pytest.raises(RuntimeError, match="failed"):
        artifact(tmp_path, boundary).acquire(EMPTY)
    assert not boundary.objects
    assert list((tmp_path / "cleanup").glob("*.json"))


def test_partial_create_compensates(tmp_path):
    boundary = DockerBoundary("create-after")
    with pytest.raises(RuntimeError, match="failed"):
        artifact(tmp_path, boundary).acquire(EMPTY)
    assert not boundary.objects


def test_absence_check_does_not_trust_remove_exit(tmp_path):
    boundary = DockerBoundary("remove-lies")
    resource = artifact(tmp_path, boundary)
    value = resource.acquire(EMPTY)
    with pytest.raises(RuntimeError, match="absence"):
        resource.release(EMPTY, value)
    assert value in boundary.objects


def image_resource(frozen_fixture, tmp_path, boundary):
    from nanolab.tasks.validation.contract_resources import contract_images_resource

    value = RecipeRun(
        frozen_fixture, tmp_path / "scenario.yaml", tmp_path / "dist", "test"
    )
    source = Resource(title="source", acquire=lambda _: value, release=lambda *_: None)
    inputs = TaskInputs._for_resources({source: value}, {source})
    resource = contract_images_resource(
        source,
        selectors=("word-stats",),
        settings=ContractConfig(),
        executor=boundary,
        run_dir=tmp_path / "attempt",
        tag="test",
    )
    return resource, inputs


def test_bake_uses_frozen_source_and_load(frozen_fixture, tmp_path):
    boundary = DockerBoundary()
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    images = resource.acquire(inputs)
    assert images[0].cell.target.name == "python-word-stats"
    baked = [
        command
        for command in boundary.commands
        if command["argv"][:3] == ["docker", "buildx", "bake"]
    ]
    assert len(baked) == 1
    assert "--load" in baked[0]["argv"]
    assert baked[0]["cwd"] == str(frozen_fixture)
    assert baked[0]["timeout_s"] == 2700
    resource.release(inputs, images)
    assert not boundary.objects


def test_changed_source_fails(frozen_fixture, tmp_path):
    boundary = DockerBoundary("source-change")
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    with pytest.raises(RuntimeError, match="source"):
        resource.acquire(inputs)
    assert not boundary.objects


def test_tag_collision_preserved(frozen_fixture, tmp_path):
    boundary = DockerBoundary()
    tag = f"nanolab-contract/test/python-word-stats:v1.0.0-{ARCH}"
    boundary.objects[tag] = {"Id": "operator-image", "Config": {"Labels": {}}}
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    with pytest.raises(RuntimeError, match="exists"):
        resource.acquire(inputs)
    assert boundary.objects[tag]["Id"] == "operator-image"
    assert tag not in boundary.remove_calls


def test_wrong_elf_or_changed_image_fails():
    from nanolab.tasks.validation.contract_resources import validate_native_binary

    with pytest.raises(ValueError, match="ELF"):
        validate_native_binary(b"#!/bin/sh\njava -jar app.jar", ARCH)


def test_private_builder_limits_verified(frozen_fixture, tmp_path):
    boundary = DockerBoundary("limits")
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    with pytest.raises(RuntimeError, match="limits"):
        resource.acquire(inputs)
    assert not boundary.objects


def test_each_image_build_has_deadline(frozen_fixture, tmp_path):
    boundary = DockerBoundary()
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    images = resource.acquire(inputs)
    builds = [
        command
        for command in boundary.commands
        if command["argv"][:3] == ["docker", "buildx", "bake"]
    ]
    assert all(command["timeout_s"] == 2700 for command in builds)
    resource.release(inputs, images)


def test_jvm_prerequisites_run_once(frozen_fixture, tmp_path):
    java = frozen_fixture / "functions/java/word-stats"
    java.mkdir(parents=True)
    (java / "Dockerfile").write_text("FROM scratch\n")
    boundary = DockerBoundary()
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    images = resource.acquire(inputs)
    assert len(images) == 3
    assert (
        len(
            [
                command
                for command in boundary.commands
                if command["argv"][0] == "./gradlew"
            ]
        )
        == 1
    )
    resource.release(inputs, images)


def test_java_lite_does_not_claim_unused_native_flags(frozen_fixture, tmp_path):
    lite = frozen_fixture / "functions/java/word-stats-lite"
    lite.mkdir(parents=True)
    (lite / "Dockerfile").write_text("FROM scratch\n")
    boundary = DockerBoundary()
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    images = resource.acquire(inputs)
    assert len(images) == 2
    target = next(
        cell for cell in images if cell.cell.target.name == "java-lite-word-stats"
    )
    assert target.native_sha256
    bake_files = list((tmp_path / "attempt/builds").glob("*.json"))
    baked = [json.loads(path.read_text()) for path in bake_files]
    lite_args = [
        item.get("args", {})
        for document in baked
        for name, item in document["target"].items()
        if name.startswith("java-lite")
    ]
    assert lite_args == [{}]
    resource.release(inputs, images)


def test_runtime_network_has_no_published_ports(tmp_path):
    boundary = DockerBoundary()
    resource = artifact(tmp_path, boundary)
    value = resource.acquire(EMPTY)
    assert boundary.objects[value]["HostConfig"]["PortBindings"] == {}
    assert all(
        "--privileged" not in command["argv"] and "--publish" not in command["argv"]
        for command in boundary.commands
    )
    resource.release(EMPTY, value)


def test_runtime_uses_private_builder_and_compensates(tmp_path):
    from nanolab.tasks.validation.contract_resources import contract_runtime_resource

    boundary = DockerBoundary("start")
    runtime = contract_runtime_resource(
        settings=ContractConfig(), executor=boundary, run_dir=tmp_path, tag="test"
    )
    with pytest.raises(RuntimeError, match="failed"):
        runtime.acquire(EMPTY)
    assert not boundary.objects
    builds = [
        item["argv"]
        for item in boundary.commands
        if item["argv"][:3] == ["docker", "buildx", "build"]
    ]
    assert builds[0][builds[0].index("--builder") + 1] == "nanolab-contract-test"
    assert "--load" in builds[0]


@pytest.mark.parametrize(
    ("command", "bound", "failure_key"),
    [
        ("import time; time.sleep(1)", 1000, "timed_out"),
        ("print('x' * 2000)", 100, "quota_exceeded"),
    ],
)
def test_real_owned_executor_bounds(tmp_path, command, bound, failure_key):
    from sonata_tasks.execution.adapters import HostCommandTaskExecutor
    from sonata_tasks.execution.models import CommandTaskSpec

    from nanolab.tasks.validation.contract_resources import ContractExecutor

    @dataclass(frozen=True)
    class HostResult:
        return_code: int
        stdout: str
        stderr: str

    class Host:
        def run(self, argv, /, *, cwd, env, dry_run):
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=env or None,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return HostResult(result.returncode, result.stdout, result.stderr)

    executor = ContractExecutor(
        HostCommandTaskExecutor(Host()),
        tmp_path,
        ContractConfig(request_seconds=0.1, log_bytes=bound),
    )
    with pytest.raises(RuntimeError, match="deadline/output/cleanup"):
        executor.run(
            CommandTaskSpec("bounded", "bounded", (sys.executable, "-c", command))
        )
    receipt = json.loads(next((tmp_path / "commands").glob("*.json")).read_text())
    assert receipt["result"][failure_key] is True
    assert receipt["result"]["reaped"] is True


def test_built_image_identity_bound_before_release(frozen_fixture, tmp_path):
    boundary = DockerBoundary()
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    images = resource.acquire(inputs)
    leases = list((tmp_path / "attempt" / "ownership").glob("image-*.json"))
    assert len(leases) == 1
    assert (
        json.loads(
            (tmp_path / "attempt" / "identity-bindings" / leases[0].name).read_text()
        )["identity"]
        == images[0].image_id
    )
    resource.release(inputs, images)


@pytest.mark.parametrize("kind", ["container", "network", "image"])
def test_replacement_after_inspection_is_preserved(tmp_path, kind):
    from nanolab.tasks.validation.contract_resources import (
        ContractExecutor,
        _register,
        _release_owned,
    )

    class Race(DockerBoundary):
        def dispatch(self, argv, config):
            inspecting = argv[:2] == ("docker", "inspect") or argv[:3] in (
                ("docker", "network", "inspect"),
                ("docker", "image", "inspect"),
            )
            if inspecting:
                value = next(
                    (
                        v
                        for key, v in self.objects.items()
                        if key == argv[-1] or v["Id"] == argv[-1]
                    ),
                    None,
                )
                return (json.dumps([value]), 0) if value else ("No such object", 1)
            removing = argv[:2] == ("docker", "rm") or argv[:3] in (
                ("docker", "network", "rm"),
                ("docker", "image", "rm"),
            )
            if removing:
                self.objects["old-id"] = self.objects.pop("leased")
                self.objects["leased"] = {
                    "Id": "operator-id",
                    "Labels": {},
                    "Config": {"Labels": {}},
                }
                value = self.objects.pop(argv[-1], None)
                if value:
                    for key in list(self.objects):
                        if self.objects[key]["Id"] == value["Id"]:
                            self.objects.pop(key)
                return "", 0
            return super().dispatch(argv, config)

    boundary = Race()
    labels = {"nanolab.contract.owner": "test"}
    boundary.objects["leased"] = {
        "Id": "old-id",
        "Labels": labels,
        "Config": {"Labels": labels},
    }
    lease = _register(tmp_path, kind, "leased", "test", "old-id")
    with pytest.raises(RuntimeError, match="absence"):
        _release_owned(
            ContractExecutor(boundary, tmp_path, ContractConfig()),
            EMPTY,
            lease,
            tmp_path,
        )
    assert boundary.objects["leased"]["Id"] == "operator-id"
    assert "old-id" not in boundary.objects


def test_renamed_original_is_still_removed(tmp_path):
    from nanolab.tasks.validation.contract_resources import (
        ContractExecutor,
        _register,
        _release_owned,
    )

    class Renamed(DockerBoundary):
        def dispatch(self, argv, config):
            if argv[:2] == ("docker", "inspect"):
                value = next(
                    (
                        v
                        for key, v in self.objects.items()
                        if key == argv[-1] or v["Id"] == argv[-1]
                    ),
                    None,
                )
                return (json.dumps([value]), 0) if value else ("No such object", 1)
            if argv[:2] == ("docker", "rm"):
                for key in list(self.objects):
                    if self.objects[key]["Id"] == argv[-1]:
                        self.objects.pop(key)
                return "", 0
            return super().dispatch(argv, config)

    boundary = Renamed()
    boundary.objects["renamed"] = {
        "Id": "old-id",
        "Config": {"Labels": {"nanolab.contract.owner": "test"}},
    }
    lease = _register(tmp_path, "container", "leased", "test", "old-id")
    _release_owned(
        ContractExecutor(boundary, tmp_path, ContractConfig()), EMPTY, lease, tmp_path
    )
    assert not boundary.objects


def test_native_bake_supplies_empty_maven_context(frozen_fixture, tmp_path):
    java = frozen_fixture / "functions/java/word-stats"
    java.mkdir(parents=True)
    (java / "Dockerfile").write_text("FROM scratch\n")
    boundary = DockerBoundary()
    resource, inputs = image_resource(frozen_fixture, tmp_path, boundary)
    images = resource.acquire(inputs)
    targets = [
        target
        for p in (tmp_path / "attempt/builds").glob("*.json")
        for target in json.loads(p.read_text())["target"].values()
        if target.get("args", {}).get("NATIVE_TASK")
    ]
    assert len(targets) == 1
    context = Path(targets[0]["contexts"]["containerd_maven_repo"])
    assert context.is_dir()
    assert not list(context.iterdir())
    native_commands = [
        command
        for command in boundary.commands
        if command["argv"][:3] == ["docker", "buildx", "bake"]
        and "contexts"
        in next(
            iter(
                json.loads(
                    Path(
                        command["argv"][command["argv"].index("--file") + 1]
                    ).read_text()
                )["target"].values()
            )
        )
    ]
    assert len(native_commands) == 1
    assert f"--allow=fs.read={context}" in native_commands[0]["argv"]
    resource.release(inputs, images)
