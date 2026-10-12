"""Mapper tests run offline: no FHIR server, terminology from the cached dictionary."""
import pytest

from fhirmap.mapper import build_bundle
from fhirmap.terminology import vocab


def extraction(doc_type, fields):
    return {"fax_id": "test", "doc_type": doc_type, "fields": [{"key": k, "value": v} for k, v in fields]}


def resources(bundle, rtype):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rtype]


@pytest.fixture(scope="module")
def known():
    v = vocab()
    return {cat: next(iter(v[cat].values())) for cat in ("condition", "medication", "lab")}


def test_new_patient_from_referral(known):
    ext = extraction("referral", [
        ("patient.family", "Mertz"), ("patient.given", "Alonso"), ("patient.birthDate", "2/4/1945"),
        ("patient.gender", "M"), ("patient.phone", "555-591-8901"),
        ("referral.reason", known["condition"]["display"]), ("referral.priority", "urgent"),
        ("medications[0]", known["medication"]["display"]),
    ])
    bundle, issues, reconciled = build_bundle(ext)
    assert issues == [] and reconciled == []
    (p,) = resources(bundle, "Patient")
    assert p["birthDate"] == "1945-02-04" and p["gender"] == "male"
    (sr,) = resources(bundle, "ServiceRequest")
    assert sr["priority"] == "urgent"
    assert sr["reasonReference"][0]["reference"].startswith("urn:uuid:")
    (mr,) = resources(bundle, "MedicationRequest")
    assert mr["medicationCodeableConcept"]["coding"][0]["code"] == known["medication"]["code"]
    assert all(r["meta"]["tag"][1]["code"] == "test" for r in (e["resource"] for e in bundle["entry"]))


def test_bad_values_become_issues_not_guesses():
    ext = extraction("referral", [
        ("patient.family", "Zzyzx"), ("patient.birthDate", "13/45/1990"), ("patient.phone", "555-377-773"),
        ("medications[0]", "Unobtainium 5 MG Oral Tablet"), ("referral.priority", "whenever"),
    ])
    bundle, issues, _ = build_bundle(ext)
    keys = {i["key"] for i in issues}
    assert {"patient.birthDate", "patient.phone", "medications[0]", "referral.priority"} <= keys
    (p,) = resources(bundle, "Patient")
    assert "birthDate" not in p and "telecom" not in p


def test_existing_chart_items_are_linked_not_copied(known):
    lab, cond = known["lab"], known["condition"]
    chart = {
        "condition": {f"{cond['system']}|{cond['code']}": "Condition/1"},
        "medication": {},
        "lab": {(f"{lab['system']}|{lab['code']}", "2026-03-12"): "Observation/7"},
        "lab_values": {"Observation/7": 12.97},
    }
    ext = extraction("lab", [
        ("lab.collected", "03/12/2026"),
        ("labs[0].test", lab["display"]), ("labs[0].value", "13.0"), ("labs[0].unit", "mg/dL"),
        ("conditions[0]", cond["display"]),
    ])
    bundle, issues, reconciled = build_bundle(ext, "Patient/5", chart)
    assert {r["existing"] for r in reconciled} == {"Condition/1", "Observation/7"}
    assert resources(bundle, "Observation") == [] and resources(bundle, "Condition") == []
    (dr,) = resources(bundle, "DiagnosticReport")
    assert dr["result"] == [{"reference": "Observation/7"}]
    assert issues == []  # 13.0 printed matches 12.97 on file at one decimal place

    ext["fields"][2]["value"] = "1.3"
    _, issues, _ = build_bundle(ext, "Patient/5", chart)
    assert [i["key"] for i in issues] == ["labs[0].value"]
