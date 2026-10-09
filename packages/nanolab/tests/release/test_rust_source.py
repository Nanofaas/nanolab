import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nanolab.release.build import source_test_commands

EXPECTED = [
    "sdks/rust",
    "functions/rust/word-stats",
    "functions/rust/json-transform",
    "functions/rust/roman-numeral",
    "functions/rust/qr-code",
    "runtimes/watchdog",
    "watchdog-smoke",
]


@pytest.mark.parametrize("failure", ["", "sdks/rust", "functions/rust/roman-numeral"])
def test_rust_source_phase_executes_every_suite_and_stops_on_failure(
    tmp_path: Path, failure: str
) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for relative in EXPECTED[:-1]:
        (source / relative).mkdir(parents=True)
    (source / "runtimes/watchdog/test-local.sh").write_text(
        "test -f runtimes/watchdog/target/debug/nanofaas-watchdog || exit 18\n"
        'printf "%s\\n" "watchdog-smoke" >> "$TRACE"\n'
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "apk").write_text("#!/bin/sh\nexit 0\n")
    (tools / "cargo").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "cwd = Path.cwd().relative_to(os.environ['WORKSPACE']).as_posix()\n"
        "suite = 'runtimes/watchdog' if '--manifest-path' in sys.argv else cwd\n"
        "with open(os.environ['TRACE'], 'a') as stream: stream.write(suite + '\\n')\n"
        "with open(os.environ['CACHE_TRACE'], 'a') as stream:\n"
        " stream.write(json.dumps(os.environ.get('CARGO_TARGET_DIR')) + '\\n')\n"
        "if suite == 'runtimes/watchdog':\n"
        " target_dir = os.environ.get('CARGO_TARGET_DIR', 'runtimes/watchdog/target')\n"
        " target = Path(target_dir) / 'debug'\n"
        " target.mkdir(parents=True, exist_ok=True)\n"
        " (target / 'nanofaas-watchdog').touch()\n"
        "sys.exit(17 if suite == os.environ['FAIL_SUITE'] else 0)\n"
    )
    for tool in tools.iterdir():
        tool.chmod(0o755)
    task = next(
        t for t in source_test_commands(source) if t.task_id == "release.source.rust"
    )
    # Run the actual generated shell; only relocate container filesystem roots.
    script = (
        task.argv[-1]
        .replace("/source", str(source))
        .replace("/workspace", str(workspace))
    )
    trace = tmp_path / "trace"
    cache_trace = tmp_path / "cache-trace"
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=workspace,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "WORKSPACE": str(workspace),
            "TRACE": str(trace),
            "CACHE_TRACE": str(cache_trace),
            "FAIL_SUITE": failure,
        },
        capture_output=True,
        check=False,
    )
    expected = EXPECTED if not failure else EXPECTED[: EXPECTED.index(failure) + 1]
    assert trace.read_text().splitlines() == expected
    assert result.returncode == (17 if failure else 0)
    cache_dirs = [json.loads(line) for line in cache_trace.read_text().splitlines()]
    sdk_cache_dirs = cache_dirs[:-1] if not failure else cache_dirs
    assert len(set(sdk_cache_dirs)) == 1
    assert cache_dirs[0] == str(workspace / "build/cargo-target")
    if not failure:
        assert cache_dirs[-1] is None
