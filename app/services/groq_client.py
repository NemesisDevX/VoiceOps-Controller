"""Groq-powered agentic SRE post-mortem generator with deterministic fallback."""

from __future__ import annotations

import logging

from groq import AsyncGroq

from app.core.config import get_settings
from app.services.incident_log import IncidentRecord, render_markdown

logger = logging.getLogger(__name__)


def _build_prompt(record: IncidentRecord) -> str:
    """Build a strict SRE prompt from the resolved incident record."""
    mttr = f"{record.mttr_seconds:.3f}s" if record.mttr_seconds is not None else "N/A"
    return (
        "You are a Staff Site Reliability Engineer writing a formal, production-grade "
        "incident post-mortem in Markdown. Using only the data below, produce a detailed "
        "Root Cause Analysis (RCA) with these sections: Executive Summary, Triggering Event, "
        "Impact / Blast Radius, Remediation Action Taken, Root Cause Analysis, Mean Time To "
        f"Resolution ({mttr}), and Lessons Learned & Next Steps.\n\n"
        f"Original voice command: {record.audio_remediation_command}\n"
        f"Detected intent: {record.intent}\n"
        f"Target: {record.target}\n"
        f"Telemetry mode: {record.mode}\n"
        f"Language: {record.language}\n"
        f"Success: {record.success}\n"
        f"Resolution message: {record.resolution_message or 'N/A'}\n"
        f"Confirmation token: {record.token}\n"
        f"MTTR: {mttr}\n\n"
        "Write in a concise, engineering-first tone. Output valid Markdown only; no preamble, "
        "no JSON, and no filler meta-commentary."
    )


async def generate_groq_post_mortem(record: IncidentRecord) -> str | None:
    """Generate an agentic Markdown post-mortem via Groq, or return None to trigger fallback.

    Returns the raw Markdown content on success. Returns `None` when `GROQ_API_KEY` is not
    configured or when the Groq call fails for any reason, allowing the deterministic renderer
    to take over without crashing the request.
    """
    settings = get_settings()
    api_key = settings.GROQ_API_KEY.get_secret_value() if settings.GROQ_API_KEY is not None else None
    if not api_key:
        return None

    try:
        client = AsyncGroq(api_key=api_key)
        completion = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert SRE. Write a formal incident post-mortem and "
                        "root cause analysis in Markdown with no preamble."
                    ),
                },
                {"role": "user", "content": _build_prompt(record)},
            ],
            temperature=0.3,
            max_tokens=2048,
        )
        content = completion.choices[0].message.content
        return content if content else None
    except Exception as exc:
        logger.warning("Groq post-mortem generation failed; falling back to deterministic report: %s", exc)
        return None


async def render_agentic_post_mortem(record: IncidentRecord) -> tuple[str, bool]:
    """Return the best available Markdown report and a flag indicating whether it came from Groq."""
    ai_report = await generate_groq_post_mortem(record)
    if ai_report is not None:
        return ai_report, True
    return render_markdown(record), False
