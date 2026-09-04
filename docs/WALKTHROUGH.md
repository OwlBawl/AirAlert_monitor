# AirAlert Monitor — Verification & Walkthrough

The AirAlert dual-client Telegram keyword monitor is built, configured, and tested.

## Changes Overview

- [config.py](file:///Users/Andru/Downloads/AirAlert_monitor/config.py): Environment configuration loader with safety constants (1 msg/sec interval, 10s API timeout, 30s heartbeat).
- [safety.py](file:///Users/Andru/Downloads/AirAlert_monitor/safety.py): 
  - `DeduplicationCache`: LRU/TTL cache `(chat_id, message_id)`.
  - `AlertRateLimiter`: Strict 1 alert/sec pacing mechanism.
  - `safe_api_call`: Strict `asyncio.wait_for(..., timeout=10.0)` wrapper with `FloodWaitError` backoff.
  - `ServiceMetrics`: Telemetry for uptime, scanned messages, duplicates caught, and errors.
- [storage.py](file:///Users/Andru/Downloads/AirAlert_monitor/storage.py): Dynamic JSON storage for [keywords.json](file:///Users/Andru/Downloads/AirAlert_monitor/keywords.json) and [channels.json](file:///Users/Andru/Downloads/AirAlert_monitor/channels.json) with unicode word-boundary regex (`(?<!\w)...(?!\w)`).
- [dispatcher.py](file:///Users/Andru/Downloads/AirAlert_monitor/dispatcher.py): Queue worker handling native message forwards and tier alert banners (sound enabled for Critical).
- [bot_manager.py](file:///Users/Andru/Downloads/AirAlert_monitor/bot_manager.py): Interactive bot commands (`/add_key`, `/del_key`, `/add_critical`, `/del_critical`, `/list_keys`, `/add_channel`, `/del_channel`, `/list_channels`, `/status`, `/id`).
- [parser.py](file:///Users/Andru/Downloads/AirAlert_monitor/parser.py): User account listener with loop guard, channel filtering, and text matching.
- [main.py](file:///Users/Andru/Downloads/AirAlert_monitor/main.py): Service orchestrator, dual-client connection, 30-second watchdog heartbeat, and signal handlers.
- [tests/run_tests.py](file:///Users/Andru/Downloads/AirAlert_monitor/tests/run_tests.py): Unittest test suite covering matching, dedup, rate limiting, and timeouts.
- [systemd/airalert.service](file:///Users/Andru/Downloads/AirAlert_monitor/systemd/airalert.service): Systemd service unit for Oracle / Google Cloud VM deployment.

---

## Verification Results

### Automated Unit Tests
Command: `python3 -m unittest tests/run_tests.py`
```
Ran 5 tests in 1.131s
OK
```
- ✅ Word boundary & tier prioritization (critical matched before standard; no false sub-string triggers).
- ✅ Deduplication cache (same post in same chat rejected within TTL).
- ✅ Rate-limiter pacing (enforces >= 1.0s between dispatches).
- ✅ Safe API call timeout escape (cancelled hung call promptly without freezing).
- ✅ Dynamic store hot additions/removals and atomic JSON writing.

### Compilation Check
Command: `python3 -m py_compile config.py safety.py storage.py dispatcher.py bot_manager.py parser.py main.py`
- ✅ All modules compiled without errors or syntax warnings.
