#!/usr/bin/env bash
# Load Synthea bundles into HAPI. Org/practitioner bundles must go first.
set -euo pipefail
BASE="${FHIR_BASE:-http://localhost:8080/fhir}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/data/synthea/fhir"
until curl -sf "$BASE/metadata" >/dev/null; do echo "waiting for HAPI..."; sleep 5; done
post() {
  code=$(curl -s -m 300 -o /dev/null -w '%{http_code}' -X POST "$BASE" \
    -H 'Content-Type: application/fhir+json' --data-binary @"$1")
  echo "$code $(basename "$1")"
}
for f in "$DIR"/hospitalInformation*.json "$DIR"/practitionerInformation*.json; do post "$f"; done
for f in "$DIR"/*.json; do
  case "$(basename "$f")" in hospitalInformation*|practitionerInformation*) ;; *) post "$f";; esac
done
echo "Patients: $(curl -s "$BASE/Patient?_summary=count" | python3 -c 'import sys,json;print(json.load(sys.stdin)["total"])')"
