"""Read a fax image with Claude and return fields with confidence and location.

Output has the same shape as a truth file's fields, so the mapper, the review
screen and the evaluator all consume it unchanged:
    {fax_id, doc_type, extractor, fields: [{key, value, confidence, bbox}], usage}
"""
import base64
import io
import json
from pathlib import Path
from typing import Literal

import anthropic
from PIL import Image
from pydantic import BaseModel, Field

MODEL = "claude-haiku-5-5"
PRICE_PER_MTOK = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}
MAX_EDGE = 1568  # the API downsizes larger images anyway; resizing ourselves lets us map boxes back

SYSTEM = """You read faxed medical documents (referrals, lab reports, prior authorization requests) for a hospital intake desk. Your output is written into the patient's electronic health record after a person reviews any field you are unsure of, so an honest confidence score matters as much as the value.

Transcribe each field exactly as printed or handwritten on the fax. Do not correct spelling, expand abbreviations, reformat dates or look up codes; later steps normalize values. If a field is blank or not on the page, leave it out rather than guessing.

Use these keys:
- Patient (all document types): patient.family, patient.given, patient.birthDate, patient.gender (as written, e.g. "M"), patient.mrn, patient.phone, patient.address (one line, as written)
- Referral: referral.referring, referral.specialty (the "Refer to" value), referral.reason, referral.priority ("routine" or "urgent", from whichever box is checked), conditions[0], conditions[1], ... (the other active problems, in page order), medications[0], medications[1], ... (current medications, in page order)
- Lab report: lab.collected, lab.ordering, then for each row of the results table labs[i].test, labs[i].value, labs[i].unit (i counts rows from 0, top to bottom)
- Prior authorization: coverage.memberId, coverage.payer, rx.medication, rx.sig, rx.quantity, rx.refills, rx.diagnosis, rx.prescriber, rx.npi

Confidence is your probability that the value is exactly right, character for character:
- 0.95-1.0: clean printed text you can read without doubt
- 0.8-0.95: readable, but noise, a stamp or skew touches it
- 0.5-0.8: handwriting or degradation leaves one or more characters uncertain
- below 0.5: you are largely guessing
Handwritten digits (dates, phone numbers, IDs) deserve particular caution: one misread digit makes the whole value wrong.

bbox is the box around the value (not its label) in pixels of the image you were given: [left, top, right, bottom]."""


class FaxField(BaseModel):
    key: str
    value: str
    confidence: float = Field(description="Probability the value is exactly right, 0 to 1")
    bbox: list[int] = Field(description="[left, top, right, bottom] in image pixels")


class FaxReading(BaseModel):
    doc_type: Literal["referral", "lab", "prior_auth", "other"]
    fields: list[FaxField]


def _encode(path: Path) -> tuple[str, str, float]:
    """PNG bytes (base64) of the fax, downscaled to MAX_EDGE, plus the scale back to original pixels."""
    img = Image.open(path).convert("L")
    scale = min(1.0, MAX_EDGE / max(img.size))
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode(), "image/png", scale


def cost(usage) -> float:
    return (usage.input_tokens * PRICE_PER_MTOK["input"]
            + usage.output_tokens * PRICE_PER_MTOK["output"]
            + (usage.cache_read_input_tokens or 0) * PRICE_PER_MTOK["cache_read"]
            + (usage.cache_creation_input_tokens or 0) * PRICE_PER_MTOK["cache_write"]) / 1e6


class RefusalError(RuntimeError):
    pass


def extract(path: Path, fax_id: str | None = None, client: anthropic.Anthropic | None = None) -> dict:
    client = client or anthropic.Anthropic()
    data, media_type, scale = _encode(path)
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": "Read every field on this fax."},
        ]}],
        output_format=FaxReading,
    )
    if response.stop_reason == "refusal":
        raise RefusalError(getattr(response.stop_details, "explanation", None) or "model declined this fax")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("extraction was cut off at max_tokens")
    reading: FaxReading = response.parsed_output

    fields = []
    for f in reading.fields:
        box = [round(v / scale) for v in f.bbox] if len(f.bbox) == 4 else None
        fields.append({"key": f.key, "value": f.value, "confidence": max(0.0, min(1.0, f.confidence)),
                       "bbox": box})
    u = response.usage
    return {"fax_id": fax_id or Path(path).name.split(".")[0], "doc_type": reading.doc_type,
            "extractor": MODEL, "fields": fields,
            "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                      "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                      "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
                      "cost_usd": round(cost(u), 6), "request_id": response._request_id}}


if __name__ == "__main__":
    import sys
    print(json.dumps(extract(Path(sys.argv[1])), indent=2))
