"""
Voice Agent Factory — creates the appropriate voice agent based on config.

The active backend is stored in the database (settings_dao) under
the key ``voice_agent_backend`` with values ``"deepgram"`` or ``"gemini"``.
The default is ``"gemini"`` (requires GEMINI_API_KEY).
"""

import os
from typing import Any, Optional

from config import get_settings

# Module-level cache
_voice_agent_instance = None
_voice_agent_backend: Optional[str] = None


def get_available_backends() -> list[dict[str, Any]]:
    """Return metadata about all available voice agent backends."""
    return [
        {
            "id": "deepgram",
            "name": "Deepgram Voice Agent",
            "description": "Cloud STT + Gemini LLM + TTS (managed WebSocket). Requires DEEPGRAM_API_KEY.",
            "requires_api_key": True,
            "api_key_configured": bool(get_settings().deepgram_api_key),
            "latency": "low (cloud)",
        },
        {
            "id": "gemini",
            "name": "Gemini Live (cloud)",
            "description": "Google Gemini native audio WebSocket — STT + LLM + TTS in one stream. Requires GEMINI_API_KEY. No local processing needed.",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("GEMINI_API_KEY")),
            "latency": "low (native audio WebSocket)",
        },
    ]


async def get_backend_from_db() -> str:
    """Read the current voice agent backend from the database.

    Returns ``"deepgram"`` if not set or on error.
    """
    try:
        from database import settings_dao
        val = await settings_dao.get_setting("voice_agent_backend")
        if val in ("deepgram", "gemini"):
            return val
    except Exception:
        pass
    # Fallback: check env var
    env_val = os.getenv("VOICE_AGENT_BACKEND", "gemini")
    if env_val in ("deepgram", "gemini"):
        return env_val
    return "gemini"


def _create_agent(backend: str):
    """Create a new voice agent instance (no caching).

    Args:
        backend: ``"deepgram"`` or ``"gemini"``.
    """
    settings = get_settings()

    if backend == "gemini":
        from .gemini_agent import GeminiVoiceAgent
        return GeminiVoiceAgent(api_key=os.getenv("GEMINI_API_KEY", ""))

    # Default: Deepgram
    if not settings.deepgram_api_key:
        print("[AgentFactory] WARNING: DEEPGRAM_API_KEY not set — voice agent will fail to connect")
    from .deepgram_agent import DeepgramVoiceAgent
    return DeepgramVoiceAgent(api_key=settings.deepgram_api_key)


def get_voice_agent(backend: Optional[str] = None):
    """Get or create the voice agent singleton.

    Args:
        backend: Force a specific backend. If None, reads from DB/env.

    Returns:
        A VoiceAgentBase instance (DeepgramVoiceAgent or GeminiVoiceAgent).
    """
    global _voice_agent_instance, _voice_agent_backend

    import os
    resolved = backend or os.getenv("VOICE_AGENT_BACKEND", "gemini")

    if _voice_agent_instance is not None and _voice_agent_backend == resolved:
        return _voice_agent_instance

    # Create new instance
    _voice_agent_instance = _create_agent(resolved or "gemini")
    _voice_agent_backend = resolved
    print(f"[AgentFactory] Created voice agent: {resolved}")
    return _voice_agent_instance


async def get_voice_agent_async(backend: Optional[str] = None):
    """Async version: resolves backend from DB if not specified.

    Deepgram and Gemini Live don't need Pipecat-style TTS settings,
    so this is simpler than the previous version.
    """
    global _voice_agent_instance, _voice_agent_backend

    resolved = backend or await get_backend_from_db()

    if _voice_agent_instance is not None and _voice_agent_backend == resolved:
        return _voice_agent_instance

    _voice_agent_instance = _create_agent(resolved)
    _voice_agent_backend = resolved
    print(f"[AgentFactory] Created voice agent (async): {resolved}")
    return _voice_agent_instance


def get_cached_voice_agent():
    """Return the cached voice agent instance WITHOUT creating one.

    Returns ``None`` if no agent has been instantiated yet.  Useful for
    live-config updates (e.g. the mic RMS energy threshold) that should
    only be applied to an already-running agent — new agents load their
    own settings from the DB on ``connect()``.
    """
    return _voice_agent_instance


def reset_voice_agent():
    """Clear the cached agent instance.  Call before creating a new one
    with a different backend."""
    global _voice_agent_instance, _voice_agent_backend
    _voice_agent_instance = None
    _voice_agent_backend = None
