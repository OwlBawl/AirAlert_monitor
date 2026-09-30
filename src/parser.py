"""Channel parser listener module.

Listens on the Telethon User client for incoming and edited channel messages,
performs logical-key matching/suppression, and feeds the alert queue.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Optional

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
            class Event:
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
    """Attach explicit new/edit intake handlers to the Telethon User client."""

    async def process_message(event: Any, event_kind: str) -> None:
        consumed_reservation = None
        suppression_reservation = None
        enqueued = False

        try:
            chat_id = event.chat_id
            if not chat_id:
                return

            # 1. Allowlist check.
            monitored_channels = await store.get_channels()
            if not monitored_channels:
                return

            if not store.is_channel_monitored(chat_id):
                chat = event.chat
                temp_username = None
                if isinstance(chat, Channel):
                    temp_username = chat.username
                elif isinstance(chat, User):
                    temp_username = chat.username

                if not store.is_channel_monitored(chat_id, temp_username):
                    return

            # 2. Loop guard.
            if config.target_chat_id != 0 and chat_id == config.target_chat_id:
                return

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

            # 3. Message age guard. Edits retain the original Telegram message date.
            message_date = event.message.date
            if message_date:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=datetime.timezone.utc)
                age_seconds = (now_utc - message_date).total_seconds()
                if age_seconds > config.max_message_age_seconds:
                    metrics.stale_messages_dropped += 1
                    logger.info(
                        "Skipping stale message event=%s msg_id=%s channel='%s' age=%.1fs max=%.1fs",
                        event_kind,
                        message_id,
                        chat_title,
                        age_seconds,
                        config.max_message_age_seconds,
                    )
                    return
            else:
                message_date = datetime.datetime.now(datetime.timezone.utc)

            # 4. Extract text.
            raw_text = event.raw_text or event.message.message or ""
            if not raw_text.strip():
                return

            metrics.messages_scanned += 1

            # 5. Collect every valid logical JSON key. Group negatives have already
            # been applied by DynamicStore only to their own positive group.
            match_result = store.match_text(raw_text)
            if not match_result:
                return

            logger.info(
                "Candidate event=%s msg_id=%s chat=%s keys critical=%s standard=%s cancellation=%s critical_context=%s",
                event_kind,
                message_id,
                chat_id,
                match_result.critical_words,
                match_result.standard_words,
                match_result.cancellation_words,
                match_result.critical_context_words,
            )

            # 6. One opportunity per logical key for this Telegram message.
            consumed_reservation, consumed_existing, consumed_new = (
                await suppression_cache.reserve_consumed(
                    chat_id,
                    message_id,
                    match_result.consumed_words,
                )
            )
            if not consumed_new:
                logger.info(
                    "Filtered event=%s msg_id=%s reason=consumed consumed_existing=%s consumed_new=()",
                    event_kind,
                    message_id,
                    consumed_existing,
                )
                return

            # 7. Build normalized full-message dedup key.
            entities = getattr(event.message, "entities", None) or ()
            text_url_spans = tuple(
                (entity.offset, entity.length)
                for entity in entities
                if isinstance(entity, MessageEntityTextUrl)
            )
            message_dedup_key = build_message_dedup_key(raw_text, text_url_spans)
            dedup_success_status = "pass" if message_dedup_key is not None else "none"

            new_key_set = set(consumed_new)
            cooldown_blocked: tuple[str, ...] = ()
            cooldown_reserved: tuple[str, ...] = ()
            selected_tier: Optional[str] = None

            if match_result.cancellation_words:
                # Cancellation keeps message-level precedence. A newly introduced
                # critical context key can therefore upgrade an edited cancellation.
                new_cancellation_words = tuple(
                    word for word in match_result.cancellation_words if word in new_key_set
                )
                display_words = new_cancellation_words or match_result.cancellation_words
                selected_tier = match_result.tier

                suppression_reservation, rejected_by = await suppression_cache.check_and_reserve(
                    display_words,
                    selected_tier,
                    message_dedup_key,
                    consumed_entries=consumed_reservation.entries,
                )
                if suppression_reservation is None:
                    if rejected_by == "message_dedup":
                        metrics.message_dedup_filtered += 1
                        dedup_status = "blocked"
                        cooldown_status = f"free:{selected_tier}"
                    else:
                        metrics.keyword_cooldown_filtered += 1
                        dedup_status = "not_checked"
                        cooldown_status = f"blocked:{selected_tier}"
                    logger.info(
                        "Filtered event=%s msg_id=%s reason=%s consumed_existing=%s consumed_new=%s cooldown=%s dedup=%s",
                        event_kind,
                        message_id,
                        rejected_by,
                        consumed_existing,
                        consumed_new,
                        cooldown_status,
                        dedup_status,
                    )
                    return

                cooldown_reserved = display_words
            else:
                new_critical_words = tuple(
                    word for word in match_result.critical_words if word in new_key_set
                )
                new_standard_words = tuple(
                    word for word in match_result.standard_words if word in new_key_set
                )

                active_result = await suppression_cache.check_and_reserve_active(
                    new_critical_words,
                    new_standard_words,
                    message_dedup_key,
                    consumed_entries=consumed_reservation.entries,
                )
                suppression_reservation = active_result.reservation
                cooldown_blocked = active_result.blocked_words
                cooldown_reserved = active_result.free_words
                selected_tier = active_result.selected_tier

                if suppression_reservation is None:
                    if active_result.rejected_by == "message_dedup":
                        metrics.message_dedup_filtered += 1
                        dedup_status = "blocked"
                    else:
                        metrics.keyword_cooldown_filtered += 1
                        dedup_status = "not_checked"
                    logger.info(
                        "Filtered event=%s msg_id=%s reason=%s consumed_existing=%s consumed_new=%s cooldown_blocked=%s cooldown_free=%s dedup=%s",
                        event_kind,
                        message_id,
                        active_result.rejected_by,
                        consumed_existing,
                        consumed_new,
                        active_result.blocked_words,
                        active_result.free_words,
                        dedup_status,
                    )
                    return

            if selected_tier is None or suppression_reservation is None:
                raise RuntimeError("Suppression accepted without a selected tier")

            final_match = KeywordMatch(
                tier=selected_tier,
                matched_words=suppression_reservation.words,
            )

            # 8. Construct job.
            job = AlertJob(
                source_chat_id=chat_id,
                source_chat_title=chat_title,
                source_chat_username=chat_username,
                message_id=message_id,
                message_date=message_date,
                message_text=raw_text,
                match=final_match,
                suppression_reservation=suppression_reservation,
            )

            # Accepted active alerts open only the matching cancellation gate. This
            # deletion of prior state remains intentionally outside rollback.
            await suppression_cache.reset_cancellation_for_active_tier(selected_tier)

            # 9. Queue handoff. Failure rolls back every state entry newly created
            # by this attempt, including consumed keys.
            enqueued = dispatcher.enqueue(job)
            if not enqueued:
                await suppression_cache.release(suppression_reservation)
                suppression_reservation = None
                consumed_reservation = None
                logger.info(
                    "Filtered event=%s msg_id=%s reason=enqueue_failed selected=%s rollback=complete",
                    event_kind,
                    message_id,
                    selected_tier,
                )
                return

            logger.info(
                "Matched event=%s tier=%s keys=%s channel='%s' id=%s msg_id=%s consumed_existing=%s consumed_new=%s cooldown_blocked=%s cooldown_reserved=%s dedup=%s enqueue=ok",
                event_kind,
                selected_tier,
                final_match.matched_words,
                chat_title,
                chat_id,
                message_id,
                consumed_existing,
                consumed_new,
                cooldown_blocked,
                cooldown_reserved,
                dedup_success_status,
            )
        except Exception as exc:
            # An exception before successful queue handoff is a failed processing
            # attempt, so all newly-owned state must become available immediately.
            if not enqueued:
                if suppression_reservation is not None:
                    await suppression_cache.release(suppression_reservation)
                elif consumed_reservation is not None:
                    await suppression_cache.release_consumed(consumed_reservation)
            logger.error(
                "Error processing message event=%s: %s",
                event_kind,
                exc,
                exc_info=True,
            )
            metrics.errors_caught += 1

    @user_client.on(events.NewMessage)
    async def handle_new_message(event: events.NewMessage.Event) -> None:
        await process_message(event, "new")

    @user_client.on(events.MessageEdited)
    async def handle_edited_message(event: events.MessageEdited.Event) -> None:
        await process_message(event, "edited")
