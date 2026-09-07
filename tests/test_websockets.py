from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from app.api.v1 import websockets as routes
from app.core.security import confirmation_registry
from app.schemas.command import ExecutionResult, IntentType, ParsedCommand
from app.services import assemblyai_client, system_engine


PCM_FRAME = b"\x00\x00" * 1600


def turn(text: str, order: int = 0, final: bool = True, formatted: bool = False) -> dict:
    return {
        "type": "Turn", "turn_order": order, "turn_is_formatted": formatted,
        "end_of_turn": final, "transcript": text, "end_of_turn_confidence": 1.0, "words": [],
    }


def begin() -> dict:
    return {"type": "Begin", "id": "fake-session", "expires_at": 2000000000, "configuration": {"model": "universal-streaming-english"}}


def provider_error(code: int) -> dict:
    return {"type": "Error", "error_code": code, "error": "provider-secret"}


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(
        instances=[], requests=[], events=[], final_events=[], connect_error=None,
        setup_event=None, connect_blocked=False, begin_blocked=False, upload_blocked=False,
        write_error=None, read_error=None, close_blocked=False, termination_failure=False,
        opened=asyncio.Event(),
    )

    class FakeTransport:
        def __init__(self):
            self.frames = []
            self.sent = []
            self.closed = False
            self.begin_received = False
            self.incoming = asyncio.Queue()
            self.sending = False
            self.send_cancelled = False
            if state.setup_event is not None:
                self.push(state.setup_event)
            elif not state.begin_blocked:
                self.push(begin())
            state.instances.append(self)

        def push(self, event):
            self.incoming.put_nowait(json.dumps(event) if isinstance(event, dict) else event)

        async def recv(self):
            raw = await self.incoming.get()
            if isinstance(raw, Exception):
                raise raw
            if self.begin_received and state.read_error:
                raise state.read_error
            if isinstance(raw, str) and '"type": "Begin"' in raw:
                self.begin_received = True
            return raw

        async def send(self, data):
            assert not self.sending
            self.sending = True
            try:
                if isinstance(data, bytes):
                    assert self.begin_received
                    if state.upload_blocked:
                        await asyncio.Event().wait()
                    if state.write_error:
                        raise state.write_error
                    self.frames.append(data)
                    if state.events:
                        for event in state.events.pop(0):
                            self.push(event)
                else:
                    assert json.loads(data) == {"type": "Terminate"}
                    if state.termination_failure:
                        raise RuntimeError("provider-secret")
                    if not state.close_blocked:
                        for event in state.final_events:
                            self.push(event)
                        self.push({"type": "Termination", "audio_duration_seconds": 1, "session_duration_seconds": 1})
                self.sent.append(data)
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                self.send_cancelled = True
                raise
            finally:
                self.sending = False

        async def close(self):
            self.closed = True

    async def fake_connect(url, **kwargs):
        state.requests.append((url, kwargs))
        if state.connect_error:
            raise state.connect_error
        if state.connect_blocked:
            await asyncio.Event().wait()
        transport = FakeTransport()
        state.opened.set()
        return transport

    monkeypatch.setattr(assemblyai_client, "websocket_connect", fake_connect)
    return state


class VoiceHarness:
    def __init__(self, frames=(), *, stop=True, origin=None, host="testserver", scheme="ws", disconnect=False):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.outgoing = []
        self.frames = frames
        self.stop = stop
        self.disconnect = disconnect
        self.sending = False
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        self.scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "scheme": scheme, "path": "/api/v1/ws/voice-stream", "raw_path": b"/api/v1/ws/voice-stream",
            "query_string": b"", "root_path": "", "headers": headers,
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80), "subprotocols": [],
        }

    @property
    def messages(self):
        return [json.loads(item["text"]) for item in self.outgoing if item["type"] == "websocket.send"]

    @property
    def errors(self):
        return [item for item in self.messages if item["type"] == "error"]

    async def send(self, message):
        assert not self.sending
        self.sending = True
        try:
            await asyncio.sleep(0)
            self.outgoing.append(message)
            if message["type"] == "websocket.send" and json.loads(message["text"])["type"] == "ready":
                for frame in self.frames:
                    self.incoming.put_nowait({"type": "websocket.receive", "bytes": frame})
                if self.disconnect:
                    self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
                elif self.stop:
                    self.incoming.put_nowait({"type": "websocket.receive", "text": "stop"})
        finally:
            self.sending = False

    async def run(self, timeout=2):
        app = FastAPI()
        app.include_router(routes.router, prefix="/api/v1")
        existing = asyncio.all_tasks()
        await asyncio.wait_for(app(self.scope, self.incoming.get, self.send), timeout=timeout)
        await asyncio.sleep(0)
        assert not [task for task in asyncio.all_tasks() - existing if not task.done()]


def assert_closed(provider):
    for instance in provider.instances:
        assert instance.closed
        assert not instance.sending


def assert_safe_error(harness, retryable):
    assert len(harness.errors) == 1
    assert set(harness.errors[0]) == {"type", "message", "retryable"}
    assert isinstance(harness.errors[0]["message"], str)
    assert harness.errors[0]["message"]
    assert harness.errors[0]["retryable"] is retryable
    assert "provider-secret" not in json.dumps(harness.outgoing)


@pytest.mark.asyncio
async def test_ready_binary_forwarding_and_stop_drains(provider):
    frames = [PCM_FRAME, b"\x01\x00" * 800, b"\x02\x00" * 16000]
    harness = VoiceHarness(frames)
    await harness.run()
    assert harness.messages == [{"type": "ready", "sample_rate": 16000}]
    assert provider.instances[0].frames == frames
    url, options = provider.requests[0]
    assert urlsplit(url).scheme == "wss"
    assert urlsplit(url).netloc == "streaming.assemblyai.com"
    assert urlsplit(url).path == "/v3/ws"
    assert parse_qs(urlsplit(url).query) == {
        "sample_rate": ["16000"], "encoding": ["pcm_s16le"],
        "speech_model": ["universal-streaming-english"], "format_turns": ["true"],
    }
    assert set(options["additional_headers"]) == {"Authorization"}
    assert options["additional_headers"]["Authorization"] not in url
    assert 0 < options["max_queue"] <= 8
    assert 0 < options["write_limit"] <= 32768
    assert 0 < options["max_size"] <= 262144
    assert provider.instances[0].begin_received
    assert json.loads(provider.instances[0].sent[-1]) == {"type": "Terminate"}
    assert harness.outgoing[-1] == {"type": "websocket.close", "code": 1000, "reason": ""}
    assert_closed(provider)


@pytest.mark.parametrize("size", [0, 2, 1598, 1601, 31999, 32002, 64000])
@pytest.mark.asyncio
async def test_invalid_pcm_frames_are_rejected(provider, size):
    harness = VoiceHarness([b"\x00" * size])
    await harness.run()
    assert_safe_error(harness, False)
    assert not provider.instances[0].frames
    assert harness.outgoing[-1]["code"] == 1008
    assert_closed(provider)


@pytest.mark.asyncio
async def test_setup_failure_is_safe_and_has_no_ready(provider):
    provider.connect_error = RuntimeError("provider-secret in setup exception")
    harness = VoiceHarness()
    await harness.run()
    assert_safe_error(harness, True)
    assert not any(message["type"] == "ready" for message in harness.messages)
    assert_closed(provider)


@pytest.mark.asyncio
async def test_constructor_failure_is_safe(provider, monkeypatch):
    monkeypatch.setattr(routes, "AssemblyAIStreamingSession", Mock(side_effect=ValueError("provider-secret")))
    harness = VoiceHarness()
    await harness.run()
    assert_safe_error(harness, True)
    assert not any(message["type"] == "ready" for message in harness.messages)


@pytest.mark.parametrize("code,retryable", [(401, False), (1008, False), (4001, False), (4002, False), (429, True), (503, True)])
@pytest.mark.asyncio
async def test_setup_callback_error_is_not_a_ready_session(provider, code, retryable):
    provider.setup_event = provider_error(code)
    harness = VoiceHarness()
    await harness.run()
    assert_safe_error(harness, retryable)
    assert not any(message["type"] == "ready" for message in harness.messages)
    assert_closed(provider)


@pytest.mark.asyncio
async def test_inspection_then_confirmation_uses_fake_engine(provider, monkeypatch, sample_processes):
    provider.events = [[turn("show top memory")], [turn("kill it", order=1)]]
    inspect = AsyncMock(return_value=sample_processes)
    preview = AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Safe preview", dry_run=True, affected_pid=4242,
    ))
    monkeypatch.setattr(system_engine, "get_top_processes", inspect)
    monkeypatch.setattr(system_engine, "terminate_process", preview)
    harness = VoiceHarness([PCM_FRAME, PCM_FRAME])
    await harness.run()
    assert [message["type"] for message in harness.messages] == [
        "ready", "transcript", "command_result", "transcript", "confirmation_required",
    ]
    assert harness.messages[2]["processes"][0]["pid"] == 4242
    pending = confirmation_registry.peek(harness.messages[-1]["token"])
    assert pending.command.target_pid == 4242
    assert pending.command.resolved_from_context is True
    inspect.assert_awaited_once()
    preview.assert_awaited_once_with(4242, dry_run=True)
    assert_closed(provider)


@pytest.mark.parametrize("intent,method", [(IntentType.PROCESS_KILL, "terminate_process"), (IntentType.NETWORK_ISOLATE, "isolate_network")])
@pytest.mark.asyncio
async def test_failed_preview_never_issues_confirmation(monkeypatch, intent, method):
    preview = AsyncMock(return_value=ExecutionResult(
        success=False, intent=intent, message="Access denied", dry_run=True, affected_pid=4242,
    ))
    monkeypatch.setattr(system_engine, method, preview)
    register = Mock()
    monkeypatch.setattr(confirmation_registry, "register", register)
    socket = SimpleNamespace(send_json=AsyncMock())
    await routes._gate_mutation(socket, ParsedCommand(raw_text="kill pid 4242", intent=intent, target_pid=4242))
    register.assert_not_called()
    response = socket.send_json.call_args.args[0]
    assert response["type"] == "command_result"
    assert response["success"] is False
    assert response["message"] == "Access denied"
    assert "token" not in response
    preview.assert_awaited_once_with(4242, dry_run=True)


@pytest.mark.asyncio
async def test_failed_preview_over_websocket_has_no_token(provider, monkeypatch):
    provider.events = [[turn("kill pid 4242")]]
    monkeypatch.setattr(system_engine, "terminate_process", AsyncMock(return_value=ExecutionResult(
        success=False, intent=IntentType.PROCESS_KILL, message="Access denied", dry_run=True,
    )))
    harness = VoiceHarness([PCM_FRAME])
    await harness.run()
    assert harness.messages[-1]["type"] == "command_result"
    assert harness.messages[-1]["success"] is False
    assert not confirmation_registry._pending


@pytest.mark.asyncio
async def test_upstream_error_cancels_upload_and_browser_receive(provider):
    provider.events = [[provider_error(3005)]]
    harness = VoiceHarness([PCM_FRAME], stop=False)
    await harness.run()
    assert_safe_error(harness, True)
    assert_closed(provider)


@pytest.mark.asyncio
async def test_partial_and_duplicate_final_turns_do_not_repeat_actions(provider, monkeypatch):
    provider.events = [[
        turn("kill pid", final=False),
        turn("kill pid 4242"),
        turn("Kill PID 4242.", formatted=True),
        turn("kill pid 4242", order=1),
        turn("Kill PID 4242.", order=0, formatted=True),
    ]]
    preview = AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Safe preview", dry_run=True,
    ))
    monkeypatch.setattr(system_engine, "terminate_process", preview)
    harness = VoiceHarness([PCM_FRAME])
    await harness.run()
    confirmations = [message for message in harness.messages if message["type"] == "confirmation_required"]
    assert len(confirmations) == 2
    assert preview.await_count == 2
    assert len(confirmation_registry._pending) == 2
    assert [message["end_of_turn"] for message in harness.messages if message["type"] == "transcript"] == [False, True, True]
    assert_closed(provider)


@pytest.mark.asyncio
async def test_stop_delivers_final_provider_transcript(provider):
    provider.final_events = [turn("unrecognized phrase")]
    harness = VoiceHarness([PCM_FRAME])
    await harness.run()
    assert [message["type"] for message in harness.messages] == ["ready", "transcript", "command_result"]
    assert harness.messages[-1]["success"] is False
    assert_closed(provider)


@pytest.mark.parametrize("origin", ["https://evil.example", "http://testserver.evil.example", "http://testserver:81", "http://testserver:0", "http://testserver:invalid", "https://testserver", "null", "http://evil.example@testserver", "http://testserver/path"])
@pytest.mark.asyncio
async def test_cross_origin_connections_are_rejected_before_provider_setup(provider, origin):
    harness = VoiceHarness(origin=origin)
    await harness.run()
    assert harness.outgoing == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    assert not provider.instances


@pytest.mark.parametrize("origin,host,scheme", [(None, "testserver", "ws"), ("http://testserver", "testserver", "ws"), ("http://testserver:80", "testserver", "ws"), ("https://testserver", "testserver", "wss"), ("http://localhost:8080", "localhost:8080", "ws")])
@pytest.mark.asyncio
async def test_same_origin_and_no_origin_clients_are_allowed(provider, origin, host, scheme):
    harness = VoiceHarness(origin=origin, host=host, scheme=scheme)
    await harness.run()
    assert harness.messages[0] == {"type": "ready", "sample_rate": 16000}
    assert not harness.errors
    assert_closed(provider)


@pytest.mark.asyncio
async def test_disconnect_cancels_provider_tasks(provider):
    harness = VoiceHarness([PCM_FRAME], disconnect=True)
    await harness.run()
    assert_closed(provider)


@pytest.mark.asyncio
async def test_disconnect_during_setup_cancels_connect(provider):
    provider.connect_blocked = True
    harness = VoiceHarness()
    harness.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await harness.run()
    assert not any(message["type"] == "ready" for message in harness.messages)
    assert_closed(provider)


@pytest.mark.parametrize("task", ["write_error", "read_error"])
@pytest.mark.asyncio
async def test_unhandled_provider_task_failure_has_no_orphans(provider, task):
    setattr(provider, task, RuntimeError("provider-secret"))
    provider.events = [[turn("unrecognized phrase")]]
    harness = VoiceHarness([PCM_FRAME], stop=False)
    await harness.run()
    assert_safe_error(harness, True)
    assert_closed(provider)


@pytest.mark.asyncio
async def test_backpressure_is_bounded_and_retryable(provider, monkeypatch):
    provider.upload_blocked = True
    monkeypatch.setattr(routes, "AUDIO_QUEUE_MAXSIZE", 2)
    monkeypatch.setattr(routes, "AUDIO_BACKPRESSURE_TIMEOUT", 0.03)
    monkeypatch.setattr(assemblyai_client, "PROVIDER_QUEUE_MAXSIZE", 2)
    monkeypatch.setattr(assemblyai_client, "PROVIDER_CLOSE_TIMEOUT", 0.05)
    harness = VoiceHarness([PCM_FRAME] * 20, stop=False)
    await harness.run()
    assert_safe_error(harness, True)
    assert provider.requests[0][1]["max_queue"] == 2
    assert not provider.instances[0].frames
    assert provider.instances[0].send_cancelled
    assert_closed(provider)


@pytest.mark.asyncio
async def test_stop_and_provider_close_are_bounded(provider, monkeypatch):
    provider.close_blocked = True
    monkeypatch.setattr(assemblyai_client, "PROVIDER_CLOSE_TIMEOUT", 0.03)
    harness = VoiceHarness([PCM_FRAME])
    await harness.run()
    assert_safe_error(harness, True)
    assert json.loads(provider.instances[0].sent[-1]) == {"type": "Terminate"}
    assert_closed(provider)


@pytest.mark.asyncio
async def test_setup_timeout_is_bounded(provider, monkeypatch):
    provider.connect_blocked = True
    monkeypatch.setattr(routes, "PROVIDER_CONNECT_TIMEOUT", 0.03)
    harness = VoiceHarness()
    await harness.run()
    assert_safe_error(harness, True)
    assert not any(message["type"] == "ready" for message in harness.messages)
    assert_closed(provider)


@pytest.mark.asyncio
async def test_upload_coroutine_failure_cancels_siblings(provider):
    async def audio_frames():
        yield PCM_FRAME
        raise RuntimeError("provider-secret")

    existing = asyncio.all_tasks()
    on_error = AsyncMock()
    session = assemblyai_client.AssemblyAIStreamingSession("fake-key", AsyncMock(), on_error=on_error)
    await session.connect()
    with pytest.raises(assemblyai_client.AssemblyAIStreamingError, match="unavailable"):
        await session.stream(audio_frames())
    with pytest.raises(assemblyai_client.AssemblyAIStreamingError):
        await session.close()
    on_error.assert_awaited_once()
    assert "provider-secret" not in on_error.call_args.args[0]
    assert_closed(provider)
    assert not [task for task in asyncio.all_tasks() - existing if not task.done()]


@pytest.mark.asyncio
async def test_engine_callback_failure_stops_session_safely(provider, monkeypatch):
    provider.events = [[turn("show top memory")]]
    monkeypatch.setattr(system_engine, "get_top_processes", AsyncMock(side_effect=RuntimeError("provider-secret")))
    harness = VoiceHarness([PCM_FRAME], stop=False)
    await harness.run()
    assert_safe_error(harness, True)
    assert not confirmation_registry._pending
    assert_closed(provider)


@pytest.mark.asyncio
async def test_send_lock_serializes_concurrent_messages():
    sending = False
    sent = []

    async def send_json(data):
        nonlocal sending
        assert not sending
        sending = True
        await asyncio.sleep(0)
        sent.append(data)
        sending = False

    sender = routes._LockedSender(SimpleNamespace(send_json=send_json))
    await asyncio.gather(*(sender.send_json({"index": index}) for index in range(20)))
    assert len(sent) == 20


@pytest.mark.asyncio
async def test_telemetry_rejects_cross_origin_before_broadcaster_access(provider):
    harness = VoiceHarness(origin="https://evil.example")
    harness.scope["path"] = "/api/v1/ws/telemetry"
    harness.scope["raw_path"] = b"/api/v1/ws/telemetry"
    await harness.run()
    assert harness.outgoing == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    assert not provider.instances


@pytest.mark.parametrize("termination_failure", [False, True])
@pytest.mark.asyncio
async def test_native_transport_final_turns_drain_without_orphans(provider, termination_failure):
    provider.termination_failure = termination_failure
    provider.final_events = [turn("unrecognized phrase"), turn("Unrecognized phrase.", formatted=True)]
    harness = VoiceHarness([PCM_FRAME, PCM_FRAME])
    await harness.run()
    assert provider.instances[0].frames == [PCM_FRAME, PCM_FRAME]
    if termination_failure:
        assert_safe_error(harness, True)
    else:
        assert [message["type"] for message in harness.messages] == ["ready", "transcript", "command_result"]
        assert not harness.errors
        assert provider.instances[0].sent[:2] == [PCM_FRAME, PCM_FRAME]
        assert json.loads(provider.instances[0].sent[2]) == {"type": "Terminate"}
    assert_closed(provider)


@pytest.mark.asyncio
async def test_ready_waits_for_confirmed_begin(provider):
    provider.begin_blocked = True
    harness = VoiceHarness([PCM_FRAME])
    task = asyncio.create_task(harness.run())
    await asyncio.wait_for(provider.opened.wait(), 1)
    await asyncio.sleep(0)
    assert not harness.messages
    assert not provider.instances[0].frames
    provider.instances[0].push(begin())
    await task
    assert harness.messages == [{"type": "ready", "sample_rate": 16000}]
    assert_closed(provider)


@pytest.mark.asyncio
async def test_missing_begin_times_out_without_ready(provider, monkeypatch):
    provider.begin_blocked = True
    monkeypatch.setattr(routes, "PROVIDER_CONNECT_TIMEOUT", 0.03)
    harness = VoiceHarness()
    await harness.run()
    assert_safe_error(harness, True)
    assert not any(message["type"] == "ready" for message in harness.messages)
    assert_closed(provider)


@pytest.mark.parametrize("event", [turn("kill pid 4242"), {"type": "Begin"}, "provider-secret", {"type": "Begin", "id": "fake", "expires_at": 2000000000, "configuration": {"model": "different-model"}}])
@pytest.mark.asyncio
async def test_invalid_begin_never_readies_or_executes(provider, event):
    provider.setup_event = event
    harness = VoiceHarness()
    await harness.run()
    assert_safe_error(harness, True)
    assert not any(message["type"] == "ready" for message in harness.messages)
    assert not confirmation_registry._pending
    assert_closed(provider)


@pytest.mark.parametrize("code,retryable", [(401, False), (403, False), (429, True), (503, True)])
@pytest.mark.asyncio
async def test_native_handshake_errors_are_safe(provider, code, retryable):
    provider.connect_error = InvalidStatus(Response(code, "provider-secret", Headers(), b"provider-secret"))
    harness = VoiceHarness()
    await harness.run()
    assert_safe_error(harness, retryable)
    assert not provider.instances


@pytest.mark.parametrize("code,retryable", [(1008, False), (4001, False), (3005, True), (1000, True)])
@pytest.mark.asyncio
async def test_native_close_without_error_event_is_reported(provider, code, retryable):
    provider.events = [[ConnectionClosedError(Close(code, "provider-secret"), None)]]
    harness = VoiceHarness([PCM_FRAME], stop=False)
    await harness.run()
    assert_safe_error(harness, retryable)
    assert_closed(provider)


@pytest.mark.parametrize("event", ["provider-secret", [], {"type": "Turn", "turn_order": 0, "end_of_turn": "true", "transcript": "kill pid 4242"}, {"type": "Turn", "turn_order": "0", "end_of_turn": True, "transcript": "kill pid 4242"}])
@pytest.mark.asyncio
async def test_malformed_provider_messages_stop_without_commands(provider, event):
    provider.events = [[json.dumps(event) if isinstance(event, list) else event]]
    harness = VoiceHarness([PCM_FRAME], stop=False)
    await harness.run()
    assert_safe_error(harness, True)
    assert not confirmation_registry._pending
    assert_closed(provider)


@pytest.mark.asyncio
async def test_provider_transport_logger_does_not_log_payloads_or_keys(provider, caplog):
    harness = VoiceHarness()
    await harness.run()
    transport_logger = provider.requests[0][1]["logger"]
    for level in (10, 20, 30, 40, 50):
        transport_logger.log(level, "provider-secret")
    assert "provider-secret" not in caplog.text
