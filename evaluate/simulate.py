"""A fake extractor for testing the evaluation and review pipeline without a model.

It copies the truth and corrupts it with OCR-like errors whose rate depends on
noise level and handwriting, and gives each field a confidence that is
*imperfectly* correlated with whether it was corrupted. Its numbers say nothing
about real extraction accuracy; they only prove the scoring works.
"""
import json
import random
from pathlib import Path

FAXES = Path(__file__).resolve().parent.parent / "data" / "faxes"

ERR_RATE = {"clean": 0.02, "mild": 0.06, "ugly": 0.14}
CONFUSABLE = {"0": "O", "O": "0", "1": "l", "l": "1", "5": "S", "S": "5", "8": "B", "B": "8",
              "rn": "m", "m": "rn", "e": "c", "a": "o", "/": "1", "7": "1"}


def corrupt(value: str, rng: random.Random) -> str:
    if not value:
        return value
    r = rng.random()
    if r < 0.15:
        return ""  # missed entirely
    for _ in range(rng.randint(1, 2)):
        hits = [k for k in CONFUSABLE if k in value]
        if hits and rng.random() < 0.8:
            k = rng.choice(hits)
            i = value.find(k)
            value = value[:i] + CONFUSABLE[k] + value[i + len(k):]
        elif value:
            i = rng.randrange(len(value))
            value = value[:i] + value[i + 1:]
    return value


def simulate_fax(truth: dict, seed: int = 0) -> dict:
    """Simulated extraction for one fax. Keeps truth bboxes so the review screen can crop fields."""
    rng = random.Random(f"{seed}:{truth['fax_id']}")
    fields = []
    for f in truth["fields"]:
        p = ERR_RATE[truth["level"]] * (3 if f["handwritten"] else 1)
        wrong = rng.random() < p
        value = corrupt(f["value"], rng) if wrong else f["value"]
        if value == "":
            continue
        # Confidence: errors usually look uncertain, but not always (that is what the review gate must handle).
        base = rng.betavariate(2, 5) if wrong and rng.random() < 0.75 else rng.betavariate(9, 1.3)
        conf = max(0.01, min(0.99, base - (0.08 if f["handwritten"] else 0)))
        fields.append({"key": f["key"], "value": value, "confidence": round(conf, 3), "bbox": f["bbox"]})
    return {"fax_id": truth["fax_id"], "doc_type": truth["doc_type"], "extractor": "simulated", "fields": fields}


def simulate(out_dir: Path, seed: int = 0):
    out_dir.mkdir(parents=True, exist_ok=True)
    for tpath in sorted(FAXES.glob("*.truth.json")):
        pred = simulate_fax(json.loads(tpath.read_text()), seed)
        (out_dir / f"{pred['fax_id']}.json").write_text(json.dumps(pred, indent=1))
