#!/usr/bin/env bash
# Start HAPI and the review screen. Runs on every codespace start.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d
until curl -sf http://localhost:8080/fhir/metadata >/dev/null; do sleep 5; done
pkill -f "uvicorn app.server" || true
nohup .venv/bin/uvicorn app.server:app --host 0.0.0.0 --port 8000 > data/server.log 2>&1 &
echo "Review screen on port 8000 (log: data/server.log)."
