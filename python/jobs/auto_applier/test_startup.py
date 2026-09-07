"""
BARQ Auto Applier -- Startup Test.

Verifies the unified pipeline instantiates and runs (even if it exits with no jobs found).
Run: python python/jobs/auto_applier/test_startup.py
"""

import asyncio
import importlib
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


async def main():
    sep = "=" * 50
    print(sep)
    print("BARQ Auto Applier -- Startup Test")
    print(sep)

    # 1. Config check (lazy import)
    print()
    try:
        cfg_mod = importlib.import_module("jobs.auto_applier.config")
        PROFILE = cfg_mod.PROFILE
        CONFIG = cfg_mod.CONFIG
        print(f"[Config] Profile: {PROFILE.full_name}")
        print(f"  Education: {PROFILE.education}")
        print(f"  Skills: {len(PROFILE.skills)}")
        print(f"  Experiences: {len(PROFILE.experiences)}")
        print(f"  Ollama: {CONFIG.ollama_model}")
        print(f"  Browser headless: {CONFIG.headless}")
        print(f"  Telegram: {'OK' if CONFIG.telegram_bot_token else 'MISSING'} token")
        print(f"  LinkedIn: {'OK' if CONFIG.linkedin_email else 'MISSING'} credentials")
    except ImportError as e:
        print(f"[Config] WARNING: Config unavailable: {e}")

    # 2. Test ApplicationEngine instantiation (lazy)
    print()
    print("[Init] Testing ApplicationEngine import...")
    try:
        engine_mod = importlib.import_module("jobs.auto_applier.applier.engine")
        engine = engine_mod.ApplicationEngine()
        print("  [OK] ApplicationEngine instantiated")
    except ImportError as e:
        print(f"  [SKIP] ApplicationEngine unavailable (playwright not installed): {e}")

    # 3. Test DynamicResumeBuilder instantiation (lazy)
    print()
    print("[Init] Testing DynamicResumeBuilder import...")
    try:
        builder_mod = importlib.import_module("jobs.auto_applier.resume.dynamic_builder")
        builder = builder_mod.DynamicResumeBuilder()
        print("  [OK] DynamicResumeBuilder instantiated")
    except ImportError as e:
        print(f"  [SKIP] DynamicResumeBuilder unavailable: {e}")

    # 4. Run unified pipeline (discovery only, no browser)
    print()
    print("[Run] Running unified pipeline (discovery only)...")
    try:
        from jobs.pipeline import run_pipeline
        result = await run_pipeline({"auto_apply": False, "send_telegram": False})

        print()
        print(f"[Result] Status: {result['status']}")
        print(f"  Total: {result.get('total', 0)}")
        print(f"  Succeeded: {result.get('succeeded', 0)}")
        print(f"  Failed: {result.get('failed', 0)}")
        if result.get("elapsed_seconds"):
            print(f"  Elapsed: {result['elapsed_seconds']}s")
    except Exception as e:
        print(f"  [ERROR] Pipeline failed: {e}")
        result = {"status": "error"}

    print()
    print(sep)
    if result.get("status") in ("complete", "idle"):
        print("[PASS] Pipeline startup test PASSED")
    else:
        print(f"[WARN] Pipeline returned: {result.get('status', 'unknown')}")
    print(sep)


if __name__ == "__main__":
    asyncio.run(main())
