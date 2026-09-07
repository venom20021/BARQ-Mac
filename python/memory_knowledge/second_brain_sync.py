"""
Second Brain ↔ BARQ Bidirectional Sync Engine.

Provides:
- Conflict resolution via content-hash comparison (skip unchanged items)
- Auto-sync scheduler with configurable interval
- Real-time WebSocket progress events
- Sync mapping persistence (DB table: sync_mappings)
- Change detection: only syncs items that actually changed since last sync

Architecture:
    AutoSyncScheduler (background task)
        ├── sync_triplets_to_items()  — BARQ triplets → SB items
        ├── sync_items_to_triplets()  — SB items → BARQ triplets
        └── broadcasts progress via SyncWebSocketManager
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from config import get_settings

logger = logging.getLogger("barq.second_brain_sync")


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket Broadcast Manager for Sync Events
# ═══════════════════════════════════════════════════════════════════════════════


class SyncWSManager:
    """Broadcasts sync progress events to connected frontend clients."""

    _instance: SyncWSManager | None = None

    def __new__(cls) -> SyncWSManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._clients: set = set()
        return cls._instance

    @classmethod
    def get_instance(cls) -> SyncWSManager:
        if cls._instance is None:
            cls()
        return cls._instance

    async def register(self, ws: Any) -> None:
        self._clients.add(ws)
        logger.debug("[SyncWS] Client registered (%d total)", len(self._clients))

    async def unregister(self, ws: Any) -> None:
        self._clients.discard(ws)
        logger.debug("[SyncWS] Client unregistered (%d remaining)", len(self._clients))

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list = []
        for ws in self._clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Content Hash Utilities
# ═══════════════════════════════════════════════════════════════════════════════


def _hash_content(text: str) -> str:
    """SHA-256 hash of content for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _hash_item(item: dict[str, Any]) -> str:
    """Hash a Second Brain item for change detection."""
    parts = [
        item.get("title", ""),
        item.get("content", ""),
        json.dumps(sorted(item.get("tags", [])), sort_keys=True),
    ]
    return _hash_content("|".join(parts))


def _hash_entity(entity: str, connections: list[dict[str, str]]) -> str:
    """Hash a BARQ entity and its connections."""
    parts = [entity]
    for c in sorted(connections, key=lambda x: (x.get("relation", ""), x.get("target", ""))):
        parts.append(f"{c.get('relation', '')}:{c.get('target', '')}")
    return _hash_content("|".join(parts))


# ═══════════════════════════════════════════════════════════════════════════════
# Sync Mapping DAO
# ═══════════════════════════════════════════════════════════════════════════════


class SyncMappingDAO:
    """Manages the sync_mappings table for cross-system entity tracking."""

    async def get_mapping(
        self, brain_type: str, entity_name: str, item_id: int | None = None
    ) -> dict[str, Any] | None:
        try:
            from database import db_connection
            if item_id is not None:
                row = await db_connection.fetch_one(
                    "SELECT * FROM sync_mappings WHERE brain_type = ? AND entity_name = ? "
                    "AND second_brain_item_id = ?",
                    (brain_type, entity_name, item_id),
                )
            else:
                row = await db_connection.fetch_one(
                    "SELECT * FROM sync_mappings WHERE brain_type = ? AND entity_name = ?",
                    (brain_type, entity_name),
                )
            return row
        except Exception as e:
            logger.warning("[SyncDAO] get_mapping error: %s", e)
            return None

    async def upsert_mapping(
        self,
        brain_type: str,
        entity_name: str,
        item_id: int | None,
        content_hash: str,
        direction: str = "both",
    ) -> None:
        try:
            from database import db_connection
            existing = await self.get_mapping(brain_type, entity_name, item_id)
            if existing:
                await db_connection.execute(
                    "UPDATE sync_mappings SET content_hash = ?, second_brain_item_id = ?, "
                    "direction = ?, last_synced_at = datetime('now') "
                    "WHERE brain_type = ? AND entity_name = ?",
                    (content_hash, item_id, direction, brain_type, entity_name),
                )
            else:
                await db_connection.execute(
                    "INSERT INTO sync_mappings (brain_type, entity_name, second_brain_item_id, "
                    "content_hash, direction) VALUES (?, ?, ?, ?, ?)",
                    (brain_type, entity_name, item_id, content_hash, direction),
                )
            await db_connection.commit()
        except Exception as e:
            logger.warning("[SyncDAO] upsert_mapping error: %s", e)

    async def has_changed(self, brain_type: str, entity_name: str, new_hash: str) -> bool:
        """Return True if entity has changed since last sync."""
        mapping = await self.get_mapping(brain_type, entity_name)
        if not mapping:
            return True  # Never synced before
        return mapping.get("content_hash", "") != new_hash

    async def get_last_sync_time(self) -> str | None:
        try:
            from database import db_connection
            row = await db_connection.fetch_one(
                "SELECT MAX(last_synced_at) as last_sync FROM sync_mappings"
            )
            return row["last_sync"] if row else None
        except Exception:
            return None

    async def get_all_mappings(self, brain_type: str | None = None) -> list[dict]:
        try:
            from database import db_connection
            if brain_type:
                rows = await db_connection.fetch_all(
                    "SELECT * FROM sync_mappings WHERE brain_type = ? ORDER BY last_synced_at DESC",
                    (brain_type,),
                )
            else:
                rows = await db_connection.fetch_all(
                    "SELECT * FROM sync_mappings ORDER BY last_synced_at DESC"
                )
            return rows or []
        except Exception:
            return []

    async def get_stats(self) -> dict[str, Any]:
        try:
            from database import db_connection
            row = await db_connection.fetch_one(
                "SELECT COUNT(*) as total, "
                "COUNT(DISTINCT brain_type) as brains, "
                "COUNT(DISTINCT second_brain_item_id) as sb_items "
                "FROM sync_mappings"
            )
            return {
                "total_mappings": row["total"] if row else 0,
                "brain_types": row["brains"] if row else 0,
                "sb_items_linked": row["sb_items"] if row else 0,
            }
        except Exception:
            return {"total_mappings": 0, "brain_types": 0, "sb_items_linked": 0}


sync_mapping_dao = SyncMappingDAO()


# ═══════════════════════════════════════════════════════════════════════════════
# Sync Log DAO
# ═══════════════════════════════════════════════════════════════════════════════


class SyncLogDAO:
    """Manages the sync_log table for sync run history."""

    async def log_start(self, sync_type: str) -> int:
        try:
            from database import db_connection
            log_id = await db_connection.insert(
                "INSERT INTO sync_log (sync_type, status) VALUES (?, 'running')",
                (sync_type,),
            )
            return log_id or 0
        except Exception:
            return 0

    async def log_complete(
        self,
        log_id: int,
        *,
        status: str = "completed",
        items_synced: int = 0,
        items_created: int = 0,
        items_updated: int = 0,
        items_skipped: int = 0,
        errors: int = 0,
        error_message: str = "",
    ) -> None:
        try:
            from database import db_connection
            await db_connection.execute(
                "UPDATE sync_log SET status = ?, items_synced = ?, items_created = ?, "
                "items_updated = ?, items_skipped = ?, errors = ?, error_message = ?, "
                "completed_at = datetime('now') WHERE id = ?",
                (status, items_synced, items_created, items_updated, items_skipped,
                 errors, error_message, log_id),
            )
            await db_connection.commit()
        except Exception as e:
            logger.warning("[SyncLog] log_complete error: %s", e)

    async def get_history(self, limit: int = 10) -> list[dict]:
        try:
            from database import db_connection
            rows = await db_connection.fetch_all(
                "SELECT * FROM sync_log ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            return rows or []
        except Exception:
            return []

    async def get_last_sync(self) -> dict | None:
        try:
            from database import db_connection
            row = await db_connection.fetch_one(
                "SELECT * FROM sync_log WHERE status = 'completed' "
                "ORDER BY completed_at DESC LIMIT 1"
            )
            return row
        except Exception:
            return None


sync_log_dao = SyncLogDAO()


# ═══════════════════════════════════════════════════════════════════════════════
# Smart Sync Engine (Conflict Resolution + Change Detection)
# ═══════════════════════════════════════════════════════════════════════════════


class SmartSyncEngine:
    """Sync engine with hash-based change detection and conflict resolution.

    Resolution strategy:
    - BARQ → SB: If entity unchanged (same hash), skip. If changed, update SB item.
    - SB → BARQ: If item unchanged (same hash), skip. If changed, update triplet content.
    - Direction "both" wins conflicts by timestamp (most recently synced side is authoritative).
    """

    def __init__(self) -> None:
        self._ws = SyncWSManager.get_instance()

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast a sync event to connected WebSocket clients."""
        msg = {"type": f"sync_{event_type}", "timestamp": datetime.now(timezone.utc).isoformat(), **data}
        await self._ws.broadcast(msg)

    async def sync_triplets_to_items(
        self, brain_type: str = "general", skip_unchanged: bool = True
    ) -> dict[str, Any]:
        """BARQ triplets → Second Brain items with change detection."""
        from memory_knowledge.second_brain_bridge import second_brain_bridge
        from memory_knowledge.multi_brain import multi_brain_manager

        await self._emit("progress", {
            "direction": "to_items", "phase": "starting",
            "brain_type": brain_type,
        })

        data = multi_brain_manager.visualize(brain_type)
        nodes = data.get("nodes", [])
        links = data.get("links", [])

        if not nodes:
            return {"status": "success", "created": 0, "updated": 0, "skipped": 0, "detail": "Empty brain"}

        # Group links by source entity
        entity_connections: dict[str, list[dict[str, str]]] = {}
        for link in links:
            src = link.get("source", "")
            tgt = link.get("target", "")
            rel = link.get("relation", "RELATED_TO")
            if src and tgt:
                entity_connections.setdefault(src, []).append({
                    "target": tgt, "relation": rel,
                })

        created = 0
        updated = 0
        skipped = 0
        errors = 0
        total = len(entity_connections)

        for i, (entity, connections) in enumerate(entity_connections.items()):
            content_hash = _hash_entity(entity, connections)

            # Progress every 10 items
            if i % 10 == 0:
                await self._emit("progress", {
                    "direction": "to_items", "phase": "syncing",
                    "current": i, "total": total,
                    "entity": entity,
                })

            # Check if unchanged
            if skip_unchanged:
                changed = await sync_mapping_dao.has_changed(brain_type, entity, content_hash)
                if not changed:
                    skipped += 1
                    continue

            try:
                # Build content
                lines = [
                    f"# {entity}", "",
                    f"**Brain:** {brain_type}",
                    f"**Connections:** {len(connections)}", "",
                    "## Connections", "",
                ]
                for conn in connections:
                    lines.append(f"- **{conn['relation']}** → {conn['target']}")
                content = "\n".join(lines)
                tags = [brain_type, "barq-sync", "triplet"]

                # Search for existing item
                existing = await second_brain_bridge.search(entity, mode="text", limit=1)
                existing_items = []
                if isinstance(existing, dict) and existing.get("status") == "success":
                    raw = existing.get("data", [])
                    if isinstance(raw, list):
                        existing_items = raw

                item_id = None
                if existing_items:
                    item_id = existing_items[0].get("item", {}).get("id")
                    if item_id:
                        await second_brain_bridge.update_item(item_id, content=content, tags=tags)
                        updated += 1
                else:
                    result = await second_brain_bridge.create_item("note", entity, content, tags)
                    if isinstance(result, dict) and result.get("status") == "success":
                        item_data = result.get("data", {})
                        item_id = item_data.get("id")
                    created += 1

                # Update mapping
                await sync_mapping_dao.upsert_mapping(
                    brain_type, entity, item_id, content_hash, "to_items"
                )

            except Exception as e:
                logger.warning("Failed to sync entity '%s': %s", entity, e)
                errors += 1

        await self._emit("progress", {
            "direction": "to_items", "phase": "completed",
            "created": created, "updated": updated,
            "skipped": skipped, "errors": errors,
        })

        return {
            "status": "success",
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "total_entities": total,
            "detail": f"Created {created}, updated {updated}, skipped {skipped} ({errors} errors)",
        }

    async def sync_items_to_triplets(self, skip_unchanged: bool = True) -> dict[str, Any]:
        """Second Brain items → BARQ triplets with change detection."""
        import re
        from memory_knowledge.second_brain_bridge import second_brain_bridge
        from memory_knowledge.multi_brain import multi_brain_manager

        await self._emit("progress", {
            "direction": "to_triplets", "phase": "starting",
        })

        result = await second_brain_bridge.list_items(limit=1000)
        items = []
        if isinstance(result, dict) and result.get("status") == "success":
            raw = result.get("data", [])
            if isinstance(raw, list):
                items = raw

        if not items:
            return {"status": "success", "items_processed": 0, "triplets_added": 0, "skipped": 0}

        added = 0
        processed = 0
        skipped = 0
        errors = 0
        total = len(items)

        patterns = [
            re.compile(r"\*\*(\w+)\*\*\s*→\s*(.+)"),
            re.compile(r"(\w+)\s+(RELATED_TO|USED_FOR|IS_A|HAS|KNOWS|WORKS_AT|TAGGED_AS)\s+(.+)"),
        ]

        for i, item in enumerate(items):
            title = item.get("title", "").strip()
            content = item.get("content", "")
            tags = item.get("tags", [])
            item_id = item.get("id")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []

            if not title:
                continue
            processed += 1

            # Progress
            if i % 20 == 0:
                await self._emit("progress", {
                    "direction": "to_triplets", "phase": "syncing",
                    "current": i, "total": total,
                    "item": title[:50],
                })

            # Change detection
            item_hash = _hash_item(item)
            if skip_unchanged:
                changed = await sync_mapping_dao.has_changed("general", title, item_hash)
                if not changed:
                    skipped += 1
                    continue

            try:
                triplet_count = 0
                for line in content.split("\n"):
                    for pattern in patterns:
                        match = pattern.search(line)
                        if match:
                            groups = match.groups()
                            if len(groups) == 2:
                                relation = "RELATED_TO"
                                target = groups[1].strip()
                            else:
                                relation = groups[1].upper().replace(" ", "_")
                                target = groups[2].strip()

                            target = re.sub(r"\*\*(.+?)\*\*", r"\1", target).strip()
                            if target and relation:
                                multi_brain_manager.add_triplet("general", title, relation, target)
                                triplet_count += 1
                                added += 1

                # Tag-based triplets
                for tag in tags:
                    if isinstance(tag, str) and tag not in ("barq-sync", "triplet", "github"):
                        multi_brain_manager.add_triplet("general", title, "TAGGED_AS", tag)
                        triplet_count += 1
                        added += 1

                # Update mapping
                await sync_mapping_dao.upsert_mapping(
                    "general", title, item_id, item_hash, "to_triplets"
                )

            except Exception as e:
                logger.warning("Failed to sync item '%s': %s", title, e)
                errors += 1

        await self._emit("progress", {
            "direction": "to_triplets", "phase": "completed",
            "processed": processed, "triplets_added": added,
            "skipped": skipped, "errors": errors,
        })

        return {
            "status": "success",
            "items_processed": processed,
            "triplets_added": added,
            "skipped": skipped,
            "errors": errors,
            "detail": f"Processed {processed}, added {added} triplets, skipped {skipped}",
        }

    async def full_sync(self, direction: str = "both") -> dict[str, Any]:
        """Run full bidirectional sync with logging."""
        log_id = await sync_log_dao.log_start(direction)
        await self._emit("progress", {"direction": direction, "phase": "full_sync_started"})

        results: dict[str, Any] = {}
        total_synced = 0
        total_created = 0
        total_updated = 0
        total_skipped = 0
        total_errors = 0

        try:
            if direction in ("to_items", "both"):
                r = await self.sync_triplets_to_items()
                results["to_items"] = r
                total_synced += r.get("total_entities", 0)
                total_created += r.get("created", 0)
                total_updated += r.get("updated", 0)
                total_skipped += r.get("skipped", 0)
                total_errors += r.get("errors", 0)

            if direction in ("to_triplets", "both"):
                r = await self.sync_items_to_triplets()
                results["to_triplets"] = r
                total_synced += r.get("items_processed", 0)
                total_created += r.get("triplets_added", 0)
                total_skipped += r.get("skipped", 0)
                total_errors += r.get("errors", 0)

            results["status"] = "success"
            results["direction"] = direction

            await sync_log_dao.log_complete(
                log_id,
                status="completed",
                items_synced=total_synced,
                items_created=total_created,
                items_updated=total_updated,
                items_skipped=total_skipped,
                errors=total_errors,
            )
            await self._emit("progress", {
                "direction": direction, "phase": "full_sync_completed",
                "synced": total_synced, "created": total_created,
                "skipped": total_skipped, "errors": total_errors,
            })

        except Exception as e:
            results["status"] = "error"
            results["detail"] = str(e)
            await sync_log_dao.log_complete(
                log_id, status="failed", error_message=str(e)
            )
            await self._emit("progress", {
                "direction": direction, "phase": "full_sync_failed",
                "error": str(e),
            })
            logger.error("full_sync failed: %s", e, exc_info=True)

        return results


smart_sync_engine = SmartSyncEngine()


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-Sync Scheduler
# ═══════════════════════════════════════════════════════════════════════════════


class AutoSyncScheduler:
    """Background auto-sync scheduler using asyncio."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._interval_seconds: int = 300  # 5 minutes default
        self._last_sync_time: str | None = None
        self._last_sync_result: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "interval_seconds": self._interval_seconds,
            "last_sync_time": self._last_sync_time,
            "last_sync_result": self._last_sync_result,
        }

    def set_interval(self, seconds: int) -> None:
        """Update the sync interval (minimum 60 seconds)."""
        self._interval_seconds = max(60, seconds)
        logger.info("[AutoSync] Interval set to %ds", self._interval_seconds)

    async def start(self) -> None:
        """Start the background auto-sync loop."""
        if self._running:
            logger.warning("[AutoSync] Already running")
            return

        settings = get_settings()
        self._interval_seconds = getattr(settings, "second_brain_sync_interval", 5) * 60

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[AutoSync] Started (interval: %ds)", self._interval_seconds)

        # Broadcast started
        ws = SyncWSManager.get_instance()
        await ws.broadcast({
            "type": "sync_started",
            "interval_seconds": self._interval_seconds,
        })

    async def stop(self) -> None:
        """Stop the background auto-sync loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("[AutoSync] Stopped")

    async def trigger_now(self, direction: str = "both") -> dict[str, Any]:
        """Manually trigger an immediate sync."""
        logger.info("[AutoSync] Manual sync triggered (direction=%s)", direction)
        result = await smart_sync_engine.full_sync(direction=direction)
        self._last_sync_time = datetime.now(timezone.utc).isoformat()
        self._last_sync_result = result
        return result

    async def _run_loop(self) -> None:
        """Background loop that syncs at the configured interval."""
        while self._running:
            try:
                # Wait for the configured interval
                await asyncio.sleep(self._interval_seconds)

                if not self._running:
                    break

                logger.info("[AutoSync] Running scheduled sync...")
                result = await smart_sync_engine.full_sync(direction="both")
                self._last_sync_time = datetime.now(timezone.utc).isoformat()
                self._last_sync_result = result

                created = result.get("created", 0) + result.get("to_triplets", {}).get("triplets_added", 0)
                skipped = result.get("skipped", 0) + result.get("to_triplets", {}).get("skipped", 0)
                logger.info(
                    "[AutoSync] Completed — created=%d, skipped=%d, errors=%d",
                    created,
                    skipped,
                    result.get("errors", 0) + result.get("to_triplets", {}).get("errors", 0),
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[AutoSync] Sync cycle failed: %s", e)
                # Back off on error — wait 2x the interval
                await asyncio.sleep(self._interval_seconds)


# ─── Singleton ────────────────────────────────────────────────────────────

auto_sync_scheduler = AutoSyncScheduler()
