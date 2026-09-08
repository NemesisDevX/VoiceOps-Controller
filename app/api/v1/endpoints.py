"""REST endpoints: liveness, system state snapshot, and mutating-command confirmation."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import confirmation_registry
from app.schemas.command import ConfirmationRequest, ExecutionResult, IntentType
from app.schemas.telemetry import ClusterTelemetry, SystemTelemetry, TelemetryMode
from app.services import discord_alerts, system_engine
from app.services.incident_log import incident_log
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


class TelemetryModeResponse(BaseModel):
    """Response body for `GET`/`POST /api/v1/telemetry/mode`."""

    mode: TelemetryMode = Field(..., description="The currently active telemetry mode.")


class TelemetryModeRequest(BaseModel):
    """Request body for `POST /api/v1/telemetry/mode`."""

    mode: TelemetryMode = Field(..., description="The telemetry mode to switch to.")


@commands_router.get(
    "/telemetry/mode",
    response_model=TelemetryModeResponse,
    summary="Get the currently active telemetry mode",
)
async def get_telemetry_mode(request: Request) -> TelemetryModeResponse:
    """Return whether the HUD/voice pipeline is currently bound to HOST_LOCAL or K8S_CLUSTER telemetry."""
    broadcaster = request.app.state.telemetry_broadcaster
    mode = await broadcaster.get_mode()
    return TelemetryModeResponse(mode=mode)


@commands_router.post(
    "/telemetry/mode",
    response_model=TelemetryModeResponse,
    summary="Switch the active telemetry mode",
)
async def set_telemetry_mode(request: Request, body: TelemetryModeRequest) -> TelemetryModeResponse:
    """Switch which telemetry source subsequent broadcast cycles sample from."""
    broadcaster = request.app.state.telemetry_broadcaster
    await broadcaster.set_mode(body.mode)
    return TelemetryModeResponse(mode=body.mode)


@commands_router.get(
    "/cluster/state",
    response_model=ClusterTelemetry,
    summary="Current simulated Kubernetes cluster telemetry snapshot",
)
async def get_cluster_state() -> ClusterTelemetry:
    """Return a single, on-demand simulated cluster telemetry snapshot (rps, p99, error rate, pods)."""
    return await system_engine.get_cluster_telemetry()


@commands_router.post(
    "/commands/confirm",
    response_model=ExecutionResult,
    summary="Redeem a confirmation token and execute (or dry-run) the pending mutating command",
)
async def confirm_command(request: ConfirmationRequest, background_tasks: BackgroundTasks) -> ExecutionResult:
    """Execute the mutating command associated with `token`, consuming it in the process.

    If `dry_run` is True, the pending command is evaluated and reported without mutating
    system state, and the token is *not* consumed so it may still be redeemed for real.

    On successful, non-dry-run mitigation, a Discord webhook alert is dispatched
    asynchronously in the background.
    """
    pending = confirmation_registry.peek(request.token) if request.dry_run else confirmation_registry.redeem(request.token)
    if pending is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or expired confirmation token.")

    command = pending.command
    cluster_intents = frozenset({IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE, IntentType.ROLLBACK})

    try:
        if pending.mode == "K8S_CLUSTER" and command.intent in cluster_intents:
            if not command.target_process_name:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Pending command has no target pod name."
                )
            result = await system_engine.cluster_execute(
                command.intent, command.target_process_name, dry_run=request.dry_run
            )
        elif command.intent is IntentType.ROLLBACK:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ROLLBACK is only available in K8S_CLUSTER telemetry mode.",
            )
        elif command.intent is IntentType.PROCESS_KILL:
            if command.target_pid is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pending command has no target PID.")
            result = await system_engine.terminate_process(command.target_pid, dry_run=request.dry_run)
        elif command.intent is IntentType.NETWORK_ISOLATE:
            if command.target_pid is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pending command has no target PID.")
            result = await system_engine.isolate_network(command.target_pid, dry_run=request.dry_run)
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Intent {command.intent} is not mutating.")
    except ProtectedProcessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ProcessNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not request.dry_run:
        record = incident_log.resolve_incident(request.token, result)
        if record is not None and record.success:
            background_tasks.add_task(discord_alerts.send_resolution_alert, record)
    return result
