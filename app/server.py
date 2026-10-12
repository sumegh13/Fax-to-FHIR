"""Fax intake web app: upload a fax, review uncertain fields, commit to HAPI.

    .venv/bin/uvicorn app.server:app --reload --port 8000

Extraction is pluggable: EXTRACTOR=claude reads any fax with Claude Haiku 5.5; EXTRACTOR=simulated reads
the ground truth of a generated fax (matched by filename) and corrupts it, so
only faxes from data/faxes/ can be processed.
"""
import json
import os
import shutil
import time
import uuid
from collections import deque
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
FAX_DPI = 200  # fax "fine" mode; PDFs are rendered at this resolution
# A shared demo link spends the host's API credit; cap model calls per rolling hour.
MAX_FAXES_PER_HOUR = int(os.environ.get("MAX_FAXES_PER_HOUR", "60"))
_recent_calls: deque[float] = deque()


def _take_quota():
    now = time.time()
    while _recent_calls and now - _recent_calls[0] > 3600:
        _recent_calls.popleft()
    if len(_recent_calls) >= MAX_FAXES_PER_HOUR:
        wait = int(3600 - (now - _recent_calls[0])) // 60 + 1 if _recent_calls else 60
        raise HTTPException(429, f"This demo has read its limit of {MAX_FAXES_PER_HOUR} faxes this hour. "
                                 f"Try again in about {wait} minutes.")
    _recent_calls.append(now)

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
        _take_quota()
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
    bundle, issues, patient_ref, reconciled = prepare(extraction)
    on_chart = {r["key"]: r["existing"] for r in reconciled}
    issue_by_key = {}
    for i in issues:
        issue_by_key.setdefault(i["key"], []).append(i["reason"])
    fields = []
    for f in extraction["fields"]:
        reasons = list(issue_by_key.get(f["key"], []))
        if f.get("confidence", 1) < THRESHOLD:
            reasons.insert(0, f"Low confidence ({f['confidence']:.0%})")
        fields.append({**f, "reasons": reasons, "needs_review": bool(reasons), "on_chart": on_chart.get(f["key"])})
    known = {f["key"] for f in extraction["fields"]}
    general = [i for i in issues if i["key"] not in known]
    return {"job_id": job_id, "fax_id": extraction["fax_id"], "doc_type": extraction["doc_type"],
            "extractor": extraction.get("extractor"), "threshold": THRESHOLD, "patient_ref": patient_ref,
            "fields": fields, "issues": general, "resource_count": len(bundle["entry"]),
            "on_chart_count": len(reconciled),
            "resource_types": sorted({e["resource"]["resourceType"] for e in bundle["entry"]})}


@app.post("/api/faxes")
async def upload(file: UploadFile):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_TYPES | {".pdf"}:
        raise HTTPException(415, "Upload a fax as PDF, PNG, JPG or TIFF.")
    job_id = uuid.uuid4().hex[:12]
    d = INBOX / job_id
    d.mkdir(parents=True)
    raw = d / f"original{ext}"
    with raw.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    pages = 1
    if ext == ".pdf":
        import pypdfium2
        try:
            pdf = pypdfium2.PdfDocument(raw)
        except pypdfium2.PdfiumError:
            raise HTTPException(422, "That PDF could not be opened. Check that it is not password-protected.")
        pages = len(pdf)
        img = pdf[0].render(scale=FAX_DPI / 72).to_pil()
    else:
        img = Image.open(raw)
    img.convert("L").save(d / "fax.png")
    extraction = extract(d / "fax.png", file.filename)
    (d / "extraction.json").write_text(json.dumps(extraction))
    view = review_view(job_id, extraction)
    if pages > 1:
        view["issues"].append({"key": "fax", "value": pages,
                               "reason": f"This fax has {pages} pages; only page 1 was read."})
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
    bundle, issues, patient_ref, _ = prepare(extraction)
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


@app.get("/api/samples")
def samples():
    """A few generated faxes for visitors to try: the noisiest of each document type."""
    manifest = FAXES / "manifest.jsonl"
    if not manifest.exists():
        return []
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    order = {"ugly": 0, "mild": 1, "clean": 2}
    picks = []
    for doc in ("referral", "lab", "prior_auth"):
        of_type = sorted((r for r in rows if r["doc_type"] == doc),
                         key=lambda r: (order[r["level"]], -r["n_handwritten"], r["fax_id"]))
        picks += of_type[:2]
    return [{"file": f"{r['fax_id']}.png", "doc_type": r["doc_type"], "level": r["level"]} for r in picks]


@app.get("/api/samples/{name}")
def sample(name: str):
    """Serve a generated fax so the demo can open one by URL (?sample=<fax file>)."""
    path = FAXES / Path(name).name
    if path.suffix != ".png" or not path.exists():
        raise HTTPException(404, "No such sample fax")
    return FileResponse(path, media_type="image/png", filename=path.name)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
