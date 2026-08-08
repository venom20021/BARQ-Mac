"""
Deepgram Voice Agent for BARQ.

Manages a full STT → LLM → TTS pipeline via Deepgram's managed Voice Agent API.
Connects via WebSocket to wss://agent.deepgram.com/, sends the Voice Agent
configuration, streams microphone audio, and plays back responses.

Architecture:
  Wake word (local Vosk) → Deepgram Voice Agent (STT + LLM + TTS) → Audio out

The Voice Agent handles:
  - Speech-to-text (Deepgram flux-general-en)
  - LLM reasoning (Google Gemini 3.1 Flash Lite)
  - Text-to-speech (Deepgram aura-2-odysseus-en)

BARQ only handles:
  - Wake word detection (Vosk, local)
  - Audio capture from mic → agent
  - Audio playback from agent → speakers
"""

import asyncio
import collections
import copy
import json
import queue
import time
from typing import Any, Optional, cast

import numpy as np

from config import get_settings

from .agent_base import VoiceAgentBase
from .function_executor import get_function_schemas


# ── Voice Agent Settings (from user's config) ──────────────────────────

AGENT_SETTINGS = {
    "type": "Settings",
    "audio": {
        "input": {
            "encoding": "linear16",
            "sample_rate": 48000,
        },
        "output": {
            "encoding": "linear16",
            "sample_rate": 24000,
            "container": "none",
        },
    },
    "agent": {
        "listen": {
            "provider": {
                "type": "deepgram",
                "version": "v2",
                "model": "flux-general-en",
            },
        },
        "think": {
            "provider": {
                "type": "google",
                "model": "gemini-3.1-flash-lite",
            },
            "prompt": (
                "#Role\n"
                "You are BARQ, an advanced, real-time AI desktop assistant integrated "
                "into the user's local operating system.\n\n"
                "#General Guidelines\n"
                "-Be highly responsive, sharp, and capable.\n"
                "-Keep most responses to 1–2 sentences. You are speaking aloud, "
                "so do not use markdown, code blocks, or special formatting.\n"
                "-Speak in a natural, conversational, and fast-paced tone.\n"
                "-Do not waste time with pleasantries unless greeted.\n\n"
                "#Call Flow Objective\n"
                "-When activated, provide quick, accurate answers regarding software "
                "development, desktop automation, or general queries.\n"
                "-If the request involves complex backend systems or data pipelines, "
                "summarize the technical concepts clearly without reading out literal code.\n"
                "-If the request is unclear, ask a quick clarifying question.\n\n"
                "#Barge-In Context\n"
                "-Expect the user to interrupt you frequently. If they do, immediately "
                "stop your current thought, accept the new context, and pivot gracefully "
                "without apologizing."
            ),
        },
        "speak": {
            "provider": {
                "type": "deepgram",
                "model": "aura-2-odysseus-en",
            },
        },
        "greeting": "BARQ system online. What do you need?",
    },
}

# Deepgram Voice Agent WebSocket endpoint
# Use /v1/agent/converse for the managed voice pipeline
AGENT_WS_URL = "wss://agent.deepgram.com/v1/agent/converse"

# Audio capture settings (mic → agent requires 48kHz)
AGENT_INPUT_SAMPLE_RATE = 48000
AGENT_INPUT_BLOCK_SIZE = 2400  # 50ms at 48kHz

# Audio playback settings (agent output is 24kHz raw PCM)
AGENT_OUTPUT_SAMPLE_RATE = 24000


class DeepgramVoiceAgent(VoiceAgentBase):
    """Manages a Deepgram Voice Agent WebSocket session.

    Creates a connection to Deepgram's managed voice pipeline,
    streams microphone audio, and plays back responses.

    Usage:
        agent = DeepgramVoiceAgent(api_key=...)
        await agent.connect()
        await agent.start_conversation()
        # ... conversation runs until stop is called ...
        await agent.stop()
    """

    _loop_ex_handler_installed: bool = False

    @classmethod
    def _suppress_proactor_assertion_error(cls, loop, context):
        """Suppress the benign _ProactorBaseWritePipeTransport AssertionError.

        This is a known Windows asyncio issue: when a WebSocket transport is
        closed while a write is still pending, the completion callback fires
        with a mismatched future, causing:
            AssertionError in _ProactorBaseWritePipeTransport._loop_writing()

        The error is harmless but spams stderr. We suppress it here.
        """
        exc = context.get("exception")
        if isinstance(exc, AssertionError):
            msg = context.get("message", "")
            if "_loop_writing" in msg or "_ProactorBaseWritePipeTransport" in msg:
                return  # Suppress — known benign Windows asyncio behavior
        loop.default_exception_handler(context)

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.settings = get_settings()
        self._ws: Optional[Any] = None  # websocket connection
        self._running = False
        self._settings_applied = asyncio.Event()
        # Wait for agent greeting to finish before sending mic audio
        self._can_send_audio = asyncio.Event()
        self._input_stream: Optional[Any] = None  # sd.InputStream
        self._send_task: Optional[asyncio.Task] = None
        self._output_stream: Optional[Any] = None  # sd.OutputStream (callback-based)
        self._captured_text: list[str] = []

        # Thread-safe audio queues
        self._audio_queue: queue.Queue = queue.Queue(maxsize=500)

        # Ring buffer for output audio playback
        self._output_ring_buffer: collections.deque = collections.deque(maxlen=72000)  # 3s at 24kHz

        # Callbacks — wired by the conversation listener
        # (These default to None from VoiceAgentBase)
        # self.on_interim_transcript = None  -- inherited from base class
        # self.on_final_transcript = None
        # self.on_agent_speaking = None
        # self.on_agent_done_speaking = None
        # self.on_audio_chunk = None
        # self.on_agent_text = None

        # Function call tracking (dedup)
        self._pending_function_calls: set[str] = set()

        # UserStartedSpeaking debounce
        self._last_user_spoke_at: float = 0.0

        # Post-speech lockout
        self._last_agent_done_speaking_at: float = 0.0

        # Rate-limit timer for ring buffer full log messages
        self._output_log_timer: float = 0.0

        # Agent speaking state: when True, mic audio is NOT sent to Deepgram
        self._agent_is_speaking: bool = False

        # Auth failure flag — fast-fail subsequent connect() attempts
        self._auth_failure: bool = False

        # Audio energy gate threshold (int16 RMS)
        self._energy_threshold: int = 250

        # Rate-limit timer for low-energy drop log messages
        self._energy_log_timer: float = 0.0

        # Rate-limit timer for sample rate mismatch diagnostic logs
        self._sample_rate_log_timer: float = 0.0

        # Mic cooldown task
        self._mic_cooldown_task: Optional[asyncio.Task] = None

        # Mic tail-timer delay — the mic reopens this long after the agent's
        # audio stops flowing (even if AgentAudioDone never arrives).
        self._mic_tail_delay: float = 1.2

        # Install the benign error suppressor once (class-level guard)
        if not DeepgramVoiceAgent._loop_ex_handler_installed:
            try:
                loop = asyncio.get_event_loop()
                loop.set_exception_handler(
                    DeepgramVoiceAgent._suppress_proactor_assertion_error
                )
                DeepgramVoiceAgent._loop_ex_handler_installed = True
                print("[DeepgramAgent] Installed benign AssertionError suppressor")
            except RuntimeError:
                pass  # No event loop in this thread yet — installed in connect()

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Mic Energy Threshold (RMS voice-activity gate) ──────────────

    def set_energy_threshold(self, value: int) -> None:
        """Update the mic RMS energy gate threshold at runtime.

        Clamped to the supported [50, 2000] range. Takes effect on the
        next audio callback — no reconnect needed.
        """
        clamped = max(50, min(2000, int(value)))
        self._energy_threshold = clamped
        print(f"[DeepgramAgent] Mic RMS energy threshold set to {clamped}")

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
                print(f"[DeepgramAgent] Applied wake-time adaptive threshold {pending}")
                return
        except Exception as e:
            print(f"[DeepgramAgent] Adaptive threshold consume failed (non-fatal): {e}")

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

    async def _validate_api_key(self) -> bool:
        """Validate the Deepgram API key via REST API before WebSocket connection.

        Deepgram's Voice Agent WebSocket endpoint doesn't send an explicit
        auth error — it just silently drops the connection or never sends
        a Welcome message if the key is invalid. This leads to misleading
        "Timeout during connect handshake" logs that look like network issues.

        This method checks the key against the REST API first so we can
        print a clear diagnostic message before attempting the WebSocket.

        Once an auth failure is detected, sets ``self._auth_failure = True``
        so subsequent calls to ``connect()`` return ``False`` immediately
        without re-validating or retrying.

        Returns:
            True if the key appears valid.
            False if the key is empty or rejected by the API.
        """
        if not self.api_key:
            print("[DeepgramAgent] " + "=" * 70)
            print("[DeepgramAgent] [FAIL] NO DEEPGRAM API KEY CONFIGURED")
            print("[DeepgramAgent] " + "=" * 70)
            print("[DeepgramAgent] Set DEEPGRAM_API_KEY in your .env file.")
            print("[DeepgramAgent] Get a key at: https://console.deepgram.com/")
            self._auth_failure = True
            return False

        try:
            import httpx
        except ImportError:
            print("[DeepgramAgent] httpx not available — cannot validate API key, will try WebSocket")
            return True

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.deepgram.com/v1/projects",
                    headers={"Authorization": f"Token {self.api_key}"},
                )
                if resp.status_code == 200:
                    print("[DeepgramAgent] [OK] Deepgram API key validated")
                    return True
                elif resp.status_code == 401:
                    print("[DeepgramAgent] " + "=" * 70)
                    print("[DeepgramAgent] [FAIL] DEEPGRAM API KEY REJECTED (401 Unauthorized)")
                    print("[DeepgramAgent] " + "=" * 70)
                    print("[DeepgramAgent] The API key in your .env file is invalid or revoked.")
                    print("[DeepgramAgent] Update DEEPGRAM_API_KEY in .env or get a new key at:")
                    print("[DeepgramAgent]   https://console.deepgram.com/")
                    self._auth_failure = True
                    return False
                else:
                    print(f"[DeepgramAgent] Deepgram API check returned {resp.status_code}, will try WebSocket anyway")
                    return True  # Non-auth errors might be transient
        except httpx.TimeoutException:
            print("[DeepgramAgent] Deepgram REST API unreachable — will try WebSocket connection directly")
            return True
        except Exception as e:
            print(f"[DeepgramAgent] Deepgram API check failed ({e}) — will try WebSocket anyway")
            return True

    async def connect(self) -> bool:
        """Connect to the Deepgram Voice Agent WebSocket.

        Protocol:
        1. Validate API key via REST (clear error if invalid)
        2. Open WebSocket connection with auth subprotocol
        3. Receive "Welcome" message from server
        4. Send Settings configuration
        5. Receive "SettingsApplied" confirmation

        Returns:
            True if connected and configured successfully.
            False on any protocol or connection error.
        """
        # ── Step 0a: Fast-fail on known auth failure ────────────────
        # If a previous connect() attempt already detected an invalid key,
        # return immediately without re-validating. This prevents the
        # conversation listener from wasting 5 retries with exponential
        # backoff (1s + 3s + 9s + 27s + 81s = 121s).
        if self._auth_failure:
            return False

        # ── Load mic RMS energy threshold (DB/env) before streaming ──
        try:
            await self._load_energy_threshold()
        except Exception as e:
            print(f"[DeepgramAgent] Energy threshold load failed (non-fatal): {e}")

        # Ensure the benign error suppressor is installed (in case __init__
        # was called before the event loop existed)
        if not DeepgramVoiceAgent._loop_ex_handler_installed:
            try:
                asyncio.get_event_loop().set_exception_handler(
                    DeepgramVoiceAgent._suppress_proactor_assertion_error
                )
                DeepgramVoiceAgent._loop_ex_handler_installed = True
                print("[DeepgramAgent] Installed benign AssertionError suppressor (on connect)")
            except RuntimeError:
                pass

        # ── Step 0b: Validate API key before WebSocket attempt ──────
        if not await self._validate_api_key():
            return False

        try:
            import websockets as ws_module

            # ── Step 0c: Pre-resolve DNS to IPv4 ─────────────────────
            # Windows sometimes resolves agent.deepgram.com to an IPv6
            # NAT64 address that is unreachable, causing the WebSocket
            # handshake to time out after 20+ seconds.  We force IPv4
            # by creating a pre-connected TCP socket and passing it to
            # websockets.connect().
            import socket
            try:
                addrs = socket.getaddrinfo(
                    "agent.deepgram.com", 443,
                    socket.AF_INET, socket.SOCK_STREAM,
                )
                host_ip = addrs[0][4][0]
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((host_ip, 443))
                sock.settimeout(None)
                print(f"[DeepgramAgent] IPv4 TCP connected ({host_ip})")
                self._ws = await ws_module.connect(
                    AGENT_WS_URL,
                    subprotocols=cast(Any, ["token", self.api_key]),
                    sock=sock,
                    server_hostname="agent.deepgram.com",
                )
            except Exception as dns_err:
                # Close the pre-connected socket if it was created
                # (prevents socket leak on Windows when websockets
                # connect raises after TCP connect succeeds)
                try:
                    sock.close()  # type: ignore[name-defined]
                except NameError:
                    pass
                # Fallback — let websockets handle DNS
                print(f"[DeepgramAgent] IPv4 pre-connect failed ({dns_err}), falling back to default DNS")
                self._ws = await ws_module.connect(
                    AGENT_WS_URL,
                    subprotocols=cast(Any, ["token", self.api_key]),
                )
            print("[DeepgramAgent] WebSocket connected")

            # ── Step 1: Receive Welcome message ─────────────────────
            # The server sends a Welcome message immediately after
            # connection to confirm the protocol is ready.
            welcome = await asyncio.wait_for(
                self._ws.recv(), timeout=10.0
            )
            if isinstance(welcome, str):
                data = json.loads(welcome)
                if data.get("type") != "Welcome":
                    print(f"[DeepgramAgent] Expected Welcome, got: {data.get('type', 'unknown')}")
                    return False
                print("[DeepgramAgent] Welcome received — protocol ready")
            else:
                print("[DeepgramAgent] Expected text Welcome, got binary")
                return False

            # ── Step 2: Inject function schemas into settings ──────
            settings_payload: dict = copy.deepcopy(AGENT_SETTINGS)
            function_schemas = get_function_schemas()
            if function_schemas:
                think_cfg = settings_payload.setdefault("agent", {}).setdefault("think", {})
                think_cfg["functions"] = function_schemas
                print(f"[DeepgramAgent] Injected {len(function_schemas)} function schemas into settings")

            # ── Step 3: Send Settings configuration ─────────────────
            settings_json = json.dumps(settings_payload)
            await self._ws.send(settings_json)
            print("[DeepgramAgent] Settings sent — waiting for confirmation...")

            # ── Step 4: Wait for SettingsApplied confirmation ───────
            response = await asyncio.wait_for(
                self._ws.recv(), timeout=15.0
            )
            if isinstance(response, str):
                data = json.loads(response)
                if data.get("type") == "SettingsApplied":
                    print("[DeepgramAgent] Settings applied — agent ready")
                    self._settings_applied.set()
                    return True
                else:
                    err_desc = data.get("description", data.get("message", "No details"))
                    print(f"[DeepgramAgent] Settings rejected: {data.get('type', 'unknown')} — {err_desc}")
                    return False
            else:
                print("[DeepgramAgent] Expected text SettingsApplied, got binary")
                return False

        except asyncio.TimeoutError:
            print("[DeepgramAgent] " + "=" * 70)
            print("[DeepgramAgent] [FAIL] TIMEOUT during WebSocket handshake with Deepgram")
            print("[DeepgramAgent] " + "=" * 70)
            print("[DeepgramAgent] The WebSocket connection to agent.deepgram.com succeeded,")
            print("[DeepgramAgent] but no Welcome message was received within 10 seconds.")
            print("[DeepgramAgent] This typically means:")
            print("[DeepgramAgent]   1. The API key was rejected (despite the REST check)")
            print("[DeepgramAgent]   2. Deepgram Voice Agent API is temporarily unavailable")
            print("[DeepgramAgent]   3. A firewall or proxy is interfering with the WebSocket protocol")
            return False
        except Exception as e:
            print(f"[DeepgramAgent] Connection failed: {e}")
            return False

    async def start_conversation(self, audio_device: Optional[int] = None):
        """Start streaming microphone audio and receiving responses.

        Runs two concurrent tasks:
        1. Audio capture → send as Media messages (48kHz)
        2. Receive messages → play audio / dispatch callbacks

        Args:
            audio_device: sounddevice input device index. None = default.
        """
        if not self._running:
            self._running = True
            self._send_task = asyncio.create_task(
                self._audio_send_loop(audio_device)
            )
            asyncio.create_task(self._receive_loop())

    async def stop(self):
        """Stop the conversation and close the WebSocket connection."""
        self._running = False

        # Cancel the send task
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
            self._send_task = None

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

        # Clear ring buffer
        self._output_ring_buffer.clear()

        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._settings_applied.clear()
        self._can_send_audio.clear()
        self._pending_function_calls.clear()
        self._flush_audio_queue("stop")
        self._agent_is_speaking = False
        # Cancel any pending mic tail-timer
        self._cancel_mic_tail_timer()
        print("[DeepgramAgent] Disconnected")

    # ── Queue Management ────────────────────────────────────────────

    def _flush_audio_queue(self, reason: str = ""):
        """Discard all queued mic audio chunks.

        When the agent starts speaking, any chunks already in the queue
        contain echo from the agent's speech. If sent later (after cooldown),
        they'll trigger a false UserStartedSpeaking on Deepgram's end,
        causing the echo loop.

        Args:
            reason: Optional description for the log message.
        """
        flushed = 0
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                flushed += 1
            except queue.Empty:
                break
        if flushed:
            tag = f" ({reason})" if reason else ""
            print(f"[DeepgramAgent] Flushed {flushed} stale chunks{tag}")

    # ── Audio Send Loop (mic → agent) ──────────────────────────────

    def _audio_capture_callback(self, indata, frames, time_info, status):
        """sounddevice InputStream callback — runs on a background thread.

        Computes RMS energy of the audio chunk. If below the energy
        threshold, drops the chunk (low-level echo / silence suppression).
        Only puts audio into the queue if it exceeds the threshold.

        Args:
            indata: numpy array of captured audio (int16)
            frames: number of frames
            time_info: timing info (unused)
            status: PortAudio status flags
        """
        if status:
            print(f"[DeepgramAgent] Audio callback status: {status}")

        # ── Guard 1: Agent is speaking — drop ALL audio ─────────────
        # This is the PRIMARY echo defense. While the agent is speaking,
        # no mic audio is sent to Deepgram, so echo can't reach the VAD.
        if self._agent_is_speaking:
            return

        # ── Guard 2: Energy gate — drop quiet chunks ────────────────
        # SECONDARY defense: suppress low-level residual echo that might
        # still be in the room after the agent stops speaking.
        # Uses float32 to avoid int16 overflow during squaring.
        rms = int(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        if rms < self._energy_threshold:
            now = time.time()
            if now - self._energy_log_timer > 2.0:
                self._energy_log_timer = now
                print(f"[DeepgramAgent] Low-energy chunk dropped (RMS={rms}, threshold={self._energy_threshold})")
            return

        try:
            self._audio_queue.put_nowait(indata.copy())
        except queue.Full:
            print("[DeepgramAgent] Audio queue full — dropping chunk")

    def _output_stream_callback(self, outdata, frames, time_info, status):
        """sounddevice OutputStream callback — reads from ring buffer.

        Runs on a background PortAudio thread. Reads float32 samples
        from the ring buffer (deque). If the buffer is empty, fills
        with silence (zeros) to prevent underflow clicks.

        Args:
            outdata: numpy array to fill with audio data (float32, shape=(frames, channels))
            frames: number of frames requested
            time_info: timing info (unused)
            status: PortAudio status flags
        """
        if status:
            print(f"[DeepgramAgent] Output callback status: {status}")

        available = len(self._output_ring_buffer)
        outdata.fill(0.0)  # Pre-fill with silence

        if available >= frames:
            # Enough samples — fill directly from ring buffer
            for i in range(frames):
                try:
                    outdata[i, 0] = self._output_ring_buffer.popleft()
                except IndexError:
                    break
        elif available > 0:
            # Partial fill — use what's available, rest is silence
            for i in range(available):
                try:
                    outdata[i, 0] = self._output_ring_buffer.popleft()
                except IndexError:
                    break

    async def _audio_send_loop(self, device: Optional[int] = None):
        """Capture microphone audio via callback-based InputStream and send as raw PCM.

        Uses a callback-based InputStream to avoid blocking the asyncio event loop.
        The callback runs on a background thread and puts data into a thread-safe
        queue. The async task reads from the queue and sends via WebSocket.

        Waits for:
        1. SettingsApplied (handshake complete)
        2. Agent greeting to finish (ConversationText or timeout)

        Then starts the output playback thread and mic capture.
        """
        # Step 1: Wait for SettingsApplied
        await self._settings_applied.wait()

        # Step 2: Wait for greeting to finish
        print("[DeepgramAgent] Waiting for greeting to finish before starting mic...")
        try:
            await asyncio.wait_for(self._can_send_audio.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            print("[DeepgramAgent] Timeout waiting for greeting — starting mic anyway")
            self._can_send_audio.set()

        import sounddevice as sd

        # Resolve input device
        from .audio_device import resolve_input_device
        if device is None:
            device = resolve_input_device(self.settings.audio_input_device)

        # Start output playback stream (callback-based, gapless)
        from .audio_device import resolve_output_device
        output_device = resolve_output_device(self.settings.audio_output_device)
        self._output_stream = sd.OutputStream(
            device=output_device,
            samplerate=AGENT_OUTPUT_SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._output_stream_callback,
            blocksize=480,  # 20ms at 24kHz
        )
        self._output_stream.start()
        print("[DeepgramAgent] Output stream started — gapless playback")

        try:
            # Open callback-based InputStream (non-blocking, runs on background thread)
            self._input_stream = sd.InputStream(
                device=device,
                samplerate=AGENT_INPUT_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=AGENT_INPUT_BLOCK_SIZE,
                callback=self._audio_capture_callback,
            )
            self._input_stream.start()
            print("[DeepgramAgent] Mic streaming started — callback mode")

            # Async loop: read from thread-safe queue and send via WebSocket
            while self._running:
                try:
                    # Check BEFORE consuming from queue — prevents stale
                    # cross-boundary chunks from reaching Deepgram while
                    # the agent is speaking or in cooldown.
                    if self._agent_is_speaking:
                        await asyncio.sleep(0.05)
                        continue

                    # Consume one chunk from the callback thread
                    data = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self._audio_queue.get(timeout=0.1),
                    )
                    audio_bytes = data.tobytes()
                    if self._ws is not None:
                        await self._ws.send(audio_bytes)
                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"[DeepgramAgent] Send error: {e}")
                    break

        except Exception as e:
            print(f"[DeepgramAgent] Audio capture error: {e}")
        finally:
            self._running = False
            print("[DeepgramAgent] Audio send loop ended")

    # ── Receive Loop (agent → us) ──────────────────────────────────

    async def _receive_loop(self):
        """Receive messages from the Voice Agent WebSocket.

        Handles:
        - Binary messages → Audio samples (raw PCM 24kHz)
        - JSON messages → UserStartedSpeaking, AgentThinking, etc.

        IMPORTANT: The server sends messages in this order after connect:
          1. Welcome (text, handled in connect())
          2. SettingsApplied (text, handled in connect())
          3. BINARY — greeting audio chunks (960 bytes each at 24kHz)
          4. ConversationText (text) — "Hello! How may I help you?"
          5. History (text)
          6. BINARY — more audio
          7. ... (listening for user input)

        We must NOT send any text messages back except:
          - KeepAlive (only after 30s of inactivity, which should never happen)
        """
        if not self._ws:
            return

        try:
            while self._running:
                try:
                    message = await asyncio.wait_for(
                        self._ws.recv(), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    print("[DeepgramAgent] Receive timeout — connection may be idle, continuing")
                    continue

                if isinstance(message, bytes):
                    # Audio chunk — raw PCM at 24kHz
                    await self._handle_audio_bytes(message)
                else:
                    # JSON message
                    try:
                        data = json.loads(message)
                        await self._handle_json_message(data)
                    except json.JSONDecodeError:
                        print(f"[DeepgramAgent] Non-JSON message: {message[:100]}")

        except Exception as e:
            print(f"[DeepgramAgent] Receive loop error: {e}")
        finally:
            self._running = False
            print("[DeepgramAgent] Receive loop ended")

    async def _handle_audio_bytes(self, audio_bytes: bytes):
        """Process raw PCM audio from the agent.

        Appends samples to the ring buffer for gapless playback via
        the OutputStream callback. Dispatches on_audio_chunk callback
        for UI state updates.

        IMPORTANT: ANY incoming audio means the agent is speaking.
        We set `_agent_is_speaking = True` here on every chunk because
        `AgentStartedSpeaking` control messages may not fire reliably
        for every response segment. The binary audio is the ONLY reliable
        signal that the agent is producing sound.

        Args:
            audio_bytes: Raw linear16 PCM audio at 24kHz (container=none).
        """
        # ── Guard: audio data arriving = agent is speaking ──────────
        # This is set on EVERY audio chunk, not just AgentStartedSpeaking.
        # The control message might not fire for subtler response segments,
        # but the audio always arrives.
        self._agent_is_speaking = True
        # Flush stale mic chunks — audio from the agent means any queued
        # mic data is stale echo that should not be sent to Deepgram.
        self._flush_audio_queue("audio arriving")

        # Restart the mic tail-timer: reopen the mic shortly after the
        # agent's audio stops flowing, even if AgentAudioDone never fires.
        # (Without this, a missing AgentAudioDone would leave the mic muted
        # forever → BARQ "stops listening" after the greeting.)
        self._restart_mic_tail_timer()

        # Convert bytes to float32 numpy array
        raw_samples = np.frombuffer(audio_bytes, dtype=np.int16)
        actual_samples = len(raw_samples)

        # ── Detect & correct sample rate mismatch ──────────────────
        # Expected: 480 int16 samples per 20ms chunk at 24kHz (960 bytes)
        # If chunks are consistently a different size, Deepgram may be
        # sending audio at a different rate (e.g., 240 samples = 12kHz,
        # which played at 24kHz = 2x speed).
        #
        # Only resample if the chunk size is close to a known alternative
        # rate (within 10%). Random partial chunks at end-of-speech are
        # left as-is to avoid stretching artifacts.
        # Known alternative chunk sizes (240=12kHz, 120=6kHz, 960=48kHz)
        # These are the most common alternative sample rates for audio TTS.
        needs_resample = any(
            abs(actual_samples - alt) / alt < 0.10
            for alt in (240, 120, 960)
        ) if actual_samples != 480 else False

        if needs_resample:
            # Rate-limited diagnostic log (max once per 5 seconds)
            now = time.time()
            if now - self._sample_rate_log_timer > 5.0:
                self._sample_rate_log_timer = now
                print(f"[DeepgramAgent] Sample rate mismatch: got {actual_samples} samples (expected 480) — resampling")

            # Nearest-neighbor resample to 480 samples
            ratio = 480.0 / actual_samples
            indices = np.round(np.arange(480) / ratio).astype(np.int32)
            indices = np.clip(indices, 0, actual_samples - 1)
            raw_samples = raw_samples[indices]
        elif actual_samples != 480:
            # Partial tail chunk — leave as-is, don't stretch
            # Rate-limited log (max once per 5 seconds)
            now = time.time()
            if now - self._sample_rate_log_timer > 5.0:
                self._sample_rate_log_timer = now
                print(f"[DeepgramAgent] Partial chunk: {actual_samples} samples (left as-is)")

        # Cast to float32 for output stream
        pcm_array = raw_samples.astype(np.float32) / 32768.0

        # Append to ring buffer (thread-safe deque.extend)
        self._output_ring_buffer.extend(pcm_array)

        # Rate-limited log if ring buffer is near capacity
        ring_usage = len(self._output_ring_buffer) / (self._output_ring_buffer.maxlen or 1)
        if ring_usage > 0.9:
            now = time.time()
            if now - self._output_log_timer > 1.0:
                self._output_log_timer = now
                print(f"[DeepgramAgent] Output ring buffer at {ring_usage:.0%} capacity")

        # Dispatch to callback (conversation listener for state updates)
        if self.on_audio_chunk:
            self.on_audio_chunk(pcm_array, AGENT_OUTPUT_SAMPLE_RATE)

    async def _handle_json_message(self, data: dict):
        """Process a JSON control message from the agent.

        Args:
            data: Parsed JSON dict with message type.
        """
        msg_type = data.get("type", "")

        if msg_type == "SettingsApplied":
            self._settings_applied.set()

        elif msg_type == "UserStartedSpeaking":
            now = time.time()

            # ── Mic reopen: Deepgram's server-side VAD confirms the user
            # is speaking RIGHT NOW.  Cancel any mic tail-timer and reopen
            # the mic immediately so the user's words are heard — regardless
            # of the barge-in bookkeeping below.  This is the primary fix
            # for "BARQ stops listening after the greeting".
            self._cancel_mic_tail_timer()
            self._agent_is_speaking = False

            # ── Post-speech lockout: ignore for 1.5s after agent finished ──
            # Prevents residual speaker audio from triggering false barge-in
            seconds_since_agent_done = now - self._last_agent_done_speaking_at
            if seconds_since_agent_done < 1.5:
                print(f"[DeepgramAgent] UserStartedSpeaking post-speech lockout ({seconds_since_agent_done:.1f}s since agent done)")
                return

            # ── Debounce: ignore rapid duplicate events (echo guard) ──
            # 3-second cooldown prevents the acoustic echo loop where
            # speaker output is picked up by the mic and re-detected
            if now - self._last_user_spoke_at < 3.0:
                print("[DeepgramAgent] UserStartedSpeaking debounced (echo guard, 3s cooldown)")
                return
            self._last_user_spoke_at = now

            print("[DeepgramAgent] User started speaking — barge-in")
            self._captured_text = []

            # ── Ring buffer management ─────────────────────────────
            # Only clear the ring buffer if the agent was actively speaking
            # (true barge-in from a real interruption). If the agent already
            # finished speaking (cooldown period), the ring buffer still has
            # remaining audio that should finish playing naturally.
            # The 3s ring buffer holds audio that outlasts AgentAudioDone.
            seconds_since_done = now - self._last_agent_done_speaking_at
            if seconds_since_done > 5.0:
                # Agent wasn't recently speaking — this is a real barge-in
                self._output_ring_buffer.clear()
                print("[DeepgramAgent] Barge-in: ring buffer cleared")
            else:
                # Agent finished recently — keep ring buffer intact so the
                # remaining ~1-3s of audio finishes playing naturally
                print(f"[DeepgramAgent] Barge-in suppressed: agent finished {seconds_since_done:.1f}s ago, keeping ring buffer intact")

            # Notify frontend that agent stopped speaking
            if self.on_agent_done_speaking:
                self.on_agent_done_speaking()
            # If user speaks before agent finishes greeting, enable audio sending
            if not self._can_send_audio.is_set():
                self._can_send_audio.set()
                print("[DeepgramAgent] User spoke before greeting done — enabling mic now")

        elif msg_type == "UserTranscript":
            transcript = data.get("transcript", "")
            is_final = data.get("is_final", False)
            if is_final and transcript.strip():
                self._captured_text.append(transcript.strip())
                if self.on_final_transcript:
                    self.on_final_transcript(transcript.strip())
            elif transcript.strip():
                if self.on_interim_transcript:
                    self.on_interim_transcript(transcript)

        elif msg_type == "AgentThinking":
            print("[DeepgramAgent] Agent is thinking...")

        elif msg_type == "AgentStartedSpeaking":
            print("[DeepgramAgent] Agent started speaking — pausing mic audio")
            # Flush stale chunks that may have accumulated before the flag
            # was set (cross-boundary echo). These would trigger a false
            # UserStartedSpeaking if sent later after cooldown.
            self._flush_audio_queue("agent started")
            self._agent_is_speaking = True
            self._cancel_mic_tail_timer()
            if self.on_agent_speaking:
                self.on_agent_speaking()

        elif msg_type == "AgentAudioDone":
            print(f"[DeepgramAgent] Agent finished speaking — mic tail-timer started ({self._mic_tail_delay}s)")
            self._last_agent_done_speaking_at = time.time()
            if not self._can_send_audio.is_set():
                self._can_send_audio.set()
            if self.on_agent_done_speaking:
                self.on_agent_done_speaking()
            # Short tail: reopen the mic ~1.2s after the agent finishes so
            # speaker audio can decay while the user's next words stay
            # audible.  A fixed 6s mute would drop the user's first reply.
            self._restart_mic_tail_timer()

        elif msg_type == "ConversationText":
            # Greeting or agent response text.
            # Only pause mic for the INITIAL greeting (when _can_send_audio is
            # still False). AgentStartedSpeaking doesn't fire for the greeting,
            # so we use ConversationText to know audio is about to play.
            # For subsequent responses, AgentStartedSpeaking handles the pause.
            if not self._can_send_audio.is_set():
                self._agent_is_speaking = True
                self._flush_audio_queue("greeting")
                self._can_send_audio.set()
                print("[DeepgramAgent] Greeting text received — pausing mic during greeting audio")
            # Forward the text as a caption_barq message for live captions
            conv_text = data.get("text", "") or data.get("content", "")
            if conv_text and self.on_agent_text:
                self.on_agent_text(conv_text)

        elif msg_type == "FunctionCallRequest":
            functions = data.get("functions", [])
            if functions:
                fn = functions[0]
                fn_name = fn.get("name", "unknown")
                print(f"[DeepgramAgent] Function call: {fn_name} (client_side={fn.get('client_side', False)})")
            else:
                print("[DeepgramAgent] Function call: unknown (empty functions array)")
            await self._handle_function_call(data)

        elif msg_type == "KeepAlive":
            # Server keepalive — do NOT respond. The Voice Agent API
            # does not expect a response for keepalive, and sending one
            # will cause a "Text message received from client did not
            # match any of the formats we expect" error.
            pass

        elif msg_type == "Close":
            print("[DeepgramAgent] Server requested close")
            self._running = False

        elif msg_type == "Error":
            error_msg = data.get("description", data.get("message", "Unknown"))
            print(f"[DeepgramAgent] Error: {error_msg}")

    async def _handle_function_call(self, data: dict):
        """Handle a FunctionCallRequest from Deepgram.

        Executes the requested function locally and sends a
        FunctionCallResponse back to Deepgram so Gemini can
        speak the result to the user.

        Args:
            data: The FunctionCallRequest JSON dict from Deepgram.
                  Deepgram wraps functions in a list under the "functions" key.
                  Structure:
                    {
                      "type": "FunctionCallRequest",
                      "functions": [
                        {
                          "id": "<call_id>",
                          "name": "<function_name>",
                          "arguments": "<JSON string>",
                          "client_side": true|false,
                          "thought_signature": "<optional>"
                        }
                      ]
                    }
        """
        functions = data.get("functions", [])
        if not functions:
            print(f"[DeepgramAgent] Invalid FunctionCallRequest (empty functions array): {data}")
            return

        fn = functions[0]
        function_name = fn.get("name", "")
        function_call_id = fn.get("id", "")

        # Deepgram sends arguments as a JSON string — parse it
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {}
        else:
            arguments = raw_args

        if not function_name or not function_call_id:
            print(f"[DeepgramAgent] Invalid FunctionCallRequest (missing name/id): {data}")
            return

        # Dedup: ignore if we've already processed this call ID
        if function_call_id in self._pending_function_calls:
            print(f"[DeepgramAgent] Duplicate FunctionCallRequest ignored: {function_call_id}")
            return
        self._pending_function_calls.add(function_call_id)

        print(f"[DeepgramAgent] Executing function '{function_name}' with args: {arguments}")

        try:
            from .function_executor import execute_function

            # Execute with timeout (15s) so the audio loop isn't blocked forever
            result = await asyncio.wait_for(
                execute_function(function_name, arguments),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            result = {"status": "error", "detail": "Function execution timed out (15s limit)"}
        except Exception as e:
            result = {"status": "error", "detail": str(e)}
        finally:
            # Always clean up the call ID, even if cancelled or timed out
            self._pending_function_calls.discard(function_call_id)

        # Send FunctionCallResponse back to Deepgram
        response = {
            "type": "FunctionCallResponse",
            "function_call_id": function_call_id,
            "output": result,
        }

        if self._ws:
            try:
                await self._ws.send(json.dumps(response))
                print(f"[DeepgramAgent] FunctionCallResponse sent for '{function_name}'")
            except Exception as e:
                print(f"[DeepgramAgent] Failed to send FunctionCallResponse: {e}")
        else:
            print("[DeepgramAgent] WebSocket closed — cannot send FunctionCallResponse")

    def _cancel_mic_tail_timer(self) -> None:
        """Cancel any pending mic tail-timer and clear the reference.

        Consumes the cancelled task's result so asyncio doesn't warn about
        a pending task that was never awaited.
        """
        old = self._mic_cooldown_task
        self._mic_cooldown_task = None
        if old is not None and not old.done():
            old.cancel()
            old.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )

    def _restart_mic_tail_timer(self, delay: Optional[float] = None) -> None:
        """(Re)start the mic tail-timer.

        After the agent's audio stops flowing, the mic reopens once this
        timer elapses.  Restarted on every incoming audio chunk so the mic
        stays muted only while agent audio is actively arriving.

        Args:
            delay: Seconds to wait before reopening the mic.  Defaults to
                   ``self._mic_tail_delay`` (1.2s).
        """
        if delay is None:
            delay = self._mic_tail_delay
        self._cancel_mic_tail_timer()
        self._mic_cooldown_task = asyncio.create_task(
            self._delayed_mic_resume(delay)
        )

    async def _delayed_mic_resume(self, delay: float):
        """Wait for `delay` seconds, then resume mic audio.

        This is the mic tail-timer: while the agent is speaking we keep
        `_agent_is_speaking = True` so the mic drops echo audio.  Once the
        timer expires (shortly after audio stops), the mic reopens and the
        user's voice can be heard again.

        Args:
            delay: Seconds to wait before resuming mic (default 1.2s).
        """
        try:
            await asyncio.sleep(delay)
            self._agent_is_speaking = False
            print("[DeepgramAgent] Mic tail-timer complete — mic audio resumed")
            # Set the barge-in debounce so the first echo after cooldown
            # doesn't immediately trigger a new cycle
            self._last_user_spoke_at = time.time()
        except asyncio.CancelledError:
            # Timer was cancelled (new audio arrived, stop() called, etc.)
            pass

    @property
    def full_transcript(self) -> str:
        """Get the full accumulated transcript from this conversation."""
        return " ".join(self._captured_text)


def get_agent_config_json() -> dict:
    """Return the Voice Agent configuration as a dict.

    Can be inspected or modified at runtime.
    """
    return dict(AGENT_SETTINGS)
