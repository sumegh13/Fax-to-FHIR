"""Map free-text diagnoses, drugs and lab names to standard codes.

A fax gives "Metformin hydrochloride 500 MG Extended Release Oral Tablet",
not RxNorm 860975. We build a small dictionary from every coded concept in the
Synthea bundles (standing in for a real SNOMED/RxNorm/LOINC terminology
server) and match against it, returning a score so weak matches can be sent to
review instead of being coded wrong.
"""
import json
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLES = ROOT / "data" / "synthea" / "fhir"
CACHE = ROOT / "data" / "terminology.json"

# resource type -> (element holding the CodeableConcept, our category name)
SOURCES = {"Condition": ("code", "condition"),
           "MedicationRequest": ("medicationCodeableConcept", "medication"),
           "Observation": ("code", "lab")}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def build() -> dict:
    vocab = {cat: {} for _, cat in SOURCES.values()}
    for path in BUNDLES.glob("*.json"):
        for e in json.loads(path.read_text()).get("entry", []):
            r = e["resource"]
            if r["resourceType"] not in SOURCES:
                continue
            elem, cat = SOURCES[r["resourceType"]]
            for c in r.get(elem, {}).get("coding", []):
                if c.get("display"):
                    vocab[cat][norm(c["display"])] = {"system": c["system"], "code": c["code"],
                                                      "display": c["display"]}
    CACHE.write_text(json.dumps(vocab))
    return vocab


@lru_cache
def vocab() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else build()


def lookup(category: str, text: str, min_score: float = 0.80) -> tuple[dict | None, float]:
    """Best coding for `text` and its similarity score (1.0 = exact after normalizing)."""
    q = norm(text)
    table = vocab()[category]
    if q in table:
        return table[q], 1.0
    best, score = None, 0.0
    for k, v in table.items():
        s = SequenceMatcher(None, q, k).ratio()
        if s > score:
            best, score = v, s
    return (best, round(score, 3)) if score >= min_score else (None, round(score, 3))
