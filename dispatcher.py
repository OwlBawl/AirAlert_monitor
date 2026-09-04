"""Alert dispatcher module.

Manages the rate-limited queue, executes native Telegram forwards, and sends
high-visibility tier banners with sound notifications for critical alerts.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import logging
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient

from config import AppConfig
from safety import AlertRateLimiter, metrics, safe_api_call
from storage import KeywordMatch

logger = logging.getLogger("AirAlert.Dispatcher")


@dataclass
class AlertJob:
    """Represents a matched post ready to be dispatched to the target chat."""

    source_chat_id: int
    source_chat_title: str
    source_chat_username: Optional[str]
    message_id: int
    message_date: datetime.datetime
    message_text: str
    match: KeywordMatch


class AlertDispatcher:
    """Consumes alert jobs from queue, enforcing 1 alert/sec and explicit timeouts."""

    def __init__(self, bot_client: TelegramClient, config: AppConfig) -> None:
        self.bot = bot_client
        self.config = config
        self.queue: asyncio.Queue[AlertJob] = asyncio.Queue(maxsize=1000)
        self.rate_limiter = AlertRateLimiter(min_interval_seconds=config.alert_interval_seconds)
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._running = False

    def enqueue(self, job: AlertJob) -> bool:
        """Add an alert job to the queue without blocking. Discards if queue is overloaded."""
        try:
            self.queue.put_nowait(job)
            metrics.keywords_matched += 1
            return True
        except asyncio.QueueFull:
            logger.error("Alert queue is full! Dropping alert for message %s from chat %s", job.message_id, job.source_chat_id)
            metrics.errors_caught += 1
            return False

    def start(self) -> None:
        """Start the background dispatch worker."""
        if self._worker_task is None or self._worker_task.done():
            self._running = True
            self._worker_task = asyncio.create_task(self._process_queue_loop())
            logger.info("Alert dispatcher worker started.")

    async def stop(self) -> None:
        """Gracefully stop the worker, flushing remaining items if possible."""
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Alert dispatcher worker stopped.")

    async def _process_queue_loop(self) -> None:
        """Main queue consumer loop with rate limiting and timeout guards."""
        while self._running:
            try:
                # Wait for next alert job
                job = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                # Enforce minimum rate limit interval (e.g. 1 alert / sec)
                await self.rate_limiter.wait_turn()
                await self._dispatch_single_alert(job)
            except Exception as exc:
                logger.error("Unexpected error in alert dispatch loop: %s", exc, exc_info=True)
                metrics.errors_caught += 1
            finally:
                self.queue.task_done()

    async def _dispatch_single_alert(self, job: AlertJob) -> None:
        """Forward original message and deliver accompanying banner."""
        target_chat = self.config.target_chat_id
        if not target_chat:
            logger.warning("TARGET_CHAT_ID is not configured. Alert not sent to Telegram.")
            return

        is_critical = job.match.tier == "critical"
        disable_sound = not is_critical  # Sound always ON for critical, silent for standard

        # 1. Native Telegram Forward
        forward_success = False
        try:
            forward_result = await safe_api_call(
                lambda: self.bot.forward_messages(
                    entity=target_chat,
                    messages=job.message_id,
                    from_peer=job.source_chat_id,
                    silent=disable_sound,
                ),
                timeout_seconds=self.config.api_timeout_seconds,
                action_name="Native Message Forward",
            )
            if forward_result:
                forward_success = True
        except Exception as exc:
            logger.warning("Native forward failed (possible protected content): %s", exc)

        # 2. Build message link if public username or channel ID
        if job.source_chat_username:
            link = f"https://t.me/{job.source_chat_username}/{job.message_id}"
            source_display = f'<a href="{link}">{html.escape(job.source_chat_title)}</a>'
        else:
            source_display = f"<b>{html.escape(job.source_chat_title)}</b>"

        matched_str = ", ".join(f"<code>{html.escape(w)}</code>" for w in job.match.matched_words)
        timestamp_str = job.message_date.strftime("%Y-%m-%d %H:%M:%S")

        # 3. High-visibility banner
        if is_critical:
            banner = (
                "🚨🚨🚨 <b>КРИТИЧНА ТРИВОГА / CRITICAL ALERT</b> 🚨🚨🚨\n"
                f"🎯 <b>Ключові слова:</b> {matched_str}\n"
                f"📢 <b>Джерело:</b> {source_display}\n"
                f"⏰ <b>Час:</b> {timestamp_str} UTC\n"
            )
        else:
            banner = (
                "⚠️ <b>СПОВІЩЕННЯ / KEYWORD ALERT</b> ⚠️\n"
                f"🎯 <b>Ключові слова:</b> {matched_str}\n"
                f"📢 <b>Джерело:</b> {source_display}\n"
                f"⏰ <b>Час:</b> {timestamp_str} UTC\n"
            )

        # If forward failed (e.g. forward restricted by channel), include preview excerpt
        if not forward_success and job.message_text:
            snippet = html.escape(job.message_text[:400])
            if len(job.message_text) > 400:
                snippet += "..."
            banner += f"\n💬 <b>Текст повідомлення:</b>\n<blockquote>{snippet}</blockquote>"

        # 4. Send the banner message with explicit sound flag
        await safe_api_call(
            lambda: self.bot.send_message(
                entity=target_chat,
                message=banner,
                parse_mode="html",
                silent=disable_sound,
                link_preview=False,
            ),
            timeout_seconds=self.config.api_timeout_seconds,
            action_name="Alert Banner Send",
        )

        metrics.alerts_forwarded += 1
        logger.info(
            "Alert dispatched successfully: tier=%s, chat=%s, msg_id=%s, critical=%s",
            job.match.tier,
            job.source_chat_id,
            job.message_id,
            is_critical,
        )
