"""Talk to the HAPI FHIR server: match patients, validate, commit, undo."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from .normalize import strip_digits, to_date

load_dotenv()  # .env at the repo root; real environment variables win

BASE = os.environ.get("FHIR_BASE", "http://localhost:8080/fhir")
COMMITS = Path(__file__).resolve().parent.parent / "data" / "commits"
HEADERS = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}


class FhirError(RuntimeError):
    def __init__(self, status: int, body: dict | str):
        self.status, self.body = status, body
        diag = body
        if isinstance(body, dict):
            diag = "; ".join(i.get("diagnostics", "") for i in body.get("issue", []))
        super().__init__(f"HTTP {status}: {diag}")


def request(method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict:
    url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            raise FhirError(e.code, json.loads(raw)) from None
        except json.JSONDecodeError:
            raise FhirError(e.code, raw) from None


def match_patient(family: str, given: str, birth_date: str) -> list[dict]:
    """Candidates with the same DOB whose names match once Synthea's digit suffixes are removed.

    FHIR string search is starts-with, so 'Mertz' finds 'Mertz280'; we then
    compare exactly to avoid 'Mertz' matching 'Mertzke'.
    """
    dob = to_date(birth_date)
    if not (family and dob):
        return []
    q = urllib.parse.urlencode({"family": family, "birthdate": dob, "_count": 20})
    out = []
    for e in request("GET", f"Patient?{q}").get("entry", []):
        p = e["resource"]
        for n in p.get("name", []):
            fam = strip_digits(n.get("family", "")).lower()
            giv = [strip_digits(g).lower() for g in n.get("given", [])]
            if fam == family.lower() and (not given or given.lower() in giv):
                out.append(p)
                break
    return out


def validate(bundle: dict) -> list[dict]:
    """Server-side validation of each resource; returns error/fatal issues only."""
    problems = []
    for i, e in enumerate(bundle["entry"]):
        r = e["resource"]
        try:
            oo = request("POST", f"{r['resourceType']}/$validate", r)
        except FhirError as err:
            oo = err.body if isinstance(err.body, dict) else {"issue": [{"severity": "error",
                                                                        "diagnostics": str(err.body)}]}
        for iss in oo.get("issue", []):
            if iss.get("severity") in ("error", "fatal"):
                problems.append({"entry": i, "resourceType": r["resourceType"],
                                 "diagnostics": iss.get("diagnostics", "")})
    return problems


def commit(bundle: dict, fax_id: str) -> dict:
    """POST the transaction and log what was created so it can be undone."""
    resp = request("POST", "", bundle, timeout=300)
    created = []
    for req_entry, res_entry in zip(bundle["entry"], resp.get("entry", [])):
        loc = res_entry["response"].get("location", "")
        status = res_entry["response"]["status"]
        # "/_history/1" suffix; conditional creates that matched return 200 and must not be undone.
        ref = "/".join(loc.split("/")[:2])
        created.append({"ref": ref, "status": status, "new": status.startswith("201")})
    log = {"fax_id": fax_id, "committed_at": datetime.now(timezone.utc).isoformat(), "resources": created}
    COMMITS.mkdir(parents=True, exist_ok=True)
    (COMMITS / f"{fax_id}.json").write_text(json.dumps(log, indent=2))
    return log


def undo(fax_id: str) -> list[str]:
    """Delete everything a commit created, newest first so references never dangle."""
    path = COMMITS / f"{fax_id}.json"
    log = json.loads(path.read_text())
    deleted = []
    for r in reversed(log["resources"]):
        if r["new"]:
            request("DELETE", r["ref"])
            deleted.append(r["ref"])
    path.unlink()
    return deleted


def chart_index(patient_ref: str) -> dict:
    """What the patient's chart already holds, keyed the way the mapper looks things up.

    {"condition": {key: ref}, "medication": {key: ref}, "lab": {(key, date): ref}, "lab_values": {ref: value}}
    where key is "system|code" when coded, else normalized text.
    """
    from .terminology import norm

    pid = patient_ref.split("/")[1]

    def keys(cc: dict) -> list[str]:
        ks = [f"{c.get('system')}|{c.get('code')}" for c in cc.get("coding", []) if c.get("code")]
        for t in [cc.get("text")] + [c.get("display") for c in cc.get("coding", [])]:
            if t:
                ks.append(norm(t))
        return ks

    def fetch(query: str) -> list[dict]:
        out, url = [], query
        while url:
            page = request("GET", url)
            out += [e["resource"] for e in page.get("entry", [])]
            url = next((l["url"] for l in page.get("link", []) if l["relation"] == "next"), None)
        return out

    idx = {"condition": {}, "medication": {}, "lab": {}, "lab_values": {}}
    for c in fetch(f"Condition?patient={pid}&clinical-status=active&_count=200"):
        for k in keys(c.get("code", {})):
            idx["condition"].setdefault(k, f"Condition/{c['id']}")
    for m in fetch(f"MedicationRequest?patient={pid}&status=active&_count=200"):
        for k in keys(m.get("medicationCodeableConcept", {})):
            idx["medication"].setdefault(k, f"MedicationRequest/{m['id']}")
    for o in fetch(f"Observation?patient={pid}&category=laboratory&_count=500"):
        day = o.get("effectiveDateTime", "")[:10]
        for k in keys(o.get("code", {})):
            idx["lab"].setdefault((k, day), f"Observation/{o['id']}")
        if "valueQuantity" in o:
            idx["lab_values"][f"Observation/{o['id']}"] = o["valueQuantity"]["value"]
    return idx
