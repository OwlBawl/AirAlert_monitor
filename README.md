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
  • Native Forward of Message
  • 🚨 CRITICAL ALERT Banner (Loud Sound ON)
  • ⚠️ KEYWORD ALERT Banner (Silent / Standard)
  • Interactive Bot Management Commands (/add_key, /status, etc.)
```

---

## Safety & Anti-Hang Measures Checklist

| Risk | Protective Mechanism | Location |
| :--- | :--- | :--- |
| **API Hangs & Network Stalls** | Strict `asyncio.wait_for(..., timeout=10.0)` escape mechanism wraps every Telegram API call. | `safety.py:safe_api_call` |
| **Infinite Forwarding Loops** | Hard-coded blacklist rejecting any messages originating from or sent to `TARGET_CHAT_ID`. | `parser.py` |
| **Duplicate Alerts & Edits** | LRU/TTL Cache `(chat_id, message_id)` prevents repeated alerts for identical posts. | `safety.py:DeduplicationCache` |
| **Telegram Flood & Spam** | Async queue worker enforcing a maximum rate of 1 alert per second. | `safety.py:AlertRateLimiter` |
| **Telegram FloodWait Ban** | Dynamic `FloodWaitError` catch with sleep backoff to protect account from suspension. | `safety.py:safe_api_call` |
| **Silent Disconnects on VM** | Background watchdog task running every 30 seconds to ping sessions and auto-reconnect. | `main.py:_heartbeat_loop` |
| **Process Management & Crashes** | Clean signal handling (`SIGINT`, `SIGTERM`) + `systemd` auto-restart service unit. | `systemd/airalert.service` |
| **Data Corruption on Powerloss**| Atomic file persistence via temporary file rename (`atomic_write_json`). | `storage.py` |

---

## Quick Setup

### 1. Environment & Dependencies

```bash
cd AirAlert_monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Credentials (`.env`)

Edit `.env` (already configured with your provided API credentials):
```env
API_ID=22079200
API_HASH=0726a4499610a5fee1c1e6c75cd29a66
BOT_TOKEN=5648446763:AAFlWOqmUiSXBZCrRoWkyolzpQe9Nst-ylM
TARGET_CHAT_ID=-100xxxxxxxxxx
```

> **How to find your `TARGET_CHAT_ID`:**
> 1. Add your bot `@...` to your destination channel or group.
> 2. Send `/id` in that chat.
> 3. Copy the chat ID returned by the bot and paste it into `.env`.

### 3. First-Time Run (Interactive Authentication)

```bash
python3 main.py
```
- Telethon will prompt in the terminal for your phone number and SMS login code for the **User Account**.
- Once entered, Telethon saves the session file (`user_session.session`). Subsequent launches on the VM run 100% autonomously without prompts.

---

## Bot Commands (In Target Chat)

Any member inside the destination alert chat can manage the vocabulary dynamically:

| Command | Description |
| :--- | :--- |
| `/help` | Show command reference |
| `/id` | Show current chat ID |
| `/add_key [word]` | Add keyword to standard tier |
| `/del_key [word]` | Remove keyword |
| `/add_critical [word]` | Add **CRITICAL** keyword (triggers loud sound notification 🚨) |
| `/del_critical [word]` | Remove critical keyword |
| `/list_keys` | View all active critical and standard keywords |
| `/add_channel [@user/ID]` | Add channel or chat to monitor |
| `/del_channel [@user/ID]` | Remove channel from monitoring |
| `/list_channels` | List all channels currently monitored |
| `/status` | View uptime, messages scanned, alerts sent, queue size, and error metrics |

## Project Structure

```
AirAlert_monitor/
├── src/                    # Application source code
│   ├── config.py           # Environment & settings loader
│   ├── safety.py           # Anti-hang, rate-limiter & deduplication
│   ├── storage.py          # Dynamic keywords/channels store & regex
│   ├── dispatcher.py       # Queue consumer & forwarder
│   ├── parser.py           # UserClient channel intake listener
│   └── bot_manager.py      # Bot commands router (/add_key, etc.)
├── data/                   # Dynamic JSON data files
│   ├── keywords.json       # Tiered keywords (standard & critical)
│   └── channels.json       # Monitored channel IDs / usernames
├── deploy/                 # Deployment scripts & systemd units
│   ├── setup_vm.sh         # Automated 1-click VM setup script
│   └── airalert.service    # Systemd service definition
├── docs/                   # Internal architecture & design documents
│   ├── PROJECT_STRUCTURE.md
│   ├── IMPLEMENTATION_PLAN.md
│   └── WALKTHROUGH.md
├── tests/                  # Unit and integration test suite
│   ├── run_tests.py
│   └── test_safety_and_matching.py
├── main.py                 # Clean root service entrypoint
├── requirements.txt        # Dependencies
├── .env.example            # Environment template
├── README.md               # User guide
└── AGENTS.md               # AI & agent context specification
```

---

## Cloud VM 1-Click Deployment (Google Cloud / Oracle Cloud)

1. Clone repository to your VM:
```bash
git clone https://github.com/OwlBawl/AirAlert_monitor.git
cd AirAlert_monitor
```
2. Run the automated installer:
```bash
bash deploy/setup_vm.sh
```
3. Complete first-time phone authentication:
```bash
source venv/bin/activate
python3 main.py
# Enter phone number and SMS Telegram login code
# Verify bot responds to /status in your chat, then press Ctrl+C
```
4. Enable background service:
```bash
sudo cp deploy/airalert.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airalert.service
```
5. View live logs:
```bash
journalctl -u airalert.service -f
# or
tail -f airalert.log
```

---

## Running Tests

Run the test suite to verify regex matching, rate-limiting, deduplication, and timeout escapes:
```bash
python3 -m unittest tests/run_tests.py
```

---

## Technical Documentation & Architecture Reference

For detailed module-by-module breakdown, function signatures, data flow diagrams, and invariant guarantees, see:
- [docs/PROJECT_STRUCTURE.md](file:///Users/Andru/Downloads/AirAlert_monitor/docs/PROJECT_STRUCTURE.md)
- [docs/WALKTHROUGH.md](file:///Users/Andru/Downloads/AirAlert_monitor/docs/WALKTHROUGH.md)
- [AGENTS.md](file:///Users/Andru/Downloads/AirAlert_monitor/AGENTS.md)


