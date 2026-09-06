"""Test client that streams synthetic PCM16 audio into `/ws/voice-stream` and prints responses.

Usage:
    python scripts/mock_audio_client.py --url ws://localhost:8000/ws/voice-stream --seconds 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct

import websockets

SAMPLE_RATE = 16_000
FRAME_DURATION_SECONDS = 0.1
FRAME_SIZE_SAMPLES = int(SAMPLE_RATE * FRAME_DURATION_SECONDS)


def _generate_tone_frame(frequency_hz: float, phase: float) -> tuple[bytes, float]:
    """Generate one PCM16 mono audio frame containing a sine tone, returning (bytes, next_phase)."""
    samples = bytearray()
    for i in range(FRAME_SIZE_SAMPLES):
        phase += 2 * math.pi * frequency_hz / SAMPLE_RATE
        value = int(3000 * math.sin(phase))
        samples += struct.pack("<h", value)
    return bytes(samples), phase


async def _receive_loop(connection: websockets.WebSocketClientProtocol) -> None:
    async for message in connection:
        try:
            payload = json.loads(message)
            print(f"[server] {json.dumps(payload, indent=2)}")
        except json.JSONDecodeError:
            print(f"[server:raw] {message!r}")


async def run(url: str, seconds: float) -> None:
    async with websockets.connect(url) as connection:
        receiver = asyncio.create_task(_receive_loop(connection))
        phase = 0.0
        frame_count = int(seconds / FRAME_DURATION_SECONDS)
        for _ in range(frame_count):
            frame, phase = _generate_tone_frame(frequency_hz=440.0, phase=phase)
            await connection.send(frame)
            await asyncio.sleep(FRAME_DURATION_SECONDS)

        await connection.send("stop")
        await asyncio.sleep(1.0)
        receiver.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:8000/ws/voice-stream")
    parser.add_argument("--seconds", type=float, default=5.0)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.seconds))


if __name__ == "__main__":
    main()
