"""Unit tests for non-blocking psutil telemetry wrappers and sandboxed process handling.

All `psutil` calls are monkeypatched so these tests never touch the real host's processes.
"""

from __future__ import annotations

import contextlib
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from app.schemas.command import IntentType
from app.services import system_engine
from app.services.system_engine import ProcessNotFoundError, ProtectedProcessError


class FakeProcess:
    """A minimal stand-in for `psutil.Process` used to isolate tests from the real OS."""

    def __init__(
        self,
        pid: int,
        name: str = "worker.exe",
        cpu: float = 5.0,
        mem_percent: float = 2.0,
        rss_mb: float = 128.0,
        status: str = "running",
    ) -> None:
        self.pid = pid
        self._name = name
        self._cpu = cpu
        self._mem_percent = mem_percent
        self._rss_mb = rss_mb
        self._status = status
        self.terminated = False
        self.killed = False

    def oneshot(self):
        return contextlib.nullcontext()

    def name(self) -> str:
        return self._name

    def cpu_percent(self, interval=None) -> float:
        return self._cpu

    def memory_percent(self) -> float:
        return self._mem_percent

    def memory_info(self):
        return SimpleNamespace(rss=int(self._rss_mb * 1024 * 1024))

    def status(self) -> str:
        return self._status

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def net_connections(self, kind: str = "inet"):
        return []


@pytest.mark.asyncio
async def test_get_system_telemetry_returns_expected_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = [FakeProcess(pid=1, name="a", cpu=10.0, mem_percent=5.0), FakeProcess(pid=2, name="b", cpu=90.0, mem_percent=1.0)]

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(processes))
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 33.3)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=55.5))
    monkeypatch.setattr(psutil, "disk_usage", lambda path: SimpleNamespace(percent=77.7))
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": [object(), object()])

    telemetry = await system_engine.get_system_telemetry(top_limit=5)

    assert telemetry.cpu_percent == 33.3
    assert telemetry.memory_percent == 55.5
    assert telemetry.disk_percent == 77.7
    assert telemetry.active_sockets == 2
    assert telemetry.top_cpu_processes[0].pid == 2
    assert telemetry.top_memory_processes[0].pid == 1


@pytest.mark.asyncio
async def test_get_system_telemetry_handles_denied_socket_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([]))
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 1.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=1.0))
    monkeypatch.setattr(psutil, "disk_usage", lambda path: SimpleNamespace(percent=1.0))

    def _deny(kind="inet"):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", _deny)

    telemetry = await system_engine.get_system_telemetry()

    assert telemetry.active_sockets == 0


@pytest.mark.asyncio
async def test_get_top_processes_ranks_by_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = [FakeProcess(pid=1, mem_percent=2.0), FakeProcess(pid=2, mem_percent=9.0), FakeProcess(pid=3, mem_percent=5.0)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(processes))

    ranked = await system_engine.get_top_processes(by="memory", limit=2)

    assert [p.pid for p in ranked] == [2, 3]


@pytest.mark.asyncio
async def test_find_process_by_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(pid=99, name="target")
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 99)
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    found = await system_engine.find_process(pid=99)

    assert found is not None
    assert found.pid == 99
    assert found.name == "target"


@pytest.mark.asyncio
async def test_find_process_by_pid_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)

    found = await system_engine.find_process(pid=12345)

    assert found is None


@pytest.mark.asyncio
async def test_terminate_process_refuses_protected_process(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(pid=4, name="explorer.exe")
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    with pytest.raises(ProtectedProcessError):
        await system_engine.terminate_process(pid=4, dry_run=False)

    assert fake.terminated is False


@pytest.mark.asyncio
async def test_terminate_process_dry_run_does_not_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(pid=7, name="runaway-worker.exe")
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

    result = await system_engine.terminate_process(pid=7, dry_run=True)

    assert result.success is True
    assert result.dry_run is True
    assert fake.terminated is False
    assert result.affected_pid == 7


@pytest.mark.asyncio
async def test_terminate_process_executes_for_unprotected_process(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(pid=8, name="runaway-worker.exe")
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    result = await system_engine.terminate_process(pid=8, dry_run=False)

    assert result.success is True
    assert result.dry_run is False
    assert fake.terminated is True


@pytest.mark.asyncio
async def test_terminate_process_raises_not_found_when_process_vanished(monkeypatch: pytest.MonkeyPatch) -> None:
    class VanishedProcess(FakeProcess):
        def oneshot(self):
            raise psutil.NoSuchProcess(pid=self.pid)

    fake = VanishedProcess(pid=9, name="ghost")
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    with pytest.raises(ProcessNotFoundError):
        await system_engine.terminate_process(pid=9, dry_run=False)


@pytest.mark.asyncio
async def test_isolate_network_refuses_protected_process(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(pid=5, name="lsass.exe")
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    with pytest.raises(ProtectedProcessError):
        await system_engine.isolate_network(pid=5, dry_run=False)


@pytest.mark.asyncio
async def test_isolate_network_dry_run_previews_without_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(pid=11, name="cache-node")
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

    result = await system_engine.isolate_network(pid=11, dry_run=True)

    assert result.success is True
    assert result.dry_run is True
    assert result.affected_pid == 11


@pytest.mark.asyncio
async def test_isolate_network_blocks_active_remote_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(pid=12, name="cache-node")
    fake.net_connections = lambda kind="inet": [SimpleNamespace(raddr=SimpleNamespace(ip="10.0.0.9"))]
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    blocked_ips: list[str] = []
    monkeypatch.setattr(system_engine, "_block_ip", lambda ip: blocked_ips.append(ip))

    result = await system_engine.isolate_network(pid=12, dry_run=False)

    assert result.success is True
    assert blocked_ips == ["10.0.0.9"]


@pytest.fixture
def sandbox_process(monkeypatch: pytest.MonkeyPatch):
    fake = FakeProcess(pid=60001)
    monkeypatch.setattr(psutil, "Process", Mock(return_value=fake))
    monkeypatch.setattr(psutil, "pid_exists", Mock(return_value=True))
    run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0))
    monkeypatch.setattr(system_engine.subprocess, "run", run)
    return fake, run


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [psutil.AccessDenied, PermissionError])
@pytest.mark.parametrize("stage", ["terminate", "wait", "kill"])
async def test_terminate_denied_returns_failed_result(monkeypatch, sandbox_process, error_type, stage) -> None:
    fake, run = sandbox_process
    if stage == "kill":
        monkeypatch.setattr(fake, "wait", Mock(side_effect=psutil.TimeoutExpired(3, pid=fake.pid)))
    monkeypatch.setattr(fake, stage, Mock(side_effect=error_type("private OS details")))

    result = await system_engine.terminate_process(fake.pid)

    assert result.success is False
    assert result.intent.value == "PROCESS_KILL"
    assert result.dry_run is False
    assert result.affected_pid == fake.pid
    assert "permission denied" in result.message.lower()
    assert "private OS details" not in result.message
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["terminate_process", "isolate_network"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("stage", ["Process", "name", "memory_info"])
@pytest.mark.parametrize("error_type", [psutil.AccessDenied, PermissionError])
async def test_denied_inspection_never_reports_success(
    monkeypatch, sandbox_process, operation, dry_run, stage, error_type
) -> None:
    fake, run = sandbox_process
    target = psutil if stage == "Process" else fake
    monkeypatch.setattr(target, stage, Mock(side_effect=error_type("private OS details")))

    result = await getattr(system_engine, operation)(fake.pid, dry_run=dry_run)

    assert result.success is False
    assert result.dry_run is dry_run
    assert result.affected_pid == fake.pid
    assert "permission denied" in result.message.lower()
    assert "private OS details" not in result.message
    assert fake.terminated is False
    assert fake.killed is False
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("error_type", [psutil.AccessDenied, PermissionError])
async def test_isolate_denied_connections_returns_failed_result(monkeypatch, sandbox_process, dry_run, error_type) -> None:
    fake, run = sandbox_process
    monkeypatch.setattr(fake, "net_connections", Mock(side_effect=error_type("private OS details")))

    result = await system_engine.isolate_network(fake.pid, dry_run=dry_run)

    assert result.success is False
    assert result.intent.value == "NETWORK_ISOLATE"
    assert result.dry_run is dry_run
    assert "permission denied" in result.message.lower()
    assert "private OS details" not in result.message
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, expected_message",
    [
        (subprocess.CalledProcessError(1, "netsh", output=b"private OS details", stderr=b"elevation required"), "administrator"),
        (PermissionError("private OS details"), "permission denied"),
        (subprocess.TimeoutExpired("netsh", 5, output=b"private OS details"), "timed out"),
        (FileNotFoundError("private OS details"), "system tools"),
    ],
)
async def test_netsh_failure_never_reports_isolation_success(
    monkeypatch, sandbox_process, error, expected_message
) -> None:
    fake, run = sandbox_process
    monkeypatch.setattr(system_engine.sys, "platform", "win32")
    monkeypatch.setattr(fake, "net_connections", lambda kind="inet": [SimpleNamespace(raddr=SimpleNamespace(ip="10.0.0.9"))])
    run.side_effect = error

    result = await system_engine.isolate_network(fake.pid)

    assert result.success is False
    assert result.dry_run is False
    assert result.affected_pid == fake.pid
    assert expected_message in result.message.lower()
    assert "10.0.0.9" in result.message
    assert "private OS details" not in result.message
    assert "Isolated network access" not in result.message
    run.assert_called_once()
    assert run.call_args.args[0][0] == "netsh"
    assert run.call_args.kwargs == {"check": True, "capture_output": True, "timeout": 5}
    if isinstance(error, subprocess.TimeoutExpired):
        assert "may have been applied" in result.message.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, "netsh", stderr=b"private OS details"),
        PermissionError("private OS details"),
        subprocess.TimeoutExpired("netsh", 5),
    ],
)
async def test_isolate_partial_failure_discloses_installed_rules(monkeypatch, sandbox_process, error) -> None:
    fake, run = sandbox_process
    monkeypatch.setattr(system_engine.sys, "platform", "win32")
    monkeypatch.setattr(
        fake,
        "net_connections",
        lambda kind="inet": [
            SimpleNamespace(raddr=SimpleNamespace(ip=ip))
            for ip in ["10.0.0.3", "10.0.0.1", "10.0.0.2", "10.0.0.1"]
        ],
    )
    run.side_effect = [subprocess.CompletedProcess(args=[], returncode=0), error]

    result = await system_engine.isolate_network(fake.pid)

    assert result.success is False
    assert "partial" in result.message.lower()
    assert "10.0.0.1" in result.message


# --------------------------------------------------------------------------------------
# Phase 3: simulated Kubernetes cluster sandbox (dual-mode telemetry & mutation).
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cluster_simulator():
    """Ensure the module-level cluster simulator singleton never leaks state between tests."""
    yield
    system_engine._cluster_simulator = system_engine.ClusterSimulator()


@pytest.mark.asyncio
async def test_get_cluster_telemetry_returns_expected_shape() -> None:
    from app.schemas.telemetry import ClusterTelemetry

    telemetry = await system_engine.get_cluster_telemetry()

    assert isinstance(telemetry, ClusterTelemetry)
    assert telemetry.rps >= 0.0
    assert telemetry.p99_latency_ms >= 0.0
    assert 0.0 <= telemetry.error_rate_5xx <= 100.0
    pod_names = {pod.name for pod in telemetry.pods}
    assert pod_names == {"payment-gateway-pod", "auth-service-pod", "redis-sentinel-pod"}


@pytest.mark.asyncio
async def test_get_cluster_telemetry_seeds_payment_gateway_anomaly() -> None:
    telemetry = await system_engine.get_cluster_telemetry()

    payment_pod = next(pod for pod in telemetry.pods if pod.name == "payment-gateway-pod")
    auth_pod = next(pod for pod in telemetry.pods if pod.name == "auth-service-pod")
    redis_pod = next(pod for pod in telemetry.pods if pod.name == "redis-sentinel-pod")

    assert payment_pod.anomaly is True
    assert payment_pod.memory_percent >= 80.0
    assert auth_pod.anomaly is False
    assert redis_pod.anomaly is False
    # While the anomaly is active, cluster-wide error rate and latency should be elevated.
    assert telemetry.error_rate_5xx > 10.0
    assert telemetry.p99_latency_ms > 200.0


@pytest.mark.asyncio
async def test_get_cluster_telemetry_ticks_are_cheap_and_repeatable() -> None:
    first = await system_engine.get_cluster_telemetry()
    second = await system_engine.get_cluster_telemetry()

    assert isinstance(first.rps, float)
    assert isinstance(second.rps, float)
    assert 800.0 <= second.rps <= 1500.0


@pytest.mark.asyncio
async def test_cluster_execute_unknown_pod_raises_process_not_found() -> None:
    with pytest.raises(ProcessNotFoundError):
        await system_engine.cluster_execute(IntentType.PROCESS_KILL, "does-not-exist-pod", dry_run=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent",
    [IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE, IntentType.ROLLBACK],
)
async def test_cluster_execute_dry_run_previews_without_mutation(intent) -> None:
    before = system_engine._cluster_simulator.pods["auth-service-pod"].restarts

    result = await system_engine.cluster_execute(intent, "auth-service-pod", dry_run=True)

    assert result.success is True
    assert result.dry_run is True
    assert result.affected_process_name == "auth-service-pod"
    assert "[DRY RUN]" in result.message
    assert "Execution permissions are not verified." in result.message
    assert system_engine._cluster_simulator.pods["auth-service-pod"].restarts == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent",
    [IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE, IntentType.ROLLBACK],
)
async def test_cluster_execute_runs_for_each_intent(intent) -> None:
    result = await system_engine.cluster_execute(intent, "redis-sentinel-pod", dry_run=False)

    assert result.success is True
    assert result.dry_run is False
    assert result.affected_process_name == "redis-sentinel-pod"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [IntentType.PROCESS_KILL, IntentType.ROLLBACK])
async def test_cluster_execute_recovers_payment_gateway_anomaly(intent) -> None:
    pod = system_engine._cluster_simulator.pods["payment-gateway-pod"]
    assert pod.anomaly is True

    result = await system_engine.cluster_execute(intent, "payment-gateway-pod", dry_run=False)

    assert result.success is True
    assert pod.anomaly is False
    assert pod.memory_percent <= 25.0
    assert "recovered" in result.message.lower() or "cleared" in result.message.lower()

    telemetry = await system_engine.get_cluster_telemetry()
    assert telemetry.error_rate_5xx < 10.0
    assert telemetry.p99_latency_ms < 300.0


@pytest.mark.asyncio
async def test_cluster_execute_network_isolate_does_not_clear_anomaly() -> None:
    pod = system_engine._cluster_simulator.pods["payment-gateway-pod"]
    assert pod.anomaly is True

    result = await system_engine.cluster_execute(IntentType.NETWORK_ISOLATE, "payment-gateway-pod", dry_run=False)

    assert result.success is True
    assert pod.anomaly is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["terminate_process", "isolate_network"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("stage", ["Process", "name", "memory_info"])
async def test_process_vanished_during_inspection_is_not_found(monkeypatch, sandbox_process, operation, dry_run, stage) -> None:
    fake, run = sandbox_process
    target = psutil if stage == "Process" else fake
    monkeypatch.setattr(target, stage, Mock(side_effect=psutil.NoSuchProcess(fake.pid)))

    with pytest.raises(ProcessNotFoundError):
        await getattr(system_engine, operation)(fake.pid, dry_run=dry_run)

    assert fake.terminated is False
    assert fake.killed is False
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation, stage",
    [("terminate_process", "terminate"), ("terminate_process", "wait"), ("terminate_process", "kill"), ("isolate_network", "net_connections")],
)
async def test_process_vanished_at_mutation_boundary_is_not_found(monkeypatch, sandbox_process, operation, stage) -> None:
    fake, run = sandbox_process
    if stage == "kill":
        monkeypatch.setattr(fake, "wait", Mock(side_effect=psutil.TimeoutExpired(3, pid=fake.pid)))
    monkeypatch.setattr(fake, stage, Mock(side_effect=psutil.NoSuchProcess(fake.pid)))

    with pytest.raises(ProcessNotFoundError):
        await getattr(system_engine, operation)(fake.pid)

    run.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_final_wait_timeout_returns_failed_result(monkeypatch, sandbox_process) -> None:
    fake, run = sandbox_process
    monkeypatch.setattr(fake, "wait", Mock(side_effect=psutil.TimeoutExpired(3, pid=fake.pid)))

    result = await system_engine.terminate_process(fake.pid)

    assert result.success is False
    assert "timed out" in result.message.lower()
    assert "could not be confirmed" in result.message.lower()
    assert fake.terminated is True
    assert fake.killed is True
    assert fake.wait.call_count == 2
    run.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_timeout_fallback_succeeds_only_after_exit(monkeypatch, sandbox_process) -> None:
    fake, run = sandbox_process
    monkeypatch.setattr(fake, "wait", Mock(side_effect=[psutil.TimeoutExpired(3, pid=fake.pid), None]))

    result = await system_engine.terminate_process(fake.pid)

    assert result.success is True
    assert fake.terminated is True
    assert fake.killed is True
    assert fake.wait.call_count == 2
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["terminate_process", "isolate_network"])
async def test_protected_process_preview_still_raises(monkeypatch, sandbox_process, operation) -> None:
    fake, run = sandbox_process
    monkeypatch.setattr(fake, "name", lambda: "explorer.exe")

    with pytest.raises(ProtectedProcessError):
        await getattr(system_engine, operation)(fake.pid, dry_run=True)

    assert fake.terminated is False
    assert fake.killed is False
    run.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(1, "netsh"), PermissionError("denied"), subprocess.TimeoutExpired("netsh", 5)],
)
def test_block_ip_propagates_firewall_failure(monkeypatch, sandbox_process, error) -> None:
    fake, run = sandbox_process
    monkeypatch.setattr(system_engine.sys, "platform", "win32")
    run.side_effect = error

    with pytest.raises(type(error)):
        system_engine._block_ip("10.0.0.9")

    run.assert_called_once()


@pytest.mark.asyncio
async def test_isolate_preview_inspects_connections_without_firewall_changes(monkeypatch, sandbox_process) -> None:
    fake, run = sandbox_process
    connections = Mock(return_value=[SimpleNamespace(raddr=SimpleNamespace(ip="10.0.0.9"))])
    monkeypatch.setattr(fake, "net_connections", connections)

    result = await system_engine.isolate_network(fake.pid, dry_run=True)

    assert result.success is True
    assert result.dry_run is True
    connections.assert_called_once_with(kind="inet")
    run.assert_not_called()
