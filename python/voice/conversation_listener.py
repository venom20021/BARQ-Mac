"""
Conversation listener for continuous voice interaction.

Powered by a pluggable Voice Agent (Deepgram Voice Agent or Pipecat local).
Wake word (Vosk) triggers the conversation, then the Voice Agent handles
all speech processing. Say "nothing" to end the conversation.
"""

import asyncio
import os
import re
import time
from collections.abc import Awaitable
from typing import Callable, Optional

from ai.responder import BARQResponder
from memory.agent_memory_manager import save_session_summary
from voice.evolution_logger import get_evolution_logger, mark_voice_cycle
from voice.loop_utils import call_on_main_loop
from voice.websocket_manager import VoiceWSManager
from voice.speech import SpeechProcessor
from voice.agent_history_sync import schedule_persist_voice_utterance

# Type aliases for optional command callbacks
ParseCommandFn = Callable[[str, bool, Optional[str]], Awaitable[dict]]
ExecuteCommandFn = Callable[[str, dict], Awaitable[str]]

# Max seconds to wait for the greeting context once the agent has connected.
# The fetch is started BEFORE connect() so it normally finishes during the
# websocket handshake; this is only a ceiling so a slow network can never delay
# the greeting — and therefore the audio pipeline — indefinitely.
# Tune with GREETING_CONTEXT_MAX_WAIT.
_GREETING_CTX_MAX_WAIT_S = float(os.getenv("GREETING_CONTEXT_MAX_WAIT", "0.6"))

# Module-level reference to the current ConversationListener singleton
_conversation_listener = None


def get_listener():
    """Get the active ConversationListener singleton."""
    global _conversation_listener
    return _conversation_listener


def set_listener(listener):
    """Set the active ConversationListener singleton."""
    global _conversation_listener
    _conversation_listener = listener


class ConversationListener:
    """Manages the voice agent conversation loop.

    Once activated (via wake word), connects to the configured Voice Agent
    (Deepgram or Pipecat).  The Voice Agent handles STT → LLM → TTS internally.
    Say "nothing" (or another exit phrase) to end and return to wake-word standby.
    """

    def __init__(
        self,
        stt: SpeechProcessor,
        responder: BARQResponder,
        on_stop: Optional[Callable] = None,
        parse_command: Optional[ParseCommandFn] = None,
        execute_command: Optional[ExecuteCommandFn] = None,
    ):
        self.stt = stt
        self.responder = responder
        self.ws_manager = VoiceWSManager.get_instance()
        self.evo_logger = get_evolution_logger()
        self.on_stop = on_stop
        self._conversation_active = False
        self._loop_task: Optional[asyncio.Task] = None
        self._managed_loop: Optional[asyncio.AbstractEventLoop] = None
        # The live Voice Agent, held so stop_conversation() can release its
        # microphone stream directly rather than relying on the loop task's
        # cleanup — which never executes if the task's loop has stopped.
        self._agent = None
        self._exit_phrases = [
            "nothing", "that's all", "we're done",
            "end conversation", "stop conversation",
            "go to sleep", "shut down", "that's it for now",
        ]
        self._parse_command: Optional[ParseCommandFn] = parse_command
        self._execute_command: Optional[ExecuteCommandFn] = execute_command
        self.vad_silence_timeout = 0.4  # VAD endpointing silence threshold (seconds)

        # Register as the global singleton
        set_listener(self)

    @property
    def is_active(self) -> bool:
        return self._conversation_active

    async def start_conversation(self):
        """Start the Voice Agent conversation loop in the background."""
        if self._conversation_active:
            return
        self._conversation_active = True
        self.responder.conversation.start_session("voice_conversation")

        print("[Conversation] Voice Agent starting...")
        self._loop_task = asyncio.create_task(self._conversation_loop())

    async def stop_conversation(self):
        """End the conversation loop and return to wake-word standby."""
        self._conversation_active = False

        # ── Auto-save session summary before ending ────────────────
        # Generate a concise summary from the conversation history
        # so BARQ can recall it on next wake (morning recall feature).
        try:
            if self.responder.conversation.is_active and self.responder.conversation.turn_count > 0:
                recent = self.responder.conversation.get_recent_history(6)
                topics = set()
                for msg in recent:
                    if msg["role"] == "user":
                        text = msg["content"][:80]
                        # Extract key phrases (first few words as topic indicators)
                        words = text.strip().split()[:6]
                        if words:
                            phrase = " ".join(words)
                            topics.add(phrase)
                if topics:
                    summary = "Discussed: " + "; ".join(sorted(topics))[:280]
                    language = getattr(self.responder, "_last_language", "")
                    save_session_summary(summary, language=language)
        except Exception as e:
            print(f"[Conversation] Session summary save error (non-fatal): {e}")

        # ── Stop the loop task ─────────────────────────────────────────
        # ``_loop_task`` belongs to the managed voice loop, while this method is
        # normally called from the main/uvicorn loop.  Awaiting a task owned by
        # a foreign loop raises RuntimeError — which used to abort this method
        # (and, through routes.stop_listening, the rest of the mute path),
        # leaving the microphone open.  Await only when the task belongs to the
        # running loop; otherwise cancel() is enough to have its own loop run
        # the cleanup.
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is not None and task.get_loop() is running_loop:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"[Conversation] Loop task teardown error (non-fatal): {e}")
            else:
                print("[Conversation] Loop task owns another loop — cancelled without awaiting")

        # ── Release the microphone stream directly ──────────────────────
        # The loop task's ``finally`` calls agent.stop(), so cancel() above is
        # normally enough.  But if that task is orphaned on a stopped loop its
        # cleanup never runs and the OS mic stays hot for good — the "mute
        # doesn't mute the whole system" bug.  Stopping the agent here is
        # idempotent and guarantees the stream is released.
        agent = self._agent
        self._agent = None
        if agent is not None:
            try:
                await agent.stop()
                print("[Conversation] Voice agent stopped — microphone released")
            except Exception as e:
                print(f"[Conversation] agent.stop() error (non-fatal): {e}")

        self.responder.conversation.end_session()

        await self.ws_manager.cancel_all()

        if self._managed_loop is not None and self._managed_loop.is_running():
            try:
                self._managed_loop.stop()
                print("[Conversation] Managed event loop stopped")
            except RuntimeError:
                pass

        self.ws_manager.fire(self.ws_manager.broadcast_state("idle"))

        if self.on_stop:
            try:
                self.on_stop()
            except Exception as e:
                print(f"[Conversation] on_stop callback error: {e}")

        print("[Conversation] Conversation ended - back to wake word standby")

    # ── Voice Agent loop ─────────────────────────────────────────────

    async def _conversation_loop(self):
        """Connect to the configured Voice Agent (Deepgram or Pipecat) and stream audio.

        The Voice Agent handles STT → LLM → TTS internally.
        Streams microphone audio and plays back responses.

        Retries connection up to 5 times with exponential
        backoff (1s, 3s, 9s, 27s, 81s) if the connection drops
        unexpectedly during a conversation.
        """
        max_retries = 5
        retry_delay = 1.0  # initial delay in seconds

        # Get the active backend *before* the retry loop so we don't
        # re-read the DB on every attempt (the backend shouldn't change
        # mid-conversation).
        backend = await _get_backend_once()

        for attempt in range(1, max_retries + 1):
            if not self._conversation_active:
                return

            # Get agent from the factory (uses cached instance)
            from .agent_factory import get_voice_agent_async, reset_voice_agent
            if attempt > 1:
                # On retry, force a fresh agent instance
                reset_voice_agent()

            agent = await get_voice_agent_async(backend=backend)
            if agent is None:
                print(f"[VoiceAgent] No voice agent available for backend '{backend}'")
                self._conversation_active = False
                return
            # Held so stop_conversation() can stop it even if this task is
            # orphaned and never reaches its own ``finally``.
            self._agent = agent

            greeting_ctx_task: Optional[asyncio.Task] = None
            try:
                # Start the greeting-context fetch BEFORE connecting so its HTTP
                # round trips overlap the websocket handshake instead of blocking
                # the greeting. Awaited inline in the greeting block below, this
                # measured ~1.65s on the critical path of every single wake.
                greeting_ctx_task = asyncio.ensure_future(self._greeting_context())

                connected = await agent.connect()
                if not connected:
                    print(f"[VoiceAgent] Failed to connect (attempt {attempt}/{max_retries})")
                    if attempt < max_retries:
                        wait = retry_delay * (3 ** (attempt - 1))  # 1, 3, 9, 27, 81
                        print(f"[VoiceAgent] Retrying in {wait:.0f}s...")
                        await asyncio.sleep(wait)
                        continue
                    else:
                        print("[VoiceAgent] All connection attempts failed - ending conversation")
                        self._conversation_active = False
                        return

                # Wake → websocket handshake complete. Marking here keeps the
                # connection cost separable from the greeting + first-audio cost.
                mark_voice_cycle("voice_connected")

                # Wire up agent callbacks
                agent.on_interim_transcript = lambda text: self.ws_manager.fire(
                    self.ws_manager.broadcast({
                        "type": "caption_user",
                        "text": text,
                        "isFinal": False,
                    })
                )
                agent.on_final_transcript = self._on_agent_final_transcript
                agent.on_agent_speaking = lambda: self.ws_manager.fire(
                    self.ws_manager.broadcast_state("speaking")
                )
                agent.on_agent_done_speaking = lambda: self.ws_manager.fire(
                    self.ws_manager.broadcast_state("listening")
                )
                agent.on_agent_text = lambda text: self.ws_manager.fire(
                    self.ws_manager.broadcast({
                        "type": "caption_barq",
                        "text": text,
                    })
                )
                agent.on_audio_chunk = self._on_agent_audio_chunk

                self.responder.is_speaking_event.set()

                # ── Dynamic wake greeting with context ──────────────────
                # Build a time-aware, name-aware greeting with weather/news
                # context (like Mark-L's two-phase briefing), so each wake
                # feels natural and informed rather than a hardcoded phrase.
                try:
                    # Read user name from DB for personalized greeting
                    # The greeting-context fetch has been running since before
                    # connect(), so it is normally already done. Bound the wait so
                    # a slow network can never stall the greeting, and shield it so
                    # a timeout still lets the fetch finish and warm the cache.
                    user_name = None
                    context_phrase = None
                    if greeting_ctx_task is not None:
                        try:
                            user_name, context_phrase = await asyncio.wait_for(
                                asyncio.shield(greeting_ctx_task),
                                timeout=_GREETING_CTX_MAX_WAIT_S,
                            )
                        except asyncio.TimeoutError:
                            print(
                                "[VoiceAgent] Greeting context not ready within "
                                f"{_GREETING_CTX_MAX_WAIT_S}s — greeting without it"
                            )
                        except Exception as e:
                            print(f"[VoiceAgent] Greeting context error (non-fatal): {e}")

                    if context_phrase:
                        print(f"[VoiceAgent] Greeting context: '{context_phrase}'")

                    from .greeting_engine import build_wake_greeting
                    greeting = build_wake_greeting(
                        user_name=user_name,
                        context_phrase=context_phrase,
                    )
                    print(f"[VoiceAgent] Greeting: '{greeting}'")
                    await agent.speak_text(greeting)
                    mark_voice_cycle("voice_greeting_sent")
                except Exception as e:
                    print(f"[VoiceAgent] Greeting TTS error (non-fatal): {e}")

                await agent.start_conversation()

                print(f"[VoiceAgent] Conversation active ({backend}, attempt {attempt}/{max_retries})")

                # Wait until conversation ends
                while self._conversation_active and agent.is_running:
                    await asyncio.sleep(0.5)

                # Check why we exited the loop
                if not self._conversation_active:
                    print("[VoiceAgent] Conversation ended by user")
                    break
                elif not agent.is_running:
                    print(f"[VoiceAgent] Agent disconnected unexpectedly (attempt {attempt}/{max_retries})")
                    if attempt < max_retries:
                        wait = retry_delay * (3 ** (attempt - 1))
                        print(f"[VoiceAgent] Reconnecting in {wait:.0f}s...")
                        await asyncio.sleep(wait)
                        reset_voice_agent()
                        continue
                    else:
                        print("[VoiceAgent] Max retries reached - ending conversation")
                        break

            except Exception as e:
                print(f"[VoiceAgent] Loop error: {e}")
                if attempt < max_retries:
                    wait = retry_delay * (3 ** (attempt - 1))
                    print(f"[VoiceAgent] Retrying in {wait:.0f}s...")
                    await asyncio.sleep(wait)
                    reset_voice_agent()
                    continue
                else:
                    break
            finally:
                if greeting_ctx_task is not None and not greeting_ctx_task.done():
                    greeting_ctx_task.cancel()
                self.responder.is_speaking_event.clear()
                try:
                    await agent.stop()
                except Exception as e:
                    print(f"[VoiceAgent] agent.stop() error (non-fatal): {e}")
                finally:
                    if self._agent is agent:
                        self._agent = None

    def _on_agent_final_transcript(self, text: str):
        """Handle a final transcript from the Voice Agent."""
        self.ws_manager.fire(self.ws_manager.broadcast({
            "type": "caption_user",
            "text": text,
            "isFinal": True,
        }))

        # Persist the spoken command to agent_chat_history (voice_commands key)
        # so the re-import feeds spoken topics into the ai_chats graph.
        # Covers every Voice Agent backend (Gemini Live, Deepgram, Pipecat).
        if not self._is_exit_command(text) and len(text.strip()) >= 2:
            # Loop-safe fire-and-forget: marshals the DB write onto the main
            # loop so the managed voice loop never touches main-loop futures.
            try:
                schedule_persist_voice_utterance(text)
            except Exception as e:
                print(f"[VoiceAgent] History persist error (non-fatal): {e}")

        if self._is_exit_command(text):
            print("[VoiceAgent] Exit command detected - stopping conversation")
            asyncio.create_task(self.stop_conversation())

    def _on_agent_audio_chunk(self, pcm_array, sample_rate: int):
        """Handle an audio chunk from the Voice Agent.

        Playback is handled internally by the agent's output stream.
        This callback exists for future state tracking (e.g., captions).
        """
        pass

    # ── Helpers ─────────────────────────────────────────────────────

    async def _greeting_context(self) -> tuple[Optional[str], Optional[str]]:
        """Read the personalisation inputs and fetch the greeting context.

        Returns ``(user_name, context_phrase)``.

        Runs as a background task started *before* ``agent.connect()`` so the
        HTTP round trips overlap the websocket handshake — the wake path must
        never block on the network just to decorate a greeting.  The DB reads go
        through ``call_on_main_loop`` because settings are main-loop bound.
        """
        user_name: Optional[str] = None
        weather_city: Optional[str] = None
        context_phrase: Optional[str] = None

        try:
            from database import settings_dao

            name_val = await call_on_main_loop(settings_dao.get_setting("user_name"))
            if name_val and name_val.strip():
                user_name = name_val.strip()
        except Exception:
            pass

        # NOTE: No namespace passed — matches the convention in routes.py's
        # _gather_background_info() which also reads weather_city without one.
        try:
            from database import settings_dao

            city_val = await call_on_main_loop(settings_dao.get_setting("weather_city"))
            if city_val and city_val.strip():
                weather_city = city_val.strip()
        except Exception:
            pass

        try:
            from .greeting_context import fetch_greeting_context

            _fetch_t0 = time.perf_counter()
            ctx = await fetch_greeting_context(city=weather_city, include_news=True)
            # Recorded separately from the cycle marks because it is a plain
            # duration, not an offset: a cache hit is ~0 ms, a cold fetch ~1600 ms
            # (two sequential open-meteo round trips). This is the number the
            # greeting-context cache is judged by.
            self.evo_logger.record(
                "greeting_context_fetch",
                (time.perf_counter() - _fetch_t0) * 1000,
                {"city": weather_city or "", "has_context": bool(ctx)},
            )
            if ctx:
                context_phrase = ctx
        except Exception:
            pass

        return user_name, context_phrase

    def _is_exit_command(self, text: str) -> bool:
        """Check if user wants to end the conversation."""
        text_lower = text.lower().strip()
        for phrase in self._exit_phrases:
            if re.search(rf"\b{re.escape(phrase)}\b", text_lower):
                return True
        return False


async def _get_backend_once() -> str:
    """Read the voice agent backend from DB once (with env fallback).

    Runs on the MAIN loop (via ``call_on_main_loop``) because
    ``get_backend_from_db`` reads settings through the main-loop-bound DB
    connection — this coroutine runs on the managed voice loop.
    """
    try:
        from .agent_factory import get_backend_from_db
        resolved = await call_on_main_loop(get_backend_from_db())
        if resolved:
            return resolved
    except Exception:
        pass
    import os
    return os.getenv("VOICE_AGENT_BACKEND", "deepgram")
