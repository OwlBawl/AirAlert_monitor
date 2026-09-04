# AGENTS.md — Agent & AI Developer Guide for AirAlert Monitor

This repository contains an asynchronous Telegram channel monitoring service built with **Telethon** (Python 3.10+).

## Key Architecture Points

- **Dual-Client Architecture**:
  - `user_client` (`TelegramClient('user_session', api_id, api_hash)`): Runs in [parser.py](file:///Users/Andru/Downloads/AirAlert_monitor/parser.py) to listen to joined channels and groups.
  - `bot_client` (`TelegramClient('bot_session', api_id, api_hash)`): Runs in [bot_manager.py](file:///Users/Andru/Downloads/AirAlert_monitor/bot_manager.py) and [dispatcher.py](file:///Users/Andru/Downloads/AirAlert_monitor/dispatcher.py) to forward alerts and receive commands.
- **Dynamic Data Files**:
  - [keywords.json](file:///Users/Andru/Downloads/AirAlert_monitor/keywords.json): Tiered keywords (`critical` and `standard`).
  - [channels.json](file:///Users/Andru/Downloads/AirAlert_monitor/channels.json): Monitored channel IDs / usernames.
- **Safety Invariants**:
  - Always use `safe_api_call` in [safety.py](file:///Users/Andru/Downloads/AirAlert_monitor/safety.py) when executing Telegram API operations to enforce the 10-second timeout.
  - Dispatch rate limit is 1 alert per second ([safety.py:AlertRateLimiter](file:///Users/Andru/Downloads/AirAlert_monitor/safety.py)).
  - Loop guard in [parser.py](file:///Users/Andru/Downloads/AirAlert_monitor/parser.py) must always reject messages where `chat_id == config.target_chat_id`.
  - Word boundary regex in [storage.py](file:///Users/Andru/Downloads/AirAlert_monitor/storage.py) uses `(?<!\w)...(?!\w)` to support Cyrillic characters.

## Quick File Index

| File | Purpose |
| :--- | :--- |
| [main.py](file:///Users/Andru/Downloads/AirAlert_monitor/main.py) | Service orchestrator, connection lifecycle, 30s watchdog heartbeat |
| [config.py](file:///Users/Andru/Downloads/AirAlert_monitor/config.py) | Environment variables & `AppConfig` schema |
| [storage.py](file:///Users/Andru/Downloads/AirAlert_monitor/storage.py) | `DynamicStore` with atomic JSON updates and regex compilation |
| [safety.py](file:///Users/Andru/Downloads/AirAlert_monitor/safety.py) | `DeduplicationCache`, `AlertRateLimiter`, `safe_api_call`, metrics |
| [dispatcher.py](file:///Users/Andru/Downloads/AirAlert_monitor/dispatcher.py) | `AlertDispatcher` queue consumer, native forward, banner formatting |
| [parser.py](file:///Users/Andru/Downloads/AirAlert_monitor/parser.py) | Message listener on `user_client` with deduplication & matching |
| [bot_manager.py](file:///Users/Andru/Downloads/AirAlert_monitor/bot_manager.py) | Bot commands for chat members (`/add_key`, `/status`, etc.) |
| [tests/run_tests.py](file:///Users/Andru/Downloads/AirAlert_monitor/tests/run_tests.py) | Standard-library unittest test suite |

## Running Tests

```bash
python3 -m unittest tests/run_tests.py
```
For detailed function signatures and call graphs, refer to [PROJECT_STRUCTURE.md](file:///Users/Andru/Downloads/AirAlert_monitor/PROJECT_STRUCTURE.md).
