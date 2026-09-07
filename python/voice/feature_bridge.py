"""
BARQ Feature Bridge — connects the voice/audio pipeline to every BARQ feature.

The voice function registry (``voice/function_executor.py``) historically only
exposed OS / browser / vision / media helpers. This module adds a generic
bridge so spoken commands can reach *every* BARQ feature:

- ``barq_api`` — call any BARQ backend HTTP endpoint (jobs, social, memory,
  brain, workflows, notifications, analytics, settings, knowledge, ...).
- ``barq_skills_list`` — discover which agent skills are registered.
- ``barq_skill`` — invoke any registered agent skill by name.
- Curated convenience wrappers for the most common voice intents
  (remember/recall memory, brain summary, notify, job matches, social trends,
  workflow run/status, briefing, weekly review, analytics).

All functions are **synchronous** because the voice executor runs registry
functions via ``asyncio.to_thread``. Each returns a dict with a ``status`` key
(``"success"`` / ``"error"``) matching the rest of the voice registry.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# ─── Low-level self-HTTP dispatcher ─────────────────────────────────────


def _barq_base_url() -> str:
    """Build the base URL of BARQ's own backend from config settings."""
    from config import get_settings

    settings = get_settings()
    return f"http://{settings.host}:{settings.port}"


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform a synchronous HTTP call to BARQ's own backend.

    Returns a dict with a ``status`` key — never raises.
    """
    import httpx

    url = _barq_base_url() + path
    try:
        with httpx.Client(timeout=15.0) as client:
            if method.upper() == "GET":
                resp = client.get(url, params=payload or {})
            else:
                resp = client.post(url, json=payload or {})
        try:
            data: Any = resp.json()
        except Exception:
            data = resp.text[:2000]
        if resp.status_code >= 400:
            detail = data.get("detail", data) if isinstance(data, dict) else data
            return {"status": "error", "detail": f"HTTP {resp.status_code}: {detail}"}
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "detail": f"BARQ backend unreachable: {e}"}


def _run_coro(coro: Any) -> Any:
    """Run a coroutine synchronously from a worker-thread context."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except Exception:
        return asyncio.run(coro)


# ─── Generic dispatcher ──────────────────────────────────────────────────


def barq_api(method: str = "GET", path: str = "/", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call any BARQ feature endpoint by method + path.

    Examples:
        barq_api("GET", "/jobs/matches")
        barq_api("POST", "/agent/briefing/run", {"notify": True})

    Args:
        method: HTTP method — "GET" or "POST".
        path: Backend path (e.g. "/jobs/matches", "/social/trends").
        payload: For GET, query params. For POST, the JSON body.
    """
    method = (method or "GET").upper()
    if method not in ("GET", "POST"):
        return {"status": "error", "detail": "method must be 'GET' or 'POST'"}
    if not path.startswith("/"):
        path = "/" + path
    return _request(method, path, payload or {})


# ─── Skill Registry bridge ───────────────────────────────────────────────


def barq_skills_list(category: str = "") -> dict[str, Any]:
    """List all registered agent skills (optionally filtered by category)."""
    try:
        from agent.skill_registry import get_skill_registry

        registry = get_skill_registry()
        skills = [
            {"name": s.name, "description": s.description, "category": s.category}
            for s in registry.list(category or None)
        ]
        return {"status": "success", "data": {"skills": skills, "count": len(skills)}}
    except Exception as e:
        return {"status": "error", "detail": f"Failed to list skills: {e}"}


def barq_skill(name: str, params: str = "") -> dict[str, Any]:
    """Invoke any registered agent skill by name.

    Args:
        name: The skill name (see ``barq_skills_list`` for options).
        params: Optional JSON object string of keyword arguments,
                e.g. '{"query": "AI news"}'.
    """
    if not name:
        return {"status": "error", "detail": "Skill name is required."}
    try:
        from agent.skill_registry import get_skill_registry

        registry = get_skill_registry()
        if registry.get(name) is None:
            return {
                "status": "error",
                "detail": f"Unknown skill '{name}'. Use barq_skills_list to see available skills.",
            }

        kwargs: dict[str, Any] = {}
        if params:
            try:
                parsed = json.loads(params)
                if isinstance(parsed, dict):
                    kwargs = parsed
                else:
                    return {"status": "error", "detail": "params must be a JSON object string."}
            except json.JSONDecodeError:
                return {"status": "error", "detail": "params must be valid JSON, e.g. '{\"query\": \"AI news\"}'"}

        result = _run_coro(registry.call(name, **kwargs))
        return {"status": "success", "detail": str(result)[:2000]}
    except Exception as e:
        return {"status": "error", "detail": f"Skill '{name}' failed: {e}"}


# ─── Curated convenience wrappers ────────────────────────────────────────


def barq_remember(key: str, value: str, category: str = "general") -> dict[str, Any]:
    """Store a fact in BARQ's long-term memory.

    Args:
        key: Short unique name for the memory (e.g. 'user_birthday').
        value: The fact to remember.
        category: Optional category (default 'general').
    """
    if not key or not value:
        return {"status": "error", "detail": "Both 'key' and 'value' are required."}
    return _request("POST", "/memory/memory", {"key": key, "value": value, "category": category})


def barq_recall(query: str, limit: int = 10) -> dict[str, Any]:
    """Search BARQ's long-term memory for stored facts."""
    if not query:
        return {"status": "error", "detail": "A search 'query' is required."}
    return _request("GET", "/memory/memory/search", {"query": query, "limit": limit})


def barq_brain_summary() -> dict[str, Any]:
    """Get a summary of the knowledge brain (nodes/connections)."""
    return _request("GET", "/api/brain/list")


def barq_notify(
    title: str,
    body: str,
    priority: str = "normal",
    channel: str = "all",
    category: str = "general",
) -> dict[str, Any]:
    """Send a notification (telegram / email / desktop).

    Args:
        title: Notification title.
        body: Notification body text.
        priority: low, normal, high, or urgent.
        channel: telegram, email, desktop, or all.
        category: general, job_match, application, content, analytics, error, system.
    """
    if not title or not body:
        return {"status": "error", "detail": "Both 'title' and 'body' are required."}
    return _request(
        "POST",
        "/notifications/send",
        {
            "title": title,
            "body": body,
            "priority": priority,
            "channel": channel,
            "category": category,
        },
    )


def barq_job_matches(min_score: float = 3.0, limit: int = 20) -> dict[str, Any]:
    """Get the current job matches from the job scanner."""
    return _request("GET", "/jobs/matches", {"min_score": min_score, "limit": limit})


def barq_job_scan() -> dict[str, Any]:
    """Trigger a background scan of all configured job boards for new listings.

    Runs asynchronously on the backend; poll ``GET /jobs/scan/progress`` via
    ``barq_api`` for live progress.  Returns immediately with a status.
    """
    return _request("POST", "/jobs/scan", {})


def barq_job_details(job_id: int) -> dict[str, Any]:
    """Get full details for a specific job listing.

    Includes the raw listing, its latest match evaluation (score, reasoning,
    pros/cons), and the real application status.

    Args:
        job_id: The job listing id (from ``barq_job_matches`` results).
    """
    if not job_id:
        return {"status": "error", "detail": "A job_id is required."}
    # No int() coercion here — a non-numeric value must never raise. The
    # backend declares job_id: int, so it coerces numeric strings and returns
    # 422 for garbage, which _request surfaces as an error dict.
    return _request("GET", f"/jobs/{job_id}")


def barq_apply_preview(job_id: int) -> dict[str, Any]:
    """Safe-mode: fill a job's application form and screenshot WITHOUT submitting.

    Opens the job's URL in the user's real browser profile, detects the ATS
    platform, fills the form from the parsed resume, and returns a screenshot
    + filled-fields summary for human review.  Nothing is submitted — callers
    review, then decide whether to apply for real.

    Args:
        job_id: The job listing id to preview.
    """
    if not job_id:
        return {"status": "error", "detail": "A job_id is required."}
    # No int() coercion here — a non-numeric value must never raise. The
    # backend declares job_id: int, so it coerces numeric strings and returns
    # 422 for garbage, which _request surfaces as an error dict.
    return _request("POST", f"/jobs/{job_id}/apply/preview")


def barq_social_trends() -> dict[str, Any]:
    """Get the current social media trending topics."""
    return _request("GET", "/social/trends")


def barq_workflow_run(name: str, context: dict[str, Any] | None = None, background: bool = False) -> dict[str, Any]:
    """Run a registered agent workflow by name.

    Args:
        name: Workflow name (e.g. 'morning_briefing', 'weekly_review').
        context: Optional dict of inputs the workflow expects.
        background: Run asynchronously (returns immediately) if True.
    """
    if not name:
        return {"status": "error", "detail": "A workflow 'name' is required."}
    return _request(
        "POST",
        f"/agent/workflows/{name}/run",
        {"context": context or {}, "background": background},
    )


def barq_workflow_status(run_id: str) -> dict[str, Any]:
    """Get the status of a workflow run by its run_id."""
    if not run_id:
        return {"status": "error", "detail": "A 'run_id' is required."}
    return _request("GET", f"/agent/workflows/runs/{run_id}")


def barq_briefing(notify: bool = False) -> dict[str, Any]:
    """Generate the morning briefing report."""
    return _request("POST", "/agent/briefing/run", {"notify": notify})


def barq_weekly_review(notify: bool = False, days: int = 7) -> dict[str, Any]:
    """Generate the weekly review report (analytics + skill rates + memory)."""
    return _request("POST", "/agent/review/weekly", {"notify": notify, "days": days})


def barq_analytics(kind: str = "activity", limit: int = 50) -> dict[str, Any]:
    """Get an analytics snapshot.

    Args:
        kind: activity (default), career, social, or revenue.
        limit: Max rows for activity.
    """
    kind = (kind or "activity").lower()
    if kind in ("career", "social", "revenue"):
        return _request("GET", f"/analytics/{kind}")
    return _request("GET", "/analytics/activity", {"limit": limit})


# ─── Navigation Voice Skill ─────────────────────────────────────────────
# Routes to any BARQ page by name.  The voice agent calls this when the
# user asks to open/navigate to a specific page or feature.

_PAGE_ROUTES: dict[str, str] = {
    "dashboard": "/dashboard", "home": "/dashboard", "command": "/dashboard",
    "analytics": "/analytics", "insights": "/analytics",
    "jobs": "/jobs", "job": "/jobs", "careers": "/jobs", "career": "/jobs",
    "recruitment": "/jobs", "apply": "/jobs",
    "social": "/content", "content": "/content", "social media": "/content",
    "files": "/files", "file": "/files", "explorer": "/files",
    "dev": "/dev", "developer": "/dev", "terminal": "/dev", "code": "/dev",
    "system": "/system", "hardware": "/system", "monitor": "/system",
    "web": "/web", "browser": "/web", "weather": "/web", "stocks": "/web",
    "phone": "/phone", "mobile": "/phone",
    "research": "/research", "search": "/research", "deep research": "/research",
    "docs": "/docs", "documents": "/docs", "documentation": "/docs",
    "chat": "/chat", "conversation": "/chat", "talk": "/chat",
    "memory": "/memory", "notes & storage": "/memory", "storage": "/memory",
    "widgets": "/widgets", "widget": "/widgets",
    "settings": "/settings", "preferences": "/settings", "config": "/settings",
    "agent": "/agent", "ai agent": "/agent",
    "workflows": "/workflows", "workflow": "/workflows", "automations": "/workflows",
    "vision": "/vision", "camera": "/vision", "screen analysis": "/vision",
    "brain": "/brain", "knowledge": "/brain", "knowledge graph": "/brain",
    "apis": "/apis", "api": "/apis", "public apis": "/apis",
    "integrations": "/apis",
    "evolution": "/evolution", "evolve": "/evolution", "learning": "/evolution",
    "notes": "/notes", "note": "/notes", "notepad": "/notes",
    "gallery": "/gallery", "images": "/gallery", "photos": "/gallery",
}


def barq_navigate(page: str = "") -> dict[str, Any]:
    """Navigate the BARQ frontend to a specific page or feature.

    Call this when the user says "open", "go to", "show", "navigate to",
    or wants to switch to a specific BARQ page.  Returns the route so the
    frontend can perform the navigation.

    Available pages: dashboard, analytics, jobs, social/content, files, dev,
    system, web, phone, research, docs, chat, memory, widgets, settings,
    agent, workflows, vision, brain, apis, evolution, notes, gallery.
    """
    if not page:
        return {"status": "error", "detail": "A page name is required."}

    page_lower = page.strip().lower()
    route = _PAGE_ROUTES.get(page_lower)
    if not route:
        # Fuzzy: check if any key is a substring of the input
        for key, r in _PAGE_ROUTES.items():
            if key in page_lower or page_lower in key:
                route = r
                break
    if not route:
        available = sorted(set(_PAGE_ROUTES.values()))
        return {
            "status": "error",
            "detail": f"Unknown page '{page}'. Available: {', '.join(sorted(_PAGE_ROUTES.keys()))}",
        }

    # Notify the Electron main process via HTTP so the renderer navigates.
    try:
        import httpx as _httpx
        base = _barq_base_url()
        # The Electron main process listens on /electron/navigate — but since
        # the Python backend cannot directly call Electron IPC, we return the
        # route and let the voice pipeline broadcast it via WebSocket.
        # The frontend picks it up and navigates.
        pass  # route is returned in the response for the frontend to use
    except Exception:
        pass

    return {
        "status": "success",
        "route": route,
        "page": page_lower,
        "detail": f"Navigating to {page} ({route})",
    }


# ─── Per-feature voice skills ─────────────────────────────────────────────
# Convenience wrappers so the voice agent can directly invoke each
# BARQ feature without knowing the raw HTTP endpoint.


def barq_job_search(keywords: str = "", location: str = "") -> dict[str, Any]:
    """Search for jobs with keywords and/or location."""
    payload: dict[str, Any] = {}
    if keywords:
        payload["keywords"] = keywords
    if location:
        payload["location"] = location
    return _request("POST", "/jobs/scan", payload)


def barq_job_application_status() -> dict[str, Any]:
    """Get the status of all job applications."""
    return _request("GET", "/jobs/applications")


def barq_social_post(content: str, platforms: str = "all") -> dict[str, Any]:
    """Create and post social media content."""
    return _request("POST", "/social/generate-script", {"topic": content, "format": platforms})


def barq_social_schedule(video_id: int, platforms: str = "all", date: str = "") -> dict[str, Any]:
    """Schedule a social media post."""
    return _request("POST", "/social/calendar/schedule", {
        "video_id": video_id, "platforms": [platforms], "scheduled_date": date,
    })


def barq_file_search(query: str = "") -> dict[str, Any]:
    """Search for files in the workspace."""
    return _request("GET", "/files/search", {"query": query})


def barq_web_search(query: str = "") -> dict[str, Any]:
    """Search the web via BARQ's web skill."""
    return _request("POST", "/agent/skills/web_search/run", {"query": query})


def barq_research(query: str = "") -> dict[str, Any]:
    """Start a deep research task."""
    return _request("POST", "/agent/skills/deep_research/run", {"query": query})


def barq_docs_search(query: str = "") -> dict[str, Any]:
    """Search documentation."""
    return _request("GET", "/docs/search", {"query": query})


def barq_chat_send(message: str = "") -> dict[str, Any]:
    """Send a message to BARQ's chat AI."""
    if not message:
        return {"status": "error", "detail": "A message is required."}
    return _request("POST", "/voice/chat/text", {"message": message})


def barq_memory_list() -> dict[str, Any]:
    """List all stored memories."""
    return _request("GET", "/memory/memory")


def barq_memory_store(key: str = "", value: str = "", category: str = "general") -> dict[str, Any]:
    """Store a new memory."""
    if not key or not value:
        return {"status": "error", "detail": "Both key and value are required."}
    return _request("POST", "/memory/memory", {"key": key, "value": value, "category": category})


def barq_notes_list() -> dict[str, Any]:
    """List all notes."""
    return _request("GET", "/memory/notes")


def barq_notes_create(title: str = "", content: str = "", tags: str = "") -> dict[str, Any]:
    """Create a new note."""
    if not title:
        return {"status": "error", "detail": "A title is required."}
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    return _request("POST", "/memory/notes", {"title": title, "content": content, "tags": tag_list})


def barq_widget_list() -> dict[str, Any]:
    """List available widgets."""
    return _request("GET", "/widgets")


def barq_settings_get(section: str = "") -> dict[str, Any]:
    """Get settings for a section (voice, jobs, social, etc.)."""
    path = "/settings" + (f"/{section}" if section else "")
    return _request("GET", path)


def barq_settings_set(key: str = "", value: str = "", section: str = "general") -> dict[str, Any]:
    """Update a setting."""
    if not key or not value:
        return {"status": "error", "detail": "Both key and value are required."}
    return _request("POST", "/settings", {"key": key, "value": value, "section": section})


def barq_agent_run(task: str = "") -> dict[str, Any]:
    """Run an agent task."""
    if not task:
        return {"status": "error", "detail": "A task description is required."}
    return _request("POST", "/agent/run", {"task": task})


def barq_vision_analyze(prompt: str = "What do you see?", source: str = "screen") -> dict[str, Any]:
    """Analyze screen or camera using vision AI."""
    return _request("POST", "/vision/analyze", {"prompt": prompt, "source": source})


def barq_evolution_status() -> dict[str, Any]:
    """Get the AI evolution/learning status."""
    return _request("GET", "/evolution/status")


def barq_apis_list() -> dict[str, Any]:
    """List available public APIs and integrations."""
    return _request("GET", "/apis")


def barq_system_status() -> dict[str, Any]:
    """Get full system status (CPU, RAM, disk, GPU)."""
    return _request("GET", "/system/status")


def barq_phone_send(message: str = "", to: str = "") -> dict[str, Any]:
    """Send a message via phone/mobile integration."""
    return _request("POST", "/phone/send", {"message": message, "to": to})


# ─── Second Brain Integration ──────────────────────────────────────────────


def barq_sb_search(query: str = "", mode: str = "hybrid", limit: int = 10) -> dict[str, Any]:
    """Search Second Brain for notes, code, bookmarks, and tasks.

    Modes:
    - text: SQLite FTS5 full-text search (fast, keyword-based)
    - semantic: Vector similarity with MiniLM-L6-v2 (meaning-based)
    - hybrid: Combined 50/50 weighted (best of both)
    """
    payload: dict[str, Any] = {"query": query, "mode": mode, "limit": limit}
    return _request("POST", "/second-brain/search", payload)


def barq_sb_items(item_type: str = "", limit: int = 20) -> dict[str, Any]:
    """List items from Second Brain.

    Item types: note, code, bookmark, task.
    """
    params: dict[str, Any] = {"limit": limit}
    if item_type:
        params["item_type"] = item_type
    return _request("GET", "/second-brain/items", params)


def barq_sb_create(title: str = "", content: str = "", item_type: str = "note", tags: str = "") -> dict[str, Any]:
    """Create a new item in Second Brain.

    Item types: note, code, bookmark, task.
    Tags should be comma-separated (e.g. 'python, ai, research').
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    return _request("POST", "/second-brain/items", {
        "item_type": item_type, "title": title,
        "content": content, "tags": tag_list,
    })


def barq_sb_chat(message: str = "") -> dict[str, Any]:
    """Chat with Second Brain's Gemini AI.

    The AI searches your Second Brain for relevant context before answering,
    so responses are grounded in your stored knowledge.
    """
    return _request("POST", "/second-brain/chat", {"message": message}, timeout=30.0)


def barq_sb_sync(direction: str = "both") -> dict[str, Any]:
    """Sync knowledge between BARQ and Second Brain.

    Directions:
    - to_items: Push BARQ brain triplets as Second Brain items
    - to_triplets: Pull Second Brain items into BARQ brain triplets
    - both: Bidirectional sync (default)
    """
    return _request("POST", "/second-brain/sync/full", {"direction": direction})


def barq_sb_sync_status() -> dict[str, Any]:
    """Get sync status including auto-sync state, last sync, and mapping stats."""
    return _request("GET", "/second-brain/sync/status")


def barq_sb_sync_start(interval: int = 300) -> dict[str, Any]:
    """Start auto-sync between BARQ and Second Brain.

    Interval is in seconds (minimum 60, default 300 = 5 minutes).
    """
    return _request("POST", "/second-brain/sync/auto/start", {"interval_seconds": interval})


def barq_sb_sync_stop() -> dict[str, Any]:
    """Stop the auto-sync scheduler."""
    return _request("POST", "/second-brain/sync/auto/stop")


def barq_sb_status() -> dict[str, Any]:
    """Check Second Brain connection status and stats."""
    return _request("GET", "/second-brain/status")


# ─── Registry + Schema exports ───────────────────────────────────────────

FEATURE_FUNCTIONS: dict[str, Any] = {
    # Navigation
    "barq_navigate": barq_navigate,
    # Generic dispatcher
    "barq_api": barq_api,
    # Skills
    "barq_skills_list": barq_skills_list,
    "barq_skill": barq_skill,
    # Memory & Knowledge
    "barq_remember": barq_remember,
    "barq_recall": barq_recall,
    "barq_memory_list": barq_memory_list,
    "barq_memory_store": barq_memory_store,
    "barq_brain_summary": barq_brain_summary,
    # Notifications
    "barq_notify": barq_notify,
    # Jobs
    "barq_job_matches": barq_job_matches,
    "barq_job_scan": barq_job_scan,
    "barq_job_details": barq_job_details,
    "barq_job_search": barq_job_search,
    "barq_job_application_status": barq_job_application_status,
    "barq_apply_preview": barq_apply_preview,
    # Social / Content
    "barq_social_trends": barq_social_trends,
    "barq_social_post": barq_social_post,
    "barq_social_schedule": barq_social_schedule,
    # Files
    "barq_file_search": barq_file_search,
    # Web & Research
    "barq_web_search": barq_web_search,
    "barq_research": barq_research,
    # Docs
    "barq_docs_search": barq_docs_search,
    # Chat
    "barq_chat_send": barq_chat_send,
    # Notes
    "barq_notes_list": barq_notes_list,
    "barq_notes_create": barq_notes_create,
    # Widgets
    "barq_widget_list": barq_widget_list,
    # Settings
    "barq_settings_get": barq_settings_get,
    "barq_settings_set": barq_settings_set,
    # Agent
    "barq_agent_run": barq_agent_run,
    # Workflows
    "barq_workflow_run": barq_workflow_run,
    "barq_workflow_status": barq_workflow_status,
    # Briefing & Reviews
    "barq_briefing": barq_briefing,
    "barq_weekly_review": barq_weekly_review,
    # Analytics
    "barq_analytics": barq_analytics,
    # Vision
    "barq_vision_analyze": barq_vision_analyze,
    # Evolution
    "barq_evolution_status": barq_evolution_status,
    # APIs
    "barq_apis_list": barq_apis_list,
    # System
    "barq_system_status": barq_system_status,
    # Phone
    "barq_phone_send": barq_phone_send,
    # Second Brain Integration
    "barq_sb_search": barq_sb_search,
    "barq_sb_items": barq_sb_items,
    "barq_sb_create": barq_sb_create,
    "barq_sb_chat": barq_sb_chat,
    "barq_sb_sync": barq_sb_sync,
    "barq_sb_sync_status": barq_sb_sync_status,
    "barq_sb_sync_start": barq_sb_sync_start,
    "barq_sb_sync_stop": barq_sb_sync_stop,
    "barq_sb_status": barq_sb_status,
}

FEATURE_SCHEMAS: list[dict] = [
    {
        "name": "barq_navigate",
        "description": "Navigate the BARQ frontend to any page. Call when the user says open, go to, show, switch to, or take me to a BARQ page. Available: dashboard, analytics, jobs, social, files, dev, system, web, phone, research, docs, chat, memory, widgets, settings, agent, workflows, vision, brain, apis, evolution, notes, gallery.",
        "parameters": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "description": "Page name to navigate to (e.g. 'dashboard', 'jobs', 'brain', 'settings').",
                },
            },
            "required": ["page"],
        },
    },
    {
        "name": "barq_api",
        "description": "Call any BARQ feature by method and path. Use for jobs, social, memory, brain, workflows, notifications, analytics, settings, or any other backend endpoint. Examples: GET /jobs/matches, POST /agent/briefing/run.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "HTTP method: 'GET' or 'POST' (default GET)."},
                "path": {"type": "string", "description": "Backend path, e.g. '/jobs/matches' or '/social/trends'."},
                "payload": {"type": "object", "description": "Optional. For GET: query params. For POST: JSON body."},
            },
        },
    },
    {
        "name": "barq_skills_list",
        "description": "List all registered agent skills (web search, deep research, code helper, dev agent, recruitment, etc.) so you know what skills are available.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Optional category filter (web, system, files, research, developer, recruitment, social, communications)."},
            },
        },
    },
    {
        "name": "barq_skill",
        "description": "Invoke any registered agent skill by name, e.g. 'web_search', 'deep_research', 'code_helper', 'dev_agent'. Pass arguments as a JSON object string.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name (see barq_skills_list)."},
                "params": {"type": "string", "description": "JSON object string of keyword arguments, e.g. '{\"query\": \"AI news\"}'."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "barq_remember",
        "description": "Store a fact in BARQ's long-term memory (key-value). Use when the user asks to remember something.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short unique name, e.g. 'user_birthday'."},
                "value": {"type": "string", "description": "The fact to remember."},
                "category": {"type": "string", "description": "Optional category (default 'general')."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "barq_recall",
        "description": "Search BARQ's long-term memory for stored facts matching a query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {"type": "integer", "description": "Max results (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "barq_memory_list",
        "description": "List all stored long-term memories.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_memory_store",
        "description": "Store a new memory entry with key, value, and optional category.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Unique name for the memory."},
                "value": {"type": "string", "description": "The fact to remember."},
                "category": {"type": "string", "description": "Category (default 'general')."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "barq_brain_summary",
        "description": "Get a summary of the knowledge brain (concepts and connections).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_notify",
        "description": "Send a notification through BARQ (telegram, email, or desktop).",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title."},
                "body": {"type": "string", "description": "Notification body text."},
                "priority": {"type": "string", "description": "low, normal, high, or urgent."},
                "channel": {"type": "string", "description": "telegram, email, desktop, or all."},
                "category": {"type": "string", "description": "general, job_match, application, content, analytics, error, system."},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "barq_job_matches",
        "description": "Get the current job matches from BARQ's job scanner.",
        "parameters": {
            "type": "object",
            "properties": {
                "min_score": {"type": "number", "description": "Minimum match score (default 3.0)."},
                "limit": {"type": "integer", "description": "Max matches (default 20)."},
            },
        },
    },
    {
        "name": "barq_job_scan",
        "description": "Trigger a background scan of all configured job boards for new listings.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_job_details",
        "description": "Get full details for a specific job listing, including match evaluation and application status.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "The job listing id."},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "barq_job_search",
        "description": "Search for jobs with keywords and/or location. Triggers a scan and returns matches.",
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "Job search keywords (e.g. 'python developer')."},
                "location": {"type": "string", "description": "Location filter (e.g. 'New York', 'remote')."},
            },
        },
    },
    {
        "name": "barq_job_application_status",
        "description": "Get the status of all job applications (submitted, interview, rejected, etc.).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_apply_preview",
        "description": "Safe-mode: fill a job application form and screenshot WITHOUT submitting.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "The job listing id to preview."},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "barq_social_trends",
        "description": "Get the current social media trending topics.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_social_post",
        "description": "Create and post social media content on a topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Topic or content to post about."},
                "platforms": {"type": "string", "description": "Target platforms (e.g. 'all', 'twitter', 'instagram'). Default: 'all'."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "barq_social_schedule",
        "description": "Schedule a social media post for a specific date.",
        "parameters": {
            "type": "object",
            "properties": {
                "video_id": {"type": "integer", "description": "The video/content id to schedule."},
                "platforms": {"type": "string", "description": "Target platforms. Default: 'all'."},
                "date": {"type": "string", "description": "Scheduled date (YYYY-MM-DD or natural language)."},
            },
            "required": ["video_id", "date"],
        },
    },
    {
        "name": "barq_file_search",
        "description": "Search for files in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for files."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "barq_web_search",
        "description": "Search the web via BARQ's web skill.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Web search query."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "barq_research",
        "description": "Start a deep research task on a topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Research topic or question."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "barq_docs_search",
        "description": "Search BARQ documentation.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in docs."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "barq_chat_send",
        "description": "Send a message to BARQ's chat AI and get a response.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to send."},
            },
            "required": ["message"],
        },
    },
    {
        "name": "barq_notes_list",
        "description": "List all notes.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_notes_create",
        "description": "Create a new note with title, content, and optional tags.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Note title."},
                "content": {"type": "string", "description": "Note content."},
                "tags": {"type": "string", "description": "Comma-separated tags (e.g. 'work,important')."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "barq_widget_list",
        "description": "List available widgets.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_settings_get",
        "description": "Get BARQ settings for a section.",
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "Settings section (voice, jobs, social, etc.)."},
            },
        },
    },
    {
        "name": "barq_settings_set",
        "description": "Update a BARQ setting.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Setting key."},
                "value": {"type": "string", "description": "New value."},
                "section": {"type": "string", "description": "Settings section. Default: 'general'."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "barq_agent_run",
        "description": "Run an agent task (e.g. 'scan my inbox', 'summarize today's news').",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description for the agent."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "barq_workflow_run",
        "description": "Run a registered agent workflow by name (e.g. 'morning_briefing', 'weekly_review').",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Workflow name."},
                "context": {"type": "object", "description": "Optional dict of inputs the workflow expects."},
                "background": {"type": "boolean", "description": "Run asynchronously if True (default False)."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "barq_workflow_status",
        "description": "Get the status of a workflow run by its run_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run id returned by barq_workflow_run."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "barq_briefing",
        "description": "Generate the morning briefing report.",
        "parameters": {
            "type": "object",
            "properties": {
                "notify": {"type": "boolean", "description": "Send the briefing via notification channels."},
            },
        },
    },
    {
        "name": "barq_weekly_review",
        "description": "Generate the weekly review report (analytics + skill success rates + memory).",
        "parameters": {
            "type": "object",
            "properties": {
                "notify": {"type": "boolean", "description": "Send the review via notification channels."},
                "days": {"type": "integer", "description": "Number of days to review (default 7)."},
            },
        },
    },
    {
        "name": "barq_analytics",
        "description": "Get an analytics snapshot: activity, career, social, or revenue.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "activity (default), career, social, or revenue."},
                "limit": {"type": "integer", "description": "Max rows for activity (default 50)."},
            },
        },
    },
    {
        "name": "barq_vision_analyze",
        "description": "Analyze the screen or camera using vision AI. Ask what is on screen or what the camera sees.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Question about what to analyze. Default: 'What do you see?'"},
                "source": {"type": "string", "description": "'screen' (default) or 'camera'."},
            },
        },
    },
    {
        "name": "barq_evolution_status",
        "description": "Get the AI evolution and learning status.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_apis_list",
        "description": "List available public APIs and integrations.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_system_status",
        "description": "Get full system status including CPU, RAM, disk, and GPU.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_phone_send",
        "description": "Send a message via the phone/mobile integration.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to send."},
                "to": {"type": "string", "description": "Recipient (phone number or contact name)."},
            },
            "required": ["message", "to"],
        },
    },
    # ─── Second Brain Integration ───────────────────────────────────────
    {
        "name": "barq_sb_search",
        "description": "Search Second Brain for notes, code, bookmarks, and tasks. Supports text, semantic, and hybrid search modes.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (e.g. 'python decorators', 'meeting notes')."},
                "mode": {"type": "string", "description": "Search mode: 'text' (keyword), 'semantic' (meaning-based), or 'hybrid' (default)."},
                "limit": {"type": "integer", "description": "Max results (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "barq_sb_items",
        "description": "List items from Second Brain. Filter by type: note, code, bookmark, or task.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_type": {"type": "string", "description": "Filter by type: note, code, bookmark, or task (empty = all)."},
                "limit": {"type": "integer", "description": "Max items to return (default 20)."},
            },
        },
    },
    {
        "name": "barq_sb_create",
        "description": "Create a new item in Second Brain (note, code, bookmark, or task). Use when the user says 'save to second brain', 'create note in second brain', or 'remember this'.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Item title."},
                "content": {"type": "string", "description": "Item content (plain text or markdown)."},
                "item_type": {"type": "string", "description": "Type: note (default), code, bookmark, or task."},
                "tags": {"type": "string", "description": "Comma-separated tags (e.g. 'python, research')."},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "barq_sb_chat",
        "description": "Chat with Second Brain's Gemini AI. The AI searches your stored knowledge before answering, so responses are grounded in your notes and data.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Your question or message."},
            },
            "required": ["message"],
        },
    },
    {
        "name": "barq_sb_sync",
        "description": "Sync knowledge between BARQ and Second Brain. Push BARQ brain triplets as Second Brain items, or pull Second Brain items into BARQ's knowledge graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "description": "'to_items' (BARQ→SB), 'to_triplets' (SB→BARQ), or 'both' (default)."},
            },
        },
    },
    {
        "name": "barq_sb_sync_status",
        "description": "Get sync status: auto-sync state, last sync time, mapping stats, and recent sync history.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_sb_sync_start",
        "description": "Start automatic sync between BARQ and Second Brain at a configured interval.",
        "parameters": {
            "type": "object",
            "properties": {
                "interval": {"type": "integer", "description": "Sync interval in seconds (minimum 60, default 300)."},
            },
        },
    },
    {
        "name": "barq_sb_sync_stop",
        "description": "Stop the automatic sync scheduler.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "barq_sb_status",
        "description": "Check Second Brain connection status, item counts, and embedding coverage.",
        "parameters": {"type": "object", "properties": {}},
    },
]
