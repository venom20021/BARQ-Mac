"""
FastAPI routes for the Auto Applier module.

Integrates with the existing BARQ FastAPI server at /api/jobs/auto-apply/...

The pipeline orchestration is now handled by jobs/pipeline.py (the single
orchestrator). This module provides:
  - Status and profile endpoints
  - Direct apply/batch via ApplicationEngine (for programmatic use)
  - Full pipeline trigger (delegates to jobs.pipeline.run_pipeline)
  - Telegram bot management
  - EvoMap failure logs
"""

import asyncio
import importlib
from typing import Any, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/auto-apply", tags=["auto-apply"])

# ─── Lazy singletons ─────────────────────────────────────────────────────

_engine = None
_bot = None


def _get_engine():
    """Lazy-load ApplicationEngine (requires playwright)."""
    global _engine
    if _engine is None:
        try:
            mod = importlib.import_module("jobs.auto_applier.applier.engine")
            _engine = mod.ApplicationEngine()
        except ImportError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Auto-apply engine unavailable (playwright not installed): {e}",
            )
    return _engine


def _get_bot():
    """Lazy-load AutoApplyBot (requires aiogram)."""
    global _bot
    if _bot is None:
        try:
            mod = importlib.import_module("jobs.auto_applier.telegram.bot")
            _bot = mod.AutoApplyBot()
        except ImportError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Telegram bot unavailable (aiogram not installed): {e}",
            )
    return _bot


# ─── Schemas ──────────────────────────────────────────────────────────────


class ApplyRequest(BaseModel):
    job_url: str
    company: str = ""
    title: str = ""
    job_context: str = ""


class BatchApplyRequest(BaseModel):
    jobs: list[ApplyRequest]


class PipelineSettingsRequest(BaseModel):
    max_applications_per_run: int = 10
    match_threshold: int = 60
    pause_before_submit: bool = False
    interactive_mode: bool = False


# ─── Status ───────────────────────────────────────────────────────────────


@router.get("/status")
async def auto_apply_status():
    """Get auto applier status and configuration."""
    # Lazy import config to avoid circular deps
    try:
        cfg_mod = importlib.import_module("jobs.auto_applier.config")
        CONFIG = cfg_mod.CONFIG
        PROFILE = cfg_mod.PROFILE
    except ImportError:
        CONFIG = None
        PROFILE = None

    # Get main pipeline progress
    try:
        from jobs.pipeline import get_pipeline_progress
        pipeline_progress = get_pipeline_progress()
    except ImportError:
        pipeline_progress = {"status": "unavailable"}

    return {
        "pipeline": pipeline_progress,
        "config": {
            "ollama_model": CONFIG.ollama_model if CONFIG else "unknown",
            "ollama_host": CONFIG.ollama_host if CONFIG else "unknown",
            "max_applications_per_run": CONFIG.max_applications_per_run if CONFIG else 10,
            "match_threshold": CONFIG.match_threshold if CONFIG else 60,
            "pause_before_submit": CONFIG.pause_before_submit if CONFIG else False,
            "headless": CONFIG.headless if CONFIG else False,
            "telegram_configured": bool(CONFIG.telegram_bot_token) if CONFIG else False,
            "linkedin_configured": bool(CONFIG.linkedin_email) if CONFIG else False,
            "tinyfish_configured": bool(CONFIG.tinyfish_api_key) if CONFIG else False,
        },
        "profile": {
            "name": PROFILE.full_name if PROFILE else "unknown",
            "education": PROFILE.education if PROFILE else "",
            "seeking": PROFILE.seeking if PROFILE else "",
            "skills_count": len(PROFILE.skills) if PROFILE else 0,
        },
    }


@router.get("/profile")
async def get_profile():
    """Get the candidate profile for form filling."""
    try:
        cfg_mod = importlib.import_module("jobs.auto_applier.config")
        PROFILE = cfg_mod.PROFILE
    except ImportError:
        raise HTTPException(status_code=503, detail="Config unavailable")

    return {
        "full_name": PROFILE.full_name,
        "email": PROFILE.email,
        "education": PROFILE.education,
        "skills": PROFILE.skills,
        "seeking": PROFILE.seeking,
        "experience_count": len(PROFILE.experiences),
    }


# ─── Apply ────────────────────────────────────────────────────────────────


@router.post("/apply")
async def apply_to_job(request: ApplyRequest):
    """Apply to a single job URL using ApplicationEngine directly."""
    engine = _get_engine()
    try:
        result = await engine.apply_to_job(
            job_url=request.job_url,
            company=request.company,
            title=request.title,
            job_context=request.job_context,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/apply/batch")
async def apply_batch(request: BatchApplyRequest, background_tasks: BackgroundTasks):
    """Apply to multiple jobs in the background."""
    engine = _get_engine()
    jobs = [j.model_dump() for j in request.jobs]

    async def _run():
        try:
            await engine.apply_batch(jobs)
        except Exception as exc:
            print(f"[AutoApply] Batch error: {exc}")

    asyncio.create_task(_run())

    return {
        "status": "started",
        "jobs_queued": len(jobs),
        "message": f"Processing {len(jobs)} jobs in background",
    }


# ─── Pipeline ─────────────────────────────────────────────────────────────


@router.post("/pipeline/run")
async def run_pipeline(background_tasks: BackgroundTasks):
    """Run the full pipeline (delegates to jobs.pipeline.run_pipeline).

    This is the unified entry point — same as POST /jobs/pipeline/run.
    """
    from jobs.pipeline import get_pipeline_progress, run_pipeline as _run_pipeline

    progress = get_pipeline_progress()
    if progress["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline is already running")

    async def _run():
        try:
            await _run_pipeline({"auto_apply": True})
        except Exception as exc:
            print(f"[AutoApply] Pipeline error: {exc}")

    asyncio.create_task(_run())

    return {"status": "started", "message": "Auto-apply pipeline started in background"}


@router.get("/progress")
async def auto_apply_progress():
    """Get real-time progress of the running pipeline."""
    from jobs.pipeline import get_pipeline_progress
    return get_pipeline_progress()


@router.post("/pipeline/settings")
async def update_pipeline_settings(settings: PipelineSettingsRequest):
    """Update pipeline settings (applied to auto_applier config)."""
    try:
        cfg_mod = importlib.import_module("jobs.auto_applier.config")
        CONFIG = cfg_mod.CONFIG
        CONFIG.max_applications_per_run = settings.max_applications_per_run
        CONFIG.match_threshold = settings.match_threshold
        CONFIG.pause_before_submit = settings.pause_before_submit
        CONFIG.interactive_mode = settings.interactive_mode
    except ImportError:
        pass
    return {"status": "updated", "settings": settings.model_dump()}


# ─── Browser Settings ─────────────────────────────────────────────────────


class BrowserSettingsRequest(BaseModel):
    headless: bool = False
    slow_mo: int = 50


@router.get("/browser")
async def get_browser_settings():
    """Get current browser configuration."""
    try:
        cfg_mod = importlib.import_module("jobs.auto_applier.config")
        CONFIG = cfg_mod.CONFIG
        return {
            "headless": CONFIG.headless,
            "slow_mo": CONFIG.slow_mo,
            "browser_type": CONFIG.browser_type,
            "viewport_width": CONFIG.viewport_width,
            "viewport_height": CONFIG.viewport_height,
        }
    except ImportError:
        return {"headless": False, "slow_mo": 50, "browser_type": "chromium"}


@router.post("/browser")
async def update_browser_settings(request: BrowserSettingsRequest):
    """Update browser settings (applied at next launch)."""
    try:
        cfg_mod = importlib.import_module("jobs.auto_applier.config")
        CONFIG = cfg_mod.CONFIG
        CONFIG.headless = request.headless
        CONFIG.slow_mo = request.slow_mo
    except ImportError:
        pass
    return {"status": "updated", "headless": request.headless, "slow_mo": request.slow_mo}


# ─── Telegram ─────────────────────────────────────────────────────────────


@router.post("/telegram/start")
async def start_telegram_bot(background_tasks: BackgroundTasks):
    """Start the Telegram bot in polling mode."""
    bot = _get_bot()

    async def _start():
        await bot.start_polling()

    asyncio.create_task(_start())

    return {"status": "started", "message": "Telegram bot starting..."}


@router.post("/telegram/send-test")
async def send_telegram_test():
    """Send a test job card to Telegram."""
    bot = _get_bot()
    sent = await bot.send_job_card(
        job_id="test-001",
        company="Example Corp",
        title="Senior Software Engineer",
        score=85,
        url="https://example.com/jobs/test",
        reason="Test card — no actual job here",
    )
    return {"sent": sent}


# ─── EvoMap ──────────────────────────────────────────────────────────────


@router.get("/failures")
async def get_recent_failures():
    """Get recent failure entries from the EvoMap log."""
    try:
        from jobs.auto_applier.failure.evo_logger import EvoLogger
        evo = EvoLogger()
        return {"failures": evo.get_recent_failures(20)}
    except ImportError:
        return {"failures": [], "error": "EvoLogger unavailable"}


@router.get("/failures/summary")
async def get_failure_summary():
    """Get aggregated failure patterns for analytics and LLM feedback."""
    try:
        from jobs.auto_applier.failure.evo_logger import EvoLogger
        evo = EvoLogger()
        return evo.get_failure_patterns()
    except ImportError:
        return {"error": "EvoLogger unavailable"}


@router.get("/failures/llm-context")
async def get_failure_llm_context():
    """Get failure patterns formatted for LLM prompt injection."""
    try:
        from jobs.auto_applier.failure.evo_logger import EvoLogger
        evo = EvoLogger()
        return {"context": evo.get_llm_failure_context()}
    except ImportError:
        return {"context": ""}
