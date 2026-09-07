"""
Second Brain Integration API — exposes Second Brain data through BARQ's backend.

Endpoints:
- GET  /api/second-brain/status            — Connection status + stats
- GET  /api/second-brain/health            — Second Brain system health
- GET  /api/second-brain/items             — List items (proxied)
- POST /api/second-brain/items             — Create item (proxied)
- PUT  /api/second-brain/items/{id}        — Update item (proxied)
- DELETE /api/second-brain/items/{id}      — Delete item (proxied)
- POST /api/second-brain/search            — Search (text/semantic/hybrid)
- GET  /api/second-brain/graph             — vis.js graph data
- POST /api/second-brain/chat              — Chat with Gemini (brain-grounded)
- POST /api/second-brain/sync/to-items     — BARQ triplets → Second Brain items
- POST /api/second-brain/sync/to-triplets  — Second Brain items → BARQ triplets
- POST /api/second-brain/sync/full         — Full bidirectional sync
- GET  /api/second-brain/sync/status       — Sync status + auto-sync info
- GET  /api/second-brain/sync/mappings     — List all sync mappings
- GET  /api/second-brain/sync/history      — Sync run history
- POST /api/second-brain/sync/auto/start   — Start auto-sync scheduler
- POST /api/second-brain/sync/auto/stop    — Stop auto-sync scheduler
- WS   /ws/second-brain/sync               — Real-time sync progress WebSocket
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/second-brain", tags=["Second Brain Integration"])


# ─── Request Models ──────────────────────────────────────────────────────


class ItemCreate(BaseModel):
    """Payload for creating a Second Brain item through BARQ."""

    item_type: str = Field("note", description="note, code, bookmark, or task")
    title: str = Field(..., min_length=1, description="Item title")
    content: str = Field("", description="Item content (plain text or markdown)")
    tags: list[str] = Field(default_factory=list, description="Tag list")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata")


class ItemUpdate(BaseModel):
    """Payload for updating a Second Brain item (partial)."""

    title: Optional[str] = Field(None, description="New title")
    content: Optional[str] = Field(None, description="New content")
    tags: Optional[list[str]] = Field(None, description="Replace tags")
    metadata: Optional[dict[str, Any]] = Field(None, description="Replace metadata")
    item_type: Optional[str] = Field(None, description="New item type")


class SearchRequest(BaseModel):
    """Search query for Second Brain."""

    query: str = Field(..., min_length=1, description="Search query")
    mode: str = Field("hybrid", description="text, semantic, or hybrid")
    limit: int = Field(20, ge=1, le=100, description="Max results")
    item_type: Optional[str] = Field(None, description="Filter by type")


class ChatRequest(BaseModel):
    """Chat message for Second Brain's Gemini AI."""

    message: str = Field(..., min_length=1, description="User message")
    history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Conversation history [{role, parts}]",
    )


class SyncRequest(BaseModel):
    """Sync configuration."""

    brain_type: str = Field("general", description="BARQ brain type to sync")
    direction: str = Field("both", description="to_items, to_triplets, or both")


class AutoSyncConfig(BaseModel):
    """Auto-sync scheduler configuration."""

    interval_seconds: int = Field(300, ge=60, le=3600, description="Sync interval in seconds")


# ─── Helper ──────────────────────────────────────────────────────────────


def _get_bridge():
    """Lazy-import the bridge singleton."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge

    return second_brain_bridge


def _unwrap(result: dict) -> Any:
    """Unwrap a bridge result or raise HTTPException on error."""
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result.get("detail", "Second Brain error"))
    return result.get("data", result)


# ─── Status & Health ─────────────────────────────────────────────────────


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Check Second Brain connection and get stats.

    Returns connection status, item counts, and embedding coverage.
    """
    bridge = _get_bridge()

    if not bridge.enabled:
        return {
            "connected": False,
            "enabled": False,
            "message": (
                "Second Brain integration disabled. "
                "Set SECOND_BRAIN_ENABLED=true in your .env file."
            ),
        }

    stats_result = await bridge.get_stats()
    health_result = await bridge.get_system_health()

    connected = stats_result.get("status") == "success"

    return {
        "connected": connected,
        "enabled": True,
        "url": bridge.base_url,
        "stats": stats_result.get("data", {}),
        "health": health_result.get("data", {}),
    }


@router.get("/health")
async def get_health() -> dict[str, Any]:
    """Quick health check — is Second Brain reachable?"""
    bridge = _get_bridge()
    result = await bridge.health_check()
    return _unwrap(result)


# ─── Items CRUD ──────────────────────────────────────────────────────────


@router.get("/items")
async def list_items(
    item_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """List items from Second Brain."""
    bridge = _get_bridge()
    result = await bridge.list_items(item_type=item_type, limit=limit, offset=offset)
    return _unwrap(result)


@router.post("/items")
async def create_item(request: ItemCreate) -> Any:
    """Create a new item in Second Brain."""
    bridge = _get_bridge()
    result = await bridge.create_item(
        item_type=request.item_type,
        title=request.title,
        content=request.content,
        tags=request.tags,
        metadata=request.metadata,
    )
    return _unwrap(result)


@router.put("/items/{item_id}")
async def update_item(item_id: int, request: ItemUpdate) -> Any:
    """Update an item in Second Brain (partial update — only provided fields)."""
    bridge = _get_bridge()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    result = await bridge.update_item(item_id, **updates)
    return _unwrap(result)


@router.delete("/items/{item_id}")
async def delete_item(item_id: int) -> Any:
    """Delete an item from Second Brain."""
    bridge = _get_bridge()
    result = await bridge.delete_item(item_id)
    return _unwrap(result)


# ─── Search ──────────────────────────────────────────────────────────────


@router.post("/search")
async def search(request: SearchRequest) -> Any:
    """Search Second Brain.

    Modes:
    - **text**: SQLite FTS5 full-text search (fast, keyword-based)
    - **semantic**: Vector similarity with all-MiniLM-L6-v2 (meaning-based)
    - **hybrid**: Combined 50/50 weighted (best of both)
    """
    bridge = _get_bridge()
    result = await bridge.search(
        query=request.query,
        mode=request.mode,
        limit=request.limit,
        item_type=request.item_type,
    )
    return _unwrap(result)


# ─── Graph ───────────────────────────────────────────────────────────────


@router.get("/graph")
async def get_graph() -> Any:
    """Get the vis.js knowledge graph from Second Brain.

    Returns nodes (items + tags) and edges (shared-tag connections)
    formatted for vis.js Network.
    """
    bridge = _get_bridge()
    result = await bridge.get_graph()
    return _unwrap(result)


# ─── Chat ────────────────────────────────────────────────────────────────


@router.post("/chat")
async def chat(request: ChatRequest) -> Any:
    """Chat with Second Brain's Gemini AI.

    The AI searches your Second Brain for relevant context before answering,
    so responses are grounded in your stored knowledge.
    """
    bridge = _get_bridge()
    result = await bridge.chat(message=request.message, history=request.history)
    return _unwrap(result)


# ─── Sync ────────────────────────────────────────────────────────────────


@router.post("/sync/to-items")
async def sync_to_items(brain_type: str = "general") -> dict[str, Any]:
    """Sync BARQ brain triplets → Second Brain items with change detection.

    Uses content-hash comparison to skip unchanged entities.
    """
    from memory_knowledge.second_brain_sync import smart_sync_engine
    result = await smart_sync_engine.sync_triplets_to_items(brain_type=brain_type)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("detail"))
    return result


@router.post("/sync/to-triplets")
async def sync_to_triplets() -> dict[str, Any]:
    """Sync Second Brain items → BARQ brain triplets with change detection.

    Parses each item's content for connection patterns and creates
    triplets in the BARQ 'general' brain. Skips unchanged items.
    """
    from memory_knowledge.second_brain_sync import smart_sync_engine
    result = await smart_sync_engine.sync_items_to_triplets()
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("detail"))
    return result


@router.post("/sync/full")
async def full_sync(request: SyncRequest) -> dict[str, Any]:
    """Run a full bidirectional sync between BARQ and Second Brain.

    Uses smart sync engine with change detection and conflict resolution.
    Directions:
    - **to_items**: BARQ triplets → Second Brain items
    - **to_triplets**: Second Brain items → BARQ triplets
    - **both**: Bidirectional sync (default)
    """
    if request.direction not in ("to_items", "to_triplets", "both"):
        raise HTTPException(
            status_code=400,
            detail="direction must be 'to_items', 'to_triplets', or 'both'",
        )
    from memory_knowledge.second_brain_sync import smart_sync_engine
    result = await smart_sync_engine.full_sync(direction=request.direction)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("detail"))
    return result


# ─── Sync Status & Mappings ────────────────────────────────────────────


@router.get("/sync/status")
async def get_sync_status() -> dict[str, Any]:
    """Get sync status including auto-sync state, last sync, and mapping stats."""
    from memory_knowledge.second_brain_sync import auto_sync_scheduler, sync_mapping_dao, sync_log_dao

    mapping_stats = await sync_mapping_dao.get_stats()
    last_sync = await sync_log_dao.get_last_sync()
    history = await sync_log_dao.get_history(limit=5)

    return {
        "auto_sync": auto_sync_scheduler.status,
        "mappings": mapping_stats,
        "last_sync": last_sync,
        "recent_history": history,
    }


@router.get("/sync/mappings")
async def get_sync_mappings(brain_type: str | None = None) -> dict[str, Any]:
    """List all sync mappings (BARQ entities ↔ Second Brain items)."""
    from memory_knowledge.second_brain_sync import sync_mapping_dao
    mappings = await sync_mapping_dao.get_all_mappings(brain_type=brain_type)
    return {
        "mappings": [
            {
                "id": m.get("id"),
                "brain_type": m.get("brain_type"),
                "entity_name": m.get("entity_name"),
                "second_brain_item_id": m.get("second_brain_item_id"),
                "content_hash": m.get("content_hash"),
                "direction": m.get("direction"),
                "last_synced_at": m.get("last_synced_at"),
            }
            for m in mappings
        ],
        "count": len(mappings),
    }


@router.get("/sync/history")
async def get_sync_history(limit: int = Query(10, ge=1, le=100)) -> dict[str, Any]:
    """Get sync run history."""
    from memory_knowledge.second_brain_sync import sync_log_dao
    history = await sync_log_dao.get_history(limit=limit)
    return {"history": history, "count": len(history)}


# ─── Auto-Sync Scheduler ───────────────────────────────────────────────


@router.post("/sync/auto/start")
async def start_auto_sync(config: AutoSyncConfig | None = None) -> dict[str, Any]:
    """Start the auto-sync background scheduler."""
    from memory_knowledge.second_brain_sync import auto_sync_scheduler

    if config:
        auto_sync_scheduler.set_interval(config.interval_seconds)

    await auto_sync_scheduler.start()
    return {
        "status": "started",
        "interval_seconds": auto_sync_scheduler._interval_seconds,
    }


@router.post("/sync/auto/stop")
async def stop_auto_sync() -> dict[str, Any]:
    """Stop the auto-sync background scheduler."""
    from memory_knowledge.second_brain_sync import auto_sync_scheduler
    await auto_sync_scheduler.stop()
    return {"status": "stopped"}


# ─── WebSocket: Real-time Sync Progress ────────────────────────────────


@router.websocket("/ws/sync")
async def sync_progress_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time sync progress updates.

    Events emitted:
    - sync_started: auto-sync scheduler started
    - sync_progress: sync operation in progress (direction, phase, counts)
    - sync_completed: full sync completed
    - sync_error: sync operation failed
    """
    await websocket.accept()
    from memory_knowledge.second_brain_sync import SyncWSManager
    ws_manager = SyncWSManager.get_instance()
    await ws_manager.register(websocket)

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # Client can send commands
                try:
                    import json as _json
                    msg = _json.loads(data)
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg.get("type") == "trigger_sync":
                        from memory_knowledge.second_brain_sync import auto_sync_scheduler
                        direction = msg.get("direction", "both")
                        asyncio.create_task(auto_sync_scheduler.trigger_now(direction))
                        await websocket.send_json({"type": "sync_triggered", "direction": direction})
                except Exception:
                    pass
            except asyncio.TimeoutError:
                # Send keepalive
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.unregister(websocket)
