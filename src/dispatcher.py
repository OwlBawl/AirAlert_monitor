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
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from telethon import TelegramClient

from src.config import AppConfig
from src.safety import AlertRateLimiter, metrics, safe_api_call
from src.storage import KeywordMatch

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

    def __init__(
        self,
        bot_client: TelegramClient,
        config: AppConfig,
        user_client: Optional[TelegramClient] = None,
    ) -> None:
        self.bot = bot_client
        self.user_client = user_client
        self.config = config
        self.queue: asyncio.Queue[AlertJob] = asyncio.Queue(maxsize=config.queue_max_size)
        self.rate_limiter = AlertRateLimiter(min_interval_seconds=config.alert_interval_seconds)
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._target_entity: Optional[Any] = None

    async def resolve_target_entity(self) -> Optional[Any]:
        """Resolve and cache target chat entity, querying dialogs or trying ID variations."""
        if self._target_entity is not None:
            return self._target_entity

        target_id = self.config.target_chat_id
        if not target_id:
            return None

        # 1. Try configured ID directly
        try:
            entity = await self.bot.get_entity(target_id)
            self._target_entity = entity
            return entity
        except Exception:
            pass

        # 2. Try alternate ID format (e.g. -1005392246014 <-> -5392246014)
        alt_id: Optional[int] = None
        str_id = str(target_id)
        if str_id.startswith("-100"):
            alt_id = int("-" + str_id[4:])
        elif str_id.startswith("-"):
            alt_id = int("-100" + str_id[1:])

        if alt_id is not None:
            try:
                entity = await self.bot.get_entity(alt_id)
                self._target_entity = entity
                logger.info("Resolved target chat using alternate ID: %d", alt_id)
                return entity
            except Exception:
                pass

        # 3. Refresh bot dialogs to sync entity cache from Telegram
        try:
            dialogs = await self.bot.get_dialogs()
            for d in dialogs:
                if d.id == target_id or (alt_id and d.id == alt_id):
                    self._target_entity = d.entity
                    return self._target_entity
        except Exception:
            pass

        logger.warning(
            "Could not resolve target chat %d. Ensure @%s is added to the group and has sent/received a message.",
            target_id,
            (await self.bot.get_me()).username,
        )
        return None

    def set_target_entity(self, entity: Any) -> None:
        """Cache entity directly when received from a group event."""
        self._target_entity = entity

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

    @staticmethod
    def format_alert(job: AlertJob) -> str:
        """Format the 3-line alert message."""
        is_critical = job.match.tier == "critical"

        # 1. Quoted message text at the top (with ‼️🚨‼️ prefix for critical alerts)
        clean_text = job.message_text.strip()
        if len(clean_text) > 3500:
            clean_text = clean_text[:3500] + "..."
        crit_prefix = "‼️🚨‼️ " if is_critical else ""
        quoted_text = f"<blockquote>{crit_prefix}{html.escape(clean_text)}</blockquote>"

        # 2. Channel and matched key(s) line-by-line
        matched_str = ", ".join(job.match.matched_words)
        channel_name = job.source_chat_title or (f"@{job.source_chat_username}" if job.source_chat_username else "Channel")
        channel_key_line = f"📢 {html.escape(channel_name)}: {html.escape(matched_str)}"

        # 3. Direct link to message
        if job.source_chat_username:
            msg_link = f"https://t.me/{job.source_chat_username}/{job.message_id}"
        else:
            clean_id = str(job.source_chat_id).replace("-100", "").replace("-", "")
            msg_link = f"https://t.me/c/{clean_id}/{job.message_id}"
        link_line = f"🔗 {msg_link}"

        return f"{quoted_text}\n{channel_key_line}\n{link_line}"

    async def _dispatch_single_alert(self, job: AlertJob) -> None:
        """Forward original message and deliver accompanying banner."""
        target_entity = await self.resolve_target_entity()
        if not target_entity:
            logger.warning("Target chat entity not resolved. Alert queued/dropped.")
            return

        alert_message = self.format_alert(job)

        # Send alert card with sound notifications enabled for all messages
        await safe_api_call(
            lambda: self.bot.send_message(
                entity=target_entity,
                message=alert_message,
                parse_mode="html",
                silent=False,
                link_preview=False,
            ),
            timeout_seconds=self.config.api_timeout_seconds,
            action_name="Alert Send",
        )

        metrics.alerts_forwarded += 1
        logger.info(
            "Alert dispatched successfully: tier=%s, chat=%s, msg_id=%s, critical=%s",
            job.match.tier,
            job.source_chat_id,
            job.message_id,
            is_critical,
        )
