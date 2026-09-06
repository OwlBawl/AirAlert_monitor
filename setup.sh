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

# Helper function to check and prompt for environment variables
prompt_env_var() {
    local key="$1"
    local desc="$2"
    local link="$3"
    local default_hint="${4:-}"

    local current_val=""
    if [ -f "$CONFIG_DIR/.env" ] && [ -x "$PYTHON_BIN" ]; then
        current_val="$("$PYTHON_BIN" -c "
import dotenv
val = dotenv.dotenv_values('$CONFIG_DIR/.env').get('$key') or ''
print(val.strip())
" 2>/dev/null || true)"
    fi

    if [ -z "$current_val" ]; then
        if [ "$DRY_RUN" = true ]; then
            echo "  ⚠️  $key is NOT set."
        elif [ -t 0 ]; then
            echo ""
            echo "┌── [Configuration Wizard] $key ─────────────────────────"
            echo "│ Description: $desc"
            [ -n "$link" ] && echo "│ How to get : $link"
            echo "└───────────────────────────────────────────────────────────"
            read -r -p "Enter $key: " input_val
            input_val="${input_val:-$default_hint}"
            if [ -n "$input_val" ]; then
                "$PYTHON_BIN" -c "
import sys, re
key, val, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, 'r') as f:
    text = f.read()
if re.search(r'^' + re.escape(key) + r'=.*', text, flags=re.M):
    text = re.sub(r'^' + re.escape(key) + r'=.*', f'{key}={val}', text, flags=re.M)
else:
    text += f'\n{key}={val}\n'
with open(path, 'w') as f:
    f.write(text)
" "$key" "$input_val" "$CONFIG_DIR/.env"
                echo "  ✅ Saved $key to config/.env"
            else
                echo "  ⏭️  Skipped $key (remains empty)"
            fi
        else
            echo "  ⚠️  $key is missing or empty in config/.env"
        fi
    else
        echo "  ✅ $key is configured."
    fi
}

# 3. Check for config/.env file and prompt for credentials
echo "[3/6] Checking configuration in $CONFIG_DIR/.env..."
if [ ! -f "$CONFIG_DIR/.env" ]; then
    if [ -f "$CONFIG_DIR/.env.example" ]; then
        if [ "$DRY_RUN" = true ]; then
            echo "  [DRY-RUN] Would initialize config/.env from config/.env.example"
        else
            echo "  Initializing config/.env from config/.env.example..."
            cp "$CONFIG_DIR/.env.example" "$CONFIG_DIR/.env"
        fi
    fi
fi

prompt_env_var "API_ID" "Telegram API ID (numeric)" "Log in to https://my.telegram.org -> 'API development tools'"
prompt_env_var "API_HASH" "Telegram API Hash (hex string)" "Log in to https://my.telegram.org -> 'API development tools'"
prompt_env_var "BOT_TOKEN" "Telegram Bot Token" "Create bot via @BotFather (https://t.me/BotFather) using /newbot"
prompt_env_var "TARGET_CHAT_ID" "Alert destination chat/channel ID" "Add bot to group, send /id in that group to see chat ID (e.g., -100xxxxxxxxxx)"

# 4. Check for user session in config/ directory or user home
echo "[4/6] Checking for Telethon user session in $CONFIG_DIR..."
if [ -f "$CONFIG_DIR/user_session.session" ]; then
    echo "  ✅ Existing user_session.session found."
elif [ -f "$ACTUAL_HOME/parse_messages.session" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY-RUN] Would copy $ACTUAL_HOME/parse_messages.session -> $CONFIG_DIR/user_session.session"
    else
        echo "  Found $ACTUAL_HOME/parse_messages.session -> Copying to $CONFIG_DIR/user_session.session"
        cp "$ACTUAL_HOME/parse_messages.session" "$CONFIG_DIR/user_session.session"
    fi
elif [ -f "$HOME/parse_messages.session" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY-RUN] Would copy $HOME/parse_messages.session -> $CONFIG_DIR/user_session.session"
    else
        echo "  Found $HOME/parse_messages.session -> Copying to $CONFIG_DIR/user_session.session"
        cp "$HOME/parse_messages.session" "$CONFIG_DIR/user_session.session"
    fi
else
    if [ "$DRY_RUN" = true ]; then
        echo "  ⚠️  No user_session.session found. Phone authentication will be prompted on initial run."
    elif [ -t 0 ]; then
        echo ""
        echo "┌── [Telegram User Account Authentication] ─────────────────"
        echo "│ Telethon requires a one-time SMS verification code login"
        echo "│ to authenticate your User Account and generate user_session.session."
        echo "└───────────────────────────────────────────────────────────"
        read -r -p "Run Telegram phone authentication now? [Y/n]: " do_auth
        do_auth="${do_auth:-Y}"
        if [[ "$do_auth" =~ ^[Yy]$ ]]; then
            echo "Launching main.py for authentication (enter phone & SMS code when prompted)..."
            "$PYTHON_BIN" main.py || true
        fi
    else
        echo "  ⚠️  No user_session.session found. Run '$PYTHON_BIN main.py' interactively to authenticate."
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
        echo "  ✅ deploy/airalert.service successfully generated."
    fi
fi

echo ""
echo "=========================================================="
if [ "$DRY_RUN" = true ]; then
    echo " DRY-RUN complete. Run 'bash setup.sh' to apply."
else
    echo " Setup complete! Next steps:"
    echo " 1. Verify your configuration in config/.env"
    echo " 2. If you skipped initial phone authentication, run:"
    echo "      $PYTHON_BIN main.py"
    echo " 3. Enable background systemd service:"
    echo "      sudo cp deploy/airalert.service /etc/systemd/system/"
    echo "      sudo systemctl daemon-reload"
    echo "      sudo systemctl enable --now airalert.service"
    echo " 4. View logs:"
    echo "      journalctl -u airalert.service -f"
fi
echo "=========================================================="
