"""
Conversation listener for continuous voice interaction.

Powered by a pluggable Voice Agent (Deepgram Voice Agent or Pipecat local).
Wake word (Vosk) triggers the conversation, then the Voice Agent handles
all speech processing. Say "nothing" to end the conversation.
"""

import asyncio
import re
from collections.abc import Awaitable
from typing import Callable, Optional

from ai.responder import BARQResponder
from memory.agent_memory_manager import save_session_summary
from voice.evolution_logger import get_evolution_logger
from voice.loop_utils import call_on_main_loop
from voice.websocket_manager import VoiceWSManager
from voice.speech import SpeechProcessor
from voice.agent_history_sync import schedule_persist_voice_utterance

# Type aliases for optional command callbacks
ParseCommandFn = Callable[[str, bool, Optional[str]], Awaitable[dict]]
ExecuteCommandFn = Callable[[str, dict], Awaitable[str]]

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

        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
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

            try:
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
                    user_name = None
                    try:
                        from database import settings_dao
                        name_val = await call_on_main_loop(settings_dao.get_setting("user_name"))
                        if name_val and name_val.strip():
                            user_name = name_val.strip()
                    except Exception:
                        pass

                    # Read weather city from DB for context
                    # NOTE: No namespace passed — matches the convention in
                    # routes.py's _gather_background_info() which also reads
                    # weather_city without a namespace.
                    weather_city = None
                    try:
                        city_val = await call_on_main_loop(settings_dao.get_setting("weather_city"))
                        if city_val and city_val.strip():
                            weather_city = city_val.strip()
                    except Exception:
                        pass

                    # Fetch weather + news context in parallel (fast, non-blocking)
                    # This runs before the greeting TTS so the greeting can include
                    # "Looks like rain in Lucknow" — like Mark-L's Phase 2 briefing
                    # folded directly into the greeting.
                    context_phrase = None
                    try:
                        from .greeting_context import fetch_greeting_context
                        ctx = await fetch_greeting_context(
                            city=weather_city,
                            include_news=True,
                        )
                        if ctx:
                            context_phrase = ctx
                            print(f"[VoiceAgent] Greeting context: '{ctx}'")
                    except Exception:
                        pass

                    from .greeting_engine import build_wake_greeting
                    greeting = build_wake_greeting(
                        user_name=user_name,
                        context_phrase=context_phrase,
                    )
                    print(f"[VoiceAgent] Greeting: '{greeting}'")
                    await agent.speak_text(greeting)
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
                self.responder.is_speaking_event.clear()
                await agent.stop()

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
