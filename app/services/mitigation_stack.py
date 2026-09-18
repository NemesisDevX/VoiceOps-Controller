"""In-memory LIFO stack of executed mitigations powering instant voice-driven rollback.

Every successful, non-dry-run mutating execution pushes a `MitigationEntry` describing
exactly how to reverse it (a pod snapshot for cluster actions, blocked IPs for network
isolation). A bare multilingual "rollback"/"undo" intent pops the newest entry and
reinstates the target — restoring simulated pod state verbatim or removing previously
installed firewall rules. Entries are per-process, matching the single-worker deployment
model of the confirmation registry.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from app.schemas.command import ExecutionResult, IntentType
from app.services import system_engine

MitigationKind = Literal["cluster_pod", "network_isolate", "host_process"]


@dataclass
class MitigationEntry:
    """Everything needed to reverse one executed mitigation."""

    token: str
    intent: IntentType
    mode: str
    target: str
    kind: MitigationKind
    data: dict = field(default_factory=dict)
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def describe(self) -> str:
        """Human-readable description used in dry-run previews and rollback messages."""
        if self.kind == "cluster_pod":
            return f"{self.intent.value} of pod '{self.target}'"
        if self.kind == "network_isolate":
            ips = ", ".join(self.data.get("ips", [])) or "no recorded IPs"
            return f"network isolation of '{self.target}' ({ips})"
        return f"termination of host process '{self.target}'"


class MitigationStack:
    """Thread-safe LIFO stack of the most recent reversible mitigations."""

    def __init__(self, max_entries: int = 50) -> None:
        self._lock = threading.Lock()
        self._entries: deque[MitigationEntry] = deque(maxlen=max_entries)

    def push(self, entry: MitigationEntry) -> None:
        """Record a newly executed mitigation for later rollback."""
        with self._lock:
            self._entries.append(entry)

    def peek(self) -> MitigationEntry | None:
        """Return the newest entry without consuming it."""
        with self._lock:
            return self._entries[-1] if self._entries else None

    def pop(self) -> MitigationEntry | None:
        """Remove and return the newest entry, if any."""
        with self._lock:
            return self._entries.pop() if self._entries else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        """Drop every recorded entry (test isolation)."""
        with self._lock:
            self._entries.clear()

    async def preview_rollback(self) -> ExecutionResult | None:
        """Dry-run description of what a bare ROLLBACK would reverse, or None if the stack is empty."""
        entry = self.peek()
        if entry is None:
            return None
        return ExecutionResult(
            success=True,
            intent=IntentType.ROLLBACK,
            message=f"[DRY RUN] Would roll back {entry.describe()}. Execution permissions are not verified.",
            dry_run=True,
            affected_process_name=entry.target,
        )

    async def rollback_last(self, dry_run: bool = False) -> tuple[ExecutionResult, MitigationEntry | None]:
        """Reverse the most recently executed mitigation.

        Returns the `ExecutionResult` plus the entry that was reversed (for incident
        linkage). When `dry_run` is True the stack is only inspected, never popped, so a
        preview cannot consume the operator's rollback opportunity.
        """
        if dry_run:
            entry = self.peek()
            if entry is None:
                return self._empty_result(dry_run=True), None
            return (
                ExecutionResult(
                    success=True,
                    intent=IntentType.ROLLBACK,
                    message=f"[DRY RUN] Would roll back {entry.describe()}. Execution permissions are not verified.",
                    dry_run=True,
                    affected_process_name=entry.target,
                ),
                None,
            )

        entry = self.pop()
        if entry is None:
            return self._empty_result(dry_run=False), None

        if entry.kind == "cluster_pod":
            try:
                result = await system_engine.cluster_restore_pod(
                    entry.data.get("name", entry.target), entry.data.get("snapshot")
                )
            except system_engine.ProcessNotFoundError as exc:
                return (
                    ExecutionResult(
                        success=False,
                        intent=IntentType.ROLLBACK,
                        message=f"Rollback failed: {exc}",
                        dry_run=False,
                        affected_process_name=entry.target,
                    ),
                    entry,
                )
            result.message = f"Rolled back {entry.describe()}; pod restored to pre-mitigation state."
            return result, entry

        if entry.kind == "network_isolate":
            ips = list(entry.data.get("ips", []))
            try:
                removed = await system_engine.unblock_ips(ips)
            except system_engine._FirewallRuleError as exc:
                return (
                    ExecutionResult(
                        success=False,
                        intent=IntentType.ROLLBACK,
                        message=f"Rollback of {entry.describe()} failed: {exc}",
                        dry_run=False,
                        affected_process_name=entry.target,
                        affected_ips=ips,
                    ),
                    entry,
                )
            summary = ", ".join(removed) if removed else "no firewall rules were present"
            return (
                ExecutionResult(
                    success=True,
                    intent=IntentType.ROLLBACK,
                    message=f"Rolled back {entry.describe()}; removed isolation rules for {summary}.",
                    dry_run=False,
                    affected_process_name=entry.target,
                    affected_ips=removed,
                ),
                entry,
            )

        # Host process termination is irreversible: the process cannot be un-killed.
        return (
            ExecutionResult(
                success=False,
                intent=IntentType.ROLLBACK,
                message=(
                    f"Cannot roll back {entry.describe()}: terminated host processes cannot be "
                    "restored. Restart the process manually."
                ),
                dry_run=False,
                affected_process_name=entry.target,
            ),
            entry,
        )

    @staticmethod
    def _empty_result(dry_run: bool) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            intent=IntentType.ROLLBACK,
            message="No prior mitigation is available to roll back.",
            dry_run=dry_run,
        )


mitigation_stack = MitigationStack()
