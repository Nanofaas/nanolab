"""Synthetic workflow/HTTP tests: never invoke Docker, builds, Git or network."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest

from nanolab.tasks.soak import prerequisite_platform as platform
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.prerequisite_runtime import (
    UnsupportedPreflightError,
    make_live_runner,
)


class Config:
    artifact_limit_bytes = 1024 * 1024
    roles: ClassVar[dict[str, SimpleNamespace]] = {
        "control-plane": SimpleNamespace(
            runtime="jvm",
            runtime_options=[],
            expected_cpu=1,
            memory_limit_bytes=256 * 1024**2,
        ),
        "echo": SimpleNamespace(
            runtime="node",
            runtime_options=[],
            expected_cpu=1,
            memory_limit_bytes=256 * 1024**2,
        ),
    }

    def model_copy(self, *, deep):
        return deepcopy(self)


@dataclass
class Prepared:
    config: object
    images: dict
    run_id: str = "soak-measured"
    evidence_dir: Path = Path("/unused")
    writer: object = None


@pytest.fixture
def case(tmp_path, monkeypatch):
    images = {
        "control-plane": "cp@sha256:" + "a" * 64,
        "echo": "sdk@sha256:" + "b" * 64,
    }
    prepared = Prepared(Config(), images)
    factory = platform.make_prerequisite_platform_factory(
        prepared=prepared, ownership_root=tmp_path / "owned", bindings=object()
    )
    effective = platform.freeze_effective_config(
        prepared,
        retention_s={
            "unkeyed-sync-outcome": 30,
            "terminal-key-and-readable-outcome": 300,
            "live-key-and-execution": 1800,
        },
    )
    payload = tmp_path / "payload.json"
    payload.write_text('{"input":"hello"}')
    inputs = {
        "images": images,
        "relevant_config": {
            "sync": {
                "function": "echo",
                "role": "echo",
                "request": {"input": "hello"},
                "expected_output": "hello",
                "effective_config": effective,
            }
        },
        "settlement": {
            role: {"timers": {"limit": 0, "retention_s": 30}} for role in images
        },
        "payload": platform.describe_artifact(payload),
    }
    calls, deployments = [], []
    manifest = {
        "name": "echo",
        "image": images["echo"],
        "executionMode": "EXTERNAL",
        "endpointUrl": "http://function-1:8080/invoke",
    }

    def deploy(isolated, root, **kwargs):
        deployments.append(isolated)
        assert isolated.images == images
        assert (
            isolated.config.roles["echo"].memory_limit_bytes
            == prepared.config.roles["echo"].memory_limit_bytes
        )
        targets = tuple(
            Target(
                role,
                str(index) * 64,
                100 + index,
                "2026-01-01T00:00:00Z",
                image,
                isolated.config.roles[role].runtime,
            )
            for index, (role, image) in enumerate(images.items())
        )
        return SimpleNamespace(
            request=SimpleNamespace(
                functions=(
                    SimpleNamespace(
                        name="echo",
                        manifest=lambda: SimpleNamespace(
                            body=lambda: deepcopy(manifest)
                        ),
                    ),
                )
            ),
            project=SimpleNamespace(name=isolated.run_id),
            ownership=object(),
            api_endpoint="http://127.0.0.1:50100",
            metrics_endpoints={
                "control-plane": "http://127.0.0.1:50101/actuator/prometheus",
                "echo": "http://127.0.0.1:50102/metrics",
            },
            discover=lambda: targets,
            diagnostic_inputs={},
        )

    def workflow(request, bindings, *, measurement, **kwargs):
        class Workflow:
            def run(self, **kwargs):
                calls.append("acquire")
                try:
                    return measurement.run(None)
                finally:
                    calls.append("release")

        return Workflow()

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/actuator/configprops":
            return httpx.Response(
                200,
                json={
                    "contexts": {
                        "app": {
                            "beans": {
                                "store-ExecutionStoreProperties": {
                                    "properties": {
                                        "syncTtl": "PT30S",
                                        "ttl": "PT5M",
                                        "maxLifetime": "PT30M",
                                    }
                                }
                            }
                        }
                    }
                },
            )
        if request.url.path == "/v1/functions/echo":
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, text="runtime_active_handlers 0\n")

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        platform.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(platform, "create_local_deployment", deploy)
    monkeypatch.setattr(platform, "compose_frozen_soak_workflow", workflow)
    monkeypatch.setattr(
        platform,
        "observe_local_process",
        lambda target: deepcopy(effective["roles"][target.role]),
    )
    return factory, inputs, calls, deployments


def test_inert_factory_and_real_observer_projection(case):
    factory, inputs, calls, deployments = case
    assert not factory.root.exists()

    async def run():
        async with factory("sync", "one", inputs) as live:
            assert await live.observe_identities() == inputs["images"]
            assert await live.observe_config() == inputs["relevant_config"]
            assert live.runtime_kinds == {
                "control-plane": "control-plane",
                "echo": "javascript",
            }

    asyncio.run(run())
    assert calls.count("acquire") == calls.count("release") == 1
    assert "/actuator/configprops" in calls
    assert deployments[0].run_id != factory.prepared.run_id


def test_two_lifetimes_keep_images_settings_but_not_project_or_writer(case):
    factory, inputs, _, deployments = case

    async def run():
        for name in ("one", "two"):
            async with factory("sync", name, inputs):
                pass

    asyncio.run(run())
    assert deployments[0].run_id != deployments[1].run_id
    assert deployments[0].writer is not deployments[1].writer
    assert deployments[0].images == deployments[1].images
    assert (
        inputs["relevant_config"]["sync"]["effective_config"]["retention_s"][
            "live-key-and-execution"
        ]
        == 1800
    )


def test_precise_missing_populations_release_real_factory_context(case):
    factory, inputs, calls, _ = case
    runner = make_live_runner(inputs=inputs, factory=factory)

    async def run():
        async with runner("sync", "missing"):
            pytest.fail("missing authoritative timers cannot qualify")

    with pytest.raises(
        UnsupportedPreflightError, match=r"control-plane/timers.*echo/timers"
    ):
        asyncio.run(run())
    assert calls.count("release") == 1


def test_observed_settings_not_copied_from_expectations(case, monkeypatch):
    factory, inputs, _, _ = case
    monkeypatch.setattr(
        platform,
        "observe_local_process",
        lambda target: {
            "runtime": target.runtime,
            "runtime_options": [],
            "cpu": 1,
            "memory_bytes": None,
        },
    )

    async def run():
        async with factory("sync", "unknown", inputs) as live:
            observed = await live.observe_config()
            assert observed != inputs["relevant_config"]
            assert (
                observed["sync"]["effective_config"]["roles"]["echo"]["memory_bytes"]
                is None
            )

    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["images", "settings", "missing", "managed"])
def test_reject_mismatched_frozen_inputs_or_unowned_churn(case, mutation):
    factory, inputs, calls, _ = case
    inputs = deepcopy(inputs)
    if mutation == "images":
        inputs["images"]["echo"] = "changed@sha256:" + "c" * 64
    elif mutation == "settings":
        inputs["relevant_config"]["sync"]["effective_config"]["roles"]["echo"][
            "memory_bytes"
        ] = 1
    elif mutation == "missing":
        del inputs["relevant_config"]["sync"]["effective_config"]
    else:
        inputs["relevant_config"]["sync"]["function_spec"] = {
            "executionMode": "DEPLOYMENT"
        }

    async def run():
        async with factory("sync", "invalid", inputs):
            pytest.fail("invalid frozen contract accepted")

    with pytest.raises(
        UnsupportedPreflightError,
        match=(
            r"churn must remain on the same owned|effective_config|"
            r"prerequisite images differ from"
        ),
    ):
        asyncio.run(run())
    assert "acquire" not in calls


def test_body_cancellation_releases_workflow(case):
    factory, inputs, calls, _ = case

    async def run():
        async with factory("sync", "cancel", inputs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert calls.count("release") == 1


def test_recovery_requires_parent_reap_and_rejects_live_worker(case):
    factory, inputs, _, _ = case

    async def run():
        async with factory("sync", "live", inputs):
            pass

    asyncio.run(run())
    assert not factory.recover("live").cleanup_confirmed
    result = factory.recover("live", worker_reaped=True)
    assert not result.cleanup_confirmed
    assert "still present" in result.reason


@pytest.mark.parametrize(
    ("acquired", "leftovers"), [(False, False), (True, False), (True, True)]
)
def test_recovery_partial_is_unsupported_and_absence_is_required(
    case, monkeypatch, acquired, leftovers
):
    factory, inputs, _, _ = case
    directory = factory.root / "dead"
    directory.mkdir(parents=True)
    intent = {
        "factory_token": factory.token,
        "lifetime_id": "dead",
        "images": inputs["images"],
        "worker_pid": 12345,
        "worker_start": "old",
        "project_name": "soak-" + "a" * 32,
    }
    platform._publish(directory / "intent.json", intent)
    platform._publish(directory / "workflow-starting.json", intent)
    if acquired:
        platform._publish(directory / "acquired.json", intent)
    monkeypatch.setattr(platform, "_process_start", lambda pid: None)
    monkeypatch.setattr(
        platform,
        "read_cleanup_records",
        lambda path, include_released=False: (
            [
                {
                    "value": {
                        "schema": "nanolab-soak-owned-compose-v1",
                        "project": {"name": intent["project_name"]},
                    }
                }
            ]
            if include_released
            else []
        ),
    )
    teardown = []
    monkeypatch.setattr(
        platform,
        "TeardownSoakTask",
        lambda *args, **kwargs: SimpleNamespace(
            run=lambda inputs: teardown.append("replay")
        ),
    )
    commands = []

    def command(argv, cwd, env):
        commands.append(argv)
        return b"remaining-id" if leftovers else b""

    monkeypatch.setattr(
        platform, "LocalCleanupCommands", lambda *args, **kwargs: command
    )
    result = factory.recover("dead", worker_reaped=True)
    assert result.cleanup_confirmed is (acquired and not leftovers)
    if not acquired:
        assert result.status == "unsupported"
        assert not teardown and not commands
    elif not leftovers:
        assert teardown == ["replay"]
        assert len(commands) == 3
        assert all(
            argv[-1] == "label=com.docker.compose.project=" + intent["project_name"]
            for argv in commands
        )


def test_recovery_refuses_foreign_intent(case, monkeypatch):
    factory, _, _, _ = case
    directory = factory.root / "foreign"
    directory.mkdir(parents=True)
    platform._publish(directory / "intent.json", {"factory_token": "another"})
    result = factory.recover("foreign", worker_reaped=True)
    assert not result.cleanup_confirmed
    assert "another factory" in result.reason
