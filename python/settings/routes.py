"""
FastAPI routes for user-facing settings management.
Provides endpoints for cloud LLM configuration that can be
saved/loaded via the Settings UI.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import settings_dao

router = APIRouter()

SETTINGS_CATEGORY = "cloud_llm"


# ─── Models ──────────────────────────────────────────────────────────────────


class CloudLLMSettingsRequest(BaseModel):
    enabled: bool = True
    api_key: str = ""
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"


class AssistantCustomizationRequest(BaseModel):
    assistant_name: str = "BARQ"
    user_name: str = ""
    accent_color: str = "cyan"


class BriefingSettingsRequest(BaseModel):
    enabled: bool = True
    time: str = "08:00"  # 24h HH:MM


class SecondBrainSettingsRequest(BaseModel):
    enabled: bool = False
    url: str = "http://127.0.0.1:8000"
    api_key: str = ""
    sync_interval: int = 5  # minutes
    auto_sync: bool = False


class OllamaSettingsRequest(BaseModel):
    host: str = "http://127.0.0.1:11434"
    model: str = "llama3.2:3b"


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/settings/assistant", summary="Get assistant customization")
async def get_assistant_customization():
    """Get assistant name, user name, and accent color preferences."""
    try:
        assistant_name = await settings_dao.get_setting("assistant_name") or "BARQ"
        user_name = await settings_dao.get_setting("user_name") or ""
        accent_color = await settings_dao.get_setting("accent_color") or "cyan"
        return {
            "assistant_name": assistant_name,
            "user_name": user_name,
            "accent_color": accent_color,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings/assistant", summary="Save assistant customization")
async def save_assistant_customization(request: AssistantCustomizationRequest):
    """Save assistant name, user name, and accent color preferences."""
    try:
        await settings_dao.set_setting("assistant_name", request.assistant_name.strip() or "BARQ", "general")
        await settings_dao.set_setting("user_name", request.user_name.strip(), "general")
        await settings_dao.set_setting("accent_color", request.accent_color.strip() or "cyan", "general")
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Morning Briefing ──────────────────────────────────────────────────────────


@router.get("/settings/briefing", summary="Get morning briefing settings")
async def get_briefing_settings():
    """Get briefing preferences plus its scheduled-task registration status."""
    try:
        from .briefing import get_briefing_config
        cfg = await get_briefing_config()
        task = await settings_dao.get_scheduled_task("Morning Briefing")
        return {
            "enabled": cfg["enabled"],
            "time": cfg["time"],
            "scheduled": bool(task),
            "cron": task["cron_expression"] if task else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings/briefing", summary="Save morning briefing settings")
async def save_briefing_settings(request: BriefingSettingsRequest):
    """Persist briefing preferences and register the scheduled task."""
    try:
        from .briefing import save_briefing_config
        result = await save_briefing_config(request.enabled, request.time)
        return {"status": "saved", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/settings/cloud-llm", summary="Get cloud LLM settings")
async def get_cloud_llm_settings():
    """Get current cloud LLM configuration from the database."""
    try:
        enabled_raw = await settings_dao.get_setting("cloud_llm_enabled")
        api_key = await settings_dao.get_setting("cloud_llm_api_key")
        model = await settings_dao.get_setting("cloud_llm_model")
        base_url = await settings_dao.get_setting("cloud_llm_base_url")

        return {
            "enabled": enabled_raw == "true" if enabled_raw else True,
            "has_api_key": bool(api_key),
            "api_key_masked": (
                api_key[:8] + "..." + api_key[-4:]
                if api_key and len(api_key) > 12
                else ("*" * min(len(api_key), 8) if api_key else "")
            ),
            "model": model or "gpt-4o-mini",
            "base_url": base_url or "https://api.openai.com/v1",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings/cloud-llm", summary="Save cloud LLM settings")
async def save_cloud_llm_settings(request: CloudLLMSettingsRequest):
    """Save cloud LLM configuration to the database."""
    try:
        await settings_dao.set_setting(
            "cloud_llm_enabled", str(request.enabled).lower(), SETTINGS_CATEGORY
        )
        if request.api_key:
            await settings_dao.set_setting(
                "cloud_llm_api_key", request.api_key, SETTINGS_CATEGORY
            )
        if request.model:
            await settings_dao.set_setting(
                "cloud_llm_model", request.model, SETTINGS_CATEGORY
            )
        if request.base_url:
            await settings_dao.set_setting(
                "cloud_llm_base_url", request.base_url, SETTINGS_CATEGORY
            )
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Second Brain Integration ────────────────────────────────────────────────


@router.get("/settings/second-brain", summary="Get Second Brain settings")
async def get_second_brain_settings():
    """Get Second Brain integration configuration from the database."""
    try:
        enabled_raw = await settings_dao.get_setting("second_brain_enabled")
        url = await settings_dao.get_setting("second_brain_url")
        api_key = await settings_dao.get_setting("second_brain_api_key")
        sync_interval_raw = await settings_dao.get_setting("second_brain_sync_interval")
        auto_sync_raw = await settings_dao.get_setting("second_brain_auto_sync")

        return {
            "enabled": enabled_raw == "true" if enabled_raw else False,
            "url": url or "http://127.0.0.1:8000",
            "has_api_key": bool(api_key),
            "api_key_masked": (
                api_key[:8] + "..." + api_key[-4:]
                if api_key and len(api_key) > 12
                else ("*" * min(len(api_key), 8) if api_key else "")
            ),
            "sync_interval": int(sync_interval_raw) if sync_interval_raw else 5,
            "auto_sync": auto_sync_raw == "true" if auto_sync_raw else False,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings/second-brain", summary="Save Second Brain settings")
async def save_second_brain_settings(request: SecondBrainSettingsRequest):
    """Save Second Brain integration configuration to the database."""
    try:
        await settings_dao.set_setting(
            "second_brain_enabled", str(request.enabled).lower(), "second_brain"
        )
        await settings_dao.set_setting(
            "second_brain_url", request.url.strip() or "http://127.0.0.1:8000", "second_brain"
        )
        if request.api_key:
            await settings_dao.set_setting(
                "second_brain_api_key", request.api_key.strip(), "second_brain"
            )
        await settings_dao.set_setting(
            "second_brain_sync_interval", str(request.sync_interval), "second_brain"
        )
        await settings_dao.set_setting(
            "second_brain_auto_sync", str(request.auto_sync).lower(), "second_brain"
        )

        # Apply auto-sync changes immediately
        try:
            from memory_knowledge.second_brain_sync import auto_sync_scheduler
            if request.auto_sync:
                auto_sync_scheduler.set_interval(request.sync_interval * 60)
                await auto_sync_scheduler.start()
            else:
                await auto_sync_scheduler.stop()
        except Exception:
            pass  # Sync engine might not be available yet

        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Ollama Settings ───────────────────────────────────────────────────────


@router.get("/settings/ollama", summary="Get Ollama LLM configuration")
async def get_ollama_settings():
    """Load Ollama settings from the database (with .env fallback)."""
    try:
        host = await settings_dao.get_setting("ollama_host", "ollama")
        model = await settings_dao.get_setting("ollama_model", "ollama")
        return {
            "host": host or "http://127.0.0.1:11434",
            "model": model or "llama3.2:3b",
        }
    except Exception:
        # Fallback to env vars
        import os
        return {
            "host": os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"),
            "model": os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        }


@router.post("/settings/ollama", summary="Save Ollama LLM configuration")
async def save_ollama_settings(request: OllamaSettingsRequest):
    """Save Ollama settings to the database and apply to runtime config."""
    try:
        host = request.host.strip() or "http://127.0.0.1:11434"
        model = request.model.strip() or "llama3.2:3b"

        await settings_dao.set_setting("ollama_host", host, "ollama")
        await settings_dao.set_setting("ollama_model", model, "ollama")

        # Apply to runtime config immediately
        try:
            from config import get_settings
            settings = get_settings()
            settings.ollama_host = host
            settings.ollama_model = model
        except Exception:
            pass

        return {"status": "saved", "host": host, "model": model}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/settings/ollama/test", summary="Test Ollama connection")
async def test_ollama_connection():
    """Ping the Ollama server and check if the configured model is available."""
    import httpx

    try:
        settings = await get_ollama_settings()
        host = settings["host"].rstrip("/")
        model = settings["model"]

        async with httpx.AsyncClient(timeout=5) as client:
            # Check server
            resp = await client.get(f"{host}/api/tags")
            if resp.status_code != 200:
                return {"connected": False, "error": f"Server returned {resp.status_code}"}

            # Check model
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            model_found = any(model.split(":")[0] in m for m in models)

            return {
                "connected": True,
                "model_available": model_found,
                "model": model,
                "models_available": models[:10],
            }
    except httpx.TimeoutException:
        return {"connected": False, "error": "Connection timed out"}
    except Exception as e:
        return {"connected": False, "error": str(e)}
