"""FastAPI application entrypoint: lifespan management and routing setup."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.telemetry_stream import TelemetryBroadcaster

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    broadcaster = TelemetryBroadcaster(
        interval_ms=settings.TELEMETRY_INTERVAL_MS,
        top_process_limit=settings.TOP_PROCESS_LIMIT,
    )
    app.state.telemetry_broadcaster = broadcaster
    broadcaster.start()
    logger.info("VoiceOps Controller starting up", extra={"host": settings.HOST, "port": settings.PORT})

    try:
        yield
    finally:
        await broadcaster.stop()
        logger.info("VoiceOps Controller shut down")


def create_app() -> FastAPI:
    """Application factory: assembles the FastAPI app with routing and lifespan hooks."""
    app = FastAPI(
        title="VoiceOps Controller",
        description="Real-time voice-driven infrastructure incident mitigation backend.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


app = create_app()
