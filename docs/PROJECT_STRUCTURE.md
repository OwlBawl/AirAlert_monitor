# AirAlert Monitor — Architecture, Module & Function Reference

A comprehensive developer and AI context guide detailing the architecture, module relationships, classes, functions, and data flows.

---

## 1. System Topology & Component Map

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Telegram Cloud MTProto                          │
└───────────────┬────────────────────────────────────────▲───────────────┘
                │ Raw Messages                           │ Forwards / Commands
                ▼                                        │
     ┌──────────────────────┐                ┌───────────┴──────────┐
     │  Telethon UserClient │                │  Telethon BotClient  │
     │   (user_session)     │                │    (bot_session)     │
     └──────────┬───────────┘                └──────────▲──────────┘
                │                                        │
                ▼                                        │
       [ parser.py ]                                [ dispatcher.py ]
       • loop guard                                 • 1 msg/sec pacing
       • dedup check                                • 3-line alert card
       • channel filter                             • critical prefix
                │                                        ▲
                ▼                                        │
       [ storage.py ]                                    │
       • word boundary matching                          │
       • critical tier priority                          │
                │                                        │
                └──────────► [ AlertJob Queue ] ─────────┘
                               (asyncio.Queue)
```

---

## 2. File & Directory Structure

```
AirAlert_monitor/
├── src/                    # Application source code
│   ├── __init__.py
│   ├── config.py           # Environment & AppConfig schema
│   ├── safety.py           # DeduplicationCache, AlertRateLimiter, safe_api_call
│   ├── storage.py          # DynamicStore, KeywordMatch, atomic JSON writes
│   ├── dispatcher.py       # AlertDispatcher, AlertJob queue consumer
│   ├── parser.py           # User client listener, filtering & intake
│   └── bot_manager.py      # Bot client command router (/add_key, /status, etc.)
├── config/                 # Unified configuration, secrets & sessions
│   ├── .env.example        # Environment template (git tracked)
│   ├── keywords.json.example # Vocabulary template (git tracked)
│   ├── channels.json.example # Channels template (git tracked)
│   ├── .gitkeep            # Directory placeholder (git tracked)
│   ├── .env                # Runtime environment & secrets (gitignored)
│   ├── keywords.json       # Live keywords storage (gitignored)
│   ├── channels.json       # Live channels storage (gitignored)
│   └── *.session           # Telethon SQLite session files (gitignored)
├── deploy/                 # Systemd units & service definitions
│   └── airalert.service.template # Systemd service unit template
├── docs/                   # Internal architecture & design documents
│   ├── PROJECT_STRUCTURE.md
│   ├── IMPLEMENTATION_PLAN.md
│   └── WALKTHROUGH.md
├── tests/                  # Unit and integration test suite
│   ├── run_tests.py
│   └── test_safety_and_matching.py
├── main.py                 # Entrypoint: AirAlertService orchestrator & watchdog
├── requirements.txt        # Python dependencies
├── .env.example            # Configuration template
├── README.md               # User guide, commands, setup, and deployment
└── AGENTS.md               # AI & agent context specification
```

---

## 3. Module & Function Reference

### `src/config.py`
Provides typed, validated configuration loading with fallbacks.

- **`_get_env_int(key: str, default: int) -> int`**:
  Safe parser for integer environment variables.
- **`_get_env_float(key: str, default: float) -> float`**:
  Safe parser for floating-point environment variables.
- **`class AppConfig`** (Frozen dataclass):
  - Attributes: `api_id`, `api_hash`, `bot_token`, `target_chat_id`, `user_session_name`, `bot_session_name`, `keywords_file`, `channels_file`, `alert_interval_seconds`, `api_timeout_seconds`, `heartbeat_interval_seconds`, `dedup_ttl_seconds`, `dedup_max_size` (default: 500), `queue_max_size` (default: 100), `log_max_bytes` (default: 10MB), `log_backup_count` (default: 5).
  - `load() -> AppConfig`: Factory method constructing configuration from `.env` and environment variables.
- **`config: AppConfig`**:
  Global singleton configuration instance.

---

### `src/safety.py`
Protects the service against hangs, loops, duplicate notifications, and Telegram API flood limits.

- **`class DeduplicationCache`**:
  - `__init__(max_size: int = 500, ttl_seconds: float = 3600.0)`: Initializes in-memory `OrderedDict` with an `asyncio.Lock`.
  - `check_and_add(chat_id: int, message_id: int) -> bool`: Checks if `(chat_id, message_id)` was processed within TTL. Returns `True` if duplicate, `False` if newly registered. Evicts oldest expired items or oldest item when max size (500) is reached.
  - `clean_expired() -> int`: Active memory sweep executed on watchdog cycles to proactively evict expired message IDs.
  - `size() -> int`: Returns current cached entry count.
- **`class AlertRateLimiter`**:
  - `__init__(min_interval_seconds: float = 1.0)`: Enforces minimum elapsed time between consecutive alerts (default: 1.0 second).
  - `wait_turn() -> None`: Async sleep until at least `min_interval_seconds` has passed since the previous alert.
- **`class ServiceMetrics`**:
  - Counters: `messages_scanned`, `keywords_matched`, `alerts_forwarded`, `duplicates_filtered`, `errors_caught`, `flood_wait_events`.
  - `get_uptime_str() -> str`: Returns human-readable uptime formatted as `Xh Ym Zs`.
  - Global instance: `metrics: ServiceMetrics`.
- **`safe_api_call(coroutine_func, timeout_seconds=10.0, action_name="...") -> Optional[T]`**:
  - Wraps async call in `asyncio.wait_for` to prevent stalled network hangs.
  - Catches `asyncio.TimeoutError` and logs error without raising.
  - Catches `FloodWaitError`, sleeps for required duration, updates metrics.
  - Catches generic `Exception`, logs trace, prevents event loop crashes.

---

### `storage.py`
Manages JSON persistence, hot-reloading, and unicode word-boundary regex compilation.

- **`class KeywordMatch`** (Frozen dataclass):
  - Attributes: `tier: str` ("critical" or "standard"), `matched_words: Tuple[str, ...]`.
- **`class DynamicStore`**:
  - `__init__(keywords_file: Path, channels_file: Path)`: Initializes store with async locks and internal sets.
  - `_build_regex(words: Set[str]) -> Optional[re.Pattern]`: Compiles unicode pattern `(?<!\w)(?:word1|word2)(?!\w)` with `re.IGNORECASE | re.UNICODE`. Longer phrases sorted first to avoid partial overrides.
  - `_atomic_write_json(file_path: Path, data: Any) -> None`: Writes to `.tmp` file and replaces target atomically to guard against VM power failure corruption.
  - `load_all() -> None`: Async orchestrator loading keywords and channels from disk.
  - `add_keyword(word: str, tier: str = "standard") -> bool`: Adds keyword to specified tier, recompiles regexes, persists atomically.
  - `remove_keyword(word: str) -> bool`: Removes keyword from both tiers, recompiles regexes, persists atomically.
  - `get_keywords() -> Dict[str, List[str]]`: Returns `{ "critical": [...], "standard": [...] }`.
  - `add_channel(channel_identifier: Union[int, str]) -> bool`: Adds chat ID or `@username` to monitored list.
  - `remove_channel(channel_identifier: Union[int, str]) -> bool`: Removes channel identifier.
  - `get_channels() -> List[Union[int, str]]`: Returns sorted list of active channels.
  - `is_channel_monitored(chat_id: int, username: Optional[str] = None) -> bool`: O(1) membership check matching either numeric ID or username string.
  - `match_text(text: Optional[str]) -> Optional[KeywordMatch]`: Evaluates text. Checks Critical regex first; if matched, immediately returns `KeywordMatch(tier="critical", ...)`. Otherwise evaluates Standard regex. Returns `None` if no match.

---

### `src/dispatcher.py`
Processes the alert queue at a controlled rate and formats notifications.

- **`class AlertJob`** (Dataclass):
  - Attributes: `source_chat_id`, `source_chat_title`, `source_chat_username`, `message_id`, `message_date`, `message_text`, `match`.
- **`class AlertDispatcher`**:
  - `__init__(bot_client: TelegramClient, config: AppConfig)`: Initializes bounded queue (`maxsize=config.queue_max_size`, default 100) and rate limiter.
  - `enqueue(job: AlertJob) -> bool`: Non-blocking queue enqueue; drops safely if queue is full.
  - `start() -> None`: Launches background worker `_process_queue_loop`.
  - `stop() -> None`: Gracefully cancels worker task.
  - `_process_queue_loop() -> None`: Infinite async loop consuming jobs with `rate_limiter.wait_turn()`.
  - `format_alert(job: AlertJob) -> str`:
    Formats structured 3-line HTML alert card:
    1. Quoted text block: `<blockquote>` with original message text (prefixed with `‼️🚨‼️ ` for Critical tier).
    2. Metadata line: `📢 {channel_name}: {matched_words}`.
    3. Direct message link: `🔗 https://t.me/.../{message_id}`.
  - `_dispatch_single_alert(job: AlertJob) -> None`:
    Resolves target entity and dispatches the alert card via `safe_api_call` with sound enabled (`silent=False`).

---

### `src/parser.py`
User account listener capturing incoming and edited channel messages.

- **`setup_parser_handlers(user_client, config, store, dedup, dispatcher) -> None`**:
  Registers `@user_client.on(events.NewMessage)` and `@user_client.on(events.MessageEdited)`.
  Execution pipeline:
  1. **Loop Guard**: If `chat_id == config.target_chat_id`, skip.
  2. **Channel Filter**: If `channels.json` is configured, verifies `store.is_channel_monitored(chat_id, username)`.
  3. **Deduplication**: `dedup.check_and_add(chat_id, message_id)`. If duplicate, increment metric and drop.
  4. **Text Evaluation**: `store.match_text(raw_text)`.
  5. **Enqueue**: Creates `AlertJob` and submits to `dispatcher.enqueue()`.

---

### `src/bot_manager.py`
Interactive command controller for users inside the target chat.

- **`setup_bot_handlers(bot, config, store, dispatcher) -> None`**:
  Registers commands restricted to `event.chat_id == config.target_chat_id`:
  - `/help`, `/start`: Displays usage guide.
  - `/id`: Outputs current Telegram chat ID.
  - `/add_key [word]`: Adds standard keyword.
  - `/add_critical [word]`: Adds critical keyword (sound notification).
  - `/del_key [word]`, `/del_critical [word]`: Removes keyword.
  - `/list_keys`: Lists active keywords by tier.
  - `/add_channel [@user/ID]`: Adds channel to intake.
  - `/del_channel [@user/ID]`: Removes channel from intake.
  - `/list_channels`: Lists monitored channels.
  - `/status`: Displays uptime, queue depth, scanned counts, and error statistics.

---

### `main.py`
Lifecycle manager and watchdog runner.

- **`RotatingFileHandler`**: Logs to `airalert.log` with a 10MB file limit and 5 backup archives (50MB absolute disk ceiling).
- **`class AirAlertService`**:
  - `start() -> None`: Loads storage, starts Bot client, launches dispatcher, starts User client, binds handlers, launches heartbeat task.
  - `_heartbeat_loop() -> None`:
    - Every 30 seconds: checks connection and auto-reconnects; actively sweeps expired message IDs via `dedup.clean_expired()`.
    - Every 5 minutes: runs `gc.collect()` to free transient MTProto buffers and logs deep health telemetry.
  - `stop() -> None`: Cancels heartbeat, stops dispatcher, disconnects clients cleanly.
  - `run_until_disconnected() -> None`: Listens for OS `SIGINT` and `SIGTERM` signals for graceful exit.
- **`main() -> None`**: Entrypoint initializing `AirAlertService`.

---

## 4. Invariants & Safety Guarantees

1. **No Unbounded Waits**: No network call is permitted to execute without `safe_api_call` (guaranteed <= 10.0s max runtime).
2. **Pacing**: Telegram message dispatches are separated by at least 1.0s to prevent rate limits.
3. **No Self-Alert Loops**: Messages originating from `TARGET_CHAT_ID` are filtered at line 0 of intake in `parser.py`.
4. **Resilient Matching**: Cyrillic and English word boundaries are enforced via `(?<!\w)...(?!\w)` so sub-words never trigger false positives.
5. **Zero Data Loss on Crash**: Dynamic configurations in `storage.py` are written via temporary files and atomic POSIX replacement (`tmp_path.replace(file_path)`).
