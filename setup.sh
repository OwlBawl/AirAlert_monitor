#!/usr/bin/env bash
# ==============================================================================
# AirAlert Monitor — Cloud VM Setup Script (Target: vm-bonus / Debian 12)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
CONFIG_DIR="$PROJECT_ROOT/config"

echo "=========================================================="
echo " Setting up AirAlert Telegram Monitor in: $PROJECT_ROOT"
echo "=========================================================="

cd "$PROJECT_ROOT"
mkdir -p "$CONFIG_DIR"

# 1. Resolve Virtual Environment: Use dedicated telethon_env if present
if [ -d "/home/andru_bonus/telethon_env" ]; then
    VENV_DIR="/home/andru_bonus/telethon_env"
elif [ -d "$HOME/telethon_env" ]; then
    VENV_DIR="$HOME/telethon_env"
elif [ -d "$PROJECT_ROOT/venv" ]; then
    VENV_DIR="$PROJECT_ROOT/venv"
else
    echo "[1/5] Dedicated telethon_env not found. Creating local venv..."
    python3 -m venv "$PROJECT_ROOT/venv"
    VENV_DIR="$PROJECT_ROOT/venv"
fi

PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

echo "[1/5] Using Python environment: $VENV_DIR"

# 2. Upgrade pip and install requirements
echo "[2/5] Verifying and installing dependencies in $VENV_DIR..."
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install -r requirements.txt

# 3. Check for config/.env file
if [ ! -f "$CONFIG_DIR/.env" ]; then
    if [ -f "$PROJECT_ROOT/.env" ]; then
        echo "[3/5] Migrating existing root .env to config/.env..."
        mv "$PROJECT_ROOT/.env" "$CONFIG_DIR/.env"
    elif [ -f "$CONFIG_DIR/.env.example" ]; then
        echo "[3/5] Initializing config/.env from config/.env.example..."
        cp "$CONFIG_DIR/.env.example" "$CONFIG_DIR/.env"
    fi
else
    echo "[3/5] Existing config/.env file found."
fi

# 4. Detect and reuse existing User session from home directory
echo "[4/5] Checking for existing Telethon user session in home directory..."
if [ -f "$HOME/parse_messages.session" ] && [ ! -f "$CONFIG_DIR/user_session.session" ]; then
    echo "Found existing $HOME/parse_messages.session -> Copying to config/user_session.session"
    cp "$HOME/parse_messages.session" "$CONFIG_DIR/user_session.session"
elif [ -f "$PROJECT_ROOT/user_session.session" ] && [ ! -f "$CONFIG_DIR/user_session.session" ]; then
    echo "Migrating legacy $PROJECT_ROOT/user_session.session -> config/user_session.session"
    mv "$PROJECT_ROOT/user_session.session" "$CONFIG_DIR/user_session.session"
fi

# 5. Run tests to confirm integrity
echo "[5/5] Running self-test verification..."
"$PYTHON_BIN" -m unittest tests/run_tests.py

echo ""
echo "=========================================================="
echo " Setup complete! Next steps on vm-bonus:"
echo " 1. Verify your configuration in config/.env"
echo " 2. Run initial phone authentication if needed:"
echo "      $PYTHON_BIN main.py"
echo " 3. Enable background systemd service:"
echo "      sudo cp deploy/airalert.service /etc/systemd/system/"
echo "      sudo systemctl daemon-reload"
echo "      sudo systemctl enable --now airalert.service"
echo " 4. View logs:"
echo "      journalctl -u airalert.service -f"
echo "=========================================================="
