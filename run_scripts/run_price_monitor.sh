#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec uv run --directory "$REPO_ROOT" --env-file "$REPO_ROOT/.env" \
    env PYTHONPATH="$REPO_ROOT/src" \
    python "$REPO_ROOT/src/scripts/test_price_monitor.py" "$@"
