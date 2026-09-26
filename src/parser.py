"""Channel parser listener module.

Listens on the Telethon User client for incoming channel messages, applies keyword debounce
and loop protection, performs keyword matching, and feeds the alert queue.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

try:
    from telethon import TelegramClient, events
    from telethon.tl.types import Channel, Chat, MessageEntityTextUrl, User
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

    class MessageEntityTextUrl:  # type: ignore[no-redef]
        pass

    class User:  # type: ignore[no-redef]
        first_name: str = ""
        last_name: Optional[str] = None
        username: Optional[str] = None

from src.config import AppConfig
from src.dispatcher import AlertDispatcher, AlertJob
from src.safety import AlertSuppressionCache, build_message_dedup_key, metrics
from src.storage import DynamicStore, KeywordMatch

logger = logging.getLogger("AirAlert.Parser")


def setup_parser_handlers(
    user_client: TelegramClient,
    config: AppConfig,
    store: DynamicStore,
    suppression_cache: AlertSuppressionCache,
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

            # 5. Keyword matching (checks cancellation first, then critical, then standard)
            match_result = store.match_text(raw_text)
            if not match_result:
                return

            # 6. Build the full-message dedup key. Telegram TextUrl anchor text
            # is removed using its UTF-16 entity offsets before normalization.
            entities = getattr(event.message, "entities", None) or ()
            text_url_spans = tuple(
                (entity.offset, entity.length)
                for entity in entities
                if isinstance(entity, MessageEntityTextUrl)
            )
            message_dedup_key = build_message_dedup_key(raw_text, text_url_spans)

            # 7. Check keyword cooldown and message dedup under one lock. Their
            # keys/TTLs remain separate and only applicable keys are reserved.
            suppression_reservation, rejected_by = await suppression_cache.check_and_reserve(
                match_result.matched_words,
                match_result.tier,
                message_dedup_key,
            )
            if suppression_reservation is None:
                if rejected_by == "message_dedup":
                    metrics.message_dedup_filtered += 1
                else:
                    metrics.keyword_cooldown_filtered += 1
                return

            try:
                match_result = KeywordMatch(
                    tier=match_result.tier,
                    matched_words=suppression_reservation.words,
                )

                # 8. Construct AlertJob and enqueue for priority dispatch.
                job = AlertJob(
                    source_chat_id=chat_id,
                    source_chat_title=chat_title,
                    source_chat_username=chat_username,
                    message_id=message_id,
                    message_date=message_date,
                    message_text=raw_text,
                    match=match_result,
                    suppression_reservation=suppression_reservation,
                )

                # An accepted active alert immediately opens its matching
                # cancellation gate before queue handoff. This state transition is
                # independent from the job reservation and is never rolled back if
                # enqueue or later Telegram delivery fails.
                await suppression_cache.reset_cancellation_for_active_tier(match_result.tier)

                enqueued = dispatcher.enqueue(job)
            except Exception:
                await suppression_cache.release(suppression_reservation)
                raise

            if not enqueued:
                await suppression_cache.release(suppression_reservation)
                return

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
