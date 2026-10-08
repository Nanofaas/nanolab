"""Paired contracts against independent state at the external command boundary.

Synthetic ELF headers qualify only the parser; installed/native execution is a
separate live gate. No contract factory or verifier is replaced in ordinary runs.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml
from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.models import TaskResult
from sonata_tasks.minikube import MinikubeTarget

from nanolab.tasks.recipes.workflow import (
    RecipeComponent,
    RecipeDistribution,
    RecipeImage,
)
from nanolab.workspace.recipe import RecipeRun

OUTPUT = {
    "wordCount": 5,
    "uniqueWords": 3,
    "topWords": [
        {"word": "alpha", "count": 2},
        {"word": "beta", "count": 2},
        {"word": "gamma", "count": 1},
    ],
    "averageWordLength": 4.6,
}
CP_IMAGE = RecipeImage(
    "127.0.0.1:5000/nanofaas/control-plane:cli-test",
    "sha256:" + "a" * 64,
    "built",
    None,
)
FN_IMAGE = RecipeImage(
    "127.0.0.1:5000/nanofaas/word-stats-java:cli-test",
    "sha256:" + "b" * 64,
    "built",
    None,
)
MODULES = ("k8s-deployment-provider", "build-metadata", "runtime-config")


def test_cli_deadline_works_through_the_product_host_adapter(tmp_path):
    from types import SimpleNamespace

    from sonata_tasks.command import CommandTask
    from sonata_tasks.execution.adapters import HostCommandTaskExecutor
    from sonata_tasks.execution.models import CommandOptions

    from nanolab.tasks.validation.cli_parity import _EvidenceExecutor

    class HostRunner:
        def run(self, argv, *, cwd, env, dry_run):
            assert argv[:3] == ["timeout", "--kill-after=5s", "45s"]
            result = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, check=True
            )
            return SimpleNamespace(
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    with (tmp_path / "commands.jsonl").open("w") as stream:
        task = CommandTask(
            title="Bounded host command",
            argv=("printf", "ready"),
            executor=_EvidenceExecutor(HostCommandTaskExecutor(HostRunner()), stream),
            options=CommandOptions(timeout_seconds=45),
        )
        assert task.run(TaskInputs({}, frozenset())).value.stdout == "ready"


class PlatformBoundary:
    def __init__(self, fault=None, baseline=1000000):
        self.fault, self.rate, self.revision = fault, baseline, 0
        self.function = None
        self.replicas = 1
        self.uid = "deployment-1"
        self.container_id = "containerd://" + "c" * 64
        self.mode = "jvm"
        self.invoked = False
        self.specs = []
        self.events = []

    def binding_key(self, role):
        return role

    def run(self, task, *, dry_run=False):
        self.specs.append(task)
        argv = task.argv[3:] if task.argv[0] == "timeout" else task.argv
        joined = " ".join(argv)
        if "nativeCompile/nanofaas-cli" in joined:
            self.mode = "native"
        elif "install/nanofaas-cli" in joined:
            self.mode = "jvm"
        self.events.append((self.mode, task.summary, self.rate, self.function is None))
        code, err = 0, ""
        if argv[0] == "curl":
            data = {
                "modules": list(MODULES),
                "build": {
                    "type": "jvm",
                    "variant": "recipe-v2-cli-k8s-jvm",
                    "optimization": "c2",
                },
            }
        elif argv[0] == "minikube":
            data = {
                "images": [
                    {
                        "id": CP_IMAGE.id,
                        "repoTags": [CP_IMAGE.reference],
                        "repoDigests": [],
                    }
                ]
            }
        elif "logs" in argv:
            data = "Started control-plane\n"
            if self.invoked and self.fault == "log-marker":
                data += "MissingReflectionRegistrationError\n"
            if self.invoked and self.fault == "oversize-log":
                data = "x" * (8 * 1024 * 1024 + 1)
        elif argv[0] == "kubectl" and "get" in argv:
            index = argv.index("get")
            kind = argv[index + 1]
            if kind == "deployment":
                data = {
                    "metadata": {"uid": self.uid, "name": "nanofaas-control-plane"},
                    "spec": {"replicas": 1},
                }
            elif kind == "replicasets":
                data = {
                    "items": [
                        {
                            "metadata": {
                                "uid": "rs-1",
                                "ownerReferences": [
                                    {"kind": "Deployment", "uid": self.uid}
                                ],
                            }
                        }
                    ]
                }
            elif kind == "pods":
                data = {
                    "items": [
                        {
                            "metadata": {
                                "name": "cp-pod",
                                "uid": "pod-" + self.uid,
                                "ownerReferences": [
                                    {"kind": "ReplicaSet", "uid": "rs-1"}
                                ],
                            },
                            "spec": {
                                "nodeName": "node-1",
                                "containers": [
                                    {
                                        "name": "control-plane",
                                        "image": CP_IMAGE.reference,
                                    }
                                ],
                            },
                            "status": {
                                "phase": "Running",
                                "containerStatuses": [
                                    {
                                        "name": "control-plane",
                                        "ready": True,
                                        "containerID": self.container_id,
                                        "imageID": CP_IMAGE.id,
                                    }
                                ],
                            },
                        }
                    ]
                }
            else:
                raise AssertionError(argv)
        elif "kubectl" in joined:
            data = "deployment ready"
            if "--ignore-not-found" in joined:
                if self.function is not None or (
                    self.fault == "leaked-resources" and self.invoked
                ):
                    code, err = 1, "function resources did not disappear"
                data = ""
        else:
            tokens = shlex.split(argv[-1]) if argv[0] == "bash" else list(argv)
            content = (
                yaml.safe_load(tokens[tokens.index("printf") + 2])
                if "printf" in tokens
                else None
            )
            if "control-plane" in tokens:
                op = tokens[tokens.index("control-plane") + 1 :]
                if op == ["info"]:
                    data = {
                        "capabilities": {
                            "functionUpdate": True,
                            "replicas": True,
                            "buildMetadata": True,
                            "runtimeConfig": True,
                            "asyncInvocation": False,
                        },
                        "metadata": {
                            "version": "0.22.0",
                            "modules": list(MODULES),
                            "build": {
                                "type": "jvm",
                                "variant": "recipe-v2-cli-k8s-jvm",
                                "optimization": "c2",
                            },
                        },
                    }
                elif op == ["contract"]:
                    data = "openapi: 3.1.0\npaths:\n  /v1/functions: {}\n"
                elif "validate" in op:
                    assert isinstance(content, dict)
                    invalid = content["rateMaxPerSecond"] < 0
                    code, err = (
                        (1, "Runtime configuration is invalid") if invalid else (0, "")
                    )
                    data = "" if invalid else {"valid": True}
                elif "patch" in op:
                    assert isinstance(content, dict)
                    values = content.get("values", content)
                    if content.get("expectedRevision", self.revision) != self.revision:
                        code, err = 1, "Revision conflict"
                    elif not (
                        self.mode == "native"
                        and self.fault == "ignored-native"
                        and "expectedRevision" not in content
                    ):
                        self.rate = values["rateMaxPerSecond"]
                        self.revision += 1
                    if (
                        self.fault == "replacement-during-restore"
                        and self.mode == "native"
                        and "expectedRevision" in content
                    ):
                        self.uid = "deployment-2"
                    data = {
                        "revision": self.revision,
                        "effectiveConfig": {
                            "revision": self.revision,
                            "namespaces": {
                                "control-plane": {"rateMaxPerSecond": self.rate}
                            },
                        },
                    }
                else:
                    data = {
                        "revision": self.revision,
                        "namespaces": {
                            "control-plane": {"rateMaxPerSecond": self.rate}
                        },
                    }
            elif "invoke" in tokens:
                self.invoked = True
                data = {
                    "status": "success",
                    "statusCode": None,
                    "output": OUTPUT
                    if self.fault != "wrong-output"
                    else {"wordCount": 99},
                    "executionId": self.mode + "-id",
                }
                if self.fault == "replacement":
                    self.uid = "deployment-2"
                if self.fault == "container-restart":
                    self.container_id = "containerd://" + "d" * 64
                if self.fault == "artifact-change":
                    binary = Path(tokens[0])
                    binary.write_bytes(binary.read_bytes() + b"changed")
                if self.fault == "source-change":
                    (
                        task.options.cwd
                        / "functions/test-data/word-stats/correctness.json"
                    ).write_text('{"cases":[]}')
                if self.fault == "native-failure" and self.mode == "native":
                    code, err = 1, "native unavailable"
                if self.fault == "stderr-marker":
                    err = "MissingResourceRegistrationError"
                if self.fault == "oversize-output":
                    data = "x" * (1024 * 1024 + 1)
            else:
                op = tokens[tokens.index("fn") + 1 :]
                if op[0] == "apply":
                    assert isinstance(content, dict)
                    if (
                        self.function is not None
                        and content["queueSize"] != self.function["queueSize"]
                        and "--replace" not in op
                    ):
                        code, err, data = 1, "rerun with --replace", ""
                    else:
                        self.function = {
                            **content,
                            "requestedExecutionMode": "DEPLOYMENT",
                            "effectiveExecutionMode": "DEPLOYMENT",
                            "deploymentBackend": "k8s",
                            "endpointUrl": f"http://fn-fn.owned-cli.svc.cluster.local:8080/invoke?pass={self.mode}",
                        }
                        self.function.pop("executionMode")
                        self.replicas = 1
                        data = dict(self.function) if "--replace" in op else ""
                elif op[0] == "update":
                    assert isinstance(content, dict)
                    assert self.function is not None
                    self.function.update(content)
                    data = dict(self.function)
                elif op[0] == "delete":
                    if self.fault == "cleanup":
                        code, err, data = 1, "cleanup unavailable", ""
                    else:
                        self.function, data = None, ""
                elif op[0] == "list":
                    data = "fn\t" + FN_IMAGE.reference + "\n" if self.function else ""
                elif op[0] == "get":
                    assert self.function is not None
                    data = dict(self.function)
                elif op[0] == "replicas":
                    if op[1] == "set":
                        self.replicas = int(op[-1])
                        data = {"function": "fn", "replicas": self.replicas}
                    else:
                        data = {
                            "name": "fn",
                            "desiredReplicas": self.replicas,
                            "readyReplicas": self.replicas,
                        }
                else:
                    raise AssertionError(op)
        return TaskResult(
            task_id=task.task_id,
            status="passed" if code in task.options.expected_exit_codes else "failed",
            return_code=code,
            expected_exit_codes=task.options.expected_exit_codes,
            stdout=data if isinstance(data, str) else json.dumps(data),
            stderr=err,
        )


def _resource(title, value):
    return Resource(
        title=title, acquire=lambda _inputs: value, release=lambda *_args: None
    )


@pytest.fixture
def cli_gate(tmp_path):
    from nanolab.comparison.prepare import captured_source_state
    from nanolab.tasks.cli_artifacts import describe_cli_artifact

    source = tmp_path / "source"
    source.mkdir()
    (source / "build.gradle").write_text("version = '0.22.0'\n")
    corpus = source / "functions/test-data/word-stats/correctness.json"
    corpus.parent.mkdir(parents=True)
    corpus.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "literal corpus",
                        "input": {"text": "beta alpha gamma beta alpha", "topN": 3},
                        "expected": OUTPUT,
                    }
                ]
            }
        )
    )
    for args in (
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ),
    ):
        subprocess.run(("git", *args), cwd=source, check=True)
    architecture = "arm64" if platform.machine() == "aarch64" else "amd64"
    artifacts = []
    evidence = tmp_path / "artifacts"
    evidence.mkdir()
    for mode in ("jvm", "native"):
        binary = source / (
            "clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli"
            if mode == "jvm"
            else "clients/cli/build/native/nativeCompile/nanofaas-cli"
        )
        binary.parent.mkdir(parents=True)
        if mode == "jvm":
            binary.write_text("#!/bin/sh\nexit 0\n")
            lib = binary.parent.parent / "lib"
            lib.mkdir()
            (lib / "cli.jar").write_bytes(b"jar")
        else:
            header = bytearray(64)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = (183 if architecture == "arm64" else 62).to_bytes(
                2, "little"
            )
            binary.write_bytes(header)
        binary.chmod(0o755)
        artifact = describe_cli_artifact(source, mode, architecture=architecture)
        artifacts.append(_resource(mode, artifact))
        (evidence / f"{mode}.json").write_text(
            json.dumps(
                {
                    "mode": mode,
                    "version": "0.22.0",
                    "binary": str(binary),
                    "files": artifact.files,
                    "source": captured_source_state(source),
                    "recipeSha256": hashlib.sha256(b"name: fixture\n").hexdigest(),
                    "tag": "cli-test",
                    "help": {"stdout": "Usage: nanofaas", "returnCode": 0},
                    "versionCommand": {"stdout": "nanofaas 0.22.0", "returnCode": 0},
                }
            )
        )
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("name: fixture\n")
    run_value = RecipeRun(source, recipe, tmp_path / "distribution", "cli-test")
    distribution_value = RecipeDistribution(
        tmp_path / "distribution.json",
        hashlib.sha256(recipe.read_bytes()).hexdigest(),
        "cli-test",
        None,
        MODULES,
        (
            RecipeComponent(
                "control-plane",
                "control-plane",
                "java",
                "jvm",
                CP_IMAGE,
                "recipe-v2-cli-k8s-jvm",
                "c2",
            ),
            RecipeComponent(
                "function", "word-stats", "java", "jvm", FN_IMAGE, None, None
            ),
        ),
    )
    run = _resource("Frozen recipe", run_value)
    distribution = _resource("Built recipe", distribution_value)
    target = _resource("Selected target", MinikubeTarget("owned", "owned", ("node-1",)))
    endpoint = _resource("API forward", "http://127.0.0.1:32123")
    values = {
        run: run_value,
        distribution: distribution_value,
        target: target.acquire(TaskInputs.empty()),
        endpoint: "http://127.0.0.1:32123",
        **{resource: resource.acquire(TaskInputs.empty()) for resource in artifacts},
    }
    inputs = TaskInputs._for_resources(values, set(values))
    boundary = PlatformBoundary()

    def task_factory():
        from nanolab.tasks.validation.cli_parity import CliParityTask

        return CliParityTask(
            run,
            distribution,
            tuple(artifacts),
            runtime="parity",
            endpoint=endpoint,
            target=target,
            namespace="owned-cli",
            function="fn",
            executor=boundary,
            evidence_dir=tmp_path / "qualification",
        )

    task = task_factory
    return task, inputs, boundary, evidence


def test_parity_checks_both_passes_and_restores_baselines(cli_gate):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    task.run(inputs)
    assert boundary.function is None
    assert boundary.rate == 1000000
    applications = [
        (mode, rate, absent)
        for mode, title, rate, absent in boundary.events
        if title == "Apply fn"
    ]
    assert applications == [("jvm", 1000000, True), ("native", 1000000, True)]
    receipt = json.loads((task.evidence_dir / "parity.json").read_text())
    assert receipt["runtime"] == "parity"
    assert receipt["modes"] == ["jvm", "native"]
    assert receipt["equal"] is True
    assert all(spec.argv[0] == "timeout" for spec in boundary.specs)


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-output",
        "replacement",
        "container-restart",
        "artifact-change",
        "source-change",
        "leaked-resources",
        "stderr-marker",
        "log-marker",
        "oversize-output",
        "oversize-log",
        "native-failure",
        "cleanup",
    ],
)
def test_parity_rejects_same_wrong_output_and_replaced_control_plane(cli_gate, fault):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    boundary.fault = fault
    with pytest.raises((ValueError, RuntimeError)):
        task.run(inputs)
    assert not (task.evidence_dir / "parity.json").exists()
    assert (task.evidence_dir / "jvm/commands.jsonl").exists()
    assert any(title == "Delete fn" for _, title, _, _ in boundary.events)


def test_baseline_equal_to_default_patch_still_proves_native_write(cli_gate):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    boundary.rate = 999999
    boundary.fault = "ignored-native"
    with pytest.raises(RuntimeError, match="rateMaxPerSecond"):
        task.run(inputs)
    assert not (task.evidence_dir / "parity.json").exists()


def test_retry_never_reuses_qualified_receipts(cli_gate):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    task.evidence_dir.mkdir()
    old = task.evidence_dir / "parity.json"
    old.write_text("old attempt\n")
    with pytest.raises(FileExistsError):
        task.run(inputs)
    assert old.read_text() == "old attempt\n"
    assert boundary.specs == []


def test_parity_refuses_missing_requested_mode(cli_gate):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    task.artifacts = task.artifacts[:1]
    with pytest.raises(ValueError, match="modes"):
        task.run(inputs)
    assert not (task.evidence_dir / "parity.json").exists()
    assert boundary.specs == []


def test_omitted_contract_cases_cannot_qualify(cli_gate, monkeypatch):
    import nanolab.tasks.validation.cli_parity as parity

    task_factory, inputs, _, _ = cli_gate
    task = task_factory()
    monkeypatch.setattr(parity, "add_cli_contract", lambda *_args, **_kwargs: ())
    with pytest.raises(RuntimeError, match="cases"):
        task.run(inputs)
    assert not (task.evidence_dir / "parity.json").exists()


@pytest.mark.parametrize("mode", ["jvm", "native"])
def test_selected_single_mode_is_not_a_parity_receipt(cli_gate, mode):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    task.runtime = mode
    task.artifacts = tuple(
        resource for resource in task.artifacts if resource.title == mode
    )
    task.run(inputs)
    assert not (task.evidence_dir / "parity.json").exists()
    receipt = json.loads((task.evidence_dir / "contracts.json").read_text())
    assert receipt["runtime"] == mode
    assert receipt["modes"] == [mode]
    assert receipt["equal"] is False
    assert [
        (selected, absent)
        for selected, title, _, absent in boundary.events
        if title == "Apply fn"
    ] == [(mode, True)]


def test_preexisting_function_is_rejected_without_deletion(cli_gate):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    boundary.function = {"name": "fn", "image": "foreign"}
    with pytest.raises(RuntimeError, match="fn"):
        task.run(inputs)
    assert not any(title == "Delete fn" for _, title, _, _ in boundary.events)
    assert boundary.function == {"name": "fn", "image": "foreign"}


def test_pass_receipts_are_bounded_as_a_whole(tmp_path):
    from sonata_tasks.execution.models import CommandTaskSpec

    from nanolab.tasks.validation.cli_parity import _EvidenceExecutor

    class OutputBoundary:
        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            return TaskResult(
                task_id=task.task_id,
                status="passed",
                return_code=0,
                stdout="x" * 900000,
            )

    path = tmp_path / "commands.jsonl"
    with path.open("x") as stream:
        executor = _EvidenceExecutor(OutputBoundary(), stream)

        def emit_responses():
            for index in range(20):
                executor.run(
                    CommandTaskSpec(
                        task_id=str(index), summary="bounded response", argv=("true",)
                    )
                )

        with pytest.raises(RuntimeError, match="receipts exceed"):
            emit_responses()
    assert path.stat().st_size <= 16 * 1024 * 1024


def test_failed_command_retains_post_request_owned_logs(cli_gate):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    boundary.fault = "stderr-marker"
    with pytest.raises(RuntimeError, match="MissingResourceRegistrationError"):
        task.run(inputs)
    assert (
        task.evidence_dir / "jvm/logs-failure.txt"
    ).read_text() == "Started control-plane\n"


def test_control_plane_continuity_covers_final_baseline_restore(cli_gate):
    task_factory, inputs, boundary, _ = cli_gate
    task = task_factory()
    boundary.fault = "replacement-during-restore"
    with pytest.raises(RuntimeError, match="replaced"):
        task.run(inputs)
    assert not (task.evidence_dir / "parity.json").exists()
