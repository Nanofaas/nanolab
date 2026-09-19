"""The containerd soak runner preserves the shared terminal contract."""

import json
import stat
import subprocess
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sonata_engine import TaskOutcome
from sonata_tasks.tasks.models import TaskResult

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.containerd_runtime import ContainerdSoakRun
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.sources import SourceEntry
from nanolab.tasks.soak.workflow import write_terminal_receipt


def _scenario():
    path = Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    return ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))


def test_failed_owned_target_inspection_records_inconclusive_terminal(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.containerd_runtime as module

    class MissingTarget:
        def __init__(self, run, executor):
            pass

        def inspect(self, role):
            raise OSError("owned task absent")

    monkeypatch.setattr(module, "RootlessCollectionTransport", MissingTarget)
    task = ContainerdSoakRun(
        _scenario(),
        RootlessRun("run123", tmp_path / "repo", tmp_path / "session.sh"),
        EnvironmentConfig.model_validate({"provider": "local"}),
        object(),
        run_dir=tmp_path / "run",
        repo_root=tmp_path / "repo",
    )
    with pytest.raises(OSError, match="owned task absent"):
        task.run(None)
    terminal = json.loads((tmp_path / "run/terminal.json").read_text())
    assert terminal["status"] == "INCONCLUSIVE"


def test_runner_binds_common_lifecycle_to_real_cgroup_observations(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.containerd_runtime as module

    policy = _scenario().soak
    assert policy is not None
    targets = {
        role: Target(
            role,
            f"owned-{role}",
            100 + index,
            "12345",
            "sha256:" + "a" * 64
            if role == "control-plane"
            else "registry/fn@sha256:" + "b" * 64,
            spec.runtime,
        )
        for index, (role, spec) in enumerate(policy.roles.items())
    }

    class ObservedTransport:
        def __init__(self, run, executor):
            assert run.run_id == "run123"

        def inspect(self, role):
            return targets[role], {
                "artifact_path": "/repo/app.jar" if role == "control-plane" else None,
                "platform": policy.images[role].platform,
            }

        def collect(self, target, endpoint, timeout_s):
            assert endpoint is None
            return {
                "configuration": {
                    "cpu_max": [200000, 100000],
                    "cpuset": "0-1",
                    "memory_bytes": policy.roles[target.role].memory_limit_bytes,
                    "limit_sources": {
                        "cpu": "cgroup-v2/cpu.max",
                        "memory_bytes": "cgroup-v2/memory.max",
                    },
                    "runtime": target.runtime,
                    "runtime_options": [],
                    "capabilities": ["procfs", "cgroup-v2"],
                    "collection_sources": ["procfs", "cgroup-v2"],
                }
            }

    evidence = tmp_path / "run/evidence"
    writer = ArtifactWriter(evidence, 1024 * 1024)
    writer.write_json("remote-source.json", {"revision": "feature-commit"})
    monkeypatch.setattr(module, "RootlessCollectionTransport", ObservedTransport)
    monkeypatch.setattr(
        module,
        "resolve_loadtest_urls",
        lambda *a, **k: ("http://127.0.0.1:8080", "http://127.0.0.1:9090"),
    )
    monkeypatch.setattr(
        module,
        "observe_local_configuration",
        lambda *a, **k: {
            "modules": policy.images["control-plane"].modules,
            "retention_s": policy.retention_s,
        },
    )

    def lifecycle(prepared, *, deployment, transport, **kwargs):
        assert isinstance(transport, ObservedTransport)
        assert set(deployment.discover()) == set(targets.values())
        observed = deployment.observations(tuple(targets.values()))
        assert observed["roles"]["control-plane"]["cpu"] == 2
        assert observed["roles"]["control-plane"]["artifact_path"] == "/repo/app.jar"
        assert observed["roles"]["control-plane"]["platform"] == "linux/arm64"
        assert observed["remote_source"]["revision"] == "feature-commit"
        assert deployment.api_endpoint == "http://127.0.0.1:8080"

        class Lifecycle:
            state = SimpleNamespace(report=None)

            def run(self, inputs):
                write_terminal_receipt(tmp_path / "run", "PASS")
                return TaskOutcome(value="shared-workload-complete")

        return Lifecycle()

    monkeypatch.setattr(module, "create_soak_lifecycle", lifecycle)
    monkeypatch.setattr(
        module.ContainerdSoakRun,
        "_prepare",
        lambda *a: SimpleNamespace(
            evidence_dir=evidence,
            writer=writer,
        ),
    )
    task = ContainerdSoakRun(
        _scenario(),
        RootlessRun("run123", tmp_path / "repo", tmp_path / "session.sh"),
        EnvironmentConfig.model_validate({"provider": "local"}),
        object(),
        run_dir=tmp_path / "run",
        repo_root=tmp_path / "repo",
    )
    assert task.run(None).value == "shared-workload-complete"
    assert json.loads((tmp_path / "run/terminal.json").read_text())["status"] == "PASS"


def test_remote_source_verification_uses_staged_content_without_git(tmp_path):
    staged = tmp_path / "nanofaas"
    staged.mkdir()
    source = staged / "feature.txt"
    source.write_text("feature commit contents")
    entry = SourceEntry(
        path="feature.txt",
        kind="file",
        mode=stat.S_IMODE(source.stat().st_mode),
        size_bytes=source.stat().st_size,
        sha256=sha256(source.read_bytes()).hexdigest(),
        link_target=None,
    )
    commands = []

    class Executor:
        def run(self, command, *, dry_run=False):
            commands.append(command.argv)
            process = subprocess.run(
                command.argv, text=True, capture_output=True, check=False
            )
            return TaskResult(
                command.task_id,
                "passed" if process.returncode == 0 else "failed",
                process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
            )

    task = ContainerdSoakRun(
        _scenario(),
        RootlessRun(
            "run123",
            staged,
            Path(__file__).parents[2] / "assets/containerd-rootless/session.sh",
        ),
        EnvironmentConfig.model_validate({"provider": "local"}),
        Executor(),
        run_dir=tmp_path / "run",
        repo_root=staged,
    )
    snapshot = SimpleNamespace(
        entries=(entry,), revision="commit123", fingerprint="source-hash"
    )
    result = task._verify_remote_source(snapshot)
    assert result["entry_count"] == 1
    assert result["revision"] == "commit123"
    assert all("git" not in argv for argv in commands)
    source.write_text("changed")
    with pytest.raises(RuntimeError, match="remote source verification failed"):
        task._verify_remote_source(snapshot)
