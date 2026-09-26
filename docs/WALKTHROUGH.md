# AirAlert Monitor — Verification & Walkthrough

The AirAlert dual-client Telegram keyword monitor is built, configured, and tested.

## Changes Overview

- [src/config.py](src/config.py): Environment configuration loader with safety constants (1 msg/sec interval, 10s API timeout, 30s heartbeat).
- [src/safety.py](src/safety.py): 
  - `AlertSuppressionCache`: Atomic active-keyword cooldown + independent shared cancellation-tier buckets + normalized cross-channel message dedup cache; accepted active alerts clear only their matching cancellation bucket before queue handoff.
  - `AlertRateLimiter`: Burst-aware alert pacing mechanism.
  - `safe_api_call`: Strict `asyncio.wait_for(..., timeout=10.0)` wrapper with `FloodWaitError` backoff.
  - `ServiceMetrics`: Telemetry for uptime, scanned messages, keyword-cooldown filtering, message-dedup filtering, and errors.
- [src/storage.py](src/storage.py): Dynamic JSON storage for `config/keywords.json` and `config/channels.json` with unicode word-boundary regex (`(?<!\w)...(?!\w)`).
- [src/dispatcher.py](src/dispatcher.py): Queue worker handling native message forwards and tier alert banners (sound enabled for Critical).
- [src/bot_manager.py](src/bot_manager.py): Interactive bot commands (`/add_key`, `/del_key`, `/add_critical`, `/del_critical`, `/list_keys`, `/add_channel`, `/del_channel`, `/list_channels`, `/status`, `/id`).
- [src/parser.py](src/parser.py): User account listener with loop guard, channel filtering, and text matching.
- [main.py](main.py): Service orchestrator, dual-client connection, 30-second watchdog heartbeat, and signal handlers.
- [tests/run_tests.py](tests/run_tests.py): Unittest test suite covering matching, atomic suppression/dedup, ownership-safe rollback, rate limiting, and timeouts.
- [deploy/airalert.service](deploy/airalert.service): Systemd service unit template for Linux cloud VM deployment.

---

## Verification Results

### Automated Unit Tests
Command: `python3 -m unittest tests/run_tests.py`
```
Ran 7 tests in 1.389s
OK
```
- ✅ Word boundary & tier prioritization (critical matched before standard; no false sub-string triggers).
- ✅ Atomic suppression cache (active per-key cooldown + separate cancellation-tier cooldowns + normalized message dedup, independent TTLs, ownership-safe release on failed enqueue/send) with non-rollback active-alert resets of the matching cancellation tier before queue handoff.
- ✅ Rate-limiter pacing (burst pacing with minimum intervals).
- ✅ Safe API call timeout escape (cancelled hung call promptly without freezing).
- ✅ Dynamic store hot additions/removals and atomic JSON writing.

### Compilation Check
Command: `python3 -m py_compile main.py src/*.py`
- ✅ All modules compiled without errors or syntax warnings.
