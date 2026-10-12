"""Per-field accuracy report for an extraction run.

    python -m evaluate simulate                  # write data/predictions/simulated/
    python -m evaluate report simulated [--threshold 0.8]

Writes data/reports/<run>/records.csv, summary.json and report.md.
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from .score import score_run
from .simulate import simulate

ROOT = Path(__file__).resolve().parent.parent
PRED = ROOT / "data" / "predictions"
REPORTS = ROOT / "data" / "reports"


def acc(rs: list[dict]) -> dict:
    truth_rs = [r for r in rs if r["truth"] is not None]
    n = len(truth_rs)
    return {"n": n,
            "accuracy": round(sum(r["correct"] for r in truth_rs) / n, 4) if n else None,
            "exact": round(sum(r["exact"] for r in truth_rs) / n, 4) if n else None,
            "missing": round(sum(not r["found"] for r in truth_rs) / n, 4) if n else None}


def by(rs, keyfn):
    groups = defaultdict(list)
    for r in rs:
        groups[keyfn(r)].append(r)
    return {k: acc(v) for k, v in sorted(groups.items())}


def gate(rs: list[dict], threshold: float) -> dict:
    """What the review screen would do at this threshold.

    review_rate: share of extracted fields sent to a human.
    auto_error_rate: share of auto-committed fields that are wrong (errors that reach the EHR).
    errors_caught: share of all wrong extracted fields that the gate sent to review.
    """
    found = [r for r in rs if r["found"]]
    review = [r for r in found if (r["confidence"] or 0) < threshold]
    auto = [r for r in found if (r["confidence"] or 0) >= threshold]
    wrong = [r for r in found if not r["correct"]]
    return {"threshold": threshold,
            "review_rate": round(len(review) / len(found), 4) if found else None,
            "auto_error_rate": round(sum(not r["correct"] for r in auto) / len(auto), 4) if auto else None,
            "errors_caught": round(sum(not r["correct"] for r in review) / len(wrong), 4) if wrong else None}


def calibration(rs: list[dict], bins: int = 10) -> list[dict]:
    found = [r for r in rs if r["found"] and r["confidence"] is not None]
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        bucket = [r for r in found if lo <= r["confidence"] < hi or (b == bins - 1 and r["confidence"] == 1)]
        if bucket:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(bucket),
                        "mean_conf": round(sum(r["confidence"] for r in bucket) / len(bucket), 3),
                        "accuracy": round(sum(r["correct"] for r in bucket) / len(bucket), 3)})
    return out


def pct(x):
    return "-" if x is None else f"{x * 100:.1f}%"


def write_md(run: str, s: dict) -> str:
    L = [f"# Extraction report: `{run}`", ""]
    if run == "simulated":
        L += ["> Simulated extractor (corrupted ground truth). These numbers test the scoring, "
              "not any real model.", ""]
    o = s["overall"]
    L += [f"**{o['n']} fields** across {s['n_faxes']} faxes: accuracy **{pct(o['accuracy'])}** "
          f"(exact string {pct(o['exact'])}, missing {pct(o['missing'])})."
          + (f" Extraction cost ${s['cost_usd']:.3f}." if s.get("cost_usd") else ""), ""]
    g = s["gate"]
    L += [f"**Review gate at confidence {g['threshold']}:** {pct(g['review_rate'])} of fields go to review, "
          f"catching {pct(g['errors_caught'])} of errors; {pct(g['auto_error_rate'])} of auto-committed "
          f"fields are wrong.", ""]
    for title, table in (("By field", s["by_family"]), ("By noise level", s["by_level"]),
                         ("Printed vs handwritten", s["by_handwriting"]), ("By document type", s["by_doc_type"])):
        L += [f"## {title}", "", "| | n | accuracy | exact | missing |", "|---|---:|---:|---:|---:|"]
        L += [f"| {k} | {v['n']} | {pct(v['accuracy'])} | {pct(v['exact'])} | {pct(v['missing'])} |"
              for k, v in table.items()]
        L.append("")
    L += ["## Threshold sweep", "", "| threshold | to review | errors caught | auto-commit error |",
          "|---:|---:|---:|---:|"]
    L += [f"| {t['threshold']} | {pct(t['review_rate'])} | {pct(t['errors_caught'])} | {pct(t['auto_error_rate'])} |"
          for t in s["sweep"]]
    L += ["", "## Calibration", "", "| confidence | n | mean conf | accuracy |", "|---|---:|---:|---:|"]
    L += [f"| {c['bin']} | {c['n']} | {c['mean_conf']} | {pct(c['accuracy'])} |" for c in s["calibration"]]
    return "\n".join(L) + "\n"


def cmd_report(args):
    rs = score_run(PRED / args.run)
    if not any(r["found"] for r in rs):
        raise SystemExit(f"no predictions in {PRED / args.run}")
    s = {"run": args.run, "n_faxes": len({r["fax_id"] for r in rs}),
         "overall": acc(rs),
         "by_family": by(rs, lambda r: r["family"]),
         "by_level": by(rs, lambda r: r["level"]),
         "by_handwriting": by(rs, lambda r: "handwritten" if r["handwritten"] else "printed"),
         "by_doc_type": by(rs, lambda r: r["doc_type"]),
         "gate": gate(rs, args.threshold),
         "sweep": [gate(rs, t / 100) for t in range(50, 100, 5)],
         "calibration": calibration(rs),
         "extras": sum(1 for r in rs if r.get("extra")),
         "cost_usd": round(sum(json.loads(p.read_text()).get("usage", {}).get("cost_usd", 0)
                               for p in (PRED / args.run).glob("*.json")), 4)}
    out = REPORTS / args.run
    out.mkdir(parents=True, exist_ok=True)
    cols = ["fax_id", "doc_type", "level", "key", "family", "handwritten", "truth", "pred", "confidence",
            "found", "exact", "correct"]
    with open(out / "records.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rs)
    (out / "summary.json").write_text(json.dumps(s, indent=2))
    md = write_md(args.run, s)
    (out / "report.md").write_text(md)
    print(md)
    print(f"wrote {out}/")


def cmd_simulate(args):
    simulate(PRED / "simulated", args.seed)
    print(f"wrote {PRED / 'simulated'}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    s = sub.add_parser("simulate")
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(fn=cmd_simulate)
    r = sub.add_parser("report")
    r.add_argument("run")
    r.add_argument("--threshold", type=float, default=0.8)
    r.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
