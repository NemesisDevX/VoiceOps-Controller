"""Sub-second latency waterfall coverage (t0 audio dispatch -> t3 remediation).

Covers: marker decoding, checkpoint assembly, waterfall propagation through the
`confirmation_required` WebSocket event, `ExecutionResult.latency_waterfall`, the
incident record / post-mortem, and the live telemetry broadcast payload.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import websockets as routes
from app.core.security import confirmation_registry
from app.schemas.command import ExecutionResult, IntentType, ParsedCommand
from app.services import assemblyai_client, system_engine
from app.services.incident_log import incident_log
from app.services.latency import waterfall_tracker
from app.services.telemetry_stream import TelemetryBroadcaster

PCM_FRAME = b"\x00\x00" * 1600


def turn(text: str, order: int = 0, final: bool = True) -> dict:
    return {
        "type": "Turn", "turn_order": order, "turn_is_formatted": False,
        "end_of_turn": final, "transcript": text, "end_of_turn_confidence": 1.0, "words": [],
    }


def begin() -> dict:
    return {"type": "Begin", "id": "fake-session", "expires_at": 2000000000, "configuration": {"model": "universal-streaming-english"}}


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(instances=[], events=[], opened=asyncio.Event())

    class FakeTransport:
        def __init__(self):
            self.frames = []
            self.sent = []
            self.closed = False
            self.begin_received = False
            self.incoming = asyncio.Queue()
            self.push(begin())
            state.instances.append(self)

        def push(self, event):
            self.incoming.put_nowait(json.dumps(event) if isinstance(event, dict) else event)

        async def recv(self):
            raw = await self.incoming.get()
            if isinstance(raw, str) and '"type": "Begin"' in raw:
                self.begin_received = True
            return raw

        async def send(self, data):
            if isinstance(data, bytes):
                self.frames.append(data)
                if state.events:
                    for event in state.events.pop(0):
                        self.push(event)
            else:
                self.push({"type": "Termination", "audio_duration_seconds": 1, "session_duration_seconds": 1})
            self.sent.append(data)
            await asyncio.sleep(0)

        async def close(self):
            self.closed = True

    async def fake_connect(url, **kwargs):
        transport = FakeTransport()
        state.opened.set()
        return transport

    monkeypatch.setattr(assemblyai_client, "websocket_connect", fake_connect)
    return state


class VoiceHarness:
    def __init__(self, frames=(), text_messages=()):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.outgoing = []
        self.frames = frames
        self.text_messages = text_messages
        self.scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "scheme": "ws", "path": "/api/v1/ws/voice-stream", "raw_path": b"/api/v1/ws/voice-stream",
            "query_string": b"", "root_path": "", "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80), "subprotocols": [],
        }

    @property
    def messages(self):
        return [json.loads(item["text"]) for item in self.outgoing if item["type"] == "websocket.send"]

    async def send(self, message):
        self.outgoing.append(message)
        await asyncio.sleep(0)
        if message["type"] == "websocket.send" and json.loads(message["text"])["type"] == "ready":
            for text in self.text_messages:
                self.incoming.put_nowait({"type": "websocket.receive", "text": text})
            for frame in self.frames:
                self.incoming.put_nowait({"type": "websocket.receive", "bytes": frame})
            self.incoming.put_nowait({"type": "websocket.receive", "text": "stop"})

    async def run(self, timeout=2):
        app = FastAPI()
        app.include_router(routes.router, prefix="/api/v1")
        await asyncio.wait_for(app(self.scope, self.incoming.get, self.send), timeout=timeout)


# --- Marker decoding and checkpoint assembly ------------------------------------------------------


@pytest.mark.parametrize("payload", ["stop", "{}", "not json", '["x"]', '{"type":"nope","ts":1}'])
def test_decode_audio_marker_rejects_non_markers(payload: str) -> None:
    assert routes._decode_audio_marker(payload) is None


@pytest.mark.parametrize("ts", [0, -5, "abc", True])
def test_decode_audio_marker_rejects_invalid_ts(ts) -> None:
    assert routes._decode_audio_marker(json.dumps({"type": "audio_marker", "ts": ts})) is None


def test_decode_audio_marker_accepts_ms_epoch() -> None:
    assert routes._decode_audio_marker(json.dumps({"type": "audio_marker", "ts": 1_700_000_000_500})) == pytest.approx(
        1_700_000_000.5
    )


def test_build_waterfall_without_t0_omits_stt() -> None:
    waterfall = routes._build_waterfall({"t0": None, "t1": 100.050}, 100.080)

    assert waterfall == {"t1_ms": 100050.0, "t2_ms": 100080.0, "gate_ms": 30.0}


def test_build_waterfall_orders_segments() -> None:
    waterfall = routes._build_waterfall({"t0": 100.0, "t1": 100.012}, 100.020)

    assert waterfall["t0_ms"] < waterfall["t1_ms"] < waterfall["t2_ms"]
    assert waterfall["stt_ms"] == pytest.approx(12.0, abs=0.01)
    assert waterfall["gate_ms"] == pytest.approx(8.0, abs=0.01)


def test_build_waterfall_none_without_timing() -> None:
    assert routes._build_waterfall(None, 1.0) is None


# --- Waterfall on the voice socket -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmation_required_carries_waterfall(provider, monkeypatch):
    preview = AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Safe preview", dry_run=True, affected_pid=4242,
    ))
    monkeypatch.setattr(system_engine, "terminate_process", preview)
    marker_ts = (time.time() - 0.05) * 1000.0
    provider.events = [[turn("kill pid 4242")]]
    harness = VoiceHarness(
        [PCM_FRAME],
        text_messages=[json.dumps({"type": "audio_marker", "ts": int(marker_ts)})],
    )

    await harness.run()

    confirmation = harness.messages[-1]
    assert confirmation["type"] == "confirmation_required"
    waterfall = confirmation["waterfall"]
    assert waterfall["t0_ms"] == pytest.approx(marker_ts, abs=2)
    assert waterfall["t0_ms"] <= waterfall["t1_ms"] <= waterfall["t2_ms"]
    assert waterfall["stt_ms"] >= 0
    assert waterfall["gate_ms"] >= 0
    assert "exec_ms" not in waterfall


@pytest.mark.asyncio
async def test_confirmation_required_waterfall_without_marker(provider, monkeypatch):
    preview = AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Safe preview", dry_run=True, affected_pid=4242,
    ))
    monkeypatch.setattr(system_engine, "terminate_process", preview)
    provider.events = [[turn("kill pid 4242")]]
    harness = VoiceHarness([PCM_FRAME])

    await harness.run()

    waterfall = harness.messages[-1]["waterfall"]
    # t0 falls back to the last consumed PCM frame timestamp.
    assert waterfall["t0_ms"] <= waterfall["t1_ms"] <= waterfall["t2_ms"]
    assert waterfall["stt_ms"] >= 0


# --- Waterfall on confirm + incident + telemetry -----------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_result_and_incident_carry_full_waterfall(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(system_engine, "cluster_execute", AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Restarted pod 'payment-gateway-pod'.",
        dry_run=False, affected_process_name="payment-gateway-pod",
    )))
    now = time.time()
    waterfall = {
        "t0_ms": round((now - 0.010) * 1000.0, 3),
        "t1_ms": round((now - 0.006) * 1000.0, 3),
        "t2_ms": round(now * 1000.0, 3),
        "stt_ms": 4.0,
        "gate_ms": 6.0,
    }
    command = ParsedCommand(
        raw_text="kill the failing pod", intent=IntentType.PROCESS_KILL,
        target_process_name="payment-gateway-pod", requires_confirmation=True,
    )
    pending = confirmation_registry.register(command, mode="K8S_CLUSTER")
    incident_log.open_incident(pending.token, command, "K8S_CLUSTER", waterfall=waterfall)

    response = client.post("/api/v1/commands/confirm", json={"token": pending.token})

    assert response.status_code == 200
    result_wf = response.json()["latency_waterfall"]
    assert result_wf["t0_ms"] == waterfall["t0_ms"]
    assert result_wf["exec_ms"] >= 0
    assert result_wf["total_ms"] == pytest.approx(result_wf["stt_ms"] + result_wf["gate_ms"] + result_wf["exec_ms"], abs=0.01)
    assert result_wf["t3_ms"] > result_wf["t2_ms"]

    record = incident_log.get(pending.token)
    assert record.latency_waterfall["total_ms"] == result_wf["total_ms"]
    assert waterfall_tracker.latest()["total_ms"] == result_wf["total_ms"]


@pytest.mark.asyncio
async def test_dry_run_confirm_leaves_waterfall_pending(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(system_engine, "cluster_execute", AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="[DRY RUN] preview.", dry_run=True,
        affected_process_name="payment-gateway-pod",
    )))
    command = ParsedCommand(
        raw_text="kill the failing pod", intent=IntentType.PROCESS_KILL,
        target_process_name="payment-gateway-pod", requires_confirmation=True,
    )
    pending = confirmation_registry.register(command, mode="K8S_CLUSTER")
    incident_log.open_incident(pending.token, command, "K8S_CLUSTER", waterfall={"t2_ms": 1.0, "gate_ms": 1.0})

    response = client.post("/api/v1/commands/confirm", json={"token": pending.token, "dry_run": True})

    assert response.status_code == 200
    # Dry runs never resolve the incident, so no exec/total segment exists yet.
    assert response.json()["latency_waterfall"] is None
    assert confirmation_registry.peek(pending.token) is not None


@pytest.mark.asyncio
async def test_telemetry_payload_includes_latest_waterfall(monkeypatch) -> None:
    monkeypatch.setattr(system_engine, "get_cluster_telemetry", AsyncMock(return_value=SimpleNamespace(
        model_dump=lambda mode=None: {"rps": 1.0, "pods": []},
    )))
    broadcaster = TelemetryBroadcaster()
    await broadcaster.set_mode(routes.TelemetryMode.K8S_CLUSTER)
    waterfall_tracker.record({"stt_ms": 1.0, "gate_ms": 2.0, "exec_ms": 3.0, "total_ms": 6.0})

    payload = await broadcaster._build_payload()

    assert payload["waterfall"]["total_ms"] == 6.0


@pytest.mark.asyncio
async def test_telemetry_payload_omits_waterfall_before_first_incident() -> None:
    waterfall_tracker._latest = None
    broadcaster = TelemetryBroadcaster()

    payload = await broadcaster._build_payload()

    assert "waterfall" not in payload


@pytest.mark.asyncio
async def test_post_mortem_markdown_contains_latency_waterfall() -> None:
    command = ParsedCommand(raw_text="kill the failing pod", intent=IntentType.PROCESS_KILL, target_process_name="pod-x")
    incident_log.open_incident(
        "wf-token", command, "K8S_CLUSTER",
        waterfall={"t0_ms": 1.0, "t1_ms": 2.0, "t2_ms": 3.0, "stt_ms": 1.0, "gate_ms": 1.0},
    )
    incident_log.resolve_incident("wf-token", ExecutionResult(success=True, intent=IntentType.PROCESS_KILL, message="done"))

    from app.services.incident_log import render_markdown

    markdown = render_markdown(incident_log.get("wf-token"))
    assert "Latency Waterfall" in markdown
    assert "Total MTTR" in markdown
