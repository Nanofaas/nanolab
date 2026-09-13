"""Prerequisite gates reject stale inputs, false success and unsettled ownership."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from importlib import import_module

import pytest

from nanolab.tasks.soak.artifacts import ArtifactWriter, describe_artifact, fingerprint


def api():
    try:
        return import_module("nanolab.tasks.soak.prerequisites")
    except ModuleNotFoundError:
        pytest.fail("Task 8 prerequisite API is not implemented")


@pytest.fixture
def inputs(tmp_path):
    payload = tmp_path / "payload.json"
    payload.write_text('{"value":42}')
    script = tmp_path / "profile.py"
    script.write_text("# frozen profile input\n")
    return {
        "images": {
            "control-plane": "sha256:" + "a" * 64,
            "java": "sha256:" + "b" * 64,
            "javascript": "sha256:" + "c" * 64,
        },
        "relevant_config": {"sync": {"expected_output": {"value": 42}, "retry": 3}},
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
            for role in ("control-plane", "java", "javascript")
        },
    }


class Session:
    def __init__(self, inputs, *, leaking=False, wrong_output=False, crash=False):
        self.inputs = inputs
        self.leaking = leaking
        self.wrong_output = wrong_output
        self.crash = crash

    async def identities(self):
        return deepcopy(self.inputs["images"])

    async def exercise(self, coverage):
        if self.crash:
            raise RuntimeError("synthetic profile failure")
        observations = {
            "sync": {
                "http_status": 200,
                "output": {"value": 0 if self.wrong_output else 42},
            },
            "error": {"http_status": 500, "outcome": "ERROR"},
            "timeout": {"outcome": "TIMEOUT"},
            "cancellation": {"outcome": "CANCELLED"},
            "async": {
                "enqueue_status": 202,
                "execution_status": "SUCCESS",
                "output": {"value": 42},
            },
            "late-callback": {
                "outcome": "TIMEOUT",
                "callback_status": "ignored",
                "callback_after_timeout": True,
            },
            "idempotent-replay": {
                "execution_ids": ["one", "one"],
                "physical_executions": 1,
                "outputs": [{"value": 42}, {"value": 42}],
            },
            "function-name-churn": {
                "created_names": ["first", "second"],
                "removed_names": ["second", "first"],
            },
        }
        return observations[coverage]

    async def populations(self):
        return {
            role: {
                name: (1 if self.leaking and name == "live_executions" else 0)
                for name in policies
            }
            for role, policies in self.inputs["settlement"].items()
        }


def execute(tmp_path, inputs, coverage=frozenset({"sync"}), **options):
    lifetime_log = tmp_path / "lifetimes.jsonl"

    def record(entry):
        with lifetime_log.open("a") as output:
            output.write(json.dumps(entry) + "\n")

    @asynccontextmanager
    async def runner(coverage_id, lifetime_id):
        record((coverage_id, lifetime_id, "start"))
        try:
            yield Session(inputs, **options)
        finally:
            record((coverage_id, lifetime_id, "stop"))

    writer = ArtifactWriter(tmp_path / "receipts", 300_000)
    receipt = asyncio.run(
        api().run_prerequisites(
            inputs=inputs,
            required_coverage=coverage,
            runner=runner,
            writer=writer,
            timeout_s=0.1,
        )
    )
    writer.close()
    lifetimes = (
        [json.loads(line) for line in lifetime_log.read_text().splitlines()]
        if lifetime_log.exists()
        else []
    )
    return receipt, lifetimes


def test_old_image_receipt_does_not_unlock_p24():
    assert (
        api()
        .validate_receipt(
            {"fingerprint": "old", "status": "PASS", "coverage": ["sync"]},
            "current",
            frozenset({"sync"}),
        )
        .status
        == "INCONCLUSIVE"
    )


@pytest.mark.parametrize(
    "change",
    [
        None,
        "response-success",
        "terminal-success",
        "different-id",
        "empty-id",
        "missing-code",
        "wrong-code",
        "unfrozen-code",
    ],
)
def test_http_200_requires_actual_matching_terminal_error(change):
    observed = {
        "http_status": 200,
        "outcome": "ERROR",
        "response": {
            "executionId": "one",
            "status": "error",
            "error": {"code": "HANDLER_ERROR"},
        },
        "execution": {
            "executionId": "one",
            "status": "error",
            "error": {"code": "HANDLER_ERROR"},
        },
    }
    config = {"expected_error_code": "HANDLER_ERROR"}
    if change == "response-success":
        observed["response"]["status"] = "success"
    elif change == "terminal-success":
        observed["execution"]["status"] = "success"
    elif change == "different-id":
        observed["execution"]["executionId"] = "another"
    elif change == "empty-id":
        observed["response"]["executionId"] = ""
    elif change == "missing-code":
        observed["response"]["error"] = {}
    elif change == "wrong-code":
        observed["execution"]["error"]["code"] = "OTHER_ERROR"
    elif change == "unfrozen-code":
        config = {}
    assertions = api()._behavior("error", observed, config)
    assert all(a["status"] == "PASS" for a in assertions) is (change is None)


def test_legacy_http_500_error_remains_supported():
    assertions = api()._behavior("error", {"http_status": 500, "outcome": "ERROR"}, {})
    assert all(a["status"] == "PASS" for a in assertions)


def test_positive_receipt_reopens_evidence_and_checks_every_role(tmp_path, inputs):
    receipt, lifetimes = execute(tmp_path, inputs)
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        == "PASS"
    )
    assert lifetimes[0][1] == lifetimes[1][1]
    assert [entry[2] for entry in lifetimes] == ["start", "stop"]


@pytest.mark.parametrize(
    "change",
    [
        "sdk",
        "config",
        "payload",
        "script",
        "missing-role",
        "purpose",
        "coverage",
        "assertions",
        "timing",
    ],
)
def test_stale_or_partial_receipt_never_unlocks_p24(tmp_path, inputs, change):
    receipt, _ = execute(tmp_path, inputs)
    expected = fingerprint(inputs)
    if change == "sdk":
        receipt["inputs"]["images"]["javascript"] = "sha256:" + "d" * 64
    elif change == "config":
        receipt["inputs"]["relevant_config"]["sync"]["retry"] = 1
    elif change in {"payload", "script"}:
        from pathlib import Path

        Path(inputs[change]["path"]).write_text("tampered")
    elif change == "missing-role":
        del receipt["inputs"]["images"]["java"]
    elif change == "purpose":
        receipt["purpose"] = "smoke"
    elif change == "coverage":
        receipt["coverage"] = ["sync", "async"]
    elif change == "assertions":
        receipt["profiles"][0]["assertions"] = []
    else:
        receipt["profiles"][0]["ended_s"] = -1
    assert (
        api().validate_receipt(receipt, expected, frozenset({"sync"})).status
        == "INCONCLUSIVE"
    )


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_tampered_evidence_fails_closed(tmp_path, inputs, missing):
    from pathlib import Path

    receipt, _ = execute(tmp_path, inputs)
    evidence = Path(receipt["profiles"][0]["evidence"]["path"])
    if missing:
        evidence.unlink()
    else:
        evidence.write_text('{"kind":"fake","status":"PASS"}\n')
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        == "INCONCLUSIVE"
    )


@pytest.mark.parametrize("options", [{"wrong_output": True}, {"leaking": True}])
def test_http_completion_does_not_hide_bad_output_or_retained_population(
    tmp_path, inputs, options
):
    receipt, lifetimes = execute(tmp_path, inputs, **options)
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        == "FAIL"
    )
    assert lifetimes[-1][2] == "stop"


def test_unsupported_coverage_is_reported_without_starting_resources(tmp_path, inputs):
    inputs["relevant_config"] = {"unsupported": {"retry": 3}}
    receipt, lifetimes = execute(tmp_path, inputs, frozenset({"unsupported"}))
    result = api().validate_receipt(
        receipt, fingerprint(inputs), frozenset({"unsupported"})
    )
    assert result.status == "INCONCLUSIVE"
    assert "unsupported" in result.reason
    assert not lifetimes


def test_profile_exception_preserves_partial_receipt_and_releases_lifetime(
    tmp_path, inputs
):
    receipt, lifetimes = execute(tmp_path, inputs, crash=True)
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        == "INCONCLUSIVE"
    )
    assert "synthetic profile failure" in json.dumps(receipt)
    assert lifetimes[-1][2] == "stop"


def test_independent_profiles_assert_semantics_and_extra_retained_owners(
    tmp_path, inputs
):
    coverage = frozenset(
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
    inputs["relevant_config"] = {
        name: {"expected_output": {"value": 42}} for name in coverage
    }
    for policies in inputs["settlement"].values():
        for name in (
            "callbacks",
            "idempotency_entries",
            "retired_owners",
            "metric_series",
        ):
            policies[name] = {"limit": 0, "retention_s": 0}
    receipt, lifetimes = execute(tmp_path, inputs, coverage)
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), coverage).status == "PASS"
    )
    starts = [entry for entry in lifetimes if entry[2] == "start"]
    assert len({entry[1] for entry in starts}) == 8
    assert [entry[2] for entry in lifetimes] == ["start", "stop"] * 8


def test_missing_population_is_incomplete_instead_of_zero(tmp_path, inputs):
    @asynccontextmanager
    async def runner(coverage, lifetime):
        session = Session(inputs)

        async def absent():
            return {"control-plane": {"live_executions": 0}}

        session.populations = absent
        yield session

    writer = ArtifactWriter(tmp_path / "absent", 100_000)
    receipt = asyncio.run(
        api().run_prerequisites(
            inputs=inputs,
            required_coverage=frozenset({"sync"}),
            runner=runner,
            writer=writer,
            timeout_s=0.1,
        )
    )
    writer.close()
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        == "INCONCLUSIVE"
    )


def test_caller_pass_string_cannot_replace_failed_observations(tmp_path, inputs):
    receipt, _ = execute(tmp_path, inputs, wrong_output=True)
    receipt["status"] = "PASS"
    receipt["profiles"][0]["status"] = "PASS"
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        != "PASS"
    )


def test_shared_prerequisite_envelope_rejects_old_schema_and_wrong_kind(
    tmp_path, inputs
):
    receipt, _ = execute(tmp_path, inputs)
    assert receipt["schema"] == "nanolab-soak-v1"
    assert receipt["kind"] == "prerequisites"
    for envelope in (
        {"schema": "nanolab-soak-prerequisites-v1"},
        {"kind": "prerequisite"},
        {"kind": "report"},
    ):
        mutated = {**receipt, **envelope}
        assert (
            api()
            .validate_receipt(mutated, fingerprint(inputs), frozenset({"sync"}))
            .status
            == "INCONCLUSIVE"
        )


async def _never_finish():
    while True:
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(10)


@pytest.mark.parametrize(
    "fault",
    [
        "acquire-error",
        "acquire-timeout",
        "acquire-stubborn",
        "body-error",
        "body-timeout",
        "body-stubborn",
        "release-error",
        "release-timeout",
        "release-stubborn",
        "body-timeout-release-stubborn",
        "background-stubborn",
    ],
)
def test_phase_failures_are_bounded_persisted_and_reaped(tmp_path, inputs, fault):
    import multiprocessing
    from time import monotonic

    # Retain intentional background work until the owned worker shuts down.
    background_tasks = []

    class FaultContext:
        async def __aenter__(self):
            if fault == "acquire-error":
                raise RuntimeError("acquire exploded")
            if fault == "acquire-timeout":
                await asyncio.sleep(10)
            if fault == "acquire-stubborn":
                await _never_finish()
            session = Session(inputs)
            if fault in {
                "body-timeout",
                "body-stubborn",
                "body-timeout-release-stubborn",
                "background-stubborn",
            }:
                original = session.exercise

                async def exercise(coverage):
                    if fault == "body-stubborn":
                        await _never_finish()
                    if fault == "background-stubborn":
                        background_tasks.append(asyncio.create_task(_never_finish()))
                        return await original(coverage)
                    await asyncio.sleep(10)
                    return None

                session.exercise = exercise
            if fault == "body-error":
                session.crash = True
            return session

        async def __aexit__(self, *exc):
            assert list((tmp_path / "fault").glob("prerequisite-*-partial.json")), (
                "partial evidence must precede release"
            )
            if fault == "release-error":
                raise RuntimeError("release exploded")
            if fault == "release-timeout":
                await asyncio.sleep(10)
            if fault in {"release-stubborn", "body-timeout-release-stubborn"}:
                await _never_finish()
            return False

    async def exercise():
        tasks_before = asyncio.all_tasks()
        writer = ArtifactWriter(tmp_path / "fault", 100_000)
        started = monotonic()
        receipt = await api().run_prerequisites(
            inputs=inputs,
            required_coverage=frozenset({"sync"}),
            runner=lambda *_: FaultContext(),
            writer=writer,
            timeout_s=0.08,
            acquire_timeout_s=0.08,
            release_timeout_s=0.08,
            stop_timeout_s=0.08,
        )
        writer.close()
        assert monotonic() - started < 2
        assert asyncio.all_tasks() == tasks_before
        return receipt

    children_before = {p.pid for p in multiprocessing.active_children()}
    receipt = asyncio.run(exercise())
    assert {p.pid for p in multiprocessing.active_children()} == children_before
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        == "INCONCLUSIVE"
    )
    terminal_files = list((tmp_path / "fault").glob("prerequisites-*.json"))
    assert len(terminal_files) == 1
    assert json.loads(terminal_files[0].read_text())["status"] == "INCONCLUSIVE"
    profile = receipt["profiles"][0]
    assert profile["worker"]["reaped"] is True
    if fault.startswith("release") or fault == "body-timeout-release-stubborn":
        assert profile["released"] is False
        assert profile["cleanup_error"]
    if fault == "body-timeout-release-stubborn":
        assert "body" in profile["reason"]
        assert "release" in profile["cleanup_error"]


@pytest.mark.parametrize("stage", ["acquire", "body", "release", "reap"])
def test_external_cancellation_persists_aborted_receipt_without_remaining_tasks(
    tmp_path, inputs, stage, monkeypatch
):
    import multiprocessing

    marker = tmp_path / "cancel-ready"
    background_tasks = []
    if stage == "reap":
        original_reap = api()._reap_worker

        async def reaping(*args, **kwargs):
            marker.touch()
            return await original_reap(*args, **kwargs)

        monkeypatch.setattr(api(), "_reap_worker", reaping)

    class CancelContext:
        async def __aenter__(self):
            if stage == "acquire":
                marker.touch()
                await asyncio.sleep(10)
            session = Session(inputs)
            if stage == "reap":
                original_exercise = session.exercise

                async def background():
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        await asyncio.sleep(0.04)

                async def exercise_with_background(coverage):
                    background_tasks.append(asyncio.create_task(background()))
                    return await original_exercise(coverage)

                session.exercise = exercise_with_background
            if stage == "body":

                async def exercise(coverage):
                    marker.touch()
                    await asyncio.sleep(10)

                session.exercise = exercise
            return session

        async def __aexit__(self, *exc):
            if stage == "release":
                marker.touch()
                await _never_finish()
            return False

    async def exercise():
        before = asyncio.all_tasks()
        writer = ArtifactWriter(tmp_path / "cancel", 100_000)
        task = asyncio.create_task(
            api().run_prerequisites(
                inputs=inputs,
                required_coverage=frozenset({"sync"}),
                runner=lambda *_: CancelContext(),
                writer=writer,
                timeout_s=1,
                acquire_timeout_s=1,
                release_timeout_s=0.1,
                stop_timeout_s=0.08,
            )
        )
        async with asyncio.timeout(2):
            while not marker.exists():
                if task.done():
                    await task
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        writer.close()
        assert asyncio.all_tasks() == before

    children_before = {p.pid for p in multiprocessing.active_children()}
    asyncio.run(exercise())
    assert {p.pid for p in multiprocessing.active_children()} == children_before
    terminal = json.loads(
        next((tmp_path / "cancel").glob("prerequisites-*.json")).read_text()
    )
    assert terminal["status"] == "ABORTED"
    assert terminal["profiles"][0]["worker"]["reaped"] is True
    assert (
        api()
        .validate_receipt(terminal, fingerprint(inputs), frozenset({"sync"}))
        .status
        != "PASS"
    )


def test_independent_body_budget_does_not_include_acquisition_or_release(
    tmp_path, inputs
):
    @asynccontextmanager
    async def runner(*_):
        await asyncio.sleep(0.06)
        try:
            yield Session(inputs)
        finally:
            await asyncio.sleep(0.06)

    writer = ArtifactWriter(tmp_path / "independent", 100_000)
    receipt = asyncio.run(
        api().run_prerequisites(
            inputs=inputs,
            required_coverage=frozenset({"sync"}),
            runner=runner,
            writer=writer,
            timeout_s=0.04,
            acquire_timeout_s=0.3,
            release_timeout_s=0.3,
            stop_timeout_s=0.1,
        )
    )
    writer.close()
    assert (
        api().validate_receipt(receipt, fingerprint(inputs), frozenset({"sync"})).status
        == "PASS"
    )
