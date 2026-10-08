"""Register, update and invoke functions through the nanofaas CLI."""

from __future__ import annotations

import json
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from sonata_tasks.command import CommandTask
from sonata_tasks.core.fingerprint import fingerprint_digest
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import TaskResult

from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.invocation import verify_invocation
from nanolab.tasks.manifest import FunctionManifest

# Stands in for the temp file the script creates on the target. Chosen so
# `shlex.quote` leaves it alone: it is always its own word, never a substring of
# one, so substituting the shell variable back in cannot corrupt a neighbour.
FILE = "@FILE@"


def _semantic_key(kind: str, **payload: object) -> str:
    return f"{kind}:{fingerprint_digest(payload)}"


def _script_with_file(content: str, *commands: tuple[str, ...]) -> str:
    """Shell that writes `content` to a temp file and runs `commands` against it.

    The file is created with `mktemp` on the target on purpose. Writing it here
    with `tempfile` would work when the CLI runs on this host and silently break
    when it runs inside a VM, which would never see a local path.

    Commands are chained with `&&`: a probe that reads back what the previous
    command wrote must not report success when the write itself failed.
    """
    rendered = " && ".join(
        " ".join(shlex.quote(value) for value in command).replace(FILE, '"$manifest"')
        for command in commands
    )
    return (
        f"manifest=$(mktemp); trap 'rm -f \"$manifest\"' EXIT; "
        f"printf '%s' {shlex.quote(content)} > \"$manifest\"; " + rendered
    )


def _apply_script(manifest: FunctionManifest, cli_argv: tuple[str, ...]) -> str:
    return _script_with_file(
        manifest.json(), (*cli_argv, "fn", "apply", "--file", FILE)
    )


def _json_stdout(result: TaskResult) -> dict[str, Any]:
    """Parse a CLI command's stdout as the JSON object it should have printed.

    A command that answered with anything else is a contract break, so the raw
    prefix of what it did print is quoted back in the error.
    """
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(
            f"CLI output was not JSON: {result.stdout[:200]!r}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"CLI output was not a JSON object: {result.stdout[:200]!r}")
    return payload


def _expect(payload: dict[str, Any], field: str, expected: object) -> None:
    actual = payload.get(field)
    if actual != expected or (type(expected) is int and type(actual) is not int):
        raise RuntimeError(f"{field} is {payload.get(field)!r}, expected {expected!r}")


def parse_cli_list(stdout: str) -> dict[str, str]:
    """Read the actual name/image TSV, rejecting ambiguous or partial rows."""
    rows: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not all(parts) or parts[0] in rows:
            raise RuntimeError(f"CLI list has an invalid or duplicate row: {line!r}")
        rows[parts[0]] = parts[1]
    return rows


class CliFunctionListTask(CommandTask):
    """Verify registered name/image pairs or absence after deletion."""

    def __init__(
        self,
        expected: dict[str, str],
        *,
        absent: tuple[str, ...] = (),
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        """Configure the command and its independent expected-state proof."""

        def verify(result: TaskResult) -> None:
            rows = parse_cli_list(result.stdout)
            for name, image in expected.items():
                _expect(rows, name, image)
            for name in absent:
                if name in rows:
                    raise RuntimeError(f"CLI list still contains deleted {name}")

        super().__init__(
            title="List functions",
            argv=(*cli_argv, "fn", "list"),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.cli-function.list:v1", expected=expected, absent=absent
            ),
            verify=verify,
        )


class CliFunctionGetTask(CommandTask):
    """Prove the function's manifest, controlled changes and deployment mode."""

    def __init__(
        self,
        manifest: FunctionManifest,
        *,
        patch: dict[str, Any] | None = None,
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        """Configure a get command using the complete selected manifest."""
        expected = {**manifest.body(), **(patch or {})}
        expected.pop("executionMode")
        expected.update(
            requestedExecutionMode=manifest.execution_mode,
            effectiveExecutionMode=manifest.execution_mode,
            deploymentBackend="k8s",
        )

        def verify(result: TaskResult) -> None:
            details = _json_stdout(result)
            for key, value in expected.items():
                _expect(details, key, value)

        super().__init__(
            title=f"Get {manifest.name}",
            argv=(*cli_argv, "fn", "get", manifest.name),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.cli-function.get:v1", expected=expected
            ),
            verify=verify,
        )


def cli_config_file_task(
    manifest: FunctionManifest,
    *,
    binary: Path,
    endpoint: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> CommandTask:
    """Exercise YAML config loading with endpoint/context environment removed."""
    config = yaml.safe_dump(
        {"currentContext": "owned", "contexts": {"owned": {"endpoint": endpoint}}}
    )
    probe = CliFunctionGetTask(
        manifest, cli_argv=(str(binary),), executor=executor, role=role, cwd=cwd
    )
    return CommandTask(
        title=f"CLI config file for {manifest.name}",
        argv=(
            "bash",
            "-lc",
            _script_with_file(
                config,
                (
                    "env",
                    "-u",
                    "NANOFAAS_ENDPOINT",
                    "-u",
                    "NANOFAAS_CONTEXT",
                    str(binary),
                    "--config",
                    FILE,
                    "fn",
                    "get",
                    manifest.name,
                ),
            ),
        ),
        executor=executor,
        role=role,
        options=CommandOptions(cwd=cwd),
        semantic_key=_semantic_key(
            "nanolab.cli-function.config-file:v1",
            manifest=manifest.body(),
            endpoint=endpoint,
        ),
        verify=probe.verify,
    )


class CliFunctionApplyTask(CommandTask):
    """Register a function by applying a manifest through the nanofaas CLI.

    `cli_argv` is the invocation prefix — binary plus its global flags — so this
    task stays out of the business of knowing how the CLI is addressed.
    """

    def __init__(
        self,
        manifest: FunctionManifest,
        *,
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        """Build the `fn apply` command that registers `manifest`.

        `cli_argv` is the invocation prefix, so this task never has to know how
        the CLI is addressed.
        """
        super().__init__(
            title=f"Apply {manifest.name}",
            argv=("bash", "-lc", _apply_script(manifest, cli_argv)),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
        )


class CliFunctionDeleteTask(CommandTask):
    """Remove a function through the nanofaas CLI.

    Only exit 0 is accepted: the CLI already exits 0 when the function is absent,
    so tolerating 1 as well would hide genuine cleanup failures.
    """

    def __init__(
        self,
        name: str,
        *,
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
        verify_absence: bool = False,
    ) -> None:
        """Build the `fn delete` command that removes `name`."""

        def verify(result: TaskResult) -> None:
            if name in parse_cli_list(result.stdout):
                raise RuntimeError(f"CLI list still contains deleted {name}")

        command = (*cli_argv, "fn", "delete", name)
        argv = (
            (
                "bash",
                "-lc",
                shlex.join(command) + " && " + shlex.join((*cli_argv, "fn", "list")),
            )
            if verify_absence
            else command
        )
        super().__init__(
            title=f"Delete {name}",
            argv=argv,
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            verify=verify if verify_absence else None,
            semantic_key=_semantic_key(
                "nanolab.cli-function.delete-absence:v1", name=name
            )
            if verify_absence
            else None,
        )


class CliFunctionInvokeTask(CommandTask):
    """Invoke a function through the nanofaas CLI and check what came back.

    The CLI's twin of `HttpFunctionInvokeTask`: different transport, same
    response, so both share `verify_invocation`.
    """

    def __init__(
        self,
        name: str,
        *,
        payload: str,
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
        expected_output: object | None = None,
    ) -> None:
        """Build the `fn invoke` command, checking the response on the way back."""

        def verify(result: TaskResult) -> None:
            verify_invocation(result)
            if expected_output is not None:
                _expect(_json_stdout(result), "output", expected_output)

        super().__init__(
            title=f"Invoke {name}",
            argv=(*cli_argv, "invoke", name, "--data", payload),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.cli-function.invoke:v3",
                name=name,
                payload=payload,
                expected_output=expected_output,
            ),
            verify=verify,
        )


def control_plane_contract_tasks(
    *,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """`control-plane info` and `contract`, checked against what this workflow uses.

    The capabilities are asserted, not merely printed: every optional command
    below (`fn update`, `fn replicas`) refuses to run when the control plane does
    not advertise it, so a build that lost the endpoint would otherwise surface
    as a puzzling "not supported by this control-plane build" three tasks later.
    """

    def verify_info(result: TaskResult) -> None:
        capabilities = _json_stdout(result).get("capabilities")
        if not isinstance(capabilities, dict):
            raise RuntimeError(
                f"control-plane info carried no capabilities: {result.stdout[:200]!r}"
            )
        missing = [
            name
            for name in ("functionUpdate", "replicas")
            if capabilities.get(name) is not True
        ]
        if missing:
            raise RuntimeError(f"control plane does not advertise {', '.join(missing)}")

    def verify_contract(result: TaskResult) -> None:
        if "openapi:" not in result.stdout or "/v1/functions" not in result.stdout:
            raise RuntimeError(
                f"contract is not an OpenAPI document: {result.stdout[:200]!r}"
            )

    return (
        CommandTask(
            title="Control-plane info",
            argv=(*cli_argv, "control-plane", "info"),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key="nanolab.cli-function.info:v1",
            verify=verify_info,
        ),
        CommandTask(
            title="Control-plane contract",
            argv=(*cli_argv, "control-plane", "contract"),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key="nanolab.cli-function.contract:v1",
            verify=verify_contract,
        ),
    )


def function_update_task(
    name: str,
    *,
    patch: dict[str, Any],
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> CommandTask:
    """`fn update` followed by the `fn get` that proves it landed.

    One task, not two: `fn update` prints nothing on success, so the chained
    `fn get` owns the whole stdout and the patch can be verified where it is
    applied. A patch that the control plane accepted but ignored is exactly the
    failure this exists to catch.
    """

    def verify(result: TaskResult) -> None:
        details = _json_stdout(result)
        for field, expected in patch.items():
            _expect(details, field, expected)

    return CommandTask(
        title=f"Update {name}",
        argv=(
            "bash",
            "-lc",
            _script_with_file(
                yaml.safe_dump(patch),
                (*cli_argv, "fn", "update", name, "--file", FILE),
                (*cli_argv, "fn", "get", name),
            ),
        ),
        executor=executor,
        role=role,
        options=CommandOptions(cwd=cwd),
        semantic_key=_semantic_key(
            "nanolab.cli-function.update:v3", name=name, patch=patch
        ),
        verify=verify,
    )


def function_replicas_tasks(
    name: str,
    *,
    replicas: int,
    require_ready: bool = False,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """`fn replicas set` and the `get` that reads the desired count back.

    Two tasks rather than one chained script: `set` prints its own JSON, so a
    chain would hand the verifier two documents on one stdout.
    """

    def verify_set(result: TaskResult) -> None:
        payload = _json_stdout(result)
        _expect(payload, "function", name)
        _expect(payload, "replicas", replicas)

    def verify_get(result: TaskResult) -> None:
        payload = _json_stdout(result)
        _expect(payload, "name", name)
        _expect(payload, "desiredReplicas", replicas)
        ready = payload.get("readyReplicas")
        if type(ready) is not int or not 0 <= ready <= replicas:
            raise RuntimeError(f"readyReplicas is not a valid count: {ready!r}")
        if require_ready:
            _expect(payload, "readyReplicas", replicas)

    return (
        CommandTask(
            title=f"Scale {name}",
            argv=(*cli_argv, "fn", "replicas", "set", name, str(replicas)),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.cli-function.scale:v3", name=name, replicas=replicas
            ),
            verify=verify_set,
        ),
        CommandTask(
            title=f"Replicas of {name}",
            argv=(*cli_argv, "fn", "replicas", "get", name),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.cli-function.replicas:v3",
                name=name,
                replicas=replicas,
                require_ready=require_ready,
            ),
            verify=verify_get,
        ),
    )


def function_replace_tasks(
    manifest: FunctionManifest,
    *,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """Both halves of the `--replace` contract, on one changed immutable field.

    `queueSize` is the field on purpose: the CLI treats it as immutable, and
    unlike the image or the resources it changes nothing the running function
    does, so the check costs a re-register and not a broken deployment.

    The refusal is the first half and is the one worth having: without it a
    changed immutable field would silently delete and re-create a function,
    which is what `--replace` exists to make someone type out loud.
    """
    changed = replace(manifest, queue_size=manifest.queue_size + 1)
    script = _script_with_file(
        changed.json(), (*cli_argv, "fn", "apply", "--file", FILE)
    )
    replace_script = _script_with_file(
        changed.json(),
        (*cli_argv, "fn", "apply", "--replace", "--file", FILE),
        (*cli_argv, "fn", "get", manifest.name),
    )

    def verify_refusal(result: TaskResult) -> None:
        if "--replace" not in result.stderr:
            raise RuntimeError(
                "apply of a changed immutable field failed without asking "
                f"for --replace: {(result.stderr or result.stdout)[:200]!r}"
            )
        if result.return_code == 0:
            raise RuntimeError("unreplaced immutable change exited successfully")

    def verify_replaced(result: TaskResult) -> None:
        _expect(_json_stdout(result), "queueSize", changed.queue_size)

    return (
        CommandTask(
            title=f"Refuse unreplaced change to {manifest.name}",
            argv=("bash", "-lc", script),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd, expected_exit_codes=frozenset({0, 1})),
            semantic_key=_semantic_key(
                "nanolab.cli-function.replace-refusal:v3", manifest=changed.body()
            ),
            verify=verify_refusal,
        ),
        CommandTask(
            title=f"Replace {manifest.name}",
            argv=("bash", "-lc", replace_script),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.cli-function.replace:v2", manifest=changed.body()
            ),
            verify=verify_replaced,
        ),
    )


def runtime_config_patch_task(
    namespace: str,
    values: dict[str, Any],
    *,
    expected_revision: int | None = None,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> CommandTask:
    """Patch controlled values, optionally guarding a baseline restoration revision."""
    content = (
        {"expectedRevision": expected_revision, "values": values}
        if expected_revision is not None
        else values
    )

    def verify(result: TaskResult) -> None:
        payload = _json_stdout(result)
        revision = payload.get("revision")
        if type(revision) is not int or (
            expected_revision is not None and revision != expected_revision + 1
        ):
            raise RuntimeError("runtime config patch returned an invalid revision")
        applied = (
            payload.get("effectiveConfig", {}).get("namespaces", {}).get(namespace, {})
        )
        for key, value in values.items():
            _expect(applied, key, value)

    return CommandTask(
        title="Patch runtime config",
        argv=(
            "bash",
            "-lc",
            _script_with_file(
                json.dumps(content, separators=(",", ":")),
                (
                    *cli_argv,
                    "control-plane",
                    "config",
                    "patch",
                    namespace,
                    "--file",
                    FILE,
                ),
            ),
        ),
        executor=executor,
        role=role,
        options=CommandOptions(cwd=cwd),
        semantic_key=_semantic_key(
            "nanolab.cli-function.runtime-patch:v1",
            namespace=namespace,
            values=values,
            expected_revision=expected_revision,
        ),
        verify=verify,
    )


def runtime_config_readback_task(
    namespace: str,
    values: dict[str, Any],
    *,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> CommandTask:
    """Read effective values independently of the mutation response."""

    def verify(result: TaskResult) -> None:
        payload = _json_stdout(result)
        if type(payload.get("revision")) is not int:
            raise RuntimeError("runtime config readback returned no integer revision")
        applied = payload.get("namespaces", {}).get(namespace, {})
        for key, value in values.items():
            _expect(applied, key, value)

    return CommandTask(
        title="Runtime config readback",
        argv=(*cli_argv, "control-plane", "config", "get"),
        executor=executor,
        role=role,
        options=CommandOptions(cwd=cwd),
        semantic_key=_semantic_key(
            "nanolab.cli-function.runtime-readback:v1",
            namespace=namespace,
            values=values,
        ),
        verify=verify,
    )


def runtime_config_tasks(
    namespace: str,
    *,
    patch: dict[str, Any],
    invalid_patch: dict[str, Any],
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """`control-plane config get|validate|patch` against a live namespace.

    The invalid patch is checked too: `validate` that never rejects anything is
    indistinguishable from `validate` that is not wired to the namespace at all.
    """
    prefix = (*cli_argv, "control-plane", "config")

    def verify_snapshot(result: TaskResult) -> None:
        snapshot = _json_stdout(result)
        if type(snapshot.get("revision")) is not int:
            raise RuntimeError(
                f"runtime config carried no revision: {result.stdout[:200]!r}"
            )
        if namespace not in (snapshot.get("namespaces") or {}):
            raise RuntimeError(f"runtime config has no '{namespace}' namespace")

    def verify_valid(result: TaskResult) -> None:
        _expect(_json_stdout(result), "valid", True)

    def verify_rejected(result: TaskResult) -> None:
        if "is invalid" not in result.stderr:
            raise RuntimeError(
                "invalid runtime config was not reported as invalid: "
                f"{(result.stderr or result.stdout)[:200]!r}"
            )
        if result.return_code == 0:
            raise RuntimeError("invalid runtime config exited successfully")

    def config_task(
        title: str,
        *arguments: str,
        content: dict[str, Any] | None = None,
        verify: Any,
        exit_codes: frozenset[int] = frozenset({0}),
    ) -> CommandTask:
        argv = (
            (
                "bash",
                "-lc",
                _script_with_file(
                    json.dumps(content, separators=(",", ":")),
                    (*prefix, *arguments, "--file", FILE),
                ),
            )
            if content is not None
            else (*prefix, *arguments)
        )
        return CommandTask(
            title=title,
            argv=argv,
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd, expected_exit_codes=exit_codes),
            semantic_key=_semantic_key(
                "nanolab.cli-function.runtime-config:v3",
                title=title,
                namespace=namespace,
                content=content,
                patch=patch,
                invalid_patch=invalid_patch,
                exit_codes=exit_codes,
            ),
            verify=verify,
        )

    return (
        config_task("Runtime config snapshot", "get", verify=verify_snapshot),
        config_task(
            "Validate runtime config",
            "validate",
            namespace,
            content=patch,
            verify=verify_valid,
        ),
        config_task(
            "Reject invalid runtime config",
            "validate",
            namespace,
            content=invalid_patch,
            verify=verify_rejected,
            exit_codes=frozenset({0, 1}),
        ),
        runtime_config_patch_task(
            namespace,
            patch,
            cli_argv=cli_argv,
            executor=executor,
            role=role,
            cwd=cwd,
        ),
    )
