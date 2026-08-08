"""
Pipecat Voice Agent for BARQ — local STT → LLM → TTS pipeline.

Architecture:
  Wake word (local Vosk) → PipecatVoiceAgent → speakers

The agent uses local models for the full voice pipeline:
  - Speech-to-text:  faster-whisper (medium model, GPU if available)
  - LLM reasoning:   Ollama (local, configurable model)
  - Text-to-speech:  edge-tts (Windows TTS voices)

No cloud API keys are needed beyond what BARQ already uses for Ollama.
"""

import asyncio
import collections
import json
import queue
import time
from typing import Any, Callable, Optional

import numpy as np

from config import get_settings

from .agent_base import VoiceAgentBase


def _safe_print(*args, **kwargs) -> None:
    """Console-safe print for Windows cp1252 terminals.

    Windows consoles default to the cp1252 codec, which cannot encode emoji or
    other non-Latin-1 characters.  A bare ``print()`` of such text raises
    ``UnicodeEncodeError`` ('charmap' codec error) and can kill the calling
    thread — which is exactly what crashed the vision stream thread and made
    ``vision_stream_start`` report an error.  This helper re-encodes with the
    console's own codec using ``errors="replace"`` (preserving encodable
    characters like ``°C`` or accents, replacing only emoji), so logging
    never crashes the stream.
    """
    import builtins
    import sys
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        # First pass: re-encode with the console's actual codec (preserves
        # encodable characters like °C or accents).  If the output still
        # can't be encoded (e.g. the console is cp1252 but stdout reports
        # utf-8), fall back to ASCII replacement — never raise.
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            safe = [
                a.encode(enc, errors="replace").decode(enc)
                if isinstance(a, str) else a
                for a in args
            ]
            builtins.print(*safe, **kwargs)
        except UnicodeEncodeError:
            safe = [
                a.encode("ascii", errors="replace").decode("ascii")
                if isinstance(a, str) else a
                for a in args
            ]
            builtins.print(*safe, **kwargs)


# ── Windows asyncio AssertionError suppressor ─────────────────────────
# Python 3.13's Proactor event loop on Windows may fire a spurious
# AssertionError from _ProactorBaseWritePipeTransport._loop_writing()
# when a pipe write completes after the transport has been closed.
# This corrupts the event loop and causes "Cannot enter into task"
# cascades, which then manifest as bogus UnicodeEncodeError / charmap
# errors in downstream HTTP calls (edge-tts, av, httpx).
#
# The fix: monkey-patch the method to catch and suppress the
# AssertionError so the event loop stays healthy.

_ORIG_LOOP_WRITING = None


def _install_proactor_assertion_guard():
    """Install a guard that suppresses AssertionError from
    asyncio.proactor_events._ProactorBaseWritePipeTransport._loop_writing.

    This is a known Windows asyncio bug in Python 3.13 where a completed
    write future doesn't match the expected future when the transport is
    being closed concurrently.
    """
    global _ORIG_LOOP_WRITING
    try:
        import asyncio.proactor_events as _pe
        cls = _pe._ProactorBaseWritePipeTransport
        if _ORIG_LOOP_WRITING is None:
            _ORIG_LOOP_WRITING = cls._loop_writing  # type: ignore[attr-defined]

            def _safe_loop_writing(self, f=None, **kwargs):
                try:
                    _ORIG_LOOP_WRITING(self, f, **kwargs)
                except AssertionError:
                    pass  # benign — transport was likely closed

            cls._loop_writing = _safe_loop_writing  # type: ignore[attr-defined]
            print("[PipecatAgent] Installed Windows asyncio AssertionError guard")
    except Exception:
        pass  # Not on Windows or different Python version


_install_proactor_assertion_guard()


def _find_ffmpeg() -> Optional[str]:
    """Try to find ffmpeg.exe on the system.

    Searches common install locations and PATH. Returns the path
    to ffmpeg.exe if found, or None if not found.
    """
    import os as _os
    import shutil as _shutil

    # 1. Check PATH first (fastest)
    exe = _shutil.which("ffmpeg")
    if exe:
        return exe

    # 2. Check common Windows install locations
    common_paths = [
        # Winget install location
        _os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        ),
        # Chocolatey
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        # Manual install under Program Files
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        # Scoop
        _os.path.expandvars(r"%USERPROFILE%\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
        # Common user install
        _os.path.expandvars(r"%USERPROFILE%\ffmpeg\bin\ffmpeg.exe"),
    ]

    for path in common_paths:
        if _os.path.isfile(path):
            return path

    # 3. Recursive scan of PATH directories for ffmpeg.exe
    path_dirs = _os.environ.get("PATH", "").split(";")
    for pdir in path_dirs:
        if not pdir or not _os.path.isdir(pdir):
            continue
        try:
            for entry in _os.listdir(pdir):
                if entry.lower() == "ffmpeg.exe":
                    return _os.path.join(pdir, entry)
        except PermissionError:
            continue

    return None


# ── Audio constants ───────────────────────────────────────────────────

STT_SAMPLE_RATE = 16000       # Whisper expects 16kHz
STT_BLOCK_SIZE = 1600         # 100ms at 16kHz
TTS_SAMPLE_RATE = 24000       # same as Deepgram output
TTS_BLOCK_SIZE = 480          # 20ms at 24kHz
ENERGY_THRESHOLD = 250        # RMS floor for voice activity detection
SILENCE_TIMEOUT_MS = 800      # ms of silence before VAD endpointing

# Supported TTS backends
TTS_BACKEND_EDGETTS = "edge-tts"
TTS_BACKEND_KOKORO = "kokoro"


class PipecatVoiceAgent(VoiceAgentBase):
    """Local voice agent using Whisper → Ollama → local TTS.

    Supports multiple TTS backends:
        - edge-tts:  Microsoft Edge TTS (online, free, no API key)
        - kokoro:    Kokoro-82M neural TTS (offline, ∼330 MB model)

    Usage:
        agent = PipecatVoiceAgent(
            ollama_host="...", ollama_model="...",
            tts_backend="kokoro", tts_voice="af_heart"
        )
        await agent.connect()
        await agent.start_conversation()
        # ... conversation runs until stop ...
        await agent.stop()
    """

    def __init__(
        self,
        ollama_host: str = "http://127.0.0.1:11434",
        ollama_model: str = "llama3.2:3b",
        tts_backend: str = TTS_BACKEND_EDGETTS,
        tts_voice: str = "af_heart",
        tts_speed: float = 1.0,
    ):
        self.ollama_host = ollama_host
        self.ollama_model = ollama_model
        self.tts_backend = tts_backend  # "edge-tts" or "kokoro"
        self.tts_voice = tts_voice      # Kokoro voice (e.g. "af_heart") or edge-tts voice
        self.tts_speed = tts_speed       # only applies to Kokoro
        self.settings = get_settings()

        self._running = False
        self._input_stream: Optional[Any] = None  # sd.InputStream
        self._output_stream: Optional[Any] = None  # sd.OutputStream
        self._send_task: Optional[asyncio.Task] = None  # audio capture loop
        self._receive_task: Optional[asyncio.Task] = None  # process loop

        # Thread-safe audio queue (mic → whisper)
        self._audio_queue: queue.Queue = queue.Queue(maxsize=500)

        # Ring buffer for TTS output audio
        self._output_ring_buffer: collections.deque = collections.deque(maxlen=72000)

        # VAD state
        self._silence_frames = 0
        self._is_speaking = False  # user is currently speaking
        self._last_voice_activity: float = 0.0

        # Agent speaking state (blocks mic input)
        self._agent_is_speaking: bool = False
        self._last_agent_done_speaking_at: float = 0.0

        # Whisper model (lazy-loaded)
        self._whisper_model: Any = None

        # Kokoro TTS engine (lazy-loaded)
        self._kokoro_engine: Any = None

        # Accumulated audio buffer for transcription
        self._accumulated_audio: list[np.ndarray] = []
        self._accumulated_samples = 0

        # Callbacks (set by ConversationListener)
        self.on_interim_transcript: Optional[Callable[..., Any]] = None
        self.on_final_transcript: Optional[Callable[..., Any]] = None
        self.on_agent_speaking: Optional[Callable[..., Any]] = None
        self.on_agent_done_speaking: Optional[Callable[..., Any]] = None
        self.on_audio_chunk: Optional[Callable[..., Any]] = None
        self.on_agent_text: Optional[Callable[..., Any]] = None

        # User transcript buffer (accumulated between LLM calls)
        self._user_text_buffer: list[str] = []

        # Barge-in debounce
        self._last_user_spoke_at: float = 0.0

        # Pending function calls (dedup)
        self._pending_functions: set[str] = set()

        # Energy gate logging rate-limiter
        self._energy_log_timer: float = 0.0

        # Mic RMS energy threshold (loaded from DB/env on connect)
        self._energy_threshold: int = ENERGY_THRESHOLD

        # Rate-limiter for ring buffer full log
        self._output_log_timer: float = 0.0

        # Rate-limiter for underflow warning
        self._underflow_log_timer: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    async def _load_whisper(self):
        """Lazy-load the faster-whisper model."""
        if self._whisper_model is not None:
            return
        print("[PipecatAgent] Loading Whisper STT model (medium)...")
        try:
            from faster_whisper import WhisperModel
            # Try GPU first, fall back to CPU
            try:
                self._whisper_model = WhisperModel(
                    "medium", device="cuda", compute_type="float16"
                )
                print("[PipecatAgent] Whisper loaded on GPU")
            except Exception:
                self._whisper_model = WhisperModel(
                    "medium", device="cpu", compute_type="int8"
                )
                print("[PipecatAgent] Whisper loaded on CPU (GPU unavailable)")
        except ImportError:
            print("[PipecatAgent] faster-whisper not installed. Install with: pip install faster-whisper")
            raise

    # ── Mic Energy Threshold (RMS voice-activity gate) ──────────────

    def set_energy_threshold(self, value: int) -> None:
        """Update the mic RMS energy gate threshold at runtime.

        Clamped to the supported [50, 2000] range. Takes effect on the
        next audio callback — no reconnect needed.
        """
        clamped = max(50, min(2000, int(value)))
        self._energy_threshold = clamped
        print(f"[PipecatAgent] Mic RMS energy threshold set to {clamped}")

    async def _load_energy_threshold(self) -> None:
        """Load the mic RMS energy threshold on connect.

        Priority:
            1. Adaptive threshold staged at wake (noise_floor module)
            2. DB setting (vad_energy_threshold)
            3. Env var VAD_ENERGY_THRESHOLD
            4. Default 250.
        """
        # Highest priority: adaptive threshold staged by the wake word
        # detector (~3x the sampled ambient noise floor).  Consumed once.
        try:
            from .noise_floor import consume_pending_adaptive_threshold
            pending = consume_pending_adaptive_threshold()
            if pending is not None:
                self.set_energy_threshold(pending)
                print(f"[PipecatAgent] Applied wake-time adaptive threshold {pending}")
                return
        except Exception as e:
            print(f"[PipecatAgent] Adaptive threshold consume failed (non-fatal): {e}")

        try:
            from database import settings_dao
            raw = await settings_dao.get_setting("vad_energy_threshold")
        except Exception:
            raw = None
        if raw is None:
            import os as _os
            raw = _os.getenv("VAD_ENERGY_THRESHOLD", "")
        if raw:
            try:
                self.set_energy_threshold(int(float(raw)))
            except (TypeError, ValueError):
                pass

    async def connect(self) -> bool:
        """Initialize models and verify Ollama is reachable.

        This doesn't open audio streams — those are opened in
        start_conversation().  We just validate that the required
        models and services are available.

        Uses ``httpx`` for Ollama verification instead of the
        ``ollama.AsyncClient`` to avoid aiohttp event loop conflicts
        that cause "Cannot enter into task" RuntimeErrors.
        """
        # Load mic RMS energy threshold from DB/env before streaming
        try:
            await self._load_energy_threshold()
        except Exception as e:
            print(f"[PipecatAgent] Energy threshold load failed (non-fatal): {e}")

        # Load Whisper model
        try:
            await self._load_whisper()
        except Exception as e:
            print(f"[PipecatAgent] Failed to load Whisper: {e}")
            return False

        # Verify Ollama is reachable via httpx (NOT aiohttp — avoids task conflicts)
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Check if Ollama server is running
                resp = await client.get(f"{self.ollama_host}/api/tags")
                if resp.status_code != 200:
                    print(f"[PipecatAgent] Ollama server returned {resp.status_code} at {self.ollama_host}")
                    return False

                # Check if the configured model exists in the model list
                models = resp.json().get("models", [])
                model_names = [m["name"] for m in models]

                if self.ollama_model not in model_names:
                    print(
                        f"[PipecatAgent] Model '{self.ollama_model}' not found on Ollama. "
                        f"Available models: {', '.join(model_names[:5])}{"..." if len(model_names) > 5 else ""}. "
                        f"Run: ollama pull {self.ollama_model}"
                    )
                    return False

                print(f"[PipecatAgent] Ollama verified (model={self.ollama_model}, {len(models)} models)")
        except httpx.ConnectError:
            print(f"[PipecatAgent] Ollama not reachable at {self.ollama_host} — is it running?")
            return False
        except httpx.TimeoutException:
            print(f"[PipecatAgent] Ollama timeout at {self.ollama_host}")
            return False
        except Exception as e:
            print(f"[PipecatAgent] Ollama error: {e}")
            return False

        # Verify edge-tts is available for TTS
        try:
            import edge_tts  # noqa: F401
            print("[PipecatAgent] edge-tts available for TTS")
        except ImportError:
            print("[PipecatAgent] edge-tts not installed. Install with: pip install edge-tts")
            # Non-fatal — we'll try piper or another fallback
            pass

        # Initialize output stream early so speak_text() works
        # before start_conversation() is called (wake greeting).
        try:
            import sounddevice as sd
            from .audio_device import resolve_output_device
            output_device = resolve_output_device(self.settings.audio_output_device)

            # Log the resolved output device for debugging
            if output_device is not None:
                dev_info = sd.query_devices(output_device)
                print(f"[PipecatAgent] Output device [{output_device}]: {dev_info['name'].strip()}")
            else:
                print("[PipecatAgent] Output device: system default")

            self._output_stream = sd.OutputStream(
                device=output_device,
                samplerate=TTS_SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self._output_callback,
                blocksize=TTS_BLOCK_SIZE,
            )
            self._output_stream.start()
            print("[PipecatAgent] Output stream started")
        except Exception as e:
            print(f"[PipecatAgent] Could not init output stream in connect(): {e}")

        print("[PipecatAgent] Ready — models loaded")
        return True

    async def start_conversation(self, audio_device: Optional[int] = None):
        """Start audio capture and the LLM conversation loop."""
        if self._running:
            return

        self._running = True
        self._user_text_buffer = []
        self._accumulated_audio = []
        self._accumulated_samples = 0

        # Start the audio capture task and the processing task
        self._send_task = asyncio.create_task(
            self._audio_capture_loop(audio_device)
        )
        self._receive_task = asyncio.create_task(
            self._process_loop()
        )

    async def stop(self):
        """Stop all loops and release audio devices."""
        self._running = False

        # Cancel tasks
        for task_name in ("_send_task", "_receive_task"):
            task = getattr(self, task_name, None)
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, task_name, None)

        # Close input stream
        if self._input_stream:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

        # Close output stream
        if self._output_stream:
            try:
                self._output_stream.stop()
                self._output_stream.close()
            except Exception:
                pass
            self._output_stream = None

        # Clear buffers
        self._output_ring_buffer.clear()
        self._flush_audio_queue()

        self._agent_is_speaking = False
        print("[PipecatAgent] Stopped")

    def _flush_audio_queue(self):
        """Discard all queued mic chunks."""
        flushed = 0
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                flushed += 1
            except queue.Empty:
                break
        if flushed:
            print(f"[PipecatAgent] Flushed {flushed} stale chunks")

    # ── Audio Capture ────────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status):
        """sounddevice InputStream callback — runs on background thread."""
        if status:
            print(f"[PipecatAgent] Audio status: {status}")

        # Guard: agent is speaking — drop audio (echo suppression)
        if self._agent_is_speaking:
            return

        # Energy gate
        rms = int(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        if rms < self._energy_threshold:
            now = time.time()
            if now - self._energy_log_timer > 2.0:
                self._energy_log_timer = now
                # Quiet log — uncomment for debugging:
                # print(f"[PipecatAgent] Low-energy chunk dropped (RMS={rms})")
            return

        try:
            self._audio_queue.put_nowait(indata.copy())
        except queue.Full:
            print("[PipecatAgent] Audio queue full — dropping chunk")

    def _output_callback(self, outdata, frames, time_info, status):
        """sounddevice OutputStream callback — reads from ring buffer."""
        if status:
            print(f"[PipecatAgent] Output status: {status}")

        available = len(self._output_ring_buffer)
        outdata.fill(0.0)

        if available >= frames:
            # Enough data — fill the entire output buffer
            for i in range(frames):
                try:
                    outdata[i, 0] = self._output_ring_buffer.popleft()
                except IndexError:
                    break
        elif available > 0:
            # Partial data — use what's available, rest stays silence
            for i in range(available):
                try:
                    outdata[i, 0] = self._output_ring_buffer.popleft()
                except IndexError:
                    break
            # Rate-limited log for underflow
            now = time.time()
            if available > 0 and now - self._underflow_log_timer > 2.0:
                self._underflow_log_timer = now
                print(f"[PipecatAgent] Output buffer underflow: {available}/{frames} samples")

        # Log volume level periodically (first callback only)
        if not hasattr(self, "_logged_first_output"):
            self._logged_first_output = True
            peak = np.max(np.abs(outdata))
            print(f"[PipecatAgent] First output callback: {available} avail, peak={peak:.4f}")

    async def _audio_capture_loop(self, device: Optional[int] = None):
        """Capture mic audio and feed it to the VAD/transcription buffer."""
        import sounddevice as sd
        from .audio_device import resolve_input_device

        if device is None:
            device = resolve_input_device(self.settings.audio_input_device)

        # Output stream is already initialized in connect() — just verify it's running
        if self._output_stream is None:
            # Fallback: create it here if connect() didn't
            from .audio_device import resolve_output_device
            output_device = resolve_output_device(self.settings.audio_output_device)
            self._output_stream = sd.OutputStream(
                device=output_device,
                samplerate=TTS_SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self._output_callback,
                blocksize=TTS_BLOCK_SIZE,
            )
            self._output_stream.start()
            print("[PipecatAgent] Output stream started (fallback)")

        # Open mic input stream
        self._input_stream = sd.InputStream(
            device=device,
            samplerate=STT_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=STT_BLOCK_SIZE,
            callback=self._audio_callback,
        )
        self._input_stream.start()
        print("[PipecatAgent] Mic capture started")

        # Read from queue and accumulate for transcription
        while self._running:
            try:
                data = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._audio_queue.get(timeout=0.1),
                )
                # Accumulate audio for VAD/transcription
                self._accumulated_audio.append(data)
                samples = data.shape[0]
                self._accumulated_samples += samples
                self._last_voice_activity = time.time()

                # If we have enough audio (~2s), trigger transcription
                if self._accumulated_samples >= STT_SAMPLE_RATE * 2:  # 2 seconds
                    audio_for_stt = np.concatenate(self._accumulated_audio)
                    self._accumulated_audio = []
                    self._accumulated_samples = 0
                    asyncio.create_task(self._transcribe(audio_for_stt))

            except queue.Empty:
                # Check for silence timeout (VAD endpointing)
                if self._accumulated_samples > 0:
                    silence_duration = (time.time() - self._last_voice_activity) * 1000
                    if silence_duration > SILENCE_TIMEOUT_MS:
                        # Endpoint: transcribe what we have
                        audio_for_stt = np.concatenate(self._accumulated_audio)
                        self._accumulated_audio = []
                        self._accumulated_samples = 0
                        asyncio.create_task(self._transcribe(audio_for_stt))
                continue
            except Exception as e:
                print(f"[PipecatAgent] Capture error: {e}")
                break

    # ── Transcription (Whisper) ──────────────────────────────────────

    async def _transcribe(self, audio: np.ndarray):
        """Run Whisper transcription on accumulated audio."""
        if self._whisper_model is None:
            return

        try:
            # Convert int16 to float32 and flatten to 1D (sounddevice gives
            # shape (frames, channels) even for mono — Whisper needs flat 1D)
            audio_float = audio.astype(np.float32).flatten() / 32768.0

            # Run Whisper in a thread to avoid blocking
            segments, info = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._whisper_model.transcribe(
                    audio_float,
                    language="en",
                    vad_filter=True,
                    vad_parameters=dict(
                        min_silence_duration_ms=SILENCE_TIMEOUT_MS,
                        threshold=0.5,
                    ),
                )
            )

            # Collect transcription results
            text_parts: list[str] = []
            for seg in segments:
                text_parts.append(seg.text.strip())

            if not text_parts:
                return

            full_text = " ".join(text_parts)
            if not full_text.strip():
                return

            print(f"[PipecatAgent] Transcribed: '{full_text[:80]}{'...' if len(full_text) > 80 else ''}'")

            # Notify interim transcript
            if self.on_interim_transcript:
                self.on_interim_transcript(full_text)

            # Accumulate for LLM processing
            self._user_text_buffer.append(full_text)

            # If we have enough text (or silence after speech), trigger LLM
            combined = " ".join(self._user_text_buffer)
            if len(combined) > 10:  # Minimum viable utterance
                self._user_text_buffer = []
                # Notify final transcript
                if self.on_final_transcript:
                    self.on_final_transcript(combined)
                # Trigger LLM processing
                asyncio.create_task(self._process_with_llm(combined))

        except Exception as e:
            print(f"[PipecatAgent] Transcription error: {e}")

    # ── LLM Processing (Ollama) ──────────────────────────────────────

    async def _process_with_llm(self, user_text: str):
        """Send user text to Ollama LLM and process the response."""
        # Check for exit commands
        exit_phrases = [
            "nothing", "that's all", "we're done",
            "end conversation", "stop conversation",
            "go to sleep", "shut down",
        ]
        if any(phrase in user_text.lower() for phrase in exit_phrases):
            print("[PipecatAgent] Exit command detected")
            if self.on_final_transcript:
                self.on_final_transcript(user_text)
            # Notify to stop conversation
            asyncio.create_task(self._handle_exit())
            return

        # ── Check for function calling commands ──────────────────────
        # Simple keyword-based function routing for common commands
        func_result = await self._try_function_calling(user_text)
        if func_result:
            # A function was executed — the result text will be spoken
            system_msg = (
                f"You are BARQ, a desktop assistant. "
                f"The user said: '{user_text}'. "
                f"A function was executed with this result: {json.dumps(func_result)}. "
                f"Briefly tell the user what happened."
            )
        else:
            # Build system prompt
            from .function_executor import get_function_schemas
            schemas = get_function_schemas()
            tools_desc = ""
            if schemas:
                tool_names = [s["name"] for s in schemas]
                tools_desc = (
                    f"\n\nAvailable tools you can trigger via BARQ: "
                    f"{', '.join(tool_names)}. "
                    f"BARQ automatically routes command-like requests to the right tool."
                )

            system_msg = (
                "You are BARQ, an AI desktop assistant. "
                "Respond concisely in 1-3 sentences. "
                "Do not use markdown, code blocks, or special formatting. "
                "Speak naturally." + tools_desc
            )

        # Call Ollama via httpx (no aiohttp — avoids event loop conflicts)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.ollama_host}/api/chat",
                    json={
                        "model": self.ollama_model,
                        "messages": [
                            {"role": "system", "content": system_msg},
                            {"role": "user", "content": user_text},
                        ],
                        "stream": False,
                        "options": {
                            "temperature": 0.7,
                            "num_predict": 256,
                        },
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                reply = data.get("message", {}).get("content", "")

            if not reply:
                reply = "I understand. Let me know how I can help."

            print(f"[PipecatAgent] LLM response: '{reply[:80]}...'")

            # Notify agent text for captions
            if self.on_agent_text:
                self.on_agent_text(reply)

            # Synthesize speech
            await self._synthesize_and_play(reply)

        except httpx.TimeoutException:
            print("[PipecatAgent] LLM timed out")
            await self._synthesize_and_play("Sorry, I'm thinking too long. Please try again.")
        except Exception as e:
            print(f"[PipecatAgent] LLM error: {e}")

    async def _try_function_calling(self, text: str) -> Optional[dict]:
        """Check if the user text matches a function call pattern.

        Returns the function result dict if a function was executed,
        or None if no match.
        """
        text_lower = text.lower().strip()

        # Map of keywords → function name + argument extractor
        function_map: dict[str, tuple[str, dict]] = {
            "minimize": ("minimize_window", {}),
            "maximize": ("maximize_window", {}),
            "screenshot": ("take_screenshot", {}),
            "screenshot to": ("take_screenshot", {}),
            "open file": ("open_file", {}),
        }

        for keyword, (fn_name, default_args) in function_map.items():
            if keyword in text_lower:
                try:
                    from .function_executor import execute_function
                    result = await asyncio.wait_for(
                        execute_function(fn_name, default_args),
                        timeout=15.0,
                    )
                    return result
                except Exception as e:
                    return {"status": "error", "detail": str(e)}

        return None

    async def _handle_exit(self):
        """Handle conversation exit."""
        if self.on_agent_done_speaking:
            self.on_agent_done_speaking()
        # Let the conversation listener handle the stop
        from .conversation_listener import get_listener
        listener = get_listener()
        if listener:
            await listener.stop_conversation()

    # ── TTS (Edge-TTS / Kokoro) ──────────────────────────────────────

    def cancel_current_tts(self):
        """Cancel any in-progress TTS synthesis (for barge-in).

        Interrupts Kokoro synthesis if it's running, so CPU/GPU
        resources aren't wasted on speech that won't be played.
        """
        if self._kokoro_engine is not None:
            self._kokoro_engine.cancel()

    async def speak_text(self, text: str) -> None:
        """Synthesize and play a spoken phrase through the TTS output.

        Used for wake greetings and proactive speech outside the normal
        conversation turn-taking loop.  Feeds audio directly into the
        output ring buffer so it plays through the speakers.

        Args:
            text: The text to speak aloud (e.g. "How can I help you?").
        """
        if not self._output_stream:
            print("[PipecatAgent] Cannot speak — output stream not initialized")
            return

        self._agent_is_speaking = True
        if self.on_agent_speaking:
            self.on_agent_speaking()

        try:
            await self._synthesize_edge_tts(text)
        except Exception as e:
            print(f"[PipecatAgent] speak_text error: {e}")

        # Wait for the ring buffer to drain (playback to finish)
        wait_start = time.time()
        while len(self._output_ring_buffer) > 0 and time.time() - wait_start < 10.0:
            await asyncio.sleep(0.05)

        self._agent_is_speaking = False
        if self.on_agent_done_speaking:
            self.on_agent_done_speaking()

        print("[PipecatAgent] Greeting done speaking")

    async def _synthesize_and_play(self, text: str):
        """Convert text to speech and feed audio to output ring buffer.

        Uses the configured TTS backend (``self.tts_backend``).
        """
        print(f"[PipecatAgent] Speaking ({self.tts_backend}): '{text[:60]}...'")

        # Notify agent speaking
        self._agent_is_speaking = True
        if self.on_agent_speaking:
            self.on_agent_speaking()

        if self.tts_backend == TTS_BACKEND_KOKORO:
            try:
                await self._synthesize_kokoro(text)
            except Exception as e:
                print(f"[PipecatAgent] Kokoro TTS failed ({e}), falling back to edge-tts...")
                try:
                    await self._synthesize_edge_tts(text)
                except Exception as e2:
                    print(f"[PipecatAgent] Edge-TTS fallback also failed: {e2}")
        else:
            # Default: edge-tts
            try:
                await self._synthesize_edge_tts(text)
            except Exception as e:
                print(f"[PipecatAgent] edge-tts failed ({e}), trying Kokoro fallback...")
                try:
                    await self._synthesize_kokoro(text)
                except Exception as e2:
                    print(f"[PipecatAgent] All TTS methods failed: {e2}")

        # Notify agent done speaking
        self._last_agent_done_speaking_at = time.time()
        self._agent_is_speaking = False
        if self.on_agent_done_speaking:
            self.on_agent_done_speaking()

        print("[PipecatAgent] Done speaking")

    async def _get_kokoro_engine(self):
        """Lazy-load the Kokoro TTS engine (uses singleton factory)."""
        if self._kokoro_engine is not None:
            return self._kokoro_engine
        from .kokoro_tts import get_kokoro_engine
        # Use the singleton factory — creates one engine, reused across agent lifetimes
        self._kokoro_engine = get_kokoro_engine(
            voice=self.tts_voice,
            speed=self.tts_speed,
        )
        if not self._kokoro_engine.is_loaded:
            ok = await self._kokoro_engine.load()
            if ok:
                print(f"[PipecatAgent] Kokoro TTS ready (voice={self.tts_voice})")
            else:
                print("[PipecatAgent] Kokoro TTS failed to load")
                self._kokoro_engine = None
        return self._kokoro_engine

    async def _synthesize_kokoro(self, text: str):
        """Use Kokoro-82M neural TTS for speech synthesis (fully offline)."""
        engine = await self._get_kokoro_engine()
        if engine is None:
            raise RuntimeError("Kokoro engine not available")

        audio, sample_rate = await engine.synthesize(text)
        if audio.size == 0:
            print("[PipecatAgent] Kokoro returned empty audio")
            return

        # Kokoro already produces 24kHz float32 PCM — feed directly
        self._feed_pcm_to_buffer(audio, sample_rate)

    @staticmethod
    def _sanitize_text_for_tts(text: str) -> str:
        """Strip or replace non-ASCII characters for edge-tts.

        Edge-TTS uses the system's default encoding (cp1252 on Windows)
        for HTTP requests, which can't handle Unicode smart quotes,
        em-dashes, or other extended characters. This function replaces
        common problematic characters with ASCII equivalents.

        Args:
            text: Raw input text (may contain Unicode characters).

        Returns:
            Cleaned text safe for edge-tts and console printing.
        """
        replacements = {
            "\u2018": "'",  # Left single quotation mark → ASCII apostrophe
            "\u2019": "'",  # Right single quotation mark → ASCII apostrophe
            "\u201c": '"',  # Left double quotation mark → ASCII quote
            "\u201d": '"',  # Right double quotation mark → ASCII quote
            "\u2013": "-",  # En dash → hyphen
            "\u2014": "--", # Em dash → double hyphen
            "\u2026": "...",# Ellipsis → three dots
            "\u00a0": " ",  # Non-breaking space → space
            "\u00b0": " degrees",  # Degree symbol
        }
        for old, new in replacements.items():
            text = text.replace(old, new)

        # Encode to ASCII, dropping anything that can't be mapped
        text = text.encode("ascii", "ignore").decode("ascii")

        return text.strip()

    async def _synthesize_edge_tts(self, text: str):
        """Use edge-tts for speech synthesis."""
        import edge_tts
        import io

        # Sanitize text to avoid UnicodeEncodeError on Windows cp1252
        text = self._sanitize_text_for_tts(text)
        if not text:
            print("[PipecatAgent] edge-tts text empty after sanitization")
            return

        communicate = edge_tts.Communicate(text, voice="en-US-AriaNeural")
        audio_data = b""

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data += chunk["data"]

        if not audio_data:
            print("[PipecatAgent] edge-tts returned empty audio")
            return

        # edge-tts returns MP3 bytes by default. We need PCM.
        # Decode MP3 to PCM — try pydub first (more reliable on Windows),
        # fall back to av (av on Windows sometimes decodes to all zeros).
        pcm_float = None
        sample_rate = 24000

        # Priority 1: pydub (more reliable on Windows)
        try:
            from pydub import AudioSegment

            # Try to find ffmpeg at runtime in case it's installed but not on PATH
            _ffmpeg_path = _find_ffmpeg()
            if _ffmpeg_path:
                AudioSegment.converter = _ffmpeg_path
                AudioSegment.ffmpeg = _ffmpeg_path

            seg = AudioSegment.from_file(io.BytesIO(audio_data), format="mp3")
            raw = seg.raw_data
            pcm_int16 = np.frombuffer(raw, dtype=np.int16)
            pcm_float = pcm_int16.astype(np.float32) / 32768.0
            sample_rate = seg.frame_rate
            print(f"[PipecatAgent] Decoded MP3 via pydub: {len(pcm_float)} samples at {sample_rate}Hz")
        except Exception:
            # pydub may fail at runtime if ffmpeg isn't on PATH — fall through to av
            print("[PipecatAgent] pydub unavailable or failed, trying av...")

        # Priority 2: av (fallback if pydub not available or failed)
        if pcm_float is None:
            try:
                import av

                input_file = av.open(io.BytesIO(audio_data))
                pcm_chunks: list[np.ndarray] = []
                last_shape = "unknown"

                for frame in input_file.decode(audio=0):  # type: ignore[union-attr]
                    arr = frame.to_ndarray()  # shape varies: (C, T), (C, 1, T), or (C, T, 1)
                    last_shape = str(arr.shape)

                    # Flatten to 1D while handling any shape:
                    #   (1, T)       → channels-first mono: take axis 0
                    #   (2, T)       → channels-first stereo: average
                    #   (T, 1), (T,) → already planar: ravel
                    #   (1, 1, T)    → 3D: ravel
                    if arr.ndim == 1:
                        flat = arr
                    elif arr.ndim == 2:
                        if arr.shape[0] <= 2 and arr.shape[1] >= arr.shape[0]:
                            flat = np.mean(arr.astype(np.float64), axis=0)
                        else:
                            flat = arr[:, 0] if arr.shape[1] <= 2 else arr.ravel()
                    elif arr.ndim == 3:
                        # (C, 1, T) → squeeze to (C, T) then average
                        flat = np.mean(arr.squeeze().astype(np.float64), axis=0) if arr.squeeze().ndim == 2 else arr.ravel()
                    else:
                        flat = arr.ravel()

                    pcm_chunks.append(flat.astype(np.float32) / 32768.0)

                if pcm_chunks:
                    pcm_float = np.concatenate(pcm_chunks)
                    peak_check = float(np.max(np.abs(pcm_float)))
                    print(f"[PipecatAgent] Decoded MP3 via av: {len(pcm_float)} samples, shape={last_shape} (peak={peak_check:.4f})")
                    if peak_check < 0.001:
                        _safe_print(f"[PipecatAgent] ⚠️ av decoded audio is silent (shape={last_shape})")
                else:
                    print("[PipecatAgent] av decoded 0 PCM chunks")
            except ImportError:
                print("[PipecatAgent] av not installed either — no MP3 decoder available")
            except Exception as e:
                print(f"[PipecatAgent] av decode error: {e}")

        if pcm_float is None:
            print("[PipecatAgent] No MP3 decoder available — need pydub or av")
            return

        self._feed_pcm_to_buffer(pcm_float, sample_rate)

    def _feed_pcm_to_buffer(self, pcm_float: np.ndarray, sample_rate: int):
        """Resample PCM to TTS_SAMPLE_RATE and feed to the ring buffer."""
        # Simple nearest-neighbor resample if needed
        if sample_rate != TTS_SAMPLE_RATE:
            ratio = TTS_SAMPLE_RATE / sample_rate
            target_len = int(len(pcm_float) * ratio)
            indices = np.round(np.linspace(0, len(pcm_float) - 1, target_len)).astype(np.int32)
            pcm_float = pcm_float[indices]

        # Diagnostic: check audio amplitude — if near zero, something went wrong
        peak = float(np.max(np.abs(pcm_float)))
        rms_val = float(np.sqrt(np.mean(pcm_float ** 2)))
        print(f"[PipecatAgent] Feeding {len(pcm_float)} samples to buffer (peak={peak:.4f}, rms={rms_val:.4f})")

        if peak < 0.001:
            _safe_print(f"[PipecatAgent] ⚠️ Audio appears silent (peak={peak:.4f}) — possible decode issue")
            return  # Don't feed silent audio to the buffer

        # Feed to ring buffer
        self._output_ring_buffer.extend(pcm_float)

        # Dispatch to callback
        if self.on_audio_chunk:
            self.on_audio_chunk(pcm_float, TTS_SAMPLE_RATE)

        # Rate-limited capacity warning
        usage = len(self._output_ring_buffer) / (self._output_ring_buffer.maxlen or 1)
        if usage > 0.9:
            now = time.time()
            if now - self._output_log_timer > 1.0:
                self._output_log_timer = now
                print(f"[PipecatAgent] Ring buffer at {usage:.0%} capacity")

    # ── Process Loop ─────────────────────────────────────────────────

    async def _process_loop(self):
        """Background loop — handles periodic tasks."""
        while self._running:
            await asyncio.sleep(0.5)

        print("[PipecatAgent] Process loop ended")


def get_listener():
    """Get the active ConversationListener singleton (imported here
    to avoid circular imports at module level)."""
    from .conversation_listener import get_listener as _get_listener
    return _get_listener()
