"""FastAPI `TestClient` tests for health, state, and command-confirmation REST endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psutil
import pytest
from fastapi.testclient import TestClient

from app.core.security import confirmation_registry
from app.schemas.command import ExecutionResult, IntentType, ParsedCommand, PendingConfirmation
from app.services import system_engine


def test_dashboard_is_served_at_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="waveform"' in response.text
    assert 'id="confirmation-dialog"' in response.text
    assert '/static/app.js' in response.text


@pytest.mark.parametrize("asset,content_type", [("style.css", "text/css"), ("app.js", "javascript")])
def test_dashboard_static_assets(client: TestClient, asset: str, content_type: str) -> None:
    response = client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_static_mount_does_not_expose_private_files(client: TestClient) -> None:
    for path in ("/.env", "/static/.env", "/static/../core/config.py", "/static/missing.js"):
        assert client.get(path).status_code == 404


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_state_endpoint_returns_telemetry_shape(client: TestClient) -> None:
    response = client.get("/api/v1/state")

    assert response.status_code == 200
    body = response.json()
    assert set(["cpu_percent", "memory_percent", "disk_percent", "active_sockets", "top_memory_processes", "top_cpu_processes"]).issubset(body.keys())


@pytest.mark.parametrize("intent", [IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE])
@pytest.mark.parametrize("dry_run", [False, True])
def test_confirm_permission_failure_is_clear_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, intent: IntentType, dry_run: bool,
) -> None:
    pending = confirmation_registry.register(
        ParsedCommand(raw_text="kill pid 999", intent=intent, target_pid=999, requires_confirmation=True)
    )

    def denied_process(pid: int) -> None:
        raise psutil.AccessDenied(pid=pid)

    monkeypatch.setattr(psutil, "Process", denied_process)
    response = client.post("/api/v1/commands/confirm", json={"token": pending.token, "dry_run": dry_run})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["dry_run"] is dry_run
    assert "Permission denied" in body["message"]
    assert "Administrator" in body["message"]


def test_confirm_unknown_token_returns_404(client: TestClient) -> None:
    response = client.post("/api/v1/commands/confirm", json={"token": "does-not-exist"})

    assert response.status_code == 404


def test_confirm_executes_pending_process_kill(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    command = ParsedCommand(
        raw_text="kill pid 999",
        intent=IntentType.PROCESS_KILL,
        target_pid=999,
        requires_confirmation=True,
    )
    now = datetime.now(timezone.utc)
    pending = PendingConfirmation(
        token="test-token-123",
        command=command,
        created_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    confirmation_registry._pending[pending.token] = pending

    async def fake_terminate(pid: int, dry_run: bool = False) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            intent=IntentType.PROCESS_KILL,
            message=f"Terminated process (pid={pid}).",
            dry_run=dry_run,
            affected_pid=pid,
        )

    monkeypatch.setattr(system_engine, "terminate_process", fake_terminate)

    response = client.post("/api/v1/commands/confirm", json={"token": "test-token-123"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["affected_pid"] == 999

    # Token is single-use: a second redemption must now fail.
    replay = client.post("/api/v1/commands/confirm", json={"token": "test-token-123"})
    assert replay.status_code == 404


def test_confirm_dry_run_does_not_consume_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    command = ParsedCommand(
        raw_text="isolate pid 888",
        intent=IntentType.NETWORK_ISOLATE,
        target_pid=888,
        requires_confirmation=True,
    )
    now = datetime.now(timezone.utc)
    pending = PendingConfirmation(
        token="dry-run-token",
        command=command,
        created_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    confirmation_registry._pending[pending.token] = pending

    async def fake_isolate(pid: int, dry_run: bool = False) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            intent=IntentType.NETWORK_ISOLATE,
            message="[DRY RUN] preview",
            dry_run=dry_run,
            affected_pid=pid,
        )

    monkeypatch.setattr(system_engine, "isolate_network", fake_isolate)

    response = client.post("/api/v1/commands/confirm", json={"token": "dry-run-token", "dry_run": True})

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert confirmation_registry.peek("dry-run-token") is not None


def test_confirm_rejects_non_mutating_pending_command(client: TestClient) -> None:
    command = ParsedCommand(raw_text="show top memory", intent=IntentType.INSPECT)
    now = datetime.now(timezone.utc)
    pending = PendingConfirmation(
        token="inspect-token",
        command=command,
        created_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    confirmation_registry._pending[pending.token] = pending

    response = client.post("/api/v1/commands/confirm", json={"token": "inspect-token"})

    assert response.status_code == 400
