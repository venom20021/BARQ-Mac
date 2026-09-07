"""
BARQ Auto Applier — Production-grade job automation engine.

Headful Playwright orchestration with zero-selector AI element discovery,
two-way interactive Telegram control, and EvoMap failure protocol.

Architecture:
  telegram/          ← aiogram bot with inline Apply/Skip buttons
  browser/           ← Stealth Playwright launcher with session persistence
  dom/               ← Hybrid accessibility-tree + filtered-DOM extraction
  llm/               ← Ollama wrapper for element selection + Q&A generation
  applier/           ← Zero-selector form filler + resume uploader
  boards/            ← Strategy pattern (LinkedIn, Indeed, Wellfound, etc.)
  discovery/         ← Job URL ingestion (TinyFish + LinkedIn + BARQ boards)
  failure/           ← EvoMap error logging with DOM snapshots

The pipeline orchestration has been unified into jobs/pipeline.py.
This module provides the execution engine (browser automation, form filling,
resume generation) that the main pipeline calls into.

Usage:
    from jobs.auto_applier.applier.engine import ApplicationEngine
    engine = ApplicationEngine()
    result = await engine.apply_to_job(job_url="...", company="Acme")
"""

# Lazy imports — only import when accessed to avoid crashes when
# playwright/aiogram are not installed.
import importlib as _importlib


def __getattr__(name: str):
    """Lazy module-level attribute access. Imports on first use."""
    _LAZY_MAP = {
        "AutoApplyBot": (".telegram.bot", "AutoApplyBot"),
        "ApplicationEngine": (".applier.engine", "ApplicationEngine"),
        "JobBoardStrategy": (".boards.base", "JobBoardStrategy"),
        "ApplierConfig": (".config", "ApplierConfig"),
        "DynamicResumeBuilder": (".resume.dynamic_builder", "DynamicResumeBuilder"),
        "build_tailored_resume": (".resume.dynamic_builder", "build_tailored_resume"),
        "AIResumeBridge": (".resume.ai_resume_bridge", "AIResumeBridge"),
        "JobAggregator": (".discovery.aggregator", "JobAggregator"),
        "EvoLogger": (".failure.evo_logger", "EvoLogger"),
    }
    if name in _LAZY_MAP:
        module_path, attr = _LAZY_MAP[name]
        mod = _importlib.import_module(module_path, __package__)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AutoApplyBot",
    "ApplicationEngine",
    "JobBoardStrategy",
    "ApplierConfig",
    "DynamicResumeBuilder",
    "build_tailored_resume",
    "AIResumeBridge",
    "JobAggregator",
    "EvoLogger",
]
