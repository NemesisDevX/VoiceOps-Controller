"""Unit and API tests for the incident lifecycle log and post-mortem export endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.schemas.command import ExecutionResult, IntentType, ParsedCommand
from app.services.incident_log import incident_log, render_markdown


def _command(**overrides) -> ParsedCommand:
    defaults = dict(raw_text="kill pid 4242", intent=IntentType.PROCESS_KILL, target_pid=4242, language="en")
    defaults.update(overrides)
    return ParsedCommand(**defaults)


def test_open_incident_records_triggering_alert_and_target() -> None:
    record = incident_log.open_incident("token-a", _command(target_process_name="runaway-worker.exe"), "HOST_LOCAL")

    assert record.token == "token-a"
    assert record.mode == "HOST_LOCAL"
    assert "PROCESS_KILL" in record.triggering_alert
    assert "runaway-worker.exe" in record.target
    assert "pid 4242" in record.target
    assert record.resolved_at is None
    assert record.mttr_seconds is None
    assert len(record.timeline) == 1


def test_resolve_incident_computes_mttr_and_appends_timeline() -> None:
    incident_log.open_incident("token-b", _command(), "HOST_LOCAL")
    result = ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="Terminated.", dry_run=False, affected_pid=4242)

    resolved = incident_log.resolve_incident("token-b", result)

    assert resolved is not None
    assert resolved.success is True
    assert resolved.resolution_message == "Terminated."
    assert resolved.mttr_seconds is not None
    assert resolved.mttr_seconds >= 0
    assert len(resolved.timeline) == 2


def test_resolve_unknown_incident_returns_none() -> None:
    assert incident_log.resolve_incident("does-not-exist", ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="x")) is None


def test_latest_resolved_ignores_pending_incidents() -> None:
    incident_log.open_incident("pending-token", _command(), "HOST_LOCAL")
    assert incident_log.latest_resolved() is None

    incident_log.open_incident("resolved-token", _command(raw_text="kill pid 999", target_pid=999), "HOST_LOCAL")
    incident_log.resolve_incident("resolved-token", ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="done"))

    latest = incident_log.latest_resolved()
    assert latest is not None
    assert latest.token == "resolved-token"


def test_render_markdown_includes_key_sre_fields() -> None:
    record = incident_log.open_incident("token-c", _command(), "K8S_CLUSTER")
    incident_log.resolve_incident("token-c", ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="Restarted pod."))
    record = incident_log.get("token-c")

    markdown = render_markdown(record)

    assert "# Incident Post-Mortem" in markdown
    assert "token-c" in markdown
    assert "Mean Time To Resolution" in markdown
    assert "Restarted pod." in markdown
    assert record.actor in markdown


def test_post_mortem_endpoint_404_without_any_resolved_incident(client: TestClient) -> None:
    response = client.get("/api/v1/incident/post-mortem")
    assert response.status_code == 404


def test_post_mortem_endpoint_returns_latest_resolved_json(client: TestClient) -> None:
    incident_log.open_incident("json-token", _command(), "HOST_LOCAL")
    incident_log.resolve_incident("json-token", ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="Terminated."))

    response = client.get("/api/v1/incident/post-mortem")

    assert response.status_code == 200
    body = response.json()
    assert body["token"] == "json-token"
    assert body["success"] is True
    assert "markdown" in body
    assert "Terminated." in body["markdown"]


def test_post_mortem_endpoint_by_token(client: TestClient) -> None:
    incident_log.open_incident("token-x", _command(), "HOST_LOCAL")
    incident_log.resolve_incident("token-x", ExecutionResult(success=False, intent=IntentType.PROCESS_KILL, message="Denied."))
    incident_log.open_incident("token-y", _command(raw_text="kill pid 1", target_pid=1), "HOST_LOCAL")
    incident_log.resolve_incident("token-y", ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="Terminated."))

    response = client.get("/api/v1/incident/post-mortem", params={"token": "token-x"})

    assert response.status_code == 200
    assert response.json()["token"] == "token-x"
    assert response.json()["success"] is False


def test_post_mortem_endpoint_unknown_token_404(client: TestClient) -> None:
    response = client.get("/api/v1/incident/post-mortem", params={"token": "no-such-token"})
    assert response.status_code == 404


def test_post_mortem_endpoint_markdown_is_downloadable(client: TestClient) -> None:
    incident_log.open_incident("md-token", _command(), "HOST_LOCAL")
    incident_log.resolve_incident("md-token", ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="Terminated."))

    response = client.get("/api/v1/incident/post-mortem", params={"format": "markdown"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "# Incident Post-Mortem" in response.text


def test_post_mortem_endpoint_rejects_invalid_format(client: TestClient) -> None:
    response = client.get("/api/v1/incident/post-mortem", params={"format": "xml"})
    assert response.status_code == 422


def test_pending_only_incident_is_not_exportable(client: TestClient) -> None:
    incident_log.open_incident("pending-only", _command(), "HOST_LOCAL")

    response = client.get("/api/v1/incident/post-mortem", params={"token": "pending-only"})

    assert response.status_code == 404
