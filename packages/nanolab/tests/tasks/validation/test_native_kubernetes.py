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
                        "function": "fn",
                        "desiredReplicas": 1,
                        "readyReplicas": 1,
                        "pods": [],
                    }
                )
            elif self.path.endswith(":invoke"):
                body = {
                    "status": "success",
                    "statusCode": 200,
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
