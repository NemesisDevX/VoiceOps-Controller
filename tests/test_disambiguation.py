"""Multi-turn disambiguation and voice-driven rollback coverage.

Covers: ambiguous-target detection in K8S_CLUSTER mode, the `disambiguation_required`
WebSocket event, the 15-second clarification context on `IntentParser`, follow-up
utterance resolution against pending candidates, and the mitigation-stack rollback
engine (push/pop, pod snapshot restoration, idempotency, post-mortem linkage).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import websockets as routes
from app.core.security import confirmation_registry
from app.schemas.command import ExecutionResult, IntentType, ParsedCommand
from app.schemas.telemetry import ClusterTelemetry, PodInfo, TelemetryMode
from app.services import assemblyai_client, system_engine
from app.services.incident_log import incident_log
from app.services.intent_parser import IntentParser
from app.services.mitigation_stack import MitigationEntry, mitigation_stack

PCM_FRAME = b"\x00\x00" * 1600


def turn(text: str, order: int = 0, final: bool = True, formatted: bool = False) -> dict:
    return {
        "type": "Turn", "turn_order": order, "turn_is_formatted": formatted,
        "end_of_turn": final, "transcript": text, "end_of_turn_confidence": 1.0, "words": [],
    }


def begin() -> dict:
    return {"type": "Begin", "id": "fake-session", "expires_at": 2000000000, "configuration": {"model": "universal-streaming-english"}}


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(instances=[], requests=[], events=[], opened=asyncio.Event())

    class FakeTransport:
        def __init__(self):
            self.frames = []
            self.sent = []
            self.closed = False
            self.begin_received = False
            self.incoming = asyncio.Queue()
            self.sending = False
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
                for event in self.final_events:
                    self.push(event)
                self.push({"type": "Termination", "audio_duration_seconds": 1, "session_duration_seconds": 1})
            self.sent.append(data)
            await asyncio.sleep(0)

        final_events: list = []

        async def close(self):
            self.closed = True

    async def fake_connect(url, **kwargs):
        state.requests.append((url, kwargs))
        transport = FakeTransport()
        state.opened.set()
        return transport

    monkeypatch.setattr(assemblyai_client, "websocket_connect", fake_connect)
    return state


class VoiceHarness:
    """Minimal ASGI harness with an injected K8S_CLUSTER-mode broadcaster."""

    def __init__(self, frames=(), *, mode=TelemetryMode.K8S_CLUSTER, text_messages=()):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.outgoing = []
        self.frames = frames
        self.text_messages = text_messages
        self.mode = mode
        self.sending = False
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
        app.state.telemetry_broadcaster = SimpleNamespace(get_mode=AsyncMock(return_value=self.mode))
        await asyncio.wait_for(app(self.scope, self.incoming.get, self.send), timeout=timeout)


def degraded_cluster(names=("payment-gateway-pod", "auth-service-pod")) -> ClusterTelemetry:
    return ClusterTelemetry(
        rps=1400.0,
        p99_latency_ms=980.0,
        error_rate_5xx=38.5,
        pods=[
            PodInfo(name=name, status="Running", cpu_percent=80.0, memory_percent=90.0, restarts=2, anomaly=True)
            for name in names
        ],
    )


# --- Parser clarification context -------------------------------------------------------------


def test_set_disambiguation_and_pending_property() -> None:
    parser = IntentParser()
    pending = parser.set_disambiguation(IntentType.PROCESS_KILL, ["pod-a", "pod-b"], language="en")

    assert parser.pending_disambiguation is pending
    assert pending.intent is IntentType.PROCESS_KILL
    assert pending.candidates == ["pod-a", "pod-b"]
    assert pending.expires_at > datetime.now(timezone.utc)


def test_pending_disambiguation_expires_after_ttl() -> None:
    parser = IntentParser()
    parser.set_disambiguation(IntentType.PROCESS_KILL, ["pod-a", "pod-b"], ttl_seconds=-1)

    assert parser.pending_disambiguation is None
    assert parser.try_resolve_disambiguation("pod-a") is None


def test_try_resolve_disambiguation_matches_name_token() -> None:
    parser = IntentParser()
    parser.set_disambiguation(
        IntentType.PROCESS_KILL, ["payment-gateway-pod", "auth-service-pod"], language="en"
    )

    resolved = parser.try_resolve_disambiguation("isolate payment")

    assert resolved is not None
    assert resolved.intent is IntentType.PROCESS_KILL  # stored intent wins, not the uttered verb
    assert resolved.target_process_name == "payment-gateway-pod"
    assert resolved.resolved_from_context is True
    assert resolved.requires_confirmation is True
    assert parser.pending_disambiguation is None


def test_try_resolve_disambiguation_unmatched_keeps_context() -> None:
    parser = IntentParser()
    parser.set_disambiguation(IntentType.PROCESS_KILL, ["payment-gateway-pod", "auth-service-pod"])

    assert parser.try_resolve_disambiguation("hmm ok") is None
    assert parser.pending_disambiguation is not None


def test_try_resolve_disambiguation_tie_keeps_context() -> None:
    parser = IntentParser()
    parser.set_disambiguation(IntentType.PROCESS_KILL, ["cache-alpha-pod", "cache-beta-pod"])

    # "cache" scores identically for both candidates -> still ambiguous.
    assert parser.try_resolve_disambiguation("kill cache") is None
    assert parser.pending_disambiguation is not None


def test_narrow_candidates_prefers_partial_matches() -> None:
    parser = IntentParser()
    parser.set_disambiguation(IntentType.PROCESS_KILL, ["payment-gateway-pod", "auth-service-pod"])

    assert parser.narrow_candidates("payment") == ["payment-gateway-pod"]
    assert parser.narrow_candidates("zzzz") == ["payment-gateway-pod", "auth-service-pod"]


# --- WebSocket gate: ambiguous cluster targets --------------------------------------------------


@pytest.mark.asyncio
async def test_gate_mutation_emits_disambiguation_for_multiple_degraded_pods(monkeypatch):
    monkeypatch.setattr(system_engine, "get_cluster_telemetry", AsyncMock(return_value=degraded_cluster()))
    socket = SimpleNamespace(send_json=AsyncMock())
    parser = IntentParser()
    command = ParsedCommand(raw_text="kill the failing pod", intent=IntentType.PROCESS_KILL, requires_confirmation=True)

    await routes._gate_mutation(socket, command, TelemetryMode.K8S_CLUSTER, parser=parser)

    payload = socket.send_json.await_args.args[0]
    assert payload["type"] == "disambiguation_required"
    assert payload["intent"] == "PROCESS_KILL"
    names = [candidate["name"] for candidate in payload["candidates"]]
    assert names == ["payment-gateway-pod", "auth-service-pod"]
    assert "Ambiguity detected" in payload["message"]
    assert datetime.fromisoformat(payload["expires_at"]) > datetime.now(timezone.utc)
    assert parser.pending_disambiguation is not None
    assert parser.pending_disambiguation.candidates == names
    assert not confirmation_registry._pending


@pytest.mark.asyncio
async def test_gate_mutation_auto_resolves_single_degraded_pod(monkeypatch):
    monkeypatch.setattr(
        system_engine, "get_cluster_telemetry", AsyncMock(return_value=degraded_cluster(("payment-gateway-pod",)))
    )
    preview = AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Safe preview", dry_run=True,
        affected_process_name="payment-gateway-pod",
    ))
    monkeypatch.setattr(system_engine, "cluster_execute", preview)
    socket = SimpleNamespace(send_json=AsyncMock())
    command = ParsedCommand(raw_text="kill the failing pod", intent=IntentType.PROCESS_KILL, requires_confirmation=True)

    await routes._gate_mutation(socket, command, TelemetryMode.K8S_CLUSTER, parser=IntentParser())

    payload = socket.send_json.await_args.args[0]
    assert payload["type"] == "confirmation_required"
    preview.assert_awaited_once_with(IntentType.PROCESS_KILL, "payment-gateway-pod", dry_run=True)


@pytest.mark.asyncio
async def test_gate_mutation_no_degraded_pods_reports_no_target(monkeypatch):
    healthy = degraded_cluster()
    healthy.pods = [pod.model_copy(update={"anomaly": False, "memory_percent": 10.0}) for pod in healthy.pods]
    monkeypatch.setattr(system_engine, "get_cluster_telemetry", AsyncMock(return_value=healthy))
    socket = SimpleNamespace(send_json=AsyncMock())
    command = ParsedCommand(raw_text="kill the failing pod", intent=IntentType.PROCESS_KILL, requires_confirmation=True)

    await routes._gate_mutation(socket, command, TelemetryMode.K8S_CLUSTER, parser=IntentParser())

    payload = socket.send_json.await_args.args[0]
    assert payload["type"] == "command_result"
    assert payload["success"] is False


@pytest.mark.asyncio
async def test_handle_command_resolves_followup_against_pending_candidates(monkeypatch):
    monkeypatch.setattr(system_engine, "get_cluster_telemetry", AsyncMock(return_value=degraded_cluster()))
    preview = AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Safe preview", dry_run=True,
        affected_process_name="payment-gateway-pod",
    ))
    monkeypatch.setattr(system_engine, "cluster_execute", preview)
    socket = SimpleNamespace(send_json=AsyncMock())
    parser = IntentParser()
    timing = {"t0": None, "t1": 1_700_000_000.0}

    await routes._handle_command(socket, parser, "kill the failing pod", TelemetryMode.K8S_CLUSTER, timing=timing)
    assert socket.send_json.await_args.args[0]["type"] == "disambiguation_required"

    socket.send_json.reset_mock()
    await routes._handle_command(socket, parser, "isolate payment", TelemetryMode.K8S_CLUSTER, timing=timing)

    payload = socket.send_json.await_args.args[0]
    assert payload["type"] == "confirmation_required"
    preview.assert_awaited_once_with(IntentType.PROCESS_KILL, "payment-gateway-pod", dry_run=True)
    pending = confirmation_registry.peek(payload["token"])
    assert pending.command.target_process_name == "payment-gateway-pod"
    assert parser.pending_disambiguation is None


@pytest.mark.asyncio
async def test_handle_command_reemits_disambiguation_on_unmatched_followup():
    socket = SimpleNamespace(send_json=AsyncMock())
    parser = IntentParser()
    parser.set_disambiguation(IntentType.PROCESS_KILL, ["payment-gateway-pod", "auth-service-pod"])

    await routes._handle_command(socket, parser, "hmm ok", TelemetryMode.K8S_CLUSTER)

    payload = socket.send_json.await_args.args[0]
    assert payload["type"] == "disambiguation_required"
    assert parser.pending_disambiguation is not None


@pytest.mark.asyncio
async def test_handle_command_new_inspect_clears_pending_disambiguation(monkeypatch):
    monkeypatch.setattr(system_engine, "get_top_processes", AsyncMock(return_value=[]))
    socket = SimpleNamespace(send_json=AsyncMock())
    parser = IntentParser()
    parser.set_disambiguation(IntentType.PROCESS_KILL, ["payment-gateway-pod", "auth-service-pod"])

    await routes._handle_command(socket, parser, "show top memory", TelemetryMode.K8S_CLUSTER)

    assert socket.send_json.await_args.args[0]["type"] == "command_result"
    assert parser.pending_disambiguation is None


@pytest.mark.asyncio
async def test_full_socket_flow_disambiguates_then_confirms(provider, monkeypatch):
    monkeypatch.setattr(system_engine, "get_cluster_telemetry", AsyncMock(return_value=degraded_cluster()))
    monkeypatch.setattr(system_engine, "cluster_execute", AsyncMock(return_value=ExecutionResult(
        success=True, intent=IntentType.PROCESS_KILL, message="Safe preview", dry_run=True,
        affected_process_name="payment-gateway-pod",
    )))
    provider.events = [[turn("kill the failing pod")], [turn("isolate payment", order=1)]]
    harness = VoiceHarness([PCM_FRAME, PCM_FRAME])

    await harness.run()

    assert [message["type"] for message in harness.messages] == [
        "ready", "transcript", "disambiguation_required", "transcript", "confirmation_required",
    ]
    disamb = harness.messages[2]
    assert {candidate["name"] for candidate in disamb["candidates"]} == {"payment-gateway-pod", "auth-service-pod"}
    pending = confirmation_registry.peek(harness.messages[-1]["token"])
    assert pending.command.target_process_name == "payment-gateway-pod"


# --- Multilingual rollback intents ---------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,language",
    [
        ("rollback", "en"),
        ("undo last action", "en"),
        ("revert the change", "en"),
        ("تراجع عن التعديل", "ar"),
        ("revertir el despliegue", "es"),
        ("deshacer el cambio", "es"),
        ("annuler le déploiement", "fr"),
        ("回滚上次更改", "zh"),
    ],
)
def test_multilingual_rollback_intents(utterance: str, language: str) -> None:
    command = IntentParser().parse(utterance)

    assert command.intent is IntentType.ROLLBACK
    assert command.requires_confirmation is True
    assert command.language == language


# --- Mitigation stack / rollback engine ------------------------------------------------------------


def _cluster_entry(name="payment-gateway-pod") -> MitigationEntry:
    return MitigationEntry(
        token="tok-1",
        intent=IntentType.PROCESS_KILL,
        mode="K8S_CLUSTER",
        target=name,
        kind="cluster_pod",
        data={"name": name, "snapshot": system_engine._cluster_simulator.snapshot_pod(name)},
    )


def test_mitigation_stack_push_peek_pop() -> None:
    entry = _cluster_entry()
    mitigation_stack.push(entry)

    assert len(mitigation_stack) == 1
    assert mitigation_stack.peek() is entry
    assert mitigation_stack.pop() is entry
    assert mitigation_stack.pop() is None


@pytest.mark.asyncio
async def test_rollback_last_empty_stack_fails() -> None:
    result, entry = await mitigation_stack.rollback_last()

    assert result.success is False
    assert entry is None
    assert "No prior mitigation" in result.message


@pytest.mark.asyncio
async def test_rollback_last_dry_run_does_not_pop() -> None:
    mitigation_stack.push(_cluster_entry())

    result, entry = await mitigation_stack.rollback_last(dry_run=True)

    assert result.success is True and result.dry_run is True
    assert entry is None
    assert len(mitigation_stack) == 1


@pytest.mark.asyncio
async def test_rollback_last_restores_cluster_pod_snapshot() -> None:
    pod = system_engine._cluster_simulator.pods["payment-gateway-pod"]
    snapshot = system_engine._cluster_simulator.snapshot_pod("payment-gateway-pod")
    # Simulate the mutation: restarted and recovered.
    pod.restarts += 1
    pod.anomaly = False
    pod.memory_percent = 20.0
    mitigation_stack.push(
        MitigationEntry(
            token="tok-1",
            intent=IntentType.PROCESS_KILL,
            mode="K8S_CLUSTER",
            target="payment-gateway-pod",
            kind="cluster_pod",
            data={"name": "payment-gateway-pod", "snapshot": snapshot},
        )
    )

    result, entry = await mitigation_stack.rollback_last()

    assert result.success is True
    assert result.intent is IntentType.ROLLBACK
    assert entry is not None
    assert pod.anomaly is True
    assert pod.restarts == snapshot["restarts"]
    assert pod.memory_percent == snapshot["memory_percent"]
    assert len(mitigation_stack) == 0


@pytest.mark.asyncio
async def test_rollback_last_network_isolate_removes_rules(monkeypatch) -> None:
    unblock = AsyncMock(return_value=["10.1.2.3", "10.4.5.6"])
    monkeypatch.setattr(system_engine, "unblock_ips", unblock)
    mitigation_stack.push(
        MitigationEntry(
            token="tok-iso",
            intent=IntentType.NETWORK_ISOLATE,
            mode="HOST_LOCAL",
            target="runaway-worker.exe",
            kind="network_isolate",
            data={"pid": 4242, "ips": ["10.1.2.3", "10.4.5.6"]},
        )
    )

    result, entry = await mitigation_stack.rollback_last()

    assert result.success is True
    assert result.affected_ips == ["10.1.2.3", "10.4.5.6"]
    assert entry is not None and entry.token == "tok-iso"
    unblock.assert_awaited_once_with(["10.1.2.3", "10.4.5.6"])


@pytest.mark.asyncio
async def test_rollback_last_host_process_is_irreversible() -> None:
    mitigation_stack.push(
        MitigationEntry(
            token="tok-kill",
            intent=IntentType.PROCESS_KILL,
            mode="HOST_LOCAL",
            target="runaway-worker.exe",
            kind="host_process",
            data={"pid": 4242},
        )
    )

    result, entry = await mitigation_stack.rollback_last()

    assert result.success is False
    assert "cannot be restored" in result.message
    assert entry is not None
    assert len(mitigation_stack) == 0


@pytest.mark.asyncio
async def test_gate_mutation_bare_rollback_uses_stack_preview(monkeypatch):
    mitigation_stack.push(_cluster_entry())
    socket = SimpleNamespace(send_json=AsyncMock())
    command = ParsedCommand(raw_text="rollback", intent=IntentType.ROLLBACK, requires_confirmation=True)

    await routes._gate_mutation(socket, command, TelemetryMode.K8S_CLUSTER, parser=IntentParser())

    payload = socket.send_json.await_args.args[0]
    assert payload["type"] == "confirmation_required"
    assert "roll back" in payload["preview"]["message"].lower()
    assert len(mitigation_stack) == 1  # preview must not consume the entry


@pytest.mark.asyncio
async def test_gate_mutation_bare_rollback_empty_stack_fails() -> None:
    socket = SimpleNamespace(send_json=AsyncMock())
    command = ParsedCommand(raw_text="rollback", intent=IntentType.ROLLBACK, requires_confirmation=True)

    await routes._gate_mutation(socket, command, TelemetryMode.K8S_CLUSTER, parser=IntentParser())

    payload = socket.send_json.await_args.args[0]
    assert payload["type"] == "command_result"
    assert payload["success"] is False
    assert "No prior mitigation" in payload["message"]


@pytest.mark.asyncio
async def test_confirm_endpoint_executes_stack_rollback_and_links_incident(client: TestClient) -> None:
    entry = _cluster_entry()
    mitigation_stack.push(entry)
    command = ParsedCommand(raw_text="undo last action", intent=IntentType.ROLLBACK, requires_confirmation=True)
    pending = confirmation_registry.register(command, mode="K8S_CLUSTER")
    incident_log.open_incident(pending.token, command, "K8S_CLUSTER")

    response = client.post("/api/v1/commands/confirm", json={"token": pending.token})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "Rolled back" in body["message"]
    record = incident_log.get(pending.token)
    assert record is not None
    assert record.linked_token == "tok-1"
    assert any("Rollback of prior incident" in event.message for event in record.timeline)
    assert len(mitigation_stack) == 0
