"""Second, independent read of uncertain fields from zoomed crops.

The first pass sees the whole page downscaled to 1568 px, so small handwriting
loses detail. Here every field below the threshold is cropped from the
full-resolution fax, enlarged, and sent in one request. The model is not told
what it read the first time, so agreement between the two reads is evidence:

- reads agree   -> keep the value, confidence rises to at least AGREE_FLOOR
- reads differ  -> keep the more confident value, confidence drops, field stays in review
"""
import base64
import io
from pathlib import Path

import anthropic
from PIL import Image
from pydantic import BaseModel, Field

from evaluate.score import family, normalized_equal

from .claude import MODEL, RefusalError, cost

THRESHOLD = 0.8
AGREE_FLOOR = 0.9
PAD_X, PAD_Y = 160, 40  # generous: include the printed label and survive slightly-off boxes
ZOOM = 2

SYSTEM = """You re-read single fields from faxed medical documents. Each image is a zoomed crop around one field; the field name tells you which value to transcribe. The crop may include the printed label and parts of neighbouring fields: transcribe only the value for the named field.

Transcribe exactly as written, character for character. Do not correct spelling, reformat dates or expand abbreviations. Handwritten letters and digits are easy to confuse (F/7, 1/l/I, 0/O/D, 5/S, 8/B): look closely and lower your confidence when a character could be either.

Confidence is your probability that the transcription is exactly right."""


class FieldRead(BaseModel):
    index: int = Field(description="The crop number given in the prompt")
    value: str
    confidence: float


class Reads(BaseModel):
    reads: list[FieldRead]


def _crop(img: Image.Image, bbox: list[int]) -> str:
    x0, y0, x1, y1 = bbox
    box = (max(0, x0 - PAD_X), max(0, y0 - PAD_Y), min(img.width, x1 + PAD_X // 2), min(img.height, y1 + PAD_Y))
    c = img.crop(box)
    c = c.resize((c.width * ZOOM, c.height * ZOOM), Image.LANCZOS)
    buf = io.BytesIO()
    c.save(buf, format="PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode()


def verify(image_path: Path, prediction: dict, client: anthropic.Anthropic | None = None) -> dict:
    """Return a new prediction with uncertain fields re-read; adds usage of the extra call."""
    targets = [f for f in prediction["fields"] if f["confidence"] < THRESHOLD and f.get("bbox")]
    if not targets:
        return {**prediction, "verify": {"fields": 0}}
    client = client or anthropic.Anthropic()
    img = Image.open(image_path).convert("L")

    content = []
    for i, f in enumerate(targets):
        content.append({"type": "text", "text": f"Crop {i}: field `{f['key']}`"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": _crop(img, f["bbox"])}})
    content.append({"type": "text", "text": f"Transcribe the named field in each of the {len(targets)} crops."})

    response = client.messages.parse(
        model=MODEL,
        max_tokens=8000,
        output_config={"effort": "medium"},
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=Reads,
    )
    if response.stop_reason == "refusal":
        raise RefusalError("model declined the verification crops")
    second = {r.index: r for r in response.parsed_output.reads}

    out_fields, agreed = [], 0
    by_key = {id(f): i for i, f in enumerate(targets)}
    for f in prediction["fields"]:
        if id(f) not in by_key or by_key[id(f)] not in second:
            out_fields.append(f)
            continue
        r = second[by_key[id(f)]]
        c2 = max(0.0, min(1.0, r.confidence))
        reads = [{"value": f["value"], "confidence": f["confidence"]}, {"value": r.value, "confidence": c2}]
        if normalized_equal(family(f["key"]), f["value"], r.value):
            agreed += 1
            new = {**f, "confidence": round(max(AGREE_FLOOR, f["confidence"], c2), 3)}
        else:
            best = max(reads, key=lambda x: x["confidence"])
            new = {**f, "value": best["value"], "confidence": round(min(f["confidence"], c2) * 0.8, 3)}
        out_fields.append({**new, "reads": reads})

    u = prediction.get("usage", {})
    extra = response.usage
    usage = {**u, "verify_input_tokens": extra.input_tokens, "verify_output_tokens": extra.output_tokens,
             "cost_usd": round(u.get("cost_usd", 0) + cost(extra), 6)}
    return {**prediction, "fields": out_fields, "usage": usage,
            "verify": {"fields": len(targets), "agreed": agreed}}
