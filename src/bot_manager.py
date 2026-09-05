"""Bot command management module.

Handles interactive commands inside the designated target chat to dynamically manage
keywords, critical keywords, channels, and query health status.
"""

from __future__ import annotations

import html
import logging
from typing import Optional

from telethon import TelegramClient, events

from src.config import AppConfig
from src.dispatcher import AlertDispatcher
from src.safety import metrics, safe_api_call
from src.storage import DynamicStore

logger = logging.getLogger("AirAlert.BotManager")


async def register_admin_bot_commands(bot: TelegramClient) -> None:
    """Configure Telegram UI bot command scopes if supported by installed Telethon version."""
    try:
        from telethon.tl.functions.bots import SetBotCommandsRequest
        from telethon.tl.types import BotCommand, BotCommandScopeDefault
        from telethon.tl import types

        # Resolve exact admin scope class across Telethon schema versions
        scope_admin_cls = getattr(
            types,
            "BotCommandScopeChatsAdmins",
            getattr(types, "BotCommandScopeChatAdministrators", None),
        )

        admin_commands = [
            BotCommand(command="help", description="Довідка команд бота"),
            BotCommand(command="add_key", description="Додати звичайне ключове слово"),
            BotCommand(command="del_key", description="Видалити ключове слово"),
            BotCommand(command="add_critical", description="Додати критичне слово (звук ON)"),
            BotCommand(command="del_critical", description="Видалити критичне слово"),
            BotCommand(command="list_keys", description="Список усіх активних слів"),
            BotCommand(command="add_channel", description="Додати канал до моніторингу"),
            BotCommand(command="del_channel", description="Видалити канал з моніторингу"),
            BotCommand(command="list_channels", description="Список каналів моніторингу"),
            BotCommand(command="status", description="Метрики системи та аптайм"),
            BotCommand(command="id", description="Показати ID поточного чату"),
        ]

        # 1. Clear suggestions for regular members in groups
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=[],
        ))

        # 2. Expose suggestions strictly to chat administrators
        if scope_admin_cls is not None:
            await bot(SetBotCommandsRequest(
                scope=scope_admin_cls(),
                lang_code="",
                commands=admin_commands,
            ))
            logger.info("Registered Telegram bot command scope: hidden from members, visible only to chat admins.")
        else:
            logger.info("Admin command scope class not found in Telethon types; commands protected at runtime.")
    except Exception as exc:
        logger.warning("Could not set bot command scope on Telegram servers: %s", exc)


def setup_bot_handlers(
    bot: TelegramClient,
    config: AppConfig,
    store: DynamicStore,
    dispatcher: AlertDispatcher,
) -> None:
    """Register command handlers for the Telegram Bot client."""

    def is_authorized_chat(event: events.NewMessage.Event) -> bool:
        """Check if message is from the authorized target chat or DM."""
        is_auth = False
        if config.target_chat_id == 0:
            is_auth = True
        elif event.chat_id == config.target_chat_id:
            is_auth = True
        else:
            # Check alternative ID formats (-100... vs -...)
            str_target = str(config.target_chat_id)
            if str_target.startswith("-100") and event.chat_id == int("-" + str_target[4:]):
                is_auth = True
            elif str_target.startswith("-") and not str_target.startswith("-100") and event.chat_id == int("-100" + str_target[1:]):
                is_auth = True

        if is_auth and event.chat:
            dispatcher.set_target_entity(event.chat)

        return is_auth

    async def is_sender_admin(event: events.NewMessage.Event) -> bool:
        """Check if sender is an admin or creator in group/channel, or sender in private DM."""
        if not is_authorized_chat(event):
            return False

        # Private chat with bot is always admin-controlled by the user
        if event.is_private:
            return True

        # In groups/supergroups, query sender permissions
        try:
            sender_id = event.sender_id
            if not sender_id:
                return False

            perms = await bot.get_permissions(event.chat_id, sender_id)
            if perms and (perms.is_admin or perms.is_creator):
                return True
        except Exception as exc:
            logger.warning("Could not check permissions for user %s: %s", event.sender_id, exc)

        # Silent ignore: do not post public errors in group to keep chat completely clean
        return False

    @bot.on(events.NewMessage(pattern=r"^/(?:start|help)(?:@\w+)?$"))
    async def handle_help(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        help_text = (
            "🤖 <b>AirAlert Monitor Bot - Довідка команд</b>\n\n"
            "<b>Управління ключовими словами:</b>\n"
            "• <code>/add_key [слово]</code> - Додати звичайне ключове слово\n"
            "• <code>/del_key [слово]</code> - Видалити слово зі словника\n"
            "• <code>/add_critical [слово]</code> - Додати КРИТИЧНЕ слово (звук ON 🚨)\n"
            "• <code>/del_critical [слово]</code> - Видалити критичне слово\n"
            "• <code>/list_keys</code> - Показати всі активні ключові слова\n\n"
            "<b>Управління каналами:</b>\n"
            "• <code>/add_channel [@канал або ID]</code> - Додати канал для моніторингу\n"
            "• <code>/del_channel [@канал або ID]</code> - Видалити канал\n"
            "• <code>/list_channels</code> - Список каналів моніторингу\n\n"
            "<b>Система:</b>\n"
            "• <code>/status</code> - Метрики стану, аптайм, черга сповіщень\n"
            "• <code>/id</code> - Показати поточний ID цього чату\n"
        )
        await safe_api_call(
            lambda: event.reply(help_text, parse_mode="html"),
            timeout_seconds=config.api_timeout_seconds,
            action_name="Bot Help Reply",
        )

    @bot.on(events.NewMessage(pattern=r"^/id(?:@\w+)?$"))
    async def handle_id(event: events.NewMessage.Event) -> None:
        if not is_authorized_chat(event):
            return
        text = f"ℹ️ <b>Chat ID:</b> <code>{event.chat_id}</code>"
        await safe_api_call(
            lambda: event.reply(text, parse_mode="html"),
            timeout_seconds=config.api_timeout_seconds,
            action_name="Bot ID Reply",
        )

    @bot.on(events.NewMessage(pattern=r"^/add_key(?:@\w+)?(?:\s+(.+)|$)"))
    async def handle_add_key(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        arg = event.pattern_match.group(1)
        if not arg or not arg.strip():
            await event.reply("⚠️ Використання: <code>/add_key [слово або фраза]</code>", parse_mode="html")
            return

        word = arg.strip()
        added = await store.add_keyword(word, tier="standard")
        if added:
            await event.reply(f"✅ Додано звичайне ключове слово: <code>{html.escape(word)}</code>", parse_mode="html")
        else:
            await event.reply(f"ℹ️ Слово вже є у списку: <code>{html.escape(word)}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/add_critical(?:@\w+)?(?:\s+(.+)|$)"))
    async def handle_add_critical(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        arg = event.pattern_match.group(1)
        if not arg or not arg.strip():
            await event.reply("⚠️ Використання: <code>/add_critical [слово або фраза]</code>", parse_mode="html")
            return

        word = arg.strip()
        added = await store.add_keyword(word, tier="critical")
        if added:
            await event.reply(f"🚨 Додано <b>КРИТИЧНЕ</b> слово: <code>{html.escape(word)}</code>", parse_mode="html")
        else:
            await event.reply(f"ℹ️ Слово вже є у списку критичних: <code>{html.escape(word)}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/(?:del_key|del_critical)(?:@\w+)?(?:\s+(.+)|$)"))
    async def handle_del_key(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        arg = event.pattern_match.group(1)
        if not arg or not arg.strip():
            await event.reply("⚠️ Використання: <code>/del_key [слово]</code>", parse_mode="html")
            return

        word = arg.strip()
        removed = await store.remove_keyword(word)
        if removed:
            await event.reply(f"🗑 Видалено слово: <code>{html.escape(word)}</code>", parse_mode="html")
        else:
            await event.reply(f"⚠️ Слово не знайдено у словнику: <code>{html.escape(word)}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/list_keys(?:@\w+)?$"))
    async def handle_list_keys(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        keys = await store.get_keywords()
        crit_list = "\n".join(f"• <code>{html.escape(w)}</code>" for w in keys["critical"]) or "<i>(порожньо)</i>"
        std_list = "\n".join(f"• <code>{html.escape(w)}</code>" for w in keys["standard"]) or "<i>(порожньо)</i>"

        msg = (
            f"📋 <b>Активні ключові слова:</b>\n\n"
            f"🚨 <b>Критичні ({len(keys['critical'])}):</b>\n{crit_list}\n\n"
            f"⚠️ <b>Звичайні ({len(keys['standard'])}):</b>\n{std_list}"
        )
        await safe_api_call(
            lambda: event.reply(msg, parse_mode="html"),
            timeout_seconds=config.api_timeout_seconds,
            action_name="List Keys",
        )

    @bot.on(events.NewMessage(pattern=r"^/add_channel(?:@\w+)?(?:\s+(.+)|$)"))
    async def handle_add_channel(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        arg = event.pattern_match.group(1)
        if not arg or not arg.strip():
            await event.reply("⚠️ Використання: <code>/add_channel [@username або ID]</code>", parse_mode="html")
            return

        raw_ch = arg.strip()
        added = await store.add_channel(raw_ch)
        if added:
            await event.reply(f"📢 Канал додано до моніторингу: <code>{html.escape(raw_ch)}</code>", parse_mode="html")
        else:
            await event.reply(f"ℹ️ Канал вже у списку: <code>{html.escape(raw_ch)}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/del_channel(?:@\w+)?(?:\s+(.+)|$)"))
    async def handle_del_channel(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        arg = event.pattern_match.group(1)
        if not arg or not arg.strip():
            await event.reply("⚠️ Використання: <code>/del_channel [@username або ID]</code>", parse_mode="html")
            return

        raw_ch = arg.strip()
        removed = await store.remove_channel(raw_ch)
        if removed:
            await event.reply(f"🗑 Канал видалено з моніторингу: <code>{html.escape(raw_ch)}</code>", parse_mode="html")
        else:
            await event.reply(f"⚠️ Канал не знайдено у списку: <code>{html.escape(raw_ch)}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/list_channels(?:@\w+)?$"))
    async def handle_list_channels(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        channels = await store.get_channels()
        if not channels:
            ch_list = "<i>(список каналів порожній)</i>"
        else:
            ch_list = "\n".join(f"• <code>{html.escape(str(c))}</code>" for c in channels)

        msg = f"📢 <b>Канали під моніторингом ({len(channels)}):</b>\n\n{ch_list}"
        await safe_api_call(
            lambda: event.reply(msg, parse_mode="html"),
            timeout_seconds=config.api_timeout_seconds,
            action_name="List Channels",
        )

    @bot.on(events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
    async def handle_status(event: events.NewMessage.Event) -> None:
        if not await is_sender_admin(event):
            return

        keys = await store.get_keywords()
        channels = await store.get_channels()

        status_text = (
            "📊 <b>Статус системи AirAlert:</b>\n\n"
            f"⏱ <b>Uptime:</b> {metrics.get_uptime_str()}\n"
            f"📡 <b>Каналів на моніторингу:</b> {len(channels)}\n"
            f"🔑 <b>Ключових слів:</b> {len(keys['critical'])} крит. / {len(keys['standard'])} звич.\n"
            f"📥 <b>Оброблено повідомлень:</b> {metrics.messages_scanned}\n"
            f"🎯 <b>Збігів знайдено:</b> {metrics.keywords_matched}\n"
            f"🚀 <b>Надіслано алертів:</b> {metrics.alerts_forwarded}\n"
            f"🛡 <b>Відфільтровано дублікатів:</b> {metrics.duplicates_filtered}\n"
            f"📬 <b>У черзі на відправку:</b> {dispatcher.queue.qsize()}\n"
            f"⚠️ <b>Помилок зафіксовано:</b> {metrics.errors_caught}\n"
            f"⏳ <b>FloodWait пауз:</b> {metrics.flood_wait_events}\n"
        )
        await safe_api_call(
            lambda: event.reply(status_text, parse_mode="html"),
            timeout_seconds=config.api_timeout_seconds,
            action_name="Bot Status Reply",
        )
