---
name: Banners and debounce
overview: Banners, parser allowlist-first, keyword cooldown (record on enqueue, release on failure), drop message-id dedup so edits can fire, and three-level send queue priority.
todos:
  - id: banners
    content: Update format_alert banners + tests + README cancel note
    status: pending
  - id: parser-order
    content: Allowlist first, then loop guard; no message-id dedup on the live path
    status: pending
  - id: debounce-cache
    content: Add KeywordDebounceCache (60s alert / 300s cancel) with filter, record, release, and clean_expired methods
    status: pending
  - id: parser-wire
    content: Filter cooled keys in parser before enqueue; record cooldown immediately on successful enqueue
    status: pending
  - id: queue-priority
    content: Queue priority 0 critical, 1 standard, 2 both cancellation tiers
    status: pending
  - id: debounce-tests
    content: Add TTL / mixed-key / edit-can-send tests; keep queue-full drop behavior
    status: pending
isProject: false
---

# Alert banners and keyword debounce

## Banner changes

Update `[src/dispatcher.py](src/dispatcher.py)` `format_alert` only. Critical stays `‼️🚨‼️`.

```python
if tier == "critical":
    banner = "‼️🚨‼️\n"
elif tier == "cancellation_critical":
    banner = "🟢✅🟢\n"
elif tier == "standard":
    banner = "⚠️\n"
elif tier == "cancellation_standard":
    banner = "✅\n"
else:
    banner = ""
```

Align tests in `[tests/run_tests.py](tests/run_tests.py)` `test_alert_formatting` (standard currently expects no banner). Update the `/add_cancel` banner note in `[README.md](README.md)`.

## Parser filter order

In `[src/parser.py](src/parser.py)`, Telethon still delivers all chats; Python still filters. Change the **first two drops** to:

1. **Allowlist first.** If `channels.json` is empty, drop. If `chat_id` / username is not in the list, drop. (Resolve `event.chat` only as needed for username matching.)
2. **Loop guard second.** If `chat_id == target_chat_id`, drop. This still blocks a feedback loop if the alert chat is accidentally added to the allowlist.

Then: age, text, **match**, **keyword cooldown filter**, enqueue. **No message-id dedup** on this path.

## Queue drop and priority

**Priority** in `[src/dispatcher.py](src/dispatcher.py)` `enqueue` (lower number first):

- **0** — `critical`
- **1** — `standard`
- **2** — `cancellation_critical` and `cancellation_standard`

Cancellation is no longer grouped with critical for queue order. Rate-limiter `wait_turn(is_critical=...)` should follow the same idea: burst pacing only for `critical`; standard and both cancellation tiers use standard interval pacing.


## Debounce (per keyword, all channels)

We send matched key only once while cooldown time (TTL).

And because we now use cooldown by keyword - we dont need dedup. So if message was mistyped and then fixed to match the key - it can be sent now (if other conditions are met like age and so on, and if it pass our cooldown logics ofcourse by default)

**Record** key to cooldown just after successful **enqueue**. So that duplicate incoming messages from other channels won't queue up multiple identical alerts while the first one is waiting to send.

**Filter** after `match_text`, **before** enqueue: cooling keys never enter the send queue untill their cooldown finishes.

Keyword cooldown **replaces** deduplication. So we should clean it up entirely / replace. 

Add `KeywordDebounceCache` in `[src/safety.py](src/safety.py)`:

- Key: normalized string from `KeywordMatch.matched_words` (the stored phrase, e.g. `відбій` or `повітряна тривога`).
- Scope: **global** (any channel). Same key in another channel is ignored until TTL ends.
- TTL & Timers (all configured in `config/.env` via `[src/config.py](src/config.py)`):
  - `KEYWORD_COOLDOWN_ALERT_SECONDS` (default: `60.0` for `critical` and `standard`)
  - `KEYWORD_COOLDOWN_CANCEL_SECONDS` (default: `300.0` for `cancellation_critical` and `cancellation_standard`)
  - `ALERT_INTERVAL_SECONDS` (default: `1.0` pacing for standard/cancellation)
  - `ALERT_BURST_MIN_INTERVAL_SECONDS` (default: `0.3` spacing for critical burst)
  - `ALERT_BURST_CAPACITY` (default: `3` tokens)
  - `API_TIMEOUT_SECONDS` (default: `10.0`)
  - `HEARTBEAT_INTERVAL_SECONDS` (default: `30.0`)
  - `MAX_MESSAGE_AGE_SECONDS` (default: `300.0`)
  - `MAX_FLOOD_WAIT_SECONDS` (default: `60.0`)

Parser (`[src/parser.py](src/parser.py)`) **after `match_text`, before enqueue**:

1. Keep only matched words that are **not** in cooldown for that tier family.
2. If none remain, return (do not enqueue).
3. Enqueue with the filtered `matched_words`.
4. If `enqueue` is successful, record the `matched_words` in cooldown immediately.

If a message matches `ракета` (already sent 10s ago) and `дрон` (new), only `дрон` is sent and only `дрон` starts a new 60s window. If every matched key is cooling down, the message is dropped. Each key has it's own nonblocking cooldown timer.

Construct `KeywordDebounceCache` in `[main.py](main.py)`; pass it into parser (for filtering and recording) and dispatcher (for releasing on failure).

## Tests

Add unit tests in `[tests/test_safety_and_matching.py](tests/test_safety_and_matching.py)` / `[tests/run_tests.py](tests/run_tests.py)`:

- Same key within 60s is blocked; after TTL it is allowed. 2 key test until 1 in cooldown.
- Cancellation uses 300s TTL.
- Different keys are independent.
- Mixed match: cooled key dropped, new key still enqueued.
- Stop-word (`-word`) suppression never records cooldown.
- Failed/dropped dispatch releases the cooldown; the same key can still send afterward.
- Same `message_id` after an edit can enqueue if the text now matches and keys are not in cooldown (no message-id dedup).
- Queue still drops only when size == `queue_max_size`.