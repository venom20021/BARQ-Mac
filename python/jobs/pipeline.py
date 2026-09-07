"""
BARQ Job Application Pipeline — end-to-end pipeline orchestrator.

Connects: Scanner → Matcher → Resume Optimizer → Cover Letter Generator
→ PDF Generator → Application Documents → Auto-Apply OR Telegram Notification

The pipeline processes approved/queued applications by:
1. Parsing the user's resume
2. Optimizing it for each specific job description
3. Generating a tailored cover letter
4. Generating PDF documents
5. Either auto-applying via Playwright OR sending a Telegram notification
   with the job link, optimized resume, and cover letter summary
"""

import asyncio
import html as html_mod
import json
import logging
import os

import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("barq.pipeline")

from config import get_settings  # noqa: E402
from database import analytics_dao, jobs_dao  # noqa: E402
from knowledge.auto_extractor import AutoExtractor  # noqa: E402
from utils import safe_filename  # noqa: E402

# Lazy-load CONFIG for Telegram settings
CONFIG = None

from .cover_letter import CoverLetterGenerator  # noqa: E402
from .optimizer import ResumeOptimizer  # noqa: E402
from .pdf_generator import (  # noqa: E402
    ResumePDFGenerator,
    GENERATED_DIR,
    compile_resume_pdf_from_json,
    generate_cover_letter_pdf,
)
from .resume_parser import DEFAULT_RESUME_PATH, parse_resume  # noqa: E402

# ─── Pipeline Settings ──────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "mode": "notify",           # "notify" → Telegram, "auto_apply" → Playwright submit
    "auto_apply": False,        # Whether to actually submit forms (requires Playwright)
    "max_per_run": 10,          # Max jobs to process per pipeline run
    "generate_pdf": True,       # Generate PDF copies of resume and cover letter
    "send_telegram": True,      # Send Telegram notification with job link + docs
    "min_match_score": 60,      # Minimum match percentage to process
    "use_dynamic_resume": True,  # Use DynamicResumeBuilder (3-tier fallback) for tailored resumes
    # Retry: auto-retry failed jobs on next run (max retries)
    "max_retries": 3,
    # Evaluator-Optimizer gate (Reflection pattern) — evaluates generated
    # resumes/cover letters against the JD and forces revisions below threshold.
    "enable_evaluator": True,
    "evaluator_threshold": 80,  # 0-100 match score required before PDF/Telegram
    "evaluator_max_iterations": 2,
}

# ─── Pipeline State ─────────────────────────────────────────────────────────

_pipeline_state: dict[str, Any] = {
    "status": "idle",           # idle | running | paused | complete | error
    "phase": "",
    "phase_index": 0,
    "total_phases": 6,
    "progress_pct": 0,
    "jobs_total": 0,
    "jobs_processed": 0,
    "jobs_succeeded": 0,
    "jobs_failed": 0,
    "current_job": "",
    "message": "",
    "started_at": None,
    "elapsed_seconds": 0,
    "results": [],
}

PHASES = [
    "Loading user resume",
    "Fetching approved jobs",
    "Optimizing resumes",
    "Generating cover letters",
    "Generating application documents",
    "Notifying & applying",
]


def get_pipeline_settings() -> dict[str, Any]:
    """Get current pipeline settings (cached in pipeline_state)."""
    return dict(DEFAULT_SETTINGS)


def get_pipeline_progress() -> dict[str, Any]:
    """Return a snapshot of pipeline progress."""
    p = _pipeline_state
    if p["started_at"]:
        p["elapsed_seconds"] = round(time.time() - p["started_at"], 1)
    return dict(p)


def reset_pipeline_state():
    """Reset pipeline state to idle."""
    for key in ("status", "phase", "current_job", "message"):
        _pipeline_state[key] = "" if key in ("phase", "current_job", "message") else "idle" if key == "status" else 0
    _pipeline_state["phase_index"] = 0
    _pipeline_state["progress_pct"] = 0
    _pipeline_state["jobs_total"] = 0
    _pipeline_state["jobs_processed"] = 0
    _pipeline_state["jobs_succeeded"] = 0
    _pipeline_state["jobs_failed"] = 0
    _pipeline_state["started_at"] = None
    _pipeline_state["elapsed_seconds"] = 0
    _pipeline_state["results"] = []


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline Runner
# ═══════════════════════════════════════════════════════════════════════════


async def run_pipeline(settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """
    Execute the full job application pipeline.

    Args:
        settings: Override DEFAULT_SETTINGS values

    Returns:
        Summary dict with status, processed count, and results
    """
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}

    reset_pipeline_state()
    _pipeline_state["status"] = "running"
    _pipeline_state["started_at"] = time.time()
    results: list[dict[str, Any]] = []

    try:
        # ── Phase 1: Load Resume ──────────────────────────────────────
        _pipeline_state["phase"] = PHASES[0]
        _pipeline_state["phase_index"] = 0
        _pipeline_state["progress_pct"] = 5
        _pipeline_state["message"] = "Parsing user resume..."
        await asyncio.sleep(0.2)

        resume = parse_resume()
        if resume.get("_error") or not resume.get("raw_md"):
            _pipeline_state["status"] = "error"
            _pipeline_state["message"] = f"Resume not found. Expected at: {DEFAULT_RESUME_PATH}"
            return {"status": "error", "message": _pipeline_state["message"], "results": []}

        resume_md = resume["raw_md"]
        print(f"[Pipeline] Resume loaded: {resume.get('full_name', 'Unknown')} ({len(resume_md)} chars)")

        # ── Phase 2: Fetch Approved Jobs ──────────────────────────────
        _pipeline_state["phase"] = PHASES[1]
        _pipeline_state["phase_index"] = 1
        _pipeline_state["progress_pct"] = 10
        _pipeline_state["message"] = "Fetching approved jobs from database..."
        await asyncio.sleep(0.2)

        # Get jobs that are approved/queued AND have good match scores.
        # exclude_notified skips applications that were already notified so
        # already-known high-match jobs don't re-trigger Telegram alerts.
        queued = await jobs_dao.get_applications_by_status(
            "queued", limit=cfg["max_per_run"], exclude_notified=True
        )
        approved = await jobs_dao.get_applications_by_status(
            "approved", limit=cfg["max_per_run"], exclude_notified=True
        )

        # Also get ready_for_review
        review = await jobs_dao.get_applications_by_status(
            "ready_for_review", limit=cfg["max_per_run"], exclude_notified=True
        )

        # Also get failed jobs that are retryable (retry_count < max_retries)
        max_retries = cfg.get("max_retries", 3)
        retryable = await jobs_dao.get_applications_by_status(
            "failed", limit=cfg["max_per_run"], exclude_notified=True
        )
        # Filter to only retryable jobs
        retryable = [
            app for app in retryable
            if (app.get("retry_count") or 0) < max_retries
        ]
        if retryable:
            print(f"[Pipeline] Found {len(retryable)} retryable failed jobs (retry_count < {max_retries})")

        all_apps = queued + approved + review + retryable

        # Deduplicate by job_listing_id
        seen_ids = set()
        unique_apps = []
        for app in all_apps:
            jid = app["job_listing_id"]
            if jid not in seen_ids:
                seen_ids.add(jid)
                unique_apps.append(app)

        # Filter by match score threshold
        filtered_apps = []
        for app in unique_apps:
            # Get the job listing with its evaluation
            job = await jobs_dao.get_job_listing(app["job_listing_id"])
            if not job:
                continue
            eval_data = await jobs_dao.get_evaluation(app["job_listing_id"])
            match_pct = eval_data.get("match_percentage", 0) if eval_data else 0
            if match_pct >= cfg["min_match_score"] or match_pct == 0:
                # Include even unscored jobs for review
                app["job"] = job
                app["evaluation"] = eval_data or {}
                app["match_percentage"] = match_pct
                filtered_apps.append(app)

        if not filtered_apps:
            _pipeline_state["status"] = "complete"
            _pipeline_state["progress_pct"] = 100
            _pipeline_state["message"] = "No approved/queued jobs found to process"
            print("[Pipeline] No jobs to process")
            return {"status": "complete", "message": "No jobs to process", "results": []}

        _pipeline_state["jobs_total"] = len(filtered_apps)
        print(f"[Pipeline] Processing {len(filtered_apps)} jobs...")

        # Initialize generators
        optimizer = ResumeOptimizer()
        cover_gen = CoverLetterGenerator()
        pdf_gen = ResumePDFGenerator()

        # Lazy-import auto_apply engine (requires playwright + aiogram)
        _application_engine = None
        if cfg["auto_apply"]:
            try:
                import importlib
                _engine_mod = importlib.import_module("jobs.auto_applier.applier.engine")
                _application_engine = _engine_mod.ApplicationEngine()
                print("[Pipeline] ApplicationEngine loaded for auto-apply")
            except ImportError as e:
                print(f"[Pipeline] ⚠️ ApplicationEngine unavailable ({e}) — auto-apply disabled")
                cfg["auto_apply"] = False

        # Lazy-import DynamicResumeBuilder for 3-tier resume generation
        _dynamic_builder = None
        if cfg.get("use_dynamic_resume", True):
            try:
                import importlib
                _builder_mod = importlib.import_module("jobs.auto_applier.resume.dynamic_builder")
                _dynamic_builder = _builder_mod.DynamicResumeBuilder()
                print("[Pipeline] DynamicResumeBuilder loaded (3-tier fallback)")
            except ImportError as e:
                print(f"[Pipeline] ⚠️ DynamicResumeBuilder unavailable ({e}) — using base resume")

        # ── Phases 3-6: Process Each Job ─────────────────────────────
        for idx, app in enumerate(filtered_apps):
            job = app["job"]
            job_id = job.get("id", 0)
            job_title = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            source_url = job.get("source_url", "")
            match_pct = app.get("match_percentage", 0)

            _pipeline_state["current_job"] = f"{job_title} at {company}"
            app_result = {
                "application_id": app.get("id", 0),
                "job_listing_id": job_id,
                "title": job_title,
                "company": company,
                "url": source_url,
                "match_percentage": match_pct,
                "status": "processing",
                "optimized_resume": "",
                "cover_letter": "",
                "pdf_paths": {},
                "telegram_sent": False,
                "auto_applied": False,
                "error": "",
            }

            try:
                # ── Check if URL should be skipped (EvoMap feedback loop) ──
                if source_url:
                    try:
                        import importlib
                        evo_mod = importlib.import_module("jobs.auto_applier.failure.evo_logger")
                        evo = evo_mod.EvoLogger()
                        if evo.should_skip_url(source_url):
                            print(f"[Pipeline] ⏭️ Skipping {job_title} @ {company} — URL has 3+ failures (EvoMap)")
                            app_result["status"] = "skipped"
                            app_result["error"] = "Skipped: URL has 3+ consecutive failures"
                            _pipeline_state["jobs_processed"] += 1
                            continue
                    except ImportError:
                        pass  # EvoLogger unavailable, don't skip

                # ── Phase 3: Optimize Resume ──────────────────────────
                pct_base = 15 + (idx / max(len(filtered_apps), 1)) * 60
                _pipeline_state["phase"] = PHASES[2]
                _pipeline_state["phase_index"] = 2
                _pipeline_state["progress_pct"] = round(pct_base, 1)
                _pipeline_state["message"] = f"Optimizing resume for {job_title} at {company}..."

                # Build match analysis from evaluation (shared across paths)
                match_analysis = {
                    "missing_skills": [],
                    "matching_skills": [],
                }
                if app.get("evaluation"):
                    eval_data = app["evaluation"]
                    try:
                        pros = json.loads(eval_data.get("pros", "[]")) if isinstance(eval_data.get("pros"), str) else eval_data.get("pros", [])
                        cons = json.loads(eval_data.get("cons", "[]")) if isinstance(eval_data.get("cons"), str) else eval_data.get("cons", [])
                        match_analysis = {
                            "matching_skills": pros[:5] if isinstance(pros, list) else [],
                            "missing_skills": cons[:5] if isinstance(cons, list) else [],
                        }
                    except (json.JSONDecodeError, TypeError):
                        pass

                # ── DynamicResumeBuilder: Generate tailored resume PDF (3-tier fallback) ──
                # This runs BEFORE the LLM optimizer — it produces a job-specific PDF
                # using: BARQ native → AI_Resume_Generator → static fallback.
                dynamic_resume_path = None
                if _dynamic_builder:
                    try:
                        dr_result = await asyncio.wait_for(
                            _dynamic_builder.build(
                                job_description=job.get("description", ""),
                                job_title=job_title,
                                company=company,
                                timeout=60,
                            ),
                            timeout=90,
                        )
                        if dr_result.pdf_path and dr_result.status != "error":
                            dynamic_resume_path = dr_result.pdf_path
                            app_result["dynamic_resume_path"] = dynamic_resume_path
                            app_result["dynamic_resume_source"] = dr_result.source
                            print(f"[Pipeline] DynamicResumeBuilder: {dr_result.status} ({dr_result.source}) — {dynamic_resume_path}")
                        else:
                            print(f"[Pipeline] DynamicResumeBuilder returned no PDF: {dr_result.error or dr_result.status}")
                    except asyncio.TimeoutError:
                        print(f"[Pipeline] ⏱️ DynamicResumeBuilder timed out for {job_title}")
                    except Exception as dr_err:
                        print(f"[Pipeline] ⚠️ DynamicResumeBuilder error: {dr_err}")

                # ── Phase 3: Try structured JSON optimization first, fall back to markdown ──
                # The LLM outputs ONLY a JSON object (no LaTeX). The PDF generator
                # injects this JSON into a hardcoded, pre-verified LaTeX template.
                llm_json_data: dict | None = None
                optimized_md: str = resume_md

                try:
                    json_result = await asyncio.wait_for(
                        optimizer.optimize_latex(resume_md, job, match_analysis),
                        timeout=35.0,
                    )
                except asyncio.TimeoutError:
                    print(f"[Pipeline] ⏱️ JSON optimization timed out for {job_title} — falling back to markdown")
                    json_result = {"_mode": "latex_json_fallback", "json_data": None}
                if json_result.get("_mode") == "latex_json" and json_result.get("json_data"):
                    llm_json_data = json_result["json_data"]
                    optimized_md = resume_md  # Keep original markdown for DB/compat
                    print(f"[Pipeline] JSON optimization succeeded for {job_title}")
                else:
                    # Fall back to markdown optimization
                    print(f"[Pipeline] JSON optimization unavailable, falling back to markdown for {job_title}")
                    try:
                        markdown_result = await asyncio.wait_for(
                            optimizer.optimize(resume_md, job, match_analysis),
                            timeout=35.0,
                        )
                    except asyncio.TimeoutError:
                        print(f"[Pipeline] ⏱️ Markdown optimization timed out for {job_title} — using original resume")
                        markdown_result = {"optimized_md": resume_md}
                    optimized_md = markdown_result.get("optimized_md", resume_md)

                app_result["optimized_resume"] = optimized_md
                app_result["llm_json_data"] = llm_json_data  # None if markdown fallback

                # ── Evaluator-Optimizer Gate: Resume (Reflection pattern) ──
                # A secondary LLM evaluates the tailored resume against the
                # job description. Below the threshold, the optimizer is
                # forced to revise with the evaluator's feedback (max N iters)
                # BEFORE any PDF is compiled or Telegram message is sent.
                eval_report: dict[str, Any] = {}
                if cfg["enable_evaluator"]:
                    try:
                        from jobs.evaluator_agent import EvaluatorAgent

                        evaluator = EvaluatorAgent(
                            threshold=cfg.get("evaluator_threshold", 80),
                            max_iterations=cfg.get("evaluator_max_iterations", 2),
                        )
                        if llm_json_data:
                            json_eval = await evaluator.ensure_resume_json(
                                llm_json_data, resume_md, job, optimizer, match_analysis
                            )
                            if json_eval.get("final_json"):
                                llm_json_data = json_eval["final_json"]
                            eval_report.update(json_eval)
                            print(f"[Pipeline] 📋 Evaluator (JSON resume): score {json_eval.get('final_score')}, passed {json_eval.get('passed')} after {json_eval.get('iterations')} iteration(s)")
                        else:
                            md_eval = await evaluator.ensure_resume_markdown(
                                optimized_md, resume_md, job, optimizer, match_analysis
                            )
                            optimized_md = md_eval.get("final_document", optimized_md)
                            eval_report.update(md_eval)
                            print(f"[Pipeline] 📋 Evaluator (markdown resume): score {md_eval.get('final_score')}, passed {md_eval.get('passed')} after {md_eval.get('iterations')} iteration(s)")
                    except Exception as eval_err:
                        print(f"[Pipeline] ⚠️ Evaluator gate error (continuing): {eval_err}")

                # ── Phase 4: Generate Cover Letter ────────────────────
                _pipeline_state["phase"] = PHASES[3]
                _pipeline_state["phase_index"] = 3
                _pipeline_state["progress_pct"] = round(pct_base + 15, 1)
                _pipeline_state["message"] = f"Writing cover letter for {job_title}..."

                try:
                    cover_letter = await asyncio.wait_for(
                        cover_gen.generate(job, resume, optimized_md),
                        timeout=35.0,
                    )
                except asyncio.TimeoutError:
                    print(f"[Pipeline] ⏱️ Cover letter generation timed out for {job_title} — skipping")
                    cover_letter = ""
                app_result["cover_letter"] = cover_letter

                # ── Evaluator-Optimizer Gate: Cover Letter ────────────
                if cfg["enable_evaluator"] and cover_letter:
                    try:
                        from jobs.evaluator_agent import EvaluatorAgent

                        evaluator = EvaluatorAgent(
                            threshold=cfg.get("evaluator_threshold", 80),
                            max_iterations=cfg.get("evaluator_max_iterations", 2),
                        )
                        cl_eval = await evaluator.ensure_cover_letter(cover_letter, job, resume)
                        cover_letter = cl_eval.get("final_document", cover_letter)
                        eval_report["cover_letter"] = cl_eval
                        print(f"[Pipeline] 📋 Evaluator (cover letter): score {cl_eval.get('final_score')}, passed {cl_eval.get('passed')} after {cl_eval.get('iterations')} iteration(s)")
                    except Exception as cl_err:
                        print(f"[Pipeline] ⚠️ Cover letter evaluator error (continuing): {cl_err}")

                # ── Phase 5: Generate PDFs (LaTeX when available) ─────
                _pipeline_state["phase"] = PHASES[4]
                _pipeline_state["phase_index"] = 4
                _pipeline_state["progress_pct"] = round(pct_base + 30, 1)
                _pipeline_state["message"] = f"Generating documents for {job_title}..."

                pdf_paths: dict[str, str] = {}
                pdf_bytes_dict: dict[str, bytes] = {}
                job_slug = safe_filename(f"{company}_{job_title}".replace(" ", "_"), max_len=50)
                job_description = job.get("description", "")

                if cfg["generate_pdf"]:
                    # ── Resume PDF ────────────────────────────────────
                    # Priority: dynamic_resume_path (3-tier) > LLM JSON → LaTeX > markdown → fpdf
                    if dynamic_resume_path and os.path.isfile(dynamic_resume_path):
                        # DynamicResumeBuilder already produced a PDF — use it directly
                        pdf_paths["resume"] = dynamic_resume_path
                        with open(dynamic_resume_path, "rb") as f:
                            pdf_bytes_dict["resume"] = f.read()
                        print(f"[Pipeline] Using DynamicResumeBuilder PDF for {job_title} ({len(pdf_bytes_dict['resume'])} bytes)")
                    elif llm_json_data:
                        # Build LaTeX from structured JSON → compile PDF (in-memory → disk)
                        # The JSON is injected into the hardcoded template — the LLM
                        # never produces raw LaTeX, preventing malformed compilation.
                        try:
                            resume_pdf_bytes = await compile_resume_pdf_from_json(
                                llm_json_data, f"Resume_{job_slug}.pdf"
                            )
                            pdf_bytes_dict["resume"] = resume_pdf_bytes
                            # Also save to disk for DB path + auto-apply
                            resume_dir = GENERATED_DIR / f"latex_{job_slug}"
                            resume_dir.mkdir(parents=True, exist_ok=True)
                            resume_pdf_path = str(resume_dir / f"Resume_{job_slug}.pdf")
                            with open(resume_pdf_path, "wb") as f:
                                f.write(resume_pdf_bytes)
                            pdf_paths["resume"] = resume_pdf_path
                            print(f"[Pipeline] JSON→LaTeX resume PDF generated for {job_title} ({len(resume_pdf_bytes)} bytes)")
                        except Exception as e:
                            print("\033[91m" + "═" * 70)
                            print("  ⚠️  !!! PDF_COMPILATION_FAILED !!!")
                            print(f"  Job: {job_title} @ {company}")
                            print("  Method: JSON→LaTeX (compile_resume_pdf_from_json)")
                            print(f"  Error: {e}")
                            print("  Fix: Ensure LaTeX (pdflatex/shell-escape) is installed and templates are valid.")
                            print("═" * 70 + "\033[0m")
                            llm_json_data = None  # Reset so markdown path is used below

                    if not llm_json_data:
                        # Fall back to template-based PDF generation
                        print(f"[Pipeline] Attempting markdown→PDF fallback for {job_title}...")
                        pdf_resume_data = {**resume, "raw_md": optimized_md}
                        resume_pdf_result = await pdf_gen.generate(
                            pdf_resume_data,
                            output_dir=str(GENERATED_DIR / f"optimized_{job_slug}"),
                            filename=f"Resume_{job_slug}",
                            job_description=job_description,
                        )
                        if resume_pdf_result.get("status") == "completed":
                            pdf_paths["resume"] = resume_pdf_result["pdf_path"]
                            # Read into bytes for in-memory Telegram send
                            with open(resume_pdf_result["pdf_path"], "rb") as f:
                                pdf_bytes_dict["resume"] = f.read()
                            print(f"[Pipeline] Markdown→PDF fallback SUCCEEDED for {job_title} ({len(pdf_bytes_dict['resume'])} bytes)")
                        else:
                            print(f"[Pipeline] ⚠️ Markdown→PDF fallback ALSO FAILED for {job_title}: {resume_pdf_result.get('message', 'unknown error')}")

                    # ── Cover Letter PDF ───────────────────────────────
                    if cover_letter:
                        cl_dir = GENERATED_DIR / f"cover_letter_{job_slug}"
                        cl_dir.mkdir(parents=True, exist_ok=True)
                        cl_path = str(cl_dir / f"Cover_Letter_{job_slug}.pdf")
                        cl_result = await generate_cover_letter_pdf(
                            cover_letter_text=cover_letter,
                            output_path=cl_path,
                            job_title=job_title,
                            company=company,
                        )
                        pdf_paths["cover_letter"] = cl_result["pdf_path"]
                        if cl_result.get("backend") != "txt_fallback" and cl_result.get("pdf_path", "").endswith(".pdf"):
                            with open(cl_result["pdf_path"], "rb") as f:
                                pdf_bytes_dict["cover_letter"] = f.read()
                            print(f"[Pipeline] Cover letter PDF generated for {job_title}")
                        else:
                            print(f"[Pipeline] ⚠️ Cover letter PDF fell back to txt: {cl_result.get('backend', 'unknown')}")

                # Log PDF generation summary for this job
                if cfg["generate_pdf"]:
                    pdf_status = "✅ PDFs generated" if pdf_bytes_dict else "❌ All PDF methods failed"
                    print(f"[Pipeline] {pdf_status} for {job_title} @ {company} (resume: {'✅' if 'resume' in pdf_bytes_dict else '❌'}, cover: {'✅' if 'cover_letter' in pdf_bytes_dict else '❌'})")
                else:
                    print(f"[Pipeline] PDF generation SKIPPED (disabled in settings) for {job_title}")
                
                if not pdf_bytes_dict:
                    print(f"[Pipeline] ⚠️ No PDF bytes available for Telegram — will send text-only summary for {job_title}")
                else:
                    print(f"[Pipeline] 📎 PDF bytes ready for Telegram attachment ({sum(len(v) for v in pdf_bytes_dict.values())} bytes total) for {job_title}")

                app_result["pdf_paths"] = pdf_paths

                # Update the application status in DB
                await jobs_dao.update_application_status(
                    app["id"],
                    "generating",
                    notes=json.dumps({
                        "pipeline_processed_at": datetime.now(timezone.utc).isoformat(),
                        "optimized": True,
                        "optimization_mode": "latex_json" if llm_json_data else "markdown",
                        "cover_letter_generated": bool(cover_letter),
                        "pdf_generated": bool(pdf_paths),
                        "match_percentage": match_pct,
                        "dynamic_resume": {
                            "path": dynamic_resume_path or "",
                            "source": app_result.get("dynamic_resume_source", ""),
                        } if dynamic_resume_path else None,
                        "evaluator": {
                            "enabled": cfg["enable_evaluator"],
                            "resume_score": eval_report.get("final_score"),
                            "resume_passed": eval_report.get("passed"),
                            "resume_iterations": eval_report.get("iterations", 0),
                            "cover_letter_score": (eval_report.get("cover_letter") or {}).get("final_score"),
                            "cover_letter_passed": (eval_report.get("cover_letter") or {}).get("passed"),
                        },
                    }),
                )

                # Store documents in DB
                import json as _json_mod
                if llm_json_data:
                    resume_content = _json_mod.dumps(llm_json_data, indent=2)
                else:
                    resume_content = optimized_md
                resume_format = "latex_json" if llm_json_data else "markdown"
                if resume_content and app["id"]:
                    await jobs_dao.insert_document({
                        "application_id": app["id"],
                        "document_type": "resume",
                        "content": resume_content,
                        "file_path": pdf_paths.get("resume", ""),
                        "format": resume_format,
                        "generated_by": "llm",
                    })
                if cover_letter and app["id"]:
                    await jobs_dao.insert_document({
                        "application_id": app["id"],
                        "document_type": "cover_letter",
                        "content": cover_letter,
                        "file_path": pdf_paths.get("cover_letter", ""),
                        "format": "markdown",
                        "generated_by": "llm",
                    })

                # ── Phase 6: Notify & Apply ──────────────────────────
                _pipeline_state["phase"] = PHASES[5]
                _pipeline_state["phase_index"] = 5
                _pipeline_state["progress_pct"] = round(pct_base + 45, 1)
                _pipeline_state["message"] = f"Sending notification for {job_title}..."

                telegram_sent = False
                auto_applied = False

                if cfg["send_telegram"]:
                    telegram_sent = await _send_telegram_notification(
                        job_title=job_title,
                        company=company,
                        job_url=source_url,
                        match_pct=match_pct,
                        app_id=app["id"],
                        resume=resume,
                        evaluation=app.get("evaluation"),
                        resume_snippet=optimized_md[:500] if optimized_md else "",
                        cover_letter_snippet=cover_letter[:300] if cover_letter else "",
                        pdf_paths=pdf_paths,
                        pdf_bytes=pdf_bytes_dict,  # In-memory bytes for sendDocument
                    )

                if cfg["auto_apply"] and source_url and _application_engine:
                    # Use ApplicationEngine for browser-based auto-apply
                    # Pass the best available resume: dynamic > PDF > base
                    resume_for_apply = dynamic_resume_path or pdf_paths.get("resume")
                    try:
                        auto_apply_result = await _application_engine.apply_to_job(
                            job_url=source_url,
                            company=company,
                            title=job_title,
                            job_context=job.get("description", ""),
                            resume_path=resume_for_apply,
                        )
                        auto_applied = auto_apply_result.get("submitted", False)
                        app_result["auto_apply_result"] = auto_apply_result
                        print(f"[Pipeline] ApplicationEngine result: submitted={auto_applied}, status={auto_apply_result.get('status')}")
                    except Exception as ae_err:
                        print(f"[Pipeline] ⚠️ ApplicationEngine error: {ae_err}")
                        auto_apply_result = {"status": "error", "error": str(ae_err)}
                        app_result["auto_apply_result"] = auto_apply_result

                # Mark application as submitted or ready_for_review.
                # notified_at is stamped when a notification was actually sent
                # — or when sending is disabled entirely — so genuine send
                # failures are retried next run without re-processing forever.
                if auto_applied:
                    await jobs_dao.update_application_status(
                        app["id"], "submitted",
                        submitted_at=datetime.now(timezone.utc).isoformat(),
                    )
                elif telegram_sent:
                    await jobs_dao.update_application_status(
                        app["id"], "ready_for_review",
                        notified_at=datetime.now(timezone.utc).isoformat(),
                    )
                elif cfg["send_telegram"]:
                    # Attempted but failed — leave un-notified so the next run retries.
                    await jobs_dao.update_application_status(
                        app["id"], "ready_for_review",
                    )
                else:
                    # Sending disabled — mark handled so the app isn't
                    # re-processed (and documents regenerated) every run.
                    await jobs_dao.update_application_status(
                        app["id"], "ready_for_review",
                        notified_at=datetime.now(timezone.utc).isoformat(),
                    )

                app_result["status"] = "completed"
                app_result["telegram_sent"] = telegram_sent
                app_result["auto_applied"] = auto_applied

                _pipeline_state["jobs_succeeded"] += 1
                results.append(app_result)

                # Log success
                # Auto-extract knowledge triplets from the job description
                try:
                    extractor = AutoExtractor()
                    await extractor.extract_from_job(
                        job_id=job_id,
                        title=job_title,
                        description=job.get("description", ""),
                        company=company,
                    )
                except Exception as exc:
                    logger.warning("[Extraction] Failed to extract from job %d: %s", job_id, exc)

                await analytics_dao.log_activity(
                    "job", "pipeline_processed",
                    f"Pipeline: {job_title} at {company} "
                    f"(match: {match_pct}%, telegram: {telegram_sent}, auto-apply: {auto_applied})",
                )

            except Exception as e:
                print(f"[Pipeline] Error processing {job_title} at {company}: {e}")
                app_result["status"] = "failed"
                app_result["error"] = str(e)
                _pipeline_state["jobs_failed"] += 1
                results.append(app_result)

                # Increment retry_count for this application
                try:
                    current_retry = app.get("retry_count") or 0
                    await jobs_dao.update_application_status(
                        app["id"],
                        "failed",
                        notes=json.dumps({
                            "error": str(e)[:500],
                            "retry_count": current_retry + 1,
                            "last_failed_at": datetime.now(timezone.utc).isoformat(),
                        }),
                    )
                except Exception as retry_err:
                    print(f"[Pipeline] Failed to update retry_count: {retry_err}")

            _pipeline_state["jobs_processed"] += 1

        # ── Complete ─────────────────────────────────────────────────
        _pipeline_state["status"] = "complete"
        _pipeline_state["progress_pct"] = 100
        _pipeline_state["message"] = (
            f"Pipeline complete — "
            f"{_pipeline_state['jobs_succeeded']} succeeded, "
            f"{_pipeline_state['jobs_failed']} failed "
            f"out of {_pipeline_state['jobs_total']} jobs"
        )
        _pipeline_state["elapsed_seconds"] = round(time.time() - _pipeline_state["started_at"], 1)
        _pipeline_state["results"] = results

        print(f"[Pipeline] Complete: {len(results)} jobs processed")
        await analytics_dao.log_activity(
            "job", "pipeline_complete",
            f"Pipeline: {_pipeline_state['jobs_succeeded']} succeeded, "
            f"{_pipeline_state['jobs_failed']} failed in "
            f"{_pipeline_state['elapsed_seconds']}s",
        )

        # Auto-reset after delay
        asyncio.create_task(_auto_reset())

        return {
            "status": "complete",
            "total": len(filtered_apps),
            "succeeded": _pipeline_state["jobs_succeeded"],
            "failed": _pipeline_state["jobs_failed"],
            "elapsed_seconds": _pipeline_state["elapsed_seconds"],
            "results": results,
        }

    except Exception as e:
        _pipeline_state["status"] = "error"
        _pipeline_state["message"] = f"Pipeline failed: {e}"
        print(f"[Pipeline] Fatal error: {e}")
        await analytics_dao.log_activity(
            "job", "pipeline_error", f"Pipeline failed: {e}", severity="error",
        )
        return {"status": "error", "message": str(e), "results": results}





async def _send_telegram_notification(
    job_title: str,
    company: str,
    job_url: str,
    match_pct: float,
    app_id: int,
    resume: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
    resume_snippet: str = "",
    cover_letter_snippet: str = "",
    pdf_paths: dict[str, str] | None = None,
    pdf_bytes: dict[str, bytes] | None = None,
) -> bool:
    """
    Send a polished Telegram notification with a concise summary + PDF attachments.

    1. Sends a clean, concise match summary: Score, Pros, Cons, Apply Link.
    2. Sends the Resume PDF and Cover Letter PDF as Telegram document attachments.
       Uses `send_document_from_bytes()` (in-memory) when pdf_bytes provided,
       falls back to `send_document()` (file-based) otherwise.

    NO raw text dumps, NO "Part 1/3" chunking, NO raw HTML tags leaking.
    """
    try:
        # Use the unified aiogram bot for all Telegram operations
        import importlib
        bot_mod = importlib.import_module("jobs.auto_applier.telegram.bot")
        telegram = bot_mod.AutoApplyBot()

        # Get Telegram config
        global CONFIG
        if CONFIG is None:
            try:
                settings = get_settings()
                CONFIG = type('Config', (), {
                    'telegram_bot_token': getattr(settings, 'telegram_bot_token', ''),
                    'telegram_chat_id': getattr(settings, 'telegram_chat_id', ''),
                })()
            except Exception:
                CONFIG = type('Config', (), {'telegram_bot_token': '', 'telegram_chat_id': ''})()

        if not CONFIG.telegram_bot_token or not CONFIG.telegram_chat_id:
            print("[Pipeline] Telegram not configured; skipping detailed notification")
            return False

        # ── REDUNDANCY ELIMINATED ─────────────────────────────────────
        # The notification_manager.send_job_match_alert() call has been REMOVED
        # because it sends a SEPARATE sendMessage text via the Telegram channel
        # (through manager.send() -> telegram.send() -> _format_message() ->
        # Bot API sendMessage). This created a duplicate "old text message"
        # alongside the new PDFs + concise summary below.
        #
        # DB history is already recorded via jobs_dao.update_application_status()
        # and analytics_dao.log_activity() in the main pipeline loop.

        safe_title = html_mod.escape(job_title)
        safe_company = html_mod.escape(company)

        # ── Parse evaluation data ─────────────────────────────────────
        pros: list = []
        cons: list = []
        if evaluation:
            try:
                pros_raw = evaluation.get("pros", "[]")
                cons_raw = evaluation.get("cons", "[]")
                pros = json.loads(pros_raw) if isinstance(pros_raw, str) else (pros_raw or [])
                cons = json.loads(cons_raw) if isinstance(cons_raw, str) else (cons_raw or [])
            except (json.JSONDecodeError, TypeError):
                pros, cons = [], []

        # 2. Send PDF documents as Telegram file attachments (in-memory when possible)
        pdfs_sent = 0
        by = pdf_bytes or {}
        paths = pdf_paths or {}

        # ── Resume PDF ────────────────────────────────────────────────
        resume_label = f"Resume_{safe_company}_{safe_title}"[:55].rstrip("_") + ".pdf"
        resume_caption = (
            f"📄 <b>Tailored Resume</b>: {safe_title} @ {safe_company} — "
            f"Match: {match_pct:.0f}%"
        )

        if by.get("resume"):
            # In-memory: send from bytes (no disk read needed)
            doc_result = await telegram.send_document_from_bytes(
                file_bytes=by["resume"],
                filename=resume_label,
                caption=resume_caption,
            )
            if getattr(doc_result, 'success', None) or doc_result.get('success'):
                pdfs_sent += 1
        elif paths.get("resume") and os.path.isfile(paths["resume"]):
            # File-based fallback
            doc_result = await telegram.send_document(
                document_path=paths["resume"],
                caption=resume_caption,
            )
            if getattr(doc_result, 'success', None) or doc_result.get('success'):
                pdfs_sent += 1

        # ── Cover Letter PDF ──────────────────────────────────────────
        cl_label = f"CoverLetter_{safe_company}_{safe_title}"[:55].rstrip("_") + ".pdf"
        cl_caption = (
            f"✉️ <b>Cover Letter</b>: {safe_title} @ {safe_company}"
        )

        if by.get("cover_letter"):
            # In-memory: send from bytes
            doc_result = await telegram.send_document_from_bytes(
                file_bytes=by["cover_letter"],
                filename=cl_label,
                caption=cl_caption,
            )
            if getattr(doc_result, 'success', None) or doc_result.get('success'):
                pdfs_sent += 1
        elif paths.get("cover_letter") and os.path.isfile(paths["cover_letter"]) and paths["cover_letter"].endswith(".pdf"):
            # File-based fallback
            doc_result = await telegram.send_document(
                document_path=paths["cover_letter"],
                caption=cl_caption,
            )
            if getattr(doc_result, 'success', None) or doc_result.get('success'):
                pdfs_sent += 1

        # 3. Send CONCISE summary — NO resume dump, NO cover letter text
        #    Just Score, Pros, Cons, and Apply Link.
        summary = (
            f"🎯 <b>Job Match Found</b>\n"
            f"<b>{safe_title}</b> at <b>{safe_company}</b>\n"
            f"📊 <b>Match Score:</b> {match_pct:.0f}%"
        )

        if job_url:
            summary += (
                f"\n\n🔗 <a href=\"{html_mod.escape(job_url)}\">"
                f"<b>⬅️ OPEN APPLICATION ➡️</b></a>"
            )
        else:
            # Fallback: generate a Google search link for the job
            search_query = html_mod.escape(f"{job_title} {company} job application")
            fallback_url = f"https://www.google.com/search?q={search_query.replace(' ', '+')}"
            summary += (
                f"\n\n🔗 <a href=\"{fallback_url}\">"
                f"<b>🔍 SEARCH FOR JOB ➡️</b></a>"
                f"\n  <i>No direct application URL available — Google search</i>"
            )
            print(f"[Pipeline] ⚠️ No job URL for {job_title} @ {company} — added search fallback link")

        if pros:
            summary += "\n\n✅ <b>Strengths</b>"
            for p in pros[:3]:
                summary += f"\n  • {html_mod.escape(str(p)[:120])}"

        if cons:
            summary += "\n\n⚠️ <b>Considerations</b>"
            for c in cons[:3]:
                summary += f"\n  • {html_mod.escape(str(c)[:120])}"

        if pdfs_sent:
            summary += "\n\n📎 <i>Resume + Cover Letter PDFs attached above</i>"

        summary += f"\n\n✅ Application #{app_id}"

        # Send the concise summary as a clean HTML message
        result = await telegram.send_html_message(
            text=summary,
            title=f"🎯 Job Match: {safe_title}",
            disable_notification=(match_pct < 80),
        )

        if not (getattr(result, 'success', None) or result.get('success')):
            error = getattr(result, 'error', None) or result.get('error', 'unknown')
            print(f"[Pipeline] Failed to send summary: {error}")
            return False

        print(f"[Pipeline] Telegram notification sent for {job_title} @ {company} (match: {match_pct:.0f}%, pdfs: {pdfs_sent})")
        return True

    except Exception as e:
        print(f"[Pipeline] Telegram notification error: {e}")
        return False


async def _auto_reset():
    """Reset pipeline state to idle after a delay."""
    await asyncio.sleep(60)
    if _pipeline_state["status"] in ("complete", "error"):
        _pipeline_state["status"] = "idle"
        _pipeline_state["progress_pct"] = 0
