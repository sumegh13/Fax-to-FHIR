"""Render Synthea patients as noisy faxes with per-field ground truth.

    python -m faxgen                     # every patient x every doc type, mixed noise
    python -m faxgen --limit 5 --level ugly

Writes data/faxes/<id>.png, <id>.pdf, <id>.truth.json and manifest.jsonl.
"""
import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import ImageDraw

from .layout import DOC_TYPES, eligible
from .noise import LEVELS, degrade
from .synthea import load_bundle

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundles", type=Path, default=ROOT / "data" / "synthea" / "fhir")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "faxes")
    ap.add_argument("--types", default=",".join(DOC_TYPES))
    ap.add_argument("--level", choices=[*LEVELS, "mixed"], default="mixed")
    ap.add_argument("--hand-prob", type=float, default=0.35, help="chance a fill-in field is handwritten")
    ap.add_argument("--limit", type=int, default=0, help="max patients")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--debug-boxes", action="store_true", help="also write <id>.boxes.png with truth boxes drawn")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    bundles = sorted(b for b in args.bundles.glob("*.json") if "Information" not in b.name)
    if args.limit:
        bundles = bundles[:args.limit]

    manifest = []
    for bi, path in enumerate(bundles):
        p = load_bundle(path)
        for doc_type in args.types.split(","):
            if not eligible(doc_type, p):
                continue
            rng = random.Random(f"{args.seed}:{p.fhir_id}:{doc_type}")
            level = rng.choice(["clean", "mild", "mild", "ugly", "ugly"]) if args.level == "mixed" else args.level
            # Send the fax a few days after the newest fact it reports, so dates stay consistent.
            if doc_type == "lab":
                dates = [p.labs[0]["date"]]
            else:
                dates = [p.conditions[0]["onset"]] + [m["authored"] for m in p.medications[:1]]
            latest = max([d for d in dates if d] or ["2026-01-01"])
            sent = (date.fromisoformat(latest) + timedelta(days=rng.randint(1, 10))).isoformat()
            stamp_date = sent[5:7] + "/" + sent[8:] + "/" + sent[:4]

            clean, fields = DOC_TYPES[doc_type](p, rng, args.hand_prob, sent)
            img, fields, angle = degrade(clean, fields, level, rng, stamp_date)

            fax_id = f"{doc_type}-{p.family.lower()}-{p.fhir_id[:8]}"
            img.save(args.out / f"{fax_id}.png", optimize=True)
            img.convert("1").save(args.out / f"{fax_id}.pdf", resolution=200)
            truth = {"fax_id": fax_id, "doc_type": doc_type, "level": level, "skew_deg": round(angle, 2),
                     "patient_fhir_id": p.fhir_id, "sent_date": sent, "fields": fields}
            (args.out / f"{fax_id}.truth.json").write_text(json.dumps(truth, indent=2))

            if args.debug_boxes:
                dbg = img.convert("RGB")
                d = ImageDraw.Draw(dbg)
                for f in fields:
                    d.rectangle(f["bbox"], outline=(255, 0, 0) if f["handwritten"] else (0, 160, 0), width=3)
                dbg.save(args.out / f"{fax_id}.boxes.png")

            manifest.append({"fax_id": fax_id, "doc_type": doc_type, "level": level, "n_fields": len(fields),
                             "n_handwritten": sum(f["handwritten"] for f in fields)})
        print(f"[{bi + 1}/{len(bundles)}] {p.given} {p.family}")

    with open(args.out / "manifest.jsonl", "w") as fh:
        fh.writelines(json.dumps(m) + "\n" for m in manifest)
    by_level = {lv: sum(m["level"] == lv for m in manifest) for lv in LEVELS}
    print(f"{len(manifest)} faxes, {sum(m['n_fields'] for m in manifest)} truth fields, levels {by_level}")


if __name__ == "__main__":
    main()
