"""In-memory incident lifecycle tracking for automated SRE post-mortem generation.

Every mutating voice command that reaches the confirmation gate is recorded as an
`IncidentRecord`. When the operator redeems the confirmation token for a real (non-dry-run)
execution, the record is marked resolved and its Mean Time To Resolution (MTTR) is computed.
This is a Phase 1-style, single-process, in-memory store; a distributed deployment would
back this with a shared, durable incident store.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.schemas.command import ExecutionResult, ParsedCommand

MAX_INCIDENTS = 200


class IncidentEvent(BaseModel):
    """A single timestamped entry in an incident's timeline."""

    timestamp: datetime = Field(..., description="UTC timestamp the event occurred.")
    message: str = Field(..., description="Human-readable description of the event.")


class IncidentRecord(BaseModel):
    """The full lifecycle of one voice-triggered mutating command, for post-mortem export."""

    token: str = Field(..., description="The single-use confirmation token that identifies this incident.")
    actor: str = Field(default="local-operator", description="Who issued the voice command.")
    mode: str = Field(..., description="Telemetry mode (HOST_LOCAL or K8S_CLUSTER) active when the incident began.")
    audio_remediation_command: str = Field(..., description="The raw transcript that triggered this incident.")
    triggering_alert: str = Field(..., description="Summary of the detected intent and target that opened the incident.")
    intent: str = Field(..., description="The mutating intent (PROCESS_KILL, NETWORK_ISOLATE, ROLLBACK).")
    target: str = Field(..., description="The target process/pod name and/or PID/IP identified for remediation.")
    language: str = Field(default="en", description="ISO 639-1 language code the voice command was matched in.")
    timeline: list[IncidentEvent] = Field(default_factory=list, description="Chronological incident events.")
    opened_at: datetime = Field(..., description="UTC timestamp the confirmation was first requested.")
    resolved_at: datetime | None = Field(default=None, description="UTC timestamp the remediation executed, if resolved.")
    success: bool | None = Field(default=None, description="Whether the remediation succeeded, once resolved.")
    resolution_message: str | None = Field(default=None, description="The final execution result message, once resolved.")
    mttr_seconds: float | None = Field(default=None, description="Mean Time To Resolution in seconds, once resolved.")


def _describe_target(command: ParsedCommand) -> str:
    parts = []
    if command.target_process_name:
        parts.append(command.target_process_name)
    if command.target_pid is not None:
        parts.append(f"pid {command.target_pid}")
    if command.target_ip:
        parts.append(command.target_ip)
    return " / ".join(parts) if parts else "unresolved target"


class IncidentLog:
    """Thread-safe, bounded, in-memory ledger of incident lifecycles for post-mortem export."""

    def __init__(self, max_incidents: int = MAX_INCIDENTS) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, IncidentRecord] = {}
        self._order: deque[str] = deque(maxlen=max_incidents)

    def open_incident(self, token: str, command: ParsedCommand, mode: str) -> IncidentRecord:
        """Record the opening of a new incident when a mutating command is gated for confirmation."""
        now = datetime.now(timezone.utc)
        target = _describe_target(command)
        record = IncidentRecord(
            token=token,
            mode=mode,
            audio_remediation_command=command.raw_text,
            triggering_alert=f"Voice command classified as {command.intent.value} targeting {target}.",
            intent=command.intent.value,
            target=target,
            language=command.language,
            timeline=[IncidentEvent(timestamp=now, message=f"Confirmation requested for '{command.raw_text}'.")],
            opened_at=now,
        )
        with self._lock:
            self._records[token] = record
            self._order.append(token)
            while len(self._order) > self._order.maxlen:
                stale = self._order.popleft()
                self._records.pop(stale, None)
        return record

    def resolve_incident(self, token: str, result: ExecutionResult) -> IncidentRecord | None:
        """Mark an incident resolved with the outcome of its real (non-dry-run) execution."""
        with self._lock:
            record = self._records.get(token)
            if record is None:
                return None
            now = datetime.now(timezone.utc)
            record.resolved_at = now
            record.success = result.success
            record.resolution_message = result.message
            record.mttr_seconds = round((now - record.opened_at).total_seconds(), 3)
            record.timeline.append(
                IncidentEvent(timestamp=now, message=f"Execution {'succeeded' if result.success else 'failed'}: {result.message}")
            )
            return record

    def latest_resolved(self) -> IncidentRecord | None:
        """Return the most recently resolved incident, if any."""
        with self._lock:
            for token in reversed(self._order):
                record = self._records.get(token)
                if record is not None and record.resolved_at is not None:
                    return record.model_copy(deep=True)
            return None

    def get(self, token: str) -> IncidentRecord | None:
        """Return a specific incident by its confirmation token, if known."""
        with self._lock:
            record = self._records.get(token)
            return record.model_copy(deep=True) if record is not None else None


def render_markdown(record: IncidentRecord) -> str:
    """Render an `IncidentRecord` as a Markdown SRE post-mortem report."""
    timeline_lines = "\n".join(f"- `{event.timestamp.isoformat()}` — {event.message}" for event in record.timeline)
    status = "RESOLVED" if record.resolved_at is not None else "PENDING"
    mttr = f"{record.mttr_seconds:.3f}s" if record.mttr_seconds is not None else "N/A (unresolved)"
    return f"""# Incident Post-Mortem — {record.token}

**Status:** {status}
**Mode:** {record.mode}
**Actor:** {record.actor}
**Language:** {record.language}

## Triggering Alert

{record.triggering_alert}

## Audio Remediation Command

> "{record.audio_remediation_command}"

## Timeline

{timeline_lines}

## Resolution

- **Intent:** {record.intent}
- **Target:** {record.target}
- **Confirmation Token:** `{record.token}`
- **Success:** {record.success if record.success is not None else "N/A (unresolved)"}
- **Resolution Message:** {record.resolution_message or "N/A (unresolved)"}
- **Mean Time To Resolution (MTTR):** {mttr}
"""


incident_log = IncidentLog()
