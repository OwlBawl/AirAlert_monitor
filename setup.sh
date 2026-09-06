#!/usr/bin/env bash
# ==============================================================================
# AirAlert Monitor — Universal Linux / Debian Environment Setup Script
# ==============================================================================

set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]]; then
    DRY_RUN=true
    echo "=========================================================="
    echo " Running in DRY-RUN mode (no filesystem modifications)    "
    echo "=========================================================="
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
CONFIG_DIR="$PROJECT_ROOT/config"

# Resolve real invoking user & home directory (handles running with or without sudo)
ACTUAL_USER="${SUDO_USER:-$(id -un)}"
if command -v getent >/dev/null 2>&1; then
    ACTUAL_HOME="$(getent passwd "$ACTUAL_USER" | cut -d: -f6)"
else
    ACTUAL_HOME="$(eval echo "~$ACTUAL_USER")"
fi
[ -z "$ACTUAL_HOME" ] && ACTUAL_HOME="$HOME"

echo "=========================================================="
echo " Setting up AirAlert Telegram Monitor in: $PROJECT_ROOT"
echo " Target user: $ACTUAL_USER ($ACTUAL_HOME)"
echo "=========================================================="

if [ "$DRY_RUN" = false ]; then
    cd "$PROJECT_ROOT"
    mkdir -p "$CONFIG_DIR"
fi

# 1. Resolve Virtual Environment: Use dedicated telethon_env or local venv
if [ -d "$ACTUAL_HOME/telethon_env" ]; then
    VENV_DIR="$ACTUAL_HOME/telethon_env"
elif [ -d "$HOME/telethon_env" ]; then
    VENV_DIR="$HOME/telethon_env"
elif [ -d "$PROJECT_ROOT/venv" ]; then
    VENV_DIR="$PROJECT_ROOT/venv"
else
    if [ "$DRY_RUN" = true ]; then
        echo "[1/6] [DRY-RUN] Virtual environment not found. Would create: $PROJECT_ROOT/venv"
        VENV_DIR="$PROJECT_ROOT/venv"
    else
        echo "[1/6] Virtual environment not found. Creating local venv..."
        python3 -m venv "$PROJECT_ROOT/venv"
        VENV_DIR="$PROJECT_ROOT/venv"
    fi
fi

PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

echo "[1/6] Using Python environment: $VENV_DIR"

# 2. Upgrade pip and install requirements
if [ "$DRY_RUN" = true ]; then
    echo "[2/6] [DRY-RUN] Skipping dependency installation (pip install -r requirements.txt)"
else
    echo "[2/6] Verifying and installing dependencies in $VENV_DIR..."
    "$PIP_BIN" install --upgrade pip
    "$PIP_BIN" install -r requirements.txt
fi

# 3. Check for config/.env file
if [ ! -f "$CONFIG_DIR/.env" ]; then
    if [ -f "$CONFIG_DIR/.env.example" ]; then
        if [ "$DRY_RUN" = true ]; then
            echo "[3/6] [DRY-RUN] Would initialize config/.env from config/.env.example"
        else
            echo "[3/6] Initializing config/.env from config/.env.example..."
            cp "$CONFIG_DIR/.env.example" "$CONFIG_DIR/.env"
        fi
    fi
else
    echo "[3/6] Existing config/.env file found."
fi

# 4. Check for user session in config/ directory or user home
echo "[4/6] Checking for Telethon user session in $CONFIG_DIR..."
if [ -f "$CONFIG_DIR/user_session.session" ]; then
    echo "Existing user_session.session found."
elif [ -f "$ACTUAL_HOME/parse_messages.session" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] Would copy $ACTUAL_HOME/parse_messages.session -> $CONFIG_DIR/user_session.session"
    else
        echo "Found $ACTUAL_HOME/parse_messages.session -> Copying to $CONFIG_DIR/user_session.session"
        cp "$ACTUAL_HOME/parse_messages.session" "$CONFIG_DIR/user_session.session"
    fi
elif [ -f "$HOME/parse_messages.session" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] Would copy $HOME/parse_messages.session -> $CONFIG_DIR/user_session.session"
    else
        echo "Found $HOME/parse_messages.session -> Copying to $CONFIG_DIR/user_session.session"
        cp "$HOME/parse_messages.session" "$CONFIG_DIR/user_session.session"
    fi
fi

# 5. Run tests to confirm integrity
if [ -x "$PYTHON_BIN" ]; then
    echo "[5/6] Running self-test verification..."
    "$PYTHON_BIN" -m unittest tests/run_tests.py
else
    echo "[5/6] [DRY-RUN] Skipping unit tests (Python binary not present at $PYTHON_BIN)"
fi

# 6. Generate systemd service unit tailored for the current host environment
echo "[6/6] Generating deploy/airalert.service for $ACTUAL_USER..."
if [ -f "$PROJECT_ROOT/deploy/airalert.service.template" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "--- [DRY-RUN] Preview of deploy/airalert.service ---"
        sed \
            -e "s|{{USER}}|$ACTUAL_USER|g" \
            -e "s|{{WORKDIR}}|$PROJECT_ROOT|g" \
            -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
            "$PROJECT_ROOT/deploy/airalert.service.template"
        echo "--------------------------------------------------"
    else
        sed \
            -e "s|{{USER}}|$ACTUAL_USER|g" \
            -e "s|{{WORKDIR}}|$PROJECT_ROOT|g" \
            -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
            "$PROJECT_ROOT/deploy/airalert.service.template" > "$PROJECT_ROOT/deploy/airalert.service"
        echo "deploy/airalert.service successfully generated."
    fi
fi

echo ""
echo "=========================================================="
if [ "$DRY_RUN" = true ]; then
    echo " DRY-RUN complete. Run 'bash setup.sh' to apply."
else
    echo " Setup complete! Next steps:"
    echo " 1. Verify your configuration in config/.env"
    echo " 2. Run initial phone authentication if needed:"
    echo "      $PYTHON_BIN main.py"
    echo " 3. Enable background systemd service:"
    echo "      sudo cp deploy/airalert.service /etc/systemd/system/"
    echo "      sudo systemctl daemon-reload"
    echo "      sudo systemctl enable --now airalert.service"
    echo " 4. View logs:"
    echo "      journalctl -u airalert.service -f"
fi
echo "=========================================================="
