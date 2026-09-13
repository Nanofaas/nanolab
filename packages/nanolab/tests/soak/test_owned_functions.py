"""Function deletion needs acquisition ownership and a continuous live catalog."""

from dataclasses import replace

import pytest
from sonata_engine import (
    JournalConfig,
    Resource,
    Task,
    TaskOutcome,
    Workflow,
    release_retained,
)

from nanolab.tasks.soak.owned_functions import (
    FunctionOwnership,
    delete_owned_function,
    journaled_function_resource,
)
from nanolab.tasks.soak.retention import CleanupState


def ownership(tmp_path):
    return FunctionOwnership(
        name="word-stats-java",
        api_endpoint="http://127.0.0.1:18080",
        control_plane_image="registry/cp@sha256:" + "a" * 64,
        project_name="soak-owned",
        cwd=str(tmp_path),
        control_plane_container_id="c" * 64,
        control_plane_started_at="2026-09-13T10:00:00.000000000Z",
    )


@pytest.mark.parametrize("delete_status", [204, 404])
def test_delete_requires_observed_absence_and_continuity(tmp_path, delete_status):
    calls = []

    def request(method, url, timeout):
        calls.append(method)
        return delete_status if method == "DELETE" else 404

    delete_owned_function(
        ownership(tmp_path),
        request=request,
        verify_owner=lambda owner: calls.append("verify"),
    )
    assert calls == ["verify", "DELETE", "GET", "verify"]


@pytest.mark.parametrize("status", [200, 202, 301, 400, 401, 409, 500, True])
def test_http_errors_or_async_acceptance_are_not_cleanup_proof(tmp_path, status):
    with pytest.raises(RuntimeError, match="owned function DELETE did not succeed"):
        delete_owned_function(
            ownership(tmp_path),
            request=lambda *args: status,
            verify_owner=lambda owner: None,
        )


def test_connection_failure_is_not_absence(tmp_path):
    def request(*args):
        raise ConnectionRefusedError("offline")

    with pytest.raises(ConnectionRefusedError, match="offline"):
        delete_owned_function(
            ownership(tmp_path), request=request, verify_owner=lambda owner: None
        )


def test_successful_delete_without_absence_remains_unconfirmed(tmp_path):
    with pytest.raises(RuntimeError, match="owned function removal is unconfirmed"):
        delete_owned_function(
            ownership(tmp_path),
            request=lambda *args: 204,
            verify_owner=lambda owner: None,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:8080",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://user@127.0.0.1:8080",
        "http://127.0.0.1:8080/actuator/health",
        "http://127.0.0.1:8080?other=target",
    ],
)
def test_nonlocal_or_ambiguous_endpoints_rejected(tmp_path, endpoint):
    with pytest.raises(ValueError, match=r"owned API endpoint requires an"):
        replace(ownership(tmp_path), api_endpoint=endpoint)


class Consume(Task):
    title = "Consume owned function"

    def run(self, inputs):
        return TaskOutcome(value=None)


def adapter(tmp_path, state, request, acquire=lambda inputs: None, owner=None):
    return journaled_function_resource(
        Resource(
            title="Acquire owned function",
            acquire=acquire,
            release=lambda *args: pytest.fail("permissive release must not run"),
        ),
        ownership=owner or ownership(tmp_path),
        cleanup_state=state,
        verify_owner=lambda owner: None,
        request=request,
    )


def test_fresh_function_resource_replays_from_durable_journal(tmp_path):
    calls = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    resource = adapter(tmp_path, CleanupState(journal), lambda *args: 404)
    Workflow(workflow_id="soak", keep=True).add(Consume(), requires=(resource,)).run()
    fresh = adapter(
        tmp_path,
        CleanupState.restore(journal),
        lambda method, *args: (
            calls.append(method) or (204 if method == "DELETE" else 404)
        ),
    )
    assert release_retained({fresh.title: fresh}, journal) == (fresh.title,)
    assert calls == ["DELETE", "GET"]
    assert release_retained({fresh.title: fresh}, journal) == ()


def test_failed_release_remains_retriable_without_keep(tmp_path):
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    resource = adapter(tmp_path, CleanupState(journal), lambda *args: 500)
    with pytest.raises(RuntimeError, match="Cleanup failed"):
        Workflow(workflow_id="soak").add(Consume(), requires=(resource,)).run()
    fresh = adapter(tmp_path, CleanupState.restore(journal), lambda *args: 404)
    assert release_retained({fresh.title: fresh}, journal) == (fresh.title,)


def test_failed_unknown_acquisition_never_deletes_existing_name(tmp_path):
    calls = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")

    def conflict(inputs):
        raise RuntimeError("HTTP 409 existing name")

    state = CleanupState(journal)
    resource = adapter(
        tmp_path, state, lambda *args: calls.append(args) or 404, conflict
    )
    with pytest.raises(RuntimeError, match="HTTP 409 existing name"):
        Workflow(workflow_id="conflict").add(Consume(), requires=(resource,)).run()
    assert calls == []
    assert state.failed
    restored = CleanupState.restore(journal)
    with pytest.raises(RuntimeError, match=r"preserving platform because resource"):
        restored.require_compose_release("Acquire Compose")


def test_changed_owner_prevents_even_first_http_request(tmp_path):
    calls = []

    def verify(owner):
        raise ValueError("container restarted")

    with pytest.raises(ValueError, match="container restarted"):
        delete_owned_function(
            ownership(tmp_path),
            verify_owner=verify,
            request=lambda *args: calls.append(args) or 404,
        )
    assert calls == []


def test_catalog_continuity_loss_during_delete_is_failure(tmp_path):
    verifications = []

    def verify(owner):
        verifications.append(owner)
        if len(verifications) == 2:
            raise ValueError("catalog continuity lost")

    with pytest.raises(ValueError, match="catalog continuity lost"):
        delete_owned_function(
            ownership(tmp_path), verify_owner=verify, request=lambda *args: 404
        )


def test_owner_supplier_runs_after_dependency_acquisition(tmp_path):
    acquired = []
    resource = adapter(
        tmp_path,
        CleanupState(JournalConfig(path=tmp_path / "cleanup.jsonl")),
        lambda *args: 404,
        owner=lambda: acquired[0],
    )
    acquired.append(ownership(tmp_path))
    Workflow(workflow_id="late-owner").add(Consume(), requires=(resource,)).run()
