# BARQ ↔ Second Brain Integration Plan

## Overview

Integrate Second Brain's knowledge graph UIs, semantic search, and knowledge management features into BARQ's Brain page, with **bidirectional sync** so changes in either system reflect in the other.

**Second Brain** (Flask, port 8000): Items-based knowledge + vis.js graphs + semantic search
**BARQ Brain** (FastAPI, port 8956): Triplet-based knowledge graphs + ForceGraph2D + voice skills

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    BARQ Electron App                     │
│                                                          │
│  ┌──────────────┐    ┌──────────────────────────────┐   │
│  │  BrainPage   │    │  New: UnifiedKnowledgePage    │   │
│  │  (existing)  │    │  (Second Brain UIs ported)    │   │
│  └──────┬───────┘    └──────────────┬───────────────┘   │
│         │                           │                    │
│  ┌──────▼───────────────────────────▼───────────────┐   │
│  │              BARQ Python Backend                   │   │
│  │              (port 8956)                           │   │
│  │                                                    │   │
│  │  ┌────────────────┐  ┌─────────────────────────┐ │   │
│  │  │ brain_api.py   │  │ NEW: second_brain_bridge │ │   │
│  │  │ (existing)     │  │ (API client + sync)      │ │   │
│  │  └────────────────┘  └──────────┬──────────────┘ │   │
│  │                                  │                 │   │
│  └──────────────────────────────────┼─────────────────┘   │
│                                     │ HTTP                │
└─────────────────────────────────────┼─────────────────────┘
                                      │
                    ┌─────────────────▼──────────────────┐
                    │       Second Brain (Flask)          │
                    │       (port 8000)                   │
                    │                                     │
                    │  /api/v1/items                      │
                    │  /api/v1/search                     │
                    │  /api/v1/graph                      │
                    │  /api/v1/chat                       │
                    │  brain.db (SQLite + FTS5 + embeddings)│
                    └─────────────────────────────────────┘
```

---

## Phase 1: Backend Bridge (BARQ → Second Brain)

### 1.1 Add Second Brain config to BARQ

**File**: `python/config.py`

```python
# Second Brain integration
second_brain_url: str = os.getenv("SECOND_BRAIN_URL", "http://127.0.0.1:8000")
second_brain_api_key: str = os.getenv("SECOND_BRAIN_API_KEY", "")
second_brain_enabled: bool = os.getenv("SECOND_BRAIN_ENABLED", "false").lower() == "true"
```

### 1.2 Create Second Brain Bridge Client

**New file**: `python/memory_knowledge/second_brain_bridge.py`

```python
"""
Second Brain Bridge — connects BARQ to a Second Brain instance.

Provides:
- Read/write access to Second Brain items via its REST API
- Semantic search bridged from Second Brain
- Graph data bridged from Second Brain
- Bidirectional sync (triplets ↔ items)
"""

import httpx
import json
import asyncio
from typing import Any, Optional
from config import get_settings

class SecondBrainBridge:
    """HTTP client for Second Brain's REST API."""
    
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
    
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
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h
    
    async def _request(self, method: str, path: str, data: dict = None) -> dict:
        """Make an HTTP request to Second Brain."""
        if not self.enabled:
            return {"status": "error", "detail": "Second Brain integration disabled"}
        
        url = f"{self.base_url}/api/v1{path}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
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
                    return {"status": "error", "detail": f"HTTP {resp.status_code}: {resp.text[:500]}"}
                return {"status": "success", "data": resp.json()}
        except httpx.ConnectError:
            return {"status": "error", "detail": "Second Brain not reachable — is it running on port 8000?"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}
    
    # ─── Items API ────────────────────────────────────────────────────
    
    async def list_items(self, item_type: str = None, limit: int = 50) -> dict:
        """List items from Second Brain."""
        params = {"limit": limit}
        if item_type:
            params["item_type"] = item_type
        return await self._request("GET", "/items", params)
    
    async def get_item(self, item_id: int) -> dict:
        """Get a single item."""
        return await self._request("GET", f"/items/{item_id}")
    
    async def create_item(self, item_type: str, title: str, content: str, 
                          tags: list = None, metadata: dict = None) -> dict:
        """Create a new item in Second Brain."""
        return await self._request("POST", "/items", {
            "item_type": item_type,
            "title": title,
            "content": content,
            "tags": tags or [],
            "metadata": metadata or {},
        })
    
    async def update_item(self, item_id: int, **kwargs) -> dict:
        """Update an item in Second Brain."""
        return await self._request("PUT", f"/items/{item_id}", kwargs)
    
    async def delete_item(self, item_id: int) -> dict:
        """Delete an item from Second Brain."""
        return await self._request("DELETE", f"/items/{item_id}")
    
    # ─── Search API ───────────────────────────────────────────────────
    
    async def search(self, query: str, mode: str = "hybrid", limit: int = 20) -> dict:
        """Search Second Brain (text/semantic/hybrid)."""
        return await self._request("POST", "/search", {
            "query": query,
            "mode": mode,
            "limit": limit,
        })
    
    # ─── Graph API ────────────────────────────────────────────────────
    
    async def get_graph(self) -> dict:
        """Get the knowledge graph from Second Brain."""
        return await self._request("GET", "/graph")
    
    # ─── Stats ────────────────────────────────────────────────────────
    
    async def get_stats(self) -> dict:
        """Get Second Brain statistics."""
        return await self._request("GET", "/stats")
    
    async def get_system_health(self) -> dict:
        """Get Second Brain system health."""
        return await self._request("GET", "/system")
    
    # ─── Chat ─────────────────────────────────────────────────────────
    
    async def chat(self, message: str, history: list = None) -> dict:
        """Chat with Second Brain's Gemini AI."""
        return await self._request("POST", "/chat", {
            "message": message,
            "history": history or [],
        })
    
    # ─── Bidirectional Sync ───────────────────────────────────────────
    
    async def sync_triplets_to_items(self, brain_type: str = "general") -> dict:
        """
        Sync BARQ brain triplets → Second Brain items.
        
        For each unique entity in the brain, creates/updates a Second Brain item
        with the entity's connections as structured content.
        """
        from memory_knowledge.multi_brain import multi_brain_manager
        
        try:
            data = multi_brain_manager.visualize(brain_type)
            nodes = data.get("nodes", [])
            links = data.get("links", [])
            
            # Group links by source entity
            entity_connections: dict[str, list] = {}
            for link in links:
                src = link.get("source", "")
                tgt = link.get("target", "")
                rel = link.get("relation", "RELATED_TO")
                if src not in entity_connections:
                    entity_connections[src] = []
                entity_connections[src].append({"target": tgt, "relation": rel})
            
            created = 0
            updated = 0
            for entity, connections in entity_connections.items():
                # Build content from connections
                lines = [f"# {entity}", "", "## Connections", ""]
                for conn in connections:
                    lines.append(f"- **{conn['relation']}** → {conn['target']}")
                
                content = "\n".join(lines)
                tags = [brain_type, "barq-sync", "triplet"]
                
                # Check if item already exists (by title)
                existing = await self.search(entity, mode="text", limit=1)
                items = existing.get("data", []) if isinstance(existing, dict) else []
                
                if items and len(items) > 0:
                    # Update existing
                    item_id = items[0].get("item", {}).get("id")
                    if item_id:
                        await self.update_item(item_id, content=content, tags=tags)
                        updated += 1
                else:
                    # Create new
                    await self.create_item("note", entity, content, tags)
                    created += 1
            
            return {
                "status": "success",
                "created": created,
                "updated": updated,
                "total_entities": len(entity_connections),
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}
    
    async def sync_items_to_triplets(self) -> dict:
        """
        Sync Second Brain items → BARQ brain triplets.
        
        Parses each item's content and creates triplets in the 'general' brain.
        """
        from memory_knowledge.multi_brain import multi_brain_manager
        
        try:
            result = await self.list_items(limit=1000)
            items = result.get("data", []) if isinstance(result, dict) else []
            
            added = 0
            for item in items:
                title = item.get("title", "")
                content = item.get("content", "")
                tags = item.get("tags", [])
                
                if not title:
                    continue
                
                # Simple triplet extraction from content
                # Look for "X → Y" or "X RELATED_TO Y" patterns
                import re
                patterns = [
                    r'\*\*(\w+)\*\*\s*→\s*(.+)',          # **RELATION** → target
                    r'(\w+)\s+RELATED_TO\s+(.+)',          # X RELATED_TO Y
                    r'(\w+)\s+USED_FOR\s+(.+)',            # X USED_FOR Y
                    r'(\w+)\s+IS_A\s+(.+)',                # X IS_A Y
                ]
                
                for line in content.split("\n"):
                    for pattern in patterns:
                        match = re.search(pattern, line)
                        if match:
                            relation = match.group(1).upper().replace(" ", "_")
                            target = match.group(2).strip()
                            multi_brain_manager.add_triplet(
                                "general", title, relation, target
                            )
                            added += 1
                
                # Also add a tag-based triplet
                for tag in tags:
                    if tag not in ("barq-sync", "triplet"):
                        multi_brain_manager.add_triplet(
                            "general", title, "TAGGED_AS", tag
                        )
                        added += 1
            
            return {
                "status": "success",
                "items_processed": len(items),
                "triplets_added": added,
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}


# Singleton
second_brain_bridge = SecondBrainBridge()
```

### 1.3 Add Bridge API Routes to BARQ

**New file**: `python/memory_knowledge/second_brain_routes.py`

```python
"""
Second Brain Integration API — exposes Second Brain data through BARQ's backend.

Endpoints:
- GET  /api/second-brain/status        — Connection status + stats
- GET  /api/second-brain/items         — List items (proxied)
- POST /api/second-brain/items         — Create item (proxied)
- GET  /api/second-brain/search        — Search (proxied)
- GET  /api/second-brain/graph         — Graph data (proxied)
- POST /api/second-brain/sync/to-items — BARQ triplets → Second Brain items
- POST /api/second-brain/sync/to-triplets — Second Brain items → BARQ triplets
- POST /api/second-brain/chat          — Chat with Second Brain's Gemini
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any, Optional

router = APIRouter(prefix="/api/second-brain", tags=["Second Brain Integration"])


class ItemCreate(BaseModel):
    item_type: str = "note"
    title: str
    content: str = ""
    tags: list[str] = []
    metadata: dict = {}


class SearchRequest(BaseModel):
    query: str
    mode: str = "hybrid"
    limit: int = 20


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Check Second Brain connection and get stats."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    
    if not second_brain_bridge.enabled:
        return {
            "connected": False,
            "enabled": False,
            "message": "Second Brain integration disabled. Set SECOND_BRAIN_ENABLED=true",
        }
    
    stats = await second_brain_bridge.get_stats()
    health = await second_brain_bridge.get_system_health()
    
    connected = stats.get("status") == "success"
    
    return {
        "connected": connected,
        "enabled": True,
        "url": second_brain_bridge.base_url,
        "stats": stats.get("data", {}),
        "health": health.get("data", {}),
    }


@router.get("/items")
async def list_items(item_type: str = None, limit: int = 50) -> dict[str, Any]:
    """List items from Second Brain."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    return await second_brain_bridge.list_items(item_type=item_type, limit=limit)


@router.post("/items")
async def create_item(request: ItemCreate) -> dict[str, Any]:
    """Create an item in Second Brain."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    return await second_brain_bridge.create_item(
        item_type=request.item_type,
        title=request.title,
        content=request.content,
        tags=request.tags,
        metadata=request.metadata,
    )


@router.post("/search")
async def search(request: SearchRequest) -> dict[str, Any]:
    """Search Second Brain."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    return await second_brain_bridge.search(
        query=request.query,
        mode=request.mode,
        limit=request.limit,
    )


@router.get("/graph")
async def get_graph() -> dict[str, Any]:
    """Get the knowledge graph from Second Brain."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    return await second_brain_bridge.get_graph()


@router.post("/sync/to-items")
async def sync_to_items(brain_type: str = "general") -> dict[str, Any]:
    """Sync BARQ triplets → Second Brain items."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    return await second_brain_bridge.sync_triplets_to_items(brain_type=brain_type)


@router.post("/sync/to-triplets")
async def sync_to_triplets() -> dict[str, Any]:
    """Sync Second Brain items → BARQ triplets."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    return await second_brain_bridge.sync_items_to_triplets()


@router.post("/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    """Chat with Second Brain's Gemini AI (brain-grounded)."""
    from memory_knowledge.second_brain_bridge import second_brain_bridge
    return await second_brain_bridge.chat(
        message=request.message,
        history=request.history,
    )
```

### 1.4 Register Routes in BARQ's Main

**File**: `python/main.py` — add after brain_api_router:

```python
from memory_knowledge.second_brain_routes import router as second_brain_router
app.include_router(second_brain_router, tags=["Second Brain Integration"])
```

---

## Phase 2: Frontend Integration (BARQ ← Second Brain)

### 2.1 Create UnifiedKnowledgePage in BARQ

**New file**: `src/renderer/src/pages/UnifiedKnowledgePage.tsx`

This page combines:
- BARQ's existing brain tabs + ForceGraph2D
- Second Brain's vis.js graph + item grid + search + AI chat

**Structure**:
```
UnifiedKnowledgePage
├── Header (tabs: BARQ Brains | Second Brain | Unified)
├── BARQ Brains Tab
│   └── (existing BrainPage content — tabs, ForceGraph2D, sidebar)
├── Second Brain Tab
│   ├── Search bar (hybrid search via Second Brain API)
│   ├── vis.js graph view (from /api/second-brain/graph)
│   ├── Item grid (from /api/second-brain/items)
│   └── AI Chat panel (via /api/second-brain/chat)
├── Unified Tab
│   ├── Combined graph (BARQ triplets + SB items merged)
│   ├── Cross-system search
│   └── Sync status + manual sync buttons
└── Sync Panel
    ├── Last sync time
    ├── BARQ→SB sync button
    ├── SB→BARQ sync button
    └── Auto-sync toggle
```

### 2.2 Port Second Brain's vis.js Graph to React

Create a React wrapper for Second Brain's vis.js graph:

**New file**: `src/renderer/src/components/SecondBrainGraph.tsx`

```tsx
// Wraps vis.js network graph in a React component
// Fetches data from /api/second-brain/graph
// Renders with vis-network library
```

### 2.3 Add UnifiedKnowledgePage to Routes

**File**: `src/renderer/src/App.tsx`

```tsx
import { UnifiedKnowledgePage } from './pages/UnifiedKnowledgePage'

// Add route:
<Route path="/unified-knowledge" element={<AnimatedPage><UnifiedKnowledgePage /></AnimatedPage>} />
```

### 2.4 Add to Sidebar Navigation

**File**: `src/renderer/src/components/Sidebar.tsx`

Add to Tools section:
```tsx
{ path: '/unified-knowledge', label: 'Unified Knowledge', icon: GitBranch },
```

---

## Phase 3: Bidirectional Sync

### 3.1 Auto-Sync on Changes

**BARQ → Second Brain**: When a triplet is added/removed in BARQ, automatically push to Second Brain.

**Implementation**: Hook into `multi_brain_manager.add_triplet()`:

```python
# In multi_brain.py, after add_triplet:
if get_settings().second_brain_enabled:
    asyncio.create_task(_sync_triplet_to_second_brain(brain_type, subj, rel, obj))
```

**Second Brain → BARQ**: When an item is created/updated in Second Brain, pull into BARQ.

**Implementation**: Second Brain webhook or BARQ polling:

```python
# Polling approach (in BARQ's background task):
async def _poll_second_brain_changes():
    """Check for new/updated items in Second Brain every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        if not get_settings().second_brain_enabled:
            continue
        # Fetch recent items, sync to triplets
        await second_brain_bridge.sync_items_to_triplets()
```

### 3.2 WebSocket Sync Events

**Second Brain** already has Socket.IO — extend it to emit events that BARQ can listen to:

```python
# In Second Brain's routes.py, after item create/update/delete:
sio.emit("item_changed", {"action": "created", "item": parsed_item}, broadcast=True)
```

**BARQ** connects to Second Brain's WebSocket and applies changes:

```python
# In BARQ's second_brain_bridge.py:
async def _listen_for_changes():
    """Connect to Second Brain's Socket.IO and apply real-time changes."""
    import socketio
    sio = socketio.AsyncClient()
    
    @sio.on("item_changed")
    async def on_item_changed(data):
        # Sync the changed item to BARQ's brain
        ...
    
    await sio.connect(second_brain_bridge.base_url)
```

### 3.3 Conflict Resolution

Since both systems can modify the same knowledge:
- **BARQ is authoritative for triplets** (subject→relation→object)
- **Second Brain is authoritative for items** (title+content+tags)
- **Sync maps**: Entity ↔ Item ID mapping stored in a `sync_mappings` table

```sql
CREATE TABLE IF NOT EXISTS brain_sync_mappings (
    barq_entity TEXT NOT NULL,
    barq_brain TEXT NOT NULL,
    sb_item_id INTEGER NOT NULL,
    last_sync_at TEXT NOT NULL,
    sync_direction TEXT NOT NULL,  -- 'to_items' | 'to_triplets' | 'both'
    PRIMARY KEY (barq_entity, barq_brain)
);
```

---

## Phase 4: Voice Integration

### 4.1 Add Second Brain Voice Skills

**File**: `python/voice/feature_bridge.py` — add:

```python
def barq_second_brain_search(query: str = "", mode: str = "hybrid") -> dict[str, Any]:
    """Search Second Brain's knowledge base."""
    return _request("POST", "/api/second-brain/search", {"query": query, "mode": mode})

def barq_second_brain_items(limit: int = 20) -> dict[str, Any]:
    """List recent items from Second Brain."""
    return _request("GET", "/api/second-brain/items", {"limit": limit})

def barq_second_brain_sync(direction: str = "both") -> dict[str, Any]:
    """Sync knowledge between BARQ and Second Brain."""
    if direction in ("to-items", "both"):
        _request("POST", "/api/second-brain/sync/to-items")
    if direction in ("to-triplets", "both"):
        _request("POST", "/api/second-brain/sync/to-triplets")
    return {"status": "success", "detail": f"Sync triggered: {direction}"}
```

### 4.2 Voice Commands

Add to `processQuickCommand` in App.tsx:

```tsx
} else if (cmd.includes('second brain') || cmd.includes('unified knowledge')) {
    nav('/unified-knowledge')
} else if (cmd.includes('sync knowledge') || cmd.includes('sync brains')) {
    // Trigger sync via voice
}
```

---

## Implementation Order

### Phase 1: Backend Bridge (Day 1-2)
1. Add config to `config.py`
2. Create `second_brain_bridge.py` client
3. Create `second_brain_routes.py` API routes
4. Register routes in `main.py`
5. Test connection to Second Brain

### Phase 2: Frontend Integration (Day 3-5)
1. Install `vis-network` package in BARQ
2. Create `SecondBrainGraph.tsx` component
3. Create `UnifiedKnowledgePage.tsx` with tabs
4. Add routes and sidebar entry
5. Port Second Brain's item grid + search UI
6. Port Second Brain's AI chat panel

### Phase 3: Bidirectional Sync (Day 6-7)
1. Add `sync_mappings` table to BARQ's database
2. Implement triplet→item sync
3. Implement item→triplet sync
4. Add auto-sync background task
5. Add manual sync buttons in UI
6. Add sync status indicators

### Phase 4: Voice Integration (Day 8)
1. Add Second Brain voice skills
2. Add voice commands for navigation
3. Add voice-triggered sync

---

## Files to Create/Modify

### New Files (BARQ)
```
python/memory_knowledge/second_brain_bridge.py    — API client
python/memory_knowledge/second_brain_routes.py    — FastAPI routes
src/renderer/src/pages/UnifiedKnowledgePage.tsx    — Combined UI page
src/renderer/src/components/SecondBrainGraph.tsx   — vis.js React wrapper
```

### Modified Files (BARQ)
```
python/config.py                    — Add Second Brain settings
python/main.py                      — Register new routes
python/voice/feature_bridge.py      — Add voice skills
src/renderer/src/App.tsx            — Add route + nav aliases
src/renderer/src/components/Sidebar.tsx — Add nav item
```

### Modified Files (Second Brain)
```
app/routes.py                       — Add webhook events for sync
```

---

## Environment Variables

```bash
# BARQ .env
SECOND_BRAIN_ENABLED=true
SECOND_BRAIN_URL=http://127.0.0.1:8000
SECOND_BRAIN_API_KEY=sb_your_key_here

# Second Brain .env (already has GEMINI_API_KEY)
BRAIN_DB_PATH=brain.db
```

---

## Testing

1. **Connection test**: BARQ → Second Brain health check
2. **CRUD test**: Create item in BARQ → appears in Second Brain
3. **Search test**: Search in BARQ returns Second Brain results
4. **Graph test**: Unified graph shows both data sources
5. **Sync test**: Triplet added in BARQ → item created in Second Brain
6. **Voice test**: "Search second brain for Python" → returns results
7. **Real-time test**: Item created in Second Brain → BARQ UI updates
