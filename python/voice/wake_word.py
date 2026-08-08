"""
Wake word detection using Vosk offline speech recognition.
Sends HTTP POST to Electron's /wake endpoint when triggered,
so the app window restores and focuses even when running in background.
Supports bilingual English + Hindi wake words.
"""

import array
import io
import json
import math
import re
import struct
import threading
import urllib.error
import urllib.request
import wave
from typing import Any, Callable, Optional

from config import get_settings
from utils.callback_guards import SyncCallback

from .noise_floor import (
    MIN_STAGING_SAMPLES,
    AmbientNoiseTracker,
    compute_rms,
    set_pending_adaptive_threshold,
)

# Electron wake receiver endpoint — spawned as a lightweight HTTP server
ELECTRON_WAKE_URL = "http://127.0.0.1:8112/wake"

# Reply sound configuration
_REPLY_SOUND_SAMPLE_RATE = 22050
_REPLY_SOUND_DURATION_MS = 300  # ~300ms chime

# User-persisted sound preferences (loaded/saved via /voice/sound-settings)
# These are set by routes.py on startup and when toggled from the UI
_wake_sound_enabled: bool = True
_command_sound_enabled: bool = True

# ─── Hindi wake word phrases ────────────────────────────────────────────────

HINDI_WAKE_PHRASES = [
    # Devanagari script variations
    r"\bनमस्ते बार्क\b",
    r"\bहे बार्क\b",
    r"\bजागो बार्क\b",
    # Transliterated (English script but Hindi words)
    r"\bnamaste ba[r]+k\b",
    r"\bhey ba[r]+k\b",
    r"\bjaago ba[r]+k\b",
]

# ─── Sound profile presets ────────────────────────────────────────────────
# Each preset: (hz1, hz2, split_ratio, duration_ms, volume)
# split_ratio = where the tone transition happens (0.0-1.0)

_SOUND_PROFILES: dict[str, dict[str, Any]] = {
    "wake": {
        "hz1": 880,       # A5
        "hz2": 1320,      # E6 (ascending)
        "split": 0.5,     # transition at midpoint
        "duration_ms": 300,
        "volume": 0.6,
        "desc": "two-tone ascending (wake)",
    },
    "command_accepted": {
        "hz1": 1000,      # ~B5
        "hz2": 750,       # ~F#5 (descending)
        "split": 0.3,     # quick transition
        "duration_ms": 200,
        "volume": 0.5,
        "desc": "short descending ping (command accepted)",
    },
}


def _generate_chime_wav(profile: str = "wake") -> bytes:
    """Generate a chime WAV using the given sound profile.

    Profiles:
        "wake" — ascending two-tone (880->1320Hz, 300ms)
        "command_accepted" — descending ping (1000->750Hz, 200ms)

    Returns raw WAV bytes that can be played directly.
    """
    preset = _SOUND_PROFILES.get(profile, _SOUND_PROFILES["wake"])
    sample_rate = _REPLY_SOUND_SAMPLE_RATE
    duration = preset["duration_ms"] / 1000.0
    num_samples = int(sample_rate * duration)
    split_t = duration * preset["split"]
    vol = preset["volume"]
    hz1 = preset["hz1"]
    hz2 = preset["hz2"]

    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        fade_in = min(1.0, t * 40)
        fade_out = max(0, 1.0 - (t / duration) * 0.3)

        if t < split_t:
            env = fade_in * (1.0 - t * (1.0 / split_t) * 0.5)
            val = math.sin(2 * math.pi * hz1 * t) * env
        else:
            t_rel = t - split_t
            build = min(1.0, t_rel * 20)
            env = build * fade_out
            val = math.sin(2 * math.pi * hz2 * t) * env

        samples.append(max(-1.0, min(1.0, val)))

    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = num_samples * num_channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )

    audio_data = array.array("h", (int(s * 32767 * vol) for s in samples))
    return header + audio_data.tobytes()


def _play_wav(wav_bytes: bytes, label: str = "sound"):
    """Play WAV bytes through speakers in a daemon thread."""
    def _play():
        try:
            import numpy as np
            import sounddevice as sd

            from config import get_settings as _cfg

            from .audio_device import resolve_output_device
            output_device = resolve_output_device(_cfg().audio_output_device)

            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                rate = wf.getframerate()

            sd.play(data, rate, device=output_device)
            sd.wait()
            print(f"[Audio] {label} played")
        except Exception as e:
            if "No default output device" not in str(e):
                print(f"[Audio] {label} error: {e}")

    threading.Thread(target=_play, daemon=True).start()


def set_sound_enabled(sound_type: str, enabled: bool):
    """Enable or disable a sound type.

    Args:
        sound_type: 'wake' or 'command'
        enabled: True to play, False to mute
    """
    global _wake_sound_enabled, _command_sound_enabled
    if sound_type == "wake":
        _wake_sound_enabled = enabled
    elif sound_type == "command":
        _command_sound_enabled = enabled
    else:
        print(f"[Audio] Unknown sound type: {sound_type}")
        return
    print(f"[Audio] {sound_type} sound {'enabled' if enabled else 'muted'}")


def get_sound_settings() -> dict[str, bool]:
    """Get current sound preference state."""
    return {
        "wake_sound_enabled": _wake_sound_enabled,
        "command_sound_enabled": _command_sound_enabled,
    }


def play_wake_sound():
    """Play the wake word detection chime (ascending two-tone), if enabled."""
    if not _wake_sound_enabled:
        return
    _play_wav(_generate_chime_wav("wake"), "Wake sound")


def play_command_accepted_sound():
    """Play the command accepted confirmation ping (descending), if enabled."""
    if not _command_sound_enabled:
        return
    _play_wav(_generate_chime_wav("command_accepted"), "Command accepted sound")


def _send_wake_signal():
    """Send POST request to Electron's wake receiver to restore and focus the window."""
    try:
        data = json.dumps({"source": "wake_word"}).encode("utf-8")
        req = urllib.request.Request(
            ELECTRON_WAKE_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
        print("[WakeWord] Wake signal sent to Electron — window should restore")
    except urllib.error.URLError as e:
        if hasattr(e, "code") and e.code:
            print(f"[WakeWord] Wake signal failed (HTTP {e.code})")
    except Exception as e:
        print(f"[WakeWord] Wake signal error: {e}")


class WakeWordDetector:
    """Detects wake words using Vosk with bilingual English + Hindi support.

    Loads both English and Hindi Vosk models. Runs the active model in a
    continuous background thread. Supports live switching between languages.
    On detection, sends an HTTP POST to Electron's /wake endpoint.

    .. note::

        ``on_wake_word`` and ``on_conversation_trigger`` are called from a
        **daemon thread**, not the async event loop.  They **must** be
        regular (non-async) functions — passing an ``async def`` will
        raise ``TypeError`` at assignment.
    """

    # Sync-only callback slots — assigning an async function raises TypeError
    on_wake_word = SyncCallback()
    on_conversation_trigger = SyncCallback()

    def __init__(self, on_wake_word: Optional[Callable] = None, on_conversation_trigger: Optional[Callable] = None):
        self.settings = get_settings()
        self.on_wake_word = on_wake_word
        self.on_conversation_trigger = on_conversation_trigger
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._language: str = "en"  # "en" or "hi"

        # Mic level and confidence tracking
        self._sensitivity = "medium"
        self._last_mic_level = 0.0
        self._last_vosk_confidence = 0.0
        self._rec_needs_rebuild = False  # Signal to recreate recognizer in listen loop

        # Ambient noise-floor tracker for adaptive RMS energy thresholding.
        # Fed one RMS value per mic chunk; on wake, the floor is used to
        # stage an adaptive energy threshold (~3x floor) for the voice agent.
        self._noise_tracker = AmbientNoiseTracker()

        # Pause/resume — used to release the mic stream before STT opens its own
        self._paused = False
        # Threading.Event for clean mic handoff: set when stream is fully released.
        # Conversation start should wait for this before opening its own stream.
        self._stream_released = threading.Event()

        # Last full utterance captured during wake word detection
        # Contains the complete Vosk recognition text (wake word + command)
        self._last_utterance: str = ""

        # Build sensitivity phrases from the configured wake word
        self._sensitivity_phrases = self._build_sensitivity_phrases(self.settings.wake_word)
        self._wake_phrases = self._sensitivity_phrases[self._sensitivity]

        # ─── Load English Vosk model ──────────────────────────────────
        model_path = self.settings.vosk_model_path
        self.model_en = None
        try:
            import vosk
            self.model_en = vosk.Model(model_path)
            print(f"[WakeWord] English model loaded from {model_path}")
        except ImportError:
            print("[WakeWord] Vosk not installed. Install with: pip install vosk")
        except Exception as e:
            print(f"[WakeWord] Failed to load English model: {e}")

        # ─── Load Hindi Vosk model ────────────────────────────────────
        hindi_path = self.settings.vosk_hindi_model_path
        self.model_hi = None
        try:
            import os as _os
            if hindi_path and hindi_path != model_path and _os.path.isdir(hindi_path):
                import vosk
                self.model_hi = vosk.Model(hindi_path)
                print(f"[WakeWord] Hindi model loaded from {hindi_path}")
            elif hindi_path and not _os.path.isdir(hindi_path):
                print(f"[WakeWord] Hindi model path '{hindi_path}' does not exist — skipping")
        except Exception as e:
            print(f"[WakeWord] Failed to load Hindi model (non-fatal): {e}")

        # Active model (default to English if available)
        self.model = self.model_en if self.model_en else (self.model_hi if self.model_hi else None)

    @property
    def language(self) -> str:
        """Get the current active language ('en' or 'hi')."""
        return self._language

    def set_language(self, lang: str) -> bool:
        """Switch the active recognition language.

        If the detector is currently running, the recognizer is recreated
        with the new model so the switch takes effect immediately.

        Args:
            lang: 'en' for English or 'hi' for Hindi.

        Returns:
            True if switch was successful, False if model not available.
        """
        if lang == self._language:
            return True
        if lang == "en" and self.model_en:
            self._language = "en"
            self.model = self.model_en
            self._wake_phrases = self._sensitivity_phrases[self._sensitivity]
            self._rec_needs_rebuild = True
            print("[WakeWord] Switched to English")
            return True
        elif lang == "hi" and self.model_hi:
            self._language = "hi"
            self.model = self.model_hi
            self._wake_phrases = HINDI_WAKE_PHRASES
            self._rec_needs_rebuild = True
            print(f"[WakeWord] Switched to Hindi ({len(HINDI_WAKE_PHRASES)} wake phrases)")
            return True
        else:
            print(f"[WakeWord] Cannot switch to '{lang}' — model not available")
            return False
    # ─── Configurable wake word ──────────────────────────────────────

    def set_wake_word(self, wake_word: str):
        """Dynamically change the wake word phrase.
        Rebuilds sensitivity patterns and applies immediately.

        Args:
            wake_word: New wake word phrase (e.g. "hey barq", "computer", "ok google")
        """
        self._sensitivity_phrases = self._build_sensitivity_phrases(wake_word)
        if self._language == "en":
            self._wake_phrases = self._sensitivity_phrases[self._sensitivity]
        print(f"[WakeWord] Wake phrase updated to: '{wake_word}'")

    @staticmethod
    def _build_sensitivity_phrases(wake_word: str) -> dict[str, list[str]]:
        """Build regex wake phrase patterns from a configured wake word.

        Given a base wake word like "hey barq" or "ok computer", generates:
        - Low: exact match only
        - Medium: exact + phonetic variations (for Vosk misrecognition)
        - High: exact + phonetic + partial matches

        Args:
            wake_word: The configured wake word/phrase.

        Returns:
            Dict of sensitivity level -> list of regex patterns.
        """
        word = wake_word.lower().strip()
        parts = word.split()
        primary = parts[-1] if parts else word
        prefix = " ".join(parts[:-1]) if len(parts) > 1 else None

        # Escape regex special characters for safe pattern building
        escaped_word = re.escape(word)

        patterns = {
            "low": [
                rf"\b{escaped_word}\b",
            ],
            "medium": [
                rf"\b{escaped_word}\b",
            ],
            "high": [
                rf"\b{escaped_word}\b",
            ],
        }

        # Add "wake up <primary>" variant if wake word starts differently
        if prefix and prefix not in ("wake up", "wake"):
            wake_up_phrase = f"wake up {primary}"
            escaped_wake_up = re.escape(wake_up_phrase)
            patterns["low"].append(rf"\b{escaped_wake_up}\b")
            patterns["medium"].append(rf"\b{escaped_wake_up}\b")
            patterns["high"].append(rf"\b{escaped_wake_up}\b")

        # Always add "hey <primary>" as an additional variant
        # so users can say "hey computer", "hey jarvis", etc.
        # Skip if the wake word itself already starts with "hey"
        hey_phrase = f"hey {primary}"
        if hey_phrase != word:
            escaped_hey = re.escape(hey_phrase)
            patterns["low"].append(rf"\b{escaped_hey}\b")
            patterns["medium"].append(rf"\b{escaped_hey}\b")
            patterns["high"].append(rf"\b{escaped_hey}\b")

        # Add phonetic variations for medium/high
        # Vosk often confuses similar-sounding words, so we add variants
        if len(primary) > 2:
            # Replace trailing 'q' with 'k' (barq -> bark)
            if primary.endswith("q"):
                phonetic = primary[:-1] + "k"
                if prefix:
                    phrase = f"{prefix} {phonetic}"
                else:
                    phrase = phonetic
                patterns["medium"].append(rf"\b{re.escape(phrase)}\b")
                patterns["high"].append(rf"\b{re.escape(phrase)}\b")

            # Truncate last 1-2 chars for partial match (high only)
            if len(primary) > 3:
                truncated = primary[:-1]
                if prefix:
                    phrase = f"{prefix} {truncated}"
                else:
                    phrase = truncated
                patterns["high"].append(rf"\b{re.escape(phrase)}\w*\b")

            # Common vowel-substitution variants for Vosk misrecognition
            # Vosk often confuses similar-sounding vowels, especially at word endings
            # e.g. "computer" → "computa", "computor"; "compute" → "computa"; "computa" → "compute"
            vowel_variants: list[str] = []
            if primary.endswith("er"):
                # "computer" → "computa" (dropped 'r', vowel swap)
                vowel_variants.append(primary[:-2] + "a")
                # "computer" → "computor" (vowel swap)
                vowel_variants.append(primary[:-2] + "or")
            elif primary.endswith("e") and len(primary) > 2:
                # "compute" → "computa"
                vowel_variants.append(primary[:-1] + "a")
            elif primary.endswith("a") and len(primary) > 2:
                # "computa" → "compute"
                vowel_variants.append(primary[:-1] + "e")

            for variant in vowel_variants:
                if variant == primary:
                    continue
                if prefix:
                    phrase = f"{prefix} {variant}"
                else:
                    phrase = variant
                full_pattern = rf"\b{re.escape(phrase)}\b"
                patterns["medium"].append(full_pattern)
                patterns["high"].append(full_pattern)

        return patterns

    def set_sensitivity(self, level: str):
        """Set detection sensitivity level (English only).

        Args:
            level: One of 'low', 'medium', 'high'
        """
        if level not in self._sensitivity_phrases:
            print(f"[WakeWord] Invalid sensitivity level: {level}, keeping '{self._sensitivity}'")
            return
        old_level = self._sensitivity
        self._sensitivity = level
        # Only update English wake phrases; Hindi uses its own fixed set
        if self._language == "en":
            self._wake_phrases = self._sensitivity_phrases[level]
        print(f"[WakeWord] Sensitivity changed: {old_level} -> {level}")

    def _stage_adaptive_threshold(self) -> None:
        """Compute and stage the adaptive RMS energy threshold for the voice agent.

        Uses the ambient noise floor sampled during wake-word listening:
        threshold = ~3x the floor (via ``suggested_threshold()``), staged in
        the ``noise_floor`` module for the voice agent to consume on connect.

        If fewer than ``MIN_STAGING_SAMPLES`` ambient samples have been
        collected (e.g. the very first wake after startup), no threshold is
        staged — with zero samples the floor is 0 and the threshold would
        clamp to 50 (max sensitivity), risking echo misfires.

        Never raises: failures here must not block wake handling.
        """
        try:
            if self._noise_tracker.count >= MIN_STAGING_SAMPLES:
                floor = self._noise_tracker.noise_floor()
                threshold = self._noise_tracker.suggested_threshold()
                set_pending_adaptive_threshold(threshold)
                print(
                    f"[WakeWord] Ambient noise floor RMS={floor:.0f} "
                    f"→ adaptive energy threshold={threshold}"
                )
            else:
                set_pending_adaptive_threshold(None)
                print(
                    f"[WakeWord] Skipping adaptive threshold — "
                    f"only {self._noise_tracker.count}/{MIN_STAGING_SAMPLES} "
                    f"ambient samples collected"
                )
        except Exception as e:
            print(f"[WakeWord] Adaptive threshold error (non-fatal): {e}")

    def get_mic_level(self) -> float:
        """Get the current microphone audio level (0.0-1.0)."""
        return self._last_mic_level if self._running else 0.0

    def get_last_confidence(self) -> float:
        """Get the confidence score from the most recent Vosk recognition result."""
        return self._last_vosk_confidence if self._running else 0.0

    def get_last_utterance(self) -> str:
        """Get the most recent full utterance captured during wake word detection.

        Returns the complete Vosk recognition text (wake phrase + any following
        command), or an empty string if no wake word has been triggered.
        """
        return self._last_utterance

    def clear_last_utterance(self):
        """Clear the stored utterance so it is not reprocessed."""
        self._last_utterance = ""

    def start(self):
        """Start listening for wake word in a background thread."""
        if self._running or not self.model:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        lang_label = "Hindi" if self._language == "hi" else "English"
        print(f"[WakeWord] Always-on listening started ({lang_label})")

    def pause(self):
        """Pause wake word detection and release the microphone stream.

        The background thread stays alive but idle — no audio data is consumed.
        Call ``resume()`` to reopen the stream and continue detection.

        This is used by the wake word callback to free the microphone so the
        Deepgram Voice Agent can open its own stream without resource contention.

        After this returns, callers should use ``wait_for_stream_released()``
        (with timeout) before opening their own microphone stream.
        """
        self._paused = True
        self._stream_released.clear()  # reset before release
        print("[WakeWord] Pause requested — stream will be released on next loop iteration")

    def wait_for_stream_released(self, timeout: float = 3.0) -> bool:
        """Block until the microphone stream is fully released.

        After ``pause()`` is called, the background thread asynchronously
        closes the sounddevice stream and sets ``_stream_released``.
        Call this method to wait for that handoff to complete before
        opening your own microphone stream.

        Args:
            timeout: Maximum seconds to wait (default 3.0).

        Returns:
            True if stream was released, False if timed out.
        """
        released = self._stream_released.wait(timeout=timeout)
        if released:
            print("[WakeWord] Microphone stream released cleanly")
        else:
            print(f"[WakeWord] Warning: Mic release timed out after {timeout}s")
        return released

    def resume(self):
        """Resume wake word detection and reopen the microphone stream.

        Reopens the ``sounddevice.InputStream`` that was closed during ``pause()``.
        The recognizer is rebuilt to ensure a clean state.
        """
        self._paused = False
        self._stream_released.set()
        print("[WakeWord] Resume requested — stream will be reopened on next loop iteration")

    def stop(self):
        """Stop wake word detection."""
        self._running = False
        self._paused = False  # unblock any idle spin loop
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        print("[WakeWord] Stopped")

    def _listen_loop(self):
        """Continuously listen to microphone and detect wake words in the active language.

        Supports pause/resume: when ``pause()`` is called the underlying
        ``sounddevice.InputStream`` is closed so other components (e.g. Whisper STT)
        can open the microphone without resource contention.  When ``resume()`` is
        called the stream is reopened and detection continues seamlessly.
        """
        import time as _time

        import sounddevice as sd
        import vosk

        # Resolve input device from config
        from .audio_device import resolve_input_device
        device = resolve_input_device(self.settings.audio_input_device)

        stream: Optional[sd.InputStream] = None
        rec: Optional[vosk.KaldiRecognizer] = None

        while self._running:
            # ── Pause: release mic and idle-spin ────────────────────────
            if self._paused:
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except sd.PortAudioError as e:
                        print(f"[WakeWord] PortAudio error releasing stream: {e}")
                    except Exception as e:
                        print(f"[WakeWord] Unexpected error releasing stream: {e}")
                    finally:
                        stream = None
                        rec = None
                        self._stream_released.set()  # signal: mic is free
                        print("[WakeWord] Paused — microphone released")
                else:
                    self._stream_released.set()  # already released
                while self._paused and self._running:
                    _time.sleep(0.1)
                if not self._running:
                    break
                self._stream_released.clear()  # reset for next pause cycle
                print("[WakeWord] Resuming — will reopen stream")
                continue

            # ── Open stream on first run or after resume ────────────────
            if stream is None:
                stream = sd.InputStream(
                    device=device,
                    samplerate=16000,
                    channels=1,
                    dtype="int16",
                    blocksize=4000,
                )
                try:
                    stream.start()
                    dev_info = f" (device: {device})" if device is not None else ""
                    print(f"[WakeWord] Audio stream opened{dev_info}")
                except Exception as e:
                    print(f"[WakeWord] Failed to open audio stream: {e}")
                    self._running = False
                    break
                rec = vosk.KaldiRecognizer(self.model, 16000)
                rec.SetWords(True)

            # ── Read and process audio chunk ────────────────────────────
            try:
                data, overflowed = stream.read(4000)
                data_bytes = data.tobytes()

                # Check if recognizer needs to be rebuilt (language switch)
                if self._rec_needs_rebuild:
                    rec = vosk.KaldiRecognizer(self.model, 16000)
                    rec.SetWords(True)
                    self._rec_needs_rebuild = False
                    print(f"[WakeWord] Recognizer rebuilt for {self._language}")

                # Compute RMS mic level using numpy and feed the ambient
                # noise-floor tracker (one sample per chunk).
                try:
                    rms = compute_rms(data)
                    self._last_mic_level = min(1.0, rms / 10000.0)
                    self._noise_tracker.add(rms)
                except Exception:
                    self._last_mic_level = 0.0

                if rec is not None and rec.AcceptWaveform(data_bytes):
                    result = json.loads(rec.Result())
                    text = result.get("text", "").lower().strip()

                    words = result.get("result", [])
                    if words:
                        self._last_vosk_confidence = sum(w.get("conf", 0.0) for w in words) / len(words)
                    else:
                        self._last_vosk_confidence = 0.0

                    if text and not self._paused:
                        print(f"[WakeWord] Heard: '{text}'")

                        # Check active language wake phrases
                        for phrase in self._wake_phrases:
                            if re.search(phrase, text):
                                lang_tag = "HI" if self._language == "hi" else "EN"
                                print(f"[WakeWord] [{lang_tag}] Wake word detected: '{text}' matched '{phrase}'")

                                # Store the full utterance so downstream can extract
                                # the command portion (text after the wake word).
                                self._last_utterance = text

                                # Stage an adaptive RMS energy threshold (~3x the
                                # sampled ambient noise floor) for the voice agent
                                # to consume on connect().  Best-effort: any failure
                                # here must not block wake handling.
                                self._stage_adaptive_threshold()

                                play_wake_sound()
                                _send_wake_signal()

                                if self.on_wake_word:
                                    # Pass the full utterance text to the callback
                                    self.on_wake_word(text)

                                # Trigger conversation mode (Alexa/Gemini-style)
                                if self.on_conversation_trigger:
                                    self.on_conversation_trigger()

                                break
            except OSError as e:
                if "Input overflowed" in str(e) or str(e).startswith("Buffer") or str(e).startswith("Overflow"):
                    # Log buffer overflow to evolution tracker
                    try:
                        from .evolution_logger import get_evolution_logger
                        get_evolution_logger().record(
                            "buffer_overflow",
                            metadata={"device": stream.device if hasattr(stream, "device") else "unknown"},
                        )
                    except Exception:
                        pass
                    continue
                print(f"[WakeWord] Stream error: {e}")
                continue
            except Exception as e:
                print(f"[WakeWord] Error: {e}")

        # Cleanup stream on shutdown
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
