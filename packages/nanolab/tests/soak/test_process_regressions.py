"""Synthetic process trees: reject impossible stop budgets and retain ownership."""

import contextlib
import os
import subprocess
import sys
import time
from threading import Event, Thread

import pytest

from nanolab.tasks.soak.processes import OwnedCommandRunner


def test_ultra_short_cleanup_budget_rejected_before_any_spawn(tmp_path):
    marker = tmp_path / "spawned"
    with pytest.raises(ValueError, match=r"stop_timeout_s|cleanup|minimum"):
        OwnedCommandRunner(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
            cwd=tmp_path,
            env=os.environ,
            log_path=tmp_path / "log",
            timeout_s=10,
            cancelled=Event(),
            output_limit_bytes=1024,
            stop_timeout_s=0.001,
        ).run()
    assert not marker.exists()


def test_explicit_stop_rejects_unsupported_budget_without_starting(tmp_path):
    runner = OwnedCommandRunner(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        env=os.environ,
        log_path=tmp_path / "log",
        timeout_s=1,
        cancelled=Event(),
        output_limit_bytes=1024,
    )
    with pytest.raises(ValueError, match=r"timeout|cleanup|minimum"):
        runner.stop(0.001)


@pytest.mark.parametrize("failure", ["cancel", "caller-error"])
def test_multilevel_detached_descendants_are_gone_before_return_or_raise(
    tmp_path, failure
):
    # Each process forms a new session. Killing just the root/group is insufficient.
    script = tmp_path / "tree.py"
    script.write_text("""import os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
root=Path(sys.argv[1]); depth=int(sys.argv[2])
(root/('pid-'+str(depth))).write_text(str(os.getpid()))
if depth:
    subprocess.Popen([sys.executable,__file__,str(root),str(depth-1)],start_new_session=True)
else:
    (root/'ready').touch()
while True: time.sleep(.01)
""")
    ready = tmp_path / "ready"
    event = Event()

    class CallerError:
        def is_set(self):
            return False

        def wait(self, delay):
            if ready.exists():
                raise RuntimeError("synthetic caller failure")
            time.sleep(delay)

    runner = OwnedCommandRunner(
        [sys.executable, str(script), str(tmp_path), "5"],
        cwd=tmp_path,
        env=os.environ,
        log_path=tmp_path / "log",
        timeout_s=10,
        cancelled=event if failure == "cancel" else CallerError(),
        output_limit_bytes=1024,
        stop_timeout_s=0.1,
    )
    results, errors = [], []

    def run():
        try:
            results.append(runner.run())
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True
    )
    try:
        thread.start()
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ready.exists()
        event.set()
        thread.join(2)
        assert not thread.is_alive()
        if failure == "caller-error":
            assert len(errors) == 1 and str(errors[0]) == "synthetic caller failure"
        else:
            assert not errors
            assert results[0].reaped and results[0].cancelled
        for path in tmp_path.glob("pid-*"):
            with pytest.raises(ProcessLookupError, match="Errno 3"):
                os.kill(int(path.read_text()), 0)
        assert unrelated.poll() is None
    finally:
        event.set()
        try:
            runner.stop(1)
        finally:
            thread.join(2)
            for path in tmp_path.glob("pid-*"):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(path.read_text()), 9)
            unrelated.terminate()
            unrelated.wait(timeout=2)
