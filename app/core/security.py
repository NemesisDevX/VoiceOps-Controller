"""Command safety policy: protected-process allowlisting and confirmation-token issuance.

Mutating actions (`PROCESS_KILL`, `NETWORK_ISOLATE`) are never executed directly from a
voice command. Instead, the intent parser produces a pending action that must be redeemed
through a short-lived, single-use confirmation token via `POST /api/v1/commands/confirm`.
This module owns that gate.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.schemas.command import IntentType, ParsedCommand, PendingConfirmation

# Process names that must never be terminated or network-isolated regardless of the
# operator's request. Matched case-insensitively against `psutil.Process.name()`.
PROTECTED_PROCESS_NAMES: frozenset[str] = frozenset(
    {
        # Windows core/session processes
        "system", "system idle process", "smss.exe", "csrss.exe", "wininit.exe",
        "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "explorer.exe",
        "registry", "memory compression", "fontdrvhost.exe", "dwm.exe",
        # POSIX/Linux init & core daemons
        "init", "systemd", "kernel", "kthreadd", "launchd", "kernel_task",
        # This process itself must remain protected from a runaway voice command.
        "python", "python3", "uvicorn",
    }
)

MUTATING_INTENTS: frozenset[IntentType] = frozenset({IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE, IntentType.ROLLBACK})


def is_protected_process(name: str) -> bool:
    """Return True if `name` matches a process that may never be mutated."""
    return name.strip().lower() in PROTECTED_PROCESS_NAMES


def requires_confirmation(intent: IntentType) -> bool:
    """Return True if `intent` is a mutating action requiring the confirmation gate."""
    return intent in MUTATING_INTENTS


def generate_confirmation_token() -> str:
    """Generate a cryptographically secure, URL-safe, single-use confirmation token."""
    return secrets.token_urlsafe(32)


class ConfirmationRegistry:
    """Thread-safe, in-memory store of pending confirmations awaiting operator sign-off.

    Tokens are single-use and expire after `ttl_seconds`. This is a Phase 1, single-process
    implementation; a distributed deployment would back this with a shared cache (e.g. Redis).
    """

    def __init__(self, ttl_seconds: int = 60) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, PendingConfirmation] = {}

    def register(self, command: ParsedCommand, mode: str = "HOST_LOCAL") -> PendingConfirmation:
        """Create and store a new pending confirmation for a mutating `ParsedCommand`.

        `mode` freezes which telemetry mode (HOST_LOCAL or K8S_CLUSTER) was active when the
        mutation was requested, so it can be redeemed against the correct execution path
        even if the operator switches modes before confirming.
        """
        now = datetime.now(timezone.utc)
        pending = PendingConfirmation(
            token=generate_confirmation_token(),
            command=command,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
            mode=mode,
        )
        with self._lock:
            self._prune_expired_locked()
            self._pending[pending.token] = pending
        return pending

    def redeem(self, token: str) -> PendingConfirmation | None:
        """Atomically pop and return the pending confirmation for `token`, if valid and unexpired."""
        with self._lock:
            self._prune_expired_locked()
            return self._pending.pop(token, None)

    def peek(self, token: str) -> PendingConfirmation | None:
        """Return the pending confirmation for `token` without consuming it, if valid and unexpired."""
        with self._lock:
            self._prune_expired_locked()
            return self._pending.get(token)

    def _prune_expired_locked(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [token for token, pending in self._pending.items() if pending.expires_at <= now]
        for token in expired:
            del self._pending[token]


confirmation_registry = ConfirmationRegistry(ttl_seconds=get_settings().CONFIRMATION_TOKEN_TTL_SECONDS)
