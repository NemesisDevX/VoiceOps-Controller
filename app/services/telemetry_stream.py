"""Background broadcaster that periodically pushes system telemetry to WebSocket subscribers."""

from __future__ import annotations

import asyncio
import logging

from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.schemas.telemetry import ClusterTelemetry, SystemTelemetry, TelemetryMode
from app.services import system_engine

logger = logging.getLogger(__name__)


class TelemetryConnectionManager:
    """Tracks active `/ws/telemetry` subscribers and fans out telemetry snapshots to them."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, telemetry: SystemTelemetry | dict[str, Any]) -> None:
        payload = telemetry.model_dump(mode="json") if isinstance(telemetry, SystemTelemetry) else telemetry
        async with self._lock:
            targets = list(self._connections)

        for websocket in targets:
            if websocket.client_state != WebSocketState.CONNECTED:
                await self.disconnect(websocket)
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                logger.warning("dropping unresponsive telemetry subscriber", exc_info=True)
                await self.disconnect(websocket)


class TelemetryBroadcaster:
    """Owns the periodic background task that samples and broadcasts system telemetry."""

    def __init__(self, interval_ms: int = 1000, top_process_limit: int = 5) -> None:
        self._interval_seconds = interval_ms / 1000
        self._top_process_limit = top_process_limit
        self.connections = TelemetryConnectionManager()
        self._task: asyncio.Task[None] | None = None
        self._mode_lock = asyncio.Lock()
        self._mode = TelemetryMode.HOST_LOCAL

    async def get_mode(self) -> TelemetryMode:
        """Return the currently active telemetry mode."""
        async with self._mode_lock:
            return self._mode

    async def set_mode(self, mode: TelemetryMode) -> None:
        """Switch which telemetry source subsequent broadcast cycles sample from."""
        async with self._mode_lock:
            self._mode = mode

    async def _build_payload(self) -> dict[str, Any]:
        mode = await self.get_mode()
        if mode is TelemetryMode.K8S_CLUSTER:
            cluster_telemetry: ClusterTelemetry = await system_engine.get_cluster_telemetry()
            payload = cluster_telemetry.model_dump(mode="json")
        else:
            telemetry = await system_engine.get_system_telemetry(self._top_process_limit)
            payload = telemetry.model_dump(mode="json")
        payload["mode"] = mode.value
        return payload

    async def _run(self) -> None:
        while True:
            try:
                payload = await self._build_payload()
                await self.connections.broadcast(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("telemetry broadcast cycle failed")
            await asyncio.sleep(self._interval_seconds)

    def start(self) -> None:
        """Start the periodic broadcast loop as a background asyncio task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            logger.info("telemetry broadcaster started", extra={"interval_seconds": self._interval_seconds})

    async def stop(self) -> None:
        """Cancel the background broadcast loop and await its shutdown."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("telemetry broadcaster stopped")
