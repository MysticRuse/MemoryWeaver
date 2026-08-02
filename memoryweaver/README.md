# MemoryWeaver Cleaner 🧹📸

**Clean up, organise and edit a messy photo and video library — locally, on your own machine.**

Phone camera rolls fill with screenshots, receipts, burst duplicates, credential
screengrabs and blurry misfires. This app sorts that pile: it classifies every
photo, isolates anything sensitive into a passcode-locked vault, finds
near-duplicates, shows you what deleting them would reclaim, and writes
deletions back to the folder they came from. It also carries a full photo and
video editor for the images worth keeping.

Everything runs against a local folder. Media is encrypted at rest.

---

## What it does

**Clean**
- Classifies the library into categories (people, scenery, food, pets, junk,
  documents, screenshots) using on-device Apple Vision OCR first, escalating to
  Gemini only when local detection is inconclusive
- Detects credential screenshots and routes them to a **Protected Notes Vault**
- Passcode-locks individual photos into a **Private Vault**
- Finds near-duplicates with a local 8×8 average hash — no API cost
- Reports how much space staged deletions would reclaim, then purges
- Two-way folder sync: scan a device folder in, write deletions back out

**Edit — photos**
- Detail panel: full EXIF (device, lens, ISO, aperture, capture date), GPS on a
  Leaflet map, and an AI photographer's critique
- Magic Auto-Enhance with model-tuned brightness/contrast/saturation/warmth, and
  one-click switching between the original and enhanced version
- Transformations: black & white, coloured-pencil sketch, meme captions, kids
  stickers, photogenic correction
- Voice notes and transcription attached to a photo
- Rename, re-caption, move between categories

**Edit — video**
- Watermark and logo removal (classical frame-variance detection first, Gemini
  Vision only on escalation)
- Frame extraction, and adding an extracted frame back to the library
- H.264 compression to reclaim space
- Live Photo → video conversion

---

## Quick start

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/), and a
[Google AI Studio API key](https://aistudio.google.com/apikey) for the AI
features (classification, enhancement and critique; the free tier is fine).
Cleaning, dedup and compression work without a key.

```bash
cd memoryweaver
uv sync
cp .env.example .env      # add GEMINI_API_KEY
uv run uvicorn app.fast_api_app:app --port 8000
```

Open **http://localhost:8000**.

Point it at a folder via **Run Magic Scanner**, or drag files in directly.

---

## Configuration

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Required for classification, enhancement, critique, transformations |
| `MW_ENCRYPTION_KEY` | Master key for media encryption at rest. Auto-generated to `local_storage/.encryption_key` (mode 0600) if unset — **back that file up** |
| `MW_ADMIN_TOKEN` | When set, mutating and billable routes require an `X-MW-Token` header. Unset is a no-op, which suits a local single-user install |
| `MW_BROWSE_ROOTS` | Colon-separated directories the folder browser may read. Defaults to your home directory |
| `MW_MAX_AI_REQUESTS_PER_MIN` / `MW_MAX_AI_TOKENS_PER_HOUR` | Spend cap per library (defaults 60 / 150,000) |
| `MW_PIPELINE_MODEL`, `MW_IMAGE_MODEL`, `MW_TTS_MODEL` | Override model IDs without a code change |

---

## Layout

```
app/
  fast_api_app.py     app construction, media serving, uploads, libraries
  deps.py             admin token + AI spend cap dependencies
  schemas.py          request bodies
  routers/
    cleaner.py        vault, classification, dedup, storage, sync, purge
    photo.py          metadata, enhance, transformations, rename
    video.py          frames, watermark removal, compression, description
  services/
    media_store.py    storage, encryption and imaging helpers
    classification.py auto-classification of newly ingested photos
    transforms.py     the five photo transformation builders
    video_ops.py      watermark detection and the ffmpeg delogo pipeline
  app_utils/
    crypto.py         AES-256-GCM encryption at rest (see its module docstring)
    paths.py          path confinement for every filesystem-facing route
    errors.py         typed errors -> correct HTTP status codes
    genai_client.py   one Gemini client, model IDs by role
pipeline/
  local_cleaner.py    EXIF extraction
  cost_tracker.py     per-feature cost and escalation accounting
  prompt_safety.py    input sanitising + the AI spend circuit breaker
frontend/
  cleaner.html        the application
```

---

## Cost design

The expensive path is always the fallback, never the default:

| Job | Default | Escalation |
|---|---|---|
| Duplicate detection | 8×8 average hash, local | never |
| Classification | Apple Vision OCR, local | Gemini Vision |
| Watermark detection | 20-frame variance, local | Gemini Vision |
| Compression / frame split | ffmpeg | never |

`pipeline/cost_tracker.py` records per-feature spend and the escalation rate, so
you can see when the cheap path stops carrying its weight.

---

## Tests

```bash
uv run pytest tests/unit -q
uv run ruff check app pipeline agents tests
```

The suite covers the crypto (including backward compatibility with older
on-disk formats), path confinement, the error contract, security wiring, the
editor endpoints and the video pipeline. `test_endpoint_smoke.py` additionally
asserts no route reports a failure as HTTP 200 — the defect class that once let
five broken endpoints ship unnoticed.

---

## History

This started as a multi-agent system that turned event photo dumps into narrated
keepsake journals: a shareable contributor upload link, a five-agent ADK pipeline
(moderate → dedup → score → remember → narrate), and a journal viewer.

That product was retired. The cleaning and editing surface was the part worth
keeping, so the curation pipeline, the specialist agents, the ADK/A2A layer and
the contributor and viewer pages were removed, and the editor features that had
lived in the old admin page were moved here. The git history has the full record.
