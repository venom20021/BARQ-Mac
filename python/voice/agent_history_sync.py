"""
Persist voice-detector commands (wake-word utterances, Gemini Live / Deepgram
session turns) into the backend's ``agent_chat_history`` setting under the
``voice_commands`` key, so the brain re-import feeds spoken topics into the
``ai_chats`` knowledge graph — the same sink the Chat page and AiChat panel use.

The write is best-effort and NEVER blocks the voice pipeline: any failure is
caught and logged (non-fatal). When a remote backend is reachable, the entry
is mirrored there too (fire-and-forget) so the cloud-side re-import — and the
Knowledge Graph page in remote mode — sees spoken commands as well.
"""

import asyncio
import json
import os
import time

from database import settings_dao
from voice.loop_utils import call_on_main_loop, is_main_loop, run_on_main_loop

# Serializes the local read-modify-write of agent_chat_history so two rapid
# final transcripts (different text) can't interleave and drop one entry.
# Note: asyncio.Lock() no longer binds to a loop at creation (Python 3.10+).
_persist_lock = asyncio.Lock()

# Key under which spoken commands live inside the agent_chat_history dict.
VOICE_COMMANDS_KEY = "voice_commands"
# Keep the last N utterances per key (mirrors the frontend's 100-message cap).
_MAX_ENTRIES = 200
# Skip appending if identical to the most recent entry within this window —
# prevents double-logging when a wake utterance is re-transcribed by the agent.
_DEDUPE_WINDOW_S = 90.0
# Default remote backend URL (matches the Electron bridge's DEFAULT_REMOTE_URL).
_DEFAULT_REMOTE_URL = "http://sai-prabhat-HP-All-in-One-22-dd0xxx.local:8956"
# Mirror write timeout — a slow/unreachable VM must never stall the pipeline.
_MIRROR_TIMEOUT_S = 3.0


def _remote_url() -> str:
    """Resolve the remote backend URL (env override, else the app default)."""
    return (os.getenv("BARQ_REMOTE_URL") or "").strip() or _DEFAULT_REMOTE_URL


def schedule_persist_voice_utterance(text: str) -> None:
    """Thread/loop-safe fire-and-forget persist (never blocks the voice loop).

    Safe to call from ANY thread or event loop — including the wake-word
    managed loop where ``asyncio.create_task`` on the main-loop-bound DB
    would raise "Future attached to a different loop".  The actual DB write
    is marshaled onto the main backend loop.
    """
    text = (text or "").strip()
    if not text:
        return
    if run_on_main_loop(_persist_impl(text)) is None:
        # No live main loop to marshal onto.  Only fall back to the current
        # loop if it IS the main loop (tests / pre-lifespan) — never the
        # managed voice loop, which would re-trigger the cross-loop bug.
        if is_main_loop():
            try:
                asyncio.create_task(_persist_impl(text))
            except RuntimeError:
                print("[AgentHistory] Voice persist skipped (no event loop available)")


async def persist_voice_utterance(text: str) -> None:
    """Append a spoken command/utterance to agent_chat_history (best-effort).

    Loop-safe: the DB work is marshaled onto the main backend loop via
    ``call_on_main_loop`` so callers on the managed voice loop never touch
    main-loop-bound futures directly.

    Args:
        text: The transcribed command/utterance (wake command, agent turn, ...).
    """
    text = (text or "").strip()
    if not text:
        return
    await call_on_main_loop(_persist_impl(text))


async def _persist_impl(text: str) -> None:
    """Core persistence (runs on the main loop when the main loop is known)."""
    async with _persist_lock:
        try:
            raw = await settings_dao.get_setting("agent_chat_history")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {}

            items = data.get(VOICE_COMMANDS_KEY)
            if not isinstance(items, list):
                items = []

            now = time.time()
            # Dedupe: identical text right after the previous entry (e.g. the same
            # wake utterance re-transcribed by the voice agent) is skipped.
            if items and isinstance(items[-1], dict):
                last = items[-1]
                last_text = str(last.get("content") or "").strip()
                last_ts = float(last.get("timestamp") or 0)
                if last_text == text and (now - last_ts) < _DEDUPE_WINDOW_S:
                    return

            items.append({"role": "user", "content": text, "timestamp": now})
            data[VOICE_COMMANDS_KEY] = items[-_MAX_ENTRIES:]

            await settings_dao.set_setting(
                "agent_chat_history",
                json.dumps(data),
                category="memory",
            )
            print(f"[AgentHistory] Voice command logged: '{text[:60]}'")
        except Exception as e:
            print(f"[AgentHistory] Voice persist error (non-fatal): {e}")
            return

    # Fire-and-forget mirror — must never block or fail the caller.
    try:
        asyncio.create_task(_mirror_voice_commands(data))
    except RuntimeError as e:
        # No running loop in this thread (e.g. during shutdown) — the local
        # copy is already persisted, so this is non-fatal but worth knowing.
        print(f"[AgentHistory] Mirror task skipped (no running loop): {e}")


async def _mirror_voice_commands(local_data: dict) -> None:
    """Merge ONLY the local voice_commands key into the remote history dict.

    Reads the remote dict first so other agents' keys (chat_page, aichat_panel,
    voice sessions, ...) are preserved, then POSTs the merged result back.
    """
    import httpx

    url = _remote_url()
    try:
        async with httpx.AsyncClient(timeout=_MIRROR_TIMEOUT_S) as client:
            resp = await client.get(f"{url}/memory/agent-history")
            if resp.status_code != 200:
                return
            payload = resp.json()
            remote = payload.get("history") if isinstance(payload, dict) else None
            if not isinstance(remote, dict):
                remote = {}
            voice = local_data.get(VOICE_COMMANDS_KEY)
            if isinstance(voice, list) and voice:
                remote[VOICE_COMMANDS_KEY] = voice
            await client.post(f"{url}/memory/agent-history", json={"history": remote})
            print(f"[AgentHistory] Mirrored {len(voice or [])} voice commands to {url}")
    except Exception:
        pass  # remote unreachable — local copy already persisted
