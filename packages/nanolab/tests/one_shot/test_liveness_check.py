"""Remote container checks tolerate a spell of transport stalls, never a stopped one."""

import json
import sys
import time

import pytest

from nanolab.plans.one_shot_common import LivenessCheck


def script(code):
    return (sys.executable, "-c", code)


RUNNING = script("print('true')")
STOPPED = script("print('false')")
MISSING = script(
    "import sys; sys.stderr.write('Error: No such object: c'); sys.exit(1)"
)
SSH_DOWN = script(
    "import sys; sys.stderr.write('Connection closed by 149.132.179.35'); sys.exit(255)"
)
STALLED = script("import time; time.sleep(5)")


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def check(tmp_path, *, tolerated_seconds, timeout=5.0):
    return LivenessCheck(
        "edge-1:collector",
        tmp_path / "liveness.jsonl",
        timeout=timeout,
        tolerated_seconds=tolerated_seconds,
    )


def test_a_short_spell_of_transport_failures_is_only_recorded(tmp_path):
    liveness = check(tmp_path, tolerated_seconds=60, timeout=0.3)
    liveness.run(SSH_DOWN)
    liveness.run(STALLED)
    liveness.run(RUNNING)
    outcomes = [row["outcome"] for row in rows(tmp_path / "liveness.jsonl")]
    assert outcomes == ["transport", "timeout", "running"]
    first, second, third = rows(tmp_path / "liveness.jsonl")
    assert "Connection closed" in first["stderr"]
    assert second["unreachableSeconds"] >= 0.3
    assert third["unreachableSeconds"] == 0


def test_a_node_unreachable_past_the_window_fails_the_run(tmp_path):
    liveness = check(tmp_path, tolerated_seconds=0.5)
    liveness.run(SSH_DOWN)
    time.sleep(0.6)
    with pytest.raises(RuntimeError, match="unreachable for 1 s"):
        liveness.run(SSH_DOWN)


def test_a_success_restarts_the_window(tmp_path):
    liveness = check(tmp_path, tolerated_seconds=0.5)
    liveness.run(SSH_DOWN)
    time.sleep(0.6)
    liveness.run(RUNNING)
    liveness.run(SSH_DOWN)  # a new spell, not the old one continued


@pytest.mark.parametrize(
    ("argv", "outcome"), [(STOPPED, "stopped"), (MISSING, "missing")]
)
def test_a_stopped_or_missing_container_fails_at_once(tmp_path, argv, outcome):
    with pytest.raises(RuntimeError, match=outcome):
        check(tmp_path, tolerated_seconds=300).run(argv)


def test_a_local_check_fails_at_the_first_stall(tmp_path):
    with pytest.raises(RuntimeError, match="unreachable"):
        check(tmp_path, tolerated_seconds=0, timeout=0.3).run(STALLED)
