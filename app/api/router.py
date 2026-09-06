"""Top-level API router aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import commands_router, health_router
from app.api.v1.websockets import router as websockets_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(commands_router, prefix="/api/v1")
api_router.include_router(websockets_router)
