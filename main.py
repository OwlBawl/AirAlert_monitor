"""Main entrypoint and lifecycle orchestrator for AirAlert Telethon monitor.

Manages dual-client lifecycle (User account parser + Bot account manager/dispatcher),
watchdog heartbeat, and graceful shutdown signal handlers.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from telethon import TelegramClient

from bot_manager import setup_bot_handlers
from config import config
from dispatcher import AlertDispatcher
from parser import setup_parser_handlers
from safety import DeduplicationCache, metrics, safe_api_call
from storage import DynamicStore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("airalert.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("AirAlert.Main")


class AirAlertService:
    """Orchestrates clients, workers, and background heartbeat."""

    def __init__(self) -> None:
        self.config = config
        self.store = DynamicStore(
            keywords_file=config.keywords_file,
            channels_file=config.channels_file,
        )
        self.dedup = DeduplicationCache(
            max_size=config.dedup_max_size,
            ttl_seconds=config.dedup_ttl_seconds,
        )

        # Clients
        self.user_client = TelegramClient(
            self.config.user_session_name,
            self.config.api_id,
            self.config.api_hash,
        )
        self.bot_client = TelegramClient(
            self.config.bot_session_name,
            self.config.api_id,
            self.config.api_hash,
        )

        self.dispatcher: Optional[AlertDispatcher] = None
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._shutdown_event = asyncio.Event()

    async def _heartbeat_loop(self) -> None:
        """Periodic watchdog verifying client connectivity and logging health."""
        iteration = 0
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.heartbeat_interval_seconds)
                iteration += 1

                # Check user client connection
                if not self.user_client.is_connected():
                    logger.warning("Watchdog: User client disconnected. Reconnecting...")
                    await safe_api_call(
                        self.user_client.connect,
                        timeout_seconds=self.config.api_timeout_seconds,
                        action_name="User Client Reconnect",
                    )

                # Check bot client connection
                if not self.bot_client.is_connected():
                    logger.warning("Watchdog: Bot client disconnected. Reconnecting...")
                    await safe_api_call(
                        self.bot_client.connect,
                        timeout_seconds=self.config.api_timeout_seconds,
                        action_name="Bot Client Reconnect",
                    )

                # Log periodic status report every 10 iterations (~5 minutes)
                if iteration % 10 == 0:
                    logger.info(
                        "Health status: uptime=%s, scanned=%d, matched=%d, forwarded=%d, dedup_size=%d",
                        metrics.get_uptime_str(),
                        metrics.messages_scanned,
                        metrics.keywords_matched,
                        metrics.alerts_forwarded,
                        await self.dedup.size(),
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in heartbeat loop: %s", exc, exc_info=True)

    async def start(self) -> None:
        """Initialize all subsystems and connect clients."""
        logger.info("Starting AirAlert Monitor Service...")

        # 1. Load keywords and channels from disk
        await self.store.load_all()

        # 2. Start Bot Client
        logger.info("Connecting Bot Client...")
        await self.bot_client.start(bot_token=self.config.bot_token)
        bot_me = await self.bot_client.get_me()
        logger.info("Bot Client connected successfully as @%s (id=%s)", bot_me.username, bot_me.id)

        # 3. Start Alert Dispatcher
        self.dispatcher = AlertDispatcher(self.bot_client, self.config)
        self.dispatcher.start()

        # 4. Attach Bot command handlers
        setup_bot_handlers(self.bot_client, self.config, self.store, self.dispatcher)

        # 5. Start User Client (interactive login prompt if session not saved)
        logger.info("Connecting User Client...")
        await self.user_client.start()
        user_me = await self.user_client.get_me()
        logger.info(
            "User Client connected successfully as %s %s (phone=%s, id=%s)",
            user_me.first_name,
            user_me.last_name or "",
            user_me.phone,
            user_me.id,
        )

        # 6. Attach Channel intake listener to User client
        setup_parser_handlers(self.user_client, self.config, self.store, self.dedup, self.dispatcher)

        # 7. Start watchdog heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info("AirAlert Monitor is fully operational.")
        logger.info("Monitoring channels with 1 alert/sec pacing and strict escape timeouts.")

    async def stop(self) -> None:
        """Gracefully terminate background tasks and disconnect sessions."""
        logger.info("Shutting down AirAlert Monitor Service...")
        self._shutdown_event.set()

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        if self.dispatcher:
            await self.dispatcher.stop()

        if self.user_client.is_connected():
            await self.user_client.disconnect()

        if self.bot_client.is_connected():
            await self.bot_client.disconnect()

        logger.info("AirAlert Monitor Service stopped cleanly.")

    async def run_until_disconnected(self) -> None:
        """Run until stop signal received or clients disconnect."""
        loop = asyncio.get_running_loop()

        def _signal_handler() -> None:
            logger.info("Received termination signal.")
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows fallback (if ever run on Windows)
                pass

        await self._shutdown_event.wait()


async def main() -> None:
    service = AirAlertService()
    try:
        await service.start()
        await service.run_until_disconnected()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        logger.critical("Fatal error in service: %s", exc, exc_info=True)
    finally:
        await service.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
