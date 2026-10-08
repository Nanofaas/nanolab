"""The CLI workflow: drive the nanofaas binary through build, register and invoke."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.docker import DockerPushTask
from sonata_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.gradle import GradleTask

from nanolab.tasks.cli_function import (
    CliFunctionApplyTask,
    CliFunctionDeleteTask,
    CliFunctionGetTask,
    CliFunctionInvokeTask,
    CliFunctionListTask,
    cli_config_file_task,
    control_plane_contract_tasks,
    function_replace_tasks,
    function_replicas_tasks,
    function_update_task,
    runtime_config_readback_task,
    runtime_config_tasks,
)
from nanolab.tasks.deployment import DEFAULT_NAMESPACE
from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.function import function_resource
from nanolab.tasks.kubectl import k8s_deployment_readiness
from nanolab.tasks.manifest import FunctionManifest

# Mutable settings `fn update` patches, chosen to differ from every
# `FunctionManifest` default so a control plane that ignored the patch cannot
# still match.
FUNCTION_PATCH = {"concurrency": 3, "timeoutMs": 9000, "maxRetries": 1}

# `control-plane` is the namespace the runtime-config module registers itself, so
# it is available wherever the admin API is. The patched rate ceiling is a
# hair under the default 1000000: high enough that no lab workload notices,
# different enough that the control plane cannot pass by ignoring the patch.
RUNTIME_CONFIG_NAMESPACE = "control-plane"
RUNTIME_CONFIG_PATCH = {"rateMaxPerSecond": 999999}
INVALID_RUNTIME_CONFIG_PATCH = {"rateMaxPerSecond": -1}


@dataclass(frozen=True, slots=True)
class CliFunction:
    """One function the CLI workflow registers, invokes, and removes."""

    name: str
    image: str
    payload: str
    resources: dict[str, object] | None = None
    build_argv: tuple[str, ...] | None = None
    image_build_argv: tuple[str, ...] | None = None
    expected_output: object | None = None


@dataclass(frozen=True, slots=True)
class CliWorkflowRequest:
    """Everything the CLI workflow needs that is not an executor."""

    functions: tuple[CliFunction, ...]
    cli_role: ExecutionRole = "host"
    build_role: ExecutionRole = "host"
    namespace: str = DEFAULT_NAMESPACE
    endpoint: str = "http://127.0.0.1:8080"
    binary: str = "clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli"
    push_function_images: bool = False
    # The namespace `control-plane config` is exercised against, or None where the
    # control plane exposes no runtime-config namespace (the admin API is off, or
    # the build carries no module that registers one). Not a default: a workflow
    # that silently skipped the check would look exactly like one that passed it.
    runtime_config_namespace: str | None = None
    replicas: int = 2
    config_file: Path | None = None

    def __post_init__(self) -> None:
        """Reject a role other than host/stack, or an empty function list."""
        if self.cli_role not in ("host", "stack"):
            raise ValueError("CLI workflow can run only on host or stack")
        if self.build_role not in ("host", "stack"):
            raise ValueError("CLI workflow can run only on host or stack")
        if not self.functions:
            raise ValueError("CLI workflow requires at least one function")


def _cli_argv(request: CliWorkflowRequest, *arguments: str) -> tuple[str, ...]:
    # The CLI has no --namespace flag; the namespace only serves k8s readiness checks.
    config = ("--config", str(request.config_file)) if request.config_file else ()
    return (request.binary, *config, "--endpoint", request.endpoint, *arguments)


def _manifest(function: CliFunction) -> FunctionManifest:
    return FunctionManifest(
        name=function.name, image=function.image, resources=function.resources
    )


def _function_resource(
    request: CliWorkflowRequest,
    function: CliFunction,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    *,
    readiness_timeout_seconds: int | None = None,
    requires: tuple[Resource[Any], ...] = (),
    strict: bool = False,
) -> Resource[None]:
    """Build the registered function as an acquire/release pair.

    Only the commands are CLI-specific: applying a manifest through the nanofaas
    binary. The lifecycle around them — splicing, compensation on a partial
    register, readiness — belongs to `function_resource`, which every other
    workflow reuses with its own register and delete commands.
    """
    manifest = _manifest(function)
    prefix = _cli_argv(request)
    apply_task = CliFunctionApplyTask(
        manifest, cli_argv=prefix, executor=executor, role=request.cli_role, cwd=cwd
    )
    delete_task = CliFunctionDeleteTask(
        function.name,
        verify_absence=strict,
        cli_argv=prefix,
        executor=executor,
        role=request.cli_role,
        cwd=cwd,
    )
    ready_tasks = (
        k8s_deployment_readiness(
            deployment=f"fn-{function.name}",
            namespace=request.namespace,
            executor=executor,
            role=request.cli_role,
            options=CommandOptions(timeout_seconds=readiness_timeout_seconds, cwd=cwd),
        )
        if readiness_timeout_seconds is not None
        else ()
    )

    return function_resource(
        name=function.name,
        register=apply_task,
        delete=delete_task,
        readiness=ready_tasks,
        requires=requires,
    )


def build_cli_workflow(  # NOSONAR (S3776): workflow assembly mirrors resource lifecycle
    request: CliWorkflowRequest,
    bindings: RoleBindings,
    *,
    workflow_id: str = "cli",
    cwd: Path | None = None,
    control_plane_build_argv: tuple[str, ...] | None = None,
    requires: tuple[Resource[Any], ...] = (),
    bootstrap: tuple[CommandTask, ...] = (),
    bootstrap_requires: tuple[Resource[Any], ...] = (),
    function_requires: tuple[Resource[Any], ...] = (),
    push_requires: tuple[Resource[Any], ...] = (),
    readiness_timeout_seconds: int | None = None,
) -> Workflow:
    """Build the CLI end-to-end workflow: build, register, list, invoke, remove.

    Build commands do not require the control plane. `requires` wraps only commands
    that use its API, so selection of a build task stays offline.

    `bootstrap` is an ordered sequence of tasks (e.g. VM provisioning) spliced in
    after the build tasks and before the CLI itself runs; `bootstrap_requires`
    are the resources those tasks declare (typically a VM resource), which also
    controls where that resource's acquire/release land.

    `function_requires` (e.g. a Helm release) becomes a real dependency of every
    function's own resource, so a slice that keeps one function keeps it too.
    """
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    workflow.add(
        GradleTask(
            ":nanofaas-cli:installDist",
            title="Build nanofaas-cli",
            executor=executor,
            role=request.build_role,
            options=CommandOptions(cwd=cwd),
        )
    )
    if control_plane_build_argv is not None:
        workflow.add(
            CommandTask(
                title="Build local control plane",
                argv=control_plane_build_argv,
                executor=executor,
                role=request.build_role,
                options=CommandOptions(cwd=cwd),
                semantic_key="nanolab.cli.build-control-plane:v1",
            )
        )
    for function in request.functions:
        if function.build_argv is not None:
            workflow.add(
                CommandTask(
                    title=f"Build application artifact: {function.name}"
                    if function.image_build_argv is not None
                    else f"Build image {function.name}",
                    argv=function.build_argv,
                    executor=executor,
                    role=request.build_role,
                    options=CommandOptions(cwd=cwd),
                )
            )
        if function.image_build_argv is not None:
            workflow.add(
                CommandTask(
                    title=f"Build image {function.name}",
                    argv=function.image_build_argv,
                    executor=executor,
                    role=request.build_role,
                    options=CommandOptions(cwd=cwd),
                )
            )
        if request.push_function_images and function.build_argv is not None:
            workflow.add(
                DockerPushTask(
                    image=function.image,
                    executor=executor,
                    role=request.build_role,
                    title=f"Push image {function.image}",
                    options=CommandOptions(cwd=cwd),
                ),
                requires=push_requires,
            )
    for bootstrap_task in bootstrap:
        workflow.add(bootstrap_task, requires=bootstrap_requires)
    add_cli_contract(
        workflow,
        request,
        executor=executor,
        cwd=cwd,
        requires=requires,
        function_requires=function_requires,
        readiness_timeout_seconds=readiness_timeout_seconds,
    )
    return workflow


def add_cli_contract(
    workflow: Workflow,
    request: CliWorkflowRequest,
    *,
    executor: CommandTaskExecutor,
    cwd: Path | None = None,
    requires: tuple[Resource[Any], ...] = (),
    function_requires: tuple[Resource[Any], ...] = (),
    readiness_timeout_seconds: int | None = None,
    strict: bool = False,
    runtime_config_patch: dict[str, Any] | None = None,
) -> None:
    """Assemble only the shared API contract, without building or deploying it."""
    resources = tuple(
        _function_resource(
            request,
            function,
            executor,
            cwd,
            readiness_timeout_seconds=readiness_timeout_seconds,
            requires=function_requires,
            strict=strict,
        )
        for function in request.functions
    )
    list_task = (
        CliFunctionListTask(
            {fn.name: fn.image for fn in request.functions},
            cli_argv=_cli_argv(request),
            executor=executor,
            role=request.cli_role,
            cwd=cwd,
        )
        if strict
        else CommandTask(
            title="List functions",
            argv=_cli_argv(request, "fn", "list"),
            executor=executor,
            role=request.cli_role,
            options=CommandOptions(cwd=cwd),
        )
    )
    workflow.add(list_task, requires=(*requires, *resources))
    prefix = _cli_argv(request)
    role = request.cli_role
    for task in control_plane_contract_tasks(
        cli_argv=prefix, executor=executor, role=role, cwd=cwd
    ):
        workflow.add(task, requires=requires)
    if request.runtime_config_namespace is not None:
        for task in runtime_config_tasks(
            request.runtime_config_namespace,
            patch=runtime_config_patch
            if runtime_config_patch is not None
            else RUNTIME_CONFIG_PATCH,
            invalid_patch=INVALID_RUNTIME_CONFIG_PATCH,
            cli_argv=prefix,
            executor=executor,
            role=role,
            cwd=cwd,
        ):
            workflow.add(task, requires=requires)
        if strict:
            workflow.add(
                runtime_config_readback_task(
                    request.runtime_config_namespace,
                    runtime_config_patch
                    if runtime_config_patch is not None
                    else RUNTIME_CONFIG_PATCH,
                    cli_argv=prefix,
                    executor=executor,
                    role=role,
                    cwd=cwd,
                ),
                requires=requires,
            )
    for function, resource in zip(request.functions, resources, strict=False):
        function_requires_resource = (*requires, resource)
        invoke = CliFunctionInvokeTask(
            function.name,
            payload=function.payload,
            expected_output=function.expected_output,
            cli_argv=prefix,
            executor=executor,
            role=role,
            cwd=cwd,
        )
        if strict:
            workflow.add(
                CliFunctionGetTask(
                    _manifest(function),
                    cli_argv=prefix,
                    executor=executor,
                    role=role,
                    cwd=cwd,
                ),
                requires=function_requires_resource,
            )
            workflow.add(
                cli_config_file_task(
                    _manifest(function),
                    binary=Path(request.binary),
                    endpoint=request.endpoint,
                    executor=executor,
                    role=role,
                    cwd=cwd,
                ),
                requires=function_requires_resource,
            )
        else:
            workflow.add(invoke, requires=function_requires_resource)
        workflow.add(
            function_update_task(
                function.name,
                patch=FUNCTION_PATCH,
                cli_argv=prefix,
                executor=executor,
                role=role,
                cwd=cwd,
            ),
            requires=function_requires_resource,
        )
        if strict:
            updated = CliFunctionGetTask(
                _manifest(function),
                patch=FUNCTION_PATCH,
                cli_argv=prefix,
                executor=executor,
                role=role,
                cwd=cwd,
            )
            updated.title = f"Get updated {function.name}"
            workflow.add(updated, requires=function_requires_resource)
        scale, get_replicas = function_replicas_tasks(
            function.name,
            replicas=request.replicas,
            require_ready=strict,
            cli_argv=prefix,
            executor=executor,
            role=role,
            cwd=cwd,
        )
        workflow.add(scale, requires=function_requires_resource)
        if strict:
            for task in k8s_deployment_readiness(
                deployment=f"fn-{function.name}",
                namespace=request.namespace,
                executor=executor,
                role=role,
                options=CommandOptions(
                    cwd=cwd, timeout_seconds=readiness_timeout_seconds or 45
                ),
            ):
                task.title = f"Scaled {task.title}"
                workflow.add(task, requires=function_requires_resource)
        workflow.add(get_replicas, requires=function_requires_resource)
        if strict:
            workflow.add(invoke, requires=function_requires_resource)
        # Last of the three: replacing re-registers the function from its manifest,
        # which discards the patch and the replica count the two steps above set.
        for task in function_replace_tasks(
            _manifest(function),
            cli_argv=prefix,
            executor=executor,
            role=role,
            cwd=cwd,
        ):
            workflow.add(task, requires=function_requires_resource)
        if strict:
            replaced = CliFunctionGetTask(
                _manifest(function),
                patch={"queueSize": _manifest(function).queue_size + 1},
                cli_argv=prefix,
                executor=executor,
                role=role,
                cwd=cwd,
            )
            replaced.title = f"Get replaced {function.name}"
            workflow.add(replaced, requires=function_requires_resource)
