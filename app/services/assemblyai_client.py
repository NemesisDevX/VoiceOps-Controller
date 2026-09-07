"""Async wrapper around AssemblyAI's v3 Realtime Streaming API.

Bridges raw PCM16 audio frames received over our own `/ws/voice-stream` WebSocket to an
`AsyncRealTimeTranscriber` session, invoking caller-supplied callbacks on partial and
finalized (end-of-turn) transcripts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

logger = logging.getLogger(__name__)
_transport_logger = logging.Logger("voiceops.provider.transport", level=logging.CRITICAL + 1)

TurnCallback = Callable[[str, bool], Awaitable[None]]
"""Invoked as `callback(transcript_text, end_of_turn)` for every turn update."""
ErrorCallback = Callable[[str, bool], Awaitable[None]]

PROVIDER_URL = "wss://streaming.assemblyai.com/v3/ws"
SPEECH_MODEL = "universal-streaming-english"
PROVIDER_QUEUE_MAXSIZE = 8
PROVIDER_WRITE_LIMIT = 32768
PROVIDER_MESSAGE_MAXSIZE = 262144
PROVIDER_BEGIN_TIMEOUT = 8.0
PROVIDER_SEND_TIMEOUT = 2.0
PROVIDER_CLOSE_TIMEOUT = 3.0
PROVIDER_ABORT_TIMEOUT = 1.0


class AssemblyAIStreamingError(Exception):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _safe_provider_error(error: Exception | None = None, *, code: int | None = None) -> AssemblyAIStreamingError:
    if isinstance(error, AssemblyAIStreamingError):
        return error
    if isinstance(error, InvalidStatus):
        code = error.response.status_code
    elif isinstance(error, ConnectionClosed) and error.rcvd is not None:
        code = error.rcvd.code
    if code in (401, 402, 403, 1008, 4001, 4002, 4003):
        return AssemblyAIStreamingError("Speech provider credentials or account access need attention.", False)
    if code in (400, 410, 3006, 3007, 4000, 4032, 4033, 4034, 4100, 4101):
        return AssemblyAIStreamingError("Speech provider rejected the audio stream configuration.", False)
    return AssemblyAIStreamingError("Speech provider is unavailable. Please try again.", True)


class AssemblyAIStreamingSession:
    """Manages a single AssemblyAI realtime transcription session for one voice WebSocket."""

    def __init__(
        self,
        api_key: str,
        on_turn: TurnCallback,
        sample_rate: int = 16_000,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self._api_key = api_key
        self._on_turn = on_turn
        self._on_error = on_error
        self._sample_rate = sample_rate
        self._last_finalized_turn = -1
        self._error: AssemblyAIStreamingError | None = None
        self._connection: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._upload_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._begun = False
        self._terminated = False
        self._closing = False
        self._closed = False
        self._connect_started = False

    async def _fail(self, error: AssemblyAIStreamingError) -> None:
        if self._error is not None:
            return
        self._error = error
        if self._on_error is not None:
            await self._on_error(str(error), error.retryable)

    async def _receive_message(self) -> dict:
        raw = await self._connection.recv()
        if not isinstance(raw, str):
            raise AssemblyAIStreamingError("Speech provider sent an invalid response. Please try again.")
        event = json.loads(raw)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise AssemblyAIStreamingError("Speech provider sent an invalid response. Please try again.")
        return event

    async def _handle_turn(self, event: dict) -> None:
        order = event.get("turn_order")
        text = event.get("transcript")
        final = event.get("end_of_turn")
        if type(order) is not int or order < 0 or not isinstance(text, str) or type(final) is not bool:
            raise AssemblyAIStreamingError("Speech provider sent an invalid transcript. Please try again.")
        if self._error is not None or order <= self._last_finalized_turn:
            return
        if text:
            if final:
                self._last_finalized_turn = order
            try:
                await self._on_turn(text, final)
            except Exception:
                await self._fail(AssemblyAIStreamingError("Voice command processing failed. Please try again."))

    async def _read_events(self) -> None:
        try:
            while True:
                event = await self._receive_message()
                event_type = event["type"]
                if event_type == "Turn":
                    await self._handle_turn(event)
                    if self._error is not None:
                        return
                elif event_type == "Error":
                    raise _safe_provider_error(code=event.get("error_code"))
                elif event_type == "Termination":
                    self._terminated = True
                    logger.info("assemblyai session terminated")
                    if not self._closing:
                        raise AssemblyAIStreamingError("Speech provider ended the session. Please try again.")
                    return
                elif event_type == "Begin":
                    raise AssemblyAIStreamingError("Speech provider sent an invalid response. Please try again.")
        except Exception as error:
            logger.warning("assemblyai streaming error")
            await self._fail(_safe_provider_error(error))

    async def connect(self) -> None:
        """Open the realtime WebSocket session against AssemblyAI."""
        try:
            if self._connect_started or self._closed:
                raise AssemblyAIStreamingError("Voice sessions cannot be reused. Please reconnect.", False)
            self._connect_started = True
            if type(self._sample_rate) is not int or self._sample_rate <= 0:
                raise AssemblyAIStreamingError("Audio sample rate must be a positive integer.", False)
            query = urlencode({
                "sample_rate": self._sample_rate, "encoding": "pcm_s16le",
                "speech_model": SPEECH_MODEL, "format_turns": "true",
            })
            async with asyncio.timeout(PROVIDER_BEGIN_TIMEOUT):
                self._connection = await websocket_connect(
                    f"{PROVIDER_URL}?{query}", additional_headers={"Authorization": self._api_key},
                    open_timeout=5.0, close_timeout=PROVIDER_ABORT_TIMEOUT,
                    max_queue=PROVIDER_QUEUE_MAXSIZE, write_limit=PROVIDER_WRITE_LIMIT,
                    max_size=PROVIDER_MESSAGE_MAXSIZE, logger=_transport_logger,
                )
                event = await self._receive_message()
                if event["type"] == "Error":
                    raise _safe_provider_error(code=event.get("error_code"))
                if (
                    event["type"] != "Begin" or not isinstance(event.get("id"), str) or not event["id"]
                    or type(event.get("expires_at")) not in (int, float)
                ):
                    raise AssemblyAIStreamingError("Speech provider did not confirm the session. Please try again.")
                configuration = event.get("configuration")
                if configuration is not None and (
                    not isinstance(configuration, dict) or configuration.get("model") != SPEECH_MODEL
                ):
                    raise AssemblyAIStreamingError("Speech provider did not confirm the requested model. Please try again.")
                self._begun = True
                logger.info("assemblyai session started")
                self._reader_task = asyncio.create_task(self._read_events(), name="voice.provider.receive")
        except Exception as error:
            await self._fail(_safe_provider_error(error))
            raise self._error from None

    async def _send_audio(self, audio_frames: AsyncIterator[bytes]) -> None:
        async for chunk in audio_frames:
            if (
                not isinstance(chunk, bytes) or len(chunk) % 2
                or not self._sample_rate / 10 <= len(chunk) <= self._sample_rate * 2
            ):
                raise AssemblyAIStreamingError("Audio frames must contain 50 to 1000 ms of PCM16 audio.", False)
            async with asyncio.timeout(PROVIDER_SEND_TIMEOUT):
                async with self._send_lock:
                    await self._connection.send(chunk)

    async def stream(self, audio_frames: AsyncIterator[bytes]) -> None:
        """Forward an async stream of raw PCM16 audio frames to the active session."""
        if not self._begun or self._closing or self._reader_task is None or self._upload_task is not None:
            raise AssemblyAIStreamingError("Voice session is not ready for audio.", False)
        self._upload_task = asyncio.create_task(self._send_audio(audio_frames), name="voice.provider.upload")
        completed = False
        try:
            done, _ = await asyncio.wait({self._upload_task, self._reader_task}, return_when=asyncio.FIRST_COMPLETED)
            if self._error is not None:
                raise self._error
            if self._reader_task in done:
                await self._reader_task
                raise AssemblyAIStreamingError("Speech provider disconnected. Please try again.")
            await self._upload_task
            completed = True
        except Exception as error:
            await self._fail(_safe_provider_error(error))
            raise self._error from None
        finally:
            tasks = [self._upload_task]
            if not completed:
                tasks.append(self._reader_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), PROVIDER_ABORT_TIMEOUT)

    async def close(self) -> None:
        """Gracefully terminate the realtime session."""
        if self._closed:
            return
        self._closing = True
        try:
            try:
                if (
                    self._begun and self._error is None
                    and self._reader_task is not None and not self._reader_task.done()
                ):
                    async with asyncio.timeout(PROVIDER_CLOSE_TIMEOUT):
                        if self._upload_task is not None and not self._upload_task.done():
                            await asyncio.shield(self._upload_task)
                        async with self._send_lock:
                            await self._connection.send(json.dumps({"type": "Terminate"}))
                        await asyncio.shield(self._reader_task)
                        if not self._terminated:
                            raise self._error or AssemblyAIStreamingError("Speech provider did not finish the session. Please try again.")
            except Exception as error:
                await self._fail(_safe_provider_error(error))
        finally:
            try:
                tasks = [task for task in (self._reader_task, self._upload_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                try:
                    if tasks:
                        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), PROVIDER_ABORT_TIMEOUT)
                finally:
                    if self._connection is not None:
                        await asyncio.wait_for(self._connection.close(), PROVIDER_ABORT_TIMEOUT + 1)
            except Exception as error:
                logger.warning("assemblyai disconnect failed")
                await self._fail(_safe_provider_error(error))
            finally:
                self._closed = True
        if self._error is not None:
            raise self._error from None
