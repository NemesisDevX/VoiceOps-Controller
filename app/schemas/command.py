"""Parsed voice-command models: intent enumeration, command slots, and execution results."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.telemetry import ProcessInfo


class IntentType(str, Enum):
    """Canonical intents recognized by the deterministic intent parser."""

    INSPECT = "INSPECT"
    PROCESS_KILL = "PROCESS_KILL"
    NETWORK_ISOLATE = "NETWORK_ISOLATE"
    ROLLBACK = "ROLLBACK"
    UNKNOWN = "UNKNOWN"


class InspectionTarget(str, Enum):
    """The metric dimension an INSPECT query is asking about."""

    MEMORY = "MEMORY"
    CPU = "CPU"
    DISK = "DISK"
    NETWORK = "NETWORK"
    GENERAL = "GENERAL"


class ParsedCommand(BaseModel):
    """The structured result of parsing a single voice transcript into an actionable intent."""

    raw_text: str = Field(..., description="The original transcript text that was parsed.")
    intent: IntentType = Field(..., description="The recognized intent category.")
    inspection_target: InspectionTarget | None = Field(
        default=None, description="For INSPECT intents, which metric dimension was requested."
    )
    target_pid: int | None = Field(default=None, description="Explicit or resolved target process ID, if any.")
    target_process_name: str | None = Field(
        default=None, description="Explicit or resolved target process name, if any."
    )
    target_ip: str | None = Field(default=None, description="Explicit or resolved target IP address, if any.")
    resolved_from_context: bool = Field(
        default=False, description="True if the target was resolved from conversational memory (e.g. 'it')."
    )
    requires_confirmation: bool = Field(
        default=False, description="True if this is a mutating command gated behind a confirmation token."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Parser confidence in the resolved intent.")
    language: str = Field(default="en", description="ISO 639-1 code of the language the command was matched in.")


class PendingConfirmation(BaseModel):
    """A mutating command awaiting explicit operator confirmation before execution."""

    token: str = Field(..., description="Single-use confirmation token redeemed via the confirm endpoint.")
    command: ParsedCommand = Field(..., description="The mutating command awaiting confirmation.")
    created_at: datetime = Field(..., description="UTC timestamp the confirmation was registered.")
    expires_at: datetime = Field(..., description="UTC timestamp after which the token is no longer valid.")
    mode: str = Field(default="HOST_LOCAL", description="Telemetry mode active when this confirmation was registered.")


class ConfirmationRequest(BaseModel):
    """Request body for `POST /api/v1/commands/confirm`."""

    token: str = Field(..., min_length=1, description="The confirmation token to redeem.")
    dry_run: bool = Field(
        default=False, description="If True, evaluate and report the effect without mutating system state."
    )


class ExecutionResult(BaseModel):
    """The outcome of executing (or dry-run evaluating) a parsed command."""

    success: bool = Field(..., description="Whether the action completed (or would complete) successfully.")
    intent: IntentType = Field(..., description="The intent that was executed.")
    message: str = Field(..., description="Human-readable summary of the outcome.")
    dry_run: bool = Field(default=False, description="True if this result reflects a dry-run evaluation only.")
    affected_pid: int | None = Field(default=None, description="PID affected by the action, if applicable.")
    affected_process_name: str | None = Field(default=None, description="Process name affected, if applicable.")
    processes: list[ProcessInfo] = Field(
        default_factory=list, description="Process listing returned by an INSPECT query."
    )
