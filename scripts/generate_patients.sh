#!/usr/bin/env bash
# Generate synthetic patients with Synthea (runs in Docker; host Java is too old).
set -euo pipefail
N="${1:-50}"
SEED="${2:-42}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JAR="$ROOT/data/synthea-with-dependencies.jar"
if [ ! -f "$JAR" ]; then
  curl -fL -o "$JAR" https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
fi
rm -rf "$ROOT/data/synthea"
docker run --rm -v "$ROOT/data:/data" -w /data eclipse-temurin:17-jre \
  java -jar synthea-with-dependencies.jar -p "$N" -s "$SEED" \
  --exporter.baseDirectory=/data/synthea \
  --exporter.fhir.transaction_bundle=true \
  --exporter.hospital.fhir.export=true \
  --exporter.practitioner.fhir.export=true \
  --exporter.years_of_history=5 \
  Massachusetts
echo "Bundles in $ROOT/data/synthea/fhir"
