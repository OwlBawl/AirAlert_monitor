# AGENTS.md — Agent & AI Developer Guide for AirAlert Monitor

This repository contains an asynchronous Telegram channel monitoring service built with **Telethon** (Python 3.10+).

## Key Architecture Points

- **Dual-Client Architecture**:
  - `user_client` (`TelegramClient('user_session', api_id, api_hash)`): Runs in [src/parser.py](src/parser.py) to listen to joined channels and groups.
  - `bot_client` (`TelegramClient('bot_session', api_id, api_hash)`): Runs in [src/bot_manager.py](src/bot_manager.py) and [src/dispatcher.py](src/dispatcher.py) to forward alerts and receive commands.
- **Dynamic Data Files**:
  - [config/keywords.json](config/keywords.json): Tiered keywords (`critical` and `standard`).
  - [config/channels.json](config/channels.json): Monitored channel IDs / usernames.
  - [config/.env](config/.env): Runtime environment variables & API tokens.
- **Safety Invariants**:
  - Always use `safe_api_call` in [src/safety.py](src/safety.py) when executing Telegram API operations to enforce the 10-second timeout.
  - Dispatch rate limit is 1 alert per second ([src/safety.py:AlertRateLimiter](src/safety.py)).
  - Loop guard in [src/parser.py](src/parser.py) must always reject messages where `chat_id == config.target_chat_id`.
  - Word boundary regex in [src/storage.py](src/storage.py) uses `(?<!\w)...(?!\w)` to support Cyrillic characters.

## Quick File Index

| File | Purpose |
| :--- | :--- |
| [main.py](main.py) | Service orchestrator, connection lifecycle, 30s watchdog heartbeat |
| [src/config.py](src/config.py) | Environment variables & `AppConfig` schema |
| [src/storage.py](src/storage.py) | `DynamicStore` with atomic JSON updates and regex compilation |
| [src/safety.py](src/safety.py) | `DeduplicationCache`, `AlertRateLimiter`, `safe_api_call`, metrics |
| [src/dispatcher.py](src/dispatcher.py) | `AlertDispatcher` queue consumer, native forward, banner formatting |
| [src/parser.py](src/parser.py) | Message listener on `user_client` with deduplication & matching |
| [src/bot_manager.py](src/bot_manager.py) | Bot commands for chat members (`/add_key`, `/status`, etc.) |
| [setup.sh](setup.sh) | 1-click cloud VM setup script (virtualenv, dependencies, systemd) |
| [deploy/airalert.service](deploy/airalert.service) | Systemd background service unit |
| [tests/run_tests.py](tests/run_tests.py) | Standard-library unittest test suite |

## Running Tests

```bash
python3 -m unittest tests/run_tests.py
```
For detailed function signatures and call graphs, refer to [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md).

## Target Environment & Execution Rules

- **Platform**: Debian GNU/Linux 12 (Bookworm) or compatible Linux / POSIX system
- **User & Isolation**: Runs under a dedicated non-root user (e.g., `airalert` or `$USER`) in `~/AirAlert_monitor`
- **Virtualenv**: Dedicated virtual environment (e.g., `$HOME/telethon_env` or `./venv`)
- **Python / Pip Binary**: `<venv_dir>/bin/python` and `<venv_dir>/bin/pip`
- **Rule**: Run service and commands using `<venv_dir>/bin/python`. All persistence, logs, and sessions stay inside the project root (`config/` and `airalert.log`).
