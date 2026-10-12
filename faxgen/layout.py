"""Draw clean fax pages and record a bounding box for every ground-truth field.

Pages are letter size at 200 dpi (fax "fine" mode). Each document type is a
function that takes PatientFacts and returns (image, fields). A field is
{key, value, bbox, handwritten, fhir: {...}} where `fhir` says which resource
and element the value belongs to; the evaluator scores per `key`.
"""
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .synthea import PatientFacts

W, H = 1700, 2200
MARGIN = 110
FONTS = Path(__file__).resolve().parent.parent / "data" / "fonts"
HANDS = ["HomemadeApple-Regular.ttf", "NothingYouCouldDo.ttf"]

SPECIALTIES = {
    "diabetes": "Endocrinology", "prediabetes": "Endocrinology", "hypertension": "Cardiology",
    "heart": "Cardiology", "coronary": "Cardiology", "asthma": "Pulmonology",
    "bronchitis": "Pulmonology", "pulmonary": "Pulmonology", "kidney": "Nephrology",
    "renal": "Nephrology", "anemia": "Hematology", "arthritis": "Rheumatology",
    "osteoporosis": "Rheumatology", "depress": "Psychiatry", "anxiety": "Psychiatry",
    "sinusitis": "Otolaryngology", "otitis": "Otolaryngology", "pain": "Pain Management",
    "obesity": "Bariatric Medicine", "fracture": "Orthopedics", "sprain": "Orthopedics",
}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def specialty_for(display: str) -> str:
    d = display.lower()
    return next((s for k, s in SPECIALTIES.items() if k in d), "Internal Medicine")


def fmt_date(iso: str, rng: random.Random) -> str:
    """Faxes rarely use ISO dates; vary the format so the extractor must normalize."""
    y, m, d = iso.split("-")
    return rng.choice([f"{m}/{d}/{y}", f"{int(m)}/{int(d)}/{y}", f"{m}-{d}-{y}"])


class Page:
    def __init__(self, rng: random.Random, hand_prob: float):
        self.rng = rng
        self.hand_prob = hand_prob
        self.img = Image.new("L", (W, H), 255)
        self.d = ImageDraw.Draw(self.img)
        self.y = MARGIN
        self.fields: list[dict] = []
        self.hand_name = rng.choice(HANDS)
        self.hand = font(self.hand_name, 40)
        self.f_label = font("CourierPrime-Bold.ttf", 30)
        self.f_text = font("CourierPrime-Regular.ttf", 30)
        self.f_head = font("OpenSans%5Bwdth,wght%5D.ttf", 46)
        self.f_small = font("CourierPrime-Regular.ttf", 24)

    def fit(self, s, name, size, max_w):
        """Shrink the font until `s` fits in max_w pixels (real forms cram long drug names)."""
        f = font(name, size)
        while size > 16 and self.d.textlength(s, font=f) > max_w:
            size -= 2
            f = font(name, size)
        return f

    def text(self, xy, s, f, fill=0):
        self.d.text(xy, s, font=f, fill=fill)
        return self.d.textbbox(xy, s, font=f)

    def letterhead(self, org: str, phone: str, title: str, date: str, pages="1/1"):
        self.text((MARGIN, 40), f"{date}  FROM: {phone}  {org[:38].upper()}  P.{pages}", self.f_small)
        self.y = 110
        self.text((MARGIN, self.y), org, self.f_head)
        self.y += 64
        self.text((MARGIN, self.y), f"Tel {phone}   Fax {phone[:-1]}9", self.f_small)
        self.y += 60
        self.d.line((MARGIN, self.y, W - MARGIN, self.y), fill=0, width=4)
        self.y += 30
        self.text((MARGIN, self.y), title, font("OpenSans%5Bwdth,wght%5D.ttf", 54))
        self.y += 100

    def section(self, title: str):
        self.y += 20
        self.d.rectangle((MARGIN, self.y, W - MARGIN, self.y + 44), fill=215)
        self.text((MARGIN + 12, self.y + 5), title.upper(), self.f_label)
        self.y += 66

    def field(self, key, label, value, fhir, x=MARGIN, width=None, newline=True, force_hand=None):
        """Printed label followed by a fill-in value, handwritten with probability hand_prob."""
        lb = self.text((x, self.y), f"{label}:", self.f_label)
        vx = lb[2] + 18
        hand = self.rng.random() < self.hand_prob if force_hand is None else force_hand
        line_end = x + width if width else W - MARGIN
        self.d.line((vx - 4, self.y + 38, line_end, self.y + 38), fill=120, width=2)
        max_w = line_end - vx - 24
        if hand:
            jitter = self.rng.randint(-8, 2)
            f = self.fit(value, self.hand_name, 40, max_w)
            bb = self.text((vx + self.rng.randint(0, 20), self.y - 14 + jitter + (40 - f.size) // 2), value, f, fill=25)
        else:
            f = self.fit(value, "CourierPrime-Regular.ttf", 30, max_w)
            bb = self.text((vx, self.y + (30 - f.size) // 2), value, f)
        self.fields.append({"key": key, "value": value, "bbox": list(bb), "handwritten": hand, "fhir": fhir})
        if newline:
            self.y += 62
        return bb

    def patient_block(self, p: PatientFacts):
        rng = self.rng
        self.section("Patient Information")
        self.field("patient.family", "Last Name", p.family, {"resource": "Patient", "path": "name.family"},
                   width=700, newline=False)
        self.field("patient.given", "First", p.given, {"resource": "Patient", "path": "name.given"},
                   x=MARGIN + 760)
        self.field("patient.birthDate", "DOB", fmt_date(p.birth_date, rng),
                   {"resource": "Patient", "path": "birthDate", "normalized": p.birth_date},
                   width=520, newline=False)
        sex = {"male": "M", "female": "F"}.get(p.gender, "U")
        self.field("patient.gender", "Sex", sex,
                   {"resource": "Patient", "path": "gender", "normalized": p.gender},
                   x=MARGIN + 580, width=250, newline=False)
        self.field("patient.mrn", "MRN", p.mrn, {"resource": "Patient", "path": "identifier[MR]"},
                   x=MARGIN + 890)
        self.field("patient.phone", "Phone", p.phone, {"resource": "Patient", "path": "telecom.phone"})
        self.field("patient.address", "Address", p.address, {"resource": "Patient", "path": "address"})

    def checkbox(self, x, label, checked):
        self.d.rectangle((x, self.y + 4, x + 30, self.y + 34), outline=0, width=3)
        if checked:
            o = self.rng.randint(-3, 3)
            self.d.line((x + 4 + o, self.y + 18, x + 13, self.y + 32), fill=20, width=6)
            self.d.line((x + 13, self.y + 32, x + 38 + o, self.y - 6), fill=20, width=6)
        self.text((x + 44, self.y), label, self.f_text)

    def signature(self, name: str):
        self.y = max(self.y + 40, H - 330)
        self.d.line((MARGIN, self.y + 60, MARGIN + 600, self.y + 60), fill=0, width=2)
        self.text((MARGIN + 10, self.y), name.split()[-1] if name else "", font(HANDS[0], 52), fill=20)
        self.text((MARGIN, self.y + 70), "Provider signature", self.f_small)
        self.text((MARGIN + 750, self.y + 70), "CONFIDENTIAL: intended only for the named recipient.", self.f_small)


def referral(p: PatientFacts, rng: random.Random, hand_prob: float, date: str):
    pg = Page(rng, hand_prob)
    pg.letterhead(p.organization, p.org_phone, "REFERRAL / CONSULTATION REQUEST", fmt_date(date, rng))
    pg.patient_block(p)

    cond = p.conditions[0]
    spec = specialty_for(cond["display"])
    pg.section("Referral")
    pg.field("referral.referring", "Referring Provider", p.practitioner,
             {"resource": "ServiceRequest", "path": "requester.display"})
    pg.field("referral.specialty", "Refer To", spec, {"resource": "ServiceRequest", "path": "code.text"})
    pg.field("referral.reason", "Reason / Dx", cond["display"],
             {"resource": "Condition", "path": "code", "system": cond["system"], "code": cond["code"],
              "fhir_id": cond["fhir_id"]})
    urgent = rng.random() < 0.3
    pg.text((MARGIN, pg.y), "Priority:", pg.f_label)
    pg.checkbox(MARGIN + 220, "Routine", not urgent)
    pg.checkbox(MARGIN + 520, "Urgent", urgent)
    pg.fields.append({"key": "referral.priority", "value": "urgent" if urgent else "routine",
                      "bbox": [MARGIN + 220, pg.y, MARGIN + 760, pg.y + 40], "handwritten": True,
                      "fhir": {"resource": "ServiceRequest", "path": "priority"}})
    pg.y += 70

    pg.section("Other Active Problems")
    for i, c in enumerate(p.conditions[1:4]):
        pg.field(f"conditions[{i}]", f"{i + 1}", c["display"],
                 {"resource": "Condition", "path": "code", "system": c["system"], "code": c["code"],
                  "fhir_id": c["fhir_id"]})

    pg.section("Current Medications")
    for i, m in enumerate(p.medications[:4]):
        pg.field(f"medications[{i}]", f"{i + 1}", m["display"],
                 {"resource": "MedicationRequest", "path": "medicationCodeableConcept", "system": m["system"],
                  "code": m["code"], "fhir_id": m["fhir_id"]})
    if not p.medications:
        pg.text((MARGIN, pg.y), "None reported", pg.f_text)
        pg.y += 62
    pg.signature(p.practitioner)
    return pg.img, pg.fields


def lab_result(p: PatientFacts, rng: random.Random, hand_prob: float, date: str):
    pg = Page(rng, hand_prob * 0.3)  # lab reports are mostly printed
    pg.letterhead(p.organization, p.org_phone, "LABORATORY REPORT", fmt_date(date, rng))
    pg.patient_block(p)

    latest = p.labs[0]["date"]
    panel = [l for l in p.labs if l["date"] == latest][:10]
    pg.section("Results")
    pg.field("lab.collected", "Collected", fmt_date(latest, rng),
             {"resource": "Observation", "path": "effectiveDateTime", "normalized": latest}, force_hand=False)
    pg.field("lab.ordering", "Ordering Provider", p.practitioner,
             {"resource": "ServiceRequest", "path": "requester.display"}, force_hand=False)
    pg.y += 10
    cols = (MARGIN, MARGIN + 940, MARGIN + 1180)
    for x, h in zip(cols, ("TEST", "RESULT", "UNITS")):
        pg.text((x, pg.y), h, pg.f_label)
    pg.y += 46
    pg.d.line((MARGIN, pg.y, W - MARGIN, pg.y), fill=0, width=2)
    pg.y += 14
    for i, l in enumerate(panel):
        nb = pg.text((cols[0], pg.y), l["display"],
                     pg.fit(l["display"], "CourierPrime-Regular.ttf", 24, cols[1] - cols[0] - 30))
        vb = pg.text((cols[1], pg.y), str(l["value"]), pg.f_text)
        ub = pg.text((cols[2], pg.y), l["unit"], pg.f_small)
        base = {"resource": "Observation", "system": l["system"], "code": l["code"], "fhir_id": l["fhir_id"]}
        pg.fields += [
            {"key": f"labs[{i}].test", "value": l["display"], "bbox": list(nb), "handwritten": False,
             "fhir": {**base, "path": "code"}},
            {"key": f"labs[{i}].value", "value": str(l["value"]), "bbox": list(vb), "handwritten": False,
             "fhir": {**base, "path": "valueQuantity.value"}},
            {"key": f"labs[{i}].unit", "value": l["unit"], "bbox": list(ub), "handwritten": False,
             "fhir": {**base, "path": "valueQuantity.unit"}},
        ]
        pg.y += 50
    pg.signature(p.practitioner)
    return pg.img, pg.fields


def prior_auth(p: PatientFacts, rng: random.Random, hand_prob: float, date: str):
    pg = Page(rng, hand_prob)
    pg.letterhead(p.organization, p.org_phone, "PRIOR AUTHORIZATION REQUEST", fmt_date(date, rng))
    pg.patient_block(p)

    member = f"{rng.choice('ABCHMUW')}{rng.choice('ABCHMUW')}{rng.randint(10000000, 99999999)}"
    pg.section("Insurance")
    pg.field("coverage.memberId", "Member ID", member, {"resource": "Coverage", "path": "subscriberId"},
             width=800, newline=False)
    pg.field("coverage.payer", "Plan", rng.choice(["Medicare", "Medicaid", "Blue Cross", "Aetna", "UnitedHealth"]),
             {"resource": "Coverage", "path": "payor.display"}, x=MARGIN + 860)

    med = p.medications[0]
    dx = p.conditions[0]
    pg.section("Medication Requested")
    pg.field("rx.medication", "Drug / Strength", med["display"],
             {"resource": "MedicationRequest", "path": "medicationCodeableConcept", "system": med["system"],
              "code": med["code"], "fhir_id": med["fhir_id"]})
    if med["sig"]:
        pg.field("rx.sig", "Directions", med["sig"], {"resource": "MedicationRequest", "path": "dosageInstruction"})
    qty = str(rng.choice([30, 60, 90]))
    pg.field("rx.quantity", "Qty", qty, {"resource": "MedicationRequest", "path": "dispenseRequest.quantity"},
             width=400, newline=False)
    pg.field("rx.refills", "Refills", str(rng.randint(0, 5)),
             {"resource": "MedicationRequest", "path": "dispenseRequest.numberOfRepeatsAllowed"},
             x=MARGIN + 480, width=400)
    pg.field("rx.diagnosis", "Diagnosis", dx["display"],
             {"resource": "Condition", "path": "code", "system": dx["system"], "code": dx["code"],
              "fhir_id": dx["fhir_id"]})

    pg.section("Prescriber")
    pg.field("rx.prescriber", "Name", p.practitioner, {"resource": "MedicationRequest", "path": "requester.display"})
    npi = str(rng.randint(1000000000, 1999999999))
    pg.field("rx.npi", "NPI", npi, {"resource": "Practitioner", "path": "identifier[NPI]"})

    pg.section("Clinical Justification")
    note = f"Pt with {dx['display'].lower()} since {dx['onset'][:4] or 'unknown'}. Requesting coverage."
    note_font = pg.fit(note, pg.hand_name, 40, W - 2 * MARGIN) if rng.random() < hand_prob \
        else pg.fit(note, "CourierPrime-Regular.ttf", 30, W - 2 * MARGIN)
    pg.text((MARGIN, pg.y), note, note_font, fill=25)
    pg.y += 70
    pg.signature(p.practitioner)
    return pg.img, pg.fields


DOC_TYPES = {"referral": referral, "lab": lab_result, "prior_auth": prior_auth}


def eligible(doc_type: str, p: PatientFacts) -> bool:
    if doc_type == "lab":
        return bool(p.labs)
    if doc_type == "prior_auth":
        return bool(p.medications and p.conditions)
    return bool(p.conditions)
