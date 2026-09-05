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
CONFIG_DIR = BASE_DIR / "config"
ENV_FILE = CONFIG_DIR / ".env"

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


def _prompt_and_save_missing_config() -> None:
    """Prompt user interactively in CLI if required secrets are absent or placeholders."""
    raw_api_id = os.getenv("API_ID", "").strip()
    raw_api_hash = os.getenv("API_HASH", "").strip()
    raw_bot_token = os.getenv("BOT_TOKEN", "").strip()
    raw_chat_id = os.getenv("TARGET_CHAT_ID", "").strip()

    placeholders = {
        "12345678",
        "your_api_hash_here",
        "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        "-1000000000000",
        "",
    }

    needs_prompt = (
        raw_api_id in placeholders
        or raw_api_hash in placeholders
        or raw_bot_token in placeholders
        or raw_chat_id in placeholders
    )

    if not needs_prompt:
        return

    # Check if running in an interactive terminal
    if not sys.stdin.isatty():
        logging.warning("Running non-interactively; cannot prompt for missing credentials.")
        return

    print("\n" + "=" * 60)
    print(" 🛠️  AirAlert Monitor — Initial Configuration Wizard")
    print("=" * 60)
    print("Required credentials missing in config/.env. Please enter them now:\n")

    while raw_api_id in placeholders:
        val = input("Telegram API_ID (e.g. 22079200): ").strip()
        if val.isdigit():
            raw_api_id = val
            os.environ["API_ID"] = val
        else:
            print("❌ Invalid API_ID. Must be numbers only.")

    while raw_api_hash in placeholders:
        val = input("Telegram API_HASH: ").strip()
        if val and val not in placeholders:
            raw_api_hash = val
            os.environ["API_HASH"] = val
        else:
            print("❌ API_HASH cannot be empty.")

    while raw_bot_token in placeholders:
        val = input("Telegram BOT_TOKEN (from @BotFather): ").strip()
        if ":" in val and len(val) > 20:
            raw_bot_token = val
            os.environ["BOT_TOKEN"] = val
        else:
            print("❌ Invalid bot token format. Example: 123456789:ABCdef...")

    while raw_chat_id in placeholders:
        val = input("Target Alert Chat ID (e.g. -1004471880394): ").strip()
        try:
            int(val)
            raw_chat_id = val
            os.environ["TARGET_CHAT_ID"] = val
        except ValueError:
            print("❌ Invalid Chat ID. Must be integer (e.g. -100xxxxxxxxxx).")

    # Persist back to config/.env
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target_env = CONFIG_DIR / ".env"
    env_lines = [
        "# AirAlert Monitor Configuration",
        f"API_ID={raw_api_id}",
        f"API_HASH={raw_api_hash}",
        f"BOT_TOKEN={raw_bot_token}",
        f"TARGET_CHAT_ID={raw_chat_id}",
        "",
        "# Safety & Performance Config",
        f"ALERT_INTERVAL_SECONDS={os.getenv('ALERT_INTERVAL_SECONDS', '1.0')}",
        f"API_TIMEOUT_SECONDS={os.getenv('API_TIMEOUT_SECONDS', '10.0')}",
        f"HEARTBEAT_INTERVAL_SECONDS={os.getenv('HEARTBEAT_INTERVAL_SECONDS', '30.0')}",
        f"DEDUP_TTL_SECONDS={os.getenv('DEDUP_TTL_SECONDS', '3600')}",
        f"DEDUP_MAX_SIZE={os.getenv('DEDUP_MAX_SIZE', '500')}",
        f"QUEUE_MAX_SIZE={os.getenv('QUEUE_MAX_SIZE', '100')}",
        f"LOG_MAX_BYTES={os.getenv('LOG_MAX_BYTES', '10485760')}",
        f"LOG_BACKUP_COUNT={os.getenv('LOG_BACKUP_COUNT', '5')}",
        "",
    ]
    with open(target_env, "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines))

    print(f"\n✅ Configuration saved to: {target_env}\n" + "=" * 60 + "\n")


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
        _prompt_and_save_missing_config()

        api_id = _get_env_int("API_ID", 0)
        api_hash = os.getenv("API_HASH", "").strip()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        target_chat_id = _get_env_int("TARGET_CHAT_ID", 0)

        # Ensure config directory exists
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        # Default session files are stored cleanly inside config/
        raw_user_session = os.getenv("USER_SESSION_NAME", "user_session").strip()
        user_path = Path(raw_user_session)
        user_session_name = str(user_path if user_path.is_absolute() else CONFIG_DIR / user_path)

        raw_bot_session = os.getenv("BOT_SESSION_NAME", "bot_session").strip()
        bot_path = Path(raw_bot_session)
        bot_session_name = str(bot_path if bot_path.is_absolute() else CONFIG_DIR / bot_path)

        # Dynamic JSON stores located inside config/
        keywords_file = CONFIG_DIR / "keywords.json"
        channels_file = CONFIG_DIR / "channels.json"

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
