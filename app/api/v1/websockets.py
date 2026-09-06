"""WebSocket routes: the telemetry HUD feed and the live voice-command audio ingestion pipeline."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import get_settings
from app.core.security import confirmation_registry
from app.schemas.command import IntentType, ParsedCommand
from app.services import system_engine
from app.services.assemblyai_client import AssemblyAIStreamingSession
from app.services.intent_parser import IntentParser
from app.services.system_engine import ProcessNotFoundError, ProtectedProcessError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websockets"])

_INSPECTION_TARGET_TO_RANKING = {
    "MEMORY": "memory",
    "CPU": "cpu",
}


@router.websocket("/ws/telemetry")
async def telemetry_feed(websocket: WebSocket) -> None:
    """Subscribe this connection to periodic (default 1s) system telemetry broadcasts."""
    broadcaster = websocket.app.state.telemetry_broadcaster
    await broadcaster.connections.connect(websocket)
    try:
        while True:
            # This endpoint is broadcast-only; block on receive purely to detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.connections.disconnect(websocket)


@router.websocket("/ws/voice-stream")
async def voice_stream(websocket: WebSocket) -> None:
    """Ingest a live PCM16 audio stream, transcribe it via AssemblyAI, and act on recognized commands."""
    await websocket.accept()
    settings = get_settings()
    parser = IntentParser()
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def on_turn(transcript: str, end_of_turn: bool) -> None:
        await websocket.send_json({"type": "transcript", "text": transcript, "end_of_turn": end_of_turn})
        if end_of_turn:
            await _handle_command(websocket, parser, transcript)

    session = AssemblyAIStreamingSession(
        api_key=settings.ASSEMBLYAI_API_KEY.get_secret_value(),
        on_turn=on_turn,
    )

    async def audio_frames():
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                return
            yield chunk

    stream_task: asyncio.Task[None] | None = None
    try:
        await session.connect()
        stream_task = asyncio.create_task(session.stream(audio_frames()))

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if (audio_bytes := message.get("bytes")) is not None:
                await audio_queue.put(audio_bytes)
            elif message.get("text") == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("voice stream session failed")
    finally:
        await audio_queue.put(None)
        if stream_task is not None:
            try:
                await asyncio.wait_for(stream_task, timeout=5)
            except Exception:
                stream_task.cancel()
        await session.close()
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()


async def _handle_command(websocket: WebSocket, parser: IntentParser, transcript: str) -> None:
    """Parse a finalized transcript and act on it: execute INSPECT queries, gate mutations."""
    command = parser.parse(transcript)

    if command.intent is IntentType.INSPECT:
        await _execute_inspection(websocket, parser, command)
        return

    if command.intent in (IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE):
        await _gate_mutation(websocket, command)
        return

    await websocket.send_json(
        {"type": "command_result", "success": False, "intent": IntentType.UNKNOWN.value, "message": f"Could not understand command: '{transcript}'."}
    )


async def _execute_inspection(websocket: WebSocket, parser: IntentParser, command: ParsedCommand) -> None:
    settings = get_settings()
    ranking = _INSPECTION_TARGET_TO_RANKING.get(
        command.inspection_target.value if command.inspection_target else "", "memory"
    )
    processes = await system_engine.get_top_processes(by=ranking, limit=settings.TOP_PROCESS_LIMIT)
    parser.remember_inspection(command.inspection_target, processes)

    await websocket.send_json(
        {
            "type": "command_result",
            "success": True,
            "intent": IntentType.INSPECT.value,
            "message": f"Top {len(processes)} processes by {ranking}.",
            "processes": [p.model_dump(mode="json") for p in processes],
        }
    )


async def _gate_mutation(websocket: WebSocket, command: ParsedCommand) -> None:
    if command.target_pid is None:
        await websocket.send_json(
            {
                "type": "command_result",
                "success": False,
                "intent": command.intent.value,
                "message": "No target process could be identified for this command.",
            }
        )
        return

    try:
        preview = (
            await system_engine.terminate_process(command.target_pid, dry_run=True)
            if command.intent is IntentType.PROCESS_KILL
            else await system_engine.isolate_network(command.target_pid, dry_run=True)
        )
    except ProtectedProcessError as exc:
        await websocket.send_json(
            {"type": "command_result", "success": False, "intent": command.intent.value, "message": str(exc)}
        )
        return
    except ProcessNotFoundError as exc:
        await websocket.send_json(
            {"type": "command_result", "success": False, "intent": command.intent.value, "message": str(exc)}
        )
        return

    pending = confirmation_registry.register(command)
    await websocket.send_json(
        {
            "type": "confirmation_required",
            "intent": command.intent.value,
            "token": pending.token,
            "expires_at": pending.expires_at.isoformat(),
            "preview": preview.model_dump(mode="json"),
        }
    )
