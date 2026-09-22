"""
EvoMap Evolution Logger — tracks latency, buffer overflows, WebSocket drops,
and other performance events for self-optimization over time.

Events are stored as timestamped JSON files in ``./memory/evolution/``, one
file per day, following the GEP (Genome Evolution Protocol) pattern.

Each event has a ``type``, ``timestamp``, ``duration_ms`` (where applicable),
and metadata dict. The evolver engine can analyse these files offline to
detect performance bottlenecks and suggest configuration changes.

Event types
-----------
- ``ttfb``: Time to first token (wake word → first LLM token)
- ``full_response``: Total response time (wake word → TTS playback end)
- ``buffer_overflow``: PyAudio input buffer overflow
- ``ws_disconnect``: WebSocket connection drop
- ``stt_latency``: Speech-to-text transcription duration
- ``tts_latency``: Text-to-speech synthesis duration
- ``llm_latency``: LLM inference duration
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("barq.evolution")

# ─── Constants ──────────────────────────────────────────────────────────────

EVOLUTION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "memory",
    "evolution",
)

MAX_EVENTS_PER_DAY = 10_000  # safety cap per daily file
MAX_DAYS_TO_KEEP = 30        # auto-prune files older than this


# ═════════════════════════════════════════════════════════════════════════════
#  EvolutionEvent dataclass
# ═════════════════════════════════════════════════════════════════════════════


class EvolutionEvent:
    """A single self-optimisation event recorded by the evolver system.

    Attributes:
        event_type: Short identifier like ``\"ttfb\"``, ``\"buffer_overflow\"``.
        timestamp: ISO-8601 UTC string of when the event occurred.
        duration_ms: How long the operation took (0 if not applicable).
        metadata: Dict of extra contextual info (model name, device, etc.).
    """

    def __init__(
        self,
        event_type: str,
        duration_ms: float = 0.0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.event_type = event_type
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.duration_ms = duration_ms
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.event_type,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }


# ═════════════════════════════════════════════════════════════════════════════
#  EvolutionLogger — Thread-Safe Event Recorder
# ═════════════════════════════════════════════════════════════════════════════


class EvolutionLogger:
    """Thread-safe singleton that records performance events to disk.

    Usage::

        logger = EvolutionLogger.get_instance()
        logger.record("ttfb", duration_ms=850, metadata={"model": "llama3.2:3b"})
        logger.record("buffer_overflow", metadata={"device": "Microphone (Realtek)"})

    Each day gets its own JSON file at ``./memory/evolution/YYYY-MM-DD.json``.
    Old files are auto-pruned on init (files > 30 days).
    """

    _instance: Optional[EvolutionLogger] = None
    _lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls) -> EvolutionLogger:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._events: list[EvolutionEvent] = []
            self._events_lock = threading.Lock()
            self._save_lock = threading.Lock()
            self._dir = Path(EVOLUTION_DIR)
            self._dir.mkdir(parents=True, exist_ok=True)
            self._prune_old_files()
            self._load_today()
            self._initialized = True
            logger.info("EvolutionLogger initialised at %s", self._dir)

    @classmethod
    def get_instance(cls) -> EvolutionLogger:
        if cls._instance is None:
            cls()
        assert cls._instance is not None
        return cls._instance

    # ── Public API ──────────────────────────────────────────────────

    def record(
        self,
        event_type: str,
        duration_ms: float = 0.0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record a single evolution event.

        Args:
            event_type: Event identifier (e.g. ``\"ttfb\"``, ``\"buffer_overflow\"``).
            duration_ms: How long the operation took in milliseconds.
            metadata: Optional dict with extra context.
        """
        event = EvolutionEvent(event_type, duration_ms, metadata)
        logger.debug(
            "[Evolution] %s: %.1fms %s",
            event_type, duration_ms,
            json.dumps(metadata) if metadata else "",
        )

        with self._events_lock:
            self._events.append(event)
            # Flush to disk every 50 events or if it's a critical event
            if len(self._events) >= 50 or event_type in (
                "buffer_overflow", "ws_disconnect", "stt_error", "llm_error",
            ):
                self._flush()

    def query(
        self,
        event_type: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query recent evolution events, newest first.

        Args:
            event_type: Filter to a specific event type (or None for all).
            since: ISO-8601 timestamp — only return events after this time.
            limit: Max events to return.
            offset: Pagination offset.

        Returns:
            List of event dicts, newest first.
        """
        with self._events_lock:
            events = list(reversed(self._events))

        if event_type:
            events = [e for e in events if e.event_type == event_type]

        if since:
            events = [e for e in events if e.timestamp >= since]

        return [e.to_dict() for e in events[offset:offset + limit]]

    def get_summary(self) -> dict[str, Any]:
        """Return aggregate stats across all recorded events.

        Returns counts and average durations per event type.
        """
        with self._events_lock:
            events = list(self._events)

        summary: dict[str, dict[str, Any]] = {}
        for e in events:
            if e.event_type not in summary:
                summary[e.event_type] = {
                    "event_type": e.event_type,
                    "count": 0,
                    "total_duration_ms": 0.0,
                    "avg_duration_ms": 0.0,
                    "last_timestamp": "",
                }
            s = summary[e.event_type]
            s["count"] += 1
            s["total_duration_ms"] += e.duration_ms
            if e.timestamp > s["last_timestamp"]:
                s["last_timestamp"] = e.timestamp

        for s in summary.values():
            if s["count"] > 0:
                s["avg_duration_ms"] = round(s["total_duration_ms"] / s["count"], 2)
            del s["total_duration_ms"]

        return {
            "total_events": len(events),
            "event_types": sorted(summary.values(), key=lambda x: x["last_timestamp"], reverse=True),
            "storage_path": str(self._dir),
            "daily_file": self._daily_path().name,
        }

    def get_latency_report(
        self,
        event_types: Optional[list[str]] = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Percentile report for duration-bearing events.

        ``get_summary()`` only reports an average, which is useless for latency:
        one 3 s outlier hides behind nine fast wakes. This returns p50/p95 so a
        regression in the tail is visible.

        Args:
            event_types: Restrict to these event types (None = all).
            limit: Analyse only the most recent N events.

        Returns:
            ``{"event_types": {name: {count, p50_ms, p95_ms, min_ms, max_ms}},
            "window": N}``
        """
        with self._events_lock:
            events = list(self._events)

        if event_types:
            events = [e for e in events if e.event_type in event_types]
        events = events[-limit:]

        by_type: dict[str, list[float]] = {}
        for e in events:
            by_type.setdefault(e.event_type, []).append(e.duration_ms)

        report: dict[str, dict[str, Any]] = {}
        for et, values in by_type.items():
            values.sort()
            report[et] = {
                "count": len(values),
                "p50_ms": round(_percentile(values, 50), 1),
                "p95_ms": round(_percentile(values, 95), 1),
                "min_ms": round(values[0], 1),
                "max_ms": round(values[-1], 1),
            }

        return {"event_types": report, "window": len(events)}

    def flush(self) -> None:
        """Force-flush all in-memory events to disk."""
        self._flush()

    # ── Internal ────────────────────────────────────────────────────

    def _daily_path(self) -> Path:
        """Path to today's event file: ``./memory/evolution/YYYY-MM-DD.json``."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._dir / f"{date_str}.json"

    def _load_today(self) -> None:
        """Load any existing events from today's file on startup."""
        path = self._daily_path()
        if not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            entries = data.get("events", []) if isinstance(data, dict) else data
            for entry in entries[-MAX_EVENTS_PER_DAY:]:
                event = EvolutionEvent(
                    event_type=entry.get("type", "unknown"),
                    duration_ms=entry.get("duration_ms", 0.0),
                    metadata=entry.get("metadata", {}),
                )
                event.timestamp = entry.get("timestamp", event.timestamp)
                self._events.append(event)
            logger.info("[Evolution] Loaded %d events from today's file", len(entries))
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("[Evolution] Failed to load today's events: %s", exc)

    def _flush(self) -> None:
        """Write all buffered events to today's JSON file."""
        with self._save_lock:
            path = self._daily_path()
            try:
                # Read existing events from disk to avoid overwriting
                existing: list[dict] = []
                if path.exists():
                    try:
                        raw = path.read_text(encoding="utf-8")
                        data = json.loads(raw)
                        existing = data.get("events", data) if isinstance(data, dict) else data
                    except (json.JSONDecodeError, OSError):
                        existing = []

                # Merge with in-memory events (dedup by timestamp)
                with self._events_lock:
                    in_memory = [e.to_dict() for e in self._events]

                # Combine: existing first, then new in-memory events
                seen_timestamps = {e["timestamp"] for e in existing}
                merged = list(existing)
                for e in in_memory:
                    if e["timestamp"] not in seen_timestamps:
                        merged.append(e)
                        seen_timestamps.add(e["timestamp"])

                # Cap at MAX_EVENTS_PER_DAY
                if len(merged) > MAX_EVENTS_PER_DAY:
                    merged = merged[-MAX_EVENTS_PER_DAY:]

                payload = {
                    "_meta": {
                        "version": 1,
                        "event_count": len(merged),
                        "saved_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "events": merged,
                }
                path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
                logger.debug("[Evolution] Flushed %d events to %s", len(in_memory), path.name)
            except OSError as exc:
                logger.warning("[Evolution] Failed to flush events: %s", exc)

    def _prune_old_files(self) -> None:
        """Remove evolution files older than MAX_DAYS_TO_KEEP."""
        try:
            now = time.time()
            for f in self._dir.glob("*.json"):
                if not f.is_file():
                    continue
                age_days = (now - f.stat().st_mtime) / 86400
                if age_days > MAX_DAYS_TO_KEEP:
                    f.unlink()
                    logger.info("[Evolution] Pruned old event file: %s", f.name)
        except OSError as exc:
            logger.warning("[Evolution] Failed to prune old files: %s", exc)


# ─── Module-level singleton ────────────────────────────────────────────────

_evolution_logger: EvolutionLogger = EvolutionLogger.get_instance()


def get_evolution_logger() -> EvolutionLogger:
    """Return the singleton ``EvolutionLogger`` instance."""
    return _evolution_logger


# ═════════════════════════════════════════════════════════════════════════════
#  Timing Context Manager — measure and auto-record operation durations
# ═════════════════════════════════════════════════════════════════════════════


class TimingContext:
    """Async context manager that measures operation duration and records it.

    Usage::

        async with TimingContext(\"ttfb\", metadata={\"model\": \"llama3\"}) as tc:
            result = await some_operation()
        # tc.duration_ms is now set, event automatically recorded
    """

    def __init__(
        self,
        event_type: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.event_type = event_type
        self.metadata = metadata or {}
        self.duration_ms: float = 0.0
        self._start: float = 0.0
        self._logger = get_evolution_logger()

    async def __aenter__(self) -> TimingContext:
        self._start = time.perf_counter()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.duration_ms = (time.perf_counter() - self._start) * 1000
        self._logger.record(self.event_type, self.duration_ms, self.metadata)


# ═════════════════════════════════════════════════════════════════════════════
#  Voice-cycle timing — wake → audible response
# ═════════════════════════════════════════════════════════════════════════════
#
# A "voice cycle" is one wake → conversation. ``mark_voice_cycle()`` stores each
# event's duration as its OFFSET FROM THE CYCLE START, so every pairwise delta
# (wake→connected, connected→greeting, wake→first audible audio) is derivable
# from the recorded events without any extra plumbing.
#
# Why this exists: the pre-existing ``ttfb`` event is only recorded on the
# Ollama text-chat path (ai/responder.py, metadata={"model": "ollama"}), so the
# Gemini Live / Deepgram VOICE path — the one users actually feel — had no
# numbers at all.

_cycle_start: float = 0.0
_cycle_first: set[str] = set()


def begin_voice_cycle() -> None:
    """Start a new voice cycle. Call at wake-word detection."""
    global _cycle_start, _cycle_first
    _cycle_start = time.perf_counter()
    _cycle_first = set()


def voice_cycle_age_ms() -> Optional[float]:
    """Milliseconds since the current cycle began, or None if no cycle is open."""
    if _cycle_start <= 0:
        return None
    return (time.perf_counter() - _cycle_start) * 1000


def mark_voice_cycle(
    event_type: str,
    metadata: Optional[dict[str, Any]] = None,
    once: bool = False,
) -> Optional[float]:
    """Record an event whose duration is the offset from the cycle start.

    Args:
        event_type: e.g. ``"voice_first_audio"``.
        metadata: Extra context stored alongside the event.
        once: Record only the first occurrence in this cycle — for single-shot
            marks like the first audible audio, which would otherwise be
            re-recorded on every audio chunk.

    Returns:
        The recorded offset in ms, or None if no cycle is open (or the mark was
        already recorded and ``once`` was set).
    """
    global _cycle_first
    if _cycle_start <= 0:
        return None
    if once:
        if event_type in _cycle_first:
            return None
        _cycle_first.add(event_type)

    offset_ms = (time.perf_counter() - _cycle_start) * 1000
    get_evolution_logger().record(event_type, offset_ms, metadata)
    return offset_ms


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] * (hi - k) + sorted_values[hi] * (k - lo)


# ═════════════════════════════════════════════════════════════════════════════
#  CLI Entrypoint
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """CLI entrypoint for querying evolution events.

    Usage::

        python -m voice.evolution_logger status
        python -m voice.evolution_logger query --type ttfb --limit 10
        python -m voice.evolution_logger query --since 2026-07-17T12:00:00Z
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="BARQ Evolution Logger — query self-optimisation events",
    )
    subparsers = parser.add_subparsers(dest="command")

    # status
    subparsers.add_parser("status", help="Show evolution logger summary")

    # query
    query_parser = subparsers.add_parser("query", help="Query evolution events")
    query_parser.add_argument("--type", "-t", type=str, default=None, help="Event type filter")
    query_parser.add_argument("--since", "-s", type=str, default=None, help="ISO-8601 start timestamp")
    query_parser.add_argument("--limit", "-l", type=int, default=20, help="Max events")
    query_parser.add_argument("--offset", "-o", type=int, default=0, help="Offset")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    evo = get_evolution_logger()

    if args.command == "status":
        summary = evo.get_summary()
        print(f"Total events:       {summary['total_events']}")
        print(f"Storage path:       {summary['storage_path']}")
        print(f"Daily file:         {summary['daily_file']}")
        print()
        for et in summary["event_types"]:
            print(f"  {et['event_type']:20s}  count={et['count']:4d}  "
                  f"avg={et['avg_duration_ms']:>8.1f}ms  "
                  f"latest={et['last_timestamp'][:19]}")

    elif args.command == "query":
        results = evo.query(
            event_type=args.type,
            since=args.since,
            limit=args.limit,
            offset=args.offset,
        )
        for r in results:
            meta = f" | {json.dumps(r['metadata'])}" if r.get("metadata") else ""
            print(f"  [{r['type']:20s}] {r['timestamp'][:23]}  "
                  f"{r['duration_ms']:>8.1f}ms{meta}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
