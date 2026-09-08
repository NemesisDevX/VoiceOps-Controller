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

import random

from app.core.security import is_protected_process
from app.schemas.command import ExecutionResult, IntentType
from app.schemas.telemetry import ClusterTelemetry, PodInfo, ProcessInfo, SystemTelemetry

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


# --------------------------------------------------------------------------------------
# Kubernetes cluster sandbox (Phase 3): a fully simulated, in-memory "cluster" used to
# demonstrate dual-mode telemetry and mutation without touching any real infrastructure.
# Nothing below this line reads or mutates real host state.
# --------------------------------------------------------------------------------------


class _SimulatedPod:
    """Mutable in-memory state for a single simulated Kubernetes pod."""

    def __init__(
        self,
        name: str,
        *,
        status: str = "Running",
        cpu_percent: float = 5.0,
        memory_percent: float = 20.0,
        restarts: int = 0,
        anomaly: bool = False,
    ) -> None:
        self.name = name
        self.status = status
        self.cpu_percent = cpu_percent
        self.memory_percent = memory_percent
        self.restarts = restarts
        self.anomaly = anomaly

    def to_info(self) -> PodInfo:
        return PodInfo(
            name=self.name,
            status=self.status,
            cpu_percent=round(self.cpu_percent, 2),
            memory_percent=round(self.memory_percent, 2),
            restarts=self.restarts,
            anomaly=self.anomaly,
        )


class ClusterSimulator:
    """Owns the simulated cluster-wide metrics and per-pod state for K8S_CLUSTER mode.

    A module-level singleton (`_cluster_simulator`) is used so the simulation persists
    across calls, ticking forward slightly on every `get_cluster_telemetry` invocation
    rather than being fully recomputed from scratch each time.
    """

    def __init__(self) -> None:
        self.pods: dict[str, _SimulatedPod] = {
            "payment-gateway-pod": _SimulatedPod(
                "payment-gateway-pod",
                status="Running",
                cpu_percent=72.0,
                memory_percent=88.0,
                restarts=3,
                anomaly=True,
            ),
            "auth-service-pod": _SimulatedPod(
                "auth-service-pod",
                status="Running",
                cpu_percent=8.0,
                memory_percent=22.0,
                restarts=0,
                anomaly=False,
            ),
            "redis-sentinel-pod": _SimulatedPod(
                "redis-sentinel-pod",
                status="Running",
                cpu_percent=4.0,
                memory_percent=15.0,
                restarts=0,
                anomaly=False,
            ),
        }
        self.rps = 1100.0
        self.p99_latency_ms = 60.0
        self.error_rate_5xx = 0.05

    def _has_active_anomaly(self) -> bool:
        return any(pod.anomaly for pod in self.pods.values())

    def tick(self) -> ClusterTelemetry:
        """Advance the simulation by one small random-walk step and return a snapshot."""
        anomaly_active = self._has_active_anomaly()

        # Cluster-wide requests-per-second: random walk within a plausible band.
        self.rps += random.uniform(-40.0, 40.0)
        self.rps = max(800.0, min(1500.0, self.rps))

        if anomaly_active:
            target_latency = random.uniform(900.0, 1400.0)
            target_error_rate = random.uniform(35.0, 45.0)
        else:
            target_latency = random.uniform(40.0, 90.0)
            target_error_rate = random.uniform(0.02, 0.1)

        # Nudge toward the target band rather than snapping straight to it, for a
        # "live" feel between ticks.
        self.p99_latency_ms += (target_latency - self.p99_latency_ms) * 0.5
        self.error_rate_5xx += (target_error_rate - self.error_rate_5xx) * 0.5
        self.p99_latency_ms = max(0.0, self.p99_latency_ms)
        self.error_rate_5xx = max(0.0, min(100.0, self.error_rate_5xx))

        for pod in self.pods.values():
            if pod.name == "payment-gateway-pod" and pod.anomaly:
                pod.cpu_percent = max(0.0, min(100.0, pod.cpu_percent + random.uniform(-2.0, 2.0)))
                pod.memory_percent = max(0.0, min(100.0, pod.memory_percent + random.uniform(-1.5, 1.5)))
                pod.memory_percent = max(85.0, min(95.0, pod.memory_percent))
            else:
                pod.cpu_percent = max(0.0, min(100.0, pod.cpu_percent + random.uniform(-1.0, 1.0)))
                pod.memory_percent = max(0.0, min(100.0, pod.memory_percent + random.uniform(-1.0, 1.0)))

        return ClusterTelemetry(
            rps=round(self.rps, 2),
            p99_latency_ms=round(self.p99_latency_ms, 2),
            error_rate_5xx=round(self.error_rate_5xx, 3),
            pods=[pod.to_info() for pod in self.pods.values()],
        )

    def recover_pod(self, pod_name: str) -> None:
        """Immediately clear a pod's anomaly and normalize cluster-wide metrics."""
        pod = self.pods[pod_name]
        pod.anomaly = False
        pod.status = "Running"
        pod.cpu_percent = min(pod.cpu_percent, 15.0)
        pod.memory_percent = min(pod.memory_percent, 25.0)
        # Immediate metric recovery: snap cluster-wide indicators back to healthy values
        # rather than waiting for subsequent ticks to converge.
        self.error_rate_5xx = 0.05
        self.p99_latency_ms = 65.0
        self.rps = max(800.0, min(1500.0, self.rps))


_cluster_simulator = ClusterSimulator()


async def get_cluster_telemetry() -> ClusterTelemetry:
    """Advance the simulated cluster one tick and return a snapshot.

    Pure Python/random arithmetic; cheap and non-blocking, so it is safe to call once per
    second from the telemetry broadcaster. Declared `async def` for interface parity with
    `get_system_telemetry`, even though no actual I/O or thread offload is required.
    """
    return _cluster_simulator.tick()


async def cluster_execute(intent: IntentType, target_name: str, dry_run: bool = False) -> ExecutionResult:
    """Simulate a mutating action (`PROCESS_KILL`, `NETWORK_ISOLATE`, or `ROLLBACK`) against
    a named pod in the simulated Kubernetes cluster.

    Raises `ProcessNotFoundError` if `target_name` does not match a pod in the simulator,
    mirroring the host-mode error handling contract so callers can catch uniformly.
    """
    if not target_name or target_name not in _cluster_simulator.pods:
        raise ProcessNotFoundError(f"Pod '{target_name}' was not found in the simulated cluster.")

    pod = _cluster_simulator.pods[target_name]

    if intent is IntentType.PROCESS_KILL:
        action_preview = "restart"
        action_done = "Restarted"
    elif intent is IntentType.NETWORK_ISOLATE:
        action_preview = "network-isolate"
        action_done = "Isolated network access for"
    elif intent is IntentType.ROLLBACK:
        action_preview = "roll back the deployment for"
        action_done = "Rolled back the deployment for"
    else:
        return ExecutionResult(
            success=False,
            intent=intent,
            message=f"Intent {intent} is not a supported cluster mutation.",
            dry_run=dry_run,
            affected_process_name=target_name,
        )

    if dry_run:
        return ExecutionResult(
            success=True,
            intent=intent,
            message=f"[DRY RUN] Would {action_preview} pod '{pod.name}'. Execution permissions are not verified.",
            dry_run=True,
            affected_process_name=pod.name,
        )

    was_anomalous = pod.anomaly
    if intent is IntentType.PROCESS_KILL:
        pod.restarts += 1
        if was_anomalous and pod.name == "payment-gateway-pod":
            _cluster_simulator.recover_pod(pod.name)
    elif intent is IntentType.ROLLBACK:
        if was_anomalous and pod.name == "payment-gateway-pod":
            _cluster_simulator.recover_pod(pod.name)
        pod.restarts += 1
    # NETWORK_ISOLATE does not, on its own, clear an anomaly in this sandbox.

    recovery_note = ""
    if was_anomalous and pod.name == "payment-gateway-pod" and not pod.anomaly:
        recovery_note = " Anomaly cleared; metrics have recovered."

    return ExecutionResult(
        success=True,
        intent=intent,
        message=f"{action_done} pod '{pod.name}'.{recovery_note}",
        dry_run=False,
        affected_process_name=pod.name,
    )
