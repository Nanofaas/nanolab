"""Live prerequisite adapters tested using HTTP transports, without sockets."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import httpx
import pytest

from nanolab.tasks.soak.artifacts import ArtifactWriter, describe_artifact, fingerprint
from nanolab.tasks.soak.prerequisite_runtime import (
    _DEFAULT_METRICS,
    LivePlatform,
    LiveProfileSession,
    UnsupportedPreflightError,
    expand_coverage,
    make_live_runner,
    required_body_budget,
)
from nanolab.tasks.soak.prerequisites import run_prerequisites, validate_receipt


def test_default_metrics_bind_each_soak_population_to_its_owner():
    assert _DEFAULT_METRICS == {
        "control-plane": {
            "execution_records": ("execution_in_flight_records",),
            "outcomes": ("execution_store_size",),
            "idempotency_entries": ("idempotency_keys_held",),
            "logical_executions": ("invocation_execution_reservations",),
            "canonical_input_bytes": ("invocation_canonical_input_bytes",),
            "physical_input_copy_bytes": ("invocation_physical_input_copy_bytes",),
            "waiters": ("execution_waiters_retained",),
            "expiry_queue_depth": ("execution_expiry_queue_depth",),
            "pending_acquisitions": ("nanofaas_http_pool_pending_acquisitions",),
            "replica_snapshots": ("replica_snapshot_entries",),
            "retired_owners": ("function_capacity_retired_generations",),
        },
        "java": {
            "live_executions": ("runtime_active_handlers",),
            "callbacks": ("runtime_pending_callbacks",),
            "callback_bytes": ("runtime_pending_callback_bytes",),
        },
        "javascript": {
            "live_executions": ("runtime_active_handlers",),
            "input_bytes": ("runtime_input_bytes",),
            "output_bytes": ("runtime_output_bytes",),
            "callbacks": ("runtime_pending_callbacks",),
            "callback_bytes": ("runtime_pending_callback_bytes",),
            "serialized_callback_bytes": ("runtime_serialized_callback_bytes",),
        },
    }


@pytest.fixture
def frozen(tmp_path):
    payload = tmp_path / "payload.json"
    payload.write_text('{"value":42}')
    script = tmp_path / "profile.py"
    script.write_text("# synthetic transport profile\n")
    return {
        "images": {
            "control-plane": "sha256:" + "a" * 64,
            "javascript": "sha256:" + "b" * 64,
        },
        "metrics_profile": "advanced",
        "relevant_config": {
            "sync": {
                "function": "echo",
                "role": "javascript",
                "expected_output": {"value": 42},
                "exercise_timeout_s": 1,
                "poll_interval_s": 0.001,
            }
        },
        "payload": describe_artifact(payload),
        "script": describe_artifact(script),
        "settlement": {
            role: {
                name: {"limit": 0, "retention_s": 0}
                for name in (
                    "live_executions",
                    "payload_bytes",
                    "timers",
                    "pending_http",
                )
            }
            for role in ("control-plane", "javascript")
        },
    }


class Server:
    """A stateful HTTP fixture, including mutable physical invocation counts."""

    def __init__(self):
        self.calls = []
        self.count = 0
        self.replays = {}
        self.functions = {"echo"}
        self.terminal = "success"
        self.callback_rewrites = False
        self.result = {"value": 42}
        self.status = 200
        self.error_code = "HANDLER_ERROR"
        self.response_error_code = "HANDLER_ERROR"
        self.response_status = None
        self.waiters = 0
        self.block = False
        self.extra_metrics = ""
        self.deleted_still_present = False

    async def handle(self, request):
        import json

        path = request.url.path
        self.calls.append((request.method, path))
        if path.endswith("/metrics"):
            return httpx.Response(
                200,
                text=(
                    f'runtime_invocations_total{{success="true"}} {self.count}\n'
                    'runtime_invocations_total{success="false"} 0\n'
                    f"test_handler_starts_total {self.count}\n"
                    "runtime_active_handlers 0\nruntime_input_bytes 0\n"
                    "runtime_output_bytes 0\nruntime_serialized_callback_bytes 0\n"
                    "runtime_pending_callbacks 0\n"
                    f"test_waiters {self.waiters}\n" + self.extra_metrics
                ),
            )
        if path.endswith(":complete"):
            if self.callback_rewrites:
                self.terminal = "success"
            return httpx.Response(204)
        if path.startswith("/v1/executions/"):
            return httpx.Response(
                200,
                json={
                    "executionId": path.rsplit("/", 1)[-1],
                    "status": self.terminal,
                    "output": self.result if self.terminal == "success" else None,
                    "error": {"code": self.error_code}
                    if self.terminal == "error"
                    else None,
                },
            )
        if path.endswith((":invoke", ":enqueue")):
            key = request.headers.get("Idempotency-Key")
            if key and key in self.replays:
                identity = self.replays[key]
            else:
                self.count += 1
                identity = f"execution-{self.count}"
                if key:
                    self.replays[key] = identity
            if self.block:
                self.waiters += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    self.waiters -= 1
            return httpx.Response(
                202 if path.endswith(":enqueue") else self.status,
                json={
                    "executionId": identity,
                    "status": self.response_status or self.terminal,
                    "output": self.result if self.terminal == "success" else None,
                    "error": {"code": self.response_error_code}
                    if self.terminal == "error"
                    else None,
                },
                headers={"X-Execution-Id": identity},
            )
        if path == "/v1/functions" and request.method == "POST":
            spec = json.loads(request.content)
            self.functions.add(spec["name"])
            return httpx.Response(201, json=spec)
        name = path.rsplit("/", 1)[-1]
        if request.method == "DELETE":
            if not self.deleted_still_present:
                self.functions.discard(name)
            return httpx.Response(204)
        return httpx.Response(
            200 if name in self.functions else 404, json={"name": name}
        )


@asynccontextmanager
async def platform(frozen, server, lifetime="lifetime-1"):
    async def identities():
        return deepcopy(frozen["images"])

    async def config():
        return deepcopy(frozen["relevant_config"])

    async with httpx.AsyncClient(
        base_url="http://platform", transport=httpx.MockTransport(server.handle)
    ) as client:
        yield LivePlatform(
            lifetime_id=lifetime,
            client=client,
            metrics_urls={
                "control-plane": "http://cp/metrics",
                "javascript": "http://js/metrics",
            },
            observe_identities=identities,
            observe_config=config,
            runtime_kinds={
                "control-plane": "control-plane",
                "javascript": "javascript",
            },
        )


def set_profile(frozen, coverage, **config):
    base = frozen["relevant_config"]["sync"]
    frozen["relevant_config"] = {coverage: {**base, **config}}


def instrument_fixture(frozen, server):
    """Only the synthetic server exposes these explicitly named test gauges."""
    bindings = {}
    for role, policies in frozen["settlement"].items():
        bindings[role] = {}
        for population in policies:
            name = f"test_{population}"
            bindings[role][population] = [{"metric": name}]
    frozen["population_metrics"] = bindings
    server.extra_metrics = "".join(
        f"test_{name} 0\n" for name in frozen["settlement"]["control-plane"]
    )


def test_grouped_p24_coverage_expands_to_exact_validator_ids():
    assert expand_coverage(
        {
            "sync",
            "error-timeout-cancellation",
            "async-late-callback",
            "idempotent-replay",
            "function-name-churn",
        }
    ) == frozenset(
        {
            "sync",
            "error",
            "timeout",
            "cancellation",
            "async",
            "late-callback",
            "idempotent-replay",
            "function-name-churn",
        }
    )
    with pytest.raises(UnsupportedPreflightError, match="unsupported coverage"):
        expand_coverage({"smoke"})


def test_real_current_gaps_are_preflight_errors_before_any_invocation(frozen):
    server = Server()

    async def exercise():
        async with platform(frozen, server) as live:
            session = LiveProfileSession(live, frozen, "sync")
            with pytest.raises(
                UnsupportedPreflightError,
                match="unsupported prerequisite populations",
            ) as error:
                await session.preflight()
            assert "control-plane/live_executions" in str(error.value)
            assert "javascript/timers" in str(error.value)
            assert "javascript/pending_http" in str(error.value)
            assert all(path.endswith("/metrics") for _, path in server.calls)

    asyncio.run(exercise())


@pytest.mark.parametrize("coverage", ["sync", "async", "idempotent-replay"])
def test_actual_http_outputs_and_replay_identity(frozen, coverage):
    server = Server()
    set_profile(frozen, coverage)

    async def exercise():
        async with platform(frozen, server) as live:
            observed = await LiveProfileSession(live, frozen, coverage).exercise(
                coverage
            )
            if coverage == "idempotent-replay":
                assert observed["execution_ids"] == ["execution-1"] * 2
                assert observed["outputs"] == [{"value": 42}] * 2
            else:
                assert observed["output"] == {"value": 42}
            assert observed["runtime_evidence"]["events"]
            assert server.count == 1

    asyncio.run(exercise())


def test_wrong_output_is_returned_as_observed_not_expected(frozen):
    server = Server()
    server.result = {"value": "wrong"}  # pyright: ignore[reportAttributeAccessIssue]

    async def exercise():
        async with platform(frozen, server) as live:
            result = await LiveProfileSession(live, frozen, "sync").exercise("sync")
            assert result["output"] == server.result

    asyncio.run(exercise())


@pytest.mark.parametrize("coverage", ["error", "timeout", "late-callback"])
def test_terminal_outcomes_come_from_execution_api(frozen, coverage):
    server = Server()
    server.terminal = "error" if coverage == "error" else "timeout"
    server.status = 200
    set_profile(
        frozen,
        coverage,
        expected_error_code="HANDLER_ERROR",
        callback_result={"success": True, "output": 42},
    )

    async def exercise():
        async with platform(frozen, server) as live:
            result = await LiveProfileSession(live, frozen, coverage).exercise(coverage)
            assert result["outcome"] == ("ERROR" if coverage == "error" else "TIMEOUT")
            if coverage == "error":
                assert result["http_status"] == 200
                assert result["response"]["status"] == "error"
                assert result["response"]["error"]["code"] == "HANDLER_ERROR"
            if coverage == "late-callback":
                assert result["callback_status"] == "ignored"
                assert result["callback_after_timeout"] is True

    asyncio.run(exercise())


def test_late_callback_rewriting_terminal_state_is_not_ignored(frozen):
    server = Server()
    server.terminal = "timeout"
    server.callback_rewrites = True
    set_profile(
        frozen, "late-callback", callback_result={"success": True, "output": 42}
    )

    async def exercise():
        async with platform(frozen, server) as live:
            result = await LiveProfileSession(live, frozen, "late-callback").exercise(
                "late-callback"
            )
            assert result["callback_status"] == "changed"

    asyncio.run(exercise())


def test_transport_timeout_is_not_execution_timeout(frozen):
    server = Server()
    set_profile(frozen, "timeout")

    async def exercise():
        async with platform(frozen, server) as live:

            async def timeout(request):
                raise httpx.ReadTimeout("synthetic client timeout", request=request)

            async with httpx.AsyncClient(
                base_url="http://platform", transport=httpx.MockTransport(timeout)
            ) as client:
                live.client = client
                with pytest.raises(
                    UnsupportedPreflightError, match="HTTP transport observation"
                ):
                    await LiveProfileSession(live, frozen, "timeout").exercise(
                        "timeout"
                    )

    asyncio.run(exercise())


def test_cancellation_requires_server_waiter_observations(frozen):
    server = Server()
    server.block = True
    set_profile(frozen, "cancellation")
    frozen["population_metrics"] = {
        "control-plane": {"waiters": [{"metric": "test_waiters"}]}
    }

    async def exercise():
        async with platform(frozen, server) as live:
            result = await LiveProfileSession(live, frozen, "cancellation").exercise(
                "cancellation"
            )
            assert result["outcome"] == "CANCELLED"
            assert result["waiter_counts"] == [0, 1, 0]
            assert server.waiters == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("still_present", [False, True])
def test_churn_creates_invokes_and_confirms_deletion(frozen, still_present):
    server = Server()
    server.deleted_still_present = still_present
    set_profile(
        frozen,
        "function-name-churn",
        function_spec={
            "image": frozen["images"]["javascript"],
            "executionMode": "DEPLOYMENT",
        },
    )

    async def exercise():
        async with platform(frozen, server) as live:
            session = LiveProfileSession(live, frozen, "function-name-churn")
            if still_present:
                with pytest.raises(
                    UnsupportedPreflightError, match="churn function removal"
                ):
                    await session.exercise("function-name-churn")
            else:
                result = await session.exercise("function-name-churn")
                assert len(result["created_names"]) == 2
                assert result["created_names"] == result["removed_names"]
                assert server.functions == {"echo"}
                assert server.count == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("metric", ["", "test_timers NaN\n", "test_timers -1\n"])
def test_missing_nonfinite_negative_population_never_becomes_zero(frozen, metric):
    server = Server()
    instrument_fixture(frozen, server)
    server.extra_metrics = server.extra_metrics.replace("test_timers 0\n", metric)

    async def exercise():
        async with platform(frozen, server) as live:
            with pytest.raises(
                UnsupportedPreflightError,
                match="unsupported prerequisite populations",
            ):
                await LiveProfileSession(live, frozen, "sync").populations()

    asyncio.run(exercise())


def test_factory_receipt_round_trip_and_cleanup(frozen, tmp_path):
    server = Server()
    instrument_fixture(frozen, server)
    lifetime_log = tmp_path / "factory.log"

    @asynccontextmanager
    async def factory(coverage, lifetime_id, frozen_inputs):
        assert coverage == "sync"
        assert frozen_inputs == frozen
        with lifetime_log.open("a") as output:
            output.write(lifetime_id + " acquire\n")
        try:
            async with platform(frozen_inputs, server, lifetime_id) as live:
                yield live
        finally:
            with lifetime_log.open("a") as output:
                output.write(lifetime_id + " release\n")

    writer = ArtifactWriter(tmp_path / "receipts", 200_000)
    try:
        receipt = asyncio.run(
            run_prerequisites(
                inputs=frozen,
                required_coverage=frozenset({"sync"}),
                runner=make_live_runner(inputs=frozen, factory=factory),
                writer=writer,
                timeout_s=2,
            )
        )
    finally:
        writer.close()
    assert (
        validate_receipt(receipt, fingerprint(frozen), frozenset({"sync"})).status
        == "PASS"
    )
    lines = lifetime_log.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].split()[0] == lines[1].split()[0]
    assert lines[1].endswith(" release")


def test_effective_config_mismatch_releases_factory(frozen):
    released = []

    @asynccontextmanager
    async def factory(coverage, lifetime, inputs):
        try:
            async with platform(inputs, Server(), lifetime) as live:

                async def different_config():
                    return {"sync": {"ttl": 1}}

                live.observe_config = different_config
                yield live
        finally:
            released.append(lifetime)

    async def exercise():
        runner = make_live_runner(inputs=frozen, factory=factory)
        with pytest.raises(
            UnsupportedPreflightError, match="effective configuration differs"
        ):
            async with runner("sync", "owned-1"):
                pytest.fail("configuration mismatch entered body")

    asyncio.run(exercise())
    assert released == ["owned-1"]


def test_default_ttls_are_not_shortened(frozen):
    original = deepcopy(frozen)
    for index, policies in enumerate(frozen["settlement"].values()):
        for policy in policies.values():
            policy["retention_s"] = (300, 1800)[index]
    before = deepcopy(frozen)
    assert required_body_budget(frozen) > 1800
    assert frozen == before
    assert original["settlement"] != frozen["settlement"]


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("error", "PASS"),
        ("success", "FAIL"),
        ("response-success", "FAIL"),
        ("wrong-code", "FAIL"),
        ("missing-code", "FAIL"),
        ("unfrozen-code", "FAIL"),
    ],
)
def test_http_200_error_receipt_requires_matching_terminal_code(
    frozen, tmp_path, variant, expected
):
    server = Server()
    server.terminal = "error"
    set_profile(frozen, "error", expected_error_code="HANDLER_ERROR")
    instrument_fixture(frozen, server)
    if variant == "success":
        server.terminal = "success"
    elif variant == "response-success":
        server.response_status = "success"  # pyright: ignore[reportAttributeAccessIssue]
    elif variant == "wrong-code":
        server.response_error_code = "OTHER_ERROR"
    elif variant == "missing-code":
        server.error_code = None  # pyright: ignore[reportAttributeAccessIssue]
    elif variant == "unfrozen-code":
        del frozen["relevant_config"]["error"]["expected_error_code"]

    @asynccontextmanager
    async def factory(coverage, lifetime, inputs):
        async with platform(inputs, server, lifetime) as live:
            yield live

    writer = ArtifactWriter(tmp_path / "error-receipt", 200_000)
    try:
        receipt = asyncio.run(
            run_prerequisites(
                inputs=frozen,
                required_coverage=frozenset({"error"}),
                runner=make_live_runner(inputs=frozen, factory=factory),
                writer=writer,
                timeout_s=2,
            )
        )
    finally:
        writer.close()
    assert receipt["status"] == expected
    assert (
        validate_receipt(receipt, fingerprint(frozen), frozenset({"error"})).status
        == expected
    )


@pytest.mark.parametrize(
    ("binding", "semantics"),
    [
        (None, None),
        ("runtime_invocations_total", None),
        ("runtime_invocations_total", "handler-starts"),
        ("test_handler_starts_total", None),
    ],
)
def test_request_counter_never_establishes_physical_execution(
    frozen, binding, semantics
):
    server = Server()
    set_profile(frozen, "idempotent-replay")
    if binding:
        frozen["population_metrics"] = {
            "javascript": {"physical_executions": [{"metric": binding}]}
        }
    if semantics:
        frozen["population_semantics"] = {
            "javascript": {"physical_executions": semantics}
        }

    async def exercise():
        async with platform(frozen, server) as live:
            with pytest.raises(UnsupportedPreflightError, match="javascript"):
                await LiveProfileSession(live, frozen, "idempotent-replay")._population(
                    "javascript", "physical_executions"
                )
            assert server.count == 0

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "semantics", [None, "registry", "prometheus-exposition-series"]
)
def test_metric_series_counts_label_identities_only_with_frozen_semantics(
    frozen, semantics
):
    server = Server()
    if semantics:
        frozen["population_semantics"] = {"javascript": {"metric_series": semantics}}

    async def exercise():
        async with platform(frozen, server) as live:
            session = LiveProfileSession(live, frozen, "sync")
            if semantics != "prometheus-exposition-series":
                with pytest.raises(
                    UnsupportedPreflightError,
                    match=r"explicit frozen exposition semantics",
                ):
                    await session._population("javascript", "metric_series")
            else:
                baseline = await session._population("javascript", "metric_series")
                server.extra_metrics = (
                    'extra{function="one"} 9\nextra{function="two"} 7\n'
                )
                assert (
                    await session._population("javascript", "metric_series")
                    == baseline + 2
                )
                assert session.events[-1]["semantics"] == semantics

    asyncio.run(exercise())
