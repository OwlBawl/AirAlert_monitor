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

# Base directory paths
BASE_DIR = Path(__file__).resolve().parent
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

    # Safety and Timing Settings
    alert_interval_seconds: float
    api_timeout_seconds: float
    heartbeat_interval_seconds: float
    dedup_ttl_seconds: float
    dedup_max_size: int

    @classmethod
    def load(cls) -> AppConfig:
        """Instantiate configuration from environment and defaults."""
        api_id = _get_env_int("API_ID", 22079200)
        api_hash = os.getenv("API_HASH", "0726a4499610a5fee1c1e6c75cd29a66").strip()
        bot_token = os.getenv("BOT_TOKEN", "5648446763:AAFlWOqmUiSXBZCrRoWkyolzpQe9Nst-ylM").strip()
        target_chat_id = _get_env_int("TARGET_CHAT_ID", 0)

        user_session_name = str(BASE_DIR / os.getenv("USER_SESSION_NAME", "user_session"))
        bot_session_name = str(BASE_DIR / os.getenv("BOT_SESSION_NAME", "bot_session"))

        keywords_file = BASE_DIR / "keywords.json"
        channels_file = BASE_DIR / "channels.json"

        alert_interval_seconds = _get_env_float("ALERT_INTERVAL_SECONDS", 1.0)
        api_timeout_seconds = _get_env_float("API_TIMEOUT_SECONDS", 10.0)
        heartbeat_interval_seconds = _get_env_float("HEARTBEAT_INTERVAL_SECONDS", 30.0)
        dedup_ttl_seconds = _get_env_float("DEDUP_TTL_SECONDS", 3600.0)
        dedup_max_size = _get_env_int("DEDUP_MAX_SIZE", 5000)

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
        )


# Global singleton instance
config = AppConfig.load()
