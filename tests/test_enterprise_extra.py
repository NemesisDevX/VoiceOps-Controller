"""Additional edge-case and settings tests for the enterprise Layer-3 integrations."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from types import SimpleNamespace

from app.schemas.command import ExecutionResult, IntentType, ParsedCommand
from app.services import discord_alerts, groq_client
from app.services.incident_log import incident_log


def _record_for(intent: IntentType, success: bool = True, mttr: float | None = 0.15):
    command = ParsedCommand(
        raw_text=f"{intent.value.lower()} payment-gateway-pod",
        intent=intent,
        target_process_name="payment-gateway-pod",
        language="en",
    )
    incident_log.open_incident("edge-token", command, "K8S_CLUSTER")
    result = ExecutionResult(
        success=success,
        intent=intent,
        message=f"Executed {intent.value}.",
        dry_run=False,
        affected_process_name="payment-gateway-pod",
    )
    record = incident_log.resolve_incident("edge-token", result)
    if record is not None and mttr is not None:
        record.mttr_seconds = mttr
    return record


def _settings(groq_key: str | None = None, discord_url: str | None = None, alerts_enabled: bool = True):
    return SimpleNamespace(
        GROQ_API_KEY=SecretStr(groq_key) if groq_key is not None else None,
        GROQ_MODEL="llama3-8b-8192",
        DISCORD_WEBHOOK_URL=discord_url,
        DISCORD_ALERTS_ENABLED=alerts_enabled,
    )


def test_groq_build_prompt_contains_original_command_and_target() -> None:
    """The prompt must contain the raw voice command and the resolved target."""
    record = _record_for(IntentType.PROCESS_KILL, mttr=0.5)
    prompt = groq_client._build_prompt(record)

    assert record.audio_remediation_command in prompt
    assert record.target in prompt
    assert "0.500s" in prompt
    assert "Root Cause Analysis" in prompt


def test_groq_build_prompt_handles_missing_mttr() -> None:
    """The prompt should not crash when MTTR has not been computed."""
    record = _record_for(IntentType.NETWORK_ISOLATE, mttr=None)
    record.mttr_seconds = None
    prompt = groq_client._build_prompt(record)

    assert "MTTR" in prompt
    assert "N/A" in prompt


@pytest.mark.asyncio
async def test_groq_client_returns_none_on_empty_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty string completion must be treated as a failed generation (fallback)."""
    record = _record_for(IntentType.PROCESS_KILL)
    monkeypatch.setattr(groq_client, "get_settings", lambda: _settings(groq_key="test-key"))

    def _fake(api_key: str):
        async def _create(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    monkeypatch.setattr(groq_client, "AsyncGroq", _fake)

    report = await groq_client.generate_groq_post_mortem(record)

    assert report is None


@pytest.mark.parametrize(
    "mttr, expected_substring",
    [
        (0.09, "0.090s (sub-second)"),
        (0.99, "0.990s (sub-second)"),
        (1.5, "1.500s"),
    ],
)
def test_discord_payload_mttr_formatting(mttr: float, expected_substring: str) -> None:
    """The MTTR field must flag sub-second values and plain format others."""
    record = _record_for(IntentType.ROLLBACK, mttr=mttr)
    payload = discord_alerts._build_payload(record)

    assert any(expected_substring in field["value"] for field in payload["embeds"][0]["fields"])


@pytest.mark.parametrize(
    "intent",
    [IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE, IntentType.ROLLBACK],
)
def test_discord_payload_title_for_each_mutating_intent(intent: IntentType) -> None:
    """Every mutating intent that can resolve an incident must produce a Discord embed."""
    record = _record_for(intent)
    payload = discord_alerts._build_payload(record)

    assert payload["embeds"][0]["title"] == "🚨 CRITICAL INCIDENT RESOLVED"
    assert payload["username"] == "VoiceOps Controller"


@pytest.mark.asyncio
async def test_discord_alert_respects_disabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DISCORD_ALERTS_ENABLED is false no HTTP call should be attempted."""
    record = _record_for(IntentType.PROCESS_KILL)
    monkeypatch.setattr(discord_alerts, "get_settings", lambda: _settings(discord_url="https://discord.example/webhook", alerts_enabled=False))

    calls = []
    monkeypatch.setattr(discord_alerts, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs: None))

    await discord_alerts.send_resolution_alert(record)

    assert calls == []


@pytest.mark.asyncio
async def test_discord_alert_noops_for_unresolved_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record that is still pending should not trigger a Discord alert."""
    command = ParsedCommand(
        raw_text="kill payment-gateway-pod",
        intent=IntentType.PROCESS_KILL,
        target_process_name="payment-gateway-pod",
        language="en",
    )
    record = incident_log.open_incident("unresolved-token", command, "K8S_CLUSTER")
    monkeypatch.setattr(discord_alerts, "get_settings", lambda: _settings(discord_url="https://discord.example/webhook"))

    calls = []

    class _NoopClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(discord_alerts, "httpx", SimpleNamespace(AsyncClient=_NoopClient))

    await discord_alerts.send_resolution_alert(record)

    assert calls == []


@pytest.mark.asyncio
async def test_discord_alert_noops_for_failed_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed execution must not be announced as a resolved incident."""
    record = _record_for(IntentType.PROCESS_KILL, success=False)
    monkeypatch.setattr(discord_alerts, "get_settings", lambda: _settings(discord_url="https://discord.example/webhook"))

    calls = []

    class _NoopClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(discord_alerts, "httpx", SimpleNamespace(AsyncClient=_NoopClient))

    await discord_alerts.send_resolution_alert(record)

    assert calls == []
