#!/usr/bin/env bash
# Start the backend API on http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")/../backend"

if [ ! -d .venv ]; then
  echo "No virtualenv found; running 'uv sync' first..."
  uv sync
fi

exec uv run --no-sync uvicorn app.main:app --host "${HW2_HOST:-127.0.0.1}" --port "${HW2_PORT:-8000}" "$@"
