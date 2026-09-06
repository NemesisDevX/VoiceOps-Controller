"""Deterministic, stateful voice-command parsing with conversational pronoun resolution.

The parser is intentionally rule-based (no LLM) so that operator commands issued during an
incident are fast, auditable, and fully reproducible. `ConversationContext` retains the
subjects of the most recent INSPECT query so that a natural follow-up like "kill it" or
"isolate the top one" resolves against the process that was just surfaced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.security import requires_confirmation
from app.schemas.command import InspectionTarget, IntentType, ParsedCommand
from app.schemas.telemetry import ProcessInfo

_PRONOUN_PATTERN = re.compile(
    r"\b(it|that process|that one|the top one|the top process|that pid|this process)\b",
    re.IGNORECASE,
)
_PID_PATTERN = re.compile(r"\bpid\s+(\d+)\b", re.IGNORECASE)
_BARE_NUMBER_PATTERN = re.compile(r"\b(\d+)\b")
_IP_PATTERN = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_PROCESS_NAME_PATTERN = re.compile(
    r"\b(?:process|processes|program)\s+(?:named|called)?\s*([a-zA-Z0-9_.\-]+)",
    re.IGNORECASE,
)

_KILL_VERBS = ("kill", "terminate", "stop", "end")
_ISOLATE_VERBS = ("isolate", "quarantine", "block", "disconnect", "cut off")
_INSPECT_VERBS = ("show", "list", "what", "which", "display", "get", "check")


@dataclass
class ConversationContext:
    """Stateful memory of the most recent INSPECT result set, used for pronoun resolution."""

    last_intent: IntentType | None = None
    last_subjects: list[ProcessInfo] = field(default_factory=list)
    last_inspection_target: InspectionTarget | None = None
    updated_at: datetime | None = None

    def remember(self, intent: IntentType, subjects: list[ProcessInfo], target: InspectionTarget | None = None) -> None:
        """Record the results of an executed INSPECT query for later pronoun resolution."""
        self.last_intent = intent
        self.last_subjects = subjects
        self.last_inspection_target = target
        self.updated_at = datetime.now(timezone.utc)

    def top_subject(self) -> ProcessInfo | None:
        """Return the primary (highest-ranked) subject from the last INSPECT query, if any."""
        return self.last_subjects[0] if self.last_subjects else None

    def clear(self) -> None:
        self.last_intent = None
        self.last_subjects = []
        self.last_inspection_target = None
        self.updated_at = None


def _detect_inspection_target(text: str) -> InspectionTarget:
    if "memory" in text or "ram" in text:
        return InspectionTarget.MEMORY
    if "cpu" in text or "processor" in text:
        return InspectionTarget.CPU
    if "disk" in text or "storage" in text:
        return InspectionTarget.DISK
    if "network" in text or "socket" in text or "connection" in text:
        return InspectionTarget.NETWORK
    return InspectionTarget.GENERAL


class IntentParser:
    """Parses raw transcripts into `ParsedCommand`s, maintaining per-session conversational state."""

    def __init__(self) -> None:
        self.context = ConversationContext()

    def parse(self, text: str) -> ParsedCommand:
        """Parse a single transcript into a `ParsedCommand`, resolving pronouns against context."""
        normalized = text.strip().lower()
        if not normalized:
            return ParsedCommand(raw_text=text, intent=IntentType.UNKNOWN, confidence=0.0)

        if any(verb in normalized for verb in _KILL_VERBS):
            return self._parse_mutation(text, normalized, IntentType.PROCESS_KILL)

        if any(verb in normalized for verb in _ISOLATE_VERBS):
            return self._parse_mutation(text, normalized, IntentType.NETWORK_ISOLATE)

        if any(verb in normalized for verb in _INSPECT_VERBS):
            return self._parse_inspection(text, normalized)

        return ParsedCommand(raw_text=text, intent=IntentType.UNKNOWN, confidence=0.0)

    def _parse_inspection(self, raw_text: str, normalized: str) -> ParsedCommand:
        target = _detect_inspection_target(normalized)
        return ParsedCommand(
            raw_text=raw_text,
            intent=IntentType.INSPECT,
            inspection_target=target,
            requires_confirmation=False,
            confidence=0.9,
        )

    def _parse_mutation(self, raw_text: str, normalized: str, intent: IntentType) -> ParsedCommand:
        pid_match = _PID_PATTERN.search(normalized)
        ip_match = _IP_PATTERN.search(normalized) if intent is IntentType.NETWORK_ISOLATE else None
        name_match = _PROCESS_NAME_PATTERN.search(normalized)

        target_pid: int | None = int(pid_match.group(1)) if pid_match else None
        target_ip: str | None = ip_match.group(1) if ip_match else None
        target_name: str | None = name_match.group(1) if name_match else None

        if target_pid is None and target_name is None and not target_ip:
            bare_number = _BARE_NUMBER_PATTERN.search(normalized)
            if bare_number:
                target_pid = int(bare_number.group(1))

        resolved_from_context = False
        if target_pid is None and target_name is None and not target_ip and _PRONOUN_PATTERN.search(normalized):
            subject = self.context.top_subject()
            if subject is not None:
                target_pid = subject.pid
                target_name = subject.name
                resolved_from_context = True

        return ParsedCommand(
            raw_text=raw_text,
            intent=intent,
            target_pid=target_pid,
            target_process_name=target_name,
            target_ip=target_ip,
            resolved_from_context=resolved_from_context,
            requires_confirmation=requires_confirmation(intent),
            confidence=0.85 if (target_pid or target_name or target_ip) else 0.4,
        )

    def remember_inspection(self, target: InspectionTarget, subjects: list[ProcessInfo]) -> None:
        """Update conversational memory after an INSPECT command has been executed."""
        self.context.remember(IntentType.INSPECT, subjects, target)
