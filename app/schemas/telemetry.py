"""System metrics models: CPU, memory, disk, socket, and per-process telemetry."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class ProcessInfo(BaseModel):
    """A single point-in-time snapshot of one operating-system process."""

    pid: int = Field(..., description="Operating system process identifier.")
    name: str = Field(..., description="Executable/process name as reported by the OS.")
    cpu_percent: float = Field(..., ge=0.0, description="CPU utilization percentage since last sample.")
    memory_percent: float = Field(..., ge=0.0, description="Resident memory usage as a percentage of total RAM.")
    memory_rss_mb: float = Field(..., ge=0.0, description="Resident set size in megabytes.")
    status: str = Field(..., description="Process run state (e.g. running, sleeping, zombie).")


class SystemTelemetry(BaseModel):
    """Aggregate system health snapshot broadcast to the telemetry HUD."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    cpu_percent: float = Field(..., ge=0.0, le=100.0, description="System-wide CPU utilization percentage.")
    memory_percent: float = Field(..., ge=0.0, le=100.0, description="System-wide memory utilization percentage.")
    disk_percent: float = Field(..., ge=0.0, le=100.0, description="Utilization percentage of the root disk partition.")
    active_sockets: int = Field(..., ge=0, description="Count of active network connections/sockets.")
    top_memory_processes: list[ProcessInfo] = Field(
        default_factory=list, description="Highest memory-consuming processes at capture time."
    )
    top_cpu_processes: list[ProcessInfo] = Field(
        default_factory=list, description="Highest CPU-consuming processes at capture time."
    )
