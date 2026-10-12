from evaluate.score import family, normalized_equal, score_fax


def truth(fields):
    return {"fax_id": "t", "doc_type": "referral", "level": "clean",
            "fields": [{"key": k, "value": v, "handwritten": False} for k, v in fields]}


def pred(fields):
    return {"fields": [{"key": k, "value": v, "confidence": 0.9} for k, v in fields]}


def by_key(records):
    return {r["key"]: r for r in records}


def test_family_strips_indexes():
    assert family("labs[3].value") == "labs.value"
    assert family("medications[0]") == "medications"
    assert family("patient.mrn") == "patient.mrn"


def test_normalized_match_is_type_aware():
    assert normalized_equal("patient.birthDate", "2/4/1945", "02-04-1945")
    assert normalized_equal("patient.gender", "M", "male")
    assert normalized_equal("labs.value", "13.0", "13")
    assert normalized_equal("referral.reason", "Chronic sinusitis (disorder)", "chronic sinusitis disorder")
    assert not normalized_equal("patient.mrn", "2DE4DD0D", "20E40000")
    assert not normalized_equal("patient.birthDate", "13/45/1990", "13/45/1990")  # unparseable never counts


def test_list_order_does_not_matter():
    t = truth([("medications[0]", "Aspirin 81 MG Oral Tablet"), ("medications[1]", "Lisinopril 10 MG Oral Tablet")])
    p = pred([("medications[0]", "Lisinopril 10 MG Oral Tablet"), ("medications[1]", "Aspirin 81 MG Oral Tablet")])
    assert all(r["correct"] for r in score_fax(t, p))


def test_missing_and_extra_fields():
    t = truth([("patient.family", "Mertz"), ("conditions[0]", "Asthma")])
    p = pred([("conditions[0]", "Asthma"), ("conditions[1]", "Invented condition")])
    rs = score_fax(t, p)
    fam = next(r for r in rs if r["key"] == "patient.family")
    assert not fam["found"] and not fam["correct"]
    extras = [r for r in rs if r.get("extra")]
    assert len(extras) == 1 and extras[0]["pred"] == "Invented condition"


def test_lab_rows_align_by_test_name():
    t = truth([("labs[0].test", "Glucose"), ("labs[0].value", "99.4"),
               ("labs[1].test", "Sodium"), ("labs[1].value", "139.6")])
    p = pred([("labs[0].test", "Sodium"), ("labs[0].value", "139.6"),
              ("labs[1].test", "Glucose"), ("labs[1].value", "9.4")])
    rs = by_key(score_fax(t, p))
    assert rs["labs[1].value"]["correct"]          # Sodium matched despite swapped rows
    assert not rs["labs[0].value"]["correct"]      # Glucose 99.4 read as 9.4
