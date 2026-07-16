#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=src .venv/bin/python src/scripts/run_snapshot.py "$@"
