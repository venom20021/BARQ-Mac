"""
BARQ Python Sidecar - FastAPI Application

This service runs alongside the Electron app and provides:
- Voice control (Vosk wake word + Whisper STT)
- Job search automation (scraping, evaluation, application)
- Social media content pipeline (trends, scripts, video, posting)
- Analytics aggregation
- AI-powered resume parsing, matching, optimization
- Cover letter & cold email generation
- Playwright-based auto-apply
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Ensure the python directory is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Line-buffer stdout so that print() output is immediately visible in log
# files when running in background mode (nohup, Task Scheduler, etc.).
# Without this, Python buffers output when stdout is not a TTY, making
# the log appear to freeze during the startup lifespan.
#
# errors="replace" is critical on Windows: the default console codec is
# cp1252, so print() of any emoji/Unicode (✅, 💬, 🧠, …) raises
# UnicodeEncodeError ('charmap' codec error).  That exception can escape
# into worker threads — e.g. it crashed the vision stream thread and made
# vision_stream_start fail.  Replacing unencodable chars (instead of
# raising) makes every print() across the whole backend console-safe.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(line_buffering=True, errors="replace")

from agent.agent_kernel_routes import (
    kernel_router as agent_kernel_router,
    skill_router as agent_skill_router,
    memory_bus_router as memory_bus_router,
)
from agent.recruitment.routes import router as recruitment_router
from agent.research.routes import router as research_router
from agent.routes import router as agent_router
from agent.workflow_routes import router as workflow_router
from agent.vision_routes import router as vision_router
from knowledge.routes import router as knowledge_router
from analytics.routes import router as analytics_router
from api.routes import router as api_v1_router
from settings.routes import router as settings_router
from auth_routes import router as auth_router
from config import get_settings
from database import analytics_dao, close_db, init_db
from desktop_automation.routes import router as desktop_router
from desktop_automation.clipboard_routes import router as clipboard_router
from documents.routes import router as documents_router
from external_apis.routes import router as external_apis_router
from graph_brain import graph_brain
from jobs.routes import router as jobs_router
from app.services.gemini_routes import router as gemini_router
from memory_knowledge.brain_api import router as brain_api_router
from memory_knowledge.graph_routes import router as graph_router
from memory_knowledge.ingestion_routes import router as ingestion_router
from memory_knowledge.migration_routes import router as migration_router
from memory_knowledge.routes import router as memory_router
from notifications.routes import router as notification_router
from notifications.reminder_routes import router as reminder_router
from social.routes import router as social_router
from system_control.routes import router as system_router
from system_control.hardware_routes import router as hardware_router
from voice.routes import router as voice_router
from web_media.routes import router as web_router

settings = get_settings()
logger = logging.getLogger("barq")

# ─── Background Job Scheduler ────────────────────────────────────────────────

scheduler = None


async def start_scheduler():
    """Start the APScheduler for background tasks."""
    global scheduler
    if not settings.scheduler_enabled:
        print("[Scheduler] Disabled via BARQ_SCHEDULER=false — background jobs will not run")
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = AsyncIOScheduler()

        # Auto-scrape jobs every 6 hours
        scheduler.add_job(
            _auto_scan_jobs,
            IntervalTrigger(hours=settings.job_scan_interval_hours),
            id="auto_scan_jobs",
            replace_existing=True,
        )

        # Auto-match new jobs every hour
        scheduler.add_job(
            _auto_match_jobs,
            IntervalTrigger(hours=1),
            id="auto_match_jobs",
            replace_existing=True,
        )

        # Auto-extract knowledge triplets from new content every 3 hours
        scheduler.add_job(
            _auto_extract_knowledge,
            IntervalTrigger(hours=3),
            id="auto_extract_knowledge",
            replace_existing=True,
        )

        # Proactive check-in every 30 minutes (if user is idle)
        scheduler.add_job(
            _proactive_checkin,
            IntervalTrigger(minutes=30),
            id="proactive_checkin",
            replace_existing=True,
        )

        # W4: Morning briefing (daily at briefing_time; DB settings override env)
        try:
            from settings.briefing import get_briefing_config, upsert_briefing_task
            briefing = await get_briefing_config()
            if briefing["enabled"]:
                _hour, _minute = briefing["time"].split(":")
                scheduler.add_job(
                    _run_morning_briefing,
                    CronTrigger(hour=int(_hour), minute=int(_minute)),
                    id="morning_briefing",
                    replace_existing=True,
                )
                logger.info(f"[Scheduler] Morning briefing scheduled at {briefing['time']}")
            # Register/refresh the scheduled_tasks row (Settings UI source of truth)
            await upsert_briefing_task(briefing["enabled"], briefing["time"])
            logger.info(
                f"[Scheduler] Morning briefing registered in scheduled_tasks (enabled={briefing['enabled']})"
            )
        except Exception as e:
            logger.warning(f"[Scheduler] Morning briefing not scheduled: {e}")

        # W11: Weekly review (weekly on weekly_review_day at weekly_review_time)
        if settings.weekly_review_enabled:
            try:
                _rh, _rm = settings.weekly_review_time.split(":")
                scheduler.add_job(
                    _run_weekly_review,
                    CronTrigger(day_of_week=settings.weekly_review_day, hour=int(_rh), minute=int(_rm)),
                    id="weekly_review",
                    replace_existing=True,
                )
                logger.info(
                    f"[Scheduler] Weekly review scheduled {settings.weekly_review_day} {settings.weekly_review_time}"
                )
            except Exception as e:
                logger.warning(f"[Scheduler] Weekly review not scheduled: {e}")

        # Periodic knowledge-graph re-import (notes / memory / jobs → brains)
        try:
            from settings.brain_sync import get_brain_sync_config, upsert_brain_sync_task
            brain_sync = await get_brain_sync_config()
            if brain_sync["enabled"]:
                scheduler.add_job(
                    _run_brain_reimport,
                    IntervalTrigger(hours=brain_sync["interval_hours"]),
                    id="brain_reimport",
                    replace_existing=True,
                )
                logger.info(
                    f"[Scheduler] Knowledge graph re-import scheduled every {brain_sync['interval_hours']}h"
                )
            # Register/refresh the scheduled_tasks row (Settings UI source of truth)
            await upsert_brain_sync_task(brain_sync["enabled"], brain_sync["interval_hours"])
            logger.info(
                f"[Scheduler] Knowledge graph re-import registered in scheduled_tasks (enabled={brain_sync['enabled']})"
            )
        except Exception as e:
            logger.warning(f"[Scheduler] Knowledge graph re-import not scheduled: {e}")

        # Background topic monitoring every 6 hours
        scheduler.add_job(
            _background_monitor_check,
            IntervalTrigger(hours=6),
            id="background_monitor_check",
            replace_existing=True,
        )

        scheduler.start()
        logger.info(f"[Scheduler] Started with {len(scheduler.get_jobs())} jobs")
    except ImportError:
        logger.warning("[Scheduler] APScheduler not installed — background tasks disabled")
    except Exception as e:
        logger.error(f"[Scheduler] Failed to start: {e}")


async def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("[Scheduler] Stopped")


async def _auto_scan_jobs():
    """Auto-scan for new jobs (called by scheduler)."""
    try:
        from database import analytics_dao, jobs_dao
        from jobs import JobScanner

        scanner = JobScanner()
        jobs = await scanner.scan_all(
            keywords=["software engineer", "developer", "full stack"],
            location="remote",
        )
        count = 0
        for job in jobs[:50]:
            # Insert the listing — already-known jobs are skipped entirely so
            # they never get re-counted, re-evaluated, or re-notified.
            listing_id = await jobs_dao.insert_job_listing_if_new(job)
            if listing_id is None:
                continue
            # Insert evaluation if scanner already evaluated the job
            if "overall_score" in job:
                try:
                    await jobs_dao.insert_evaluation({
                        "job_listing_id": listing_id,
                        "overall_score": float(job.get("overall_score", 3.0)),
                        "match_percentage": float(job.get("match_percentage", 0)),
                        "reasoning": job.get("reasoning", ""),
                        "pros": json.dumps(job.get("pros", [])),
                        "cons": json.dumps(job.get("cons", [])),
                        "evaluated_by": "scanner",
                    })
                except Exception as eval_err:
                    logger.warning(f"[AutoScan] Failed to insert evaluation: {eval_err}")
            count += 1

        await analytics_dao.log_activity(
            "job", "auto_scan", f"Auto-scanned {count} new jobs"
        )
        logger.info(f"[AutoScan] Found {count} new jobs")

        # ── Auto-trigger pipeline to process high-match jobs ──────────
        if count > 0:
            try:
                from jobs.pipeline import run_pipeline
                pipeline_result = await run_pipeline({
                    "mode": "notify",
                    "max_per_run": 10,
                    "min_match_score": 60,
                    "generate_pdf": False,  # Skip PDF gen in auto-mode (speed)
                    "send_telegram": True,
                })
                logger.info(f"[AutoScan] Pipeline processed {pipeline_result.get('succeeded', 0)} jobs")
            except Exception as pipe_err:
                logger.warning(f"[AutoScan] Pipeline trigger failed: {pipe_err}")

    except Exception as e:
        logger.error(f"[AutoScan] Failed: {e}")


async def _auto_match_jobs():
    """Auto-match new jobs against resume (called by scheduler)."""
    try:
        from database import analytics_dao, jobs_dao
        from jobs.matcher import JobMatcher
        from jobs.resume_parser import parse_resume

        resume = parse_resume()
        if resume.get("_error"):
            return

        matcher = JobMatcher()
        jobs = await jobs_dao.get_active_jobs(limit=50)

        matched = 0
        for job in jobs:
            existing = await jobs_dao.get_evaluation(job["id"])
            if existing:
                continue

            result = await matcher.match(job, resume)
            await jobs_dao.insert_evaluation({
                "job_listing_id": job["id"],
                "overall_score": result["overall_score"] / 20,
                "match_percentage": result["overall_score"],
                "reasoning": result["fit_summary"],
                "pros": json.dumps(result["matching_skills"]),
                "cons": json.dumps(result["missing_skills"]),
                "evaluated_by": "llm",
            })
            matched += 1

        await analytics_dao.log_activity(
            "job", "auto_match", f"Auto-matched {matched} jobs"
        )
        logger.info(f"[AutoMatch] Matched {matched} jobs")
    except Exception as e:
        logger.error(f"[AutoMatch] Failed: {e}")


# ─── Lifespan ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    # Startup — wrap database init with a timeout so the health endpoint
    # becomes available even if Turso cloud is slow or unreachable.
    print(f"[BARQ Sidecar] Starting on {settings.host}:{settings.port}")

    # Capture the MAIN event loop for the voice pipeline.  The wake-word flow
    # runs the conversation on a separate managed loop (Vosk thread), but the
    # shared aiosqlite connection lives here — so voice coroutines that touch
    # the DB (persist_voice_utterance, background info) marshal onto this loop
    # via voice.loop_utils to avoid "Future attached to a different loop".
    try:
        from voice.loop_utils import set_main_loop
        set_main_loop(asyncio.get_running_loop())
        print("[BARQ Sidecar] Main event loop captured for voice pipeline")
    except Exception as _loop_err:
        print(f"[BARQ Sidecar] [WARN] Main loop capture failed (non-fatal): {_loop_err}")

    try:
        await asyncio.wait_for(init_db(), timeout=15.0)
    except asyncio.TimeoutError:
        print("[BARQ Sidecar] [WARN] Database init timed out after 15s (Turso may be unreachable)")
        print("[BARQ Sidecar] [WARN] Starting without database — some features will be degraded")
    except Exception as e:
        print(f"[BARQ Sidecar] [WARN] Database init failed: {e}")
        print("[BARQ Sidecar] [WARN] Starting without database — some features will be degraded")

    # Check LLM availability (Ollama + cloud fallback)
    try:
        from utils.ollama_client import OllamaClient, CloudLLMClient

        _ollama_check = OllamaClient()
        _cloud = CloudLLMClient()

        if await _ollama_check.is_available():
            print(f"[BARQ Sidecar] [OK] Ollama '{settings.ollama_model}' ready at {settings.ollama_host}")
        else:
            # Socket check to give a friendly diagnostic message
            import socket as _socket
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _s.settimeout(1)
            try:
                _s.connect(("127.0.0.1", 11434))
                _s.close()
                print(f"[BARQ Sidecar] [WARN] Ollama is running but model '{settings.ollama_model}' is not pulled yet")
                print(f"[BARQ Sidecar]   >> Run: ollama pull {settings.ollama_model}")
            except Exception:
                print("[BARQ Sidecar] [WARN] Ollama is NOT running")
                print("[BARQ Sidecar]   >> Install from: https://ollama.com/download/windows")
            finally:
                try:
                    _s.close()
                except Exception:
                    pass

            # Check cloud fallback
            if _cloud.enabled:
                print(f"[BARQ Sidecar] [OK] Cloud LLM fallback ready ({_cloud.model} at {_cloud.base_url})")
            else:
                print("[BARQ Sidecar] [WARN] No cloud LLM fallback -- set OPENAI_API_KEY in .env to enable")
                print("[BARQ Sidecar]   >> Get a key at: https://platform.openai.com/api-keys")
    except Exception:
        pass  # don't crash on startup diagnostics

    # Load knowledge graph from disk if it exists
    import os as _os
    _graph_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "graph.json")
    graph_brain.load_from_disk(_graph_path)

    # Load multi-brain domain-specific graphs from disk
    from memory_knowledge.multi_brain import multi_brain_manager as _mbm
    _brains_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "brains")
    _mbm.set_data_dir(_brains_dir)
    _loaded = _mbm.load_all()
    _loaded_count = sum(1 for v in _loaded.values() if v)
    print(f"[BARQ Sidecar] Loaded {_loaded_count}/{len(_loaded)} domain-specific brains from {_brains_dir}")

    # Auto-populate the knowledge graph on first run (all brains empty) so the
    # Brain / Knowledge Graph page is never blank: seeds a demo graph and
    # imports real notes/memory/jobs data into the multi-brain graphs.
    try:
        from memory_knowledge.brain_api import ensure_populated
        _pop = await ensure_populated()
        print(f"[BARQ Sidecar] Knowledge graph population: {_pop.get('status')}")
    except Exception as _e:
        print(f"[BARQ Sidecar] Knowledge graph auto-populate skipped: {_e}")

    try:
        await analytics_dao.log_activity(
            "system", "startup", f"BARQ Sidecar v2.0 started on {settings.host}:{settings.port}",
            severity="info"
        )
    except Exception:
        pass  # DB might not be ready yet
    await start_scheduler()
    # Start the agent task queue
    try:
        from agent.task_queue import get_task_queue
        queue = get_task_queue()
        await queue.start()
        print("[BARQ Sidecar] Agent task queue started")
    except Exception as e:
        print(f"[BARQ Sidecar] Agent task queue start error: {e}")

    # Register agentic workflow skills (idempotent) so the planner can use them
    try:
        from agent.agentic_skills import register_agentic_skills
        register_agentic_skills()
        print("[BARQ Sidecar] Agentic workflow skills registered")
    except Exception as e:
        print(f"[BARQ Sidecar] Agentic skills registration error: {e}")

    # Start the AgentKernel (central mediation for all LLM calls)
    try:
        from agent.agent_kernel import get_agent_kernel
        kernel = get_agent_kernel()
        kernel.start()
        print("[BARQ Sidecar] AgentKernel started")
    except Exception as e:
        print(f"[BARQ Sidecar] AgentKernel start error: {e}")

    # Start the MemoryBus (unified memory with FTS5)
    try:
        from memory.memory_bus import get_memory_bus
        bus = get_memory_bus()
        bus.start()
        # Migrate any legacy long-term memory
        bus.load_legacy_memory()
        print("[BARQ Sidecar] MemoryBus started (legacy migration done)")
    except Exception as e:
        print(f"[BARQ Sidecar] MemoryBus start error: {e}")

    # Start the reminder background check
    try:
        from notifications.reminders import reminder_manager
        await reminder_manager.start_background_check(interval_seconds=30)
        print("[BARQ Sidecar] Reminder background check started")
    except Exception as e:
        print(f"[BARQ Sidecar] Reminder check start error: {e}")

    # Start the ingestion drop-folder watcher
    _ingestion_monitor = None
    try:
        from memory_knowledge.ingestion import DropFolderMonitor
        from memory_knowledge.ingestion_routes import set_monitor as _set_ingestion_monitor

        _ingestion_monitor = DropFolderMonitor()
        _ingestion_monitor.process_all_existing()
        _ingestion_monitor.start()
        _set_ingestion_monitor(_ingestion_monitor)
        print("[BARQ Sidecar] Ingestion watcher started")
    except Exception as e:
        print(f"[BARQ Sidecar] Ingestion watcher start error: {e}")

    # Start the Gemini file watcher (background ingestion of Gemini chat history)
    _gemini_watcher_inst = None
    try:
        from app.services.gemini_watcher import GeminiFileWatcher
        from app.services.gemini_routes import set_monitor as _set_gemini_monitor

        _gemini_watcher_inst = GeminiFileWatcher()
        _gemini_watcher_inst.process_all_existing()
        _gemini_watcher_inst.start()
        _set_gemini_monitor(_gemini_watcher_inst)
        print("[BARQ Sidecar] Gemini file watcher started")
    except Exception as e:
        print(f"[BARQ Sidecar] Gemini file watcher start error: {e}")

    # Preload faster-whisper model in background so first transcription
    # does not block the event loop.  Uses ``run_in_executor`` internally
    # to keep the health endpoint responsive even during model download.
    try:
        from voice.routes import _preload_whisper_model
        asyncio.ensure_future(_preload_whisper_model())
    except Exception:
        pass

    # ── Silent Language Persistence ──
    # Load saved voice settings (including language) from DB so the user's
    # language preference is silently restored across restarts — no manual
    # re-selection needed (Mark-L's "Silent Language Memory" feature).
    try:
        from voice.routes import load_sound_settings
        await load_sound_settings()
        print("[BARQ Sidecar] Voice settings loaded from DB (silent language persistence)")
    except Exception as e:
        print(f"[BARQ Sidecar] Voice settings load skipped (non-fatal): {e}")

    # ── Start Second Brain Auto-Sync ──────────────────────────────────────
    try:
        from memory_knowledge.second_brain_sync import auto_sync_scheduler
        from config import get_settings as _cfg_settings
        _sb_settings = _cfg_settings()
        if _sb_settings.second_brain_enabled:
            await auto_sync_scheduler.start()
            print(f"[BARQ Sidecar] Second Brain auto-sync started (interval: {auto_sync_scheduler._interval_seconds}s)")
        else:
            print("[BARQ Sidecar] Second Brain auto-sync skipped (disabled)")
    except Exception as e:
        print(f"[BARQ Sidecar] Second Brain auto-sync start skipped (non-fatal): {e}")

    # ── Start Telegram Ingestion Bot ─────────────────────────────────────
    _telegram_app = None
    # If BARQ_SKIP_TELEGRAM=1 or true, skip the bot (local dev instances should
    # set this env var so they don't conflict with the VM's polling bot).
    _skip_telegram = os.getenv("BARQ_SKIP_TELEGRAM", "").lower() in ("1", "true")
    if _skip_telegram:
        print("[BARQ Sidecar] [INFO] Telegram bot skipped locally (BARQ_SKIP_TELEGRAM is set)")
    elif settings.telegram_bot_token:
        try:
            from telegram import Update as _TGUpdate
            from telegram.error import Conflict as _TGConflict
            from telegram.ext import (
                Application as _TGApplication,
                CommandHandler as _TGCommandHandler,
                MessageHandler as _TGMessageHandler,
                filters as _TGFilters,
            )

            from telegram_ingestion_bot import (
                cmd_start as _tg_cmd_start,
                cmd_help as _tg_cmd_help,
                handle_message as _tg_handle_message,
                handle_pdf_document as _tg_handle_pdf,
                error_handler as _tg_error_handler,
            )

            _telegram_app = (
                _TGApplication.builder()
                .token(settings.telegram_bot_token)
                .concurrent_updates(True)
                .read_timeout(30)
                .write_timeout(30)
                .build()
            )

            _telegram_app.add_handler(_TGCommandHandler("start", _tg_cmd_start))
            _telegram_app.add_handler(_TGCommandHandler("help", _tg_cmd_help))
            _telegram_app.add_handler(_TGMessageHandler(_TGFilters.TEXT & ~_TGFilters.COMMAND, _tg_handle_message))
            _telegram_app.add_handler(_TGMessageHandler(_TGFilters.Document.PDF, _tg_handle_pdf))
            _telegram_app.add_error_handler(_tg_error_handler)

            # Non-blocking start: initialize + start polling in background
            await _telegram_app.initialize()
            await _telegram_app.start()
            await _telegram_app.updater.start_polling(allowed_updates=_TGUpdate.ALL_TYPES)

            print("[BARQ Sidecar] Telegram ingestion bot started")
        except ImportError as _tg_ie:
            print(f"[BARQ Sidecar] [WARN] Telegram bot unavailable: {_tg_ie}")
            print("[BARQ Sidecar]   >> Run: pip install python-telegram-bot")
            _telegram_app = None
        except _TGConflict:
            print("[BARQ Sidecar] [WARN] Telegram bot Conflict — another instance is already polling, skipping local bot")
            _telegram_app = None
        except Exception as _tg_e:
            print(f"[BARQ Sidecar] [WARN] Telegram bot failed to start: {_tg_e}")
            _telegram_app = None
    else:
        print("[BARQ Sidecar] [WARN] Telegram bot not configured -- set TELEGRAM_BOT_TOKEN in .env")

    print("[BARQ Sidecar] Ready for requests")
    yield
    # Shutdown
    # Stop Telegram ingestion bot
    if _telegram_app is not None:
        try:
            await _telegram_app.updater.stop()
            await _telegram_app.stop()
            await _telegram_app.shutdown()
            print("[BARQ Sidecar] Telegram ingestion bot stopped")
        except Exception as _tg_se:
            print(f"[BARQ Sidecar] [WARN] Telegram bot shutdown error: {_tg_se}")

    # Stop Second Brain auto-sync
    try:
        from memory_knowledge.second_brain_sync import auto_sync_scheduler
        await auto_sync_scheduler.stop()
        print("[BARQ Sidecar] Second Brain auto-sync stopped")
    except Exception:
        pass

    # Stop the ingestion watcher
    if _ingestion_monitor is not None:
        try:
            _ingestion_monitor.stop()
            print("[BARQ Sidecar] Ingestion watcher stopped")
        except Exception:
            pass
    # Stop the Gemini file watcher
    if _gemini_watcher_inst is not None:
        try:
            _gemini_watcher_inst.close()
            print("[BARQ Sidecar] Gemini file watcher stopped")
        except Exception:
            pass
    await stop_scheduler()
    # Stop the agent task queue
    try:
        from agent.task_queue import get_task_queue
        queue = get_task_queue()
        await queue.stop()
        print("[BARQ Sidecar] Agent task queue stopped")
    except Exception as e:
        print(f"[BARQ Sidecar] Agent task queue stop error: {e}")

    # Stop the AgentKernel
    try:
        from agent.agent_kernel import get_agent_kernel
        await get_agent_kernel().stop()
        print("[BARQ Sidecar] AgentKernel stopped")
    except Exception as e:
        print(f"[BARQ Sidecar] AgentKernel stop error: {e}")

    # Stop the MemoryBus
    try:
        from memory.memory_bus import get_memory_bus
        await get_memory_bus().stop()
        print("[BARQ Sidecar] MemoryBus stopped")
    except Exception as e:
        print(f"[BARQ Sidecar] MemoryBus stop error: {e}")

    try:
        await analytics_dao.log_activity(
            "system", "shutdown", "BARQ Sidecar shutting down",
            severity="info"
        )
    except Exception:
        pass
    await close_db()
    print("[BARQ Sidecar] Shutting down")


# ─── App Creation ────────────────────────────────────────────────────────────

app = FastAPI(
    title="BARQ Sidecar API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)

# CORS - allow local origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(voice_router, prefix="/voice", tags=["Voice"])
app.include_router(jobs_router, prefix="/jobs", tags=["Jobs"])
app.include_router(social_router, prefix="/social", tags=["Social"])
app.include_router(analytics_router, prefix="/analytics", tags=["Analytics"])
app.include_router(notification_router, prefix="/notifications", tags=["Notifications"])
app.include_router(reminder_router, prefix="/notifications", tags=["Notifications"])
app.include_router(system_router, prefix="/system", tags=["System Control"])
app.include_router(hardware_router, prefix="/system", tags=["System Control"])
app.include_router(memory_router, prefix="/memory", tags=["Memory & Knowledge"])
app.include_router(web_router, prefix="/web", tags=["Web & Media"])
app.include_router(documents_router, prefix="/documents", tags=["Document Generation"])
app.include_router(desktop_router, prefix="/desktop", tags=["Desktop Automation"])
app.include_router(clipboard_router, prefix="/desktop", tags=["Desktop Automation"])
app.include_router(graph_router, prefix="/graph", tags=["Graph Brain"])
app.include_router(brain_api_router, tags=["Brain Visualisation"])

# Second Brain Integration (BARQ ↔ Second Brain bidirectional sync)
try:
    from memory_knowledge.second_brain_routes import router as second_brain_router
    app.include_router(second_brain_router, tags=["Second Brain Integration"])
except ImportError as e:
    print(f"[Main] Second Brain integration routes skipped (import error): {e}")

app.include_router(auth_router, tags=["Auth"])
app.include_router(api_v1_router, tags=["Jobs v1"])  # Already has /api/v1 prefix
app.include_router(agent_router, prefix="/agent", tags=["Agent System"])
app.include_router(workflow_router, prefix="/agent", tags=["Agentic Workflows"])
app.include_router(vision_router, prefix="/vision", tags=["Visual Awareness"])
app.include_router(recruitment_router, prefix="/recruitment", tags=["Recruitment Agents"])
app.include_router(research_router, prefix="/research", tags=["Deep Research Agent"])
app.include_router(knowledge_router, prefix="/knowledge", tags=["Knowledge Management"])
app.include_router(ingestion_router, tags=["Ingestion Pipeline"])
app.include_router(migration_router, tags=["Graph Migration"])
app.include_router(gemini_router, tags=["Gemini File Watcher"])
app.include_router(settings_router, tags=["Settings"])
app.include_router(external_apis_router, tags=["Free Public APIs"])

# Register Agent Kernel & Analytics routers
app.include_router(agent_kernel_router)
app.include_router(agent_skill_router)
app.include_router(memory_bus_router)

# Register auto-apply router (DynamicResumeBuilder, pipeline, etc.)
try:
    from jobs.auto_applier.routes import router as auto_apply_router  # noqa: E402
    app.include_router(auto_apply_router, prefix="/api/jobs", tags=["Auto Apply"])
except ImportError as e:
    print(f"[Main] Auto-apply routes skipped (missing dependency): {e}")


@app.get("/health")
async def health():
    """Health check endpoint for the Electron main process."""
    return {
        "status": "ok",
        "service": "barq-sidecar",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/health")
async def api_health():
    """API v1 health check."""
    return {
        "status": "ok",
        "service": "barq-jobs",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/shutdown")
async def shutdown():
    """Graceful shutdown endpoint."""
    print("[BARQ Sidecar] Shutdown requested via API")
    await stop_scheduler()
    await close_db()
    os._exit(0)


# ─── Scheduler Configuration Endpoints ─────────────────────────────────────


@app.get("/scheduler/tasks")
async def get_scheduled_tasks():
    """Get all scheduled tasks from the database."""
    try:
        from database import db_connection
        tasks = await db_connection.fetch_all(
            "SELECT id, task_type, name, config, cron_expression, enabled, "
            "last_run, next_run, total_runs, last_status, created_at "
            "FROM scheduled_tasks ORDER BY task_type"
        )
        return {"tasks": tasks}
    except Exception as e:
        return {"tasks": [], "error": str(e)}


@app.post("/scheduler/tasks")
async def create_scheduled_task(data: dict):
    """Create a new scheduled task."""
    try:
        import json

        from database import db_connection
        task_id = await db_connection.insert(
            "INSERT INTO scheduled_tasks (task_type, name, config, cron_expression, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                data.get("task_type", "custom"),
                data.get("name", "New Task"),
                json.dumps(data.get("config", {})),
                data.get("cron_expression", "0 */6 * * *"),
                1 if data.get("enabled", True) else 0,
            ),
        )
        return {"status": "created", "id": task_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/scheduler/tasks/{task_id}/toggle")
async def toggle_scheduled_task(task_id: int):
    """Enable or disable a scheduled task."""
    try:
        from database import db_connection
        task = await db_connection.fetch_one(
            "SELECT enabled FROM scheduled_tasks WHERE id = ?", (task_id,)
        )
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        new_enabled = 0 if task["enabled"] else 1
        await db_connection.update(
            "UPDATE scheduled_tasks SET enabled = ? WHERE id = ?",
            (new_enabled, task_id),
        )
        return {"status": "toggled", "enabled": bool(new_enabled)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scheduler/status")
async def scheduler_status():
    """Get the current status of the APScheduler."""
    global scheduler
    if scheduler is None:
        return {"running": False, "jobs": []}
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return {"running": True, "jobs": jobs}


async def _proactive_checkin():
    """Proactive check-in — BARQ initiates a conversation if user is idle.

    Called by APScheduler every 30 minutes. Checks if the user has been
    silent and if enough time has passed since the last check-in.
    If conditions are met, generates a context-aware message.
    """
    try:
        from actions.proactive_engine import get_engine
        engine = get_engine()
        checkin = await engine.scheduled_checkin()
        if checkin:
            logger.info(f"[Proactive] Check-in generated: {checkin[:80]}")
            # If there's an active conversation, inject the check-in
            try:
                from voice.routes import responder, conversation_listener
                if responder and conversation_listener and conversation_listener.is_active:
                    # Speak the check-in to the user
                    logger.info("[Proactive] Speaking check-in during active conversation")
            except Exception:
                pass
        else:
            logger.info("[Proactive] No check-in needed (user active or cooldown)")
    except Exception as e:
        logger.error(f"[Proactive] Check-in error: {e}")


async def _background_monitor_check():
    """Check all monitored topics for new headlines.

    Called by APScheduler every 6 hours. Uses DuckDuckGo's free API.
    Alerts are dispatched via the notification system.
    """
    try:
        from actions.background_monitor import scheduled_check
        alerts = await scheduled_check()
        if alerts:
            logger.info(f"[BackgroundMonitor] {len(alerts)} new headline(s) found")
        else:
            logger.info("[BackgroundMonitor] No new headlines")
    except Exception as e:
        logger.error(f"[BackgroundMonitor] Error: {e}")


async def _run_morning_briefing():
    """W4 — Morning briefing (called by APScheduler). Best-effort only."""
    try:
        from agent.workflows.morning_briefing import run_morning_briefing
        result = await run_morning_briefing(notify=True)
        logger.info(f"[Briefing] Generated {len(result.get('briefing', ''))} chars")
    except Exception as e:
        logger.error(f"[Briefing] Failed: {e}")


async def _run_weekly_review():
    """W11 — Weekly review (called by APScheduler). Best-effort only."""
    try:
        from agent.workflows.weekly_review import run_weekly_review
        result = await run_weekly_review(notify=True)
        logger.info(f"[WeeklyReview] Generated {len(result.get('report', ''))} chars")
    except Exception as e:
        logger.error(f"[WeeklyReview] Failed: {e}")


async def _run_brain_reimport():
    """Periodic knowledge-graph re-import (called by APScheduler). Best-effort only."""
    try:
        from memory_knowledge.brain_api import run_brain_reimport
        result = await run_brain_reimport()
        direct = result.get("results", {}).get("direct_triplets", {})
        logger.info(
            "[BrainReimport] Finished — general=%s career=%s llm_notes=%s",
            direct.get("general", 0),
            direct.get("career", 0),
            result.get("results", {}).get("notes_llm_triplets", 0),
        )
    except Exception as e:
        logger.error(f"[BrainReimport] Failed: {e}")


async def _auto_extract_knowledge():
    """Auto-extract knowledge triplets from unprocessed jobs/social (called by scheduler)."""
    try:
        from knowledge.auto_extractor import AutoExtractor

        extractor = AutoExtractor()
        result = await extractor.run_full_extraction()
        logger.info(
            "[AutoExtract] Extracted %d triplets (%d from jobs, %d from trends)",
            result["total_triplets"],
            result["job_triplets"],
            result["trend_triplets"],
        )
    except Exception as e:
        logger.error("[AutoExtract] Failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# Background Monitor API
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/monitor/topics")
async def list_monitor_topics():
    """List all monitored topics."""
    try:
        from actions.background_monitor import list_monitors
        topics = list_monitors()
        return {"topics": topics, "count": len(topics)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/monitor/topics")
async def add_monitor_topic(data: dict):
    """Add a topic to monitor."""
    try:
        topic = data.get("topic", "")
        if not topic:
            raise HTTPException(status_code=400, detail="topic is required")
        from actions.background_monitor import add_monitor
        result = add_monitor(topic)
        return {"status": "ok", "result": result, "topic": topic}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/monitor/topics")
async def remove_monitor_topic(data: dict):
    """Remove a monitored topic."""
    try:
        topic = data.get("topic", "")
        if not topic:
            raise HTTPException(status_code=400, detail="topic is required")
        from actions.background_monitor import remove_monitor
        result = remove_monitor(topic)
        return {"status": "ok", "result": result, "topic": topic}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/monitor/check")
async def check_monitors_now():
    """Trigger an immediate check of all monitored topics."""
    try:
        from actions.background_monitor import scheduled_check
        alerts = await scheduled_check()
        return {"status": "completed", "alerts_count": len(alerts), "alerts": alerts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# Proactive Engine API
# ═══════════════════════════════════════════════════════════════════════════════


@app.post("/proactive/trigger")
async def trigger_proactive_checkin():
    """Manually trigger a proactive check-in."""
    try:
        from actions.proactive_engine import get_engine
        engine = get_engine()
        engine.mark_triggered()  # Reset cooldown so next should_trigger() passes
        checkin = await engine.scheduled_checkin()
        return {"status": "completed", "checkin": checkin}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/proactive/status")
async def proactive_status():
    """Get the proactive engine status."""
    try:
        from actions.proactive_engine import get_engine
        engine = get_engine()
        should = engine.should_trigger()
        return {
            "should_trigger": should,
            "min_silence_secs": engine.min_silence_secs,
            "check_cooldown": engine.check_cooldown,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/scheduler/run/{task_type}")
async def run_task_manual(task_type: str):
    """Manually trigger a scheduled task."""
    try:
        if task_type == "job_scan":
            await _auto_scan_jobs()
            return {"status": "completed", "task": task_type}
        elif task_type == "job_match":
            await _auto_match_jobs()
            return {"status": "completed", "task": task_type}
        elif task_type == "knowledge_extract":
            await _auto_extract_knowledge()
            return {"status": "completed", "task": task_type}
        else:
            return {"status": "unknown_task", "task": task_type}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO if settings.debug else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info" if settings.debug else "warning",
    )
