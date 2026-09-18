"""Latest-pipeline-waterfall tracker for live HUD latency rendering.

Each resolved incident records its t0→t3 latency waterfall on the `IncidentRecord`; this
module-level singleton retains the most recent one so the telemetry broadcaster can attach
it to every `/ws/telemetry` frame and the HUD can render a continuously-updated waterfall.
"""

from __future__ import annotations

import threading


class WaterfallTracker:
    """Thread-safe holder for the most recently completed latency waterfall."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, float] | None = None

    def record(self, waterfall: dict[str, float]) -> None:
        """Store `waterfall` as the newest completed pipeline timing breakdown."""
        with self._lock:
            self._latest = dict(waterfall)

    def latest(self) -> dict[str, float] | None:
        """Return the newest completed waterfall, or None if no incident has resolved yet."""
        with self._lock:
            return dict(self._latest) if self._latest is not None else None


waterfall_tracker = WaterfallTracker()
