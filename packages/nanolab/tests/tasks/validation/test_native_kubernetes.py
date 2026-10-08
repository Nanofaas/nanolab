"""Exercise native API checks through curl against an independent HTTP server."""

import json
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.local import LocalCommandTaskExecutor
from sonata_tasks.execution.models import TaskResult

from nanolab.tasks.platform import PlatformFunction


@pytest.fixture
def native_api_server():
    state = {"fault": None, "calls": [], "registered": True}
    function = {
        "name": "fn",
        "image": "image:qualified",
        "requestedExecutionMode": "DEPLOYMENT",
        "effectiveExecutionMode": "DEPLOYMENT",
        "deploymentBackend": "k8s",
        "concurrency": 1,
        "timeoutMs": 5000,
    }

    class Handler(BaseHTTPRequestHandler):
        def handle_api(self):
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length)) if length else None
            state["calls"].append((self.command, self.path, data))
            status, body = 200, dict(function)
            key = self.command + " " + self.path
            if self.command == "DELETE":
                status, body = 204, None
                state["registered"] = False
            elif self.command == "POST" and self.path == "/v1/functions":
                status = 201
                state["registered"] = True
            elif self.path.endswith("/replicas"):
                body = (
                    {"function": "fn", "replicas": 1}
                    if self.command == "PUT"
                    else {
                        "name": "fn",
                        "desiredReplicas": 1,
                        "readyReplicas": 1,
                        "pods": [],
                    }
                )
            elif self.path.endswith(":invoke"):
                body = {
                    "status": "success",
                    "statusCode": None,
                    "output": {"wordCount": 3},
                }
            if state["fault"] == key:
                body = {
                    "name": "other",
                    "function": "other",
                    "status": "timeout",
                    "desiredReplicas": 1,
                    "readyReplicas": 1,
                }
                if self.command == "DELETE":
                    status = 200
            if state["fault"] == "success-error" and self.path.endswith(":invoke"):
                body = {
                    "status": "success",
                    "statusCode": 200,
                    "error": "upstream failure",
                }
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass

    for method in ("GET", "POST", "PATCH", "PUT", "DELETE"):
        setattr(Handler, "do_" + method, Handler.handle_api)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "GET /v1/functions/fn",
        "POST /v1/functions",
        "PATCH /v1/functions/fn",
        "GET /v1/functions/fn/replicas",
        "PUT /v1/functions/fn/replicas",
        "POST /v1/functions/fn:invoke",
        "DELETE /v1/functions/fn",
        "success-error",
    ],
)
@pytest.mark.parametrize("resource_endpoint", [False, True])
def test_native_api_validates_bodies_and_strict_delete(
    native_api_server, tmp_path, fault, resource_endpoint
):
    from sonata_engine import Resource

    from nanolab.tasks.validation.native_kubernetes import check_native_api

    url, state = native_api_server
    state["fault"] = fault
    endpoint = Resource(
        title="Acquired HTTP endpoint",
        acquire=lambda _inputs: url,
        release=lambda *_args: None,
    )
    inputs = TaskInputs._for_resources({endpoint: url}, {endpoint})
    fn = PlatformFunction(
        name="fn",
        image="image:qualified",
        payload='{"input":{"text":"one two one"}}',
        build_argv=("true",),
    )

    def run():
        check_native_api(
            inputs,
            function=fn,
            endpoint=endpoint if resource_endpoint else url,
            executor=LocalCommandTaskExecutor(),
            role="host",
            cwd=tmp_path,
            evidence_file=tmp_path / "api.jsonl",
        )

    if fault:
        with pytest.raises(RuntimeError):
            run()
    else:
        run()
        assert state["registered"] is True
        assert (
            sum(
                method == "POST" and path == "/v1/functions"
                for method, path, _ in state["calls"]
            )
            == 2
        )
        assert any(
            method == "PATCH" and data == {"concurrency": 1, "timeoutMs": 5000}
            for method, _, data in state["calls"]
        )
        assert (tmp_path / "api.jsonl").read_text()


def test_retry_preserves_failed_native_evidence(tmp_path):
    from nanolab.tasks.validation.native_kubernetes import _native_attempt_directory

    old = tmp_path / "native"
    old.mkdir()
    (old / "api.jsonl").write_text("failed response\n")
    fresh = _native_attempt_directory(tmp_path)
    assert fresh == old and fresh.is_dir()
    assert not (fresh / "api.jsonl").exists()
    archived = list((tmp_path / "native-attempts").glob("*/api.jsonl"))
    assert len(archived) == 1 and archived[0].read_text() == "failed response\n"


@pytest.mark.parametrize("replaced", [False, True])
def test_quota_patch_is_conditional_on_the_acquired_deployment(replaced):
    from nanolab.tasks.validation.native_kubernetes import configure_native_quota

    deployment = {
        "metadata": {
            "name": "nanofaas-control-plane",
            "uid": "owned",
            "resourceVersion": "7",
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "control-plane",
                            "env": [{"name": "KEEP_ME", "value": "unchanged"}],
                        }
                    ]
                }
            }
        },
    }

    @dataclass
    class Executor:
        seen: list = field(default_factory=list)

        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            self.seen.append(task.argv)
            response = dict(deployment)
            if replaced:
                response["metadata"] = {"uid": "foreign"}
            return TaskResult(
                task_id="", status="passed", return_code=0, stdout=json.dumps(response)
            )

    executor = Executor()

    def run():
        return configure_native_quota(
            TaskInputs.empty(),
            deployment=deployment,
            executor=executor,
            role="host",
            prefix=("kubectl", "-n", "owned-ns"),
        )

    if replaced:
        with pytest.raises(RuntimeError, match="replaced"):
            run()
        assert not any("patch" in argv for argv in executor.seen)
    else:
        run()
        argv = next(argv for argv in executor.seen if "patch" in argv)
        patch = json.loads(argv[argv.index("-p") + 1])
        assert {"op": "test", "path": "/metadata/uid", "value": "owned"} in patch
        assert {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": "7",
        } in patch
        env = patch[-1]["value"]
        assert env == [
            {"name": "KEEP_ME", "value": "unchanged"},
            {
                "name": "NANOFAAS_INVOCATION_CAPACITY_EXECUTIONS_PER_FUNCTION",
                "value": "1",
            },
        ]
        assert any("rollout" in argv for argv in executor.seen)


@pytest.mark.parametrize("mode", ["jvm", "native"])
def test_native_gate_reads_the_distribution_at_execution_time(tmp_path, mode):
    from sonata_engine import Resource

    from nanolab.tasks.recipes.workflow import (
        RecipeComponent,
        RecipeDistribution,
        RecipeImage,
    )
    from nanolab.tasks.validation.native_kubernetes import NativeKubernetesLifecycleTask

    report = RecipeDistribution(
        tmp_path / "distribution.json",
        "sha",
        "tag",
        None,
        (),
        (
            RecipeComponent(
                "control-plane",
                "control-plane",
                "java",
                mode,
                RecipeImage("cp:tag", "id", "built", None),
                "variant",
                None,
            ),
            RecipeComponent(
                "function",
                "word-stats",
                "java",
                "jvm",
                RecipeImage("fn:qualified", "id", "built", None),
                None,
                None,
            ),
        ),
    )
    distribution = Resource(
        title="Resolved at run time",
        acquire=lambda _inputs: report,
        release=lambda *_args: None,
    )

    class RejectCommands:
        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            raise AssertionError("JVM must not execute native diagnostics")

    task = NativeKubernetesLifecycleTask(
        distribution,
        namespace="owned",
        function=PlatformFunction("fn", "wrong-default:tag", "{}", ("true",)),
        endpoint="http://unused",
        executor=RejectCommands(),
        role="host",
        run_dir=tmp_path,
        function_component=("word-stats", "java"),
    )
    inputs = TaskInputs._for_resources({distribution: report}, {distribution})
    if mode == "jvm":
        task.run(inputs)
        assert not (tmp_path / "native").exists()
    else:
        with pytest.raises(RuntimeError, match=r"initial.*evidence"):
            task.run(inputs)


@pytest.mark.parametrize("fault", ["api", "quota", "probe"])
def test_native_failure_releases_only_the_resources_acquired_by_the_run(
    tmp_path, monkeypatch, fault
):
    from sonata_engine import Resource, TaskOutcome, Workflow

    import nanolab.tasks.validation.native_kubernetes as native
    from nanolab.tasks.recipes.workflow import (
        RecipeComponent,
        RecipeDistribution,
        RecipeImage,
    )

    released = []
    image = RecipeImage("qualified:tag", "id", "built", None)
    report = RecipeDistribution(
        tmp_path / "distribution.json",
        "sha",
        "tag",
        None,
        (),
        (
            RecipeComponent(
                "control-plane",
                "control-plane",
                "java",
                "native",
                image,
                "variant",
                None,
            ),
            RecipeComponent("function", "word-stats", "java", "jvm", image, None, None),
        ),
    )
    distribution = Resource(
        title="Owned distribution",
        acquire=lambda _inputs: report,
        release=lambda *_args: released.append("distribution"),
    )
    namespace = Resource(
        title="Owned namespace",
        acquire=lambda _inputs: "owned",
        release=lambda *_args: released.append("namespace"),
    )
    function_resource = Resource(
        title="Owned function",
        acquire=lambda _inputs: "fn",
        release=lambda *_args: released.append("function"),
    )
    # Unacquired foreign resources never participate in compensation.
    Resource(
        title="Foreign namespace",
        acquire=lambda _inputs: "foreign",
        release=lambda *_args: released.append("foreign"),
    )
    (tmp_path / "k8s-image-control-plane-control-plane.json").write_text(
        json.dumps({"deployment": {"metadata": {"uid": "owned"}}})
    )
    pod = {
        "metadata": {"uid": "pod", "name": "owned-pod"},
        "spec": {"nodeName": "node"},
        "status": {
            "containerStatuses": [
                {
                    "name": "control-plane",
                    "ready": True,
                    "containerID": "containerd://" + "a" * 64,
                }
            ]
        },
    }
    deployment = {"metadata": {"uid": "owned", "name": "nanofaas-control-plane"}}
    monkeypatch.setattr(
        native.NativeKubernetesLifecycleTask,
        "_snapshot",
        lambda *_args: (deployment, pod),
    )
    monkeypatch.setattr(
        native.NativeKubernetesLifecycleTask, "_logs", lambda *_args: "Started"
    )
    monkeypatch.setattr(
        native.RecipeMetadataCheckTask, "run", lambda *_args: TaskOutcome()
    )
    monkeypatch.setattr(
        native.RecipeKubernetesImageCheckTask, "run", lambda *_args: TaskOutcome()
    )
    calls = []

    def gate(stage):
        def invoke(*_args, **kwargs):
            calls.append(stage)
            if stage == "api":
                assert kwargs["function"].image == "qualified:tag"
            if stage == fault:
                raise RuntimeError(f"injected {fault} failure")
            return deployment

        return invoke

    monkeypatch.setattr(native, "check_native_api", gate("api"))
    monkeypatch.setattr(native, "configure_native_quota", gate("capacity"))
    monkeypatch.setattr(native.NativeKubernetesLifecycleTask, "_quota", gate("quota"))
    monkeypatch.setattr(native.NativeKubernetesLifecycleTask, "_probe", gate("probe"))
    task = native.NativeKubernetesLifecycleTask(
        distribution,
        namespace="owned",
        function=PlatformFunction("fn", "mutable-default:tag", "{}", ("true",)),
        endpoint="http://unused",
        executor=LocalCommandTaskExecutor(),
        role="host",
        run_dir=tmp_path,
        function_component=("word-stats", "java"),
    )
    workflow = Workflow(workflow_id="native-failure")
    workflow.add(task, requires=(distribution, namespace, function_resource))
    with pytest.raises(RuntimeError, match="injected"):
        workflow.run()
    assert set(released) == {"function", "namespace", "distribution"}
    assert not (tmp_path / "native/qualified.json").exists()
    if fault != "api":
        assert calls.index("api") < calls.index("capacity") < calls.index("quota")


@pytest.mark.parametrize(
    "fault", [None, "pid", "pod", "cri", "node", "missing-threads"]
)
def test_runtime_probe_links_node_commands_to_the_owned_container(
    tmp_path, monkeypatch, fault
):
    import base64
    from copy import deepcopy

    from sonata_engine import Resource
    from sonata_tasks.minikube import MinikubeTarget

    from nanolab.tasks.recipes.workflow import RecipeDistribution
    from nanolab.tasks.validation.native_kubernetes import NativeKubernetesLifecycleTask

    def unavailable(_inputs) -> RecipeDistribution:
        raise AssertionError("Probe does not build a distribution")

    distribution = Resource(
        title="Distribution", acquire=unavailable, release=lambda *_args: None
    )
    selected = MinikubeTarget(
        profile="owned-profile", context="owned-context", nodes=("owned-node",)
    )
    target = Resource(
        title="Owned target",
        acquire=lambda _inputs: selected,
        release=lambda *_args: None,
    )
    pod = {
        "metadata": {"uid": "owned-pod", "name": "cp"},
        "spec": {
            "nodeName": "owned-node",
            "containers": [
                {"name": "control-plane", "resources": {"limits": {"cpu": "1"}}}
            ],
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "control-plane",
                    "ready": True,
                    "containerID": "containerd://" + "a" * 64,
                }
            ]
        },
    }
    after = deepcopy(pod)
    if fault == "pod":
        after["metadata"]["uid"] = "replacement"
    if fault == "node":
        pod["spec"]["nodeName"] = "foreign-node"
    seen = []

    class Executor:
        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            seen.append(task)
            if "CRI process" in task.summary:
                pid = (
                    403
                    if fault == "pid" and task.summary.startswith("Recheck")
                    else 402
                )
                data = {
                    "status": {
                        "id": "b" * 64 if fault == "cri" else "a" * 64,
                        "state": "CONTAINER_RUNNING",
                        "labels": {"io.kubernetes.pod.uid": "owned-pod"},
                    },
                    "info": {"pid": pid},
                }
                stdout = json.dumps(data)
            elif "procfs" in task.summary:
                command = base64.b64encode(
                    b"/app/application\0-Dreactor.netty.ioWorkerCount=1\0"
                ).decode()
                stdout = (
                    f"before\t10412\ncommand\t{command}\n"
                    "thread\t403\treactor-http-ep\nafter\t10412\n"
                )
                if fault == "missing-threads":
                    stdout = f"before\t10412\ncommand\t{command}\nafter\t10412\n"
            elif "architecture" in task.summary:
                stdout = '{"status":{"nodeInfo":{"architecture":"arm64"}}}'
            else:
                raise AssertionError(task.summary)
            return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)

    task = NativeKubernetesLifecycleTask(
        distribution,
        namespace="owned-ns",
        function=PlatformFunction("fn", "fixed:tag", "{}", ("true",)),
        endpoint="http://unused",
        executor=Executor(),
        role="host",
        run_dir=tmp_path,
        function_component=("word-stats", "java"),
        target=target,
    )
    monkeypatch.setattr(task, "_snapshot", lambda *_args: ({}, after))
    (tmp_path / "native").mkdir()
    inputs = TaskInputs._for_resources({target: selected}, {target})

    def run():
        task._probe(inputs, pod, "owned", logs_before="Started", logs_after="Started")

    if fault:
        with pytest.raises(RuntimeError):
            run()
    else:
        run()
        receipt = json.loads((tmp_path / "native/runtime.json").read_text())
        assert receipt["before"]["pid"] == receipt["after"]["pid"] == 402
        assert receipt["before"]["containerId"] == "containerd://" + "a" * 64
        assert receipt["commandLine"][1] == "-Dreactor.netty.ioWorkerCount=1"
        assert all(
            spec.argv[:3] == ("timeout", "--kill-after=5s", "60s") for spec in seen
        )
        assert any(
            "owned-profile" in spec.argv and "owned-node" in spec.argv for spec in seen
        )
