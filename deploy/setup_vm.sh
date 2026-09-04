#!/usr/bin/env bash
# ==============================================================================
# AirAlert Monitor — Automated Cloud VM Setup Script (Ubuntu / Debian / Oracle)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=========================================================="
echo " Setting up AirAlert Telegram Monitor in: $PROJECT_ROOT"
echo "=========================================================="

cd "$PROJECT_ROOT"

# 1. Install system dependencies if apt is available
if command -v apt-get &> /dev/null; then
    echo "[1/4] Checking and installing system packages..."
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip
else
    echo "[1/4] Non-Debian system detected. Ensure Python 3.10+ and venv are installed."
fi

# 2. Create Python virtual environment
if [ ! -d "venv" ]; then
    echo "[2/4] Creating virtual environment (venv)..."
    python3 -m venv venv
else
    echo "[2/4] Virtual environment already exists."
fi

# 3. Upgrade pip and install requirements
echo "[3/4] Installing dependencies from requirements.txt..."
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# 4. Check for .env file
if [ ! -f ".env" ]; then
    echo "[4/4] Creating .env from .env.example..."
    cp .env.example .env
    echo "⚠️ Please edit .env with your actual TARGET_CHAT_ID and credentials."
else
    echo "[4/4] Existing .env file found."
fi

# 5. Run tests to confirm integrity
echo ""
echo "Running self-test verification..."
venv/bin/python3 -m unittest tests/run_tests.py

echo ""
echo "=========================================================="
echo " Setup complete! Next steps:"
echo " 1. Verify your credentials in .env"
echo " 2. Run initial phone authentication:"
echo "      source venv/bin/activate && python3 main.py"
echo " 3. Enable background systemd service:"
echo "      sudo cp deploy/airalert.service /etc/systemd/system/"
echo "      sudo systemctl daemon-reload"
echo "      sudo systemctl enable --now airalert.service"
echo "=========================================================="
