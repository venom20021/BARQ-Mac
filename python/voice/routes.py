"""
FastAPI routes for voice control.
Handles wake word detection, speech transcription, TTS,
multi-turn conversation state, sensitivity control, and command history.
"""

import asyncio
import os
import re
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ai.responder import BARQResponder
from config import get_settings
from database import analytics_dao, db_connection, settings_dao
from system_control.command_whitelist import (
    DANGEROUS,
    SAFE,
    WARN,
    approve_command,
    classify_command,
    describe_classification,
    get_custom_rules,
    is_approved,
)

from . import (
    SpeechProcessor,
    WakeWordDetector,
    get_sound_settings,
    play_command_accepted_sound,
    set_sound_enabled,
)
from .action_log import DANGER as ACTION_DANGER
from .action_log import INFO, WARNING, get_recent_actions, log_action
from .conversation_listener import ConversationListener
from .evolution_logger import begin_voice_cycle, get_evolution_logger, mark_voice_cycle
from .websocket_manager import VoiceWSManager

from memory.agent_memory_manager import pop_last_session

router = APIRouter()


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

# Keep a reference to the ConversationListener singleton
# (used by voice agents for exit handling)
conversation_listener_singleton = None

# Singleton instances
wake_word_detector: WakeWordDetector | None = None
speech_processor = SpeechProcessor()
responder = BARQResponder()

conversation_listener = ConversationListener(
    stt=speech_processor,
    responder=responder,
)
conversation_listener_singleton = conversation_listener

# on_stop is set after _on_conversation_stopped is defined (below)
# parse_command and execute_command are set after _parse_and_route is defined

# Auto-start flag — set to True after first start
_auto_started = False

# ─── WebSocket Connection Manager for Voice ────────────────────────────
# Tracks all active /ws/status clients for instant state broadcasts.
# Use the modular VoiceWSManager for all broadcast operations.
_voice_ws = VoiceWSManager.get_instance()


def get_wake_word_detector() -> WakeWordDetector:
    """Get or create the wake word detector singleton."""
    global wake_word_detector
    if wake_word_detector is None:
        wake_word_detector = WakeWordDetector(
            on_wake_word=_on_wake_word_callback,
            on_conversation_trigger=_on_conversation_trigger,
        )
    return wake_word_detector


async def _gather_background_info() -> str:
    """Gather system info, jobs, weather, etc. asynchronously.

    Returns a formatted system-context string that can be injected into
    the conversation as a system message.  This runs in the background
    so the user can start talking immediately while the info arrives.
    """
    info_parts: list[str] = []

    # 1. System status
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        info_parts.append(
            f"System: CPU {cpu}%, mem {mem.percent}% used, {mem.available / (1024**3):.1f} GB free."
        )
    except Exception:
        info_parts.append("System is running.")

    # 2. Job search status
    try:
        row = await db_connection.fetch_one("SELECT COUNT(*) as count FROM job_listings")
        total_jobs = row["count"] if row else 0
        if total_jobs > 0:
            submitted = await db_connection.fetch_one(
                "SELECT COUNT(*) as count FROM applications WHERE status = 'submitted'"
            )
            interviews = await db_connection.fetch_one(
                "SELECT COUNT(*) as count FROM applications WHERE status = 'interview'"
            )
            queued = await db_connection.fetch_one(
                "SELECT COUNT(*) as count FROM applications WHERE status = 'queued'"
            )
            parts = [f"Jobs: {total_jobs} scanned"]
            if queued and queued["count"]:
                parts.append(f"{queued['count']} pending")
            if submitted and submitted["count"]:
                parts.append(f"{submitted['count']} applied")
            if interviews and interviews["count"]:
                parts.append(f"{interviews['count']} interviews")
            info_parts.append(", ".join(parts) + ".")
    except Exception:
        pass

    # 3. Weather
    try:
        import os as _os
        import httpx
        api_key = _os.getenv("OPENWEATHER_API_KEY", "")
        if api_key:
            city = await settings_dao.get_setting("weather_city") or "London"
            async with httpx.AsyncClient() as client:
                geo = await client.get(
                    "https://api.openweathermap.org/geo/1.0/direct",
                    params={"q": city, "limit": 1, "appid": api_key}, timeout=5,
                )
                geo_data = geo.json()
                if geo_data:
                    w = await client.get(
                        "https://api.openweathermap.org/data/2.5/weather",
                        params={"lat": geo_data[0]["lat"], "lon": geo_data[0]["lon"],
                                "appid": api_key, "units": "metric"}, timeout=5,
                    )
                    wj = w.json()
                    if "main" in wj:
                        info_parts.append(
                            f"Weather: {geo_data[0].get('name', 'your area')}, "
                            f"{wj['weather'][0]['description']}, "
                            f"{wj['main']['temp']:.0f}°C."
                        )
    except Exception:
        pass

    # 4. Stocks
    try:
        tickers_str = await settings_dao.get_setting("stock_tickers")
        if tickers_str:
            tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()][:5]
            if tickers:
                import yfinance as yf
                prices = []
                for t in tickers:
                    try:
                        s = yf.Ticker(t)
                        info = s.info
                        price = info.get("currentPrice")
                        if price:
                            chg = info.get("regularMarketChangePercent", 0)
                            direction = "up" if chg and chg >= 0 else "down"
                            prices.append(f"{t} ${price:.2f} ({direction} {abs(chg):.1f}%)")
                    except Exception:
                        continue
                if prices:
                    info_parts.append("Stocks: " + "; ".join(prices) + ".")
    except Exception:
        pass

    # 5. Headlines
    try:
        import os as _os
        import httpx
        news_key = _os.getenv("NEWS_API_KEY", "")
        if news_key:
            resp = await httpx.AsyncClient().get(
                "https://newsapi.org/v2/top-headlines",
                params={"language": "en", "pageSize": 3, "apiKey": news_key}, timeout=5,
            )
            articles = resp.json().get("articles", [])
            if articles:
                headlines = [a["title"].rstrip(".!?") for a in articles[:3] if a.get("title")]
                info_parts.append("News: " + ". ".join(headlines) + ".")
    except Exception:
        pass

    # 6. Interviews & tasks
    try:
        from datetime import date
        today = date.today().isoformat()
        upcoming_interviews = await db_connection.fetch_all(
            "SELECT j.company, a.interview_date FROM applications a "
            "JOIN job_listings j ON a.job_listing_id = j.id "
            "WHERE a.interview_date IS NOT NULL AND a.interview_date >= ? "
            "ORDER BY a.interview_date ASC LIMIT 3", (today,),
        )
        if upcoming_interviews:
            items = []
            for row in upcoming_interviews:
                label = "today" if row["interview_date"] == today else f"on {row['interview_date']}"
                items.append(f"{row['company']} {label}")
            info_parts.append("Interviews: " + ", ".join(items) + ".")
        tasks = await db_connection.fetch_all(
            "SELECT name FROM scheduled_tasks WHERE enabled = 1 AND "
            "(last_run IS NULL OR date(last_run) < ?) LIMIT 2", (today,),
        )
        if tasks:
            info_parts.append("Pending: " + ", ".join(t["name"] for t in tasks) + ".")
    except Exception:
        pass

    # 7. GitHub
    try:
        import os as _os
        import httpx
        gh_token = _os.getenv("GITHUB_TOKEN", "")
        if gh_token:
            resp = await httpx.AsyncClient().get(
                "https://api.github.com/notifications",
                headers={"Authorization": f"Bearer {gh_token}",
                         "Accept": "application/vnd.github.v3+json"}, timeout=5,
            )
            if resp.status_code == 200:
                n = resp.json()
                if n:
                    pulls = sum(1 for x in n if x["subject"]["url"].find("/pulls/") >= 0 or x["subject"]["type"] == "PullRequest")
                    issues = len(n) - pulls
                    parts = []
                    if pulls:
                        parts.append(f"{pulls} PR{'s' if pulls > 1 else ''}")
                    if issues:
                        parts.append(f"{issues} issue{'s' if issues > 1 else ''}")
                    info_parts.append("GitHub: " + " and ".join(parts) + " need attention.")
    except Exception:
        pass

    # 8. Notes
    try:
        total_row = await db_connection.fetch_one("SELECT COUNT(*) as count FROM notes")
        total = total_row["count"] if total_row else 0
        if total:
            pinned_row = await db_connection.fetch_one(
                "SELECT COUNT(*) as count FROM notes WHERE pinned = 1"
            )
            pinned = pinned_row["count"] if pinned_row else 0
            label = f"{total} note{'s' if total != 1 else ''}"
            if pinned:
                label += f", {pinned} pinned"
            info_parts.append(f"Notes: {label}.")
    except Exception:
        pass

    return "\n".join(info_parts) if info_parts else ""


def _fire_inject_background_info():
    """Fire background-info gathering on the MAIN loop, never the managed loop.

    ``_inject_background_info`` reads the DB (jobs, weather, notes, ...), so
    it must run on the main loop where ``db_connection`` lives.  Falls back
    to the local task tracker when no main loop has been captured yet.
    """
    from .loop_utils import run_on_main_loop
    coro = _inject_background_info()
    if run_on_main_loop(coro) is None:
        _voice_ws.fire(coro)


async def _inject_background_info():
    """Background task: gather info and inject it as a system message
    into the active conversation.  Runs concurrently with the conversation
    loop so the user can start talking immediately.

    Also injects the previous session's summary (morning recall) so BARQ
    remembers what was discussed last time.
    """
    print("[BackgroundInfo] Gathering system info in background...")
    try:
        info = await _gather_background_info()
        if info and responder.conversation.is_active:
            responder.conversation.add_system_message(
                f"Background context gathered at wake:\n{info}"
            )
            print(f"[BackgroundInfo] Injected {len(info)} chars into conversation context")
        elif info:
            print("[BackgroundInfo] Info gathered but conversation already ended — discarding")
        else:
            print("[BackgroundInfo] No info gathered")

        # ── Session memory recall (morning recall) ────────────────
        # Pop the last session summary so it's only injected once.
        # The summary is a short 1-2 sentence description of what was
        # discussed in the previous conversation.
        try:
            last_session = pop_last_session()
            if last_session and responder.conversation.is_active:
                summary = last_session.get("summary", "")
                session_date = last_session.get("date", "")
                if summary:
                    recall_msg = (
                        f"[Previous session — {session_date}]: {summary}\n"
                        f"Use this as context for the current conversation. "
                        f"If relevant, mention this naturally (e.g., 'Last time we talked about...')."
                    )
                    responder.conversation.add_system_message(recall_msg)
                    _safe_print(f"[SessionMemory] Injected session recall: {summary[:80]}…")
        except Exception as e:
            print(f"[SessionMemory] Recall injection error (non-fatal): {e}")

    except Exception as e:
        print(f"[BackgroundInfo] Error: {e}")


async def _speak_wake_greeting():
    """Lightweight wake greeting — play a chime and enter conversation mode.

    The wake chime is already played by ``play_wake_sound()`` in the
    wake word callback.  This function just starts the conversation loop
    immediately and spawns a background task to gather info asynchronously.
    """
    responder.is_processing = True
    try:
        # Log activity immediately
        await analytics_dao.log_activity("voice", "wake_greeting", "Wake word detected — lightweight greeting")

        # Fire off background info gathering (fire-and-forget) — runs on the
        # main loop so its DB reads (jobs/weather/notes) work.
        _fire_inject_background_info()

        print("[WakeGreeting] Lightweight greeting — entering conversation mode immediately")

    except Exception as e:
        print(f"[WakeGreeting] Error: {e}")

    # Enter conversation mode immediately — no blocking TTS or info gathering
    responder.is_processing = False
    await conversation_listener.start_conversation()


def _on_conversation_stopped():
    """Callback invoked when the conversation loop ends.

    Resumes the wake word detector so the user can trigger a new conversation
    by saying the wake word again.
    """
    global wake_word_detector
    if wake_word_detector is not None:
        wake_word_detector.resume()
        print("[Voice] Wake word detector resumed after conversation end")
    else:
        print("[Voice] No wake word detector to resume")

# Wire up the callback now that the function is defined
conversation_listener.on_stop = _on_conversation_stopped

# Auto-language detection is handled by the Deepgram Voice Agent.
# (The old SpeechProcessor auto-detection callback has been removed.)


def _on_wake_word_callback(utterance: str = ""):
    """Callback fired when wake word is detected by the background listener.

    Flow:
    1. Pause wake word detector → release mic for STT
    2. Push instant ``{"state": "listening"}`` to frontend WebSocket (<50ms)
    3. Play wake chime (handled by ``play_wake_sound()`` in detector)
    4. If command text after wake word, feed it to LLM immediately
    5. Enter conversation mode for follow-ups
    6. Background info gather (weather, jobs, etc.) runs concurrently

    When conversation ends, ``_on_conversation_stopped`` resumes the detector.

    Args:
        utterance: Full Vosk recognition text (wake word + any following command).
    """
    global _hands_free_mode
    global _wake_greeting_enabled
    global wake_word_detector

    # ── Step 0: Record wake word timestamp for TTFB tracking ──
    responder.wake_word_timestamp = time.perf_counter()
    # Open a voice-latency cycle. Every mark recorded from here on stores its
    # offset from this instant, so wake→connected→greeting→first-audio and any
    # pairwise delta between them are all derivable. Read via GET /voice/latency.
    begin_voice_cycle()

    # ── Step 1: Pause detector to free mic ──
    if wake_word_detector is not None:
        wake_word_detector.pause()
        print("[Voice] Wake word detector paused — waiting for mic release...")
        # Wait for the wake word thread to fully release the stream.
        # Uses a public method with proper try/finally guarantees.
        released = wake_word_detector.wait_for_stream_released(timeout=3.0)
        if not released:
            print("[Voice] Warning: Mic release timed out — proceeding anyway")
            try:
                import sounddevice as sd
                sd.stop()  # Force-release any PortAudio streams
            except Exception:
                pass
        # Small extra settle time for the OS audio stack
        import time as _time
        _time.sleep(0.05)
    else:
        print("[Voice] No wake word detector to pause")

    # The detector must release the mic before the voice agent can open it, so
    # this handoff sits inside wake→audio latency and is worth separating out
    # from the connection and greeting costs.
    mark_voice_cycle("voice_mic_released")

    # ── Step 2: Push instant "listening" state to frontend ──
    # Broadcast is handled inside _lightweight_wake_greeting() which runs
    # inside the proper event loop (loop.run_until_complete). We can't
    # broadcast here because _on_wake_word_callback is a synchronous
    # function called from the Vosk thread — no running event loop.
    # The broadcast_state("listening") fires as the first line of
    # _lightweight_wake_greeting below.
    pass

    # ── Step 3: Extract command after wake word ──
    command_text = _extract_command_after_wake_word(utterance)
    if command_text:
        print(f"[Voice] Extracted command from wake utterance: '{command_text}'")

    # If hands-free is off, just resume detector and return
    if not _hands_free_mode:
        print("[Voice] Hands-free mode disabled — resuming detector")
        _on_conversation_stopped()
        return

    # ── Step 4: Run the async flow in a new event loop ──
    #    (safe to call from the Vosk background thread)
    print("[Voice] Wake word detected — entering conversation mode")
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Lightweight entry: play chime + feed command + start conversation
        loop.run_until_complete(_lightweight_wake_greeting(command_text))

        # Keep loop alive for conversation listener
        if conversation_listener.is_active:
            conversation_listener._managed_loop = loop
            loop.run_forever()
            conversation_listener._managed_loop = None
        loop.close()
    except Exception as e:
        print(f"[Voice] Wake greeting error: {e}")


def _on_conversation_trigger():
    """Callback fired from the wake word detector.
    Conversation is now handled by _speak_wake_greeting() which runs after
    the wake word callback, so this is intentionally a no-op.
    """
    pass


async def _preload_whisper_model():
    """Stub — no longer needed. Deepgram Voice Agent handles STT."""
    pass


def _extract_command_after_wake_word(utterance: str) -> str:
    """Extract the command text that follows the wake word in an utterance.

    Handles common patterns:
    - "computer how are you" → "how are you"
    - "hey barq what's the weather" → "what's the weather"
    - "computer" → "" (just the wake word, no command)

    Args:
        utterance: The full Vosk recognition text.

    Returns:
        The command text after the wake word, stripped and lowercased.
        Empty string if only the wake word was spoken.
    """
    import re
    if not utterance or not utterance.strip():
        return ""

    text = utterance.strip().lower()
    cfg = get_settings()
    wake_word = cfg.wake_word.lower().strip()

    # Try to remove the wake word prefix
    # Common wake word prefixes: "computer", "ok computer", "hey barq", "wake up barq"
    # Try exact wake word match at the start
    if text.startswith(wake_word):
        rest = text[len(wake_word):].strip()
        if rest:
            return rest
        return ""

    # Try "hey" prefix variations
    wake_parts = wake_word.split()
    primary = wake_parts[-1] if wake_parts else wake_word

    # "hey <primary>"
    hey_patterns = [rf"^hey\s+{re.escape(primary)}\s+(.+)$", rf"^ok\s+{re.escape(primary)}\s+(.+)$", rf"^wake\s+up\s+{re.escape(primary)}\s+(.+)$"]

    for pattern in hey_patterns:
        m = re.match(pattern, text)
        if m:
            return m.group(1).strip()

    # If none of the patterns matched but the utterance contains the wake
    # word somewhere, just return everything after it
    if primary in text:
        idx = text.index(primary) + len(primary)
        rest = text[idx:].strip()
        if rest:
            return rest

    return ""


async def _start_conversation_with_command(command_text: str):
    """Process the command text, then start conversation mode for follow-ups.

    Used when the wake greeting is disabled but the utterance contains
    additional text beyond the wake phrase (e.g. "computer how are you").

    The command is processed FIRST (text-only, no audio), then the
    conversation loop is started so the user can ask follow-up questions
    without re-triggering the wake word.
    """
    if command_text:
        result = await responder.respond_text_only(command_text)
        if result:
            print(f"[Voice] Direct command response: {result[:100]}...")
    await conversation_listener.start_conversation()


async def _lightweight_wake_greeting(command_text: str = ""):
    """Lightweight wake entry: process command + fire background info + start conversation.

    This is the new Alexa-style flow:
    - The wake chime already played in ``play_wake_sound()`` (called by detector)
    - If a command followed the wake word, feed it to the LLM
    - Fire a background task to gather system info (jobs, weather, etc.)
    - Enter conversation mode so the user can speak immediately

    IMPORTANT: Each step is wrapped in its own try/except so that a failure
    in analytics logging or command processing NEVER prevents the conversation
    loop from starting.  This fixes the "greeting error → mic never activates"
    bug where an ``asyncio.timeout()`` RuntimeError (from a library like httpx
    when run outside a running Task) would skip ``start_conversation()``.
    """
    # ── Step 0: Broadcast "listening" state immediately ──
    # This runs inside the proper event loop (created by _on_wake_word_callback)
    _voice_ws.fire(_voice_ws.broadcast_state("listening"))

    responder.is_processing = True

    # ── Step 1: Log wake activity (own try/except — NEVER block conversation) ──
    try:
        if command_text:
            await analytics_dao.log_activity(
                "voice", "wake_with_command",
                f"Wake word + command: {command_text[:60]}"
            )
        else:
            await analytics_dao.log_activity("voice", "wake_greeting", "Wake word detected")
    except Exception as e:
        print(f"[Voice] Analytics log error (non-fatal): {e}")

    # ── Step 2: Fire background info gathering (fire-and-forget — own try/except) ──
    # Runs on the MAIN loop so its DB reads (jobs/weather/notes) never hit the
    # managed voice loop's cross-loop "different loop" error.
    try:
        _fire_inject_background_info()
    except Exception as e:
        print(f"[Voice] Background info fire error (non-fatal): {e}")

    # ═══ Step 3: START CONVERSATION (ALWAYS — own try/except) ═══
    # Start the conversation loop BEFORE processing the command, so that
    # even if the LLM call fails (asyncio timeout, Ollama unavailable, etc.),
    # the conversation loop is already active and the user can speak again.
    try:
        await conversation_listener.start_conversation()
    except Exception as e:
        print(f"[Voice] start_conversation error (will retry): {e}")
        # Last-resort: start conversation directly without the pipeline
        # Only run fallback if conversation isn't already active (guard against
        # double-start if start_conversation() partially completed)
        if not conversation_listener.is_active:
            try:
                responder.conversation.start_session("voice_conversation")
                conversation_listener._conversation_active = True
                conversation_listener._loop_task = asyncio.create_task(
                    conversation_listener._conversation_loop()
                )
                print("[Voice] Conversation started via fallback path")
            except Exception as e2:
                print(f"[Voice] Fallback conversation start also failed: {e2}")

    # ═══ Step 4: Process command text via Deepgram Voice Agent ═══
    try:
        if command_text:
            # Persist the wake-and-command utterance to agent_chat_history
            # (loop-safe: marshals the DB write onto the main loop)
            from .agent_history_sync import schedule_persist_voice_utterance
            schedule_persist_voice_utterance(command_text)
            print(f"[Voice] Processing wake command: '{command_text}' via Voice Agent")
            try:
                responder.is_speaking_event.set()
                responder.is_speaking = True
                # Use the responder's respond method (which uses BARQ's LLM)
                # for immediate wake-command responses.
                result = await responder.respond(command_text)
                responder.is_speaking = False
                if result:
                    print(f"[Voice] Command result: {str(result)[:100]}")
            except Exception as e:
                print(f"[Voice] Command respond error (continuing): {e}")
            finally:
                responder.is_speaking_event.clear()
        else:
            print("[Voice] No command — entering conversation mode directly")
    except Exception as e:
        print(f"[Voice] Command processing error (non-fatal, conversation running): {e}")
    finally:
        responder.is_processing = False


async def _speak_greeting_and_feed_command(command_text: str):
    """Legacy: speak a brief acknowledgment + LLM response, then enter conversation mode.

    Kept for backward compatibility.  New code should use
    ``_lightweight_wake_greeting()`` instead.
    """
    return await _lightweight_wake_greeting(command_text)


async def load_sound_settings():
    """Load saved sound preferences and sensitivity from the database on startup.
    This ensures user's mute preferences and sensitivity persist across app restarts."""
    global _sensitivity, _vad_silence_timeout, _energy_threshold

    # Load sound preferences
    wake_val = await settings_dao.get_setting("wake_sound_enabled")
    command_val = await settings_dao.get_setting("command_sound_enabled")

    if wake_val is not None:
        set_sound_enabled("wake", wake_val.lower() == "true")
    if command_val is not None:
        set_sound_enabled("command", command_val.lower() == "true")

    current = get_sound_settings()
    print(f"[Voice] Sound settings loaded from DB: {current}")

    # Load VAD silence timeout from DB
    vad_val = await settings_dao.get_setting("vad_silence_timeout")
    if vad_val is not None:
        try:
            _vad_silence_timeout = float(vad_val)
            if _vad_silence_timeout < 0.1 or _vad_silence_timeout > 3.0:
                _vad_silence_timeout = 0.4
            print(f"[Voice] VAD silence timeout loaded from DB: {_vad_silence_timeout}s")
        except (ValueError, TypeError):
            pass
    else:
        print(f"[Voice] No saved VAD timeout in DB, using default: {_vad_silence_timeout}s")

    # Load VAD energy threshold from DB and apply to the voice agent
    energy_val = await settings_dao.get_setting("vad_energy_threshold")
    if energy_val is not None:
        try:
            _energy_threshold = float(energy_val)
            if _energy_threshold < 50 or _energy_threshold > 2000:
                _energy_threshold = 250.0
            print(f"[Voice] VAD energy threshold loaded from DB: {_energy_threshold}")
            await _apply_energy_threshold(_energy_threshold)
        except (ValueError, TypeError):
            pass
    else:
        print(f"[Voice] No saved VAD energy threshold in DB, using default: {_energy_threshold}")
    # Load saved sensitivity level from DB
    sens_val = await settings_dao.get_setting("wake_word_sensitivity")
    if sens_val is not None and sens_val.lower() in ("low", "medium", "high"):
        _sensitivity = sens_val.lower()
        if wake_word_detector is not None:
            wake_word_detector.set_sensitivity(_sensitivity)
        print(f"[Voice] Sensitivity loaded from DB: {_sensitivity}")
    else:
        print(f"[Voice] No saved sensitivity in DB, using default: {_sensitivity}")

    # Load hands-free conversation mode setting
    hf_val = await settings_dao.get_setting("hands_free_mode")
    if hf_val is not None:
        global _hands_free_mode, _language, _tts_voice
        _hands_free_mode = hf_val.lower() == "true"
        print(f"[Voice] Hands-free mode loaded from DB: {_hands_free_mode}")

    # Load wake greeting enabled setting
    wg_val = await settings_dao.get_setting("wake_greeting_enabled")
    if wg_val is not None:
        global _wake_greeting_enabled
        _wake_greeting_enabled = wg_val.lower() == "true"
        print(f"[Voice] Wake greeting enabled loaded from DB: {_wake_greeting_enabled}")

    # ── Silent Language Persistence ──
    # Auto-load the last saved language from DB so the user never has to
    # manually re-select their language after restart — it adapts silently
    # (like Mark-L's "Silent Language Memory" feature).
    lang_val = await settings_dao.get_setting("voice_language")
    if lang_val is not None and lang_val.lower() in ("en", "hi"):
        lang = lang_val.lower()
        if lang != _language:
            _language = lang
            speech_processor.stt_language = lang
            if wake_word_detector is not None:
                wake_word_detector.set_language(lang)
            if lang == "hi":
                _tts_voice = "hi-IN-SwaraNeural"
            print(f"[Voice] Language silently restored from DB: {lang}")
        else:
            print(f"[Voice] Language already set to: {lang}")
    else:
        print(f"[Voice] No saved language in DB, using default: {_language}")

# ─── Multi-turn conversation state ─────────────────────────────────────────

_tts_voice: str = "aura-2-odysseus-en"
_sensitivity: str = "medium"
_language: str = "en"  # "en" or "hi"
_last_detected_language: str = ""  # set by auto-detection callback (not manual switch)
_last_detected_at: str = ""  # ISO timestamp of last auto-detection
_hands_free_mode: bool = True  # Alexa/Gemini-style hands-free conversation mode
_wake_greeting_enabled: bool = True  # Speak greeting when wake word is detected (separate from hands-free)
_vad_silence_timeout: float = 0.4  # VAD endpointing silence threshold in seconds (300-500ms range)
_energy_threshold: float = 250.0   # RMS energy threshold for voice activity detection
# Conversation manager is now managed by the shared responder instance
# _responder and conversation_listener are the canonical singletons above


async def _apply_energy_threshold(value: float) -> None:
    """Push the RMS energy threshold to the live voice agent (if any).

    Applies the threshold to the currently cached voice agent instance
    so the change takes effect immediately without a reconnect.  If no
    agent has been instantiated yet, the value is simply persisted and
    the next agent picks it up from the DB on ``connect()`` — we do NOT
    create an agent just to set a threshold (avoids eager init side
    effects at startup).
    """
    try:
        from .agent_factory import get_cached_voice_agent
        agent = get_cached_voice_agent()
        setter = getattr(agent, "set_energy_threshold", None) if agent is not None else None
        if setter is not None:
            setter(int(value))
            print(f"[Voice] Applied energy_threshold={int(value)} to live voice agent")
    except Exception as e:
        print(f"[Voice] Could not apply energy threshold to live agent (non-fatal): {e}")


# ─── Pending command approval state ────────────────────────────────
# Stores the command + tier awaiting voice confirmation (yes/no).
# Cleared after a confirmation decision is made.
_pending_run_command: dict[str, Any] = {
    "command": None,
    "tier": None,
}


class CommandRequest(BaseModel):
    command: str
    confidence: float = 0.0


class ChatRequest(BaseModel):
    message: str
    language: str = "en"


class SensitivityRequest(BaseModel):
    level: str  # low, medium, high


class LanguageRequest(BaseModel):
    language: str  # "en" or "hi"


class TTSVoiceRequest(BaseModel):
    voice: str


class SoundSettingsRequest(BaseModel):
    wake_sound_enabled: Optional[bool] = None
    command_sound_enabled: Optional[bool] = None


class SoundPreviewRequest(BaseModel):
    profile: str  # "wake" or "command_accepted"


class WakeWordRequest(BaseModel):
    wake_word: str  # new wake word phrase


class ConversationModeRequest(BaseModel):
    enabled: bool  # enable or disable hands-free conversation mode


class WeatherCityRequest(BaseModel):
    city: str  # city name for weather in wake greeting


class VoiceSettingsRequest(BaseModel):
    """Unified voice settings — all fields optional, only provided fields are updated."""
    vad_silence_timeout: Optional[float] = None  # seconds (0.1–3.0)
    energy_threshold: Optional[float] = None     # RMS floor (50–2000)
    language: Optional[str] = None               # "en" or "hi"


class VADSettingsRequest(BaseModel):
    silence_timeout: float  # seconds of silence before VAD endpointing (0.1–3.0)


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/sound-preview")
async def preview_sound(request: SoundPreviewRequest):
    """Play a sound profile through speakers for preview. Used by the Sounds tab.
    Bypasses the mute gate so users can always hear the preview."""
    if request.profile not in ("wake", "command_accepted"):
        raise HTTPException(status_code=400, detail="Invalid profile. Use: wake, command_accepted")

    from .wake_word import _generate_chime_wav, _play_wav
    _play_wav(_generate_chime_wav(request.profile), f"{request.profile} preview")

    return {"status": "played", "profile": request.profile}


@router.get("/sound-settings")
async def get_sound_settings_endpoint():
    """Get current sound preference state (wake chime, command accepted ping)."""
    return get_sound_settings()


@router.post("/sound-settings")
async def set_sound_settings_endpoint(request: SoundSettingsRequest):
    """Update sound preferences and persist to database."""
    changes = []
    if request.wake_sound_enabled is not None:
        set_sound_enabled("wake", request.wake_sound_enabled)
        await settings_dao.set_setting(
            "wake_sound_enabled", "true" if request.wake_sound_enabled else "false", "voice"
        )
        changes.append(f"wake={request.wake_sound_enabled}")

    if request.command_sound_enabled is not None:
        set_sound_enabled("command", request.command_sound_enabled)
        await settings_dao.set_setting(
            "command_sound_enabled", "true" if request.command_sound_enabled else "false", "voice"
        )
        changes.append(f"command={request.command_sound_enabled}")

    await analytics_dao.log_activity("voice", "sound_settings", ", ".join(changes))
    return get_sound_settings()


@router.post("/start")
async def start_listening():
    """Start wake word detection using the always-on singleton."""
    global _auto_started
    # Ensure sound preferences are loaded before detection starts
    await load_sound_settings()

    # Start whisper model preloading in the background so it doesn't block
    # the startup. The first download can take 30-60s on slow connections.
    _voice_ws.fire(_preload_whisper_model())

    detector = get_wake_word_detector()

    # Check if Vosk model was loaded successfully
    if detector.model is None:
        msg = (
            "Vosk model not loaded. The 'models/vosk/' directory is missing model files. "
            "Download with:\n"
            "  cd python/models && curl -L -o vosk-model-small-en-us-0.15.zip "
            "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip && "
            "tar -xf vosk-model-small-en-us-0.15.zip && "
            "move vosk-model-small-en-us-0.15 vosk"
        )
        print(f"[Voice] {msg}")
        return {
            "status": "error",
            "message": msg,
            "wake_word": get_settings().wake_word,
        }

    # Apply the loaded sensitivity (from load_sound_settings) to the detector
    detector.set_sensitivity(_sensitivity)
    detector.start()
    _auto_started = True
    await analytics_dao.log_activity("voice", "start_listening", "Wake word detection started")
    return {"status": "listening", "wake_word": get_settings().wake_word}


@router.post("/stop")
async def stop_listening():
    """Mute the microphone: stop wake word detection AND any active conversation.

    The voice agent (Deepgram/Gemini) opens its OWN microphone stream during a
    conversation.  Stopping only the wake word detector leaves that stream open,
    so the OS mic stays live after the dashboard mute.  We must therefore end
    the conversation too — which releases the agent's input stream via
    ``agent.stop()``.

    ``stop_conversation()`` normally resumes the wake detector through its
    ``on_stop`` callback; we suppress that here so muting actually sticks.

    Every step is independently guarded.  Muting has to reach *all* consumers
    of the microphone even if one of them fails to tear down: an exception used
    to escape from step 1 and skip steps 2 and 3, leaving the wake detector
    running after the user pressed mute.  Partial failures are reported back in
    ``warnings`` rather than aborting the mute.
    """
    global wake_word_detector

    errors: list[str] = []

    # ── Step 1: End any active voice-agent conversation (releases its mic) ──
    if conversation_listener.is_active:
        saved_on_stop = conversation_listener.on_stop
        conversation_listener.on_stop = None  # don't auto-resume the detector
        try:
            await conversation_listener.stop_conversation()
            print("[Voice] Active conversation stopped as part of mute")
        except Exception as e:
            errors.append(f"conversation: {e}")
            print(f"[Voice] stop_conversation failed during mute (continuing): {e}")
        finally:
            conversation_listener.on_stop = saved_on_stop

    # ── Step 2: Stop the wake word detector (releases its mic stream) ──
    if wake_word_detector:
        try:
            wake_word_detector.stop()
        except Exception as e:
            errors.append(f"wake_word: {e}")
            print(f"[Voice] wake_word_detector.stop failed (continuing): {e}")

    # ── Step 3: Fallback — end any lingering conversation session ──
    # (e.g. one started via /conversation/start without the agent loop)
    try:
        if responder.conversation.is_active:
            responder.conversation.end_session()
    except Exception as e:
        errors.append(f"session: {e}")
        print(f"[Voice] end_session failed (continuing): {e}")

    try:
        await analytics_dao.log_activity("voice", "stop_listening", "Microphone muted")
    except Exception as e:
        print(f"[Voice] analytics log failed (non-fatal): {e}")

    if errors:
        return {"status": "stopped", "warnings": errors}
    return {"status": "stopped"}


@router.post("/sensitivity")
async def set_sensitivity(request: SensitivityRequest):
    """Set wake word detection sensitivity and apply to running detector."""
    global _sensitivity
    if request.level not in ("low", "medium", "high"):
        raise HTTPException(status_code=400, detail="Invalid sensitivity level. Use: low, medium, high")

    _sensitivity = request.level

    # Apply to running detector (live update — no restart needed)
    detector = get_wake_word_detector()
    detector.set_sensitivity(request.level)

    await settings_dao.set_setting("wake_word_sensitivity", request.level, "voice")
    await analytics_dao.log_activity("voice", "sensitivity", f"Sensitivity set to {request.level}")
    return {"status": "set", "level": request.level}


@router.get("/sensitivity")
async def get_sensitivity():
    """Get current wake word detection sensitivity."""
    return {"level": _sensitivity}


@router.get("/language")
async def get_language():
    """Get the current active language for wake word detection."""
    lang = _language
    if wake_word_detector is not None:
        lang = wake_word_detector.language
    return {"language": lang, "languages_available": ["en", "hi"]}


@router.post("/language")
async def set_language(request: LanguageRequest):
    """Switch wake word detection language between English and Hindi.
    Also auto-switches the TTS voice to match the selected language."""
    global _language, _tts_voice
    if request.language not in ("en", "hi"):
        raise HTTPException(status_code=400, detail="Language must be 'en' or 'hi'")

    _language = request.language
    # Propagate STT language so faster-whisper transcribes in the correct language
    speech_processor.stt_language = request.language
    detector = get_wake_word_detector()
    success = detector.set_language(request.language)

    if not success:
        raise HTTPException(status_code=400, detail="Hindi model not available. Download vosk-model-small-hi-0.22")

    # Update TTS voice to match the language
    if request.language == "hi":
        _tts_voice = "hi-IN-SwaraNeural"
    else:
        _tts_voice = "aura-2-odysseus-en"

    await settings_dao.set_setting("voice_language", request.language, "voice")
    await analytics_dao.log_activity("voice", "language", f"Switched to {request.language}, TTS: {_tts_voice}")
    return {"status": "set", "language": request.language, "tts_voice": _tts_voice}


@router.post("/chat")
async def chat(request: ChatRequest):
    """Send a message to BARQ's conversation AI and get a text+audio response.
    Uses ConversationManager for context and Ollama for LLM responses.
    Returns both the text response and base64-encoded TTS audio using the
    same Edge-TTS voice as the wake greeting."""
    try:
        result = await responder.respond(request.message)

        # Extract text from the response
        if isinstance(result, dict):
            text = result.get("text") or result.get("response") or str(result)
        elif isinstance(result, str):
            text = result
        else:
            text = str(result)

        # Synthesize TTS audio using same voice as wake greeting
        try:
            audio_bytes = await speech_processor.synthesize(text, voice=_tts_voice)
            import base64
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        except Exception:
            audio_b64 = None

        await analytics_dao.log_activity("voice", "chat", f"Chat: {request.message[:50]}...")

        response = {
            "text": text,
            "audio_base64": audio_b64,
        }
        if isinstance(result, dict):
            # Merge any additional fields from the original result
            for k, v in result.items():
                if k not in ("text", "response", "audio_base64"):
                    response[k] = v
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/text")
async def chat_text_only(request: ChatRequest):
    """Quick text-only chat (no audio generation)."""
    try:
        text = await responder.respond_text_only(request.message)

        # ── W5: Conversation Memory (fire-and-forget, non-blocking) ──
        # Extracts action items / facts / entities into the memory bus.
        # Fully guarded: a failure here NEVER affects the chat response.
        try:
            from config import get_settings
            if get_settings().conversation_memory_enabled:
                from agent.workflows.conversation_memory import process_conversation_turn
                asyncio.create_task(
                    process_conversation_turn(request.message, str(text))
                )
        except Exception as mem_err:
            print(f"[Voice] Conversation memory hook error (non-fatal): {mem_err}")

        return {"text": text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _set_tts_voice_internal(voice: str):
    """Internal helper to set TTS voice across all components."""
    global _tts_voice
    _tts_voice = voice
    speech_processor.tts_voice = voice
    responder.tts_voice = voice


@router.post("/set-tts-voice")
async def set_tts_voice(request: TTSVoiceRequest):
    """Set the TTS voice for speech synthesis."""
    _set_tts_voice_internal(request.voice)
    await settings_dao.set_setting("tts_voice", request.voice, "voice")
    await analytics_dao.log_activity("voice", "tts_voice", f"TTS voice set to {request.voice}")
    return {"status": "set", "voice": request.voice}


@router.get("/tts-voices")
async def list_tts_voices():
    """List available TTS voices from all backends."""
    # Deepgram Aura-2 voices
    deepgram_voices = [
        {"id": "aura-2-odysseus-en", "name": "Odysseus", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-hera-en", "name": "Hera", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-athena-en", "name": "Athena", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-persephone-en", "name": "Persephone", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-ares-en", "name": "Ares", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-orion-en", "name": "Orion", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-helios-en", "name": "Helios", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-arcas-en", "name": "Arcas", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-stella-en", "name": "Stella", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-luna-en", "name": "Luna", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-nova-en", "name": "Nova", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-iris-en", "name": "Iris", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-asteria-en", "name": "Asteria", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-selene-en", "name": "Selene", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-aphrodite-en", "name": "Aphrodite", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-hades-en", "name": "Hades", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-poseidon-en", "name": "Poseidon", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-zeus-en", "name": "Zeus", "locale": "en-US", "backend": "deepgram"},
        {"id": "aura-2-demetra-en", "name": "Demetra", "locale": "en-US", "backend": "deepgram"},
    ]

    # Kokoro offline neural voices
    from .kokoro_tts import KNOWN_VOICES as kokoro_voices
    kokoro_list = [
        {"id": v["id"], "name": v["name"], "locale": v["locale"],
         "gender": v["gender"], "backend": "kokoro"}
        for v in kokoro_voices
    ]

    # Edge-TTS voices (partial list, common ones)
    edge_voices = [
        {"id": "en-US-AriaNeural", "name": "Aria", "locale": "en-US", "gender": "female", "backend": "edge-tts"},
        {"id": "en-US-GuyNeural", "name": "Guy", "locale": "en-US", "gender": "male", "backend": "edge-tts"},
        {"id": "en-US-JennyNeural", "name": "Jenny", "locale": "en-US", "gender": "female", "backend": "edge-tts"},
        {"id": "en-GB-SoniaNeural", "name": "Sonia", "locale": "en-GB", "gender": "female", "backend": "edge-tts"},
        {"id": "hi-IN-SwaraNeural", "name": "Swara", "locale": "hi-IN", "gender": "female", "backend": "edge-tts"},
    ]

    return {
        "voices": deepgram_voices + kokoro_list + edge_voices,
        "current": _tts_voice,
        "groups": {
            "deepgram": {"name": "Deepgram Cloud TTS", "count": len(deepgram_voices)},
            "kokoro": {"name": "Kokoro Offline Neural TTS", "count": len(kokoro_list), "offline": True},
            "edge-tts": {"name": "Edge TTS (online)", "count": len(edge_voices)},
        },
    }


@router.get("/tts-backend")
async def get_tts_backend():
    """Get the current TTS backend."""
    try:
        from .agent_factory import get_backend_from_db
        active_backend = await get_backend_from_db()
    except Exception:
        active_backend = "deepgram"

    return {
        "backend": "deepgram_agent",
        "available_backends": ["deepgram_agent"],
        "active_voice_agent": active_backend,
        "note": f"Voice agent backend: {active_backend}",
    }


class TTSBackendRequest(BaseModel):
    backend: str
    piper_model: Optional[str] = None


@router.post("/tts-backend")
async def set_tts_backend(request: TTSBackendRequest):
    """TTS backend selection."""
    valid_backends = {"deepgram_agent"}
    if request.backend not in valid_backends:
        raise HTTPException(status_code=400, detail=f"Backend must be: {', '.join(sorted(valid_backends))}")

    speech_processor.tts_backend = request.backend
    await settings_dao.set_setting("tts_backend", request.backend, "voice")
    await analytics_dao.log_activity("voice", "tts_backend", f"TTS backend set to {request.backend}")

    return {"status": "set", "backend": request.backend}


class STTBackendRequest(BaseModel):
    backend: str


@router.get("/stt-backend")
async def get_stt_backend():
    """Get the current STT backend."""
    try:
        from .agent_factory import get_backend_from_db
        active_backend = await get_backend_from_db()
    except Exception:
        active_backend = "deepgram"

    backends = ["deepgram_agent"]

    return {
        "backend": speech_processor.stt_backend,
        "available_backends": backends,
        "active_voice_agent": active_backend,
        "deepgram_configured": bool(get_settings().deepgram_api_key),
        "note": f"Voice agent: {active_backend}",
    }


@router.post("/stt-backend")
async def set_stt_backend(request: STTBackendRequest):
    """STT backend selection."""
    valid_backends = {"deepgram_agent"}
    if request.backend not in valid_backends:
        raise HTTPException(status_code=400, detail=f"Backend must be: {', '.join(sorted(valid_backends))}")

    speech_processor.stt_backend = request.backend
    await settings_dao.set_setting("stt_backend", request.backend, "voice")
    await analytics_dao.log_activity("voice", "stt_backend", f"STT backend set to {request.backend}")

    return {"status": "set", "backend": request.backend}


class VoiceBackendRequest(BaseModel):
    backend: str  # "deepgram" or "gemini"


@router.get("/backends")
async def list_voice_backends():
    """List all available voice agent backends with metadata."""
    from .agent_factory import get_available_backends
    backends = get_available_backends()

    try:
        from .agent_factory import get_backend_from_db
        active = await get_backend_from_db()
    except Exception:
        active = "deepgram"

    return {
        "backends": backends,
        "active": active,
    }


@router.get("/backend")
async def get_voice_backend():
    """Get the currently active voice agent backend."""
    from .agent_factory import get_backend_from_db
    active = await get_backend_from_db()
    return {
        "backend": active,
        "available": ["deepgram", "gemini"],
    }


@router.post("/backend")
async def set_voice_backend(request: VoiceBackendRequest):
    """Switch the voice agent backend between deepgram and gemini.

    If a conversation is active, it will be stopped and the new backend
    will be used immediately on the next wake word trigger.  The wake word
    detector is cleanly restarted so the pipeline resets to listening mode.
    """
    if request.backend not in ("deepgram", "gemini"):
        raise HTTPException(status_code=400, detail="Backend must be 'deepgram' or 'gemini'")

    # If conversation is active, stop it
    if conversation_listener.is_active:
        await conversation_listener.stop_conversation()

    # Reset the factory cache so the next get_voice_agent() call creates
    # the new backend
    from .agent_factory import reset_voice_agent
    reset_voice_agent()

    # Persist to DB
    await settings_dao.set_setting("voice_agent_backend", request.backend, "voice")

    # ── Reset voice pipeline to wake word listening mode ────────────────
    # Stop the current wake word detector, then restart it so the
    # pipeline is cleanly reset with the new backend.
    global wake_word_detector
    if wake_word_detector is not None:
        try:
            wake_word_detector.stop()
            print("[Voice] Wake word detector stopped for backend switch")
        except Exception as e:
            print(f"[Voice] Error stopping wake word detector: {e}")

    # Create fresh detector with new backend
    try:
        # Reset the global to force a fresh instance
        wake_word_detector = None
        detector = get_wake_word_detector()
        if detector.model is not None:
            detector.set_sensitivity(_sensitivity)
            detector.start()
            print(f"[Voice] Wake word detector restarted (backend: {request.backend})")
        else:
            print("[Voice] Wake word detector not restarted — Vosk model not loaded")
    except Exception as e:
        print(f"[Voice] Error restarting wake word detector: {e}")

    print(f"[Voice] Voice agent backend switched to: {request.backend}")

    await analytics_dao.log_activity(
        "voice", "backend_switch",
        f"Voice agent backend switched to {request.backend}",
    )

    return {
        "status": "set",
        "backend": request.backend,
        "note": "Voice pipeline reset — wake word listening",
    }


class VoiceAgentModeRequest(BaseModel):
    enabled: bool  # enable or disable Deepgram Voice Agent mode


@router.get("/agent")
async def get_agent_mode():
    """Get the current Voice Agent mode status."""
    from .agent_factory import get_backend_from_db
    active_backend = await get_backend_from_db()

    return {
        "voice_agent_enabled": True,
        "active_backend": active_backend,
        "deepgram_api_key_configured": bool(get_settings().deepgram_api_key),
        "note": f"Active voice agent: {active_backend}",
    }


@router.post("/agent")
async def set_agent_mode(request: VoiceAgentModeRequest):
    """Enable or disable Voice Agent mode.

    When enabled, the wake word triggers the configured voice agent
    (Deepgram or Gemini) which handles STT + LLM + TTS.
    When disabled, BARQ uses its local non-streaming pipeline.
    """
    if request.enabled and not get_settings().deepgram_api_key:
        print("[Voice] Voice Agent enabled but DEEPGRAM_API_KEY is not set")

    # If conversation is active, stop and restart
    if conversation_listener.is_active:
        await conversation_listener.stop_conversation()
        from .agent_factory import reset_voice_agent
        reset_voice_agent()

    print(f"[Voice] Voice Agent mode: {'ENABLED' if request.enabled else 'DISABLED'}")

    await settings_dao.set_setting(
        "voice_agent_enabled", "true" if request.enabled else "false", "voice"
    )
    await analytics_dao.log_activity(
        "voice", "voice_agent",
        f"Voice Agent mode {'enabled' if request.enabled else 'disabled'}",
    )

    return {
        "status": "set",
        "voice_agent_enabled": request.enabled,
    }


@router.get("/history")
async def get_command_history(limit: int = Query(default=50, ge=1, le=200)):
    """Get voice command history."""
    try:
        commands = await settings_dao.get_recent_commands(limit=limit)
        return {
            "commands": [
                {
                    "id": c.get("id"),
                    "transcript": c.get("transcript", ""),
                    "action": c.get("action", ""),
                    "success": c.get("success", False),
                    "confidence": c.get("confidence", 0.0),
                    "created_at": c.get("created_at", ""),
                }
                for c in commands
            ],
            "total": len(commands),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conversation/start")
async def start_conversation(topic: Optional[str] = None):
    """Start a multi-turn conversation session."""
    session_id = responder.conversation.start_session(topic)
    await analytics_dao.log_activity("voice", "conversation_start", f"Conversation started: {topic or 'general'}")
    return {"status": "started", "session_id": session_id}


@router.post("/conversation/end")
async def end_conversation():
    """End the current conversation session."""
    if not responder.conversation.is_active:
        return {"status": "no_active_session"}
    turns = responder.conversation.turn_count
    responder.conversation.end_session()
    await analytics_dao.log_activity("voice", "conversation_end", f"Conversation ended. Turns: {turns}")
    return {"status": "ended", "turns": turns}


@router.get("/conversation/context")
async def get_conversation_context():
    """Get the current conversation context."""
    if not responder.conversation.is_active:
        return {"active": False}
    return {
        "active": True,
        "session_id": responder.conversation.session_id,
        "turns": responder.conversation.turn_count,
        "recent_history": responder.conversation.get_recent_history(5),
    }


@router.post("/command")
async def process_command(request: CommandRequest):
    """
    Process a voice command text and route to the appropriate BARQ module.
    Supports multi-turn conversation context for follow-up commands.
    """
    command = request.command.lower().strip()

    # ─── Multi-turn conversation handling ──────────────────────────────
    cm = responder.conversation

    # Check if this is a follow-up in an active conversation
    is_follow_up = cm.is_active and cm.turn_count > 0

    # Add to conversation manager
    cm.add_user_message(command)

    # Log to database
    await settings_dao.log_command({
        "transcript": command,
        "confidence": request.confidence,
        "action": "processed",
        "processed": True,
    })

    # Persist to agent_chat_history so spoken commands feed the ai_chats graph
    # (loop-safe: marshals the DB write onto the main loop)
    from .agent_history_sync import schedule_persist_voice_utterance
    schedule_persist_voice_utterance(command)

    # Determine last intent from history
    last_intent = None
    if is_follow_up and cm.history:
        import json as _json
        for msg in reversed(cm.history):
            if msg["role"] == "assistant":
                try:
                    parsed = _json.loads(msg["content"])
                    if isinstance(parsed, dict):
                        last_intent = parsed.get("action")
                        break
                except (_json.JSONDecodeError, TypeError):
                    continue

    result = await _parse_and_route(command, is_follow_up, last_intent)

    # ─── Execute agent tasks directly when triggered via API ───────────
    if result.get("action") == "agent_task":
        goal = result.get("goal", command)
        try:
            from agent.agent_executor import AgentExecutor
            executor = AgentExecutor()
            task_result = await executor.execute(goal=goal)
            result["status"] = "completed"
            result["result"] = task_result
        except Exception as e:
            print(f"[Voice] Agent task error: {e}")
            result["status"] = "error"
            result["error"] = str(e)

    # Log every parsed command to the floating action log
    action = result.get("action", "unknown")
    if action != "unknown":
        desc = result.get("command", result.get("target", result.get("query", action.replace("_", " "))))
        severity = INFO
        if action in ("run_command_confirm",) and result.get("tier") in (WARN, DANGEROUS):
            severity = WARNING
        if result.get("tier") == DANGEROUS:
            severity = ACTION_DANGER
        await log_action(
            action=action,
            description=str(desc)[:120],
            severity=severity,
            metadata={"command": command[:80], "tier": result.get("tier")},
        )

    # Play command accepted sound + update conversation manager
    if action != "unknown":
        play_command_accepted_sound()
        if not cm.is_active:
            cm.start_session()

    # Store the result as a JSON string so follow-up detection can parse it back
    import json as _json
    cm.add_assistant_message(_json.dumps(result))

    return result


@router.post("/command/approve")
async def voice_approve_command(request: CommandRequest):
    """Approve a voice-parsed command for execution.
    Delegates to the system whitelist approval mechanism.
    Returns tier info so the caller knows if it's safe, warn, or dangerous.
    """
    cmd_text = request.command.strip()
    custom_rules = await get_custom_rules()
    tier = classify_command(cmd_text, custom_rules)

    if tier in (WARN, DANGEROUS):
        approve_command(cmd_text, tier)
        return {
            "status": "approved",
            "tier": tier,
            "tier_description": describe_classification(tier),
            "command": cmd_text,
            "message": f"{'DANGEROUS' if tier == DANGEROUS else 'MODERATE RISK'} command approved for this session via voice.",
        }

    return {
        "status": "already_safe",
        "tier": SAFE,
        "command": cmd_text,
        "message": "Command is classified as SAFE — no approval needed.",
    }


@router.post("/command/execute")
async def voice_execute_approved_command(request: CommandRequest):
    """Execute a command that has been pre-approved through the voice pipeline.
    Checks whitelist before executing, returning requires_approval if not yet approved.
    """
    cmd_text = request.command.strip()
    custom_rules = await get_custom_rules()
    tier = classify_command(cmd_text, custom_rules)

    # Check approval
    if tier == DANGEROUS and not is_approved(cmd_text, DANGEROUS):
        return {
            "status": "requires_approval",
            "tier": DANGEROUS,
            "tier_description": describe_classification(DANGEROUS),
            "command": cmd_text,
            "message": "This command is DANGEROUS. Approve via POST /voice/command/approve first.",
        }

    if tier == WARN and not is_approved(cmd_text, WARN):
        return {
            "status": "requires_approval",
            "tier": WARN,
            "tier_description": describe_classification(WARN),
            "command": cmd_text,
            "message": "This command is MODERATE RISK. Approve via POST /voice/command/approve first.",
        }

    # Execute via the system terminal endpoint
    from system_control.routes import CommandRequest as SysCommandRequest
    from system_control.routes import run_command as system_run_command

    sys_request = SysCommandRequest(command=cmd_text, cwd=None)
    result = await system_run_command(sys_request)

    # Log the voice-executed command
    await analytics_dao.log_activity(
        "voice", "command_execute",
        f"Voice executed: {cmd_text[:80]} — tier: {tier}"
    )

    return {
        "source": "voice",
        "tier": tier,
        **result,
    }


# ─── Unified Voice Settings API ────────────────────────────────────


@router.get("/settings")
async def get_voice_settings():
    """Get all current voice processing settings."""
    return {
        "vad_silence_timeout": _vad_silence_timeout,
        "energy_threshold": _energy_threshold,
        "language": _language,
        "languages_available": ["en", "hi"],
        "voice_agent": True,
        "note": "Deepgram Voice Agent handles STT/VAD/TTS — local settings are stored for reference",
    }


@router.post("/settings")
async def set_voice_settings(request: VoiceSettingsRequest):
    """Update voice processing settings at runtime.

    All fields are optional — only provided fields are updated and persisted.
    Changes take effect immediately (no restart needed).

    - ``vad_silence_timeout``: Seconds of silence before VAD endpoint cuts (0.1–3.0).
      Lower = faster response, higher = less cutting off natural pauses.
    - ``energy_threshold``: RMS energy floor for voice activity detection (50–2000).
      Lower = more sensitive, higher = less false triggers from background noise.
    - ``language``: Lock STT to this language ("en" or "hi"). Prevents auto-detection
      from misidentifying the language on short/noisy phrases.
    """
    global _vad_silence_timeout, _energy_threshold, _language, _tts_voice

    changes = []

    # ── Update VAD silence timeout (stored for reference) ──
    if request.vad_silence_timeout is not None:
        if request.vad_silence_timeout < 0.1 or request.vad_silence_timeout > 3.0:
            raise HTTPException(status_code=400, detail="vad_silence_timeout must be between 0.1 and 3.0")
        _vad_silence_timeout = request.vad_silence_timeout
        await settings_dao.set_setting("vad_silence_timeout", str(_vad_silence_timeout), "voice")
        changes.append(f"vad_silence_timeout={_vad_silence_timeout}s")
        print(f"[Voice] VAD silence timeout stored: {_vad_silence_timeout}s (Voice Agent handles VAD)")

    # ── Update energy threshold (applied live to the voice agent) ──
    if request.energy_threshold is not None:
        if request.energy_threshold < 50 or request.energy_threshold > 2000:
            raise HTTPException(status_code=400, detail="energy_threshold must be between 50 and 2000")
        _energy_threshold = request.energy_threshold
        await settings_dao.set_setting("vad_energy_threshold", str(_energy_threshold), "voice")
        changes.append(f"energy_threshold={_energy_threshold}")
        await _apply_energy_threshold(_energy_threshold)
        print(f"[Voice] VAD energy threshold stored + applied: {_energy_threshold}")

    # ── Update language ────────────────────────────────────────────────
    if request.language is not None:
        if request.language not in ("en", "hi"):
            raise HTTPException(status_code=400, detail="Language must be 'en' or 'hi'")
        _language = request.language
        speech_processor.stt_language = request.language
        detector = get_wake_word_detector()
        detector.set_language(request.language)
        if request.language == "hi":
            _tts_voice = "hi-IN-SwaraNeural"
        else:
            _tts_voice = "aura-2-odysseus-en"
        await settings_dao.set_setting("voice_language", request.language, "voice")
        changes.append(f"language={request.language}")
        print(f"[Voice] Language updated: {request.language}")

    if changes:
        await analytics_dao.log_activity(
            "voice", "voice_settings",
            f"Voice settings updated: {', '.join(changes)}",
        )

        _voice_ws.fire(_voice_ws.broadcast({
            "type": "settings_changed",
            "vad_silence_timeout": _vad_silence_timeout,
            "energy_threshold": _energy_threshold,
            "language": _language,
        }))

    return {
        "status": "updated" if changes else "no_changes",
        "changes": changes,
        "vad_silence_timeout": _vad_silence_timeout,
        "energy_threshold": _energy_threshold,
        "language": _language,
    }


# ─── Evolution Logger API ──────────────────────────────────────────


@router.get("/evolution")
async def get_evolution_events(
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    since: Optional[str] = Query(None, description="ISO-8601 start timestamp"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Query EvoMap evolution events for self-optimisation analysis.

    Returns a list of performance events (TTFB, buffer overflow, WS drops, etc.)
    recorded by the ``EvolutionLogger``.  Defaults to newest-first, capped at 100.
    """
    evo = get_evolution_logger()
    events = evo.query(
        event_type=event_type or None,
        since=since or None,
        limit=limit,
        offset=offset,
    )
    summary = evo.get_summary()
    return {
        "events": events,
        "summary": summary,
        "total_events": summary["total_events"],
    }


@router.get("/evolution/summary")
async def get_evolution_summary():
    """Get aggregate statistics across all recorded evolution events.

    Returns counts and average durations per event type, plus storage info.
    """
    evo = get_evolution_logger()
    return evo.get_summary()


async def _parse_and_route(command: str, is_follow_up: bool = False, last_intent: Optional[str] = None) -> dict[str, Any]:
    """
    Parse a voice command and determine the appropriate action.
    Supports follow-up context resolution for multi-turn conversations.
    """

    global _pending_run_command

    # ─── Conversation follow-up handling ────────────────────────────

    if is_follow_up and last_intent:
        # ─── Handle run_command_confirm (voice whitelist approval) ─
        if last_intent == "run_command_confirm":
            pending = _pending_run_command

            if command in ("yes", "yeah", "confirm", "do it", "go ahead", "okay"):
                cmd_text = pending.get("command")
                tier = pending.get("tier")
                if cmd_text and tier:
                    # Approve and execute the command
                    approve_command(cmd_text, tier)
                    _pending_run_command = {"command": None, "tier": None}
                    if tier == DANGEROUS:
                        tier_label = "DANGEROUS"
                    else:
                        tier_label = "MODERATE RISK"
                    tier_label_text = f"{tier_label} command approved. Executing..."
                    return {
                        "action": "run_command_execute",
                        "command": cmd_text,
                        "tier": tier,
                        "status": "approved",
                        "message": tier_label_text,
                        "follow_up": True,
                    }
                return {
                    "action": "run_command_execute",
                    "status": "error",
                    "message": "No pending command to execute.",
                    "follow_up": True,
                }

            if command in ("no", "nope", "cancel", "stop", "never mind", "abort"):
                _pending_run_command = {"command": None, "tier": None}
                return {
                    "action": "cancel",
                    "status": "cancelled",
                    "message": "Command cancelled.",
                    "follow_up": True,
                }

        # Handle follow-ups like "that one", "this", "the second"
        if command in ("that one", "this", "yes", "yeah", "confirm", "do it"):
            # _conversation_context was never defined; return a simple confirmation
            return {"action": last_intent, "status": "confirmed", "follow_up": True}

        if command in ("no", "nope", "cancel", "stop", "never mind"):
            return {"action": "cancel", "status": "cancelled", "follow_up": True}

        # Handle numbered selections
        selection_match = re.search(r"(?:the |number )?(first|second|third|fourth|fifth|\d+)", command)
        if selection_match:
            selection = selection_match.group(1)
            idx_map = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
            try:
                idx = idx_map.get(selection, int(selection) - 1) if selection.isdigit() else idx_map.get(selection, 0)
            except (ValueError, KeyError):
                idx = 0
            return {
                "action": f"{last_intent}_select",
                "selection_index": idx,
                "status": "selected",
                "follow_up": True,
            }

    # ─── System Commands ────────────────────────────────────────────

    if command in ("stop listening", "stop voice", "shut up", "silence"):
        return {"action": "voice_stop", "endpoint": "POST /voice/stop", "status": "triggered"}

    if command in ("start listening", "wake up", "computer", "hey computer"):
        return {"action": "voice_start", "endpoint": "POST /voice/start", "status": "triggered"}

    # ─── Conversation control ───────────────────────────────────────

    if command in ("start conversation", "let's talk", "begin session"):
        return {"action": "start_conversation", "status": "triggered"}

    if command in ("end conversation", "stop conversation", "that's all", "we're done"):
        return {"action": "end_conversation", "status": "triggered"}

    # ─── System Control Commands ────────────────────────────────────

    # Window management
    if "maximize" in command and ("window" in command or "barq" in command):
        return {"action": "window_control", "target": "maximize", "endpoint": "POST /system/window/control", "status": "triggered"}

    if "minimize" in command:
        if "all" in command:
            return {"action": "show_desktop", "status": "triggered"}
        return {"action": "window_control", "target": "minimize", "endpoint": "POST /system/window/control", "status": "triggered"}

    if "snap" in command or ("move" in command and "window" in command):
        if "left" in command:
            return {"action": "window_control", "target": "snap_left", "endpoint": "POST /system/window/control", "status": "triggered"}
        if "right" in command:
            return {"action": "window_control", "target": "snap_right", "endpoint": "POST /system/window/control", "status": "triggered"}

    if "resize" in command:
        size_match = re.search(r"(\d+)\s*x\s*(\d+)", command)
        if size_match:
            return {"action": "window_control", "target": "resize", "width": int(size_match.group(1)), "height": int(size_match.group(2)), "status": "parsed"}

    # App launcher: "open spotify", "launch chrome", "start terminal"
    app_match = re.search(r"(?:open|launch|start)\s+(.+)$", command)
    if app_match:
        app_name = app_match.group(1).strip().title()
        # Map common names
        app_map = {
            "vs code": "Visual Studio Code", "vscode": "Visual Studio Code",
            "code": "Visual Studio Code", "terminal": "Terminal",
            "spotify": "Spotify", "chrome": "Google Chrome",
            "browser": "Google Chrome", "firefox": "Firefox",
            "slack": "Slack", "discord": "Discord",
            "notion": "Notion", "obsidian": "Obsidian",
        }
        resolved_app = app_map.get(app_name.lower(), app_name)
        return {"action": "launch_app", "target": resolved_app, "endpoint": "POST /system/launch-app", "status": "parsed"}

    # Close app: "close chrome", "kill terminal"
    close_match = re.search(r"(?:close|kill|stop)\s+(.+)$", command)
    if close_match:
        return {"action": "close_app", "target": close_match.group(1).strip().title(), "endpoint": "POST /system/close-app", "status": "parsed"}

    # ─── File Commands ──────────────────────────────────────────────

    file_op_match = re.search(r"(?:create|make)\s+(?:folder|directory)\s+(.+)$", command)
    if file_op_match:
        return {"action": "create_folder", "target": file_op_match.group(1).strip(), "endpoint": "POST /system/file/create-folder", "status": "parsed"}

    read_match = re.search(r"(?:read|open|show)\s+(?:file\s+)?(.+)$", command)
    if read_match and "file" in command:
        return {"action": "read_file", "target": read_match.group(1).strip(), "endpoint": "POST /system/file/read", "status": "parsed"}

    if "sort" in command and ("folder" in command or "download" in command or "directory" in command):
        sort_match = re.search(r"sort\s+(?:my\s+)?(.+?)(?:\s+by\s+(type|date|size|name))?$", command)
        directory = sort_match.group(1) if sort_match else "downloads"
        sort_by = sort_match.group(2) if sort_match and sort_match.group(2) else "type"
        return {"action": "sort_files", "target": directory, "sort_by": sort_by, "endpoint": "POST /system/file/sort", "status": "parsed"}

    if "find" in command and "file" in command:
        search_match = re.search(r"find\s+(?:files?\s+)?(?:about\s+)?(.+)$", command)
        query = search_match.group(1) if search_match else command
        return {"action": "search_files", "query": query, "endpoint": "POST /system/file/search", "status": "parsed"}

    # ─── Developer Commands ─────────────────────────────────────────

    if "run" in command or "execute" in command:
        cmd_match = re.search(r"(?:run|execute)\s+(.+)$", command)
        if cmd_match and "scan" not in command and "match" not in command:
            cmd_text = cmd_match.group(1)
            custom = await get_custom_rules()
            tier = classify_command(cmd_text, custom)

            # Already approved in this session → proceed directly
            if is_approved(cmd_text, tier):
                return {
                    "action": "run_command",
                    "command": cmd_text,
                    "endpoint": "POST /system/terminal/run",
                    "status": "parsed",
                    "tier": tier,
                    "tier_description": describe_classification(tier),
                    "requires_approval": False,
                }

            # Needs approval → enter voice confirmation flow
            if tier in (WARN, DANGEROUS):
                _pending_run_command = {"command": cmd_text, "tier": tier}
                return {
                    "action": "run_command_confirm",
                    "command": cmd_text,
                    "tier": tier,
                    "tier_description": describe_classification(tier),
                    "status": "needs_confirmation",
                }

            # SAFE → proceed directly
            return {
                "action": "run_command",
                "command": cmd_text,
                "endpoint": "POST /system/terminal/run",
                "status": "parsed",
                "tier": tier,
                "tier_description": describe_classification(tier),
                "requires_approval": False,
            }

    if "git" in command:
        return {"action": "git_command", "command": command, "status": "parsed"}

    if "deploy" in command:
        return {"action": "deploy", "command": command, "status": "parsed"}

    tunnel_match = re.search(r"(?:expose|tunnel)\s+port\s+(\d+)", command)
    if tunnel_match:
        return {"action": "expose_port", "port": int(tunnel_match.group(1)), "endpoint": "POST /system/tunnel/expose", "status": "parsed"}

    # ─── Approvals / Whitelist ──────────────────────────────────────────

    if "clear" in command and "approval" in command:
        return {"action": "clear_approvals", "status": "triggered"}

    if "show" in command and "approval" in command:
        return {"action": "navigate", "target": "/settings"}

    # ─── Desktop Overlay ──────────────────────────────────────────

    if "overlay" in command:
        if "show" in command:
            return {"action": "overlay_show", "status": "triggered"}
        if "hide" in command:
            return {"action": "overlay_hide", "status": "triggered"}
        return {"action": "overlay_toggle", "status": "triggered"}

    # ─── Diagnostics ─────────────────────────────────────────────────

    if "diagnostics" in command or ("system" in command and "status" in command):
        return {"action": "show_diagnostics", "status": "triggered"}

    # ─── Desktop / Wallpaper Commands ───────────────────────────────

    if "wallpaper" in command or "background" in command:
        if "change" in command or "set" in command:
            desc_match = re.search(r"(?:to|as|of)\s+(.+)$", command)
            description = desc_match.group(1) if desc_match else command.replace("change wallpaper", "").replace("set wallpaper", "").replace("set background", "").strip()
            return {"action": "set_wallpaper", "description": description, "endpoint": "POST /desktop/wallpaper/set", "status": "parsed"}

    if "screenshot" in command or "capture screen" in command:
        return {"action": "screenshot", "endpoint": "POST /desktop/ocr/capture", "status": "triggered"}

    if "extract text" in command or "ocr" in command:
        return {"action": "ocr", "endpoint": "POST /desktop/ocr/capture", "status": "triggered"}

    # ─── Workflow Commands ──────────────────────────────────────────

    if "activate" in command and "mode" in command:
        mode_match = re.search(r"activate\s+(.+?)\s+mode", command)
        mode = mode_match.group(1) if mode_match else command.replace("activate", "").replace("mode", "").strip()
        return {"action": "activate_protocol", "target": f"{mode}_mode", "endpoint": "POST /desktop/protocols/activate", "status": "parsed"}

    # ─── Web & Media Commands ───────────────────────────────────────

    if "search for" in command or "google" in command:
        search_query = command.replace("search for", "").replace("google", "").strip()
        return {"action": "web_search", "query": search_query, "endpoint": "POST /web/browse/search", "status": "parsed"}

    if "open" in command and ("http" in command or "www" in command or ".com" in command or ".org" in command):
        return {"action": "open_url", "target": command.split()[-1].strip(), "endpoint": "POST /web/browse", "status": "parsed"}

    if "play" in command and "spotify" in command:
        song_match = re.search(r"play\s+(.+?)(?:\s+on\s+spotify)?$", command)
        return {"action": "spotify_play", "query": song_match.group(1).strip() if song_match else "", "status": "parsed"}

    if "pause" in command and "music" in command:
        return {"action": "spotify_pause", "status": "triggered"}

    # Weather
    weather_match = re.search(r"(?:what('s| is) the )?weather(?: in| for)?(.+)?$", command)
    if "weather" in command or weather_match:
        city = weather_match.group(2).strip() if weather_match and weather_match.group(2) else "London"
        return {"action": "get_weather", "city": city, "endpoint": "GET /web/weather", "status": "parsed"}

    # Stocks
    stock_match = re.search(r"(?:stock|price)\s+(?:of\s+)?(\w+)", command)
    if stock_match or "stock" in command:
        ticker = stock_match.group(1) if stock_match else "AAPL"
        return {"action": "get_stock", "ticker": ticker, "endpoint": "GET /web/stocks", "status": "parsed"}

    # ─── Free Public APIs (dictionary, currency, facts, etc.) ──────────

    # Dictionary / define
    define_match = re.search(r"(?:define|meaning of|definition of)\s+(.+?)(?:\s+mean)?$", command)
    if define_match:
        word = define_match.group(1).strip()
        return {"action": "lookup_definition", "word": word, "status": "parsed"}

    # "what does X mean" — separate pattern to avoid capturing "mean" as part of the word
    what_does_match = re.search(r"what does\s+(.+?)\s+mean$", command)
    if what_does_match:
        word = what_does_match.group(1).strip()
        return {"action": "lookup_definition", "word": word, "status": "parsed"}

    # Random fact
    if any(p in command for p in ["tell me a fact", "random fact", "give me a fact", "useless fact", "did you know"]):
        return {"action": "get_random_fact", "status": "parsed"}

    # Joke
    if any(p in command for p in ["tell me a joke", "tell us a joke", "joke", "chuck norris"]):
        return {"action": "get_joke", "status": "parsed"}

    # Random poem
    if any(p in command for p in ["tell me a poem", "random poem", "recite a poem", "poetry"]):
        return {"action": "get_poem", "status": "parsed"}

    # IP address
    if any(p in command for p in ["my ip", "my ip address", "what is my ip", "what's my ip"]):
        return {"action": "get_ip_info", "status": "parsed"}

    # Currency conversion
    convert_match = re.search(r"(?:convert|exchange|cambio)\s+(\d+(?:\.\d+)?)\s+(\w{3})\s+(?:to|in|into|->)\s+(\w{3})", command)
    if convert_match:
        amount = float(convert_match.group(1))
        from_cur = convert_match.group(2).upper()
        to_cur = convert_match.group(3).upper()
        return {"action": "convert_currency", "amount": amount, "from": from_cur, "to": to_cur, "status": "parsed"}

    # Currency rates lookup
    currency_match = re.search(r"(?:currency|exchange rate|rate)\s+(?:of\s+)?(\w{3})", command)
    if currency_match:
        base = currency_match.group(1).upper()
        return {"action": "get_currency_rates", "base": base, "status": "parsed"}

    # Age estimation
    age_match = re.search(r"(?:how old is|age of|guess age of|estimate age of)\s+(.+)$", command)
    if age_match:
        name = age_match.group(1).strip().split()[0]  # first name only
        return {"action": "estimate_age", "name": name, "status": "parsed"}

    # Gender estimation
    gender_match = re.search(r"(?:guess gender of|gender of|is\s+(\w+)\s+(?:male|female)|what gender is)\s+(.+)$", command)
    if gender_match:
        name = gender_match.group(2) if gender_match.lastindex and gender_match.lastindex > 1 else gender_match.group(1)
        if name:
            name = name.strip().split()[0]
            return {"action": "estimate_gender", "name": name, "status": "parsed"}

    # Nationality estimation
    nationality_match = re.search(r"(?:guess nationality of|nationality of|where is\s+(\w+)\s+from|what nationality is)\s+(.+)$", command)
    if nationality_match:
        name = nationality_match.group(2) if nationality_match.lastindex and nationality_match.lastindex > 1 else nationality_match.group(1)
        if name:
            name = name.strip().split()[0]
            return {"action": "estimate_nationality", "name": name, "status": "parsed"}

    # Bored / activity suggestion
    if any(p in command for p in ["i'm bored", "im bored", "bored", "suggest an activity", "what should i do"]):
        return {"action": "get_bored_activity", "status": "parsed"}

    # Email validation
    email_match = re.search(r"(?:validate|verify|check)\s+(?:email\s+)?([\w.+-]+@[\w-]+\.[\w.-]+)", command)
    if email_match:
        email = email_match.group(1).strip()
        return {"action": "validate_email", "email": email, "status": "parsed"}

    # ─── More Free Public APIs (steam, cocktails, nasa, trivia, etc.) ─

    # Steam deals
    if any(p in command for p in ["steam deal", "game deal", "cheap game", "deal on steam"]):
        return {"action": "get_steam_deals", "status": "parsed"}

    steam_search_match = re.search(r"(?:search|find)\s+(?:.*?)?(?:deals?|sales?)\s+(?:for|on)?\s+(.+)$", command)
    if steam_search_match and ("deal" in command or "steam" in command or "game" in command):
        title = steam_search_match.group(1).strip()
        return {"action": "search_steam_deals", "title": title, "status": "parsed"}

    # Cocktails
    cocktail_match = re.search(r"(?:how to make|recipe for|how do I make|make me a)\s+(.+)$", command)
    if cocktail_match:
        name = cocktail_match.group(1).strip()
        return {"action": "search_cocktail", "name": name, "status": "parsed"}

    if any(p in command for p in ["random cocktail", "surprise me drink", "random drink"]):
        return {"action": "random_cocktail", "status": "parsed"}

    # NASA APOD
    if any(p in command for p in ["nasa", "astronomy picture", "space photo", "apod"]):
        return {"action": "get_nasa_apod", "status": "parsed"}

    # Trivia
    if any(p in command for p in ["trivia", "quiz me", "test my knowledge", "give me a question"]):
        return {"action": "get_trivia", "status": "parsed"}

    # Animals
    if any(p in command for p in ["random dog", "show me a dog", "picture of a dog", "cute dog"]):
        return {"action": "get_random_dog", "status": "parsed"}

    if any(p in command for p in ["random cat", "show me a cat", "picture of a cat", "cute cat"]):
        return {"action": "get_random_cat", "status": "parsed"}

    # JokeAPI (variety)
    if any(p in command for p in ["tell me a random joke", "another joke", "funny joke", "make me laugh"]):
        return {"action": "get_random_joke_v2", "status": "parsed"}

    # Rick and Morty
    if any(p in command for p in ["rick and morty", "pickle rick", "wubba lubba"]):
        return {"action": "get_rick_morty", "status": "parsed"}

    # Star Wars
    if any(p in command for p in ["star wars", "may the force", "random star wars"]):
        return {"action": "get_star_wars", "status": "parsed"}

    # Number facts
    number_fact_match = re.search(r"(?:tell me about|fact about|number fact|about number)\s+(\d+)", command)
    if number_fact_match:
        num = int(number_fact_match.group(1))
        return {"action": "get_number_fact", "number": num, "status": "parsed"}

    if any(p in command for p in ["random number fact", "number trivia", "math fact"]):
        return {"action": "get_random_number_fact", "status": "parsed"}

    # Maps
    if "map of" in command or "show map" in command:
        place_match = re.search(r"(?:map of|show map)\s+(.+)$", command)
        place = place_match.group(1) if place_match else "Tokyo"
        return {"action": "show_map", "place": place, "endpoint": "GET /web/maps/place", "status": "parsed"}

    if "directions" in command and ("to" in command or "from" in command):
        dir_match = re.search(r"directions\s+(?:from\s+(.+?)\s+)?to\s+(.+)$", command)
        if dir_match:
            origin = dir_match.group(1) if dir_match.group(1) else "current location"
            destination = dir_match.group(2)
            return {"action": "get_directions", "origin": origin, "destination": destination, "status": "parsed"}

    # Image generation
    if "generate image" in command or "create image" in command or "make a picture" in command:
        prompt = command.replace("generate image of", "").replace("generate image", "").replace("create image of", "").replace("create image", "").replace("make a picture of", "").strip()
        return {"action": "generate_image", "prompt": prompt or "abstract art", "endpoint": "POST /web/images/generate", "status": "parsed"}

    # ─── Document Commands ──────────────────────────────────────────

    if "create presentation" in command or "create ppt" in command:
        topic = command.replace("create presentation about", "").replace("create presentation on", "").replace("create presentation", "").strip()
        return {"action": "create_ppt", "topic": topic or "Untitled", "endpoint": "POST /documents/powerpoint", "status": "parsed"}

    if "create spreadsheet" in command or "create excel" in command:
        desc = command.replace("create spreadsheet", "").replace("create excel", "").strip()
        return {"action": "create_excel", "description": desc or "New Spreadsheet", "endpoint": "POST /documents/excel", "status": "parsed"}

    if "export to pdf" in command or "generate pdf" in command:
        return {"action": "create_pdf", "status": "triggered", "endpoint": "POST /documents/pdf"}

    # ─── Job Pipeline Commands ───────────────────────────────────────

    # Applications list (most specific, check before generic scan)
    if ("application" in command and ("show" in command or "status" in command or "my" in command)):
        return {"action": "list_applications", "endpoint": "GET /api/v1/applications", "status": "triggered"}

    # Upload/update resume (specific action, check before generic resume and optimize)
    if ("upload" in command or "update" in command) and "resume" in command:
        return {"action": "upload_resume", "endpoint": "POST /api/v1/resume/upload", "status": "needs_file"}

    # Optimize resume (must come before generic resume check since "optimize resume" contains "resume")
    opt_match = re.search(r"optimize(?:\s+resume)?(?:\s+for)?(?:\s+job)?\s+(\d+)", command)
    if opt_match:
        job_id = opt_match.group(1)
        return {"action": "optimize_resume", "endpoint": f"POST /api/v1/jobs/{job_id}/optimize", "payload": {"job_id": job_id}, "status": "parsed"}

    # Cover letter with job ID (check before generic resume since "cover letter" could be near "resume")
    cl_match = re.search(r"(?:cover letter|coverletter)(?:\s+for)?(?:\s+job)?\s+(\d+)", command)
    if cl_match:
        job_id = cl_match.group(1)
        return {"action": "cover_letter", "endpoint": f"POST /api/v1/jobs/{job_id}/cover-letter", "payload": {"job_id": job_id}, "status": "parsed"}
    if "cover letter" in command or "write a letter" in command:
        return {"action": "cover_letter", "endpoint": "POST /api/v1/jobs/{id}/cover-letter", "status": "needs_job_id"}

    # Cold mail with job ID (check before generic resume for similar reasons)
    ce_match = re.search(r"(?:cold mail|cold email|email)(?:\s+for)?(?:\s+job)?\s+(\d+)", command)
    if ce_match:
        job_id = ce_match.group(1)
        return {"action": "cold_mail", "endpoint": f"POST /api/v1/jobs/{job_id}/cold-mail", "payload": {"job_id": job_id}, "status": "parsed"}
    if "cold mail" in command or "cold email" in command or "send email" in command or "email about" in command:
        return {"action": "cold_mail", "endpoint": "POST /api/v1/jobs/{id}/cold-mail", "status": "needs_job_id"}

    # Apply with job ID (check before generic resume just in case)
    apply_match = re.search(r"apply(?:\s+to|\s+for)?(?:\s+job)?\s+(\d+)", command)
    if apply_match:
        job_id = apply_match.group(1)
        return {"action": "apply", "endpoint": f"POST /api/v1/jobs/{job_id}/apply", "payload": {"job_id": job_id}, "status": "parsed"}
    if "apply" in command or "submit application" in command:
        return {"action": "apply", "endpoint": "POST /api/v1/jobs/{id}/apply", "status": "needs_job_id"}

    # Generic resume (fallback — any other "resume" command)
    if "resume" in command:
        return {"action": "get_resume", "endpoint": "GET /api/v1/resume", "status": "triggered"}

    # Scan jobs with optional keyword filter
    scan_match = re.search(r"(?:scan|find)\s+(?:for\s+)?(.+?)\s+(?:jobs?|positions?|openings?)(?:\s|$)", command)
    if scan_match:
        keywords = scan_match.group(1).strip()
        return {
            "action": "scan_jobs",
            "endpoint": "POST /api/v1/jobs/search",
            "payload": {"keywords": keywords},
            "status": "parsed",
        }

    if (command.startswith("scan") or command.startswith("find")) and "job" in command:
        scan_fallback = re.search(r"(?:scan|find)\s+(?:for\s+)?(.+?)\s+(?:jobs?|positions?|openings?)", command)
        keywords = scan_fallback.group(1).strip() if scan_fallback else ""
        return {
            "action": "scan_jobs",
            "endpoint": "POST /api/v1/jobs/search",
            "payload": {"keywords": keywords if keywords else "all"},
            "status": "parsed",
        }

    if "scan jobs" in command or "find jobs" in command:
        return {"action": "scan_jobs", "endpoint": "POST /api/v1/jobs/search", "status": "triggered"}

    if ("how many" in command and "jobs" in command) or "job stats" in command or "application stats" in command:
        return {"action": "get_stats", "endpoint": "GET /api/v1/dashboard/stats", "status": "triggered"}

    if ("match" in command and "jobs" in command) or "score jobs" in command or "evaluate jobs" in command:
        return {"action": "batch_match", "endpoint": "POST /api/v1/jobs/batch-match", "status": "triggered"}

    # ─── Content Pipeline Commands ───────────────────────────────────

    if "check trends" in command or "trending" in command:
        return {"action": "check_trends", "status": "triggered"}
    if "generate script" in command or "write script" in command:
        return {"action": "generate_script", "endpoint": "POST /social/generate-script", "status": "needs_topic"}
    if "render video" in command or "create video" in command:
        return {"action": "render_video", "endpoint": "POST /social/render-video", "status": "needs_script_id"}
    if "post content" in command or "publish" in command:
        return {"action": "post_content", "endpoint": "POST /social/post", "status": "needs_video_id"}

    # ─── Navigation Commands ─────────────────────────────────────────

    if "dashboard" in command or "home" in command:
        return {"action": "navigate", "target": "/dashboard"}
    if "analytics" in command or "stats" in command:
        return {"action": "navigate", "target": "/analytics"}
    if "content" in command or "studio" in command:
        return {"action": "navigate", "target": "/content"}
    if "jobs" in command or "career" in command:
        return {"action": "navigate", "target": "/jobs"}
    if "files" in command or "documents" in command:
        return {"action": "navigate", "target": "/files"}
    if "settings" in command or "preferences" in command:
        return {"action": "navigate", "target": "/settings"}
    if "notifications" in command or "alerts" in command:
        return {"action": "navigate", "target": "/settings"}
    if "help" in command or "commands" in command:
        return {"action": "help", "status": "triggered"}

    # ─── Agent Task (complex multi-step goals) ───────────────────────
    # When the user asks for multi-step work like "research X and save to file",
    # "find flights to Y", "plan a trip", etc., we route to the agent system.

    # Check for complex task patterns
    agent_patterns = [
        r"research\s+.+(?:and|then)\s+.+",      # "research X and save to file"
        r"find\s+.+(?:and|then)\s+.+",           # "find information about X and summarize"
        r"plan\s+.+(?:trip|vacation|itinerary)",  # "plan a trip to Paris"
        r"analyze\s+.+(?:and|then)\s+.+",         # "analyze this data and create a report"
        r"compare\s+.+(?:and|then)\s+.+",         # "compare these products and save"
        r"create\s+a\s+(?:report|summary|analysis)\s+of",  # "create a summary of..."
        r"could you (?:research|find|analyze|look up|investigate)",  # "could you research X..."
        r"I need you to",  # "I need you to find X and save it" — multi-step
    ]

    for pattern in agent_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return {
                "action": "agent_task",
                "goal": command,
                "status": "triggered",
            }

    # ─── Fallback ────────────────────────────────────────────────────
    return {"action": "unknown", "command": command}


# ── Command execution helper (used by conversation_listener) ────────────

async def _execute_command_action(text: str, parsed: dict) -> str:
    """Execute a parsed voice command and return a spoken confirmation.

    Routes to the appropriate system action based on ``parsed["action"]``.
    Returns a short confirmation string that will be spoken back.
    """
    import platform as _platform
    import subprocess

    action = parsed.get("action", "")
    target = parsed.get("target", "")
    command_text = parsed.get("command", "")

    try:
        if action == "launch_app":
            # Use the existing system_control launch_app logic
            from system_control.routes import AppAction
            from system_control.routes import launch_app as _launch_app
            await _launch_app(AppAction(app_name=target))
            return f"Opening {target}."

        elif action == "close_app":
            system = _platform.system().lower()
            if system == "windows":
                subprocess.run(["taskkill", "/f", "/im", f"{target}.exe"], capture_output=True, timeout=10)
            else:
                subprocess.run(["pkill", "-f", target], capture_output=True, timeout=10)
            return f"Closing {target}."

        elif action == "run_command":
            proc = subprocess.run(command_text, shell=True, capture_output=True, text=True, timeout=30)
            output = (proc.stdout or "").strip()[:200]
            if proc.returncode == 0:
                return f"Command executed. {output}" if output else "Command completed."
            return "Command returned error."

        elif action == "web_search":
            query = parsed.get("query", command_text)
            import urllib.parse
            import webbrowser
            webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote(query)}")
            return f"Searching for {query}."

        elif action == "open_url":
            url = parsed.get("target", "")
            if not url.startswith("http"):
                url = f"https://{url}"
            import webbrowser
            webbrowser.open(url)
            return f"Opening {url}."

        elif action == "show_desktop":
            system = _platform.system().lower()
            if system == "windows":
                import ctypes
                ctypes.windll.user32.keybd_event(0x5B, 0, 0, 0)  # type: ignore[attr-defined]  # Win key down
                ctypes.windll.user32.keybd_event(0x44, 0, 0, 0)  # type: ignore[attr-defined]  # D down
                ctypes.windll.user32.keybd_event(0x44, 0, 2, 0)  # type: ignore[attr-defined]  # D up
                ctypes.windll.user32.keybd_event(0x5B, 0, 2, 0)  # type: ignore[attr-defined]  # Win key up
            else:
                import subprocess
                subprocess.run(["osascript", "-e", 'tell app "Finder" to activate'], capture_output=True, timeout=5)
            return "Showing desktop."

        elif action == "navigate":
            return "Navigating."

        elif action in ("screenshot", "ocr", "check_trends", "get_weather", "get_stock"):
            return f"Processing {action.replace('_', ' ')}."

        # ─── Free Public API Executions ────────────────────────────────

        elif action == "lookup_definition":
            word = parsed.get("word", "")
            if not word:
                return "Please specify a word to define."
            try:
                from external_apis.clients import fetch_word_definition
                result = await fetch_word_definition(word)
                if result.get("status") == "ok":
                    defs = result.get("definitions", [])
                    if defs:
                        d = defs[0]
                        return f"{word}. {d.get('part_of_speech', '')}. {d.get('definition', '')}"
                    return f"No definitions found for {word}."
                return f"Sorry, couldn't find {word}: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Dictionary lookup failed: {e}"

        elif action == "get_random_fact":
            try:
                from external_apis.clients import fetch_random_fact
                result = await fetch_random_fact()
                if result.get("status") == "ok":
                    return f"Did you know? {result.get('fact', '')}"
                return "Sorry, couldn't fetch a fact."
            except Exception as e:
                return f"Fact fetch failed: {e}"

        elif action == "get_joke":
            try:
                from external_apis.clients import fetch_chuck_norris_joke
                result = await fetch_chuck_norris_joke()
                if result.get("status") == "ok":
                    return result.get("joke", "")
                return "Sorry, couldn't fetch a joke."
            except Exception as e:
                return f"Joke fetch failed: {e}"

        elif action == "get_poem":
            try:
                from external_apis.clients import fetch_random_poem
                result = await fetch_random_poem()
                if result.get("status") == "ok":
                    title = result.get("title", "")
                    author = result.get("author", "")
                    lines = result.get("lines", [])
                    if lines:
                        excerpt = " ".join(lines[:4])
                        if len(excerpt) > 400:
                            excerpt = excerpt[:397] + "..."
                        return f"{title} by {author}. {excerpt}"
                    return f"{title} by {author}."
                return "Sorry, couldn't fetch a poem."
            except Exception as e:
                return f"Poem fetch failed: {e}"

        elif action == "get_ip_info":
            try:
                from external_apis.clients import fetch_ip_info
                result = await fetch_ip_info()
                if result.get("status") == "ok":
                    ip = result.get("ip", "unknown")
                    city = result.get("city", "")
                    country = result.get("country", "")
                    isp = result.get("isp", "")
                    parts = [f"Your IP address is {ip}."]
                    if city and country:
                        parts.append(f"You appear to be in {city}, {country}.")
                    if isp:
                        parts.append(f"Your internet provider is {isp}.")
                    return " ".join(parts)
                return "Sorry, couldn't determine your IP address."
            except Exception as e:
                return f"IP lookup failed: {e}"

        elif action == "convert_currency":
            amount = parsed.get("amount", 1)
            from_cur = parsed.get("from", "USD")
            to_cur = parsed.get("to", "EUR")
            try:
                from external_apis.clients import convert_currency
                result = await convert_currency(amount, from_cur, to_cur)
                if result.get("status") == "ok":
                    converted = result.get("converted", 0)
                    rate = result.get("rate", 0)
                    return f"{amount} {from_cur} is {converted} {to_cur}. The exchange rate is {rate:.4f}."
                return f"Currency conversion failed: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Currency conversion failed: {e}"

        elif action == "get_currency_rates":
            base = parsed.get("base", "USD")
            try:
                from external_apis.clients import fetch_currency_rates
                result = await fetch_currency_rates(base)
                if result.get("status") == "ok":
                    rates = result.get("rates", {})
                    majors = ["EUR", "GBP", "JPY", "INR", "CAD", "AUD", "CHF"]
                    available = [(c, rates[c]) for c in majors if c in rates]
                    if available:
                        parts = [f"Exchange rates for {base}:"]
                        for code, rate in available[:5]:
                            parts.append(f"{code}: {rate:.4f}")
                        return ". ".join(parts)
                    return f"No rates available for {base}."
                return f"Failed to get rates: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Currency lookup failed: {e}"

        elif action == "estimate_age":
            name = parsed.get("name", "")
            if not name:
                return "Please specify a name."
            try:
                from external_apis.clients import estimate_age
                result = await estimate_age(name)
                if result.get("status") == "ok":
                    age = result.get("age")
                    if age is not None:
                        return f"Based on data, the estimated age for {name} is {age} years old."
                    return f"Not enough data to estimate age for {name}."
                return f"Age estimation failed: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Age lookup failed: {e}"

        elif action == "estimate_gender":
            name = parsed.get("name", "")
            if not name:
                return "Please specify a name."
            try:
                from external_apis.clients import estimate_gender
                result = await estimate_gender(name)
                if result.get("status") == "ok":
                    gender = result.get("gender")
                    prob = result.get("probability", 0)
                    if gender:
                        pct = round(prob * 100)
                        return f"{name} is likely {gender} with {pct} percent probability."
                    return f"Not enough data to estimate gender for {name}."
                return f"Gender estimation failed: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Gender lookup failed: {e}"

        elif action == "estimate_nationality":
            name = parsed.get("name", "")
            if not name:
                return "Please specify a name."
            try:
                from external_apis.clients import estimate_nationality
                result = await estimate_nationality(name)
                if result.get("status") == "ok":
                    countries = result.get("countries", [])
                    if countries:
                        top = countries[0]
                        return f"{name} is most likely from {top.get('country_id', 'unknown')} with {top.get('probability', 0)} percent probability."
                    return f"Not enough data to estimate nationality for {name}."
                return f"Nationality estimation failed: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Nationality lookup failed: {e}"

        elif action == "get_bored_activity":
            try:
                from external_apis.clients import fetch_bored_activity
                result = await fetch_bored_activity()
                if result.get("status") == "ok":
                    activity = result.get("activity", "")
                    atype = result.get("type", "")
                    participants = result.get("participants", 1)
                    return f"How about this: {activity}. It's a {atype} activity and needs {participants} participant{'s' if participants > 1 else ''}."
                return "Sorry, couldn't find an activity."
            except Exception as e:
                return f"Activity suggestion failed: {e}"

        elif action == "validate_email":
            email = parsed.get("email", "")
            if not email:
                return "Please specify an email address."
            try:
                from external_apis.clients import validate_email
                result = await validate_email(email)
                if result.get("status") == "ok":
                    valid = result.get("valid", False)
                    if valid:
                        return f"The email {email} appears to be valid."
                    return f"The email {email} appears to be invalid."
                return f"Email validation failed: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Email validation failed: {e}"

        # --- New Free Public API Executions -----------------------------------

        elif action == "get_steam_deals":
            try:
                from external_apis.clients import fetch_steam_deals
                result = await fetch_steam_deals()
                if result.get("status") == "ok":
                    deals = result.get("deals", [])
                    if deals:
                        top = deals[:3]
                        parts = []
                        for d in top:
                            title = d.get("title", "Unknown")
                            price = d.get("salePrice", "?")
                            savings = d.get("savings", "0")
                            parts.append(f"{title} at ${price}, saving {float(savings):.0f} percent")
                        return "Here are the latest Steam deals. " + ". ".join(parts)
                    return "No Steam deals found right now."
                return "Steam deals lookup failed."
            except Exception as e:
                return f"Steam deals failed: {e}"

        elif action == "search_steam_deals":
            title = parsed.get("title", "")
            if not title:
                return "Please specify a game title to search for."
            try:
                from external_apis.clients import search_steam_deals
                result = await search_steam_deals(title)
                if result.get("status") == "ok":
                    deals = result.get("deals", [])
                    if deals:
                        d = deals[0]
                        store = d.get("store", {})
                        store_name = store.get("name", "Unknown store") if isinstance(store, dict) else "Unknown"
                        price = d.get("salePrice", "?")
                        norm = d.get("normalPrice", "?")
                        return f"{title} is on sale at {store_name} for ${price}, normally ${norm}."
                    return f"No deals found for {title}."
                return f"Search failed for {title}."
            except Exception as e:
                return f"Steam search failed: {e}"

        elif action == "search_cocktail":
            name = parsed.get("name", "")
            if not name:
                return "Please specify a cocktail name."
            try:
                from external_apis.clients import search_cocktail
                result = await search_cocktail(name)
                if result.get("status") == "ok":
                    drinks = result.get("drinks", [])
                    if drinks:
                        d = drinks[0]
                        desc = d.get("strDrink", name)
                        glass = d.get("strGlass", "a glass")
                        ingredients = []
                        for i in range(1, 16):
                            ing = d.get(f"strIngredient{i}")
                            if ing:
                                ingredients.append(ing)
                        return f"{desc} is served in {glass}. Ingredients: {', '.join(ingredients)}."
                    return f"No cocktail found named {name}."
                return "Cocktail search failed."
            except Exception as e:
                return f"Cocktail search failed: {e}"

        elif action == "random_cocktail":
            try:
                from external_apis.clients import random_cocktail
                result = await random_cocktail()
                if result.get("status") == "ok":
                    drinks = result.get("drinks", [])
                    if drinks:
                        d = drinks[0]
                        name = d.get("strDrink", "Unknown")
                        glass = d.get("strGlass", "a glass")
                        ingredients = []
                        for i in range(1, 16):
                            ing = d.get(f"strIngredient{i}")
                            if ing:
                                ingredients.append(ing)
                        return f"How about a {name} served in {glass}. Ingredients: {', '.join(ingredients)}."
                    return "No cocktail returned."
                return "Random cocktail failed."
            except Exception as e:
                return f"Random cocktail failed: {e}"

        elif action == "get_nasa_apod":
            try:
                from external_apis.clients import fetch_nasa_apod
                result = await fetch_nasa_apod()
                if result.get("status") == "ok":
                    title = result.get("title", "Astronomy Picture")
                    date = result.get("date", "")
                    explanation = result.get("explanation", "")[:200]
                    return f"Astronomy picture of the day from {date}: {title}. {explanation}"
                return "NASA APOD lookup failed."
            except Exception as e:
                return f"NASA APOD failed: {e}"

        elif action == "get_trivia":
            try:
                from external_apis.clients import fetch_trivia_questions
                result = await fetch_trivia_questions(1)
                if result.get("status") == "ok":
                    questions = result.get("questions", [])
                    if questions:
                        q = questions[0]
                        category = q.get("category", "General")
                        difficulty = q.get("difficulty", "medium")
                        question_text = q.get("question", "")
                        correct = q.get("correct_answer", "")
                        return f"Here is a {difficulty} difficulty {category} trivia question: {question_text}. The answer is: {correct}."
                    return "No trivia questions available."
                return "Trivia lookup failed."
            except Exception as e:
                return f"Trivia failed: {e}"

        elif action == "get_random_dog":
            try:
                from external_apis.clients import fetch_random_dog
                result = await fetch_random_dog()
                if result.get("status") == "ok":
                    return "Here is a random dog picture! Check the screen to see it."
                return "Dog picture failed."
            except Exception as e:
                return f"Dog picture failed: {e}"

        elif action == "get_random_cat":
            try:
                from external_apis.clients import fetch_random_cat
                result = await fetch_random_cat()
                if result.get("status") == "ok":
                    return "Here is a random cat picture! Check the screen to see it."
                return "Cat picture failed."
            except Exception as e:
                return f"Cat picture failed: {e}"

        elif action == "get_joke_api":
            try:
                from external_apis.clients import fetch_random_joke_api
                result = await fetch_random_joke_api()
                if result.get("status") == "ok":
                    joke = result.get("joke", "")
                    if joke:
                        return joke
                    return "Couldn't fetch a joke."
                return "Joke API failed."
            except Exception as e:
                return f"Joke API failed: {e}"

        elif action == "search_rick_morty":
            name = parsed.get("name", "")
            if not name:
                return "Please specify a Rick and Morty character name."
            try:
                from external_apis.clients import fetch_rick_morty_character
                result = await fetch_rick_morty_character(name)
                if result.get("status") == "ok":
                    chars = result.get("characters", [])
                    if chars:
                        c = chars[0]
                        char_name = c.get("name", name)
                        species = c.get("species", "Unknown")
                        status = c.get("status", "Unknown")
                        return f"{char_name} is a {species} who is currently {status}."
                    return f"No Rick and Morty character found named {name}."
                return "Character search failed."
            except Exception as e:
                return f"Rick and Morty search failed: {e}"

        elif action == "random_rick_morty":
            try:
                from external_apis.clients import random_rick_morty_character
                result = await random_rick_morty_character()
                if result.get("status") == "ok":
                    c = result.get("character", {})
                    if c:
                        char_name = c.get("name", "Unknown")
                        species = c.get("species", "Unknown")
                        status = c.get("status", "Unknown")
                        return f"Random character: {char_name}, a {species} who is {status}."
                    return "No character returned."
                return "Random character failed."
            except Exception as e:
                return f"Random Rick and Morty failed: {e}"

        elif action == "search_star_wars":
            name = parsed.get("name", "")
            if not name:
                return "Please specify a Star Wars character name."
            try:
                from external_apis.clients import fetch_star_wars_character
                result = await fetch_star_wars_character(name)
                if result.get("status") == "ok":
                    chars = result.get("characters", [])
                    if chars:
                        c = chars[0]
                        char_name = c.get("name", name)
                        height = c.get("height", "unknown")
                        mass = c.get("mass", "unknown")
                        return f"{char_name} is {height} centimeters tall and weighs {mass} kilograms."
                    return f"No Star Wars character found named {name}."
                return "Character search failed."
            except Exception as e:
                return f"Star Wars search failed: {e}"

        elif action == "random_star_wars":
            try:
                from external_apis.clients import random_star_wars_character
                result = await random_star_wars_character()
                if result.get("status") == "ok":
                    c = result.get("character", {})
                    if c:
                        char_name = c.get("name", "Unknown")
                        height = c.get("height", "unknown")
                        mass = c.get("mass", "unknown")
                        return f"Random character: {char_name}, {height} centimeters tall, {mass} kilograms."
                    return "No character returned."
                return "Random character failed."
            except Exception as e:
                return f"Random Star Wars failed: {e}"

        elif action == "get_number_fact":
            number = parsed.get("number", "")
            if not number:
                return "Please specify a number."
            try:
                from external_apis.clients import fetch_number_fact
                result = await fetch_number_fact(number)
                if result.get("status") == "ok":
                    return result.get("fact", f"No fact found for {number}.")
                return "Number fact failed."
            except Exception as e:
                return f"Number fact failed: {e}"

        elif action == "random_number_fact":
            try:
                from external_apis.clients import fetch_random_number_fact
                result = await fetch_random_number_fact()
                if result.get("status") == "ok":
                    return result.get("fact", "No random fact found.")
                return "Random number fact failed."
            except Exception as e:
                return f"Random number fact failed: {e}"

        elif action == "voice_stop":
            return "Stopping voice."

        elif action == "voice_start":
            return "Starting voice."

        else:
            return f"{action.replace('_', ' ')}." if action else "Done."

    except Exception as e:
        print(f"[Voice] Command execution error ({action}): {e}")
        return "Sorry, I couldn't do that."


# Wire up command callbacks for the conversation listener
conversation_listener._parse_command = _parse_and_route
conversation_listener._execute_command = _execute_command_action


@router.post("/transcribe")
async def transcribe_audio():
    """Transcribe recent microphone input."""
    try:
        text = await speech_processor.transcribe_microphone(duration=5.0)
        await analytics_dao.log_activity("voice", "transcribe", f"Transcribed: {text[:50]}...")
        return {"text": text}
    except Exception as e:
        await analytics_dao.log_activity("voice", "transcribe_error", str(e), severity="error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/action-log/recent")
async def action_log_recent(limit: int = Query(default=10, ge=1, le=100)):
    """Get the most recent AI-executed actions for the floating action log."""
    actions = await get_recent_actions(limit=limit)
    return {"actions": actions}


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Stream a chat response token-by-token via Server-Sent Events.
    Each token is sent as an SSE event for immediate display.
    After all tokens are streamed, an 'audio' event with base64-encoded
    TTS audio is sent before the [DONE] signal — eliminating the need
    for a separate blocking audio fetch on the frontend.
    """
    from fastapi.responses import StreamingResponse

    async def event_generator():
        import base64 as _base64
        import json as _json

        try:
            # Stream from Ollama's native streaming API token-by-token
            from utils.ollama_client import OllamaClient

            cm = responder.conversation
            cm.add_user_message(request.message)
            context = cm.get_context()

            llm = OllamaClient()
            full_text = ""

            # ── 1. Stream text tokens ───────────────────────────────
            async for token in llm.stream_chat(context):
                full_text += token
                yield f"data: {_json.dumps({'type': 'token', 'text': token})}\n\n"

            cm.add_assistant_message(full_text)

            # Log the action
            action_text = full_text[:80]
            await log_action(
                "chat_response",
                f"AI: {action_text}..." if len(full_text) > 80 else f"AI: {full_text}",
                severity=INFO,
                metadata={"message": request.message[:100], "response_length": len(full_text)},
            )

            # ── 2. Generate & stream TTS audio for the full response ─
            if full_text.strip():
                try:
                    audio_bytes = await speech_processor.synthesize(
                        full_text, voice=_tts_voice
                    )
                    audio_b64 = _base64.b64encode(audio_bytes).decode("utf-8")
                    yield f"data: {_json.dumps({'type': 'audio', 'audio_base64': audio_b64})}\n\n"
                except Exception as e:
                    print(f"[ChatStream] TTS synthesis error: {e}")

            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/speak")
async def speak_text(request: CommandRequest):
    """Synthesize text to speech using the configured TTS voice."""
    try:
        audio_bytes = await speech_processor.synthesize(request.command, voice=_tts_voice)
        return {"status": "synthesized", "size_bytes": len(audio_bytes), "voice": _tts_voice}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/devices")
async def list_audio_devices():
    """List all available audio input/output devices.
    Used by the frontend to let users choose which mic/speaker to use.
    """
    from .audio_device import get_device_list, reset_cache
    reset_cache()  # Refresh device list in case hardware changed
    devices = get_device_list()
    # Also show which device is currently selected
    cfg = get_settings()
    input_device = None
    output_device = None
    import sounddevice as sd
    try:
        default_input = sd.query_devices(kind="input")
        default_output = sd.query_devices(kind="output")
        input_device = default_input["name"] if default_input else None
        output_device = default_output["name"] if default_output else None
    except Exception:
        pass
    return {
        "devices": devices,
        "config": {
            "input_setting": cfg.audio_input_device,
            "output_setting": cfg.audio_output_device,
        },
        "defaults": {
            "input_device": input_device,
            "output_device": output_device,
        },
    }


@router.get("/mic-level")
async def get_mic_level():
    """Get the current microphone audio level (0.0–1.0) from the wake word detector.
    Used by the frontend for real-time mic activity visualization."""
    detector = get_wake_word_detector()
    level = detector.get_mic_level()
    return {"level": round(level, 4), "is_listening": detector._running}


@router.post("/wake-word")
async def set_wake_word(request: WakeWordRequest):
    """Change the wake word dynamically. Rebuilds detection patterns immediately."""
    if not request.wake_word or len(request.wake_word.strip()) < 2:
        raise HTTPException(status_code=400, detail="Wake word must be at least 2 characters")

    new_word = request.wake_word.strip().lower()
    detector = get_wake_word_detector()
    detector.set_wake_word(new_word)

    # Persist to config for future sessions
    await settings_dao.set_setting("wake_word", new_word, "voice")
    await analytics_dao.log_activity("voice", "wake_word", f"Wake word changed to: {new_word}")

    return {"status": "updated", "wake_word": new_word}


@router.get("/vad-settings")
async def get_vad_settings():
    """Get current VAD (Voice Activity Detection) settings."""
    return {
        "silence_timeout": round(_vad_silence_timeout, 2),
        "min": 0.1,
        "max": 3.0,
        "step": 0.05,
    }


@router.post("/vad-settings")
async def set_vad_settings(request: VADSettingsRequest):
    """Update VAD silence timeout and persist to database."""
    global _vad_silence_timeout

    timeout = max(0.1, min(3.0, request.silence_timeout))
    _vad_silence_timeout = timeout
    # Propagate to the conversation listener immediately
    conversation_listener.vad_silence_timeout = timeout

    await settings_dao.set_setting(
        "vad_silence_timeout", str(round(timeout, 2)), "voice"
    )
    await analytics_dao.log_activity(
        "voice", "vad_settings",
        f"VAD silence timeout set to {timeout}s"
    )
    print(f"[Voice] VAD silence timeout updated to {timeout}s")

    return {
        "status": "set",
        "silence_timeout": round(timeout, 2),
    }


@router.post("/weather-city")
async def set_weather_city(request: WeatherCityRequest):
    """Set the city for weather in the wake word greeting."""
    city = request.city.strip()
    if not city or len(city) < 2:
        raise HTTPException(status_code=400, detail="City name must be at least 2 characters")

    await settings_dao.set_setting("weather_city", city, "voice")
    await analytics_dao.log_activity("voice", "weather_city", f"Weather city set to: {city}")
    print(f"[Voice] Weather city set to: {city}")

    return {"status": "set", "weather_city": city}


@router.post("/wake-greeting-mode")
async def set_wake_greeting_mode(request: ConversationModeRequest):
    """Enable or disable the wake word greeting.
    When enabled, saying the wake word will speak system/job/weather/stocks/news
    info before entering conversation mode. When disabled, the wake word
    directly enters conversation mode (or just focuses the window).
    This is independent of hands-free conversation mode.
    """
    global _wake_greeting_enabled
    _wake_greeting_enabled = request.enabled
    print(f"[Voice] Wake greeting {'enabled' if request.enabled else 'disabled'}")

    await settings_dao.set_setting("wake_greeting_enabled", str(request.enabled), "voice")
    await analytics_dao.log_activity("voice", "wake_greeting_mode", f"Wake greeting: {request.enabled}")

    return {"status": "set", "wake_greeting_enabled": request.enabled}


@router.post("/conversation-mode")
async def set_conversation_mode(request: ConversationModeRequest):
    """Enable or disable the hands-free Alexa/Gemini-style conversation mode.
    When enabled, saying the wake word will automatically start a continuous
    conversation loop (listen → transcribe → respond → speak → loop).
    When disabled, the wake word only triggers the window focus (default).
    """
    global _hands_free_mode
    _hands_free_mode = request.enabled
    print(f"[Voice] Hands-free conversation mode {'enabled' if request.enabled else 'disabled'}")

    await settings_dao.set_setting("hands_free_mode", str(request.enabled), "voice")
    await analytics_dao.log_activity("voice", "conversation_mode", f"Hands-free mode: {request.enabled}")

    return {"status": "set", "hands_free_enabled": request.enabled}


@router.websocket("/ws/status")
async def voice_status_ws(websocket: WebSocket):
    """WebSocket endpoint for real-time voice status updates.
    Pushes full status + mic level every 100ms — no polling needed.

    Events:
        ``voice_status``: Periodic status snapshot (every 100ms) with
                         ``stt_text``, ``response_text``, and current state.
        ``ai_response_token``: Fired as AI generates response tokens
                              (real-time). Contains ``text`` (new tokens),
                              ``full_text`` (accumulated), and ``is_final``.    """
    await websocket.accept()

    # Register this client for instant state broadcasts
    await _voice_ws.register(websocket)

    # Track last-known values to fire typed events only on changes
    _last_stt = ""
    _last_response_len = 0

    try:
        while True:
            detector = get_wake_word_detector()
            is_running = detector._running if detector else False

            # During conversations, read from the speech processor which keeps
            # _current_mic_level updated via the background mic monitor (between
            # STT turns) or transcribe_streaming() (during active STT).
            # When not in a conversation, fall back to the wake word detector's
            # own mic level reading.
            if responder.conversation.is_active:
                mic_level = speech_processor.get_mic_level()
            else:
                mic_level = detector.get_mic_level() if detector else 0.0

            current_stt = getattr(responder, "stt_text", "")
            current_response = getattr(responder, "response_text", "")

            # Send combined status snapshot (100ms polling)
            await websocket.send_json({
                "type": "voice_status",
                "is_listening": is_running,
                "conversation_active": responder.conversation.is_active,
                "is_speaking": responder.is_speaking,
                "is_processing": responder.is_processing,
                "conversation_turns": responder.conversation.turn_count,
                "mic_level": round(mic_level, 4),
                "stt_text": current_stt,
                "stt_confidence": getattr(responder, "stt_confidence", 0.0),
                "response_text": current_response,
                "language": _language,
                "tts_voice": _tts_voice,
                "last_detected_language": _last_detected_language,
                "last_detected_at": _last_detected_at,
            })

            # ── Fire typed events for the live-captions frontend ─────
            # NOTE: The new typed events (state_change, caption_user, caption_barq)
            # are broadcast instantly via VoiceWSManager.broadcast() / broadcast_state()
            # from the voice pipeline modules.  This poll loop only emits the legacy
            # events (user-speech-transcript, ai_response_token) for backward
            # compatibility with older frontend versions.

            # Fire user-speech-transcript when a final utterance is captured
            # (STT transitions from non-empty back to empty = utterance done)
            if _last_stt and not current_stt:
                await websocket.send_json({
                    "type": "user-speech-transcript",
                    "text": _last_stt,
                    "is_final": True,
                })
            _last_stt = current_stt if current_stt else _last_stt
            if not current_stt:
                _last_stt = ""

            # Fire ai-response-token events when response_text grows
            if current_response and len(current_response) > _last_response_len:
                new_tokens = current_response[_last_response_len:]
                await websocket.send_json({
                    "type": "ai_response_token",
                    "text": new_tokens,
                    "full_text": current_response,
                    "is_final": not responder.is_speaking and not responder.is_processing,
                })
                _last_response_len = len(current_response)

            # Reset tracking when response is cleared (turn ends)
            if not current_response and _last_response_len > 0:
                await websocket.send_json({
                    "type": "ai_response_token",
                    "text": "",
                    "full_text": "",
                    "is_final": True,
                })
                _last_response_len = 0
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    finally:
        await _voice_ws.unregister(websocket)


@router.get("/status")
async def voice_status():
    """Get the current status of the voice system."""
    cfg = get_settings()

    is_running = wake_word_detector is not None and wake_word_detector._running
    recent_commands = await settings_dao.get_recent_commands(limit=5)

    # Get last Vosk recognition confidence from the running detector
    last_confidence = 0.0
    if wake_word_detector is not None:
        last_confidence = wake_word_detector.get_last_confidence()

    # Get weather city setting
    weather_city = await settings_dao.get_setting("weather_city")
    language = _language
    if wake_word_detector is not None:
        language = wake_word_detector.language

    # Read live STT and response text for the frontend captions
    current_stt = getattr(responder, "stt_text", "")
    current_response = getattr(responder, "response_text", "")

    return {
        "is_listening": is_running,
        "wake_word": cfg.wake_word,
        "language": language,
        "stt_model": "whisper",
        "tts_model": f"{'piper' if speech_processor.tts_backend == 'piper' else 'edge-tts'}",
        "tts_backend": speech_processor.tts_backend,
        "tts_voice": _tts_voice,
        "last_detected_language": _last_detected_language,
        "last_detected_at": _last_detected_at,
        "sensitivity": _sensitivity,
        "last_confidence": round(last_confidence, 4),
        "conversation_active": responder.conversation.is_active,
        "conversation_turns": responder.conversation.turn_count,
        "is_speaking": responder.is_speaking,
        "is_processing": responder.is_processing,
        "hands_free_mode": _hands_free_mode,
        "wake_greeting_enabled": _wake_greeting_enabled,
        "weather_city": weather_city or "London",
        "stt_text": current_stt,
        "response_text": current_response,
        "recent_commands": [
            {"transcript": c["transcript"], "confidence": c.get("confidence", 0.0), "created_at": c["created_at"]}
            for c in recent_commands
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Voice latency baseline (Phase 0)
# ═════════════════════════════════════════════════════════════════════════════

# Cycle marks: duration is the OFFSET from the wake that opened the cycle, so
# deltas between them are meaningful.
_CYCLE_MARKS = (
    "voice_mic_released",
    "voice_connected",
    "voice_greeting_sent",
    "voice_first_audio",
)

# Plain durations, measured directly rather than relative to the wake.
_STANDALONE_EVENTS = ("greeting_context_fetch", "voice_response_ms")


@router.get("/latency")
async def voice_latency(limit: int = 500):
    """Voice-cycle latency baseline — the numbers that decide the LiveKit question.

    Read the ``marks`` as offsets from the wake:
      ``voice_connected``      wake → Gemini Live websocket handshake done
      ``voice_greeting_sent``  wake → greeting handed to TTS
      ``voice_first_audio``    wake → first PCM written to the speaker (headline)

    And the ``standalone`` durations:
      ``greeting_context_fetch``  weather/news fetch — cold ~1600 ms, cached ~0 ms
      ``voice_response_ms``       user stopped speaking → agent started speaking

    A cycle that has ``voice_connected`` but no ``voice_first_audio`` never
    produced sound. ``cycles_observed`` counts completed cycles.
    """
    evo = get_evolution_logger()
    evo.flush()  # keep the daily JSON file in step with this response

    marks = evo.get_latency_report(list(_CYCLE_MARKS), limit=limit)["event_types"]
    standalone = evo.get_latency_report(list(_STANDALONE_EVENTS), limit=limit)["event_types"]

    def _p50(name: str) -> Optional[float]:
        entry = marks.get(name)
        return entry["p50_ms"] if entry else None

    connected = _p50("voice_connected")
    greeting = _p50("voice_greeting_sent")
    first_audio = _p50("voice_first_audio")

    # Differences of p50s — indicative only. True per-cycle deltas need the raw
    # events, which are in ``recent`` (all offsets share one cycle origin).
    derived: dict[str, Optional[float]] = {
        "connect_ms": connected,
        "greeting_path_ms": (
            round(greeting - connected, 1)
            if greeting is not None and connected is not None else None
        ),
        "greeting_to_audio_ms": (
            round(first_audio - greeting, 1)
            if first_audio is not None and greeting is not None else None
        ),
        "wake_to_first_audio_ms": first_audio,
    }

    return {
        "cycles_observed": marks.get("voice_first_audio", {}).get("count", 0),
        "marks": marks,
        "standalone": standalone,
        "derived_p50": derived,
        "recent": evo.query(limit=15),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Dev-only: simulate a wake (requires BARQ_DEV_TRIGGERS=1)
# ═════════════════════════════════════════════════════════════════════════════

# A real wake can only be produced by speaking the wake word, which makes
# repeatable latency runs (the Phase 1 A/B work) impractical — you cannot talk to
# the machine 20 times per configuration. This runs the identical callback
# instead. Disabled by default and returns 404 rather than advertising itself.
# The sidecar binds 127.0.0.1 (see scripts/com.barq.mac.sidecar.plist), so this
# is not reachable from off-box.
_DEV_TRIGGERS_ENABLED = os.getenv("BARQ_DEV_TRIGGERS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

# The in-flight simulated-wake thread, if any. Guards against two simulated
# cycles overlapping — a real wake cannot overlap itself because the detector is
# paused for the whole conversation.
_simulate_wake_thread: Optional[threading.Thread] = None


class SimulateWakeRequest(BaseModel):
    """Body for ``POST /voice/simulate-wake``."""

    utterance: str = ""


@router.post("/simulate-wake")
async def simulate_wake(request: SimulateWakeRequest):
    """Fire the wake path without a wake word. DEV ONLY.

    Runs the exact callback a real detection runs, so the latency marks it
    records are directly comparable to a spoken wake.

    This makes BARQ speak its greeting out loud and opens the microphone.
    """
    if not _DEV_TRIGGERS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    if conversation_listener.is_active:
        raise HTTPException(
            status_code=409,
            detail=(
                "A conversation is already active — end it before simulating "
                "another wake"
            ),
        )

    # _on_wake_word_callback builds its OWN event loop and blocks on
    # loop.run_forever() for the duration of the conversation, so it has to run
    # on a dedicated thread — exactly how the Vosk detector thread calls it.
    # Awaiting it here would raise "This event loop is already running".
    #
    # Only one simulated cycle may be in flight at a time: a real wake can't
    # overlap itself (the detector is paused for the whole conversation), and two
    # concurrent callbacks would each build a competing event loop and listener.
    global _simulate_wake_thread
    if _simulate_wake_thread is not None and _simulate_wake_thread.is_alive():
        raise HTTPException(
            status_code=409,
            detail=(
                "A previous simulated wake is still running — wait for it to "
                "finish (check /voice/status for conversation_active)"
            ),
        )

    _simulate_wake_thread = threading.Thread(
        target=_on_wake_word_callback,
        args=(request.utterance,),
        name="simulate-wake",
        daemon=True,
    )
    _simulate_wake_thread.start()

    return {
        "status": "fired",
        "utterance": request.utterance,
        "hands_free_mode": _hands_free_mode,
        "note": (
            "Greeting plays aloud; the mic opens and a real conversation starts. "
            "Read GET /voice/latency for the marks."
            if _hands_free_mode
            else "WARNING: hands-free mode is OFF — the callback will return "
            "without starting a conversation. Enable hands-free first."
        ),
    }
