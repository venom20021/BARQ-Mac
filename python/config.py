"""
BARQ Configuration - Centralized settings management.
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env file from the project root
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
# Safely load .env — handle UTF-16 BOM or encoding issues gracefully.
# The `moviepy` library also calls `find_dotenv()` at import time, so a
# corrupted .env file can crash the entire app before our code runs.
# We try UTF-8 first, then fall back to detecting and fixing encoding.
if os.path.exists(env_path):
    try:
        load_dotenv(env_path, override=True)
    except UnicodeDecodeError:
        # The .env file is not valid UTF-8 (e.g. UTF-16 with BOM).
        # Attempt to decode as UTF-16, strip BOM, and re-save as UTF-8.
        print("[Config] .env file encoding issue detected — attempting repair...")
        try:
            with open(env_path, "rb") as f:
                raw = f.read()
            # Try UTF-16 LE (with BOM 0xFF 0xFE)
            if raw[:2] == b"\xff\xfe":
                decoded = raw.decode("utf-16-le")
            elif raw[:2] == b"\xfe\xff":
                decoded = raw.decode("utf-16-be")
            elif raw[:3] == b"\xef\xbb\xbf":
                decoded = raw.decode("utf-8-sig")
            else:
                decoded = raw.decode("utf-8")
            # Strip any remaining null characters
            decoded = decoded.replace("\x00", "")
            # Write back as clean UTF-8
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(decoded)
            print("[Config] .env repaired and re-saved as UTF-8")
            # Now retry load
            load_dotenv(env_path, override=True)
        except Exception as repair_err:
            print(f"[Config] Could not repair .env encoding: {repair_err}")
else:
    load_dotenv(env_path, override=True)


class Settings(BaseSettings):
    # Sidecar server
    # IMPORTANT: Port 8956 is the standard port used by all startup scripts
    # (start.bat, start_backend.vbs, watchdog.ps1) and the Electron frontend.
    # Do NOT change this default without updating ALL callers.
    host: str = os.getenv("SIDECAR_HOST", "127.0.0.1")
    port: int = int(os.getenv("SIDECAR_PORT", "8956"))
    debug: bool = os.getenv("BARQ_DEBUG", "false").lower() == "true"

    # Voice
    wake_word: str = os.getenv("WAKE_WORD", "computer")
    vosk_model_path: str = os.getenv("VOSK_MODEL_PATH", "models/vosk")
    vosk_hindi_model_path: str = os.getenv("VOSK_HINDI_MODEL_PATH", "models/vosk-hi")
    whisper_model: str = os.getenv("WHISPER_MODEL", "medium")
    voice_language: str = os.getenv("VOICE_LANGUAGE", "en")  # "en" or "hi"

    # Audio device selection (empty = system default)
    # Use "auto" to auto-detect the best physical mic, or set device index/name
    audio_input_device: str = os.getenv("AUDIO_INPUT_DEVICE", "auto")
    audio_output_device: str = os.getenv("AUDIO_OUTPUT_DEVICE", "auto")

    # LLM
    llm_backend: str = os.getenv("LLM_BACKEND", "auto")  # "auto", "ollama", or "openai"
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

    # Job Search
    scheduler_enabled: bool = os.getenv("BARQ_SCHEDULER", "true").lower() == "true"
    job_scan_interval_hours: int = int(os.getenv("JOB_SCAN_INTERVAL_HOURS", "6"))
    auto_match_interval_hours: int = int(os.getenv("AUTO_MATCH_INTERVAL_HOURS", "1"))
    match_threshold: float = float(os.getenv("MATCH_THRESHOLD", "0.7"))
    match_threshold_high: float = float(os.getenv("MATCH_THRESHOLD_HIGH", "80"))
    match_threshold_medium: float = float(os.getenv("MATCH_THRESHOLD_MEDIUM", "60"))

    # Social Media
    trend_check_interval_hours: int = int(os.getenv("TREND_CHECK_INTERVAL_HOURS", "6"))
    # Stock footage for video rendering (free Pexels API key — optional)
    pexels_api_key: str = os.getenv("PEXELS_API_KEY", "")

    # Database
    database_url: str = os.getenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'barq.db')}",
    )

    # Notifications
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Email / SMTP
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    notification_email: str = os.getenv("NOTIFICATION_EMAIL", "")
    sender_name: str = os.getenv("SENDER_NAME", "")

    # Career / Jobs
    career_ops_path: str = os.getenv("CAREER_OPS_PATH", os.path.join(os.path.expanduser("~"), "career-ops"))
    resume_path: str = os.getenv("RESUME_PATH", "")
    barq_port: int = int(os.getenv("BARQ_PORT", "8111"))

    # API Authentication
    barq_api_key: str = os.getenv("BARQ_API_KEY", "")

    # Cloud LLM Fallback (when Ollama/LM Studio is offline)
    cloud_llm_enabled: bool = os.getenv("CLOUD_LLM_ENABLED", "true").lower() == "true"
    cloud_llm_model: str = os.getenv("CLOUD_LLM_MODEL", "gpt-4o-mini")
    cloud_llm_base_url: str = os.getenv("CLOUD_LLM_BASE_URL", "https://api.openai.com/v1")  # Can use OpenRouter, Groq, etc.
    # Secondary cloud fallback (e.g. Groq when LM Studio is unreachable)
    cloud_llm_fallback_enabled: bool = os.getenv("CLOUD_LLM_FALLBACK_ENABLED", "false").lower() == "true"
    cloud_llm_fallback_base_url: str = os.getenv("CLOUD_LLM_FALLBACK_BASE_URL", "")
    cloud_llm_fallback_model: str = os.getenv("CLOUD_LLM_FALLBACK_MODEL", "gpt-4o-mini")

    # Turso Cloud Database
    turso_enabled: bool = os.getenv("TURSO_ENABLED", "false").lower() == "true"
    turso_database_url: str = os.getenv("TURSO_DATABASE_URL", "")
    turso_auth_token: str = os.getenv("TURSO_AUTH_TOKEN", "")

    # ── Agentic Workflows ────────────────────────────────────────────
    # Morning briefing (W4)
    briefing_enabled: bool = os.getenv("BRIEFING_ENABLED", "true").lower() == "true"
    briefing_time: str = os.getenv("BRIEFING_TIME", "08:00")  # 24h HH:MM
    # Conversation memory extraction (W5)
    conversation_memory_enabled: bool = os.getenv("CONVERSATION_MEMORY_ENABLED", "true").lower() == "true"
    # Research -> knowledge graph extraction (W6)
    research_to_brain_enabled: bool = os.getenv("RESEARCH_TO_BRAIN_ENABLED", "true").lower() == "true"
    # Evaluator-Optimizer gate in the jobs pipeline (Feature 2)
    evaluator_enabled: bool = os.getenv("EVALUATOR_ENABLED", "true").lower() == "true"
    evaluator_threshold: int = int(os.getenv("EVALUATOR_THRESHOLD", "80"))  # 0-100
    evaluator_max_iterations: int = int(os.getenv("EVALUATOR_MAX_ITERATIONS", "2"))
    # Content critic quality gate in the social pipeline (W7)
    content_critic_enabled: bool = os.getenv("CONTENT_CRITIC_ENABLED", "true").lower() == "true"
    content_critic_min_score: int = int(os.getenv("CONTENT_CRITIC_MIN_SCORE", "80"))  # 0-100
    content_critic_max_iterations: int = int(os.getenv("CONTENT_CRITIC_MAX_ITERATIONS", "2"))
    # Weekly review report (W11)
    weekly_review_enabled: bool = os.getenv("WEEKLY_REVIEW_ENABLED", "true").lower() == "true"
    weekly_review_time: str = os.getenv("WEEKLY_REVIEW_TIME", "09:00")  # 24h HH:MM
    weekly_review_day: str = os.getenv("WEEKLY_REVIEW_DAY", "sun")  # apscheduler day_of_week (mon..sun)
    # Periodic knowledge-graph re-import (notes / memory / jobs → brains)
    brain_reimport_enabled: bool = os.getenv("BRAIN_REIMPORT_ENABLED", "true").lower() == "true"
    brain_reimport_interval_hours: int = int(os.getenv("BRAIN_REIMPORT_INTERVAL_HOURS", "6"))
    # SQL tool-use skill (Feature 1) — safety gate for write queries
    sql_tool_readonly: bool = os.getenv("SQL_TOOL_READONLY", "true").lower() == "true"

    # Voice agent selection (deepgram or pipecat)
    voice_agent_backend: str = os.getenv("VOICE_AGENT_BACKEND", "pipecat")

    # Deepgram (cloud STT/TTS)
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")

    # ── Second Brain Integration ─────────────────────────────────────
    second_brain_enabled: bool = os.getenv("SECOND_BRAIN_ENABLED", "false").lower() == "true"
    second_brain_url: str = os.getenv("SECOND_BRAIN_URL", "http://127.0.0.1:8000")
    second_brain_api_key: str = os.getenv("SECOND_BRAIN_API_KEY", "")
    second_brain_sync_interval_minutes: int = int(os.getenv("SECOND_BRAIN_SYNC_INTERVAL", "5"))

    # External API Keys (loaded from .env)
    linkedin_email: str = os.getenv("LINKEDIN_EMAIL", "")
    linkedin_password: str = os.getenv("LINKEDIN_PASSWORD", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    youtube_api_key: str = os.getenv("YOUTUBE_API_KEY", "")
    twitter_api_key: str = os.getenv("TWITTER_API_KEY", "")
    twitter_api_secret: str = os.getenv("TWITTER_API_SECRET", "")

    model_config = {"env_file": env_path, "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()


# ─── ffmpeg PATH helper ───────────────────────────────────────────────
# pydub uses ffmpeg for audio conversion. The Winget-installed ffmpeg is in
# the LOCALAPPDATA directory which may not be on the system PATH in all
# environments (e.g. when launched from VS Code terminal, Electron sidecar,
# or Windows Service). This fallback ensures it's always available.

def ensure_ffmpeg_on_path() -> bool:
    """Ensure ffmpeg is available on the process PATH.

    Checks if ffmpeg is already findable via ``shutil.which()``.
    If not, probes common ffmpeg install locations and, if found,
    prepends its directory to ``os.environ['PATH']`` so that pydub,
    ffmpeg-python, and subprocess calls can locate it.

    Returns True if ffmpeg was found (either already on PATH or after
    the fallback), False otherwise.
    """
    import shutil

    if shutil.which("ffmpeg"):
        return True

    # Common ffmpeg install locations (in preference order)
    candidates = [
        # Winget (Gyan.FFmpeg) — wildcard publisher hash so it survives
        # winget package updates (the hash suffix can change between installs)
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Microsoft", "WinGet", "Packages",
            "Gyan.FFmpeg_*", "ffmpeg-*-full_build", "bin",
        ),
        # Scoop
        os.path.join(os.path.expanduser("~"), "scoop", "shims"),
        # Chocolatey
        r"C:\ProgramData\chocolatey\bin",
        # Manual install
        r"C:\Program Files\ffmpeg\bin",
        r"C:\tools\ffmpeg\bin",
    ]

    import glob as _glob
    for candidate in candidates:
        expanded = os.path.expanduser(candidate)
        # Handle wildcard in path (e.g. ffmpeg-*-full_build)
        matches = _glob.glob(expanded, recursive=False)
        for match in matches:
            if os.path.isdir(match) and shutil.which("ffmpeg", path=match):
                os.environ["PATH"] = match + os.pathsep + os.environ.get("PATH", "")
                print(f"[Config] ffmpeg found at: {match}")
                return True
        # Also check the raw path (non-wildcard)
        if os.path.isdir(expanded) and shutil.which("ffmpeg", path=expanded):
            os.environ["PATH"] = expanded + os.pathsep + os.environ.get("PATH", "")
            print(f"[Config] ffmpeg found at: {expanded}")
            return True

    print("[Config] ffmpeg not found — audio/video conversion via pydub will be unavailable")
    return False


# Run the check at import time so pydub picks up ffmpeg before any audio code loads
_ffmpeg_ok = ensure_ffmpeg_on_path()
