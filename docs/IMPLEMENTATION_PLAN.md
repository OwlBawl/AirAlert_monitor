# Telethon Keyword Parser & Alert Bot Implementation Plan

## Architecture Overview
- **User Client (`api_id`, `api_hash`)**: Monitors target Telegram channels/groups.
- **Bot Client (`bot_token`)**: Delivers structured alert cards (quoted text, channel & matched keyword, direct link) to the target chat ID and serves dynamic management commands (`/add_key`, `/del_key`, `/add_critical`, `/del_critical`, `/list_keys`, `/add_channel`, `/del_channel`, `/status`).
- **Dynamic Storage**: `keywords.json` and `channels.json` persisted externally and reloaded seamlessly.

## Safety & Anti-Hang Safeguards
1. **Burst-Aware Rate Limiter**: Queue worker enforcing min intervals (0.3s burst spacing, 1s standard spacing).
2. **Explicit Timeouts**: `asyncio.wait_for` (10s) on all Telegram API operations to prevent freeze/hang.
3. **Loop & Atomic Suppression Guard**: Absolute target-chat loop protection plus locked suppression state. Matching collects all logical JSON keys and applies each negative list only to its own group. New/edited events track consumed logical keys per `(chat_id, message_id)` for `MAX_MESSAGE_AGE_SECONDS + 5s`. Active candidates check existing cooldown per logical key, reserve every free critical/standard key, and send at the highest remaining tier. `cancellation_critical` and `cancellation_standard` keep separate shared tier buckets, while normalized message dedup is independent. Enqueue/send failure rolls back newly created consumed/cooldown/dedup entries ownership-safely; the accepted active alert's matching cancellation-gate reset remains final.
4. **FloodWait & Error Isolation**: Catch `FloodWaitError` with backoff; isolated per-message try/except.
5. **Heartbeat & VM Systemd Readiness**: 30-second ping with auto-reconnect, expired keyword/message suppression sweeps, and SIGINT/SIGTERM handlers.
