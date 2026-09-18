"""WebSocket routes: the telemetry HUD feed and the live voice-command audio ingestion pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import get_settings
from app.core.security import confirmation_registry
from app.schemas.command import ExecutionResult, IntentType, ParsedCommand
from app.schemas.telemetry import PodInfo, TelemetryMode
from app.services import system_engine
from app.services.assemblyai_client import AssemblyAIStreamingError, AssemblyAIStreamingSession
from app.services.incident_log import incident_log
from app.services.intent_parser import DISAMBIGUATION_TTL_SECONDS, IntentParser
from app.services.mitigation_stack import mitigation_stack
from app.services.system_engine import ProcessNotFoundError, ProtectedProcessError

MUTATING_INTENTS = frozenset({IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE, IntentType.ROLLBACK})

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websockets"])

_INSPECTION_TARGET_TO_RANKING = {
    "MEMORY": "memory",
    "CPU": "cpu",
}

SAMPLE_RATE = 16_000
AUDIO_QUEUE_MAXSIZE = 8
AUDIO_BACKPRESSURE_TIMEOUT = 2.0
PROVIDER_CONNECT_TIMEOUT = 10.0
STREAM_DRAIN_TIMEOUT = 5.0
CLEANUP_TIMEOUT = 5.0
SOCKET_SEND_TIMEOUT = 2.0


class _VoiceStreamError(Exception):
    def __init__(self, message: str, retryable: bool, close_code: int = 1011) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.close_code = close_code


class _LockedSender:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.lock = asyncio.Lock()

    async def send_json(self, data: dict) -> None:
        async with asyncio.timeout(SOCKET_SEND_TIMEOUT):
            async with self.lock:
                await self.websocket.send_json(data)

    async def close(self, code: int) -> None:
        async with asyncio.timeout(SOCKET_SEND_TIMEOUT):
            async with self.lock:
                if (
                    self.websocket.client_state == WebSocketState.CONNECTED
                    and self.websocket.application_state == WebSocketState.CONNECTED
                ):
                    await self.websocket.close(code=code)


def _same_origin(websocket: WebSocket) -> bool:
    origins = websocket.headers.getlist("origin")
    if not origins:
        return True
    if len(origins) != 1:
        return False
    try:
        origin = urlsplit(origins[0])
        expected = urlsplit(str(websocket.url))
        scheme = "https" if expected.scheme == "wss" else "http"
        if (
            origin.scheme not in ("http", "https") or not origin.hostname
            or origin.username is not None or origin.password is not None
            or origin.path or origin.query or origin.fragment
        ):
            return False
        origin_port = origin.port if origin.port is not None else (443 if origin.scheme == "https" else 80)
        expected_port = expected.port if expected.port is not None else (443 if scheme == "https" else 80)
        return origin.scheme == scheme and origin.hostname == expected.hostname and origin_port == expected_port
    except ValueError:
        return False


@router.websocket("/ws/telemetry")
async def telemetry_feed(websocket: WebSocket) -> None:
    """Subscribe this connection to periodic (default 1s) system telemetry broadcasts."""
    if not _same_origin(websocket):
        await websocket.close(code=1008)
        return
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
    if not _same_origin(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    sender = _LockedSender(websocket)
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
    failure_event = asyncio.Event()
    ready_sent = asyncio.Event()
    provider_error: tuple[str, bool] | None = None
    session: AssemblyAIStreamingSession | None = None
    tasks: set[asyncio.Task] = set()
    error: dict | None = None
    close_code = 1000
    accepting_turns = True
    last_audio_at: float | None = None
    audio_marker_ts: float | None = None

    async def on_turn(transcript: str, end_of_turn: bool) -> None:
        await ready_sent.wait()
        if not accepting_turns:
            return
        await sender.send_json({"type": "transcript", "text": transcript, "end_of_turn": end_of_turn})
        if end_of_turn:
            nonlocal audio_marker_ts
            t1 = time.time()
            # t0 anchors the waterfall at the most recent audio packet: an explicit
            # client "audio_marker" if one arrived, otherwise the last PCM frame the
            # provider consumed -- i.e. the moment the operator finished speaking.
            t0 = audio_marker_ts if (audio_marker_ts is not None and audio_marker_ts <= t1) else last_audio_at
            audio_marker_ts = None
            broadcaster = getattr(websocket.app.state, "telemetry_broadcaster", None)
            mode = await broadcaster.get_mode() if broadcaster is not None else TelemetryMode.HOST_LOCAL
            await _handle_command(sender, parser, transcript, mode, timing={"t0": t0, "t1": t1})

    async def on_error(message: str, retryable: bool) -> None:
        nonlocal provider_error
        if provider_error is None:
            provider_error = (message, retryable)
            failure_event.set()

    def check_provider_error() -> None:
        if provider_error is not None:
            raise AssemblyAIStreamingError(*provider_error)

    async def audio_frames():
        nonlocal last_audio_at
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                return
            last_audio_at = time.time()
            yield chunk

    async def enqueue(chunk: bytes | None) -> None:
        try:
            await asyncio.wait_for(audio_queue.put(chunk), AUDIO_BACKPRESSURE_TIMEOUT)
        except TimeoutError:
            raise _VoiceStreamError("Audio upload is too slow. Please reconnect and try again.", True, 1013) from None

    async def receive_audio() -> bool:
        nonlocal audio_marker_ts
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return False
            if (audio_bytes := message.get("bytes")) is not None:
                if not 1600 <= len(audio_bytes) <= 32000 or len(audio_bytes) % 2:
                    raise _VoiceStreamError("Audio frames must be PCM16, 1600 to 32000 bytes, with an even byte count.", False, 1008)
                await enqueue(audio_bytes)
            elif (text := message.get("text")) is not None:
                if text == "stop":
                    await enqueue(None)
                    return True
                marker_ts = _decode_audio_marker(text)
                if marker_ts is not None:
                    audio_marker_ts = marker_ts
                    continue
                raise _VoiceStreamError("Send binary PCM16 audio frames or the text 'stop'.", False, 1008)
            else:
                raise _VoiceStreamError("Send binary PCM16 audio frames or the text 'stop'.", False, 1008)

    async def stream_and_close() -> None:
        await session.stream(audio_frames())
        await session.close()

    try:
        settings = get_settings()
        parser = IntentParser()
        session = AssemblyAIStreamingSession(
            api_key=settings.ASSEMBLYAI_API_KEY.get_secret_value(),
            on_turn=on_turn, sample_rate=SAMPLE_RATE, on_error=on_error,
        )
        connect_task = asyncio.create_task(session.connect(), name="voice.connect")
        receive_task = asyncio.create_task(receive_audio(), name="voice.receive")
        failure_task = asyncio.create_task(failure_event.wait(), name="voice.failure")
        tasks.update((connect_task, receive_task, failure_task))
        done, _ = await asyncio.wait(tasks, timeout=PROVIDER_CONNECT_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        check_provider_error()
        if receive_task in done:
            stopped = receive_task.result()
            if not stopped or connect_task not in done:
                return
        if connect_task not in done:
            raise _VoiceStreamError("Speech provider connection timed out. Please try again.", True)
        await connect_task
        check_provider_error()
        await sender.send_json({"type": "ready", "sample_rate": SAMPLE_RATE})
        ready_sent.set()
        stream_task = asyncio.create_task(stream_and_close(), name="voice.stream")
        tasks.add(stream_task)
        done, _ = await asyncio.wait({stream_task, receive_task, failure_task}, return_when=asyncio.FIRST_COMPLETED)
        check_provider_error()
        if receive_task in done:
            if receive_task.result():
                await asyncio.wait_for(stream_task, STREAM_DRAIN_TIMEOUT)
                check_provider_error()
        else:
            await stream_task
            raise _VoiceStreamError("Speech provider ended the session. Please reconnect.", True)
    except WebSocketDisconnect:
        pass
    except _VoiceStreamError as exc:
        error = {"type": "error", "message": str(exc), "retryable": exc.retryable}
        close_code = exc.close_code
    except AssemblyAIStreamingError as exc:
        error = {"type": "error", "message": str(exc), "retryable": exc.retryable}
        close_code = 1011
    except Exception:
        logger.warning("voice stream session failed")
        error = {"type": "error", "message": "Voice streaming is unavailable. Please try again.", "retryable": True}
        close_code = 1011
    finally:
        accepting_turns = False
        ready_sent.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), CLEANUP_TIMEOUT)
            except TimeoutError:
                logger.warning("voice stream task cleanup timed out")
        if error is not None and websocket.client_state == WebSocketState.CONNECTED:
            try:
                await sender.send_json(error)
            except Exception:
                pass
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), CLEANUP_TIMEOUT)
            except Exception:
                logger.warning("voice stream provider cleanup failed")
        try:
            await sender.close(close_code)
        except Exception:
            pass


def _decode_audio_marker(text: str) -> float | None:
    """Decode a client `{"type": "audio_marker", "ts": <ms>}` control message to epoch seconds."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "audio_marker":
        return None
    ts = payload.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return float(ts) / 1000.0


def _build_waterfall(timing: dict | None, t2: float) -> dict[str, float] | None:
    """Assemble the t0→t2 latency checkpoints recorded on the voice pipeline."""
    if not timing:
        return None
    t0 = timing.get("t0")
    t1 = timing.get("t1") or t2
    waterfall: dict[str, float] = {
        "t1_ms": round(t1 * 1000.0, 3),
        "t2_ms": round(t2 * 1000.0, 3),
        "gate_ms": round(max(0.0, (t2 - t1) * 1000.0), 3),
    }
    if t0 is not None:
        waterfall["t0_ms"] = round(t0 * 1000.0, 3)
        waterfall["stt_ms"] = round(max(0.0, (t1 - t0) * 1000.0), 3)
    return waterfall


def _ambiguity_message(names: list[str]) -> str:
    joined = " and ".join(names) if len(names) <= 2 else f"{', '.join(names[:-1])}, and {names[-1]}"
    quantifier = "both" if len(names) == 2 else "all"
    return f"Ambiguity detected: {joined} are {quantifier} degraded. Specify target."


async def _emit_disambiguation(
    websocket: WebSocket | _LockedSender,
    intent: IntentType,
    candidates: list[str],
    language: str,
    expires_at: datetime,
    pods: list[PodInfo] | None = None,
) -> None:
    """Emit a `disambiguation_required` event carrying the unresolved candidate set."""
    payload_candidates = (
        [pod.model_dump(mode="json") for pod in pods] if pods is not None else [{"name": name} for name in candidates]
    )
    await websocket.send_json(
        {
            "type": "disambiguation_required",
            "intent": intent.value,
            "language": language,
            "candidates": payload_candidates,
            "expires_at": expires_at.isoformat(),
            "message": _ambiguity_message(candidates),
        }
    )


async def _degraded_pod_candidates(command: ParsedCommand) -> list[PodInfo]:
    """Return the pods matching a target-less mutation's implied "degraded" condition.

    Candidates are anomalous or memory-saturated (>=75%) pods. If the transcript already
    names a distinctive token of some degraded pod (e.g. "payment"), the set is narrowed
    to those matches so "isolate payment" resolves deterministically.
    """
    telemetry = await system_engine.get_cluster_telemetry()
    degraded = [pod for pod in telemetry.pods if pod.anomaly or pod.memory_percent >= 75.0]
    if len(degraded) <= 1:
        return degraded
    normalized = unicodedata.normalize("NFKC", command.raw_text.strip().lower())
    matched = [pod for pod in degraded if IntentParser._candidate_score(normalized, pod.name) > 0]
    return matched or degraded


async def _handle_command(
    websocket: WebSocket | _LockedSender, parser: IntentParser, transcript: str,
    mode: TelemetryMode = TelemetryMode.HOST_LOCAL, timing: dict | None = None,
) -> None:
    """Parse a finalized transcript and act on it: execute INSPECT queries, gate mutations."""
    pending_disambiguation = parser.pending_disambiguation
    command: ParsedCommand | None = None
    if pending_disambiguation is not None:
        resolved = parser.try_resolve_disambiguation(transcript)
        if resolved is not None and resolved.intent in MUTATING_INTENTS:
            command = resolved
        else:
            fresh = parser.parse(transcript)
            if fresh.intent is not IntentType.UNKNOWN:
                parser.clear_disambiguation()
                command = fresh
            else:
                await _emit_disambiguation(
                    websocket,
                    pending_disambiguation.intent,
                    parser.narrow_candidates(transcript),
                    pending_disambiguation.language,
                    pending_disambiguation.expires_at,
                )
                return
    if command is None:
        command = parser.parse(transcript)

    if command.intent is IntentType.INSPECT:
        await _execute_inspection(websocket, parser, command, mode)
        return

    if command.intent in MUTATING_INTENTS:
        await _gate_mutation(websocket, command, mode, parser=parser, timing=timing)
        return

    await websocket.send_json(
        {
            "type": "command_result", "success": False, "intent": IntentType.UNKNOWN.value,
            "language": command.language, "message": f"Could not understand command: '{transcript}'.",
        }
    )


async def _execute_inspection(
    websocket: WebSocket | _LockedSender, parser: IntentParser, command: ParsedCommand,
    mode: TelemetryMode = TelemetryMode.HOST_LOCAL,
) -> None:
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
            "language": command.language,
            "mode": mode.value,
            "message": f"Top {len(processes)} processes by {ranking}.",
            "processes": [p.model_dump(mode="json") for p in processes],
        }
    )


async def _gate_mutation(
    websocket: WebSocket | _LockedSender,
    command: ParsedCommand,
    mode: TelemetryMode = TelemetryMode.HOST_LOCAL,
    parser: IntentParser | None = None,
    timing: dict | None = None,
) -> None:
    cluster_mode = mode is TelemetryMode.K8S_CLUSTER
    rollback_preview: ExecutionResult | None = None

    if command.intent is IntentType.ROLLBACK and not command.target_process_name:
        # Bare multilingual "rollback"/"undo" reverses the newest executed mitigation.
        rollback_preview = await mitigation_stack.preview_rollback()
        if rollback_preview is None:
            await websocket.send_json(
                {
                    "type": "command_result",
                    "success": False,
                    "intent": command.intent.value,
                    "language": command.language,
                    "message": "No prior mitigation is available to roll back.",
                }
            )
            return
    elif not cluster_mode and command.intent is IntentType.ROLLBACK:
        await websocket.send_json(
            {
                "type": "command_result", "success": False, "intent": command.intent.value,
                "language": command.language, "message": "ROLLBACK is only available in K8S_CLUSTER telemetry mode.",
            }
        )
        return
    elif cluster_mode and not command.target_process_name:
        # A target-less cluster mutation ("kill the failing pod") resolves against the
        # degraded pod set; multiple matches gate on operator disambiguation instead of
        # failing or picking arbitrarily.
        candidates = await _degraded_pod_candidates(command)
        if len(candidates) > 1:
            names = [pod.name for pod in candidates]
            if parser is not None:
                pending_disamb = parser.set_disambiguation(command.intent, names, language=command.language)
                expires_at = pending_disamb.expires_at
            else:
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=DISAMBIGUATION_TTL_SECONDS)
            await _emit_disambiguation(
                websocket, command.intent, names, command.language, expires_at, pods=candidates
            )
            return
        if candidates:
            command.target_process_name = candidates[0].name
            command.resolved_from_context = True

    if rollback_preview is not None:
        preview = rollback_preview
    else:
        target_label = command.target_process_name if cluster_mode else command.target_pid
        if target_label is None:
            await websocket.send_json(
                {
                    "type": "command_result",
                    "success": False,
                    "intent": command.intent.value,
                    "language": command.language,
                    "message": "No target process could be identified for this command.",
                }
            )
            return

        try:
            if cluster_mode:
                preview = await system_engine.cluster_execute(command.intent, command.target_process_name, dry_run=True)
            elif command.intent is IntentType.PROCESS_KILL:
                preview = await system_engine.terminate_process(command.target_pid, dry_run=True)
            else:
                preview = await system_engine.isolate_network(command.target_pid, dry_run=True)
        except ProtectedProcessError as exc:
            await websocket.send_json(
                {"type": "command_result", "success": False, "intent": command.intent.value, "language": command.language, "message": str(exc)}
            )
            return
        except ProcessNotFoundError as exc:
            await websocket.send_json(
                {"type": "command_result", "success": False, "intent": command.intent.value, "language": command.language, "message": str(exc)}
            )
            return

        if not preview.success:
            await websocket.send_json({"type": "command_result", "language": command.language, **preview.model_dump(mode="json")})
            return

    waterfall = _build_waterfall(timing, time.time())
    pending = confirmation_registry.register(command, mode=mode.value)
    incident_log.open_incident(pending.token, command, mode.value, waterfall=waterfall)
    await websocket.send_json(
        {
            "type": "confirmation_required",
            "intent": command.intent.value,
            "language": command.language,
            "mode": mode.value,
            "token": pending.token,
            "expires_at": pending.expires_at.isoformat(),
            "preview": preview.model_dump(mode="json"),
            "waterfall": waterfall,
        }
    )
