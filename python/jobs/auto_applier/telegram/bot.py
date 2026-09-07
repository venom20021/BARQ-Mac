"""
aiogram bot for two-way interactive Telegram control.

Supports both polling and webhook modes.
Integrates with BARQ's existing Telegram channel.
"""

import logging
from typing import Any, Callable, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from ..config import CONFIG
from .handlers import Router as main_router

logger = logging.getLogger("barq.auto_applier.telegram")


class AutoApplyBot:
    """Telegram bot for interactive job application control."""

    def __init__(self):
        self._token: str = CONFIG.telegram_bot_token
        self._chat_id: str = CONFIG.telegram_chat_id
        self._bot: Optional[Bot] = None
        self._dp: Optional[Dispatcher] = None
        self._running = False

        # Callbacks for when user clicks [⚡ Apply Via Bot] or [❌ Skip Role]
        self.on_apply: Optional[Callable] = None
        self.on_skip: Optional[Callable] = None
        self.on_scan: Optional[Callable] = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start_polling(self) -> None:
        """Start the bot in polling mode (simple, local)."""
        if not self._token:
            logger.warning("TELEGRAM_BOT_TOKEN not configured — bot disabled")
            return

        self._bot = Bot(
            token=self._token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()
        self._dp.include_router(main_router)

        # Wire up callback handlers
        self._wire_callbacks()

        self._running = True
        logger.info("Telegram bot starting in polling mode...")
        try:
            await self._dp.start_polling(self._bot)
        except Exception as exc:
            logger.error("Telegram polling error: %s", exc)
        finally:
            self._running = False
            await self._bot.session.close()

    async def start_webhook(
        self,
        webhook_url: str,
        app: Any,
        webhook_path: str = "/webhook/telegram",
    ) -> None:
        """Start the bot in webhook mode (requires public URL).

        Args:
            webhook_url: Public HTTPS URL (e.g. https://example.com)
            app: aiohttp or FastAPI application instance
            webhook_path: Path for the webhook endpoint
        """
        if not self._token:
            logger.warning("TELEGRAM_BOT_TOKEN not configured — bot disabled")
            return

        self._bot = Bot(
            token=self._token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()
        self._dp.include_router(main_router)
        self._wire_callbacks()

        full_url = f"{webhook_url.rstrip('/')}{webhook_path}"
        await self._bot.set_webhook(full_url)
        logger.info("Webhook set to: %s", full_url)

        # For aiohttp server:
        # handler = SimpleRequestHandler(dispatcher=self._dp, bot=self._bot)
        # handler.register(app, path=webhook_path)

        self._running = True

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self._bot and self._running:
            try:
                await self._bot.delete_webhook()
                await self._bot.session.close()
            except Exception as exc:
                logger.warning("Bot stop error: %s", exc)
        self._running = False

    # ── Messaging ───────────────────────────────────────────────────────

    async def send_message(self, text: str, keyboard: Any = None) -> bool:
        """Send a text message to the configured chat."""
        if not self._bot or not self._chat_id:
            return False
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)
            return False

    async def send_job_card(
        self,
        job_id: str,
        company: str,
        title: str,
        score: int,
        url: str,
        reason: str = "",
    ) -> bool:
        """Send an interactive job card with Apply/Skip buttons."""
        from .keyboards import job_application_keyboard

        text = (
            f"<b>{title}</b>\n"
            f"🏢 {company}\n"
            f"📊 Match: <b>{score}%</b>\n"
            f"🔗 <a href=\"{url}\">View Job</a>\n"
        )
        if reason:
            text += f"\n💡 {reason}"

        return await self.send_message(
            text=text,
            keyboard=job_application_keyboard(job_id),
        )

    async def send_morning_digest(self, jobs: list[dict]) -> bool:
        """Send the morning dispatch with top job matches."""
        from .keyboards import morning_digest_keyboard
        import datetime

        date_str = datetime.datetime.now().strftime("%d %b %Y")
        text = f"<b>☀️ Good Morning — {date_str}</b>\n"
        text += f"<i>{len(jobs)} matching jobs found today</i>\n\n"

        for idx, job in enumerate(jobs[:10], 1):
            text += (
                f"<b>#{idx}</b> | {job.get('company', '?')}\n"
                f"└ {job.get('title', '?')[:80]}\n"
                f"└ 📊 {job.get('score', '?')}% match\n\n"
            )

        text += "Reply with a job number or use the buttons below."
        return await self.send_message(
            text=text,
            keyboard=morning_digest_keyboard(),
        )

    async def send_notification(self, text: str) -> bool:
        """Send a simple notification (status updates, errors)."""
        return await self.send_message(text)

    async def send_html_message(
        self,
        text: str,
        title: str = "",
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        """Send a pre-formatted HTML message directly.

        Returns dict with 'success' (bool) and optional 'error' (str)
        to match TelegramChannel's interface.
        """
        if not self._bot or not self._chat_id:
            return {"success": False, "error": "Telegram not configured"}

        try:
            message = text
            if title:
                import html as html_mod
                safe_title = html_mod.escape(title)
                message = f"<b>{safe_title}</b>\n\n{text}"

            result = await self._bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_notification=disable_notification,
            )
            return {"success": True, "message_id": result.message_id}
        except Exception as exc:
            logger.warning("Telegram HTML send failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def send_document_from_bytes(
        self,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        """Send a document (PDF, image, etc.) from in-memory bytes.

        Uses aiogram's Bot.send_document() with FSInputFile-like bytes.
        Returns dict with 'success' (bool) and optional 'error' (str).
        """
        if not self._bot or not self._chat_id:
            return {"success": False, "error": "Telegram not configured"}

        if not file_bytes:
            return {"success": False, "error": "Empty file bytes"}

        try:
            from aiogram.types import BufferedInputFile

            input_file = BufferedInputFile(file_bytes, filename=filename)
            result = await self._bot.send_document(
                chat_id=self._chat_id,
                document=input_file,
                caption=caption[:1000] if caption else "",
                parse_mode=parse_mode,
            )
            return {"success": True, "message_id": result.message_id}
        except Exception as exc:
            logger.warning("Telegram document send failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def send_document(
        self,
        document_path: str,
        caption: str = "",
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        """Send a document from a file path.

        Returns dict with 'success' (bool) and optional 'error' (str).
        """
        if not self._bot or not self._chat_id:
            return {"success": False, "error": "Telegram not configured"}

        import os
        if not os.path.isfile(document_path):
            return {"success": False, "error": f"File not found: {document_path}"}

        try:
            from aiogram.types import FSInputFile

            input_file = FSInputFile(document_path)
            result = await self._bot.send_document(
                chat_id=self._chat_id,
                document=input_file,
                caption=caption[:1000] if caption else "",
                parse_mode=parse_mode,
            )
            return {"success": True, "message_id": result.message_id}
        except Exception as exc:
            logger.warning("Telegram document send failed: %s", exc)
            return {"success": False, "error": str(exc)}

    # ── Internals ───────────────────────────────────────────────────────

    def _wire_callbacks(self) -> None:
        """Wire up the handler callbacks to our methods.

        This is called by the handlers module via set_bot_instance().
        """
        # Store bot reference for handlers to use
        from . import handlers as h
        h.set_bot_instance(self)
