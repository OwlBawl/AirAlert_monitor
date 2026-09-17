---
name: Banners and debounce
overview: Banners, parser allowlist-first, keyword cooldown after match (record after send), drop message-id dedup so edits can fire, and three-level send queue priority.
todos:
  - id: banners
    content: Update format_alert banners + tests + README cancel note
    status: pending
  - id: parser-order
    content: Allowlist first, then loop guard; no message-id dedup on the live path
    status: pending
  - id: debounce-cache
    content: Add KeywordDebounceCache (60s alert / 300s cancel) and config constants
    status: pending
  - id: parser-wire
    content: After match_text, filter cooled keys then enqueue; record cooldown  after enqueu
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

**Record** key to cooldown just after enqueue. So that keys won't be sent few times because of sending lag.

**Filter** after `match_text`, **before** enqueue: cooling keys never enter the send queue untill their cooldown finishes.

Keyword cooldown **replaces** deduplication. So we should clean it up entirely / replace. 

Add `KeywordDebounceCache` in `[src/safety.py](src/safety.py)`:

- Key: normalized string from `KeywordMatch.matched_words` (the stored phrase, e.g. `відбій` or `повітряна тривога`).
- Scope: **global** (any channel). Same key in another channel is ignored until TTL ends.
- TTL: **60s** for `critical` and `standard`; **300s** for `cancellation_critical` and `cancellation_standard`. (set in .env file, Also check if other wait timers in app could be configured via .env)

Parser (`[src/parser.py](src/parser.py)`) **after `match_text`, before enqueue** (Task 1):

1. Keep only matched words that are **not** in cooldown for that tier family.
2. If none remain, return (do not enqueue).
3. Enqueue with the filtered `matched_words`. Do **not** record cooldown here.

If a message matches `ракета` (already sent 10s ago) and `дрон` (new), only `дрон` is sent and only `дрон` starts a new 60s window. If every matched key is cooling down, the message is dropped. Each key has it's own nonblocking cooldown timer.

Construct `KeywordDebounceCache` in `[main.py](main.py)`; pass it into parser (filter) and dispatcher (record after send).

## Tests

Add unit tests in `[tests/test_safety_and_matching.py](tests/test_safety_and_matching.py)` / `[tests/run_tests.py](tests/run_tests.py)`:

- Same key within 60s is blocked; after TTL it is allowed. 2 key test until 1 in cooldown.
- Cancellation uses 300s TTL.
- Different keys are independent.
- Mixed match: cooled key dropped, new key still enqueued.
- Stop-word (`-word`) suppression never records cooldown.
- Failed/dropped dispatch does not record cooldown; the same key can still send afterward.
- Same `message_id` after an edit can enqueue if the text now matches and keys are not in cooldown (no message-id dedup).
- Queue still drops only when size == `queue_max_size`.