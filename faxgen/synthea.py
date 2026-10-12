"""Pull fax-relevant facts out of a Synthea transaction bundle.

Synthea appends digits to names (e.g. "Mueller846"); we strip them so the
printed fax looks real. Ground truth is always the *printed* value, and each
record keeps the FHIR id so it can be matched back to the HAPI server.
"""
import json
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

_DIGITS = re.compile(r"\d+")


def clean_name(s: str) -> str:
    return _DIGITS.sub("", s).strip()


def _coding(cc: dict) -> dict:
    c = (cc or {}).get("coding", [{}])[0]
    return {"system": c.get("system"), "code": c.get("code"),
            "display": c.get("display") or cc.get("text")}


@dataclass
class PatientFacts:
    fhir_id: str
    given: str
    family: str
    birth_date: str
    gender: str
    mrn: str
    phone: str
    address: str
    conditions: list = field(default_factory=list)   # active, newest first
    medications: list = field(default_factory=list)  # active, newest first
    labs: list = field(default_factory=list)         # newest first
    practitioner: str = ""
    organization: str = ""
    org_phone: str = ""


def load_bundle(path: Path) -> PatientFacts:
    entries = [e["resource"] for e in json.loads(Path(path).read_text())["entry"]]
    by_type: dict[str, list] = {}
    for r in entries:
        by_type.setdefault(r["resourceType"], []).append(r)

    p = by_type["Patient"][0]
    name = next((n for n in p["name"] if n.get("use") == "official"), p["name"][0])
    mrn_full = next(i["value"] for i in p["identifier"]
                    if i.get("type", {}).get("coding", [{}])[0].get("code") == "MR")
    addr = p.get("address", [{}])[0]
    phone = next((t["value"] for t in p.get("telecom", []) if t["system"] == "phone"), "")

    conditions = []
    for c in by_type.get("Condition", []):
        if c["clinicalStatus"]["coding"][0]["code"] != "active":
            continue
        code = _coding(c["code"])
        # Synthea codes social findings as conditions; a referral would not list them.
        if "(finding)" in (code["display"] or "") and "disorder" not in code["display"]:
            continue
        conditions.append({**code, "fhir_id": c["id"], "onset": c.get("onsetDateTime", "")[:10]})
    conditions.sort(key=lambda c: c["onset"], reverse=True)

    meds = []
    for m in by_type.get("MedicationRequest", []):
        if m["status"] != "active" or "medicationCodeableConcept" not in m:
            continue
        dose = m.get("dosageInstruction", [{}])[0]
        timing = dose.get("timing", {}).get("repeat", {})
        sig = ""
        if timing.get("frequency"):
            sig = f"{timing['frequency']}x per {timing.get('period', 1)} {timing.get('periodUnit', 'd')}"
        meds.append({**_coding(m["medicationCodeableConcept"]), "fhir_id": m["id"],
                     "authored": m.get("authoredOn", "")[:10], "sig": sig})
    meds.sort(key=lambda m: m["authored"], reverse=True)

    labs = []
    for o in by_type.get("Observation", []):
        cat = o.get("category", [{}])[0].get("coding", [{}])[0].get("code")
        vq = o.get("valueQuantity")
        if cat != "laboratory" or not vq:
            continue
        labs.append({**_coding(o["code"]), "fhir_id": o["id"],
                     "value": round(vq["value"], 1), "unit": vq.get("unit", ""),
                     "date": o.get("effectiveDateTime", "")[:10]})
    labs.sort(key=lambda l: l["date"], reverse=True)

    enc = sorted(by_type.get("Encounter", []), key=lambda e: e["period"]["start"])[-1]
    prac = enc.get("participant", [{}])[0].get("individual", {}).get("display", "")
    org = enc.get("serviceProvider", {}).get("display", "")

    return PatientFacts(
        fhir_id=p["id"],
        given=clean_name(name["given"][0]),
        family=clean_name(name["family"]),
        birth_date=p["birthDate"],
        gender=p["gender"],
        mrn=mrn_full.replace("-", "")[:8].upper(),
        phone=phone,
        address=f"{addr.get('line', [''])[0]}, {addr.get('city', '')}, {addr.get('state', '')} {addr.get('postalCode', '')}".strip(", "),
        conditions=conditions,
        medications=meds,
        labs=labs,
        practitioner=clean_name(prac),
        organization=org.title() if org.isupper() else org,
        org_phone=f"555-{zlib.crc32(org.encode()) % 900 + 100}-{zlib.crc32(org[::-1].encode()) % 9000 + 1000}",
    )
