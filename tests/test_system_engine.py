"""Unit tests for non-blocking psutil telemetry wrappers and sandboxed process handling.

All `psutil` calls are monkeypatched so these tests never touch the real host's processes.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import psutil
import pytest

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
