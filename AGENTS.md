# AGENTS.md — Agent & AI Developer Guide for AirAlert Monitor

This repository contains an asynchronous Telegram channel monitoring service built with **Telethon** (Python 3.10+).

## Key Architecture Points

- **Dual-Client Architecture**:
  - `user_client` (`TelegramClient('user_session', api_id, api_hash)`): Runs in [src/parser.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/parser.py) to listen to joined channels and groups.
  - `bot_client` (`TelegramClient('bot_session', api_id, api_hash)`): Runs in [src/bot_manager.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/bot_manager.py) and [src/dispatcher.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/dispatcher.py) to forward alerts and receive commands.
- **Dynamic Data Files**:
  - [config/keywords.json](file:///Users/Andru/Downloads/AirAlert_monitor/config/keywords.json): Tiered keywords (`critical` and `standard`).
  - [config/channels.json](file:///Users/Andru/Downloads/AirAlert_monitor/config/channels.json): Monitored channel IDs / usernames.
  - [config/.env](file:///Users/Andru/Downloads/AirAlert_monitor/config/.env): Runtime environment variables & API tokens.
- **Safety Invariants**:
  - Always use `safe_api_call` in [src/safety.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/safety.py) when executing Telegram API operations to enforce the 10-second timeout.
  - Dispatch rate limit is 1 alert per second ([src/safety.py:AlertRateLimiter](file:///Users/Andru/Downloads/AirAlert_monitor/src/safety.py)).
  - Loop guard in [src/parser.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/parser.py) must always reject messages where `chat_id == config.target_chat_id`.
  - Word boundary regex in [src/storage.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/storage.py) uses `(?<!\w)...(?!\w)` to support Cyrillic characters.

## Quick File Index

| File | Purpose |
| :--- | :--- |
| [main.py](file:///Users/Andru/Downloads/AirAlert_monitor/main.py) | Service orchestrator, connection lifecycle, 30s watchdog heartbeat |
| [src/config.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/config.py) | Environment variables & `AppConfig` schema |
| [src/storage.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/storage.py) | `DynamicStore` with atomic JSON updates and regex compilation |
| [src/safety.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/safety.py) | `DeduplicationCache`, `AlertRateLimiter`, `safe_api_call`, metrics |
| [src/dispatcher.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/dispatcher.py) | `AlertDispatcher` queue consumer, native forward, banner formatting |
| [src/parser.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/parser.py) | Message listener on `user_client` with deduplication & matching |
| [src/bot_manager.py](file:///Users/Andru/Downloads/AirAlert_monitor/src/bot_manager.py) | Bot commands for chat members (`/add_key`, `/status`, etc.) |
| [setup.sh](file:///Users/Andru/Downloads/AirAlert_monitor/setup.sh) | 1-click cloud VM setup script (virtualenv, dependencies, systemd) |
| [deploy/airalert.service](file:///Users/Andru/Downloads/AirAlert_monitor/deploy/airalert.service) | Systemd background service unit |
| [tests/run_tests.py](file:///Users/Andru/Downloads/AirAlert_monitor/tests/run_tests.py) | Standard-library unittest test suite |

## Running Tests

```bash
python3 -m unittest tests/run_tests.py
```
For detailed function signatures and call graphs, refer to [docs/PROJECT_STRUCTURE.md](file:///Users/Andru/Downloads/AirAlert_monitor/docs/PROJECT_STRUCTURE.md).

## Target VM Environment (vm-bonus)

- **Host**: `vm-bonus` (Debian GNU/Linux 12 Bookworm, kernel 6.1.0-52-cloud-amd64)
- **User & Workdir**: `andru_bonus` (`/home/andru_bonus/AirAlert_monitor`)
- **Virtualenv**: `/home/andru_bonus/telethon_env`
- **Python Binary**: `/home/andru_bonus/telethon_env/bin/python`
- **Pip Binary**: `/home/andru_bonus/telethon_env/bin/pip`
- **Rule**: All commands run under `andru_bonus` using `/home/andru_bonus/telethon_env/bin/python`. All persistence, logs, and sessions stay inside `/home/andru_bonus/`.
