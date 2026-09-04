"""Channel parser listener module.

Listens on the Telethon User client for incoming channel messages, applies deduplication
and loop protection, performs keyword matching, and feeds the alert queue.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User

from src.config import AppConfig
from src.dispatcher import AlertDispatcher, AlertJob
from src.safety import DeduplicationCache, metrics
from src.storage import DynamicStore

logger = logging.getLogger("AirAlert.Parser")


def setup_parser_handlers(
    user_client: TelegramClient,
    config: AppConfig,
    store: DynamicStore,
    dedup: DeduplicationCache,
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

            # 1. Loop Guard: NEVER process messages from or to target alert chat
            if config.target_chat_id != 0 and chat_id == config.target_chat_id:
                return

            # 2. Extract chat entity and username safely
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

            # 3. Channel Filter: Strictly require channel to be in monitored channels
            monitored_channels = await store.get_channels()
            if not monitored_channels:
                # No channels configured yet; ignore messages until channels are added via /add_channel
                return

            if not store.is_channel_monitored(chat_id, chat_username):
                return

            message_id = event.message.id

            # 4. Deduplication & Anti-Spam Guard
            is_duplicate = await dedup.check_and_add(chat_id, message_id)
            if is_duplicate:
                metrics.duplicates_filtered += 1
                return

            # 5. Extract text
            raw_text = event.raw_text or event.message.message or ""
            if not raw_text.strip():
                return

            metrics.messages_scanned += 1

            # 6. Keyword matching (checks critical tier first, then standard)
            match_result = store.match_text(raw_text)
            if not match_result:
                return

            # 7. Construct AlertJob and enqueue for 1 msg/sec dispatch
            message_date = event.message.date or datetime.datetime.now(datetime.timezone.utc)
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
