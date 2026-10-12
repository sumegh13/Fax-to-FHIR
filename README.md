# Fax to FHIR

Healthcare still runs on fax. Referrals, lab results and prior-authorization forms arrive as skewed, stamped,
partly handwritten scans, and staff retype them into the EHR by hand.

This project reads incoming faxes with Claude, turns them into structured FHIR R4 records, and writes them to a
real FHIR server (HAPI FHIR). Fields the model is unsure of go to a review screen first; nothing low-confidence
reaches the record without a person confirming it.

Because the test faxes are generated from synthetic patients, every field has ground truth, so accuracy is
measured per field rather than eyeballed.

## Results

Claude Haiku 5.5 on 133 synthetic faxes (47 referrals, 53 lab reports, 33 prior authorizations; 3,136 fields,
499 of them handwritten). Total extraction cost: **$0.18**, about $0.0013 per fax.

| | Fields | Accuracy |
|---|---:|---:|
| **All fields** | 3,136 | **97.3%** |
| Printed | 2,637 | 99.3% |
| Handwritten | 499 | 86.4% |
| Lab reports | 1,926 | 99.2% |
| Referrals | 705 | 94.5% |
| Prior authorizations | 505 | 93.9% |
| Clean / mild / ugly scans | 888 / 1,026 / 1,222 | 98.0% / 98.7% / 95.5% |

A field counts as correct when its normalized value matches (dates in any format, `M` = male, case and
punctuation ignored); exact-string accuracy is 96.8%.

**The review gate.** At a confidence threshold of 0.8, 8.6% of fields go to a reviewer and those contain 89% of
all errors. Of the fields committed without review, 0.3% are wrong. Raising the threshold to 0.9 catches 96% of
errors at the cost of reviewing 22% of fields.

**Confidence is meaningful.** Fields Haiku scores 0.9 or higher are right 99.9% of the time; fields it scores
0.5 to 0.6 are right 38% of the time.

**Location.** Each field comes with a bounding box. In 98.4% of cases the crop shown to the reviewer contains the
field (median IoU with the true box: 0.77).

Where it struggles: handwritten characters that look alike in the handwriting font (`F` read as `7`, `D` as `0`
in record numbers), and long drug names, which it sometimes paraphrases (`UNT/ML` as `UNITS/ML`).

The full report, including a threshold sweep and calibration table, is generated at
`data/reports/haiku/report.md`.

### An experiment that did not help

Re-reading low-confidence fields from zoomed full-resolution crops, without showing the model its first answer,
cut the review load from 8.6% to 3.6% but let more errors through (errors caught fell from 89% to 73%). On these
faxes the first pass already sees the page at near-native resolution, the second read was less accurate than the
first, and when both reads agreed they were still wrong 7% of the time. It is kept in `extractor/verify.py` but
not used.

## How it works

```
Synthea patients ──> faxgen ──> noisy fax PNG + truth.json
                                      │
                    extractor (Claude Haiku 5.5): fields + confidence + bbox
                                      │
          fhirmap: normalize, code (SNOMED/RxNorm/LOINC), match patient,
                   check the chart for duplicates, validate
                                      │
              review screen (fields < 0.8 confidence or with issues)
                                      │
                        HAPI FHIR transaction (undoable)
```

- **faxgen** renders three document types from Synthea bundles: referrals, lab reports, prior-auth requests. It
  adds stamps, skew up to 5 degrees, blur, low vertical resolution, 1-bit thresholding, speckle and dropout lines,
  and fills about a third of the fill-in fields in handwriting fonts. Every field's value and bounding box is
  recorded, with boxes transformed through the rotation.
- **extractor** sends the page to Claude Haiku 5.5 with a structured-output schema and asks for exact
  transcription (no normalization), a calibrated confidence and a box per field.
- **fhirmap** builds a FHIR transaction: Patient (or a match to an existing one by name and date of birth),
  DocumentReference to the source fax, Conditions, MedicationRequests, ServiceRequest, Observations with a
  DiagnosticReport, Coverage and Practitioner. Free text is coded by matching against a SNOMED/RxNorm/LOINC
  dictionary; weak matches become review issues rather than wrong codes. For a matched patient it checks the chart
  first: items already on file are linked instead of copied, and a lab value that disagrees with the chart for the
  same test and day is flagged.
- **evaluate** scores predictions against truth per field, aligning list fields by content rather than position,
  and reports the review-gate trade-off and calibration.
- **app** is the review screen: drop a fax (PDF or image), see uncertain fields highlighted on the page with a crop of each,
  correct or confirm them, commit, and undo if needed. The server refuses to commit a low-confidence field nobody
  confirmed.

## Try it in GitHub Codespaces

1. On the repository page choose Code, Codespaces, Create codespace on main. A 4-core machine is requested.
2. The first start takes about 15 minutes: it installs dependencies, generates 50 synthetic patients and their
   faxes, and loads the FHIR server. Later starts take about a minute.
3. The review screen opens on port 8000. To share it, open the Ports panel, right-click port 8000 and set
   Port Visibility to Public, then copy the forwarded address.

The hosted app has no API key of its own. Each visitor adds their own Claude API key under "API key" on the page;
it is kept in their browser, sent with each upload, used for that one request and never stored on the server.
Without a key, visitors can still open the sample faxes, which are read by the simulated extractor.

The link works only while the codespace is running; it stops after the idle timeout (30 minutes by default,
adjustable up to 4 hours in your Codespaces settings).

## Running it locally

Requirements: Docker, Python 3.11.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # add ANTHROPIC_API_KEY

# FHIR server and synthetic patients
docker compose up -d
./scripts/generate_patients.sh 50
./scripts/load_hapi.sh

# Faxes with ground truth
.venv/bin/python -m faxgen                    # add --debug-boxes to draw truth boxes

# Extract and score
.venv/bin/python -m extractor                 # writes data/predictions/haiku/
.venv/bin/python -m evaluate report haiku     # writes data/reports/haiku/

# Review screen
.venv/bin/uvicorn app.server:app --port 8000
# open http://localhost:8000/?sample=referral-hammes-2de4dd0d.png
```

Without an API key, `python -m evaluate simulate` produces a simulated extractor (ground truth with OCR-like
corruption) and the review screen uses it, so the pipeline can be exercised offline.

Tests (normalizers, scoring, FHIR mapping; offline, but the mapper tests need the Synthea data generated above):

```sh
.venv/bin/python -m pytest
```

Other commands:

```sh
.venv/bin/python -m fhirmap commit <extraction.json> [--dry-run] [--force]
.venv/bin/python -m fhirmap undo <fax_id>
.venv/bin/python -m fhirmap roundtrip         # commit every truth file, read back, undo
```

## Limitations

- The test faxes come from three fixed templates. Real faxes vary far more in layout, and these numbers will be
  optimistic for them.
- The terminology dictionary is built from the Synthea bundles, so truth text always has an exact code. A real
  deployment needs a terminology server.
- Test faxes are drawn from data already in each patient's chart, which makes the duplicate and lab-value checks
  fire more often than they would on genuinely new documents.
- Multi-page PDF faxes are accepted, but only the first page is read; the review screen says so.
