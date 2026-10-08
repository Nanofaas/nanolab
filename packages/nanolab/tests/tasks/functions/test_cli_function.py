"""Command proofs reject plausible responses from the wrong state or function."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.local import LocalCommandTaskExecutor
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.tasks.cli_function import (
    function_replace_tasks,
    function_replicas_tasks,
    runtime_config_tasks,
)
from nanolab.tasks.manifest import FunctionManifest

MANIFEST = FunctionManifest(
    name="fn", image="image:test", resources={"cpu": "100m", "memory": "128Mi"}
)
DETAILS = {
    "name": "fn",
    "image": "image:test",
    "timeoutMs": 5000,
    "concurrency": 2,
    "queueSize": 20,
    "maxRetries": 3,
    "resources": {"cpu": "100m", "memory": "128Mi"},
    "requestedExecutionMode": "DEPLOYMENT",
    "effectiveExecutionMode": "DEPLOYMENT",
    "deploymentBackend": "k8s",
}


@dataclass
class ReplyExecutor:
    stdout: str
    stderr: str = ""
    code: int = 0
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(
            task_id=task.task_id,
            status="passed"
            if self.code in task.options.expected_exit_codes
            else "failed",
            return_code=self.code,
            expected_exit_codes=task.options.expected_exit_codes,
            stdout=self.stdout,
            stderr=self.stderr,
        )


@pytest.mark.parametrize("cpu", [0.1, 0.2])
def test_get_checks_requested_resource_fields_with_full_server_dto(cpu):
    from nanolab.tasks.cli_function import CliFunctionGetTask

    details = {
        **DETAILS,
        "resources": {
            "requests": {"cpu": cpu, "memoryMiB": None},
            "limits": None,
            "requestWithinLimit": True,
        },
    }
    task = CliFunctionGetTask(
        FunctionManifest(
            name="fn", image="image:test", resources={"requests": {"cpu": 0.1}}
        ),
        cli_argv=("cli",),
        executor=ReplyExecutor(json.dumps(details)),
        role="host",
    )
    if cpu == 0.1:
        task.run(TaskInputs({}, frozenset()))
    else:
        with pytest.raises(RuntimeError, match="resources"):
            task.run(TaskInputs({}, frozenset()))


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "name",
        "image",
        "resources",
        "effectiveExecutionMode",
        "deploymentBackend",
        "concurrency",
    ],
)
def test_strict_cli_proofs_reject_wrong_identity_and_equal_bad_results(fault):
    from nanolab.tasks.cli_function import CliFunctionGetTask

    details = dict(DETAILS)
    if fault:
        details[fault] = "wrong"
    task = CliFunctionGetTask(
        MANIFEST,
        cli_argv=("cli",),
        executor=ReplyExecutor(json.dumps(details)),
        role="host",
    )
    if fault:
        with pytest.raises(RuntimeError, match=fault):
            task.run(TaskInputs.empty())
    else:
        task.run(TaskInputs.empty())


@pytest.mark.parametrize(
    "rows",
    [
        "fn\timage:test\nfn\timage:test\n",
        "fn image:test\n",
        "fn\t\n",
        "\timage:test\n",
        "fn\timage:test\textra\n",
    ],
)
def test_list_parser_rejects_ambiguous_rows(rows):
    from nanolab.tasks.cli_function import parse_cli_list

    with pytest.raises(RuntimeError, match="CLI list"):
        parse_cli_list(rows)


@pytest.mark.parametrize("rows", ["", "other\timage:test\n", "fn\timage:wrong\n"])
def test_list_requires_the_registered_name_and_image(rows):
    from nanolab.tasks.cli_function import CliFunctionListTask

    with pytest.raises(RuntimeError, match="fn"):
        CliFunctionListTask(
            {"fn": "image:test"},
            cli_argv=("cli",),
            executor=ReplyExecutor(rows),
            role="host",
        ).run(TaskInputs.empty())


def test_list_proves_delete_absence():
    from nanolab.tasks.cli_function import CliFunctionListTask

    for rows in ("", "fn\timage:test\n"):
        task = CliFunctionListTask(
            {},
            absent=("fn",),
            cli_argv=("cli",),
            executor=ReplyExecutor(rows),
            role="host",
        )
        if rows:
            with pytest.raises(RuntimeError, match="fn"):
                task.run(TaskInputs.empty())
        else:
            task.run(TaskInputs.empty())


@pytest.mark.parametrize("command", [0, 1])
@pytest.mark.parametrize("fault", ["identity", "boolean"])
def test_replica_proofs_reject_another_function_and_boolean_counts(command, fault):
    body = (
        {"function": "fn", "replicas": 1}
        if command == 0
        else {"name": "fn", "desiredReplicas": 1, "readyReplicas": 1}
    )
    body["function" if command == 0 else "name"] = (
        "other" if fault == "identity" else "fn"
    )
    if fault == "boolean":
        body["replicas" if command == 0 else "desiredReplicas"] = True
    task = function_replicas_tasks(
        "fn",
        replicas=1,
        cli_argv=("cli",),
        executor=ReplyExecutor(json.dumps(body)),
        role="host",
    )[command]
    with pytest.raises(RuntimeError):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize("kind", ["replace", "invalid-config"])
def test_negative_contract_requires_nonzero_exit(kind):
    executor = ReplyExecutor(
        "",
        stderr="rerun with --replace"
        if kind == "replace"
        else "configuration is invalid",
    )
    task = (
        function_replace_tasks(
            MANIFEST, cli_argv=("cli",), executor=executor, role="host"
        )[0]
        if kind == "replace"
        else runtime_config_tasks(
            "control-plane",
            patch={"rateMaxPerSecond": 999999},
            invalid_patch={"rateMaxPerSecond": -1},
            cli_argv=("cli",),
            executor=executor,
            role="host",
        )[2]
    )
    with pytest.raises(RuntimeError, match="exit"):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize("fault", [None, "ignored", "stale"])
def test_runtime_restore_uses_revision_envelope_and_checks_effective_values(fault):
    from nanolab.tasks.cli_function import runtime_config_patch_task

    reply = {
        "revision": 7 if fault == "stale" else 8,
        "effectiveConfig": {
            "namespaces": {
                "control-plane": {
                    "rateMaxPerSecond": 999999 if fault == "ignored" else 1000000
                }
            }
        },
    }
    executor = ReplyExecutor(json.dumps(reply))
    task = runtime_config_patch_task(
        "control-plane",
        {"rateMaxPerSecond": 1000000},
        expected_revision=7,
        cli_argv=("cli",),
        executor=executor,
        role="host",
    )
    if fault:
        with pytest.raises(RuntimeError):
            task.run(TaskInputs.empty())
    else:
        task.run(TaskInputs.empty())
        assert '"expectedRevision":7' in executor.seen[0].argv[-1]
        assert '"values":{"rateMaxPerSecond":1000000}' in executor.seen[0].argv[-1]


def test_config_file_probe_does_not_use_endpoint_override(tmp_path, monkeypatch):
    from nanolab.tasks.cli_function import cli_config_file_task

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            raw = json.dumps(DETAILS).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    binary = tmp_path / "cli"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """import os, sys, urllib.request, yaml
assert '--endpoint' not in sys.argv
assert 'NANOFAAS_ENDPOINT' not in os.environ
assert 'NANOFAAS_CONTEXT' not in os.environ
with open(sys.argv[sys.argv.index('--config') + 1]) as stream:
    config = yaml.safe_load(stream)
endpoint = config['contexts'][config['currentContext']]['endpoint']
with urllib.request.urlopen(endpoint + '/v1/functions/' + sys.argv[-1]) as reply:
    print(reply.read().decode())
"""
    )
    binary.chmod(0o755)
    monkeypatch.setenv("NANOFAAS_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("NANOFAAS_CONTEXT", "operator")
    try:
        task = cli_config_file_task(
            MANIFEST,
            binary=Path(binary),
            endpoint=f"http://127.0.0.1:{server.server_port}",
            executor=LocalCommandTaskExecutor(),
            role="host",
            cwd=tmp_path,
        )
        task.run(TaskInputs.empty())
        assert calls == ["/v1/functions/fn"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_strict_replica_status_requires_ready_desired_count():
    executor = ReplyExecutor('{"name":"fn","desiredReplicas":2,"readyReplicas":1}')
    with pytest.raises(RuntimeError, match="readyReplicas"):
        function_replicas_tasks(
            "fn",
            replicas=2,
            require_ready=True,
            cli_argv=("cli",),
            executor=executor,
            role="host",
        )[1].run(TaskInputs.empty())
