"""Synthetic tests for the build command ownership adapter."""

import importlib
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec

from nanolab.tasks.soak.processes import OwnedCommandResult


def module():
    try:
        return importlib.import_module("nanolab.tasks.soak.build_executor")
    except ModuleNotFoundError:
        pytest.fail("owned build executor is missing")


def executor(tmp_path, **overrides):
    args = {
        "cwd": tmp_path,
        "log_dir": tmp_path / "logs",
        "cancelled": threading.Event(),
        "timeout_cap_s": 2,
        "artifact_limit_bytes": 4096,
        "command_output_limit_bytes": 1024,
        "observation_limit_bytes": 512,
    }
    args.update(overrides)
    return module().OwnedBuildCommandExecutor(**args)


def task(source='print("hello")', **options):
    return CommandTaskSpec(
        "build-test",
        "synthetic build command",
        (sys.executable, "-c", source),
        options=CommandOptions(**options),
    )


def successful():
    return OwnedCommandResult(0, False, True, ended_s=time.monotonic())


def test_constructor_binding_and_dry_run_have_no_io(tmp_path, monkeypatch):
    m = module()

    def forbidden(*args, **kwargs):
        raise AssertionError("planning performed I/O")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", forbidden)
        patch.setattr(Path, "stat", forbidden)
        patch.setattr(m, "run_owned_command", forbidden)
        bound = executor(tmp_path)
        assert bound.binding_key("host") == "local"
        result = bound.run(task(), dry_run=True)
        assert result.status == "skipped" and result.return_code is None
    assert not (tmp_path / "logs").exists()


def test_host_execution_preserves_cwd_env_and_stores_output_in_file(tmp_path):
    bound = executor(tmp_path, env={"BUILD_BASE": "base"})
    result = bound.run(
        task(
            'import os;print(os.getcwd());print(os.environ["BUILD_BASE"]);'
            'print(os.environ["BUILD_EXTRA"])',
            env={"BUILD_EXTRA": "extra"},
        )
    )
    assert result.ok and result.stdout == ""
    assert bound.last_result.reaped
    assert bound.last_log_path.read_text().splitlines() == [
        str(tmp_path),
        "base",
        "extra",
    ]


@pytest.mark.parametrize("remote", ["role", "directory"])
def test_remote_execution_rejected_before_io(tmp_path, remote):
    from sonata_tasks.errors import UnsupportedCommandOptionError

    bound = executor(tmp_path)
    spec = (
        replace(task(), role="builder-vm")
        if remote == "role"
        else task(remote_dir="/remote")
    )
    with pytest.raises(
        UnsupportedCommandOptionError,
        match=(
            r"owned build executor accepts only the|"
            r"owned build executor does not support"
        ),
    ):
        bound.run(spec)
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"forced_stop": True},
        {"reaped": False},
        {"timed_out": True},
        {"quota_exceeded": True},
        {"errors": ("capture failed",)},
        {"returncode": None},
        {"returncode": True},
    ],
)
def test_zero_exit_does_not_hide_runner_failures(tmp_path, monkeypatch, changes):
    m = module()

    def run(argv, **kwargs):
        kwargs["log_path"].write_bytes(b"")
        return replace(successful(), **changes)

    monkeypatch.setattr(m, "run_owned_command", run)
    result = executor(tmp_path).run(task())
    assert not result.ok and result.status == "failed"
    assert result.stderr


def test_expected_nonzero_exit_is_supported(tmp_path):
    result = executor(tmp_path).run(
        task("raise SystemExit(7)", expected_exit_codes=frozenset({7}))
    )
    assert result.ok and result.return_code == 7


def test_task_timeout_is_capped_and_remaining_budget_is_shared(tmp_path, monkeypatch):
    m = module()
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        kwargs["log_path"].write_bytes(b"x" * kwargs["output_limit_bytes"])
        return replace(successful(), log_bytes=kwargs["output_limit_bytes"])

    monkeypatch.setattr(m, "run_owned_command", run)
    bound = executor(tmp_path, artifact_limit_bytes=150, command_output_limit_bytes=100)
    assert bound.run(task(timeout_seconds=100)).ok
    assert bound.run(task(timeout_seconds=0.25)).ok
    result = bound.run(task())
    assert not result.ok
    assert [call["timeout_s"] for call in calls] == [2, 0.25]
    assert [call["output_limit_bytes"] for call in calls] == [100, 50]


def test_preexisting_logs_consume_budget(tmp_path, monkeypatch):
    m = module()
    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "existing.log").write_bytes(b"x" * 90)
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["output_limit_bytes"])
        kwargs["log_path"].write_bytes(b"")
        return successful()

    monkeypatch.setattr(m, "run_owned_command", run)
    assert executor(tmp_path, artifact_limit_bytes=100).run(task()).ok
    assert calls == [10]
    assert (directory / "existing.log").stat().st_size == 90


def test_timeout_and_quota_are_real_bounded_failures(tmp_path):
    bound = executor(tmp_path, timeout_cap_s=0.1)
    result = bound.run(task("import time;time.sleep(60)"))
    assert not result.ok and bound.last_result.reaped and bound.last_result.timed_out
    result = bound.run(
        task('import os,time\nwhile True:\n os.write(1,b"x"*4096);time.sleep(.001)')
    )
    assert (
        not result.ok and bound.last_result.quota_exceeded and bound.last_result.reaped
    )
    assert sum(path.stat().st_size for path in (tmp_path / "logs").iterdir()) <= 4096


def test_cancellation_raises_keyboard_interrupt_after_reaping(tmp_path):
    cancelled = threading.Event()
    bound = executor(tmp_path, cancelled=cancelled)
    pid_path = tmp_path / "owned.pid"
    spec = task(
        "import os,time;from pathlib import Path;Path("
        + repr(str(pid_path))
        + ").write_text(str(os.getpid()));time.sleep(60)"
    )

    def cancel():
        deadline = time.monotonic() + 2
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancelled.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        with pytest.raises(
            KeyboardInterrupt, match=r"build cancelled after owned children"
        ):
            bound.run(spec)
        assert bound.last_result.reaped
        with pytest.raises(ProcessLookupError, match="Errno 3"):
            os.kill(int(pid_path.read_text()), 0)
    finally:
        cancelled.set()
        thread.join(3)


def test_unconfirmed_cleanup_does_not_claim_reaped_cancellation(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(
        m,
        "run_owned_command",
        lambda *args, **kwargs: replace(successful(), cancelled=True, reaped=False),
    )
    with pytest.raises(RuntimeError, match=r"build cancellation cleanup is"):
        executor(tmp_path).run(task())


def test_observe_is_bounded_and_accepts_collector_inspection(tmp_path, monkeypatch):
    m = module()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        kwargs["log_path"].write_bytes(b'{"digest":"observed"}')
        return replace(successful(), log_bytes=21)

    monkeypatch.setattr(m, "run_owned_command", run)
    bound = executor(tmp_path)
    argv = (
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "registry/image@sha256:" + "a" * 64,
        "--format",
        "{{json .Manifest}}",
    )
    assert bound.observe(argv, 100) == b'{"digest":"observed"}'
    assert calls[0][1]["timeout_s"] == 2
    assert calls[0][1]["output_limit_bytes"] == 512


@pytest.mark.parametrize(
    "argv",
    [
        ("docker", "build", "."),
        ("docker", "buildx", "inspect", "builder", "--bootstrap"),
        ("sh", "-c", "docker inspect anything"),
        ("docker", "buildx", "imagetools", "inspect", "image:mutable"),
    ],
)
def test_observe_rejects_mutating_or_unbound_commands_without_io(tmp_path, argv):
    bound = executor(tmp_path)
    with pytest.raises(ValueError, match=r"command is not an allowed read-only"):
        bound.observe(argv, 1)
    assert not (tmp_path / "logs").exists()


def test_observe_rejects_failed_or_oversized_capture(tmp_path, monkeypatch):
    m = module()

    def run(argv, **kwargs):
        kwargs["log_path"].write_bytes(b"x" * 513)
        return successful()

    monkeypatch.setattr(m, "run_owned_command", run)
    with pytest.raises(RuntimeError, match=r"bound|quota|limit"):
        executor(tmp_path).observe(("docker", "buildx", "version"), 1)


def test_collector_provenance_inspection_uses_bounded_executor(tmp_path, monkeypatch):
    from nanolab.tasks.soak.build_provenance import BuildProvenanceCollector

    m = module()
    calls = []
    body = b'{"source":"synthetic-provenance"}'

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        kwargs["log_path"].write_bytes(body)
        return replace(successful(), log_bytes=len(body))

    monkeypatch.setattr(m, "run_owned_command", run)
    bound = executor(tmp_path)
    collector = BuildProvenanceCollector(bound.observe, timeout_s=100)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    reference = "registry/image@sha256:" + "a" * 64
    assert collector._inspect(reference, "Provenance", evidence) == {
        "source": "synthetic-provenance",
    }
    assert calls[0][0] == (
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        reference,
        "--format",
        "{{json .Provenance}}",
    )
    assert calls[0][1]["timeout_s"] == 2
    assert calls[0][1]["output_limit_bytes"] == 512
    assert (evidence / "provenance.json").read_bytes() == body


@pytest.mark.parametrize(
    ("reference", "template"),
    [
        ("registry/image:mutable", "{{json .Provenance}}"),
        ("registry/image@sha256:" + "a" * 64, "{{json .UnapprovedField}}"),
    ],
)
def test_provenance_allowlist_still_requires_digest_and_exact_field(
    tmp_path, reference, template
):
    with pytest.raises(ValueError, match=r"command is not an allowed read-only"):
        executor(tmp_path).observe(
            (
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                reference,
                "--format",
                template,
            ),
            1,
        )
    assert not (tmp_path / "logs").exists()
