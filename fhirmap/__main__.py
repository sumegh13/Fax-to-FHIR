"""Commit an extraction (or a truth file) to HAPI as FHIR.

    python -m fhirmap commit data/faxes/referral-mertz-68f1bc18.truth.json [--dry-run]
    python -m fhirmap undo referral-mertz-68f1bc18
    python -m fhirmap roundtrip [--limit N]     # commit truth files, read back, compare, undo
"""
import argparse
import json
import sys
from pathlib import Path

from . import client
from .mapper import build_bundle, group
from .normalize import to_date, to_gender, to_number

ROOT = Path(__file__).resolve().parent.parent


def prepare(extraction: dict) -> tuple[dict, list, str | None]:
    """Match the patient, build the bundle, run validation. Returns (bundle, issues, matched_ref)."""
    s, _ = group(extraction["fields"])
    matches = client.match_patient(s.get("patient.family", ""), s.get("patient.given", ""),
                                   s.get("patient.birthDate", ""))
    ref = f"Patient/{matches[0]['id']}" if len(matches) == 1 else None
    bundle, issues = build_bundle(extraction, ref)
    if len(matches) > 1:
        issues.append({"key": "patient", "value": [m["id"] for m in matches],
                       "reason": f"{len(matches)} patients match name+DOB"})
    for p in client.validate(bundle):
        issues.append({"key": f"bundle[{p['entry']}].{p['resourceType']}", "value": None,
                       "reason": p["diagnostics"]})
    return bundle, issues, ref


def cmd_commit(args):
    extraction = json.loads(Path(args.file).read_text())
    bundle, issues, ref = prepare(extraction)
    print(f"patient: {ref or 'new'}   resources: {len(bundle['entry'])}   issues: {len(issues)}")
    for i in issues:
        print(f"  ! {i['key']}: {i['reason']}  ({i['value']!r})")
    if args.dry_run:
        json.dump(bundle, sys.stdout, indent=2)
        return
    if issues and not args.force:
        sys.exit("not committed: resolve issues or pass --force")
    log = client.commit(bundle, extraction["fax_id"])
    print(f"committed {sum(r['new'] for r in log['resources'])} new resources")
    for r in log["resources"]:
        print(f"  {r['status']:<12} {r['ref']}")


def cmd_undo(args):
    for ref in client.undo(args.fax_id):
        print(f"deleted {ref}")


def read_back(fields: dict, log: dict) -> list[str]:
    """Compare a few committed values with what the server now returns."""
    errs = []
    refs = {r["ref"].split("/")[0]: r["ref"] for r in log["resources"]}
    if "Patient" in refs:
        p = client.request("GET", refs["Patient"])
        if p.get("birthDate") != to_date(fields.get("patient.birthDate", "")):
            errs.append(f"birthDate {p.get('birthDate')}")
        if p.get("gender") != to_gender(fields.get("patient.gender", "")):
            errs.append(f"gender {p.get('gender')}")
    for r in log["resources"]:
        if r["ref"].startswith("Observation/"):
            o = client.request("GET", r["ref"])
            if "valueQuantity" not in o or to_number(str(o["valueQuantity"]["value"])) is None:
                errs.append(f"{r['ref']} lost its value")
    return errs


def cmd_roundtrip(args):
    files = sorted((ROOT / "data" / "faxes").glob("*.truth.json"))[: args.limit or None]
    ok = 0
    for f in files:
        ext = json.loads(f.read_text())
        bundle, issues, ref = prepare(ext)
        log = client.commit(bundle, ext["fax_id"])
        errs = read_back(group(ext["fields"])[0], log)
        new = sum(r["new"] for r in log["resources"])
        client.undo(ext["fax_id"])
        flag = "ok " if not errs and ref else "ERR"
        ok += flag == "ok "
        print(f"{flag} {ext['fax_id']:<42} matched={bool(ref)} new={new:<3} issues={len(issues)} {errs or ''}")
        for i in issues:
            print(f"      ! {i['key']}: {i['reason']}")
    print(f"{ok}/{len(files)} faxes committed, matched to their patient and read back cleanly")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    c = sub.add_parser("commit")
    c.add_argument("file")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--force", action="store_true", help="commit even with mapping issues")
    c.set_defaults(fn=cmd_commit)
    u = sub.add_parser("undo")
    u.add_argument("fax_id")
    u.set_defaults(fn=cmd_undo)
    r = sub.add_parser("roundtrip")
    r.add_argument("--limit", type=int, default=0)
    r.set_defaults(fn=cmd_roundtrip)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
