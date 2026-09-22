"""
Gemini Live Voice Agent — uses Google Gemini's native audio WebSocket API.

Architecture (inspired by Mark-L):
  Mic (16 kHz PCM) → Gemini Live Audio WebSocket → Speaker (24 kHz PCM)

Gemini handles STT + LLM + TTS in a single WebSocket connection.
No local STT, no local TTS, no MP3 decoding, no event-loop corruption.

Requires:
  - GEMINI_API_KEY environment variable
  - pip install google-genai
"""

import asyncio
import os
import threading
import time
from typing import Any, Optional, cast

import numpy as np
import sounddevice as sd
from google import genai
from google.genai import types

from config import get_settings
from .agent_base import VoiceAgentBase
from .evolution_logger import get_evolution_logger, mark_voice_cycle
from .function_executor import execute_function, get_function_schemas

LIVE_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHANNELS = 1
MIC_CHUNK_SIZE = 1024       # ~64 ms at 16 kHz
OUTPUT_BLOCK_SIZE = 2400    # ~50 ms at 24 kHz — fast barge-in


class _ConnectionClosed(Exception):
    """Internal signal: the Gemini Live WebSocket is dead and unrecoverable.

    Raised by the send/receive loops when a connection-closed error is
    detected (e.g. 1011 keepalive ping timeout).  It propagates out of
    the TaskGroup so ``start_conversation()`` returns, which sets
    ``_running = False`` and lets the ConversationListener's retry logic
    reconnect instead of hammering a dead socket forever.
    """


class GeminiVoiceAgent(VoiceAgentBase):
    """Voice agent using Gemini Live Audio WebSocket.

    Streams mic audio to Gemini and plays back the audio response.
    Gemini handles STT + LLM + TTS natively — no local processing needed.
    """

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self._api_key:
            print("[GeminiAgent] [!!] GEMINI_API_KEY not set - agent will fail to connect")

        self._client: Optional[genai.Client] = None
        self._session: Optional[Any] = None          # LiveSession
        self._live_cm: Optional[Any] = None           # async context manager for session
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

        # Audio queues
        self._audio_out_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._audio_in_queue: asyncio.Queue = asyncio.Queue()

        # Streams
        self._input_stream: Optional[sd.InputStream] = None
        self._output_stream: Optional[sd.RawOutputStream] = None

        # State
        self._is_speaking = False
        self._speaking_lock = threading.Lock()
        self._turn_done_event: Optional[asyncio.Event] = None
        self._started_event = asyncio.Event()   # set after start_conversation()

        # Safety net for the "mic stays muted after greeting" bug:
        # track when agent audio was last received so the play loop can
        # reopen the mic after a short idle gap even if the turn_complete
        # event never arrives (which would otherwise mute BARQ forever).
        self._last_audio_at: float = 0.0
        # How long after the last agent audio chunk before the mic reopens.
        self._mic_reopen_delay: float = 1.2

        # Consecutive non-connection errors in the receive loop.  After 3
        # in a row we treat the session as unrecoverable and reconnect
        # rather than spinning on errors forever.
        self._receive_error_streak = 0

        # perf_counter of the user's most recent utterance, used by the play loop
        # to measure response latency (user stopped → agent started speaking).
        # 0.0 means "no utterance awaiting a response".
        self._last_user_input_at: float = 0.0

        # Callbacks (wired by ConversationListener)
        self.on_interim_transcript = None
        self.on_final_transcript = None
        self.on_agent_speaking = None
        self.on_agent_done_speaking = None
        self.on_audio_chunk = None
        self.on_agent_text = None

        # ── Windows asyncio AssertionError suppressor ────────────────
        # Gemini Live uses aiohttp under the hood, which triggers the
        # same ProactorBaseWritePipeTransport bug on Python 3.13+ Windows.
        _install_gemini_assertion_guard()

    # ── VoiceAgentBase interface ──────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    async def connect(self) -> bool:
        """Open the Gemini Live WebSocket session."""
        if not self._api_key:
            print("[GeminiAgent] [XX] Connect failed: GEMINI_API_KEY not set")
            return False
        if self._running:
            return True

        self._loop = asyncio.get_event_loop()

        try:
            self._client = genai.Client(
                api_key=self._api_key,
                http_options={"api_version": "v1beta"},
            )

            system_prompt = self._build_system_prompt()

            # ── Register BARQ's tools as Gemini function declarations ──
            try:
                schemas = get_function_schemas()
                tools = [{"function_declarations": schemas}]
                print(f"[GeminiAgent] [pkg] Registered {len(schemas)} tools")
            except Exception as e:
                print(f"[GeminiAgent] [!!] Tool registration failed: {e}")
                tools = None

            config = types.LiveConnectConfig(
                response_modalities=cast(Any, ["AUDIO"]),
                output_audio_transcription=cast(Any, {}),
                input_audio_transcription=cast(Any, {}),
                system_instruction=system_prompt,
                tools=cast(Any, tools),
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="Charon",
                        )
                    )
                ),
            )

            # live.connect() is an async context manager (not a plain awaitable)
            self._live_cm = self._client.aio.live.connect(
                model=LIVE_MODEL,
                config=config,
            )
            self._session = await self._live_cm.__aenter__()

            print("[GeminiAgent] [OK] Connected to Gemini Live")
            self._running = True
            return True

        except Exception as e:
            print(f"[GeminiAgent] [XX] Connect failed: {e}")
            self._running = False
            return False

    async def start_conversation(self, audio_device: Optional[int] = None):
        """Begin streaming mic audio and playing Gemini responses.

        Runs three concurrent tasks inside a TaskGroup:
          1. Send mic PCM → Gemini
          2. Receive audio from Gemini → play through speakers
          3. Receive transcripts / turn-complete events
        """
        if not self._session or not self._running:
            print("[GeminiAgent] Cannot start: not connected")
            return

        self._turn_done_event = asyncio.Event()
        self._started_event.set()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._send_mic_loop(audio_device))
                tg.create_task(self._play_audio_loop())
                tg.create_task(self._receive_loop())
        except _ConnectionClosed as e:
            # The WebSocket died (keepalive ping timeout etc.).  The
            # listener will detect is_running == False and reconnect.
            print(f"[GeminiAgent] [!!] Connection closed - will reconnect ({e})")
        except Exception as e:
            print(f"[GeminiAgent] Conversation error: {e}")
        finally:
            self._running = False

    async def stop(self):
        """Gracefully stop the conversation and close WebSocket."""
        self._running = False

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

        # Close Live session via context manager exit
        if self._live_cm:
            try:
                await self._live_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._live_cm = None
            self._session = None
        elif self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

        print("[GeminiAgent] Stopped")

    async def speak_text(self, text: str) -> None:
        """Synthesise and play a spoken phrase via Gemini Live.

        Sends text as a client message; Gemini responds with audio that
        flows through the standard _play_audio_loop.
        """
        if not self._session or not self._running:
            print("[GeminiAgent] Cannot speak: not connected")
            return

        # If the conversation tasks haven't started yet, send and wait
        if not self._started_event.is_set():
            try:
                await self._session.send_client_content(
                    turns={"parts": [{"text": text}]},
                    turn_complete=True,
                )
                print(f"[GeminiAgent] Sent greeting: '{text}'")
            except Exception as e:
                print(f"[GeminiAgent] speak_text error: {e}")
            return

        # Conversation is already active — send as a client message
        try:
            await self._session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True,
            )
            print(f"[GeminiAgent] Queued speech: '{text[:60]}...'")
        except Exception as e:
            print(f"[GeminiAgent] speak_text error: {e}")

    def cancel_current_tts(self) -> None:
        """Interrupt ongoing speech (barge-in)."""
        q = self._audio_in_queue
        drained = 0
        while True:
            try:
                q.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            print(f"[GeminiAgent] [int] Barge-in - {drained} chunks discarded")

    # ── Internal helpers ──────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        """Build the system instruction with time context and session memory."""
        from datetime import datetime

        now = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")

        # Inject last session summary (morning recall)
        session_clause = ""
        try:
            from memory.agent_memory_manager import pop_last_session
            last = pop_last_session()
            if last:
                summary = last.get("summary", "")
                s_date = last.get("date", "")
                if summary:
                    session_clause = (
                        f"\nYou were last used on {s_date}. "
                        f"Summary of that session: {summary}"
                    )
        except Exception:
            pass

        return (
            f"[Current date and time: {time_str}]\n"
            f"You are BARQ, a helpful AI desktop assistant. "
            f"Be concise, natural, and conversational. "
            f"Keep responses brief — under three sentences when possible."
            f"{session_clause}"
        )

    def _enqueue_mic_audio(self, item: dict) -> None:
        """Thread-safe enqueue of a mic chunk (runs on the event loop).

        Wraps ``put_nowait`` so a full queue drops the frame silently
        instead of raising ``QueueFull`` inside an event-loop callback.
        (A try/except around ``call_soon_threadsafe`` cannot catch it —
        the callback runs later, on the loop thread, and would spam
        "Exception in callback Queue.put_nowait()" on stderr.)
        """
        try:
            self._audio_out_queue.put_nowait(item)
        except asyncio.QueueFull:
            pass  # mic produces faster than the WS can send — drop

    async def _receive_stream(self):
        """Continuously yield Gemini Live server messages across turns.

        ``LiveSession.receive()`` yields messages for exactly ONE complete
        turn and then returns (the SDK breaks after ``turn_complete``), so
        a multi-turn conversation must re-enter it.  This generator loops
        for as long as the agent is running, re-entering ``receive()``
        after each finished turn.

        A genuine connection drop surfaces as an exception
        (``websockets.ConnectionClosed`` wrapped as ``APIError``) and is
        re-raised as ``_ConnectionClosed`` so the listener reconnects.
        Transient non-connection errors are logged and retried (up to 3 in
        a row, then treated as unrecoverable) instead of killing the
        conversation on a single hiccup.
        """
        while self._running:
            try:
                if self._session is None:
                    raise RuntimeError("Gemini session closed")
                async for msg in self._session.receive():
                    self._receive_error_streak = 0
                    yield msg
            except asyncio.CancelledError:
                raise
            except _ConnectionClosed:
                raise
            except Exception as e:
                if not self._running:
                    return  # intentional shutdown — the WS may be mid-close
                if self._is_connection_closed_error(e):
                    # Connection died (and we're not shutting down) — let
                    # the listener reconnect.
                    print(f"[GeminiAgent] [!!] Receive failed - connection dead ({e})")
                    raise _ConnectionClosed(str(e)) from e
                self._receive_error_streak += 1
                if self._receive_error_streak >= 3:
                    print(f"[GeminiAgent] [!!] Repeated receive errors ({self._receive_error_streak}) - treating as dead")
                    raise _ConnectionClosed(str(e)) from e
                print(f"[GeminiAgent] [XX] Receive error: {e}")
                await asyncio.sleep(0.5)

    async def _send_mic_loop(self, device: Optional[int] = None):
        """Continuously stream mic audio frames to Gemini Live."""
        loop = self._loop or asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            if status:
                print(f"[GeminiAgent] Mic status: {status}")
            # Only send when BARQ is not speaking (avoid echo)
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking:
                data = indata.tobytes()
                # Cheap pre-check (races are fine — _enqueue_mic_audio
                # swallows QueueFull on the loop thread).
                if not self._audio_out_queue.full():
                    try:
                        loop.call_soon_threadsafe(
                            self._enqueue_mic_audio,
                            {"data": data, "mime_type": "audio/pcm"},
                        )
                    except RuntimeError:
                        pass  # event loop shut down — stream is closing

        try:
            # Resolve output device
            try:
                from .audio_device import resolve_input_device
                if device is None:
                    device = resolve_input_device(
                        get_settings().audio_input_device
                    )
            except Exception:
                pass

            self._input_stream = sd.InputStream(
                device=device,
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=MIC_CHUNK_SIZE,
                callback=callback,
            )
            self._input_stream.start()
            print("[GeminiAgent] [mic] Mic stream started (16 kHz)")

            while self._running:
                try:
                    msg = await asyncio.wait_for(
                        self._audio_out_queue.get(), timeout=0.1
                    )
                    if self._session:
                        await self._session.send_realtime_input(media=msg)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if self._running and self._is_connection_closed_error(e):
                        # Socket is dead (and we're not shutting down) — stop
                        # spamming and let the listener reconnect.  When
                        # stopping normally, _running is already False, so we
                        # just log and exit.
                        print(f"[GeminiAgent] [!!] Send failed - connection dead ({e})")
                        raise _ConnectionClosed(str(e)) from e
                    print(f"[GeminiAgent] Send error: {e}")

        except Exception as e:
            print(f"[GeminiAgent] [XX] Mic error: {e}")
            raise
        finally:
            # Always release the mic stream.  If the TaskGroup tears down
            # (dead connection / cancel), the sounddevice callback thread
            # would otherwise keep firing into a queue nothing drains,
            # spamming "Exception in callback Queue.put_nowait()" on
            # stderr until stop() eventually runs.
            if self._input_stream is not None:
                try:
                    self._input_stream.stop()
                    self._input_stream.close()
                except Exception:
                    pass
                self._input_stream = None

    async def _play_audio_loop(self):
        """Read PCM audio from Gemini and play through speakers.

        Batches chunks for smoother playback while keeping block size
        small enough for responsive barge-in.
        """
        try:
            # Resolve output device
            try:
                from .audio_device import resolve_output_device
                out_dev = resolve_output_device(
                    get_settings().audio_output_device
                )
            except Exception:
                out_dev = None

            self._output_stream = sd.RawOutputStream(
                device=out_dev,
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=OUTPUT_BLOCK_SIZE,
            )
            self._output_stream.start()
            print("[GeminiAgent] [spk] Output stream started (24 kHz)")

            while self._running:
                try:
                    chunk = await asyncio.wait_for(
                        self._audio_in_queue.get(), timeout=0.15
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self._audio_in_queue.empty()
                    ):
                        self._set_speaking(False)
                        self._turn_done_event.clear()
                    elif self._mic_reopen_needed():
                        # Safety net: no agent audio for >1.2s — assume the
                        # turn ended even without a turn_complete event, so
                        # the mic reopens and BARQ can hear the user again.
                        print("[GeminiAgent] Mic reopened via idle safety net")
                        self._set_speaking(False)
                    continue

                self._last_audio_at = time.time()
                self._set_speaking(True)

                # ── Voice-latency marks ──────────────────────────────────
                # First audible audio of this wake is the headline number
                # (wake → sound actually out of the speakers). ``once=True``
                # because this block runs for every audio chunk.
                mark_voice_cycle("voice_first_audio", once=True)

                # First agent audio after the user's last utterance = response
                # latency. Reset to 0 so it records once per turn, not per chunk.
                if self._last_user_input_at > 0:
                    get_evolution_logger().record(
                        "voice_response_ms",
                        (time.perf_counter() - self._last_user_input_at) * 1000,
                        {"backend": "gemini"},
                    )
                    self._last_user_input_at = 0.0

                # Batch up to ~200 ms of audio for smooth writes
                batch = bytearray(chunk)
                while len(batch) < 9600:  # 200 ms at 24 kHz / 16-bit
                    try:
                        batch.extend(self._audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    await asyncio.to_thread(self._output_stream.write, bytes(batch))
                except RuntimeError:
                    break
                except asyncio.CancelledError:
                    # Never swallow cancellation — the TaskGroup needs it to
                    # tear down promptly when a sibling detects a dead
                    # connection.  Swallowing it here would make the play
                    # loop spin on `while self._running` (still True until
                    # start_conversation's finally runs) and deadlock the
                    # TaskGroup indefinitely.
                    raise

        except Exception as e:
            print(f"[GeminiAgent] [XX] Play error: {e}")
        finally:
            self._set_speaking(False)
            # Always release the output stream (see _send_mic_loop finally).
            if self._output_stream is not None:
                try:
                    self._output_stream.stop()
                    self._output_stream.close()
                except Exception:
                    pass
                self._output_stream = None

    async def _receive_loop(self):
        """Receive streaming responses from Gemini Live.

        Handles:
          - Audio data → _audio_in_queue for playback
          - Output transcription → on_agent_text callback
          - Turn-complete events → _turn_done_event
          - Tool calls → logged for future integration

        ``LiveSession.receive()`` yields messages for ONE complete turn
        and then returns (the SDK breaks after ``turn_complete``), so this
        iterates over ``_receive_stream()`` which re-enters ``receive()``
        for every turn — a clean turn end is NORMAL, not a disconnect.
        A genuine connection drop surfaces as an exception
        (``websockets.ConnectionClosed`` wrapped as ``APIError``); we
        detect it and raise ``_ConnectionClosed`` so the listener
        reconnects.
        """
        out_buf: list[str] = []

        try:
            async for response in self._receive_stream():
                if not self._running:
                    break

                # ── Audio data ────────────────────────────────────────
                if response.data:
                    if self._turn_done_event and self._turn_done_event.is_set():
                        self._turn_done_event.clear()

                    audio_data = response.data
                    for i in range(0, len(audio_data), OUTPUT_BLOCK_SIZE):
                        self._audio_in_queue.put_nowait(
                            audio_data[i: i + OUTPUT_BLOCK_SIZE]
                        )

                    # Dispatch to on_audio_chunk for UI visualisation
                    if self.on_audio_chunk:
                        try:
                            arr = np.frombuffer(audio_data, dtype=np.int16)
                            self.on_audio_chunk(arr, RECEIVE_SAMPLE_RATE)
                        except Exception:
                            pass

                # ── Server content (transcripts, turn events) ─────────
                if response.server_content:
                    sc = response.server_content

                    # Output transcription — what Gemini said
                    if sc.output_transcription and sc.output_transcription.text:
                        txt = sc.output_transcription.text.strip()
                        if txt and (not out_buf or txt != out_buf[-1]):
                            out_buf.append(txt)

                    # Input transcription — what the user said
                    if sc.input_transcription and sc.input_transcription.text:
                        txt = sc.input_transcription.text.strip()
                        if txt:
                            # Timestamp the user's latest utterance so the play
                            # loop can measure time-to-response.
                            self._last_user_input_at = time.perf_counter()
                        if txt and self.on_final_transcript:
                            self.on_final_transcript(txt)

                    # Turn complete — Gemini finished speaking
                    if sc.turn_complete:
                        if self._turn_done_event:
                            self._turn_done_event.set()

                        full_out = " ".join(out_buf).strip()
                        if full_out:
                            print(f"[GeminiAgent] BARQ: {full_out}")
                            if self.on_agent_text:
                                self.on_agent_text(full_out)
                            if self.on_final_transcript:
                                self.on_final_transcript(full_out)
                        out_buf = []

                # ── Tool calls — execute via function_executor ───────
                if response.tool_call:
                    fn_responses = []
                    for fc in response.tool_call.function_calls:
                        fn_name = fc.name
                        fn_args = dict(fc.args) if fc.args else {}
                        print(f"[GeminiAgent] [tool] {fn_name}({fn_args})")

                        try:
                            result = await execute_function(fn_name, fn_args)
                            # Pass the result dict directly — Gemini parses it
                            # as structured JSON-like data for its next turn.
                            # Truncate very long string fields to avoid blowing
                            # the session context window.
                            if isinstance(result, dict):
                                for k, v in result.items():
                                    if isinstance(v, str) and len(v) > 2000:
                                        result[k] = v[:2000] + "... [truncated]"
                            fn_responses.append(types.FunctionResponse(
                                id=fc.id,
                                name=fn_name,
                                response={"result": result},
                            ))
                            print(f"[GeminiAgent] [out] {fn_name} -> {str(result)[:100]}")
                        except Exception as e:
                            print(f"[GeminiAgent] [XX] Tool '{fn_name}' failed: {e}")
                            fn_responses.append(types.FunctionResponse(
                                id=fc.id,
                                name=fn_name,
                                response={"error": str(e)},
                            ))

                    if fn_responses and self._session:
                        await self._session.send_tool_response(
                            function_responses=fn_responses
                        )

        except _ConnectionClosed:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Errors raised while processing a message (callbacks, tool
            # responses).  Stream errors are handled inside _receive_stream.
            if not self._running:
                return
            if self._is_connection_closed_error(e):
                print(f"[GeminiAgent] [!!] Receive failed - connection dead ({e})")
                raise _ConnectionClosed(str(e)) from e
            print(f"[GeminiAgent] [XX] Receive error: {e}")

    def _is_connection_closed_error(self, exc: Exception) -> bool:
        """Heuristic: does this exception mean the WebSocket is dead?

        Matches the messages the google-genai client surfaces when the
        Gemini Live WebSocket is dropped (1011 keepalive ping timeout,
        no close frame, connection reset, etc.) so we stop retrying a
        dead socket and let the listener reconnect.
        """
        if exc is None:
            return False
        name = type(exc).__name__
        if "ConnectionClosed" in name or name in (
            "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError",
        ):
            return True
        msg = str(exc).lower()
        dead_tokens = (
            "keepalive", "ping timeout", "no close frame", "1011",
            "connection reset", "broken pipe", "connection closed",
            "websocket is closed", "closed connection", "connection is closed",
            "abnormal closure", "close code", "1006", "1000", "1001",
            "1002", "1003", "1007", "1008", "1009", "1010", "1012",
            "1013", "1014", "1015",
        )
        return any(token in msg for token in dead_tokens)

    def _mic_reopen_needed(self) -> bool:
        """Whether the mic should reopen due to the idle safety net.

        True when the agent appears to have stopped speaking (no audio
        received for longer than ``_mic_reopen_delay``) and the output
        queue is drained — even if no turn_complete event was received.
        """
        return (
            self._is_speaking
            and (time.time() - self._last_audio_at) > self._mic_reopen_delay
            and self._audio_in_queue.empty()
        )

    def _set_speaking(self, value: bool):
        with self._speaking_lock:
            changed = self._is_speaking != value
            self._is_speaking = value

        if changed:
            if value and self.on_agent_speaking:
                self.on_agent_speaking()
            elif not value and self.on_agent_done_speaking:
                self.on_agent_done_speaking()


# ── Windows asyncio AssertionError suppressor ──────────────────────────
# Duplicated from pipecat_agent.py so GeminiVoiceAgent has its own guard.
# Python 3.13+'s Proactor event loop on Windows may fire a spurious
# AssertionError from _ProactorBaseWritePipeTransport._loop_writing()
# when a pipe write completes after the transport has been closed.
# This corrupts the event loop and causes "Cannot enter into task"
# cascades.  The fix: monkey-patch the method to suppress the error.
#
# We use a separate copy here (not shared) so each agent module
# is self-contained and importing either agent alone still installs
# the guard.

_ORIG_LOOP_WRITING = None


def _install_gemini_assertion_guard():
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
                    pass

            cls._loop_writing = _safe_loop_writing  # type: ignore[attr-defined]
    except Exception:
        pass
