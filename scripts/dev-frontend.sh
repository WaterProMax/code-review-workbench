#!/usr/bin/env bash
# Start the frontend workbench on http://127.0.0.1:5173 (proxies /api to :8000)
set -euo pipefail
cd "$(dirname "$0")/../frontend"

if [ ! -d node_modules ]; then
  echo "No node_modules found; running 'npm install' first..."
  npm install
fi

exec npm run dev -- --host 127.0.0.1 --port 5173 "$@"
