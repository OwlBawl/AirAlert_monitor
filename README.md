# AirAlert Monitor — Telegram Channel Keyword Parser & Alert Bot

Production-grade, dual-client Telegram monitoring system built with Telethon. Listens to public and private channels using a personal User account, matches messages against standard and critical keyword tiers, and dispatches native message forwards with high-visibility alert banners to a designated Telegram chat using a Bot.

---

## Architecture Overview

```
[ Telegram Channels / Groups ]
              │
              ▼ (Telethon User Client: api_id, api_hash)
       [ parser.py ] ─── Loop Guard & Deduplication Cache
              │
              ▼ (Word-boundary regex: standard & critical)
       [ storage.py ] ─── Dynamic JSON Store (keywords.json, channels.json)
              │
              ▼ (Async Queue with 1 msg/sec Rate Limiter)
      [ dispatcher.py ]
              │
              ▼ (Telethon Bot Client: bot_token)
  [ Target Chat / Channel ]
  • Structured HTML Alert Card:
    - Quoted message text in <blockquote> (with ‼️🚨‼️ prefix for Critical alerts)
    - Source channel name & matched keyword(s)
    - Direct link to original post
  • Interactive Bot Management Commands (/add_key, /status, etc.)
```

> 💡 **For AI Agents & Developers:** Full technical architecture, safety invariants, and internal call graphs are maintained in [AGENTS.md](AGENTS.md) and [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md).

---

## Core Operational Guarantees

- **Anti-Hang Protection:** Telegram API operations are guarded by strict 10-second timeout wrappers (`asyncio.wait_for`).
- **Loop Prevention:** Hard-coded loop guard drops any message originating from or directed to `TARGET_CHAT_ID`.
- **Deduplication:** In-memory TTL/LRU cache suppresses repeat notifications for edited or reposted messages.
- **Flood Control:** Outbound alert queue enforces a 1 msg/sec rate limit with dynamic `FloodWaitError` backoff.
- **Connection Watchdog:** 30-second heartbeat pings both clients and triggers automatic reconnections if network drops.

---

## Quick Setup

### Option 1: Automated Setup (Recommended)

The automated setup script handles the entire lifecycle: resolves or creates the virtual environment, installs dependencies, guides you through setting up credentials via an interactive wizard, offers immediate User Account phone login, executes verification tests, and generates the tailored `deploy/airalert.service` unit:

```bash
# 1. Clone repository
git clone https://github.com/OwlBawl/AirAlert_monitor.git
cd AirAlert_monitor

# 2. Preview setup and environment detection (optional dry-run)
bash setup.sh --dry-run

# 3. Run interactive setup wizard
bash setup.sh
```

<details>
<summary><b>Option 2: Alternative Manual Setup</b></summary>

```bash
# 1. Clone repository
git clone https://github.com/OwlBawl/AirAlert_monitor.git
cd AirAlert_monitor

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Initialize configuration template
cp config/.env.example config/.env

# 5. Manually edit config/.env with your credentials
nano config/.env

# 6. Generate systemd service unit from template
sed \
  -e "s|{{USER}}|$USER|g" \
  -e "s|{{WORKDIR}}|$PWD|g" \
  -e "s|{{PYTHON_BIN}}|$PWD/venv/bin/python|g" \
  deploy/airalert.service.template > deploy/airalert.service
```
</details>

---

### Configuration (`config/.env`)

When running `bash setup.sh`, an **interactive configuration wizard** checks `config/.env` for missing keys and prompts you to enter them with inline guidance and links:

```env
API_ID=12345678
API_HASH=your_api_hash_here
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TARGET_CHAT_ID=-100xxxxxxxxxx
```

> **Credential Reference & Where to Get Them:**
> - `API_ID` & `API_HASH`: Log in to [my.telegram.org](https://my.telegram.org) and navigate to *API Development Tools*.
> - `BOT_TOKEN`: Message [@BotFather](https://t.me/BotFather) on Telegram and create a bot via `/newbot`.
> - `TARGET_CHAT_ID`: Add your bot to the destination group or channel, send `/id` in that chat, and copy the returned ID.

*(Existing credentials in `config/.env` are preserved automatically. You can also edit `config/.env` directly at any time to update tokens).*

---

### Telegram Account Authentication (User Session)

To monitor channels, Telethon requires one-time phone authentication with your Telegram user account:

- **Via `setup.sh`:** If `config/user_session.session` is not detected, the script prompts `Run Telegram phone authentication now? [Y/n]:`. Answering `Y` launches the interactive login prompt on the spot.
- **Manual / Standalone:** You can also trigger authentication manually at any time:
  ```bash
  python3 main.py
  ```
  Enter your phone number (international format) and the SMS/Telegram verification code. Once verified, credentials persist in `config/user_session.session` and subsequent launches run 100% autonomously without interactive prompts.

---

## Bot Commands (In Target Chat)

Any member inside the destination alert chat can manage the monitoring rules in real-time:

| Command | Description |
| :--- | :--- |
| `/help` | Show command reference |
| `/id` | Display current chat ID |
| `/add_key [word]` | Add keyword to standard tier (silent alert) |
| `/del_key [word]` | Remove keyword from standard tier |
| `/add_critical [word]` | Add keyword to **CRITICAL** tier (loud notification 🚨) |
| `/del_critical [word]` | Remove keyword from critical tier |
| `/list_keys` | View all active critical and standard keywords |
| `/add_channel [@user/ID]` | Add channel or group to monitor |
| `/del_channel [@user/ID]` | Remove channel or group from monitoring |
| `/list_channels` | List all channels currently monitored |
| `/status` | View uptime, messages scanned, alerts dispatched, queue size, and error counters |

---

## Production Deployment (Linux / systemd)

1. **Prepare service definition:**
   - If you ran `bash setup.sh`, `deploy/airalert.service` was automatically generated for your current user and paths.
   - *(Manual setup)* Generate it from `deploy/airalert.service.template`:
     ```bash
     sed \
       -e "s|{{USER}}|$USER|g" \
       -e "s|{{WORKDIR}}|$PWD|g" \
       -e "s|{{PYTHON_BIN}}|$PWD/venv/bin/python|g" \
       deploy/airalert.service.template > deploy/airalert.service
     ```

2. **Register and start service:**
   ```bash
   sudo cp deploy/airalert.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now airalert.service
   ```

3. **Check status & logs:**
   ```bash
   sudo systemctl status airalert.service
   journalctl -u airalert.service -f
   ```

---

## Updating Existing Installation

To pull updates and restart the service on your server:

```bash
cd ~/AirAlert_monitor

# 1. Pull latest code
git pull origin main

# 2. Preview environment and changes (optional dry-run)
bash setup.sh --dry-run

# 3. Run setup script (updates dependencies, executes tests, regenerates service)
bash setup.sh

# 4. Restart background service
sudo systemctl restart airalert.service
```

> **Note:** Active runtime data (`config/.env`, `config/*.session`, `config/keywords.json`, `config/channels.json`) are ignored by git and will not be overwritten.

---

## Running Tests

Run the test suite to verify regex matching, rate-limiting, deduplication, and timeout escapes:

```bash
python3 -m unittest tests/run_tests.py
```

---

## Project Structure & Developer Reference

```
AirAlert_monitor/
├── src/                    # Application source code
│   ├── config.py           # Environment & settings loader
│   ├── safety.py           # Anti-hang, rate-limiter & deduplication
│   ├── storage.py          # Dynamic keywords/channels store & regex
│   ├── dispatcher.py       # Queue consumer & alert forwarder
│   ├── parser.py           # UserClient channel intake listener
│   └── bot_manager.py      # Bot commands router (/add_key, etc.)
├── config/                 # Secrets, sessions & runtime data (gitignored)
│   ├── .env.example        # Environment template (tracked)
│   ├── keywords.json.example
│   └── channels.json.example
├── deploy/                 # Systemd service template
│   └── airalert.service.template
├── docs/                   # Architectural guides & design docs
│   ├── PROJECT_STRUCTURE.md
│   ├── IMPLEMENTATION_PLAN.md
│   └── WALKTHROUGH.md
├── tests/                  # Unit and integration test suite
├── setup.sh                # 1-click environment setup script
├── main.py                 # Service orchestrator & entrypoint
├── requirements.txt        # Python package dependencies
├── README.md               # User guide & operations
└── AGENTS.md               # AI agent instructions & system invariants
```

### Additional Documentation
- [AGENTS.md](AGENTS.md) — Specification and architecture invariants for AI agents.
- [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) — Detailed module breakdown, function signatures, and call flows.
- [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) — Verification runbook and feature walkthrough.
