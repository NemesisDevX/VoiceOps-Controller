"""FastAPI `TestClient` tests for health, state, and command-confirmation REST endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.security import confirmation_registry
from app.schemas.command import ExecutionResult, IntentType, ParsedCommand, PendingConfirmation
from app.services import system_engine


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_state_endpoint_returns_telemetry_shape(client: TestClient) -> None:
    response = client.get("/api/v1/state")

    assert response.status_code == 200
    body = response.json()
    assert set(["cpu_percent", "memory_percent", "disk_percent", "active_sockets", "top_memory_processes", "top_cpu_processes"]).issubset(body.keys())


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
