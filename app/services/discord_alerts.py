"""Real-time Discord webhook alerts for resolved VoiceOps incidents."""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings
from app.services.incident_log import IncidentRecord

logger = logging.getLogger(__name__)


def _build_payload(record: IncidentRecord) -> dict:
    """Build a rich Discord embed for a resolved SRE incident."""
    mttr = f"{record.mttr_seconds:.3f}s" if record.mttr_seconds is not None else "N/A"
    sub_second = record.mttr_seconds is not None and record.mttr_seconds < 1.0
    mttr_value = f"{mttr} (sub-second)" if sub_second else mttr
    return {
        "username": "VoiceOps Controller",
        "embeds": [
            {
                "title": "🚨 CRITICAL INCIDENT RESOLVED",
                "description": (
                    "A destructive SRE command was executed through the VoiceOps HUD and "
                    "the incident has been mitigated."
                ),
                "color": 0x2ECC71,
                "fields": [
                    {"name": "Voice Command", "value": record.audio_remediation_command or "N/A", "inline": False},
                    {"name": "Executed Command", "value": record.intent or "N/A", "inline": True},
                    {"name": "Mitigated Target", "value": record.target or "N/A", "inline": True},
                    {"name": "MTTR", "value": mttr_value, "inline": True},
                    {"name": "Mode", "value": record.mode or "N/A", "inline": True},
                    {"name": "Confirmation Token", "value": f"`{record.token}`", "inline": True},
                    {"name": "Resolution", "value": record.resolution_message or "N/A", "inline": False},
                ],
                "footer": {"text": "VoiceOps Controller • Autonomous Voice-Driven SRE"},
            }
        ],
    }


async def send_resolution_alert(record: IncidentRecord) -> None:
    """Dispatch a Discord webhook alert for a successful, resolved incident.

    No-ops silently when `DISCORD_ALERTS_ENABLED` is false, `DISCORD_WEBHOOK_URL` is
    unset, the record is unresolved, or the resolution was not successful. Network or
    HTTP failures are logged and swallowed so alerting can never break the API response.
    """
    settings = get_settings()
    if not settings.DISCORD_ALERTS_ENABLED or not settings.DISCORD_WEBHOOK_URL:
        return
    if record.resolved_at is None or not record.success:
        return

    payload = _build_payload(record)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(settings.DISCORD_WEBHOOK_URL, json=payload)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("Discord webhook alert failed: %s", exc)
