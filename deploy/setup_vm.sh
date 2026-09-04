#!/usr/bin/env bash
# ==============================================================================
# AirAlert Monitor — Cloud VM Setup Script (Target: vm-bonus / Debian 12)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=========================================================="
echo " Setting up AirAlert Telegram Monitor in: $PROJECT_ROOT"
echo "=========================================================="

cd "$PROJECT_ROOT"

# 1. Resolve Virtual Environment: Use dedicated telethon_env if present
if [ -d "/home/andru_bonus/telethon_env" ]; then
    VENV_DIR="/home/andru_bonus/telethon_env"
elif [ -d "$HOME/telethon_env" ]; then
    VENV_DIR="$HOME/telethon_env"
elif [ -d "$PROJECT_ROOT/venv" ]; then
    VENV_DIR="$PROJECT_ROOT/venv"
else
    echo "[1/4] Dedicated telethon_env not found. Creating local venv..."
    python3 -m venv "$PROJECT_ROOT/venv"
    VENV_DIR="$PROJECT_ROOT/venv"
fi

PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

echo "[1/4] Using Python environment: $VENV_DIR"

# 2. Upgrade pip and install requirements
echo "[2/4] Verifying and installing dependencies in $VENV_DIR..."
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install -r requirements.txt

# 3. Check for .env file
if [ ! -f ".env" ]; then
    echo "[3/4] Creating .env from .env.example..."
    cp .env.example .env
    echo "⚠️ Please edit .env with your actual TARGET_CHAT_ID."
else
    echo "[3/4] Existing .env file found."
fi

# 4. Run tests to confirm integrity
echo "[4/4] Running self-test verification..."
"$PYTHON_BIN" -m unittest tests/run_tests.py

echo ""
echo "=========================================================="
echo " Setup complete! Next steps on vm-bonus:"
echo " 1. Verify your TARGET_CHAT_ID in .env"
echo " 2. Run initial phone authentication:"
echo "      source $VENV_DIR/bin/activate && python main.py"
echo " 3. Enable background systemd service:"
echo "      sudo cp deploy/airalert.service /etc/systemd/system/"
echo "      sudo systemctl daemon-reload"
echo "      sudo systemctl enable --now airalert.service"
echo " 4. View logs:"
echo "      journalctl -u airalert.service -f"
echo "=========================================================="
