#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

# Load .env for local variable checks below
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# Support both TELEGRAM_TOKEN and TELEGRAM_BOT_TOKEN
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_TOKEN:-}" ]; then
    export TELEGRAM_BOT_TOKEN="$TELEGRAM_TOKEN"
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN) is not set. Add it to .env or export it." >&2
    exit 1
fi

if [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "ERROR: TELEGRAM_CHAT_ID is not set. Add it to .env or export it." >&2
    exit 1
fi

echo "Starting Telegram bot (chat_id=$TELEGRAM_CHAT_ID) ..."
exec uv run --directory "$REPO_ROOT" --env-file "$ENV_FILE" \
    env PYTHONPATH="$REPO_ROOT/src" \
    python "$REPO_ROOT/src/interfaces/telegram/bot.py"
