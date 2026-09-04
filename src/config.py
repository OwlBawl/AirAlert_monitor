"""Configuration module for AirAlert Telethon monitor.

Loads settings from environment variables with safe defaults and validation.
"""

from __future__ import annotations

import os
import sys
import logging
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# Base directory paths (points to project root)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"

if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE)
else:
    load_dotenv()


def _get_env_int(key: str, default: int) -> int:
    """Parse integer from environment variable with fallback."""
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        logging.warning("Invalid integer for %s: %r. Using default %d", key, val, default)
        return default


def _get_env_float(key: str, default: float) -> float:
    """Parse float from environment variable with fallback."""
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    try:
        return float(val.strip())
    except ValueError:
        logging.warning("Invalid float for %s: %r. Using default %.2f", key, val, default)
        return default


@dataclass(frozen=True)
class AppConfig:
    """Application runtime configuration settings."""

    # Telegram API Credentials
    api_id: int
    api_hash: str
    bot_token: str

    # Target Alert Chat ID (can be group/supergroup ID or user chat ID)
    target_chat_id: int

    # Session file names
    user_session_name: str
    bot_session_name: str

    # Paths
    keywords_file: Path
    channels_file: Path

    # Safety, Memory and Timing Settings
    alert_interval_seconds: float
    api_timeout_seconds: float
    heartbeat_interval_seconds: float
    dedup_ttl_seconds: float
    dedup_max_size: int
    queue_max_size: int
    log_max_bytes: int
    log_backup_count: int

    @classmethod
    def load(cls) -> AppConfig:
        """Instantiate configuration from environment and defaults."""
        api_id = _get_env_int("API_ID", 22079200)
        api_hash = os.getenv("API_HASH", "0726a4499610a5fee1c1e6c75cd29a66").strip()
        bot_token = os.getenv("BOT_TOKEN", "5648446763:AAFlWOqmUiSXBZCrRoWkyolzpQe9Nst-ylM").strip()
        target_chat_id = _get_env_int("TARGET_CHAT_ID", 0)

        raw_user_session = os.getenv("USER_SESSION_NAME", "user_session").strip()
        user_path = Path(raw_user_session)
        user_session_name = str(user_path if user_path.is_absolute() else BASE_DIR / user_path)

        raw_bot_session = os.getenv("BOT_SESSION_NAME", "bot_session").strip()
        bot_path = Path(raw_bot_session)
        bot_session_name = str(bot_path if bot_path.is_absolute() else BASE_DIR / bot_path)

        keywords_file = DATA_DIR / "keywords.json"
        channels_file = DATA_DIR / "channels.json"

        alert_interval_seconds = _get_env_float("ALERT_INTERVAL_SECONDS", 1.0)
        api_timeout_seconds = _get_env_float("API_TIMEOUT_SECONDS", 10.0)
        heartbeat_interval_seconds = _get_env_float("HEARTBEAT_INTERVAL_SECONDS", 30.0)
        dedup_ttl_seconds = _get_env_float("DEDUP_TTL_SECONDS", 3600.0)
        dedup_max_size = _get_env_int("DEDUP_MAX_SIZE", 500)
        queue_max_size = _get_env_int("QUEUE_MAX_SIZE", 100)
        log_max_bytes = _get_env_int("LOG_MAX_BYTES", 10 * 1024 * 1024)  # 10 MB
        log_backup_count = _get_env_int("LOG_BACKUP_COUNT", 5)

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            target_chat_id=target_chat_id,
            user_session_name=user_session_name,
            bot_session_name=bot_session_name,
            keywords_file=keywords_file,
            channels_file=channels_file,
            alert_interval_seconds=alert_interval_seconds,
            api_timeout_seconds=api_timeout_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            dedup_ttl_seconds=dedup_ttl_seconds,
            dedup_max_size=dedup_max_size,
            queue_max_size=queue_max_size,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
        )


# Global singleton instance
config = AppConfig.load()
