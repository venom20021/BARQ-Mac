"""
EvoMap Failure Protocol.

Wraps all automation failures in structured telemetry payloads logged to
./memory/evolution/error_log.json for the EvoMap evolver daemon to audit.

Each failure entry includes:
  - URL and job context
  - Error type and message
  - DOM snapshot (first 3000 chars)
  - Screenshot path (if captured)
  - Timestamp
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import CONFIG

logger = logging.getLogger("barq.auto_applier.evo")


class EvoLogger:
    """Structured failure logger for the EvoMap protocol."""

    def __init__(self):
        self._log_path = Path(CONFIG.evolution_log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    async def log_failure(
        self,
        url: str,
        error_type: str,
        error_message: str,
        dom_snapshot: str = "",
        screenshot_path: str = "",
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Log a failure payload to the EvoMap evolution log.

        Args:
            url: The job URL where the failure occurred.
            error_type: Exception class name or error category.
            error_message: Human-readable error description.
            dom_snapshot: Truncated DOM HTML for debugging.
            screenshot_path: Path to a saved screenshot (if captured).
            context: Additional context (company, title, etc.).

        Returns:
            The failure payload dict that was logged.
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": url,
            "error_type": error_type,
            "error_message": error_message[:500],
            "dom_snapshot": dom_snapshot[:3000],
            "screenshot_path": screenshot_path,
            "context": context or {},
        }

        try:
            # Load existing log
            existing: list[dict] = []
            if self._log_path.exists():
                try:
                    raw = self._log_path.read_text(encoding="utf-8")
                    if raw.strip():
                        existing = json.loads(raw)
                        if not isinstance(existing, list):
                            existing = [existing]
                except (json.JSONDecodeError, Exception):
                    existing = []

            # Append and save
            existing.append(payload)
            self._log_path.write_text(
                json.dumps(existing, indent=2, default=str),
                encoding="utf-8",
            )

            logger.info(
                "EvoMap failure logged: %s @ %s — %s",
                error_type, url[:60], error_message[:80],
            )
            return payload

        except Exception as exc:
            logger.error("Failed to write EvoMap log: %s", exc)
            return payload

    async def log_success(
        self,
        url: str,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        """Log a successful application to the evolution log."""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": url,
            "event": "application_submitted",
            "context": context or {},
        }
        try:
            existing: list[dict] = []
            if self._log_path.exists():
                raw = self._log_path.read_text(encoding="utf-8")
                if raw.strip():
                    existing = json.loads(raw)
            existing.append(payload)
            self._log_path.write_text(
                json.dumps(existing, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Failed to log success: %s", exc)

    def get_recent_failures(self, count: int = 10) -> list[dict[str, Any]]:
        """Return the most recent failure entries."""
        if not self._log_path.exists():
            return []
        try:
            data = json.loads(self._log_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # Filter to failures only
                failures = [e for e in data if "error_type" in e]
                return failures[-count:]
            return []
        except Exception:
            return []

    def get_failure_patterns(self) -> dict[str, Any]:
        """Aggregate failure patterns by error_type, domain, and URL.

        Returns a dict with:
          - by_error: {error_type: count} sorted by frequency
          - by_domain: {domain: count} sorted by frequency
          - by_url: {url: {count, last_error, error_types}} for top failing URLs
          - total_failures: total failure count
          - total_successes: total success count
          - skip_urls: list of URLs that failed 3+ times (should be skipped)
        """
        if not self._log_path.exists():
            return {
                "by_error": {}, "by_domain": {}, "by_url": {},
                "total_failures": 0, "total_successes": 0, "skip_urls": [],
            }

        try:
            data = json.loads(self._log_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return {
                    "by_error": {}, "by_domain": {}, "by_url": {},
                    "total_failures": 0, "total_successes": 0, "skip_urls": [],
                }
        except Exception:
            return {
                "by_error": {}, "by_domain": {}, "by_url": {},
                "total_failures": 0, "total_successes": 0, "skip_urls": [],
            }

        failures = [e for e in data if "error_type" in e]
        successes = [e for e in data if e.get("event") == "application_submitted"]

        # Aggregate by error type
        by_error: dict[str, int] = {}
        for f in failures:
            et = f.get("error_type", "unknown")
            by_error[et] = by_error.get(et, 0) + 1
        by_error = dict(sorted(by_error.items(), key=lambda x: x[1], reverse=True))

        # Aggregate by domain
        by_domain: dict[str, int] = {}
        for f in failures:
            url = f.get("url", "")
            try:
                from urllib.parse import urlparse
                domain = urlparse(url).netloc.lower().removeprefix("www.")
                if domain:
                    by_domain[domain] = by_domain.get(domain, 0) + 1
            except Exception:
                pass
        by_domain = dict(sorted(by_domain.items(), key=lambda x: x[1], reverse=True))

        # Aggregate by URL (top failing URLs)
        by_url: dict[str, dict] = {}
        for f in failures:
            url = f.get("url", "")
            if not url:
                continue
            if url not in by_url:
                by_url[url] = {"count": 0, "last_error": "", "error_types": set(), "last_at": ""}
            by_url[url]["count"] += 1
            by_url[url]["last_error"] = f.get("error_message", "")[:200]
            by_url[url]["error_types"].add(f.get("error_type", "unknown"))
            ts = f.get("timestamp", "")
            if ts > by_url[url]["last_at"]:
                by_url[url]["last_at"] = ts

        # Convert sets to lists for JSON serialization
        for url_data in by_url.values():
            url_data["error_types"] = list(url_data["error_types"])

        by_url = dict(sorted(by_url.items(), key=lambda x: x[1]["count"], reverse=True)[:50])

        # URLs that failed 3+ times — should be skipped
        skip_urls = [url for url, info in by_url.items() if info["count"] >= 3]

        return {
            "by_error": by_error,
            "by_domain": by_domain,
            "by_url": by_url,
            "total_failures": len(failures),
            "total_successes": len(successes),
            "skip_urls": skip_urls,
        }

    def should_skip_url(self, url: str) -> bool:
        """Check if a URL has failed 3+ times and should be skipped."""
        patterns = self.get_failure_patterns()
        url_info = patterns["by_url"].get(url)
        return url_info is not None and url_info["count"] >= 3

    def get_llm_failure_context(self, max_chars: int = 1500) -> str:
        """Generate a text summary of failure patterns for LLM prompts.

        This feeds back into the Q&A generator and form filler so the LLM
        learns from past failures (e.g., avoid CAPTCHAs on certain domains,
        use different answer strategies for specific question types).
        """
        patterns = self.get_failure_patterns()

        lines = ["PAST APPLICATION FAILURES (learn from these):\n"]

        # Top error types
        if patterns["by_error"]:
            lines.append("Most common errors:")
            for error_type, count in list(patterns["by_error"].items())[:5]:
                lines.append(f"  - {error_type}: {count} occurrences")
            lines.append("")

        # Top failing domains
        if patterns["by_domain"]:
            lines.append("Domains with most failures:")
            for domain, count in list(patterns["by_domain"].items())[:5]:
                lines.append(f"  - {domain}: {count} failures")
            lines.append("")

        # Specific failure examples
        if patterns["by_url"]:
            lines.append("Recent failure examples:")
            for url, info in list(patterns["by_url"].items())[:3]:
                error_types = ", ".join(info["error_types"][:2])
                lines.append(f"  - {url[:80]}... ({info["count"]}x, types: {error_types})")
                if info["last_error"]:
                    lines.append(f"    Last error: {info["last_error"][:120]}")
            lines.append("")

        # Skip list
        if patterns["skip_urls"]:
            lines.append(f"URLs to SKIP ({len(patterns['skip_urls'])} permanently failing):")
            for url in patterns["skip_urls"][:5]:
                lines.append(f"  - {url[:80]}")
            lines.append("")

        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result
