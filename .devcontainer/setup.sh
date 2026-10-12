#!/usr/bin/env bash
# One-time setup for a new codespace: dependencies, synthetic data, loaded FHIR server.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
exec > >(tee -a data/setup.log) 2>&1
echo "== setup $(date -u +%FT%TZ)"

# docker-in-docker starts its daemon asynchronously; wait for it.
for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker info >/dev/null 2>&1 || { echo "Docker daemon did not start"; exit 1; }

python -m venv .venv
.venv/bin/pip install -q -r requirements.txt

docker compose up -d
[ -d data/synthea/fhir ] || ./scripts/generate_patients.sh 50
[ -f data/faxes/manifest.jsonl ] || .venv/bin/python -m faxgen

count=$(curl -sf --retry 60 --retry-delay 5 --retry-all-errors 'http://localhost:8080/fhir/Patient?_summary=count' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["total"])')
[ "$count" -gt 0 ] || ./scripts/load_hapi.sh
echo "Setup complete."
