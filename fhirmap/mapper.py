"""Turn extracted fax fields into a FHIR R4 transaction bundle.

Input is an "extraction": {fax_id, doc_type, fields: [{key, value, confidence?}]}.
A truth file has the same shape, so ground truth can be committed directly to
test this half of the pipeline without a model.

Output is (bundle, issues). Issues are fields the mapper could not turn into
valid FHIR (unparseable date, uncoded drug, ...). The review screen shows them
alongside low-confidence fields; the mapper never invents a value.
"""
import re
import uuid
from collections import defaultdict

from . import normalize as nz
from .terminology import lookup, norm

TAG = {"system": "urn:fax-to-fhir", "code": "fax-ingest"}


def _urn() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _concept(coding: dict | None, text: str) -> dict:
    cc = {"text": text}
    if coding:
        cc["coding"] = [coding]
    return cc


def group(fields: list[dict]) -> tuple[dict, dict]:
    """Split keys into scalars ({'patient.family': v}) and lists ({'labs': {0: {'test': v}}})."""
    scalars, lists = {}, defaultdict(lambda: defaultdict(dict))
    for f in fields:
        m = re.match(r"^(\w+)\[(\d+)\](?:\.(\w+))?$", f["key"])
        if m:
            lists[m[1]][int(m[2])][m[3] or "value"] = f["value"]
        else:
            scalars[f["key"]] = f["value"]
    return scalars, {k: [v[i] for i in sorted(v)] for k, v in lists.items()}


class Builder:
    def __init__(self, fax_id: str, patient_ref: str | None, chart: dict | None = None):
        self.fax_id = fax_id
        self.entries: list[dict] = []
        self.issues: list[dict] = []
        self.chart = chart or {"condition": {}, "medication": {}, "lab": {}, "lab_values": {}}
        self.reconciled: list[dict] = []  # fax items already in the chart, not written again
        self.patient_ref = patient_ref  # "Patient/123" if matched, else set by add_patient

    def add(self, resource: dict, if_none_exist: str | None = None) -> str:
        resource.setdefault("meta", {})["tag"] = [TAG, {"system": "urn:fax-to-fhir:fax", "code": self.fax_id}]
        full_url = _urn()
        req = {"method": "POST", "url": resource["resourceType"]}
        if if_none_exist:
            req["ifNoneExist"] = if_none_exist
        self.entries.append({"fullUrl": full_url, "resource": resource, "request": req})
        return full_url

    def issue(self, key: str, value, reason: str):
        self.issues.append({"key": key, "value": value, "reason": reason})

    def subject(self) -> dict:
        return {"reference": self.patient_ref}

    def on_chart(self, category: str, key: str, coding: dict | None, text: str, day: str | None = None):
        """Reference to an existing chart entry for the same concept, if there is one."""
        cands = ([f"{coding['system']}|{coding['code']}"] if coding else []) + [norm(text)]
        for k in cands:
            ref = self.chart[category].get((k, day) if category == "lab" else k)
            if ref:
                self.reconciled.append({"key": key, "value": text, "existing": ref})
                return ref
        return None

    def bundle(self) -> dict:
        return {"resourceType": "Bundle", "type": "transaction", "entry": self.entries}

    # --- resources -------------------------------------------------------

    def patient(self, s: dict):
        p = {"resourceType": "Patient", "name": [{"use": "official"}]}
        if s.get("patient.family"):
            p["name"][0]["family"] = s["patient.family"]
        if s.get("patient.given"):
            p["name"][0]["given"] = [s["patient.given"]]
        if "patient.birthDate" in s:
            d = nz.to_date(s["patient.birthDate"])
            if d:
                p["birthDate"] = d
            else:
                self.issue("patient.birthDate", s["patient.birthDate"], "unparseable date")
        if "patient.gender" in s:
            g = nz.to_gender(s["patient.gender"])
            if g:
                p["gender"] = g
            else:
                self.issue("patient.gender", s["patient.gender"], "unknown sex code")
        if s.get("patient.mrn"):
            p["identifier"] = [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                                                     "code": "MR"}]},
                                "system": "urn:fax-to-fhir:mrn", "value": s["patient.mrn"]}]
        if "patient.phone" in s:
            ph = nz.to_phone(s["patient.phone"])
            if ph:
                p["telecom"] = [{"system": "phone", "value": ph, "use": "home"}]
            else:
                self.issue("patient.phone", s["patient.phone"], "phone is not 10 digits")
        if s.get("patient.address"):
            p["address"] = [nz.to_address(s["patient.address"])]
        self.patient_ref = self.add(p)

    def condition(self, key: str, text: str) -> str:
        coding, score = lookup("condition", text)
        existing = self.on_chart("condition", key, coding, text)
        if existing:
            return existing
        if not coding:
            self.issue(key, text, f"no SNOMED match (best score {score})")
        return self.add({
            "resourceType": "Condition",
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                           "code": "active"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                      "code": "problem-list-item"}]}],
            "code": _concept(coding, text),
            "subject": self.subject(),
        })

    def practitioner(self, name: str, npi: str | None) -> dict:
        """Requester reference. With an NPI we can create/reuse a real Practitioner; without, display only."""
        if not npi:
            return {"display": name}
        if not re.fullmatch(r"\d{10}", npi):
            self.issue("rx.npi", npi, "NPI must be 10 digits")
            return {"display": name}
        parts = re.sub(r"^(Dr\.?|Mr\.?|Mrs\.?|Ms\.?)\s+", "", name).split()
        ref = self.add({"resourceType": "Practitioner",
                        "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": npi}],
                        "name": [{"text": name, "family": parts[-1] if parts else name,
                                  "given": parts[:-1]}]},
                       if_none_exist=f"identifier=http://hl7.org/fhir/sid/us-npi|{npi}")
        return {"reference": ref, "display": name}

    def med_request(self, key: str, text: str, extra: dict | None = None) -> str:
        coding, score = lookup("medication", text)
        # A medication-list entry already on the chart is not re-ordered; a prior-auth request (extra) always is.
        if not extra:
            existing = self.on_chart("medication", key, coding, text)
            if existing:
                return existing
        if not coding:
            self.issue(key, text, f"no RxNorm match (best score {score})")
        return self.add({"resourceType": "MedicationRequest", "status": "active", "intent": "order",
                         "medicationCodeableConcept": _concept(coding, text), "subject": self.subject(),
                         **(extra or {})})


def build_bundle(extraction: dict, patient_ref: str | None = None,
                 chart: dict | None = None) -> tuple[dict, list[dict], list[dict]]:
    """patient_ref: existing 'Patient/<id>' to attach to; None creates a new Patient.
    chart: client.chart_index() of that patient, so items already on file are referenced, not duplicated.
    Returns (bundle, issues, reconciled)."""
    s, lists = group(extraction["fields"])
    b = Builder(extraction["fax_id"], patient_ref, chart)
    if not patient_ref:
        b.patient(s)

    b.add({"resourceType": "DocumentReference", "status": "current",
           "type": {"text": f"Inbound fax: {extraction['doc_type']}"},
           "subject": b.subject(), "description": f"Source fax {extraction['fax_id']}",
           "content": [{"attachment": {"contentType": "application/pdf",
                                       "url": f"faxes/{extraction['fax_id']}.pdf",
                                       "title": extraction["fax_id"]}}]})

    for i, c in enumerate(lists.get("conditions", [])):
        b.condition(f"conditions[{i}]", c["value"])
    for i, m in enumerate(lists.get("medications", [])):
        b.med_request(f"medications[{i}]", m["value"])

    doc = extraction["doc_type"]
    if doc == "referral":
        reason = b.condition("referral.reason", s["referral.reason"]) if s.get("referral.reason") else None
        sr = {"resourceType": "ServiceRequest", "status": "active", "intent": "order",
              "category": [{"coding": [{"system": "http://snomed.info/sct", "code": "3457005",
                                        "display": "Patient referral"}]}],
              "code": {"text": f"Referral to {s.get('referral.specialty', 'specialist')}"},
              "subject": b.subject()}
        if reason:
            sr["reasonReference"] = [{"reference": reason}]
        if s.get("referral.referring"):
            sr["requester"] = {"display": s["referral.referring"]}
        if "referral.priority" in s:
            pr = nz.to_priority(s["referral.priority"])
            if pr:
                sr["priority"] = pr
            else:
                b.issue("referral.priority", s["referral.priority"], "unknown priority")
        b.add(sr)

    elif doc == "lab":
        when = nz.to_date(s.get("lab.collected", ""))
        if not when:
            b.issue("lab.collected", s.get("lab.collected"), "unparseable date")
        results = []
        for i, row in enumerate(lists.get("labs", [])):
            coding, score = lookup("lab", row.get("test", ""))
            existing = b.on_chart("lab", f"labs[{i}].test", coding, row.get("test", ""), when) if when else None
            if existing:
                # Same test, same day already on file: reference it, but a different value means someone misread.
                on_file = b.chart["lab_values"].get(existing)
                read = nz.to_number(row.get("value", ""))
                printed = row.get("value", "").strip()
                places = len(printed.split(".")[1]) if "." in printed else 0
                if on_file is not None and read is not None and round(on_file, places) != round(read, places):
                    b.issue(f"labs[{i}].value", row.get("value"), f"chart has {on_file:g} for this test on {when}")
                results.append({"reference": existing})
                continue
            if not coding:
                b.issue(f"labs[{i}].test", row.get("test"), f"no LOINC match (best score {score})")
            value = nz.to_number(row.get("value", ""))
            obs = {"resourceType": "Observation", "status": "final",
                   "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                             "code": "laboratory"}]}],
                   "code": _concept(coding, row.get("test", "")), "subject": b.subject()}
            if when:
                obs["effectiveDateTime"] = when
            if value is None:
                b.issue(f"labs[{i}].value", row.get("value"), "result is not numeric")
                obs["valueString"] = row.get("value", "")
            else:
                unit = row.get("unit", "")
                obs["valueQuantity"] = {"value": value, "unit": unit, "system": "http://unitsofmeasure.org",
                                        "code": unit}
            results.append({"reference": b.add(obs)})
        dr = {"resourceType": "DiagnosticReport", "status": "final",
              "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB"}]}],
              "code": {"text": "Laboratory report (fax)"}, "subject": b.subject(), "result": results}
        if when:
            dr["effectiveDateTime"] = when
        if s.get("lab.ordering"):
            dr["performer"] = [{"display": s["lab.ordering"]}]
        b.add(dr)

    elif doc == "prior_auth":
        dx = b.condition("rx.diagnosis", s["rx.diagnosis"]) if s.get("rx.diagnosis") else None
        extra = {"requester": b.practitioner(s.get("rx.prescriber", ""), s.get("rx.npi"))}
        if dx:
            extra["reasonReference"] = [{"reference": dx}]
        if s.get("rx.sig"):
            extra["dosageInstruction"] = [{"text": s["rx.sig"]}]
        disp = {}
        qty, refills = nz.to_int(s.get("rx.quantity", "")), nz.to_int(s.get("rx.refills", ""))
        if qty is not None:
            disp["quantity"] = {"value": qty}
        elif "rx.quantity" in s:
            b.issue("rx.quantity", s["rx.quantity"], "quantity is not a whole number")
        if refills is not None:
            disp["numberOfRepeatsAllowed"] = refills
        elif "rx.refills" in s:
            b.issue("rx.refills", s["rx.refills"], "refills is not a whole number")
        if disp:
            extra["dispenseRequest"] = disp
        if s.get("rx.medication"):
            b.med_request("rx.medication", s["rx.medication"], extra)
        if s.get("coverage.memberId"):
            b.add({"resourceType": "Coverage", "status": "active", "subscriberId": s["coverage.memberId"],
                   "beneficiary": b.subject(), "payor": [{"display": s.get("coverage.payer", "Unknown payer")}]})

    return b.bundle(), b.issues, b.reconciled
