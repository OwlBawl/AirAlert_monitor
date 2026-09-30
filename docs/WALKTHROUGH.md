# AirAlert Monitor — Verification & Walkthrough

The AirAlert dual-client Telegram keyword monitor is built and configured. The current `test`-branch changes below require server-side validation before promotion.

## Changes Overview

- [src/config.py](src/config.py): Environment configuration loader with safety constants (1 msg/sec interval, 10s API timeout, 30s heartbeat).
- [src/safety.py](src/safety.py): 
  - `AlertSuppressionCache`: Consumed logical-key tracking per Telegram message, atomic mixed-tier active cooldown filtering, independent shared cancellation-tier buckets, and normalized cross-channel message dedup; failed enqueue/send rolls back newly owned state while active cancellation-gate resets remain final.
  - `AlertRateLimiter`: Burst-aware alert pacing mechanism.
  - `safe_api_call`: Strict `asyncio.wait_for(..., timeout=10.0)` wrapper with `FloodWaitError` backoff.
  - `ServiceMetrics`: Telemetry for uptime, scanned messages, keyword-cooldown filtering, message-dedup filtering, and errors.
- [src/storage.py](src/storage.py): Dynamic JSON storage and logical-key matching; all matches are collected and negative keys are scoped to their own critical/standard/cancellation group.
- [src/dispatcher.py](src/dispatcher.py): Queue worker handling native message forwards and tier alert banners (sound enabled for Critical).
- [src/bot_manager.py](src/bot_manager.py): Interactive bot commands (`/add_key`, `/del_key`, `/add_critical`, `/del_critical`, `/list_keys`, `/add_channel`, `/del_channel`, `/list_channels`, `/status`, `/id`).
- [src/parser.py](src/parser.py): Explicit new/edit listeners with event-kind diagnostics, consumed-key filtering, mixed-tier cooldown selection, dedup, and queue handoff.
- [main.py](main.py): Service orchestrator, dual-client connection, 30-second watchdog heartbeat, and signal handlers.
- [tests/run_tests.py](tests/run_tests.py): Unittest test suite covering matching, atomic suppression/dedup, ownership-safe rollback, rate limiting, and timeouts.
- [deploy/airalert.service](deploy/airalert.service): Systemd service unit template for Linux cloud VM deployment.

---

## Verification Status

The standard unittest file currently contains **34 test methods**, including new coverage for complete logical-key collection, group-local negatives, mixed-tier cooldown fallback, consumed-key behavior, and ownership-safe rollback.

Server validation is still pending for this branch. Run:

```bash
python3 -m unittest tests/run_tests.py
python3 -m py_compile main.py src/*.py
```

Do not treat the current changes as production-validated until both commands complete successfully in the deployment environment.
