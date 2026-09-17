"""Channel parser listener module.

Listens on the Telethon User client for incoming channel messages, applies deduplication
and loop protection, performs keyword matching, and feeds the alert queue.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

try:
    from telethon import TelegramClient, events
    from telethon.tl.types import Channel, Chat, User
except ImportError:
    class TelegramClient:  # type: ignore[no-redef]
        pass

    class events:  # type: ignore[no-redef]
        class NewMessage:
            class Event:
                pass

        class MessageEdited:
            pass

    class Channel:  # type: ignore[no-redef]
        title: str = ""
        username: Optional[str] = None

    class Chat:  # type: ignore[no-redef]
        title: str = ""

    class User:  # type: ignore[no-redef]
        first_name: str = ""
        last_name: Optional[str] = None
        username: Optional[str] = None

from src.config import AppConfig
from src.dispatcher import AlertDispatcher, AlertJob
from src.safety import KeywordDebounceCache, metrics
from src.storage import DynamicStore, KeywordMatch

logger = logging.getLogger("AirAlert.Parser")


def setup_parser_handlers(
    user_client: TelegramClient,
    config: AppConfig,
    store: DynamicStore,
    debounce_cache: KeywordDebounceCache,
    dispatcher: AlertDispatcher,
) -> None:
    """Attach message intake event handlers to Telethon User account client."""

    @user_client.on(events.NewMessage)
    @user_client.on(events.MessageEdited)
    async def handle_incoming_message(event: events.NewMessage.Event) -> None:
        """Process incoming or edited message from monitored channels."""
        try:
            chat_id = event.chat_id
            if not chat_id:
                return

            # 1. Allowlist Check: Fast path by chat_id
            monitored_channels = await store.get_channels()
            if not monitored_channels:
                return

            if not store.is_channel_monitored(chat_id):
                # Fallback: resolve entity to check by username
                chat = event.chat
                temp_username = None
                if isinstance(chat, Channel):
                    temp_username = chat.username
                elif isinstance(chat, User):
                    temp_username = chat.username
                
                if not store.is_channel_monitored(chat_id, temp_username):
                    return

            # 2. Loop Guard: NEVER process messages from or to target alert chat
            if config.target_chat_id != 0 and chat_id == config.target_chat_id:
                return

            # Extract chat title & username for downstream logging
            chat_title = "Unknown Channel"
            chat_username: Optional[str] = None
            chat = event.chat
            if isinstance(chat, (Channel, Chat)):
                chat_title = chat.title or "Channel"
                if isinstance(chat, Channel):
                    chat_username = chat.username
            elif isinstance(chat, User):
                chat_title = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "User"
                chat_username = chat.username

            message_id = event.message.id

            # 3. Message Age Guard: Drop stale, historical, or old re-edited messages (> 5 mins)
            message_date = event.message.date
            if message_date:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=datetime.timezone.utc)
                age_seconds = (now_utc - message_date).total_seconds()
                if age_seconds > config.max_message_age_seconds:
                    metrics.stale_messages_dropped += 1
                    logger.info(
                        "Skipping stale message %s from '%s' (age: %.1fs > max %.1fs)",
                        message_id,
                        chat_title,
                        age_seconds,
                        config.max_message_age_seconds,
                    )
                    return
            else:
                message_date = datetime.datetime.now(datetime.timezone.utc)

            # 4. Extract text
            raw_text = event.raw_text or event.message.message or ""
            if not raw_text.strip():
                return

            metrics.messages_scanned += 1

            # 5. Keyword matching (checks critical tier first, then standard)
            match_result = store.match_text(raw_text)
            if not match_result:
                return

            # 6. Keyword Debounce Filter
            uncooled_words = await debounce_cache.filter_uncooled(match_result.matched_words, match_result.tier)
            if not uncooled_words:
                metrics.duplicates_filtered += 1
                return

            match_result = KeywordMatch(tier=match_result.tier, matched_words=uncooled_words)

            # 7. Construct AlertJob and enqueue for priority dispatch
            job = AlertJob(
                source_chat_id=chat_id,
                source_chat_title=chat_title,
                source_chat_username=chat_username,
                message_id=message_id,
                message_date=message_date,
                message_text=raw_text,
                match=match_result,
            )

            enqueued = dispatcher.enqueue(job)
            if enqueued:
                await debounce_cache.record(uncooled_words)
                logger.info(
                    "Matched %s keywords %s in channel '%s' (id=%s, msg_id=%s)",
                    match_result.tier,
                    match_result.matched_words,
                    chat_title,
                    chat_id,
                    message_id,
                )
        except Exception as exc:
            logger.error("Error processing incoming message event: %s", exc, exc_info=True)
            metrics.errors_caught += 1
