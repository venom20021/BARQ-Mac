"""
FastAPI routes for job search automation.
Uses database DAOs for all CRUD operations.
"""

import asyncio
import base64
import json
import os
import re
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from database import analytics_dao, db_connection, jobs_dao

from . import (
    FollowUpAutomation, JobApplier, JobEvaluator, JobScanner,
    ResponseTracker, get_pipeline_progress, get_pipeline_settings, run_pipeline,
)
from .scanner import get_scan_progress, set_scan_error

router = APIRouter()

scanner = JobScanner()
evaluator = JobEvaluator()
applier = JobApplier()
response_tracker = ResponseTracker()
followup_automation = FollowUpAutomation()


@router.get("/")
async def jobs_root():
    """Jobs module root — returns module status."""
    return {
        "module": "jobs",
        "status": "ready",
        "endpoints": ["/scan", "/matches", "/applications", "/approve", "/resume", "/analytics", "/followups"],
    }


class ApproveRequest(BaseModel):
    job_id: str


async def _run_scan():
    """Background scan that runs in a separate asyncio task."""
    try:
        jobs = await scanner.scan_all(
            keywords=[
                "software engineer", "developer", "full stack",
                "backend", "frontend", "devops", "data engineer",
                "machine learning", "python", "javascript", "typescript",
                "full stack developer", "software developer", "sde",
                "cloud engineer", "backend engineer", "python developer",
            ],
            location="global",  # Global scan: Italy, Luxembourg, Middle East, UK, US, Canada, India
        )
        count = 0
        new_boards: set[str] = set()
        for job in jobs[:50]:
            # Insert the listing — already-known jobs are skipped entirely so
            # they never get re-counted, re-evaluated, or re-notified.
            listing_id = await jobs_dao.insert_job_listing_if_new(job)
            if listing_id is None:
                continue
            board_name = job.get("source_board", "") or job.get("source", "")
            if board_name:
                new_boards.add(board_name)
            # Insert evaluation data if the scanner already evaluated it
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
                    print(f"[Scan] Failed to insert evaluation for job #{listing_id}: {eval_err}")
            count += 1

        source_boards = len(new_boards)
        await analytics_dao.log_activity(
            "job", "scan",
            f"Scanned {count} new job listings from {source_boards} boards"
        )
        # ── Auto-trigger pipeline to process high-match jobs ──────────
        if count > 0:
            try:
                from .pipeline import run_pipeline
                pipeline_result = await run_pipeline({
                    "mode": "notify",
                    "max_per_run": 10,
                    "min_match_score": 60,
                    "generate_pdf": True,   # Generate PDFs for auto-triggered pipeline runs
                    "send_telegram": True,
                })
                succeeded = pipeline_result.get("succeeded", 0)
                print(f"[Scan] Pipeline processed {succeeded} jobs — Telegram notifications sent")
            except Exception as pipe_err:
                print(f"[Scan] Pipeline trigger failed: {pipe_err}")

    except Exception as e:
        set_scan_error(f"Scan failed: {e}")
        await analytics_dao.log_activity("job", "scan_error", str(e), severity="error")


@router.post("/scan")
async def scan_jobs(background_tasks: BackgroundTasks):
    """Trigger a scan of all job boards in the background with real-time progress tracking."""
    # Don't start a new scan if one is already running
    progress = get_scan_progress()
    if progress["status"] in ("scanning", "evaluating"):
        return {"status": "already_running", "message": "A scan is already in progress", "progress": progress}

    # Reset and start scan as a background task
    background_tasks.add_task(_run_scan)

    return {
        "status": "started",
        "message": "Scan started in background",
    }


@router.get("/scan/progress")
async def scan_progress():
    """Get real-time progress of the current scan operation."""
    progress = get_scan_progress()
    return progress


@router.get("/scan/stream")
async def scan_stream():
    """Server-Sent Events endpoint that streams real-time scan progress.

    Keeps the connection open, yielding progress updates as the scanner
    works. Closes automatically when the scan completes or errors.
    Returns immediately with a keepalive comment if no scan is active.
    """
    async def event_generator():
        last_message = ""
        last_pct = -1
        try:
            while True:
                progress = get_scan_progress()

                # If no scan has been seen yet, wait (don't close — scan may be starting)
                if progress.get("status") == "idle" and last_message == "":
                    yield f"data: {json.dumps({'status': 'waiting', 'message': 'Waiting for scan to start...'})}\n\n"
                    last_message = "waiting"
                    await asyncio.sleep(1)
                    continue

                # Only yield when progress actually changes
                msg = progress.get("message", "")
                pct = progress.get("progress_pct", 0)
                status = progress.get("status", "idle")

                if msg != last_message or pct != last_pct or status in ("complete", "error"):
                    yield f"data: {json.dumps(progress)}\n\n"
                    last_message = msg
                    last_pct = pct

                # Stop streaming on completion / error
                if status in ("complete", "error"):
                    yield f"data: {json.dumps({'status': 'completed', 'final': True})}\n\n"
                    return

                # Wait for next notification (with heartbeat ping every 2s)
                from .scanner import _get_event
                try:
                    await asyncio.wait_for(_get_event().wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass  # Heartbeat — send keepalive comment
                finally:
                    # Event was consumed; reset for next round
                    _get_event().clear()
        except Exception:
            pass  # Client disconnected

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/matches")
async def get_matches(min_score: float = 3.0, limit: int = 20):
    """Get evaluated job matches from the database with real application status."""
    try:
        matches = await jobs_dao.get_top_matches(min_score=min_score, limit=limit)
        # Batch-fetch application statuses for all returned jobs
        job_ids = [m["id"] for m in matches]
        app_statuses = await jobs_dao.get_application_statuses_for_jobs(job_ids)
        print(f"[MATCHES] Fetched {len(matches)} jobs, batch-app statuses found for {len(app_statuses)} jobs: {app_statuses}")
        return {
            "matches": [
                {
                    "id": m["id"],
                    "title": m["title"],
                    "company": m["company"],
                    "location": m.get("location", ""),
                    "salary_min": m.get("salary_min", 0),
                    "salary_max": m.get("salary_max", 0),
                    "match_score": m.get("overall_score", 0),
                    "match_percentage": m.get("match_percentage", 0),
                    "pros": m.get("pros", "[]"),
                    "cons": m.get("cons", "[]"),
                    "reasoning": m.get("reasoning", ""),
                    "source": m.get("source_board", ""),
                    "description": m.get("description", ""),
                    "source_url": m.get("source_url", ""),
                    "status": app_statuses.get(m["id"], "new"),
                }
                for m in matches
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approve")
async def approve_application(request: ApproveRequest):
    """Approve a job for automated application."""
    try:
        job_id = int(request.job_id)
        app_id = await jobs_dao.insert_application({
            "job_listing_id": job_id,
            "status": "queued",
            "application_type": "auto",
        })
        await analytics_dao.log_activity(
            "job", "approve", f"Application queued for job listing #{job_id}"
        )
        return {
            "status": "approved",
            "application_id": app_id,
            "job_id": request.job_id,
            "message": "Application queued for processing",
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id: must be an integer")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{job_id}/apply/preview", summary="Safe-mode: fill a job application form and screenshot WITHOUT submitting")
async def preview_application(job_id: int):
    """Safe-mode auto-apply v1: fill the application form and capture a screenshot,
    but NEVER submit.

    Uses the user's real browser profile (Playwright) to navigate to the job's
    source URL, detect the ATS platform, fill the form fields from the parsed
    resume, and return a screenshot + filled-fields summary for human review.

    This is the "human-confirm before submit" gate — callers review the result
    and only then decide to apply (POST /jobs/{job_id}/apply).
    """
    try:
        job = await jobs_dao.get_job_listing(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job #{job_id} not found")

        job_url = (job.get("source_url", "") or job.get("url", "") or "").strip()
        if not job_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Job has no usable web application URL")

        from .resume_parser import parse_resume
        resume = parse_resume()
        user_profile = {
            "full_name": resume.get("full_name", ""),
            "email": resume.get("email", ""),
            "phone": resume.get("phone", ""),
            "linkedin_url": resume.get("linkedin_url", ""),
            "github_url": resume.get("github_url", ""),
            "portfolio_url": resume.get("portfolio_url", ""),
            "skills": resume.get("skills", []),
        }

        result = await applier.auto_fill_application(
            job_url=job_url,
            user_profile=user_profile,
            resume_path=None,
        )

        # Base64-encode the screenshot so the renderer can display it through
        # the JSON IPC bridge (works in both local and cloud mode — a direct
        # file:// or backend URL would be blocked by the bridge/CORS).
        screenshot_base64 = ""
        screenshot_path = str(result.get("screenshot_path", "") or "")
        if screenshot_path and os.path.exists(screenshot_path):
            try:
                with open(screenshot_path, "rb") as _f:
                    screenshot_base64 = "data:image/png;base64," + base64.b64encode(_f.read()).decode("utf-8")
            except Exception as _enc_err:
                print(f"[ApplyPreview] Screenshot encode failed (non-fatal): {_enc_err}")

        await analytics_dao.log_activity(
            "job", "apply_preview",
            f"Safe-mode form preview for #{job_id}: {job.get('title', '')} — {result.get('status')}"
        )

        return {
            "job_id": job_id,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            **result,
            "screenshot_base64": screenshot_base64,
            "next": {
                "action": "apply",
                "endpoint": f"/jobs/{job_id}/apply",
                "note": "Review the screenshot, then POST to apply to submit the real application.",
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        await analytics_dao.log_activity(
            "job", "apply_preview_error", str(e), severity="error"
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{job_id}/apply", status_code=202)
async def apply_to_job(job_id: int, background_tasks: BackgroundTasks):
    """Submit an application for a specific job. Returns immediately (202).

    Validates the job exists, queues the application, and runs the
    apply worker in the background so the UI feels instantly responsive.
    """
    try:
        # Validate job exists
        job = await jobs_dao.get_job_listing(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job #{job_id} not found")

        # Insert application record — start as 'queued' (proven to pass CHECK constraint)
        # The worker will transition to 'submitted'/'ready_for_review'/'failed'
        app_id = await jobs_dao.insert_application({
            "job_listing_id": job_id,
            "status": "queued",
            "application_type": "auto",
        })

        # 🚨 DIAGNOSTIC: Verify DB commit — re-query immediately
        verify_app = await jobs_dao.get_application(app_id)
        print(f"[APPLY] Created app #{app_id} for job #{job_id}, DB verify: status={verify_app['status'] if verify_app else 'NOT_FOUND'}, job_title={verify_app.get('title','') if verify_app else '?'}")

        await analytics_dao.log_activity(
            "job", "apply_start", f"Application started for #{job_id}: {job.get('title', '')} at {job.get('company', '')}"
        )

        # Background apply worker — runs async, doesn't block the response
        async def _apply_worker():
            """Background worker that runs the actual application process.

            Stages:
            1. Parse resume → build user_profile
            2. Generate cover letter
            3. Generate optimized resume PDF
            4. If job has a URL, attempt Playwright auto-fill (headful)
            5. Update application status
            """
            try:
                from .resume_parser import parse_resume
                resume = parse_resume()

                # Build user profile from parsed resume
                user_profile = {
                    "full_name": resume.get("full_name", ""),
                    "email": resume.get("email", ""),
                    "phone": resume.get("phone", ""),
                    "linkedin_url": resume.get("linkedin_url", ""),
                    "github_url": resume.get("github_url", ""),
                    "portfolio_url": resume.get("portfolio_url", ""),
                    "skills": resume.get("skills", []),
                    "experience": resume.get("experience", []),
                    "education": resume.get("education", []),
                }

                app_status = "queued"
                auto_fill_result = None

                # Generate cover letter
                from .cover_letter import CoverLetterGenerator
                cl_gen = CoverLetterGenerator()
                cover_letter = await cl_gen.generate(job, resume)

                # Generate optimized resume PDF
                pdf_result = None
                pdf_path = None
                try:
                    from .pdf_generator import ResumePDFGenerator
                    pdf_gen = ResumePDFGenerator()
                    pdf_result = await pdf_gen.generate(resume)
                    pdf_path = pdf_result.get("pdf_path", "") if isinstance(pdf_result, dict) else ""
                except Exception as pdf_err:
                    print(f"[ApplyWorker] PDF generation failed: {pdf_err}")

                # Stage 4: Playwright-based auto-fill (headful attempt)
                job_url = (job.get("source_url", "") or job.get("url", "") or "").strip()
                # Sanitize: only proceed if it's a valid http/https URL
                if job_url and not job_url.startswith(("http://", "https://")):
                    print(f"[ApplyWorker] Blocked invalid job URL (not http/https): {job_url}")
                    job_url = ""
                if job_url and len(job_url) > 10 and (job_url.startswith("http://") or job_url.startswith("https://")):
                    try:
                        print(f"[ApplyWorker] Attempting Playwright auto-fill for {job_url}")
                        auto_fill_result = await applier.auto_fill_application(
                            job_url=job_url,
                            user_profile=user_profile,
                            resume_path=pdf_path if pdf_path else None,
                        )
                        if auto_fill_result and auto_fill_result.get("status") == "completed":
                            app_status = "submitted"
                            platform = auto_fill_result.get("detected_platform", "unknown")
                            print(f"[ApplyWorker] Playwright auto-fill completed — {platform}")
                        else:
                            app_status = "ready_for_review"
                            reason = auto_fill_result.get("message", "auto-fill unavailable") if auto_fill_result else "no result"
                            print(f"[ApplyWorker] Playwright auto-fill: {reason}")
                    except Exception as pw_err:
                        print(f"[ApplyWorker] Playwright error (non-fatal): {pw_err}")
                        app_status = "ready_for_review"
                else:
                    print("[ApplyWorker] No job URL available — documents only")
                    app_status = "ready_for_review"

                # Update application with results
                update_data: dict[str, Any] = {
                    "status": app_status,
                    "notes": json.dumps({
                        "cover_letter_generated": len(cover_letter) > 0,
                        "pdf_generated": bool(pdf_result),
                    }),
                }
                if auto_fill_result:
                    notes = json.loads(update_data["notes"])
                    notes["playwright"] = auto_fill_result.get("status")
                    notes["platform"] = auto_fill_result.get("detected_platform", "")
                    notes["filled_fields"] = len(auto_fill_result.get("filled_fields", {}))
                    notes["screenshot"] = auto_fill_result.get("screenshot_path", "")
                    update_data["notes"] = json.dumps(notes)

                # Use update_application_status — the correct method name in JobsDAO
                # kwargs expands into named params for the SQL SET clause
                await jobs_dao.update_application_status(
                    app_id,
                    status=update_data["status"],
                    notes=update_data.get("notes", ""),
                )

                await analytics_dao.log_activity(
                    "job", "apply_complete",
                    f"Application #{app_id} for #{job_id} — {app_status}"
                )
            except Exception as worker_err:
                print(f"[ApplyWorker] Fatal error: {worker_err}")
                await jobs_dao.update_application_status(
                    app_id,
                    status="failed",
                    notes=f"Error: {worker_err}",
                )
                await analytics_dao.log_activity(
                    "job", "apply_error", str(worker_err), severity="error"
                )

        background_tasks.add_task(_apply_worker)

        return {
            "status": "accepted",
            "application_id": app_id,
            "job_id": job_id,
            "message": f"Application for #{job_id} accepted — processing in background",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/applications")
async def get_applications(status: str = "", limit: int = 50):
    """Get applications, optionally filtered by status."""
    try:
        if status:
            apps = await jobs_dao.get_applications_by_status(status, limit)
        else:
            apps = await jobs_dao.get_applications_by_status("queued", limit)
        return {"applications": apps, "count": len(apps)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Response Rate Analytics ────────────────────────────────────────


@router.get("/analytics/responses")
async def get_response_analytics():
    """Get comprehensive response rate analytics."""
    try:
        analytics = await response_tracker.get_response_analytics()
        return analytics
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/matches")
async def get_match_analytics():
    """Get job match analytics: score tiers, top sources, scan stats."""
    try:
        analytics = await jobs_dao.get_match_analytics()
        return analytics
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/responses/record")
async def record_response(data: dict):
    """Record a response for an application (interview, rejection, offer)."""
    try:
        result = await response_tracker.record_response(
            application_id=data["application_id"],
            response_type=data["response_type"],
            response_text=data.get("response_text"),
            interview_date=data.get("interview_date"),
        )
        return result
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Follow-Up Automation ────────────────────────────────────────────


@router.get("/followups/candidates")
async def get_followup_candidates():
    """Get applications that need follow-up (submitted > 14 days, no response)."""
    try:
        candidates = await response_tracker.get_followup_candidates()
        return {"candidates": candidates, "count": len(candidates)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/followups/schedule")
async def schedule_followups():
    """Check all applications and generate follow-up drafts."""
    try:
        scheduled = await followup_automation.schedule_followups()
        return {"scheduled": scheduled, "count": len(scheduled)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/followups/send")
async def send_followup(data: dict):
    """Mark a follow-up as sent for an application."""
    try:
        result = await followup_automation.send_followup(
            application_id=data["application_id"],
            followup_number=data.get("followup_number", 1),
        )
        return result
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/followups/history")
async def get_followup_history():
    """Get follow-up history."""
    try:
        history = await response_tracker.get_followup_history()
        return {"history": history, "count": len(history)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Resume Upload ─────────────────────────────────────────────────


class ResumeUploadResponse(BaseModel):
    status: str
    message: str
    path: str = ""


@router.post("/resume/upload", summary="Upload a resume file to replace ~/career-ops/cv.md")
async def upload_resume(data: dict):
    """
    Upload resume content (markdown text) and save it to ~/career-ops/cv.md.
    The pipeline and resume parser will use this file for job matching.
    """
    from pathlib import Path

    try:
        content = data.get("content", "")
        if not content or len(content.strip()) < 50:
            raise HTTPException(status_code=400, detail="Resume content must be at least 50 characters")

        from .resume_parser import DEFAULT_RESUME_PATH
        path = Path(DEFAULT_RESUME_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"[ResumeUpload] Saved {len(content)} chars to {path}")

        from .resume_parser import clear_parse_cache
        clear_parse_cache()

        await analytics_dao.log_activity("job", "resume_upload", "Resume updated via upload")

        return {"status": "saved", "message": "Resume saved successfully", "path": str(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resume/upload-pdf", summary="Upload a PDF resume and extract text to ~/career-ops/cv.md")
async def upload_resume_pdf(file: UploadFile = File(...)):
    """
    Accept a PDF resume file, extract its text content using PyMuPDF,
    and save the extracted text as ~/career-ops/cv.md for the pipeline.

    Supports .pdf files only. Extracted markdown preserves headings, paragraphs,
    and bullet points where possible.
    """
    from pathlib import Path

    # Validate file type
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    # Read the uploaded PDF bytes
    pdf_bytes = await file.read()
    if len(pdf_bytes) < 100:
        raise HTTPException(status_code=400, detail="PDF file appears empty")
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF file exceeds 10MB limit")

    try:
        # Try PyMuPDF for text extraction
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        extracted_pages = []
        for page_num, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                extracted_pages.append(text.strip())
        doc.close()

        if not extracted_pages or all(
            len(p.strip()) < 20 for p in extracted_pages
        ):
            # PDF has no extractable text (scanned image) — fallback
            raise HTTPException(
                status_code=400,
                detail="No extractable text found in PDF. The PDF may be scanned/inage-based. "
                       "Try a text-based PDF or paste the resume markdown directly.",
            )

        # Join pages and normalise whitespace
        full_text = "\n\n".join(extracted_pages)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)
        full_text = full_text.strip()

        if len(full_text) < 50:
            raise HTTPException(
                status_code=400,
                detail=f"Only {len(full_text)} characters extracted — PDF may be scanned. "
                       "Try a text-based PDF or paste markdown directly.",
            )

        # Save as markdown
        from .resume_parser import DEFAULT_RESUME_PATH, clear_parse_cache
        path = Path(DEFAULT_RESUME_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(full_text, encoding="utf-8")
        print(f"[ResumeUpload] PDF extracted {len(full_text)} chars, {len(extracted_pages)} pages to {path}")
        clear_parse_cache()

        await analytics_dao.log_activity(
            "job", "resume_upload_pdf",
            f"Resume parsed from PDF ({len(full_text)} chars, {len(extracted_pages)} pages)",
        )

        # Parse the newly saved resume to return structured info
        from .resume_parser import parse_resume
        parsed = parse_resume()

        return {
            "status": "saved",
            "message": f"Resume extracted from PDF — {len(full_text)} characters, {len(extracted_pages)} pages",
            "path": str(path),
            "char_count": len(full_text),
            "page_count": len(extracted_pages),
            "parsed": {
                "full_name": parsed.get("full_name", ""),
                "skills_count": len(parsed.get("skills", [])),
                "experience_count": len(parsed.get("experience", [])),
            },
        }

    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PDF text extraction requires PyMuPDF. Install with: pip install PyMuPDF",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse PDF: {str(e)}",
        )


class PdfUploadBase64Request(BaseModel):
    filename: str
    data: str  # base64-encoded PDF bytes


@router.post("/resume/upload-pdf-base64", summary="Upload a PDF resume via base64 JSON (works through proxy bridge)")
async def upload_resume_pdf_base64(req: PdfUploadBase64Request):
    """
    Accept a PDF resume as base64-encoded data inside a JSON payload.

    This endpoint exists so the Electron frontend can send PDFs through the
    IPC bridge (which speaks JSON) instead of using a direct `fetch()` with
    FormData, which can fail due to CORS / web security when the renderer
    targets a remote (cloud) backend.

    Decodes the base64 → extracts text via PyMuPDF → saves as ~/career-ops/cv.md.
    """
    import base64
    from pathlib import Path

    # Validate filename
    if not req.filename or not req.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    # Decode base64 payload
    try:
        pdf_bytes = base64.b64decode(req.data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 data")

    if len(pdf_bytes) < 100:
        raise HTTPException(status_code=400, detail="PDF file appears empty")
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF file exceeds 10MB limit")

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        extracted_pages = []
        for page_num, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                extracted_pages.append(text.strip())
        doc.close()

        if not extracted_pages or all(len(p.strip()) < 20 for p in extracted_pages):
            raise HTTPException(
                status_code=400,
                detail="No extractable text found in PDF.",
            )

        full_text = "\n\n".join(extracted_pages)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)
        full_text = full_text.strip()

        if len(full_text) < 50:
            raise HTTPException(status_code=400, detail=f"Only {len(full_text)} chars extracted — PDF may be scanned.")

        from .resume_parser import DEFAULT_RESUME_PATH, clear_parse_cache
        path = Path(DEFAULT_RESUME_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(full_text, encoding="utf-8")
        clear_parse_cache()

        await analytics_dao.log_activity(
            "job", "resume_upload_pdf",
            f"Resume parsed from PDF (base64) ({len(full_text)} chars, {len(extracted_pages)} pages)",
        )

        from .resume_parser import parse_resume
        parsed = parse_resume()

        return {
            "status": "saved",
            "message": f"Resume extracted from PDF — {len(full_text)} characters, {len(extracted_pages)} pages",
            "path": str(path),
            "char_count": len(full_text),
            "page_count": len(extracted_pages),
            "parsed": {
                "full_name": parsed.get("full_name", ""),
                "skills_count": len(parsed.get("skills", [])),
                "experience_count": len(parsed.get("experience", [])),
            },
        }

    except ImportError:
        raise HTTPException(status_code=500, detail="PDF text extraction requires PyMuPDF")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse PDF: {str(e)}")


@router.get("/resume", summary="Get the current resume content and parse status")
async def get_resume():
    """Get the current resume file content and parsed data."""
    try:
        from .resume_parser import DEFAULT_RESUME_PATH, parse_resume
        parsed = parse_resume()
        path_exists = bool(parsed.get("raw_md"))
        return {
            "exists": path_exists,
            "path": DEFAULT_RESUME_PATH,
            "parsed": {
                "full_name": parsed.get("full_name", ""),
                "email": parsed.get("email", ""),
                "skills_count": len(parsed.get("skills", [])),
                "experience_count": len(parsed.get("experience", [])),
                "education_count": len(parsed.get("education", [])),
            },
            "char_count": len(parsed.get("raw_md", "")),
            "error": parsed.get("_error", ""),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Pipeline Endpoints ────────────────────────────────────────────

class PipelineSettingsRequest(BaseModel):
    mode: str = "notify"
    auto_apply: bool = False
    max_per_run: int = 10
    generate_pdf: bool = True
    send_telegram: bool = True
    min_match_score: int = 60


@router.post("/pipeline/run")
async def run_application_pipeline(settings: Optional[PipelineSettingsRequest] = None):
    """
    Execute the end-to-end job application pipeline.

    Processes queued/approved jobs by:
    1. Parsing resume from ~/career-ops/cv.md
    2. Optimizing resume for each specific job
    3. Generating tailored cover letters
    4. Generating PDF documents
    5. Sending Telegram notification with job link + docs
       OR auto-applying via Playwright

    Returns real-time progress via GET /jobs/pipeline/progress
    """
    import asyncio

    # Check if pipeline is already running
    progress = get_pipeline_progress()
    if progress["status"] == "running":
        return {"status": "already_running", "message": "Pipeline is already running", "progress": progress}

    cfg = settings.model_dump() if settings else {}

    # Run pipeline as a background task
    async def _run():
        try:
            await run_pipeline(cfg)
        except Exception as e:
            print(f"[Routes] Pipeline background task error: {e}")

    asyncio.create_task(_run())

    return {
        "status": "started",
        "message": "Job application pipeline started in background",
        "settings": cfg or get_pipeline_settings(),
    }


@router.get("/pipeline/progress")
async def pipeline_progress():
    """Get real-time progress of the running pipeline."""
    return get_pipeline_progress()


@router.get("/pipeline/settings")
async def pipeline_settings():
    """Get current pipeline settings."""
    return get_pipeline_settings()


@router.post("/pipeline/retry")
async def retry_failed_jobs(background_tasks: BackgroundTasks):
    """Retry all failed applications that haven't exceeded max retries."""
    from .pipeline import get_pipeline_progress, DEFAULT_SETTINGS as PIPELINE_SETTINGS

    progress = get_pipeline_progress()
    if progress["status"] == "running":
        return {"status": "already_running", "message": "Pipeline is already running"}

    # Get retryable failed jobs
    max_retries = PIPELINE_SETTINGS.get("max_retries", 3)
    failed = await jobs_dao.get_applications_by_status("failed", limit=50)
    retryable = [
        app for app in failed
        if (app.get("retry_count") or 0) < max_retries
    ]

    if not retryable:
        return {"status": "no_retryable", "message": "No failed jobs eligible for retry"}

    # Reset their status to queued so the next pipeline run picks them up
    for app in retryable:
        await jobs_dao.update_application_status(app["id"], "queued")

    await analytics_dao.log_activity(
        "job", "retry_triggered",
        f"Reset {len(retryable)} failed applications to queued for retry"
    )

    # Trigger the pipeline
    from .pipeline import run_pipeline as _run_pipeline
    asyncio.create_task(_run_pipeline())

    return {
        "status": "started",
        "retry_count": len(retryable),
        "message": f"Retrying {len(retryable)} failed jobs",
    }


@router.get("/pipeline/retry/status")
async def retry_status():
    """Get count of retryable failed jobs."""
    from .pipeline import DEFAULT_SETTINGS as PIPELINE_SETTINGS
    max_retries = PIPELINE_SETTINGS.get("max_retries", 3)
    failed = await jobs_dao.get_applications_by_status("failed", limit=100)
    retryable = [
        app for app in failed
        if (app.get("retry_count") or 0) < max_retries
    ]
    exhausted = [
        app for app in failed
        if (app.get("retry_count") or 0) >= max_retries
    ]
    return {
        "retryable_count": len(retryable),
        "exhausted_count": len(exhausted),
        "max_retries": max_retries,
    }


@router.get("/scan/history")
async def scan_history(hours: int = 24):
    """Get scan history from the activity log for the last N hours."""
    try:
        rows = await db_connection.fetch_all(
            """SELECT id, action, description, metadata, severity, created_at
               FROM activity_log
               WHERE type = 'job' AND action IN ('scan', 'scan_error')
                 AND created_at >= datetime('now', ? || ' hours', 'localtime')
               ORDER BY created_at DESC
               LIMIT 20""",
            (f"-{hours}",),
        )
        return {
            "scans": [
                {
                    "id": r["id"],
                    "action": r["action"],
                    "description": r["description"],
                    "severity": r["severity"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "count": len(rows),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Auto-Scan Scheduler ──────────────────────────────────────────────────

# Module-level state for the auto-scan background task
_auto_scan_enabled: bool = False
_auto_scan_task: asyncio.Task | None = None


def _get_auto_scan_interval() -> int:
    """Get the auto-scan interval in seconds from config."""
    from config import get_settings
    return max(1800, get_settings().job_scan_interval_hours * 3600)  # Min 30 min


async def _auto_scan_loop():
    """Background loop that periodically triggers job scans."""
    global _auto_scan_enabled
    while _auto_scan_enabled:
        interval = _get_auto_scan_interval()
        print(f"[AutoScan] Next scan in {interval // 3600}h {(interval % 3600) // 60}m")
        await asyncio.sleep(interval)
        if not _auto_scan_enabled:
            break
        try:
            print("[AutoScan] Starting scheduled scan...")
            await _run_scan()
            print("[AutoScan] Scheduled scan completed")
        except Exception as e:
            print(f"[AutoScan] Scan failed: {e}")


@router.post("/scan/auto-toggle")
async def toggle_auto_scan(data: dict):
    """Enable or disable the automatic hourly job scan."""
    global _auto_scan_enabled, _auto_scan_task
    try:
        enable = bool(data.get("enabled", False))

        if enable and not _auto_scan_enabled:
            _auto_scan_enabled = True
            _auto_scan_task = asyncio.create_task(_auto_scan_loop())
            await analytics_dao.log_activity(
                "job", "auto_scan_enabled",
                f"Auto-scan enabled (every {_get_auto_scan_interval() // 60} min)",
            )
            return {"status": "enabled", "interval_minutes": _get_auto_scan_interval() // 60}

        elif not enable and _auto_scan_enabled:
            _auto_scan_enabled = False
            if _auto_scan_task and not _auto_scan_task.done():
                _auto_scan_task.cancel()
                _auto_scan_task = None
            await analytics_dao.log_activity(
                "job", "auto_scan_disabled", "Auto-scan disabled"
            )
            return {"status": "disabled"}

        return {"status": "enabled" if _auto_scan_enabled else "disabled"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scan/auto-status")
async def auto_scan_status():
    """Check whether auto-scan is currently enabled."""
    return {
        "enabled": _auto_scan_enabled,
        "interval_seconds": _get_auto_scan_interval(),
        "interval_minutes": _get_auto_scan_interval() // 60,
        "interval_hours": _get_auto_scan_interval() / 3600,
    }


# ─── Resume-Based Job Role Suggestions ───────────────────────────────


async def _generate_role_suggestions() -> list[dict]:
    """
    Analyze the parsed resume and suggest job roles the user should target.

    Uses the LLM (via OllamaClient) for intelligent suggestions, falls back
    to a keyword-based approach when the LLM is unavailable.
    """
    from .resume_parser import parse_resume
    from utils.ollama_client import OllamaClient
    import json

    resume = parse_resume()
    if resume.get("_error"):
        return []

    skills = resume.get("skills", [])
    experience = resume.get("experience", [])
    education = resume.get("education", [])
    summary = resume.get("summary", "") or resume.get("headline", "")

    # Build skill summary
    skill_text = ", ".join(skills[:30]) if skills else "Not listed"

    # Build experience summary
    exp_text = ""
    for exp in experience[:4]:
        role = exp.get("role", "")
        company = exp.get("company", "")
        bullets = exp.get("bullets", [])
        exp_text += f"- {role} at {company}\n"
        for b in bullets[:3]:
            exp_text += f"  • {b[:100]}\n"

    # Build education summary
    edu_text = ""
    for edu in education[:2]:
        edu_text += f"- {edu.get('title', '')}\n"

    # Try LLM-based suggestions first
    try:
        client = OllamaClient(temperature=0.3)
        prompt = f"""Analyze this resume and suggest 6 job roles the person should apply for.

SKILLS:
{skill_text}

EXPERIENCE:
{exp_text}

EDUCATION:
{edu_text}

SUMMARY:
{summary[:300]}

Respond ONLY with a JSON array. Each entry: {{"title": "Job Title", "match_score": 0-100, "reasoning": "Why this fits", "matched_skills": ["skill1", "skill2"]}}"""

        content = await client.chat([
            {"role": "system", "content": "You are a career coach. Analyze resumes and suggest fitting job roles. Respond ONLY with valid JSON array."},
            {"role": "user", "content": prompt},
        ])

        # Extract JSON from markdown code fences if present
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
        if json_match:
            content = json_match.group(1)
        # Parse JSON response
        suggestions = json.loads(content)
        if isinstance(suggestions, list):
            return suggestions[:8]
    except Exception:
        pass

    # Fallback: keyword-based suggestions
    return _keyword_suggestions(skills, experience)


def _keyword_suggestions(skills: list[str], experience: list[dict]) -> list[dict]:
    """Generate role suggestions based on keyword matching against known roles."""
    skill_lower = " ".join(s.lower() for s in skills)
    exp_text = " "
    for exp in experience:
        exp_text += f" {exp.get('role', '')} {exp.get('company', '')} "
        for b in exp.get("bullets", []):
            exp_text += f" {b}"
    exp_text = exp_text.lower()
    combined = f"{skill_lower} {exp_text}"

    # Define role profiles with keywords
    role_profiles = [
        ("Full Stack Developer", ["react", "node", "typescript", "javascript", "python", "frontend", "backend", "fullstack", "angular", "vue", "api", "rest", "css", "html"], 95),
        ("Backend Engineer", ["backend", "api", "microservice", "python", "java", "golang", "node", "c#", ".net", "rest", "grpc", "sql", "nosql", "postgres", "mongodb", "redis", "kafka", "rabbitmq", "aws", "docker"], 85),
        ("Frontend Engineer", ["react", "angular", "vue", "typescript", "javascript", "css", "html", "frontend", "ui", "ux", "tailwind", "framer", "webpack", "next"], 75),
        ("Software Engineer", ["software", "engineer", "developer", "python", "java", "c++", "golang", "rust", "system", "design", "algorithms", "data", "structure"], 90),
        ("DevOps Engineer", ["devops", "docker", "kubernetes", "k8s", "aws", "gcp", "azure", "ci/cd", "pipeline", "infrastructure", "terraform", "ansible", "linux", "shell"], 70),
        ("Data Engineer", ["data", "pipeline", "etl", "spark", "hadoop", "sql", "nosql", "python", "airflow", "kafka", "big", "analytics", "warehouse"], 65),
        ("Machine Learning Engineer", ["machine", "learning", "ml", "ai", "deep", "tensorflow", "pytorch", "python", "data", "model", "nlp", "neural", "llm"], 60),
        ("Cloud Engineer", ["aws", "gcp", "azure", "cloud", "ec2", "s3", "lambda", "serverless", "docker", "kubernetes", "terraform", "infrastructure"], 70),
        ("Tech Lead", ["lead", "architect", "mentor", "team", "system", "design", "microservice", "scalable", "distributed", "technical"], 55),
        ("Solutions Architect", ["architect", "solution", "system", "design", "scalable", "distributed", "aws", "cloud", "microservice", "technical"], 50),
    ]

    suggestions = []
    for title, keywords, base_score in role_profiles:
        matched = [kw for kw in keywords if kw in combined]
        match_count = len(matched)
        total = len(keywords)
        # Proportional scoring: base_score * (matched/total), capped at 85% max.
        # No multiplier — if you match 8/14 keywords, you get 8/14 of base_score.
        score = min(int(base_score * (match_count / max(total, 1))), 85)
        if score >= 20:
            suggestions.append({
                "title": title,
                "match_score": score,
                "reasoning": f"Matched {match_count}/{total} relevant keywords",
                "matched_skills": matched[:6],
            })

    suggestions.sort(key=lambda x: x["match_score"], reverse=True)
    return suggestions[:8]


@router.get("/suggestions")
async def get_role_suggestions():
    """
    Analyze the user's resume and suggest job roles they should target.

    Uses LLM (via OllamaClient) for intelligent suggestions with reasoning.
    Falls back to keyword-based matching when LLM is unavailable.
    Returns a list of suggested roles with match scores, reasoning,
    and matched skills.
    """
    try:
        from .resume_parser import parse_resume

        resume = parse_resume()
        name = resume.get("full_name", "User")
        skills = resume.get("skills", [])
        experience = resume.get("experience", [])

        suggestions = await _generate_role_suggestions()

        return {
            "suggestions": suggestions,
            "resume_info": {
                "name": name,
                "skills_count": len(skills),
                "experience_count": len(experience),
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def job_status():
    """Get current job search status from the database."""
    try:
        counts = await jobs_dao.get_application_count_by_status()
        status_map = {row["status"]: row["count"] for row in counts}
        row = await db_connection.fetch_one(
            "SELECT COUNT(*) as count FROM job_listings"
        )
        total_scanned = row["count"] if row else 0
        return {
            "is_scanning": False,
            "auto_scan": _auto_scan_enabled,
            "total_jobs_scanned": total_scanned,
            "pending_review": status_map.get("ready_for_review", 0),
            "applications_queued": status_map.get("queued", 0),
            "applications_submitted": status_map.get("submitted", 0),
            "interviews": status_map.get("interview", 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Job Detail ──────────────────────────────────────────────────────────
# Registered LAST so the dynamic /{job_id} path never shadows the literal
# GET routes above (/matches, /applications, /status, ...).


@router.get("/{job_id}")
async def get_job_detail(job_id: int):
    """Get full details for a single job listing.

    Returns the raw listing plus its latest evaluation (match score, reasoning,
    pros/cons) and the real application status — the one-stop detail view the
    voice agent and frontend use to describe a specific job.
    """
    try:
        job = await jobs_dao.get_job_listing(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job #{job_id} not found")

        evaluation = await db_connection.fetch_one(
            "SELECT overall_score, match_percentage, reasoning, pros, cons, "
            "       evaluated_by, evaluated_at "
            "FROM job_evaluations WHERE job_listing_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        app_statuses = await jobs_dao.get_application_statuses_for_jobs([job_id])

        return {
            "job": job,
            "evaluation": dict(evaluation) if evaluation else None,
            "application_status": app_statuses.get(job_id, "new"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
