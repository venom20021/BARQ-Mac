"""
Zero-Selector Form Filler.

Orchestrates the fill-submit cycle using AI element discovery:
  1. Extract DOM context (accessibility tree + interactive elements)
  2. LLM decides which element to act on
  3. Execute the action (click/type/select/upload)
  4. Detect page transitions (next page, review, submit)
"""

import asyncio
import logging
from typing import Any, Callable, Optional

from ..dom.extractor import DOMExtractor
from ..llm.element_selector import ElementSelector
from ..llm.qa_generator import QAGenerator
from ..config import PROFILE, CONFIG
from ..browser.stealth import StealthConfig

logger = logging.getLogger("barq.auto_applier.form_filler")

# ─── Page transition keywords for LLM to detect ──────────────────────────

NEXT_BUTTONS = ["next", "continue", "review", "proceed"]
SUBMIT_BUTTONS = ["submit", "submit application", "apply", "send application",
                   "submit your application", "apply now", "done"]
BACK_BUTTONS = ["back", "previous", "go back"]
EXIT_BUTTONS = ["discard", "cancel", "exit", "close"]

FORM_ACTION_PROMPT = """\
You are analyzing an application form page. Given the interactive elements below, determine:

1. What is the current stage? (initial / filling / review / submitted / error)
2. What is the next action we should take? (fill_field / click_next / click_submit / click_back / exit)
3. If fill_field: which element_id to fill and what value to enter

INTERACTIVE ELEMENTS:
{form_context}

CANDIDATE PROFILE:
- Name: {name}
- Skills: {skills}
- Has experience with: .NET, AWS, JavaScript, React, Python, Node.js

Respond with JSON only:
{{
  "stage": "initial|filling|review|submitted|error",
  "next_action": "fill_field|click_next|click_submit|click_back|exit|captcha_detected",
  "element_id": "element to fill (if fill_field)",
  "value": "value to enter (if fill_field)",
  "reason": "brief reasoning"
}}
"""


class FormFiller:
    """Fills multi-page application forms using zero-selector AI discovery."""

    def __init__(
        self,
        page: Any,
        ollama_client: Any = None,
        resume_uploader: Any = None,
    ):
        self.page = page
        self.dom = DOMExtractor(page)
        self.selector = ElementSelector(ollama_client)
        self.qa = QAGenerator(ollama_client)
        self.resume_uploader = resume_uploader
        self._max_pages = 20  # Safety limit
        self._resume_uploaded = False

    async def fill_application(
        self,
        job_context: str = "",
        progress_callback: Optional[Callable] = None,
    ) -> dict[str, Any]:
        """Fill a multi-page application form from start to submit.

        Args:
            job_context: Job description text for context-aware answers.
            progress_callback: Optional async callback(stage, message).

        Returns:
            dict with status, pages_completed, errors, submitted.
        """
        result = {
            "status": "started",
            "pages_completed": 0,
            "fields_filled": 0,
            "errors": [],
            "submitted": False,
        }

        for page_num in range(1, self._max_pages + 1):
            logger.info("Form page %d/%d", page_num, self._max_pages)
            if progress_callback:
                await progress_callback(f"page_{page_num}", f"Filling page {page_num}...")

            # 1. Wait for page to stabilize
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1.0)
            await StealthConfig.human_delay(self.page, 500, 1500)

            # 2. Extract form context
            form_context = await self.dom.extract_form_context()
            elements = await self.dom.extract()

            if not elements["elements"]:
                logger.info("No interactive elements found — form may be complete")
                result["submitted"] = True
                break

            # 3. Detect current stage + next action via LLM
            action = await self._decide_action(form_context, job_context)

            if action["next_action"] == "exit":
                logger.info("LLM decided to exit form")
                break

            elif action["next_action"] == "captcha_detected":
                logger.warning("CAPTCHA detected on page %d", page_num)
                result["errors"].append("captcha_detected")
                if CONFIG.interactive_mode:
                    logger.info("Interactive mode — pausing for human to solve CAPTCHA")
                    if progress_callback:
                        await progress_callback("captcha", "CAPTCHA detected — please solve it")
                    # Wait up to 5 minutes for human to solve
                    await self._wait_for_page_change(timeout=300)
                else:
                    logger.info("Skipping form due to CAPTCHA")
                    result["errors"].append("captcha_skipped")
                    break

            elif action["next_action"] == "click_submit":
                logger.info("LLM identified submit action")
                clicked = await self._execute_action(action, elements)
                if clicked:
                    await asyncio.sleep(2)
                    result["submitted"] = True
                    result["pages_completed"] = page_num
                    logger.info("Application submitted successfully!")
                break

            elif action["next_action"] == "click_next":
                logger.info("LLM identified next-page action")
                clicked = await self._execute_action(action, elements)
                if clicked:
                    result["pages_completed"] = page_num
                    await StealthConfig.human_delay(self.page, 1000, 2000)
                    continue

            elif action["next_action"] == "fill_field":
                filled = await self._execute_action(action, elements)
                if filled:
                    result["fields_filled"] += 1
                    await StealthConfig.human_delay(self.page, 300, 800)

                # After filling a field, attempt resume upload if not already done
                # and we have a resume_uploader (handles file input detection internally)
                if not self._resume_uploaded and self.resume_uploader:
                    upload_result = await self.resume_uploader.upload()
                    if upload_result.get("uploaded"):
                        self._resume_uploaded = True
                        result["resume_uploaded"] = True
                        result["resume_filename"] = upload_result.get("filename", "")
                        logger.info("Tailored resume uploaded during form fill: %s",
                                    upload_result.get("filename"))

            elif action["next_action"] == "click_back":
                await self._execute_action(action, elements)
                break

            # Safety: if no progress for 3 consecutive pages, break
            if page_num > 3 and result["pages_completed"] < page_num - 3:
                logger.warning("No progress detected — breaking out of form")
                result["errors"].append("stuck_no_progress")
                break

        result["status"] = "submitted" if result["submitted"] else "incomplete"
        return result

    async def _decide_action(self, form_context: str, job_context: str) -> dict:
        """Ask LLM to analyze the current page and decide the next action."""
        prompt = FORM_ACTION_PROMPT.format(
            form_context=form_context[:3000],
            name=PROFILE.full_name,
            skills=", ".join(PROFILE.skills[:10]),
        )

        # Inject failure patterns from EvoMap (learn from past failures)
        try:
            import importlib
            evo_mod = importlib.import_module("jobs.auto_applier.failure.evo_logger")
            evo = evo_mod.EvoLogger()
            failure_ctx = evo.get_llm_failure_context(max_chars=600)
            if failure_ctx:
                prompt += f"\n\n{failure_ctx}"
        except ImportError:
            pass

        try:
            action = await self.selector.ollama.generate_json(
                prompt=prompt,
                temperature=0.1,
            )
            logger.info("LLM action: %s → %s (element: %s)",
                        action.get("next_action"), action.get("reason", "")[:80],
                        action.get("element_id", "none"))
            return action
        except Exception as exc:
            logger.warning("LLM action decision failed: %s", exc)
            return {"next_action": "exit", "reason": str(exc)[:80],
                    "element_id": None, "value": None}

    async def _execute_action(self, action: dict, elements: dict) -> bool:
        """Execute the interaction decided by the LLM."""
        el_id = action.get("element_id", "")
        interaction = action.get("interaction", "click")
        value = action.get("value")

        if not el_id or el_id == "unknown":
            logger.warning("No element_id to act on")
            return False

        try:
            # Try to locate the element by ID
            locator = self.page.locator(f"#{el_id}")
            if await locator.count() == 0:
                # Fallback: try by name or aria-label
                locator = self.page.locator(f"[name=\"{el_id}\"]")
                if await locator.count() == 0:
                    locator = self.page.locator(f"[aria-label=\"{el_id}\"]")
                    if await locator.count() == 0:
                        # Broad fallback: text match
                        locator = self.page.get_by_role("any", name=el_id)
                        if await locator.count() == 0:
                            logger.warning("Element not found: %s", el_id)
                            return False

            await locator.wait_for(state="visible", timeout=5000)

            if interaction == "click":
                await locator.click()
                logger.debug("Clicked element: %s", el_id)

            elif interaction == "type":
                # Generate context-aware answer if this is a textarea/question
                actual_value = value or "yes"
                if not value:
                    # Try to get the label for context-aware Q&A
                    label = await locator.get_attribute("aria-label") or el_id
                    actual_value = await self.qa.answer_question(
                        question=label,
                        job_context="",
                    )
                    if not actual_value:
                        actual_value = "Yes"

                await locator.click()
                await locator.fill("")
                await self.page.wait_for_timeout(200)
                await locator.fill(actual_value)
                logger.debug("Typed into %s: %s", el_id, actual_value[:50])

            elif interaction == "select":
                if value:
                    await locator.select_option(value)

            elif interaction == "upload":
                if value:
                    await locator.set_input_files(value)

            return True

        except Exception as exc:
            logger.warning("Action execution failed on %s: %s", el_id, exc)
            return False

    async def _wait_for_page_change(self, timeout: int = 300) -> bool:
        """Wait for a URL change (human solving CAPTCHA)."""
        original_url = self.page.url
        try:
            await self.page.wait_for_url(
                lambda url: url != original_url,
                timeout=timeout * 1000,
            )
            return True
        except Exception:
            return False
