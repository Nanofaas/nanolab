import os
import subprocess
from pathlib import Path

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.testing import RecordingExecutor

from nanolab.tasks.kubectl import k8s_function_resources_absent


def test_it_waits_until_a_deleted_function_has_no_deployment_or_service() -> None:
    executor = RecordingExecutor()

    _ = k8s_function_resources_absent(
        function="word-stats",
        namespace="nf",
        executor=executor,
        role="stack",
    ).run(TaskInputs.empty())

    script = executor.seen[0].argv[-1]
    assert executor.seen[0].argv[:2] == ("bash", "-lc")
    assert "deployment/fn-word-stats" in script
    assert "service/fn-word-stats" in script
    assert "kubectl -n nf get" in script


@pytest.mark.parametrize(
    ("response", "status"),
    [
        ("Unable to connect to the server: connection refused", 1),
        ("Error from server (Forbidden): deployments.apps is forbidden", 3),
        ("error: current-context is not set", 2),
    ],
)
def test_cleanup_preserves_kubectl_errors(
    tmp_path: Path, response: str, status: int
) -> None:
    result = _run_cleanup(tmp_path, f"echo '{response}' >&2; exit {status}")
    assert result.returncode == status
    assert response in result.stderr


def test_cleanup_succeeds_for_missing_objects(tmp_path: Path) -> None:
    result = _run_cleanup(
        tmp_path,
        'case "$*" in *--ignore-not-found*"-o name"*) exit 0;; '
        '*) echo "Error from server (NotFound)" >&2; exit 1;; esac',
    )
    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize("resource", ["deployment", "service"])
def test_cleanup_fails_when_either_object_remains(
    tmp_path: Path, resource: str
) -> None:
    result = _run_cleanup(
        tmp_path,
        f'case "$*" in *{resource}/fn-word-stats*) '
        f'echo "{resource}/fn-word-stats";; esac',
    )
    assert result.returncode == 1
    assert "resources did not disappear" in result.stderr


def test_cleanup_waits_until_both_resources_disappear(tmp_path: Path) -> None:
    state = tmp_path / "seen"
    result = _run_cleanup(
        tmp_path,
        f'if [ ! -f "{state}" ]; then echo deployment/fn-word-stats; '
        f'touch "{state}"; fi',
        timeout_seconds=4,
    )
    assert result.returncode == 0


def _run_cleanup(
    tmp_path: Path, kubectl_body: str, *, timeout_seconds: int = 2
) -> subprocess.CompletedProcess[str]:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f"#!/bin/sh\n{kubectl_body}\n", encoding="utf-8")
    kubectl.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    executor = RecordingExecutor()
    k8s_function_resources_absent(
        function="word-stats",
        namespace="nf",
        executor=executor,
        role="stack",
        timeout_seconds=timeout_seconds,
    ).run(TaskInputs.empty())
    return subprocess.run(
        ["bash", "-c", executor.seen[0].argv[-1]],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
