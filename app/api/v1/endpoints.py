"""REST endpoints: liveness, system state snapshot, and mutating-command confirmation."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings
from app.core.security import confirmation_registry
from app.schemas.command import ConfirmationRequest, ExecutionResult, IntentType
from app.schemas.telemetry import SystemTelemetry
from app.services import system_engine
from app.services.system_engine import ProcessNotFoundError, ProtectedProcessError

logger = logging.getLogger(__name__)

health_router = APIRouter(tags=["health"])
commands_router = APIRouter(tags=["state", "commands"])


@health_router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Return a trivial payload indicating the service process is up."""
    return {"status": "ok"}


@commands_router.get("/state", response_model=SystemTelemetry, summary="Current system telemetry snapshot")
async def get_state() -> SystemTelemetry:
    """Return a single, on-demand system telemetry snapshot (CPU, memory, disk, sockets, top processes)."""
    settings = get_settings()
    return await system_engine.get_system_telemetry(settings.TOP_PROCESS_LIMIT)


@commands_router.post(
    "/commands/confirm",
    response_model=ExecutionResult,
    summary="Redeem a confirmation token and execute (or dry-run) the pending mutating command",
)
async def confirm_command(request: ConfirmationRequest) -> ExecutionResult:
    """Execute the mutating command associated with `token`, consuming it in the process.

    If `dry_run` is True, the pending command is evaluated and reported without mutating
    system state, and the token is *not* consumed so it may still be redeemed for real.
    """
    pending = confirmation_registry.peek(request.token) if request.dry_run else confirmation_registry.redeem(request.token)
    if pending is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or expired confirmation token.")

    command = pending.command
    try:
        if command.intent is IntentType.PROCESS_KILL:
            if command.target_pid is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pending command has no target PID.")
            return await system_engine.terminate_process(command.target_pid, dry_run=request.dry_run)

        if command.intent is IntentType.NETWORK_ISOLATE:
            if command.target_pid is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pending command has no target PID.")
            return await system_engine.isolate_network(command.target_pid, dry_run=request.dry_run)

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Intent {command.intent} is not mutating.")
    except ProtectedProcessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ProcessNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
