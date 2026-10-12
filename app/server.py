"""Fax intake web app: upload a fax, review uncertain fields, commit to HAPI.

    .venv/bin/uvicorn app.server:app --reload --port 8000

Extraction is pluggable. Until a model is wired in, EXTRACTOR=simulated reads
the ground truth of a generated fax (matched by filename) and corrupts it, so
only faxes from data/faxes/ can be processed.
"""
import json
import os
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv
import anthropic
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from evaluate.simulate import simulate_fax
from fhirmap import client
from fhirmap.__main__ import prepare

load_dotenv()  # .env at the repo root; real environment variables win

ROOT = Path(__file__).resolve().parent.parent
FAXES = ROOT / "data" / "faxes"
INBOX = ROOT / "data" / "inbox"
# "claude" reads any fax with Claude Haiku 5.5; "simulated" needs no API key but only knows generated faxes.
EXTRACTOR = os.environ.get("EXTRACTOR", "claude" if os.environ.get("ANTHROPIC_API_KEY") else "simulated")
THRESHOLD = float(os.environ.get("REVIEW_THRESHOLD", "0.8"))
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

app = FastAPI(title="Fax to FHIR")


def extract(image: Path, original_name: str) -> dict:
    if EXTRACTOR == "simulated":
        stem = Path(original_name).name.split(".")[0]
        truth = FAXES / f"{stem}.truth.json"
        if not truth.exists():
            raise HTTPException(422, f"The simulated extractor only reads generated faxes; "
                                     f"no ground truth for '{stem}'. Drop a file from data/faxes/.")
        return simulate_fax(json.loads(truth.read_text()))
    if EXTRACTOR == "claude":
        from extractor.claude import RefusalError, extract as claude_extract
        try:
            return claude_extract(image, Path(original_name).name.split(".")[0])
        except RefusalError as e:
            raise HTTPException(422, f"The model declined to read this fax: {e}")
        except anthropic.APIError as e:
            raise HTTPException(502, f"Extraction failed: {e}")
    raise HTTPException(500, f"Extractor '{EXTRACTOR}' is not configured.")


def job_dir(job_id: str) -> Path:
    d = INBOX / job_id
    if not d.is_dir() or "/" in job_id:
        raise HTTPException(404, "Unknown fax")
    return d


def review_view(job_id: str, extraction: dict) -> dict:
    """Build the bundle, then mark which fields need a human."""
    bundle, issues, patient_ref = prepare(extraction)
    issue_by_key = {}
    for i in issues:
        issue_by_key.setdefault(i["key"], []).append(i["reason"])
    fields = []
    for f in extraction["fields"]:
        reasons = list(issue_by_key.get(f["key"], []))
        if f.get("confidence", 1) < THRESHOLD:
            reasons.insert(0, f"Low confidence ({f['confidence']:.0%})")
        fields.append({**f, "reasons": reasons, "needs_review": bool(reasons)})
    known = {f["key"] for f in extraction["fields"]}
    general = [i for i in issues if i["key"] not in known]
    return {"job_id": job_id, "fax_id": extraction["fax_id"], "doc_type": extraction["doc_type"],
            "extractor": extraction.get("extractor"), "threshold": THRESHOLD, "patient_ref": patient_ref,
            "fields": fields, "issues": general, "resource_count": len(bundle["entry"]),
            "resource_types": sorted({e["resource"]["resourceType"] for e in bundle["entry"]})}


@app.post("/api/faxes")
async def upload(file: UploadFile):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_TYPES:
        raise HTTPException(415, "Upload a fax image (PNG, JPG or TIFF).")
    job_id = uuid.uuid4().hex[:12]
    d = INBOX / job_id
    d.mkdir(parents=True)
    raw = d / f"original{ext}"
    with raw.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    img = Image.open(raw)
    img.convert("L").save(d / "fax.png")
    extraction = extract(raw, file.filename)
    (d / "extraction.json").write_text(json.dumps(extraction))
    view = review_view(job_id, extraction)
    view["image"] = {"url": f"/api/faxes/{job_id}/image", "width": img.width, "height": img.height}
    return view


@app.get("/api/faxes/{job_id}/image")
def image(job_id: str):
    return FileResponse(job_dir(job_id) / "fax.png", media_type="image/png")


class Edits(BaseModel):
    values: dict[str, str] = {}       # key -> corrected value
    removed: list[str] = []           # keys the reviewer rejected outright
    force: bool = False


def apply_edits(job_id: str, edits: Edits) -> dict:
    extraction = json.loads((job_dir(job_id) / "extraction.json").read_text())
    fields = []
    for f in extraction["fields"]:
        if f["key"] in edits.removed:
            continue
        if f["key"] in edits.values:
            f = {**f, "value": edits.values[f["key"]], "confidence": 1.0, "reviewed": True}
        fields.append(f)
    return {**extraction, "fields": fields}


@app.post("/api/faxes/{job_id}/check")
def check(job_id: str, edits: Edits):
    """Re-run mapping and validation on the reviewer's corrections without committing."""
    return review_view(job_id, apply_edits(job_id, edits))


@app.post("/api/faxes/{job_id}/commit")
def commit(job_id: str, edits: Edits):
    extraction = apply_edits(job_id, edits)
    # Nothing below the threshold reaches the EHR unless a person confirmed it.
    unreviewed = [f["key"] for f in extraction["fields"] if f.get("confidence", 1) < THRESHOLD]
    if unreviewed and not edits.force:
        raise HTTPException(409, {"message": "Some low-confidence fields have not been reviewed.",
                                  "issues": [{"key": k, "reason": "not reviewed"} for k in unreviewed]})
    bundle, issues, patient_ref = prepare(extraction)
    if issues and not edits.force:
        raise HTTPException(409, {"message": "Fix these before committing.", "issues": issues})
    try:
        log = client.commit(bundle, job_id)
    except client.FhirError as e:
        raise HTTPException(502, f"FHIR server rejected the bundle: {e}")
    (job_dir(job_id) / "committed.json").write_text(json.dumps(log))
    patient = patient_ref or next(r["ref"] for r in log["resources"] if r["ref"].startswith("Patient/"))
    return {"patient_ref": patient, "matched_existing": bool(patient_ref), "resources": log["resources"],
            "fhir_base": client.BASE}


@app.post("/api/faxes/{job_id}/undo")
def undo(job_id: str):
    deleted = client.undo(job_id)
    (job_dir(job_id) / "committed.json").unlink(missing_ok=True)
    return {"deleted": deleted}


@app.get("/api/patient/{pid}")
def patient_summary(pid: str):
    """Small record view for the confirmation screen, proxied so the browser needs no CORS."""
    p = client.request("GET", f"Patient/{pid}")
    everything = client.request("GET", f"Patient/{pid}/$everything?_count=500")
    counts = {}
    for e in everything.get("entry", []):
        t = e["resource"]["resourceType"]
        counts[t] = counts.get(t, 0) + 1
    return {"patient": p, "counts": counts}


@app.get("/api/samples/{name}")
def sample(name: str):
    """Serve a generated fax so the demo can open one by URL (?sample=<fax file>)."""
    path = FAXES / Path(name).name
    if path.suffix != ".png" or not path.exists():
        raise HTTPException(404, "No such sample fax")
    return FileResponse(path, media_type="image/png", filename=path.name)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
