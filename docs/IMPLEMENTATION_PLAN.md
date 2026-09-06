# Telethon Keyword Parser & Alert Bot Implementation Plan

## Architecture Overview
- **User Client (`api_id`, `api_hash`)**: Monitors target Telegram channels/groups.
- **Bot Client (`bot_token`)**: Delivers structured alert cards (quoted text, channel & matched keyword, direct link) to the target chat ID and serves dynamic management commands (`/add_key`, `/del_key`, `/add_critical`, `/del_critical`, `/list_keys`, `/add_channel`, `/del_channel`, `/status`).
- **Dynamic Storage**: `keywords.json` and `channels.json` persisted externally and reloaded seamlessly.

## Safety & Anti-Hang Safeguards
1. **1 msg/sec Rate Limiter**: Queue worker enforcing max 1 alert/sec.
2. **Explicit Timeouts**: `asyncio.wait_for` (10s) on all Telegram API operations to prevent freeze/hang.
3. **Loop & Deduplication Guard**: TTL deduplication cache `(chat_id, message_id)` and absolute blacklisting of target alert chat.
4. **FloodWait & Error Isolation**: Catch `FloodWaitError` with backoff; isolated per-message try/except.
5. **Heartbeat & VM Systemd Readiness**: 30-second ping with auto-reconnect and SIGINT/SIGTERM handlers.
