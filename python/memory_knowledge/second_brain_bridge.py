"""
Second Brain Bridge — connects BARQ to a Second Brain instance.

Provides:
- Read/write access to Second Brain items via its REST API
- Semantic search bridged from Second Brain
- Graph data bridged from Second Brain
- Bidirectional sync (triplets ↔ items)

Architecture:
    BARQ (FastAPI :8956) → HTTP → Second Brain (Flask :8000)

All methods are async and return dicts with a ``status`` key
(``"success"`` / ``"error"``) matching the rest of the BARQ voice registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

import httpx

from config import get_settings

logger = logging.getLogger("barq.second_brain_bridge")


class SecondBrainBridge:
    """Async HTTP client for Second Brain's REST API."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    # ─── Configuration ────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return get_settings().second_brain_url.rstrip("/")

    @property
    def api_key(self) -> str:
        return get_settings().second_brain_api_key

    @property
    def enabled(self) -> bool:
        return get_settings().second_brain_enabled

    @property
    def headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    # ─── Low-level HTTP ───────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Make an HTTP request to Second Brain.

        Returns a dict with ``status`` key — never raises.
        """
        if not self.enabled:
            return {"status": "error", "detail": "Second Brain integration disabled"}

        url = f"{self.base_url}/api/v1{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    resp = await client.get(url, headers=self.headers, params=data)
                elif method == "POST":
                    resp = await client.post(url, headers=self.headers, json=data)
                elif method == "PUT":
                    resp = await client.put(url, headers=self.headers, json=data)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=self.headers)
                else:
                    return {"status": "error", "detail": f"Unsupported method: {method}"}

                if resp.status_code >= 400:
                    detail = resp.text[:500]
                    return {"status": "error", "detail": f"HTTP {resp.status_code}: {detail}"}
                return {"status": "success", "data": resp.json()}
        except httpx.ConnectError:
            return {
                "status": "error",
                "detail": (
                    "Second Brain not reachable — is it running on "
                    f"{self.base_url}? Start with: python app/main.py"
                ),
            }
        except httpx.TimeoutException:
            return {"status": "error", "detail": f"Second Brain request timed out ({timeout}s)"}
        except Exception as e:
            return {"status": "error", "detail": f"Second Brain bridge error: {e}"}

    # ─── Health / Status ──────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Check if Second Brain is reachable."""
        return await self._request("GET", "/stats", timeout=5.0)

    async def get_stats(self) -> dict[str, Any]:
        """Get Second Brain item statistics."""
        return await self._request("GET", "/stats")

    async def get_system_health(self) -> dict[str, Any]:
        """Get Second Brain system health (server, DB, embeddings)."""
        return await self._request("GET", "/system")

    # ─── Items CRUD ───────────────────────────────────────────────────

    async def list_items(
        self,
        item_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List items from Second Brain."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if item_type:
            params["item_type"] = item_type
        return await self._request("GET", "/items", params)

    async def get_item(self, item_id: int) -> dict[str, Any]:
        """Get a single item by ID."""
        return await self._request("GET", f"/items/{item_id}")

    async def create_item(
        self,
        item_type: str,
        title: str,
        content: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new item in Second Brain."""
        return await self._request("POST", "/items", {
            "item_type": item_type,
            "title": title,
            "content": content,
            "tags": tags or [],
            "metadata": metadata or {},
        })

    async def update_item(self, item_id: int, **kwargs: Any) -> dict[str, Any]:
        """Update an item in Second Brain (partial update)."""
        return await self._request("PUT", f"/items/{item_id}", kwargs)

    async def delete_item(self, item_id: int) -> dict[str, Any]:
        """Delete an item from Second Brain."""
        return await self._request("DELETE", f"/items/{item_id}")

    # ─── Search ───────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        mode: str = "hybrid",
        limit: int = 20,
        item_type: str | None = None,
    ) -> dict[str, Any]:
        """Search Second Brain (text / semantic / hybrid).

        Modes:
        - text: SQLite FTS5 full-text search
        - semantic: vector similarity (all-MiniLM-L6-v2)
        - hybrid: combined 50/50 weighted
        """
        payload: dict[str, Any] = {"query": query, "mode": mode, "limit": limit}
        if item_type:
            payload["item_type"] = item_type
        return await self._request("POST", "/search", payload)

    # ─── Graph ────────────────────────────────────────────────────────

    async def get_graph(self) -> dict[str, Any]:
        """Build a vis.js knowledge graph from Second Brain items.

        Builds the graph locally from items data instead of calling SB's
        /graph endpoint, which is O(n²) and times out with 1000+ items.
        This approach is O(n * t) where t = avg tags per item.
        """
        try:
            # Fetch items — prioritize real content over sync-created items
            # First pass: items with unique (non-sync) tags
            all_items_result = await self.list_items(limit=1000)
            all_items = all_items_result.get("data", []) if isinstance(all_items_result, dict) else (
                all_items_result if isinstance(all_items_result, list) else []
            )

            # Split: real-content items vs sync-created items
            real_items = []
            sync_items = []
            for item in all_items:
                tags_raw = item.get("tags", [])
                if isinstance(tags_raw, str):
                    import json as _json
                    try:
                        tags_raw = _json.loads(tags_raw)
                    except Exception:
                        tags_raw = []
                has_real_tag = any(
                    t not in ("general", "barq-sync", "triplet", "")
                    for t in tags_raw if isinstance(t, str)
                )
                (real_items if has_real_tag else sync_items).append(item)

            # Use all real items + up to 200 sync items for graph context
            items = real_items + sync_items[:200]

            if not items:
                return {
                    "data": {
                        "nodes": [],
                        "links": [],
                        "stats": {"total_nodes": 0, "total_edges": 0, "total_communities": 0},
                    }
                }

            # Color palette per item type
            TYPE_COLORS = {
                "note": "#4ade80",
                "code": "#22d3ee",
                "bookmark": "#fbbf24",
                "task": "#f87171",
            }

            # 1. Build item nodes + tag index
            nodes = []
            tag_to_items: dict[str, list[str]] = {}
            item_tag_sets: dict[str, set[str]] = {}

            for item in items:
                iid = f"item_{item.get('id', '')}"
                title = item.get("title", "Untitled")
                item_type = item.get("item_type", "note")
                tags_raw = item.get("tags", [])
                if isinstance(tags_raw, str):
                    import json as _json
                    try:
                        tags_raw = _json.loads(tags_raw)
                    except Exception:
                        tags_raw = [t.strip() for t in tags_raw.split(",") if t.strip()]

                tags = [t for t in tags_raw if isinstance(t, str)]
                item_tag_sets[iid] = set(tags)

                for t in tags:
                    tag_to_items.setdefault(t, []).append(iid)

                nodes.append({
                    "id": iid,
                    "label": title[:40],
                    "title": title,
                    "node_type": "item",
                    "item_type": item_type,
                    "content_preview": (item.get("content", "") or "")[:200].replace("\n", " "),
                    "tags": tags,
                    "size": max(8, min(25, len(item.get("content", "") or "") / 200 + 6)),
                })

            # 2. Build edges: items sharing 2+ tags (efficient O(n * t²))
            links = []
            edge_set: set[tuple[str, str]] = set()
            node_edge_count: dict[str, int] = {}
            MAX_EDGES_PER_NODE = 6

            for tag, member_ids in tag_to_items.items():
                if len(member_ids) < 2:
                    continue
                for i in range(len(member_ids)):
                    for j in range(i + 1, len(member_ids)):
                        a, b = sorted([member_ids[i], member_ids[j]])
                        edge_key = (a, b)
                        if edge_key in edge_set:
                            continue
                        a_count = node_edge_count.get(a, 0)
                        b_count = node_edge_count.get(b, 0)
                        if a_count >= MAX_EDGES_PER_NODE or b_count >= MAX_EDGES_PER_NODE:
                            continue
                        edge_set.add(edge_key)
                        links.append({
                            "source": a,
                            "target": b,
                            "weight": 1.0,
                            "shared_tags": [tag],
                            "confidence": "EXTRACTED",
                        })
                        node_edge_count[a] = a_count + 1
                        node_edge_count[b] = b_count + 1

            # 3. Add tag nodes for non-trivial tags
            for tag, member_ids in tag_to_items.items():
                if len(member_ids) >= 2:
                    tag_id = f"tag_{tag}"
                    nodes.append({
                        "id": tag_id,
                        "label": f"#{tag}",
                        "title": f"Tag: {tag} ({len(member_ids)} items)",
                        "node_type": "tag",
                        "size": 5 + min(len(member_ids), 15),
                    })
                    # Connect tag to its items (limited)
                    for iid in member_ids[:8]:
                        edge_key = (tag_id, iid)
                        if edge_key not in edge_set:
                            links.append({
                                "source": tag_id,
                                "target": iid,
                                "weight": 0.3,
                                "confidence": "TAG",
                            })
                            edge_set.add(edge_key)

            return {
                "data": {
                    "nodes": nodes,
                    "links": links,
                    "stats": {
                        "total_nodes": len(nodes),
                        "total_edges": len(links),
                        "total_communities": 0,
                    },
                }
            }
        except Exception as e:
            logger.warning("[SB Bridge] Graph build failed: %s", e)
            return {"status": "error", "detail": str(e)}

    # ─── Chat ─────────────────────────────────────────────────────────

    async def chat(
        self,
        message: str,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Chat with Second Brain's Gemini AI (brain-grounded)."""
        return await self._request("POST", "/chat", {
            "message": message,
            "history": history or [],
        }, timeout=30.0)

    # ─── Export / Import ──────────────────────────────────────────────

    async def export_json(self, item_type: str | None = None) -> dict[str, Any]:
        """Export all items as JSON."""
        params: dict[str, str] = {}
        if item_type:
            params["item_type"] = item_type
        return await self._request("GET", "/export", params)

    async def import_json(self, items: list[dict], reindex: bool = True) -> dict[str, Any]:
        """Bulk import items from JSON."""
        return await self._request("POST", f"/import?reindex={'true' if reindex else 'false'}", {
            "items": items,
        })

    # ═══════════════════════════════════════════════════════════════════
    # Bidirectional Sync
    # ═══════════════════════════════════════════════════════════════════

    async def sync_triplets_to_items(
        self,
        brain_type: str = "general",
    ) -> dict[str, Any]:
        """Sync BARQ brain triplets → Second Brain items.

        For each unique entity in the brain, creates or updates a
        Second Brain note with the entity's connections as structured content.
        """
        from memory_knowledge.multi_brain import multi_brain_manager

        try:
            data = multi_brain_manager.visualize(brain_type)
            nodes = data.get("nodes", [])
            links = data.get("links", [])

            if not nodes:
                return {"status": "success", "created": 0, "updated": 0, "detail": "Empty brain"}

            # Group links by source entity
            entity_connections: dict[str, list[dict[str, str]]] = {}
            for link in links:
                src = link.get("source", "")
                tgt = link.get("target", "")
                rel = link.get("relation", "RELATED_TO")
                if src and tgt:
                    entity_connections.setdefault(src, []).append({
                        "target": tgt,
                        "relation": rel,
                    })

            created = 0
            updated = 0
            errors = 0

            for entity, connections in entity_connections.items():
                try:
                    # Build content from connections
                    lines = [
                        f"# {entity}",
                        "",
                        f"**Brain:** {brain_type}",
                        f"**Connections:** {len(connections)}",
                        "",
                        "## Connections",
                        "",
                    ]
                    for conn in connections:
                        lines.append(f"- **{conn['relation']}** → {conn['target']}")

                    content = "\n".join(lines)
                    tags = [brain_type, "barq-sync", "triplet"]

                    # Check if item already exists (search by title)
                    existing = await self.search(entity, mode="text", limit=1)
                    existing_items = []
                    if isinstance(existing, dict) and existing.get("status") == "success":
                        raw = existing.get("data", [])
                        if isinstance(raw, list):
                            existing_items = raw

                    if existing_items:
                        item_id = existing_items[0].get("item", {}).get("id")
                        if item_id:
                            await self.update_item(item_id, content=content, tags=tags)
                            updated += 1
                    else:
                        await self.create_item("note", entity, content, tags)
                        created += 1

                except Exception as e:
                    logger.warning("Failed to sync entity '%s': %s", entity, e)
                    errors += 1

            return {
                "status": "success",
                "created": created,
                "updated": updated,
                "errors": errors,
                "total_entities": len(entity_connections),
                "detail": f"Synced {created} new, updated {updated} existing ({errors} errors)",
            }
        except Exception as e:
            logger.error("sync_triplets_to_items failed: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def sync_items_to_triplets(self) -> dict[str, Any]:
        """Sync Second Brain items → BARQ brain triplets.

        Parses each item's content for connection patterns and creates
        triplets in the BARQ 'general' brain.
        """
        from memory_knowledge.multi_brain import multi_brain_manager

        try:
            result = await self.list_items(limit=1000)
            items = []
            if isinstance(result, dict) and result.get("status") == "success":
                raw = result.get("data", [])
                if isinstance(raw, list):
                    items = raw

            if not items:
                return {"status": "success", "items_processed": 0, "triplets_added": 0}

            added = 0
            processed = 0

            for item in items:
                title = item.get("title", "").strip()
                content = item.get("content", "")
                tags = item.get("tags", [])
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except (json.JSONDecodeError, TypeError):
                        tags = []

                if not title:
                    continue

                processed += 1

                # Parse connection patterns from content
                # Pattern: **RELATION** → target
                # Pattern: X RELATED_TO Y / X USED_FOR Y / X IS_A Y
                patterns = [
                    re.compile(r"\*\*(\w+)\*\*\s*→\s*(.+)"),
                    re.compile(r"(\w+)\s+(RELATED_TO|USED_FOR|IS_A|HAS|KNOWS|WORKS_AT|TAGGED_AS)\s+(.+)"),
                ]

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

                            # Clean up target (remove markdown formatting)
                            target = re.sub(r"\*\*(.+?)\*\*", r"\1", target).strip()
                            if target and relation:
                                multi_brain_manager.add_triplet(
                                    "general", title, relation, target
                                )
                                added += 1

                # Add tag-based triplets
                for tag in tags:
                    if isinstance(tag, str) and tag not in ("barq-sync", "triplet", "github"):
                        multi_brain_manager.add_triplet(
                            "general", title, "TAGGED_AS", tag
                        )
                        added += 1

            return {
                "status": "success",
                "items_processed": processed,
                "triplets_added": added,
                "detail": f"Processed {processed} items, added {added} triplets",
            }
        except Exception as e:
            logger.error("sync_items_to_triplets failed: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def full_sync(self, direction: str = "both") -> dict[str, Any]:
        """Run a full bidirectional sync.

        Args:
            direction: "to_items" (BARQ→SB), "to_triplets" (SB→BARQ), or "both"
        """
        results: dict[str, Any] = {}

        if direction in ("to_items", "both"):
            results["to_items"] = await self.sync_triplets_to_items()

        if direction in ("to_triplets", "both"):
            results["to_triplets"] = await self.sync_items_to_triplets()

        results["status"] = "success"
        results["direction"] = direction
        return results


# ─── Singleton ────────────────────────────────────────────────────────────

second_brain_bridge = SecondBrainBridge()
