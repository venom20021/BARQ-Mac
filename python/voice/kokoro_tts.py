"""
KokoroTTSEngine — offline neural TTS using pykokoro (Kokoro-82M model).

Lazy-loads the model on first use, then compiles the PyTorch JIT graph
so subsequent calls are near-instant.  Produces float32 PCM audio at
24 kHz compatible with BARQ's ring buffer playback system.

All heavy inference runs in ``asyncio.to_thread()`` so the main event
loop is never blocked.

Usage:
    engine = KokoroTTSEngine(voice="af_heart", speed=1.0)
    pcm, sample_rate = await engine.synthesize("Hello world")
    engine.cancel()          # stop any in-progress synthesis
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import numpy as np


# Suppress TensorFlow import in transformers (saves ~4s startup)
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Kokoro voice prefix → lang_code mapping
_LANG_CODES = {
    "a": "a",   # American English  (af_*, am_*)
    "b": "b",   # British English   (bf_*, bm_*)
    "j": "j",   # Japanese          (jf_*, jm_*)
    "z": "z",   # Mandarin Chinese  (zf_*, zm_*)
    "s": "s",   # Spanish           (sf_*, sm_*)
    "f": "f",   # French            (ff_*, fm_*)
    "h": "h",   # Hindi             (hf_*, hm_*)
    "i": "i",   # Italian           (if_*, im_*)
    "p": "p",   # Brazilian Portuguese
    "r": "r",   # Russian           (rf_*, rm_*)
    "e": "e",   # German            (ef_*, em_*)
}

# Known Kokoro voices available in pykokoro
KNOWN_VOICES: list[dict[str, str]] = [
    # American English Female
    {"id": "af_heart",   "name": "Heart",    "locale": "en-US", "gender": "female"},
    {"id": "af_bella",   "name": "Bella",    "locale": "en-US", "gender": "female"},
    {"id": "af_nicole",  "name": "Nicole",   "locale": "en-US", "gender": "female"},
    {"id": "af_sarah",   "name": "Sarah",    "locale": "en-US", "gender": "female"},
    {"id": "af_nova",    "name": "Nova",     "locale": "en-US", "gender": "female"},
    {"id": "af_sky",     "name": "Sky",      "locale": "en-US", "gender": "female"},
    {"id": "af_alloy",   "name": "Alloy",    "locale": "en-US", "gender": "female"},
    {"id": "af_aoede",   "name": "Aoede",    "locale": "en-US", "gender": "female"},
    # American English Male
    {"id": "am_michael", "name": "Michael",  "locale": "en-US", "gender": "male"},
    {"id": "am_adam",    "name": "Adam",     "locale": "en-US", "gender": "male"},
    {"id": "am_echo",    "name": "Echo",     "locale": "en-US", "gender": "male"},
    {"id": "am_puck",    "name": "Puck",     "locale": "en-US", "gender": "male"},
    {"id": "am_fenrir",  "name": "Fenrir",   "locale": "en-US", "gender": "male"},
    # British English Female
    {"id": "bf_emma",    "name": "Emma",     "locale": "en-GB", "gender": "female"},
    {"id": "bf_isabella","name": "Isabella", "locale": "en-GB", "gender": "female"},
    {"id": "bf_alice",   "name": "Alice",    "locale": "en-GB", "gender": "female"},
    {"id": "bf_lily",    "name": "Lily",     "locale": "en-GB", "gender": "female"},
    # British English Male
    {"id": "bm_george",  "name": "George",   "locale": "en-GB", "gender": "male"},
    {"id": "bm_fable",   "name": "Fable",    "locale": "en-GB", "gender": "male"},
    {"id": "bm_lewis",   "name": "Lewis",    "locale": "en-GB", "gender": "male"},
    {"id": "bm_daniel",  "name": "Daniel",   "locale": "en-GB", "gender": "male"},
    # Hindi
    {"id": "hf_alpha",   "name": "Alpha",    "locale": "hi-IN", "gender": "female"},
    {"id": "hm_omega",   "name": "Omega",    "locale": "hi-IN", "gender": "male"},
]


class KokoroTTSEngine:
    """Fully offline Kokoro neural TTS engine.

    The model (~330 MB) is downloaded from HuggingFace on first use,
    then cached locally.  GPU acceleration is used if CUDA is available.

    All inference runs via ``asyncio.to_thread()`` to avoid blocking
    the main event loop.
    """

    def __init__(
        self,
        voice: str = "af_heart",
        speed: float = 1.0,
        use_gpu: bool = True,
    ):
        self.voice = voice
        self.speed = speed
        self.use_gpu = use_gpu

        self._pipeline = None   # KokoroPipeline (lazy-loaded)
        self._lock = asyncio.Lock()
        self._cancel_flag = False
        self._loaded = False

    @property
    def _lang_code(self) -> str:
        """Derive the language code from the voice prefix."""
        prefix = self.voice[0].lower() if self.voice else "a"
        return _LANG_CODES.get(prefix, "a")

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> bool:
        """Load the Kokoro pipeline (lazy, safe to call multiple times).

        Downloads model from HuggingFace on first use (∼330 MB).

        Returns:
            True if loaded successfully.
        """
        if self._loaded and self._pipeline is not None:
            return True

        async with self._lock:
            if self._loaded and self._pipeline is not None:
                return True  # Double-checked locking

            print(f"[KokoroTTS] Loading Kokoro pipeline (lang='{self._lang_code}', "
                  f"voice='{self.voice}')...")

            try:
                loop = asyncio.get_event_loop()
                self._pipeline = await loop.run_in_executor(
                    None,
                    self._build_pipeline_sync,
                )  # type: ignore[func-returns-value]

                # Warmup: compile PyTorch JIT graph so first real call is instant
                print("[KokoroTTS] Compiling (first-time warmup)...")
                await loop.run_in_executor(
                    None,
                    self._warmup_sync,
                )

                self._loaded = True
                print("[KokoroTTS] Ready — offline neural TTS active")
                return True

            except Exception as e:
                print(f"[KokoroTTS] Failed to load pipeline: {e}")
                self._pipeline = None
                return False

    def _build_pipeline_sync(self) -> Any:
        """Build the Kokoro pipeline (blocking, run in thread)."""
        from pykokoro import build_pipeline

        # Build pipeline with lang_code derived from voice
        overrides = {
            "voice": self._lang_code,
        }

        pipeline = build_pipeline(overrides=overrides)
        return pipeline

    def _warmup_sync(self):
        """Run a short warmup inference to compile the JIT graph."""
        if self._pipeline is None:
            return
        try:
            result = self._pipeline.run(
                "Hello.",
                voice=self.voice,
                speed=self.speed,
            )
            # Touch the audio to force tensor evaluation
            _ = result.audio.shape
        except Exception as e:
            print(f"[KokoroTTS] Warmup warning (non-fatal): {e}")

    async def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesize text to speech.

        Runs Kokoro inference in a thread to avoid blocking the event loop.

        Args:
            text: Text to synthesize.

        Returns:
            Tuple of (float32 PCM audio array, sample_rate).
            sample_rate is always 24000 Hz.
            Empty array if cancelled or failed.
        """
        if self._cancel_flag:
            self._cancel_flag = False
            return np.array([], dtype=np.float32), 24000

        if not self._loaded or self._pipeline is None:
            ok = await self.load()
            if not ok:
                print("[KokoroTTS] Pipeline not available — returning silence")
                return np.array([], dtype=np.float32), 24000

        if self._cancel_flag:
            self._cancel_flag = False
            return np.array([], dtype=np.float32), 24000

        try:
            loop = asyncio.get_event_loop()

            result = await loop.run_in_executor(
                None,
                self._synthesize_sync,
                text,
            )

            if result is None or self._cancel_flag:
                self._cancel_flag = False
                return np.array([], dtype=np.float32), 24000

            audio, sample_rate = result
            return audio, sample_rate

        except Exception as e:
            print(f"[KokoroTTS] Synthesis error: {e}")
            return np.array([], dtype=np.float32), 24000

    def _synthesize_sync(self, text: str) -> Optional[tuple]:
        """Run Kokoro inference synchronously (in a thread)."""
        if self._pipeline is None:
            return None

        try:
            result = self._pipeline.run(
                text,
                voice=self.voice,
                speed=self.speed,
            )

            if result is None:
                return None

            audio = result.audio
            sample_rate = result.sample_rate

            # Ensure float32 mono
            if audio.dtype != np.float32:
                audio = audio.astype(np.float32)

            if audio.ndim > 1:
                # Mix down to mono if stereo
                audio = np.mean(audio, axis=-1)

            # Compress long silence pauses (punctuation pauses can be 1-2s)
            audio = self._compress_silence(audio, sample_rate)

            return audio, sample_rate

        except Exception as e:
            print(f"[KokoroTTS] Sync synthesis error: {e}")
            return None

    def _compress_silence(
        self,
        arr: np.ndarray,
        sample_rate: int = 24000,
        max_silence_ms: int = 500,
        threshold: float = 0.003,
    ) -> np.ndarray:
        """Shorten long punctuation pauses while preserving natural prosody.

        Kokoro sometimes produces 1-2 second pauses at punctuation boundaries.
        This caps them at ``max_silence_ms`` for a more natural pace.
        """
        max_samp = int(max_silence_ms * sample_rate / 1000)
        frame_len = 240  # ~10 ms at 24 kHz
        out: list[np.ndarray] = []
        silent_acc = 0

        for i in range(0, len(arr), frame_len):
            chunk = arr[i: i + frame_len]
            if np.sqrt(np.mean(chunk ** 2) + 1e-12) < threshold:
                silent_acc += len(chunk)
                if silent_acc <= max_samp:
                    out.append(chunk)
            else:
                silent_acc = 0
                out.append(chunk)

        return np.concatenate(out) if out else arr

    def cancel(self):
        """Signal cancellation for the current or next synthesize() call."""
        self._cancel_flag = True

    async def close(self):
        """Release resources.  The pipeline can be re-loaded later."""
        async with self._lock:
            if self._pipeline is not None:
                try:
                    self._pipeline.close()
                except Exception:
                    pass
                self._pipeline = None
            self._loaded = False

    @staticmethod
    def list_voices() -> list[dict[str, str]]:
        """Return metadata about all known Kokoro voices."""
        return KNOWN_VOICES


# ── Singleton factory (lazy) ──────────────────────────────────────────

_kokoro_engine: Optional[KokoroTTSEngine] = None


def get_kokoro_engine(voice: str = "af_heart", speed: float = 1.0) -> KokoroTTSEngine:
    """Get or create the KokoroTTSEngine singleton.

    If the engine was created with different voice/speed, a new one
    is returned (old one is discarded — GC will close it).
    """
    global _kokoro_engine
    if _kokoro_engine is None or _kokoro_engine.voice != voice:
        if _kokoro_engine is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(_kokoro_engine.close())
            except Exception:
                pass
        _kokoro_engine = KokoroTTSEngine(voice=voice, speed=speed)
    return _kokoro_engine


def reset_kokoro_engine():
    """Reset the singleton (call when switching voices)."""
    global _kokoro_engine
    _kokoro_engine = None
