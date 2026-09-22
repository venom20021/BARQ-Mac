"""Phase 1: measure LiveKit Agents' voice latency with scripted audio.

Feeds a WAV into AgentSession's own input stream — no mic, no SFU, no server —
so the run is repeatable and can be driven by an agent rather than by talking.

The input class mirrors `ConsoleAudioInput` (livekit/agents/cli/_legacy.py): an
`io.AudioInput` fed by an aio.Chan, which RoomIO would normally wire up. Here we
assign it ourselves (room_io.py does `session.input.audio = audio_input`).

Measures the two numbers comparable to BARQ:
  connect   session.start() until the session is live
  response  last input audio frame -> first agent "speaking" state
            (BARQ's equivalent is `voice_response_ms` in GET /voice/latency)

Usage:
    .venv/bin/python measure.py [wav] [repeats]
"""

from __future__ import annotations

import asyncio
import os
import re
import statistics
import sys
import time
import wave
from pathlib import Path

BARQ_ENV = Path.home() / "Project/BARQ-Mac/python/.env"

# Deliberately BARQ's exact model: holding the model constant is what makes this
# a test of the framework/transport rather than of the model.
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")


def load_api_key() -> str | None:
    """Reuse BARQ's Gemini key so both stacks hit the same account."""
    for name in KEY_NAMES:
        val = os.getenv(name)
        if val:
            return val
    if BARQ_ENV.exists():
        for line in BARQ_ENV.read_text().splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m and m.group(1) in KEY_NAMES:
                return m.group(2).strip().strip('"').strip("'")
    return None


def read_wav(path: str) -> tuple[bytes, int, int, int]:
    with wave.open(path) as w:
        return (
            w.readframes(w.getnframes()),
            w.getframerate(),
            w.getnchannels(),
            w.getsampwidth(),
        )


def make_wav_input(lkio, aio, rtc):
    """A mic-free AudioInput: we push frames in, the session reads them out."""

    class WavAudioInput(lkio.AudioInput):
        def __init__(self) -> None:
            super().__init__(label="WavFile")
            self._ch: aio.Chan = aio.Chan()
            self._attached = True

        def push_frame(self, frame) -> None:
            if self._attached:
                self._ch.send_nowait(frame)

        async def __anext__(self):
            return await self._ch.__anext__()

        def on_attached(self) -> None:
            self._attached = True

        def on_detached(self) -> None:
            self._attached = False

    return WavAudioInput()


async def main() -> int:
    wav_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/barq_utt.wav"
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    api_key = load_api_key()
    if not api_key:
        print("FATAL: no Gemini API key (checked env + BARQ python/.env)")
        return 2

    pcm, rate, ch, sw = read_wav(wav_path)
    print(f"input: {wav_path} — {rate}Hz {ch}ch {sw * 8}bit, "
          f"{len(pcm) / (rate * ch * sw):.2f}s speech")

    from livekit import rtc
    from livekit.agents import Agent, AgentSession
    from livekit.agents import io as lkio
    from livekit.plugins import google, silero

    try:
        from livekit.agents.utils import aio  # type: ignore
    except ImportError:
        from livekit.agents import utils
        aio = utils.aio  # type: ignore

    frame_bytes = max(2, int(rate * 0.02) * ch * sw)  # 20 ms
    results: list[dict] = []

    for attempt in range(1, repeats + 1):
        print(f"\n--- cycle {attempt}/{repeats} ---")
        session = AgentSession(
            llm=google.realtime.RealtimeModel(model=MODEL, voice="Puck", api_key=api_key),
            vad=silero.VAD.load(),
        )
        wav_in = make_wav_input(lkio, aio, rtc)
        session.input.audio = wav_in

        speaking: list[float] = []

        def on_state(ev) -> None:
            state = str(getattr(ev, "new_state", None) or getattr(ev, "state", "")).lower()
            if "speaking" in state:
                speaking.append(time.perf_counter())

        session.on("agent_state_changed", on_state)

        t0 = time.perf_counter()
        await session.start(
            agent=Agent(instructions="You are a terse assistant. Answer in one short sentence.")
        )
        connect_ms = (time.perf_counter() - t0) * 1000
        print(f"  connect (session.start): {connect_ms:.0f} ms")

        # Push the utterance in real time — faster-than-real-time audio would
        # confuse end-of-utterance detection.
        last_push = time.perf_counter()
        for i in range(0, len(pcm), frame_bytes):
            chunk = pcm[i : i + frame_bytes]
            if len(chunk) < 2:
                break
            wav_in.push_frame(
                rtc.AudioFrame(
                    data=chunk,
                    sample_rate=rate,
                    num_channels=ch,
                    samples_per_channel=len(chunk) // (ch * sw),
                )
            )
            last_push = time.perf_counter()
            await asyncio.sleep(0.02)

        # Trailing silence so end-of-speech is unambiguous, then wait.
        silence = b"\x00" * frame_bytes
        deadline = time.perf_counter() + 8.0
        while time.perf_counter() < deadline and not speaking:
            wav_in.push_frame(
                rtc.AudioFrame(
                    data=silence,
                    sample_rate=rate,
                    num_channels=ch,
                    samples_per_channel=len(silence) // (ch * sw),
                )
            )
            await asyncio.sleep(0.02)

        response_ms = (speaking[0] - last_push) * 1000 if speaking else None
        results.append({"connect_ms": connect_ms, "response_ms": response_ms})
        shown = "n/a (agent never reported speaking)" if response_ms is None else f"{response_ms:.0f} ms"
        print(f"  response (last input -> agent speaking): {shown}")

        await session.aclose()

    print("\n=== LiveKit Agents — scripted audio (BARQ's model) ===")
    conn = [r["connect_ms"] for r in results]
    resp = [r["response_ms"] for r in results if r["response_ms"] is not None]
    print(f"  connect  n={len(conn)}  median {statistics.median(conn):.0f} ms")
    if resp:
        print(f"  response n={len(resp)}  median {statistics.median(resp):.0f} ms  "
              f"min {min(resp):.0f}  max {max(resp):.0f}")
    else:
        print("  response: no samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
