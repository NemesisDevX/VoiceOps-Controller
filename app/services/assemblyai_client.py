"""Async wrapper around AssemblyAI's v3 Realtime Streaming API.

Bridges raw PCM16 audio frames received over our own `/ws/voice-stream` WebSocket to an
`AsyncRealTimeTranscriber` session, invoking caller-supplied callbacks on partial and
finalized (end-of-turn) transcripts.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from assemblyai.streaming.v3 import (
    BeginEvent,
    RealTimeError,
    RealTimeEvents,
    RealTimeParameters,
    RealTimeTranscriberOptions,
    TerminationEvent,
    TurnEvent,
)
from assemblyai.streaming.v3.async_client import AsyncRealTimeTranscriber

logger = logging.getLogger(__name__)

TurnCallback = Callable[[str, bool], Awaitable[None]]
"""Invoked as `callback(transcript_text, end_of_turn)` for every turn update."""


class AssemblyAIStreamingSession:
    """Manages a single AssemblyAI realtime transcription session for one voice WebSocket."""

    def __init__(
        self,
        api_key: str,
        on_turn: TurnCallback,
        sample_rate: int = 16_000,
    ) -> None:
        self._api_key = api_key
        self._on_turn = on_turn
        self._sample_rate = sample_rate
        self._transcriber = AsyncRealTimeTranscriber(
            RealTimeTranscriberOptions(api_key=self._api_key)
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        self._transcriber.on(RealTimeEvents.Begin, self._handle_begin)
        self._transcriber.on(RealTimeEvents.Turn, self._handle_turn)
        self._transcriber.on(RealTimeEvents.Termination, self._handle_termination)
        self._transcriber.on(RealTimeEvents.Error, self._handle_error)

    async def _handle_begin(self, _client: AsyncRealTimeTranscriber, event: BeginEvent) -> None:
        logger.info("assemblyai session started", extra={"session_id": event.id})

    async def _handle_turn(self, _client: AsyncRealTimeTranscriber, event: TurnEvent) -> None:
        if event.transcript:
            await self._on_turn(event.transcript, event.end_of_turn)

    async def _handle_termination(self, _client: AsyncRealTimeTranscriber, event: TerminationEvent) -> None:
        logger.info(
            "assemblyai session terminated",
            extra={"audio_duration_seconds": event.audio_duration_seconds},
        )

    async def _handle_error(self, _client: AsyncRealTimeTranscriber, error: RealTimeError) -> None:
        logger.error("assemblyai streaming error", extra={"error_code": error.code, "error": str(error)})

    async def connect(self) -> None:
        """Open the realtime WebSocket session against AssemblyAI."""
        await self._transcriber.connect(
            RealTimeParameters(sample_rate=self._sample_rate, format_turns=True)
        )

    async def stream(self, audio_frames: AsyncIterator[bytes]) -> None:
        """Forward an async stream of raw PCM16 audio frames to the active session."""
        await self._transcriber.stream(audio_frames)

    async def close(self) -> None:
        """Gracefully terminate the realtime session."""
        await self._transcriber.disconnect(terminate=True)
