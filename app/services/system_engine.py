"""Non-blocking psutil wrappers for process inspection and safety-gated process termination.

Every psutil call in this module is synchronous/blocking, so each public coroutine offloads
its work to a worker thread via `asyncio.to_thread` to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from typing import Literal

import psutil

from app.core.security import is_protected_process
from app.schemas.command import ExecutionResult, IntentType
from app.schemas.telemetry import ProcessInfo, SystemTelemetry

logger = logging.getLogger(__name__)


class ProtectedProcessError(Exception):
    """Raised when a mutating action targets a process on the protected allowlist."""


class ProcessNotFoundError(Exception):
    """Raised when a target process cannot be located."""


class _FirewallRuleError(Exception):
    pass


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, _FirewallRuleError):
        return str(exc)
    if isinstance(exc, (psutil.AccessDenied, PermissionError)):
        return "Permission denied. Administrator privileges may be required."
    if isinstance(exc, subprocess.CalledProcessError):
        return f"The firewall command failed (exit code {exc.returncode}). Administrator privileges may be required."
    if isinstance(exc, subprocess.TimeoutExpired):
        return "The firewall command timed out; the last rule may have been applied. Check firewall state before retrying."
    if isinstance(exc, psutil.TimeoutExpired):
        return "Timed out waiting for the process to exit; termination could not be confirmed."
    if isinstance(exc, TimeoutError):
        return "The operation timed out; its outcome could not be confirmed. Check system state before retrying."
    if isinstance(exc, OSError):
        return "The operating system could not complete the operation. Check permissions and required system tools."
    return "The firewall command could not be completed. Check firewall state before retrying."


def _to_process_info(proc: psutil.Process, *, strict: bool = False) -> ProcessInfo | None:
    """Build a `ProcessInfo` snapshot from a live `psutil.Process`, or None if it has vanished."""
    try:
        with proc.oneshot():
            memory_info = proc.memory_info()
            return ProcessInfo(
                pid=proc.pid,
                name=proc.name(),
                cpu_percent=proc.cpu_percent(interval=None),
                memory_percent=round(proc.memory_percent(), 2),
                memory_rss_mb=round(memory_info.rss / (1024 * 1024), 2),
                status=proc.status(),
            )
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, PermissionError):
        if strict:
            raise
        return None


def _list_processes() -> list[ProcessInfo]:
    """Synchronously snapshot every visible process on the host."""
    snapshots: list[ProcessInfo] = []
    for proc in psutil.process_iter(["pid", "name"]):
        info = _to_process_info(proc)
        if info is not None:
            snapshots.append(info)
    return snapshots


def _count_active_sockets() -> int:
    """Best-effort count of active network sockets; returns 0 if the OS denies access."""
    try:
        return len(psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, PermissionError):
        return 0


def _capture_telemetry(top_limit: int) -> SystemTelemetry:
    """Synchronously build a full `SystemTelemetry` snapshot."""
    processes = _list_processes()
    top_memory = sorted(processes, key=lambda p: p.memory_percent, reverse=True)[:top_limit]
    top_cpu = sorted(processes, key=lambda p: p.cpu_percent, reverse=True)[:top_limit]

    return SystemTelemetry(
        cpu_percent=psutil.cpu_percent(interval=None),
        memory_percent=psutil.virtual_memory().percent,
        disk_percent=psutil.disk_usage(_root_path()).percent,
        active_sockets=_count_active_sockets(),
        top_memory_processes=top_memory,
        top_cpu_processes=top_cpu,
    )


def _root_path() -> str:
    return "C:\\" if sys.platform == "win32" else "/"


def _find_process_sync(pid: int | None, name: str | None) -> psutil.Process | None:
    if pid is not None:
        return psutil.Process(pid) if psutil.pid_exists(pid) else None
    if name is not None:
        target = name.strip().lower()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.name().strip().lower() == target:
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return None


def _terminate_sync(pid: int, dry_run: bool = False) -> ProcessInfo:
    proc = psutil.Process(pid)
    if is_protected_process(proc.name()):
        raise ProtectedProcessError(f"Refusing to terminate protected process '{proc.name()}' (pid={pid}).")

    info = _to_process_info(proc, strict=True)
    if info is None:
        raise ProcessNotFoundError(f"Process {pid} no longer exists.")
    if dry_run:
        return info

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except psutil.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    return info


def _isolate_network_sync(pid: int, dry_run: bool = False) -> tuple[ProcessInfo, list[str]]:
    proc = psutil.Process(pid)
    if is_protected_process(proc.name()):
        raise ProtectedProcessError(f"Refusing to network-isolate protected process '{proc.name()}' (pid={pid}).")

    info = _to_process_info(proc, strict=True)
    if info is None:
        raise ProcessNotFoundError(f"Process {pid} no longer exists.")

    remote_ips = sorted(
        {
            conn.raddr.ip
            for conn in proc.net_connections(kind="inet")
            if conn.raddr
        }
    )

    if not dry_run:
        blocked_ips: list[str] = []
        for ip in remote_ips:
            try:
                _block_ip(ip)
            except (subprocess.SubprocessError, OSError) as exc:
                message = f"Failed to install outbound firewall rule for {ip}. {_failure_reason(exc)}"
                if blocked_ips:
                    message += (
                        f" Partial firewall changes: rules already installed for {', '.join(blocked_ips)}"
                        "; these rules remain in place and were not rolled back."
                    )
                else:
                    message += " No firewall rules were confirmed installed."
                message += " Remaining rules were not attempted."
                raise _FirewallRuleError(message) from exc
            blocked_ips.append(ip)

    return info, remote_ips


def _block_ip(ip: str) -> None:
    """Insert a best-effort outbound firewall rule blocking `ip`. Silently logs failures."""
    try:
        if sys.platform == "win32":
            rule_name = f"voiceops-isolate-{ip.replace('.', '-').replace(':', '-')}"
            subprocess.run(
                [
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={rule_name}", "dir=out", "action=block", f"remoteip={ip}",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
        else:
            subprocess.run(
                ["iptables", "-A", "OUTPUT", "-d", ip, "-j", "DROP"],
                check=True,
                capture_output=True,
                timeout=5,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Failed to install firewall rule for %s: %s", ip, exc)
        raise


async def get_system_telemetry(top_limit: int = 5) -> SystemTelemetry:
    """Return a full, non-blocking system telemetry snapshot."""
    return await asyncio.to_thread(_capture_telemetry, top_limit)


async def get_top_processes(by: Literal["memory", "cpu"] = "memory", limit: int = 5) -> list[ProcessInfo]:
    """Return the top `limit` processes ranked by memory or CPU utilization."""
    processes = await asyncio.to_thread(_list_processes)
    key = (lambda p: p.memory_percent) if by == "memory" else (lambda p: p.cpu_percent)
    return sorted(processes, key=key, reverse=True)[:limit]


async def find_process(pid: int | None = None, name: str | None = None) -> ProcessInfo | None:
    """Locate a process by PID or exact (case-insensitive) name match."""
    proc = await asyncio.to_thread(_find_process_sync, pid, name)
    if proc is None:
        return None
    return await asyncio.to_thread(_to_process_info, proc)


async def terminate_process(pid: int, dry_run: bool = False) -> ExecutionResult:
    """Terminate `pid`, or evaluate the action without mutating state if `dry_run` is True.

    Raises `ProtectedProcessError` if `pid` belongs to a protected process, and
    `ProcessNotFoundError` if `pid` does not resolve to a live process.
    """
    try:
        info = await asyncio.to_thread(_terminate_sync, pid, dry_run)
    except psutil.NoSuchProcess as exc:
        raise ProcessNotFoundError(f"Process {pid} no longer exists.") from exc
    except (psutil.AccessDenied, psutil.TimeoutExpired, subprocess.SubprocessError, OSError) as exc:
        action = "preview termination of" if dry_run else "terminate"
        message = f"Failed to {action} process (pid={pid}). {_failure_reason(exc)}"
        if not dry_run:
            message += " Termination was not confirmed; check process state before retrying."
        return ExecutionResult(
            success=False,
            intent=IntentType.PROCESS_KILL,
            message=message,
            dry_run=dry_run,
            affected_pid=pid,
        )

    if dry_run:
        return ExecutionResult(
            success=True,
            intent=IntentType.PROCESS_KILL,
            message=f"[DRY RUN] Would terminate process '{info.name}' (pid={pid}). Execution permissions are not verified.",
            dry_run=True,
            affected_pid=pid,
            affected_process_name=info.name,
        )

    return ExecutionResult(
        success=True,
        intent=IntentType.PROCESS_KILL,
        message=f"Terminated process '{info.name}' (pid={pid}).",
        dry_run=False,
        affected_pid=pid,
        affected_process_name=info.name,
    )


async def isolate_network(pid: int, dry_run: bool = False) -> ExecutionResult:
    """Block outbound network traffic for `pid`'s active connections.

    Raises `ProtectedProcessError` if `pid` belongs to a protected process, and
    `ProcessNotFoundError` if `pid` does not resolve to a live process.
    """
    try:
        info, remote_ips = await asyncio.to_thread(_isolate_network_sync, pid, dry_run)
    except psutil.NoSuchProcess as exc:
        raise ProcessNotFoundError(f"Process {pid} no longer exists.") from exc
    except (psutil.AccessDenied, psutil.TimeoutExpired, subprocess.SubprocessError, OSError, _FirewallRuleError) as exc:
        action = "preview network isolation for" if dry_run else "isolate network access for"
        return ExecutionResult(
            success=False,
            intent=IntentType.NETWORK_ISOLATE,
            message=f"Failed to {action} process (pid={pid}). {_failure_reason(exc)}",
            dry_run=dry_run,
            affected_pid=pid,
        )

    if dry_run:
        return ExecutionResult(
            success=True,
            intent=IntentType.NETWORK_ISOLATE,
            message=f"[DRY RUN] Would isolate network access for '{info.name}' (pid={pid}). Execution permissions are not verified.",
            dry_run=True,
            affected_pid=pid,
            affected_process_name=info.name,
        )

    ip_summary = ", ".join(remote_ips) if remote_ips else "no active remote connections"
    return ExecutionResult(
        success=True,
        intent=IntentType.NETWORK_ISOLATE,
        message=f"Isolated network access for '{info.name}' (pid={pid}); blocked: {ip_summary}.",
        dry_run=False,
        affected_pid=pid,
        affected_process_name=info.name,
    )
