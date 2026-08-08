"""
BARQ Responder - generates both text and audio responses.
Uses ConversationManager for context and Ollama for LLM responses.

Supports streaming: LLM tokens are emitted incrementally, split at sentence
boundaries, and each sentence is immediately sent to TTS so audio playback
can begin while the LLM continues generating the rest of the response.
"""

import asyncio
import hashlib
import io
import re
import threading
import time
from pathlib import Path
from typing import AsyncIterable

import edge_tts
import numpy as np

from ai.conversation import ConversationManager, get_small_talk
from utils.ollama_client import OllamaClient
from voice.evolution_logger import get_evolution_logger
from voice.speech import SpeechProcessor
from voice.websocket_manager import VoiceWSManager

# Module-level speaking event — accessible cross-module without circular imports.
# Set by BARQResponder.__init__(), consumed by speech.py's mic monitor to
# discard audio frames during TTS playback (echo prevention).
_speaking_event: threading.Event = threading.Event()

# Audio output directory
AUDIO_DIR = Path(__file__).parent.parent / "data" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def _split_sentences(text: str) -> list[str]:
    """Split text at sentence/natural-pause boundaries.
    Returns complete chunks that are at least a few words long;
    the caller should keep the last element as a running buffer
    if it does not end with terminal punctuation.
    Splits on . ! ? : ; and commas so that smaller chunks
    can be sent to TTS faster while the LLM keeps generating.
    """
    parts = re.split(r"(?<=[.!?:;,])\s+", text)
    return [p.strip() for p in parts if p.strip()]


class BARQResponder:
    """Handles user input and returns both text + audio response."""

    def __init__(self):
        self.conversation = ConversationManager()
        self.llm = OllamaClient()
        self.speech = SpeechProcessor()
        self.ws_manager = VoiceWSManager.get_instance()
        self.tts_voice: str = "en-US-JennyNeural"  # must match routes.py default
        self.is_speaking = False
        # `is_speaking_event` is a reference to the module-level `_speaking_event`,
        # accessible cross-module by speech.py's mic monitor without circular imports.
        self.is_speaking_event = _speaking_event
        self.is_processing = False
        self._interrupt_requested = False  # set True to abort an active stream
        self.stt_text: str = ""  # latest interim STT transcript (for live display via WebSocket)
        self.stt_confidence: float = 0.0  # confidence score of current/interim STT (0.0-1.0)
        self.response_text: str = ""  # accumulated AI response text (for live display via WebSocket)
        self.evo_logger = get_evolution_logger()
        self.wake_word_timestamp: float = 0.0  # set by routes.py _on_wake_word_callback for TTFB measurement

    # ── Non-streaming (legacy) respond ───────────────────────────────

    async def respond(self, user_input: str, confidence: float = 0.0) -> dict:
        """Process user input and return text + audio response.

        Checks small talk first (faster than LLM), then routes commands
        or sends to Ollama for a conversational response.

        Args:
            user_input: The transcribed text from the user.
            confidence: Confidence score of the transcription (0.0-1.0).

        Returns:
            Dict with keys: text, audio_path, action
        """
        self.is_processing = True
        try:
            # ── Slash commands ──────────────────────────────────────
            stripped = user_input.strip()
            if stripped.startswith("/"):
                if stripped.startswith("/ponytail-review"):
                    response_text = await self._handle_ponytail_review()
                else:
                    response_text = f"Unknown slash command: {stripped.split()[0]}"
                self.conversation.add_assistant_message(response_text)
                audio_path = await self._text_to_speech(response_text)
                return {
                    "text": response_text,
                    "audio_path": str(audio_path),
                    "action": "slash_command",
                }

            self.conversation.add_user_message(user_input)

            # ── Small talk check (faster than LLM) ──────────────────
            small_talk_reply = get_small_talk(user_input)
            if small_talk_reply:
                response_text = small_talk_reply
                self.conversation.add_assistant_message(response_text)
                audio_path = await self._text_to_speech(response_text)
                return {
                    "text": response_text,
                    "audio_path": str(audio_path),
                    "action": "conversation",
                }

            intent = self._classify_intent(user_input)

            if intent == "command":
                response_text = await self._handle_command(user_input)
            else:
                context = self.conversation.get_context()
                try:
                    response_text = await self.llm.chat(context)
                except Exception as e:
                    response_text = f"Sorry, I couldn't reach the language model. {e}"

            self.conversation.add_assistant_message(response_text)
            audio_path = await self._text_to_speech(response_text)

            return {
                "text": response_text,
                "audio_path": str(audio_path),
                "action": intent,
            }
        finally:
            self.is_processing = False

    # ── Streaming respond ───────────────────────────────────────────

    async def _heartbeat_loop(self, interval: float = 5.0):
        """Background heartbeat: broadcasts ``{"type": "state_change", "status": "processing"}``
        at regular intervals while the LLM is actively generating.  This prevents
        the frontend from timing out during long-running generations (e.g. code
        snippets, large responses).

        The loop exits when ``self.is_processing`` becomes False or when
        ``self._interrupt_requested`` is set.

        Args:
            interval: Seconds between heartbeats (default 5.0).
        """
        while self.is_processing and not self._interrupt_requested:
            await asyncio.sleep(interval)
            if not self.is_processing or self._interrupt_requested:
                break
            self.ws_manager.fire(self.ws_manager.broadcast_state("processing"))
            print("[Responder] Heartbeat — processing...")

    async def stream_respond(self, user_input: str, confidence: float = 0.0) -> AsyncIterable[dict]:
        """Stream-process user input: LLM tokens → sentence-aware TTS chunks.

        Each yielded dict has the shape:
            {"text": "<sentence>", "audio_path": "<path-to-mp3>", "audio_pcm": (ndarray, sr)}

        ``audio_pcm`` is a tuple of (float32_array, sample_rate=24000) for
        in-memory playback — the caller can use this instead of loading from file.
        ``audio_path`` is kept for backward compatibility with interrupt handler.

        The caller should play each audio chunk immediately.  If
        ``self._interrupt_requested`` is set to True while iterating,
        the remaining stream is abandoned cleanly.

        Args:
            user_input: The transcribed text from the user.
            confidence: Confidence score of the transcription (0.0-1.0).
        """
        self.is_processing = True
        self._interrupt_requested = False
        # Broadcast processing state to frontend
        self.ws_manager.fire(self.ws_manager.broadcast_state("processing"))
        try:
            # ── 0. Small talk check (faster than LLM) ───────────────
            small_talk_reply = get_small_talk(user_input)
            if small_talk_reply:
                self.conversation.add_user_message(user_input)
                self.conversation.add_assistant_message(small_talk_reply)
                self.response_text = small_talk_reply
                # Broadcast speaking + caption_barq for small talk
                self.ws_manager.fire(self.ws_manager.broadcast_state("speaking"))
                self.ws_manager.fire(self.ws_manager.broadcast({
                    "type": "caption_barq",
                    "text": small_talk_reply,
                }))
                audio_path, audio_pcm = await self._text_to_speech_both(small_talk_reply)
                yield {
                    "text": small_talk_reply,
                    "audio_path": str(audio_path),
                    "audio_pcm": audio_pcm,
                }
                return

            # ── 1. Store user message & classify ─────────────────────
            self.conversation.add_user_message(user_input)
            intent = self._classify_intent(user_input)

            # Commands are short — use non-streaming path
            if intent == "command":
                self.response_text = "Processing command..."
                result = await self.respond(user_input)
                # Broadcast speaking + caption_barq for command response
                self.ws_manager.fire(self.ws_manager.broadcast_state("speaking"))
                self.ws_manager.fire(self.ws_manager.broadcast({
                    "type": "caption_barq",
                    "text": result.get("text", ""),
                }))
                # Add PCM data to command results (generated once)
                if "audio_path" in result:
                    try:
                        _, audio_pcm = await self._text_to_speech_both(result["text"])
                        result["audio_pcm"] = audio_pcm
                    except Exception:
                        pass
                self.response_text = result.get("text", "")
                yield result
                return

            # ── 2. Streaming LLM ────────────────────────────────────
            context = self.conversation.get_context()
            buffer = ""
            full_text = ""
            self.response_text = ""  # reset for new turn
            _ttfb_recorded = False  # track time-to-first-token

            # Start heartbeat task to keep frontend alive during long generations
            heartbeat_task = self.ws_manager.fire(self._heartbeat_loop())

            # ── Wrap LLM stream with first-token timeout ────────────
            # If the LLM doesn't produce the first token within 15 seconds,
            # fall back to a graceful apology instead of hanging silently.
            _LLM_FIRST_TOKEN_TIMEOUT = 15.0  # seconds
            _LLM_TOTAL_TIMEOUT = 60.0        # seconds total

            llm_stream = self.llm.stream_chat(context)
            llm_iter = llm_stream.__aiter__()

            try:
                # Wait for first token with timeout
                first_token_task = asyncio.ensure_future(llm_iter.__anext__())
                try:
                    token = await asyncio.wait_for(first_token_task, timeout=_LLM_FIRST_TOKEN_TIMEOUT)
                    # ── First token received ────────────────────
                    if self._interrupt_requested:
                        return
                    _ttfb_recorded = True
                    ttfb_ms = (time.perf_counter() - self.wake_word_timestamp) * 1000 if self.wake_word_timestamp > 0 else 0.0
                    self.evo_logger.record("ttfb", duration_ms=ttfb_ms, metadata={"model": "ollama"})
                    buffer += token
                    full_text += token
                    self.response_text = full_text
                except asyncio.TimeoutError:
                    # First token took too long — graceful fallback
                    print(f"[Responder] LLM first-token timeout after {_LLM_FIRST_TOKEN_TIMEOUT}s")
                    self.evo_logger.record("ttfb", duration_ms=_LLM_FIRST_TOKEN_TIMEOUT * 1000,
                                           metadata={"model": "ollama", "timeout": True})
                    full_text = "I'm sorry, the response is taking longer than expected. Please try again."
                    self.conversation.add_assistant_message(full_text)
                    self.ws_manager.fire(self.ws_manager.broadcast_state("speaking"))
                    self.ws_manager.fire(self.ws_manager.broadcast({
                        "type": "caption_barq", "text": full_text,
                    }))
                    audio_path, audio_pcm = await self._text_to_speech_both(full_text)
                    yield {"text": full_text, "audio_path": str(audio_path), "audio_pcm": audio_pcm}
                    return

                # ── Continue streaming remaining tokens ──────────
                # IMPORTANT: use `llm_iter` (the SAME iterator that already consumed
                # the first token), NOT `llm_stream` which would create a fresh
                # iterator and duplicate the first token.
                async for token in llm_iter:
                    if self._interrupt_requested:
                        break
                    buffer += token
                    full_text += token
                    self.response_text = full_text  # update live display text

                    # Flush complete sentences from the buffer
                    parts = _split_sentences(buffer)
                    if len(parts) > 1:
                        # All parts except the last are guaranteed complete
                        complete = parts[:-1]
                        buffer = parts[-1]  # keep the potentially-incomplete tail
                        for sentence in complete:
                            if sentence.strip():
                                # Broadcast speaking state + caption_barq for each sentence chunk
                                self.ws_manager.fire(self.ws_manager.broadcast_state("speaking"))
                                self.ws_manager.fire(self.ws_manager.broadcast({
                                    "type": "caption_barq",
                                    "text": sentence,
                                }))
                                audio_path, audio_pcm = await self._text_to_speech_both(sentence)
                                yield {
                                    "text": sentence,
                                    "audio_path": str(audio_path),
                                    "audio_pcm": audio_pcm,
                                }
            except Exception as e:
                print(f"[Responder] LLM streaming error: {e}")
                # Log LLM streaming error to evolution tracker
                self.evo_logger.record("llm_error", metadata={
                    "source": "responder",
                    "error": str(e)[:120],
                    "has_partial": bool(full_text.strip()),
                })
                # If we got partial text, use it as-is
                if not full_text.strip():
                    full_text = f"Sorry, I encountered an error. {e}"
                    # Broadcast speaking + error caption
                    self.ws_manager.fire(self.ws_manager.broadcast_state("speaking"))
                    self.ws_manager.fire(self.ws_manager.broadcast({
                        "type": "caption_barq",
                        "text": full_text,
                    }))
                    audio_path, audio_pcm = await self._text_to_speech_both(full_text)
                    yield {
                        "text": full_text,
                        "audio_path": str(audio_path),
                        "audio_pcm": audio_pcm,
                    }
                    return

            # ── 3. Flush remaining buffer ───────────────────────────
            if buffer.strip() and not self._interrupt_requested:
                # Broadcast speaking + remaining caption
                self.ws_manager.fire(self.ws_manager.broadcast_state("speaking"))
                self.ws_manager.fire(self.ws_manager.broadcast({
                    "type": "caption_barq",
                    "text": buffer,
                }))
                audio_path, audio_pcm = await self._text_to_speech_both(buffer)
                yield {
                    "text": buffer,
                    "audio_path": str(audio_path),
                    "audio_pcm": audio_pcm,
                }

            # ── 4. Store complete response in history ───────────────
            if full_text.strip():
                self.conversation.add_assistant_message(full_text)

        finally:
            # Cancel the heartbeat task if it was started
            try:
                heartbeat_task.cancel()
            except (NameError, Exception):
                pass
            self.is_processing = False
            self._interrupt_requested = False
            self.response_text = ""  # clear live display text when turn ends

    # ── Intent classification ────────────────────────────────────────

    def _classify_intent(self, text: str) -> str:
        """Determine if user wants a command executed or just chatting."""
        command_keywords = [
            "open", "close", "run", "scan", "create", "delete",
            "search", "find", "show", "set", "change", "toggle",
            "minimize", "maximize", "launch", "start", "stop",
            "shut up", "silence",
        ]
        text_lower = text.lower().strip()
        for kw in command_keywords:
            if text_lower.startswith(kw):
                return "command"
        return "conversation"

    # ── Command handler ──────────────────────────────────────────────

    async    def _handle_command(self, text: str) -> str:
        """Route a command and return a human-readable result."""
        exit_phrases = ["nothing", "goodbye", "bye", "exit", "end conversation",
                        "stop conversation", "that's all", "we're done",
                        "go to sleep", "that's it for now"]
        if any(p in text.lower() for p in exit_phrases):
            self.conversation.end_session()
            return "Goodbye! Say 'computer' when you need me again."

        try:
            from voice.routes import _parse_and_route
            result = await _parse_and_route(text, is_follow_up=False, last_intent=None)
            action = result.get("action", "unknown")

            if action == "run_command_confirm":
                cmd = result.get("command", "")
                tier = result.get("tier", "warn")
                desc = result.get("tier_description", "")
                tier_upper = "DANGEROUS" if tier == "dangerous" else "MODERATE RISK"
                return (
                    f"The command {cmd} is flagged as {tier_upper}. "
                    f"{desc} "
                    f"Say 'yes' to confirm or 'cancel' to abort."
                )

            if action == "run_command_execute":
                return result.get("message", "Command approved and executed.")

            if action == "cancel":
                return "Command cancelled."

            if action == "agent_task":
                goal = result.get("goal", text)
                try:
                    from agent.agent_executor import AgentExecutor
                    executor = AgentExecutor()
                    task_result = await executor.execute(goal=goal)
                    return task_result
                except Exception as e:
                    print(f"[Responder] Agent task error: {e}")
                    # Fallback: try conversational response via LLM
                    context = self.conversation.get_context()
                    try:
                        return await self.llm.chat(context)
                    except Exception:
                        return "I'll work on that now and let you know when I'm done."

            if action == "unknown":
                context = self.conversation.get_context()
                try:
                    return await self.llm.chat(context)
                except Exception:
                    return "Sorry, I didn't understand that command."

            action_text = action.replace("_", " ")
            target = result.get("target", "") or result.get("query", "")
            if target:
                return f"Okay, I'll {action_text} {target}."
            return f"Okay, {action_text}."
        except Exception as e:
            return f"I heard: '{text}' but couldn't process it. {e}"

    # ── TTS helpers ─────────────────────────────────────────────────

    async def _text_to_speech(self, text: str) -> Path:
        """Convert text to speech using edge-tts (file-based, for backward compatibility).

        Returns:
            Path to the saved MP3 file.
        """
        content_hash = hashlib.md5(text.encode()).hexdigest()[:12]
        output_path = AUDIO_DIR / f"response_{content_hash}.mp3"

        if not output_path.exists():
            communicate = edge_tts.Communicate(text, self.tts_voice)
            await communicate.save(str(output_path))

        return output_path

    async def _text_to_speech_both(self, text: str) -> tuple[Path, tuple[np.ndarray, int]]:
        """Generate TTS audio once, returning both file path and in-memory PCM data.

        This avoids calling edge-tts twice for the same text (which would
        double latency).  The MP3 bytes are streamed once, saved to file,
        and decoded to PCM for direct playback.

        Returns:
            Tuple of (output_path, (pcm_float32_array, sample_rate=24000))
        """
        content_hash = hashlib.md5(text.encode()).hexdigest()[:12]
        output_path = AUDIO_DIR / f"response_{content_hash}.mp3"

        _tts_start = time.perf_counter()
        was_cached = False

        if output_path.exists():
            # File exists — load cached bytes for PCM decode
            mp3_bytes = output_path.read_bytes()
            was_cached = True
        else:
            # Generate TTS audio once
            communicate = edge_tts.Communicate(text, self.tts_voice)
            audio_chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])
            mp3_bytes = b"".join(audio_chunks)
            output_path.write_bytes(mp3_bytes)

        # Decode MP3 bytes to PCM in-memory via PyAV (lazy import)
        audio = self._decode_mp3_to_pcm(mp3_bytes)

        # Record TTS latency (including decode time)
        tts_duration = (time.perf_counter() - _tts_start) * 1000
        self.evo_logger.record("tts_latency", duration_ms=tts_duration, metadata={
            "text_length": len(text),
            "cached": was_cached,
            "voice": self.tts_voice,
        })

        return output_path, (audio, 24000)

    def _decode_mp3_to_pcm(self, mp3_bytes: bytes) -> np.ndarray:
        """Decode MP3 bytes to a float32 PCM array normalized to [-1.0, 1.0] at 24 kHz.

        Uses PyAV (av) under the hood — imported lazily so the module loads
        even if the optional ``av`` package is not installed.

        Returns:
            Float32 numpy array of audio samples.

        Raises:
            ImportError: If PyAV is not installed.
            ValueError: If no audio frames could be decoded.
        """
        try:
            import av
        except ImportError:
            raise ImportError(
                "PyAV (av) is required for PCM audio decoding. "
                "Install it with: pip install av"
            )

        container = av.open(io.BytesIO(mp3_bytes), format="mp3")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)

        pcm_chunks: list[np.ndarray] = []
        for frame in container.decode(audio=0):  # type: ignore[union-attr]
            resampled = resampler.resample(frame)
            for r in resampled:
                pcm_chunks.append(r.to_ndarray().flatten())
        container.close()

        if not pcm_chunks:
            raise ValueError("No audio frames decoded from TTS output")

        return np.concatenate(pcm_chunks).astype(np.float32) / 32768.0

    # ── Slash command: /ponytail-review ────────────────────────────

    async def _handle_ponytail_review(self) -> str:
        """Run a Ponytail review audit on the current git diff.

        Gets the diff, loads the Ponytail rules, and sends both to
        Ollama for a "lazy senior dev" over-engineering assessment.
        """
        import asyncio

        # 1. Get git diff (staged + unstaged)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            diff = stdout.decode(errors="replace")
            if not diff.strip():
                # Try just unstaged
                proc = await asyncio.create_subprocess_exec(
                    "git", "diff",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                diff = stdout.decode(errors="replace")
            if not diff.strip():
                return "No changes to review — working tree is clean."
        except Exception as e:
            return f"Could not run git diff: {e}"

        # 2. Load Ponytail rules
        try:
            from pathlib import Path
            rules_path = Path(__file__).parent.parent / "agent" / "PONYTAIL.md"
            ponytail_rules = rules_path.read_text(encoding="utf-8")
        except Exception as e:
            ponytail_rules = f"[Could not load Ponytail rules: {e}]"

        # 3. Build analysis prompt
        review_prompt = (
            "You are a lazy senior dev code reviewer. "
            "Review the following git diff against the Ponytail philosophy below.\n\n"
            f"## Ponytail Rules\n{ponytail_rules}\n\n"
            "## Git Diff to Review\n```\n"
            f"{diff[:6000]}"  # cap at ~6k chars to avoid prompt overflow
            + ("" if len(diff) <= 6000 else "\n[... truncated to first 6000 chars ...]")
            + "\n```\n\n"
            "Give a concise, honest review. For each change:"
            "\n- ❌ Over-engineered (too many files, abstractions, boilerplate, new deps)"
            "\n- ✅ Minimal and justified"
            "\n- 🔧 Could be simpler (suggest how)"
            "\n\nEnd with a summary: PASS, MINOR ISSUES, or OVER-ENGINEERED."
        )

        # 4. Send to Ollama
        messages = [
            {"role": "system", "content": "You are a lazy senior developer who reviews code for over-engineering. Be brutally honest and concise."},
            {"role": "user", "content": review_prompt},
        ]
        try:
            review = await self.llm.chat(messages)
            return review.strip()
        except Exception as e:
            return f"Review generation failed: {e}"

    # ── Text-only respond ──────────────────────────────────────────

    async def respond_text_only(self, user_input: str) -> str:
        """Quick text-only response (no audio generation)."""

        # ── Slash commands ──────────────────────────────────────────
        stripped = user_input.strip()
        if stripped.startswith("/ponytail-review"):
            return await self._handle_ponytail_review()
        if stripped.startswith("/"):
            return f"Unknown slash command: {stripped.split()[0]}"

        self.conversation.add_user_message(user_input)
        context = self.conversation.get_context()
        try:
            response_text = await self.llm.chat(context)
        except Exception as e:
            response_text = f"Error: {e}"
        self.conversation.add_assistant_message(response_text)
        return response_text
