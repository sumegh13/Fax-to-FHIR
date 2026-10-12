"""Score extractor predictions against fax ground truth, field by field.

Predictions: data/predictions/<run>/<fax_id>.json = {fax_id, fields: [{key, value, confidence}]}.

List fields (conditions[i], medications[i], labs[i].*) are aligned by content,
not index: an extractor that reads the medication list in a different order is
not wrong. Labs align by test name, then value/unit are scored on that row.

A field is correct when the *normalized* value matches (dates in any format,
M == male, case/punctuation-insensitive text). Exact string match is reported
too, so normalization never hides a reading error silently.
"""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from fhirmap.normalize import to_date, to_gender, to_number, to_phone

FAXES = Path(__file__).resolve().parent.parent / "data" / "faxes"


def family(key: str) -> str:
    """'labs[3].value' -> 'labs.value', 'medications[0]' -> 'medications'."""
    return re.sub(r"\[\d+\]", "", key)


def _text(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def normalized_equal(fam: str, truth: str, pred: str) -> bool:
    if pred is None:
        return False
    if fam.endswith("birthDate") or fam == "lab.collected":
        return to_date(truth) is not None and to_date(truth) == to_date(pred)
    if fam.endswith("gender"):
        return to_gender(truth) == to_gender(pred)
    if fam.endswith("phone"):
        return to_phone(truth) == to_phone(pred)
    if fam in ("labs.value", "rx.quantity", "rx.refills"):
        a, b = to_number(truth), to_number(pred)
        return a is not None and a == b
    return _text(truth) == _text(pred)


def _align(truth_items: list, pred_items: list, key=lambda x: x) -> tuple[list[tuple], list]:
    """Greedy best-similarity pairing; unmatched truth items pair with None."""
    pairs, used = [], set()
    scored = sorted(((SequenceMatcher(None, _text(key(t)), _text(key(p))).ratio(), ti, pi)
                     for ti, t in enumerate(truth_items) for pi, p in enumerate(pred_items)), reverse=True)
    taken_t = {}
    for s, ti, pi in scored:
        if ti in taken_t or pi in used or s < 0.5:
            continue
        taken_t[ti] = pi
        used.add(pi)
    for ti, t in enumerate(truth_items):
        pairs.append((t, pred_items[taken_t[ti]] if ti in taken_t else None))
    extras = [p for pi, p in enumerate(pred_items) if pi not in used]
    return pairs, extras


def _rows(fields: list[dict], prefix: str) -> dict[int, dict]:
    rows = {}
    for f in fields:
        m = re.match(rf"^{prefix}\[(\d+)\](?:\.(\w+))?$", f["key"])
        if m:
            rows.setdefault(int(m[1]), {})[m[2] or "value"] = f
    return rows


def score_fax(truth: dict, pred: dict) -> list[dict]:
    """One record per truth field (plus extras), with correctness and the prediction's confidence."""
    out = []
    pf = pred.get("fields", [])
    pred_scalar = {f["key"]: f for f in pf if "[" not in f["key"]}
    meta = {"fax_id": truth["fax_id"], "doc_type": truth["doc_type"], "level": truth["level"]}

    def rec(t, p, fam):
        pv = p["value"] if p else None
        return {**meta, "key": t["key"], "family": fam, "truth": t["value"], "pred": pv,
                "handwritten": t["handwritten"], "confidence": p.get("confidence") if p else None,
                "found": p is not None, "exact": pv == t["value"],
                "correct": normalized_equal(fam, t["value"], pv)}

    for t in truth["fields"]:
        if "[" not in t["key"]:
            out.append(rec(t, pred_scalar.get(t["key"]), family(t["key"])))

    for prefix in ("conditions", "medications"):
        tr = [r["value"] for r in _rows(truth["fields"], prefix).values()]
        pr = [r["value"] for r in _rows(pf, prefix).values()]
        pairs, extras = _align(tr, pr, key=lambda f: f["value"])
        out += [rec(t, p, prefix) for t, p in pairs]
        out += [{**meta, "key": e["key"], "family": prefix, "truth": None, "pred": e["value"], "handwritten": False,
                 "confidence": e.get("confidence"), "found": True, "exact": False, "correct": False, "extra": True}
                for e in extras]

    tr, pr = list(_rows(truth["fields"], "labs").values()), list(_rows(pf, "labs").values())
    pairs, _ = _align(tr, pr, key=lambda row: row.get("test", {}).get("value", ""))
    for trow, prow in pairs:
        for col, t in trow.items():
            out.append(rec(t, (prow or {}).get(col), f"labs.{col}"))
    return out


def score_run(pred_dir: Path) -> list[dict]:
    records = []
    for tpath in sorted(FAXES.glob("*.truth.json")):
        truth = json.loads(tpath.read_text())
        ppath = pred_dir / f"{truth['fax_id']}.json"
        pred = json.loads(ppath.read_text()) if ppath.exists() else {"fields": []}
        records += score_fax(truth, pred)
    return records
