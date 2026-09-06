"""Shared pytest fixtures for the VoiceOps Controller test suite."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ASSEMBLYAI_API_KEY", "test-key-not-real")

from app.core.security import confirmation_registry
from app.main import create_app
from app.schemas.telemetry import ProcessInfo


@pytest.fixture
def sample_processes() -> list[ProcessInfo]:
    """A deterministic, descending-by-memory list of fake process snapshots."""
    return [
        ProcessInfo(pid=4242, name="runaway-worker.exe", cpu_percent=87.5, memory_percent=41.2, memory_rss_mb=4096.0, status="running"),
        ProcessInfo(pid=1010, name="cache-node", cpu_percent=12.0, memory_percent=18.4, memory_rss_mb=1024.0, status="running"),
        ProcessInfo(pid=2020, name="log-shipper", cpu_percent=3.1, memory_percent=4.0, memory_rss_mb=256.0, status="sleeping"),
    ]


@pytest.fixture(autouse=True)
def _clear_confirmation_registry():
    """Ensure pending confirmations never leak between tests."""
    yield
    with confirmation_registry._lock:
        confirmation_registry._pending.clear()


@pytest.fixture
def client() -> TestClient:
    """A `TestClient` bound to a fresh app instance with lifespan startup/shutdown."""
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
