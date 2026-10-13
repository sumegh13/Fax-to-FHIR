#!/usr/bin/env bash
# Start HAPI and the review screen. Runs on every codespace start.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
exec > >(tee -a data/start.log) 2>&1
echo "== start $(date -u +%FT%TZ)"

# docker-in-docker starts its daemon asynchronously; wait for it.
for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker info >/dev/null 2>&1 || { echo "Docker daemon did not start"; exit 1; }
docker compose up -d
until curl -sf http://localhost:8080/fhir/metadata >/dev/null; do sleep 5; done
# Docker runs the review screen: background processes started here die when this hook exits.
APP_UID=$(id -u) APP_GID=$(id -g) docker compose --profile app up -d review
echo "Review screen on port 8000 (logs: docker compose logs review)."
