"""Bulk reclassification via Gemini's Batch API.

`/api/cleaner/analyze-all` is interactive: one Gemini request per uncached
photo, streamed to a live progress bar, because a user is watching. Wiring
that same one-request-per-photo loop into an unattended full-library
reclassify (after a taxonomy change, say) would burn the same request count
for no reason - nobody is waiting on a bar to fill in. This submits every
photo the local heuristics couldn't resolve as a single Gemini Batch job
instead (about half the per-call cost, at the price of asynchronous
turnaround), meant to be started and polled from a background/cron caller.
"""

import io
import json
import os

from PIL import Image

from app.app_utils.ai_budget import track_ai_call
from app.app_utils.genai_client import PIPELINE_MODEL, get_gemini_client, text_config
from app.app_utils.logging_config import get_logger
from app.app_utils.storage import StorageHelper
from app.services.classification import (
    classify_known_credential_filename,
    classify_video_file,
    run_local_ocr_credential_check,
)
from app.services.media_store import (
    get_global_vault_file_path,
    humanize_caption,
    load_image_bytes_decrypted,
)
from app.services.photo_taxonomy import CATEGORIZER_PROMPT, parse_categorizer_response

logger = get_logger(__name__)

_JOBS_FILE = "batch_reclassify_jobs.json"


def _jobs_path(session_storage: StorageHelper) -> str:
    return os.path.join(session_storage.local_base, "sessions", _JOBS_FILE)


def _load_jobs(session_storage: StorageHelper) -> dict:
    path = _jobs_path(session_storage)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("%s: best-effort step failed, continuing: %s", "batch_reclassify", exc)
    return {}


def _save_jobs(session_storage: StorageHelper, jobs: dict) -> None:
    os.makedirs(os.path.dirname(_jobs_path(session_storage)), exist_ok=True)
    with open(_jobs_path(session_storage), "w") as f:
        json.dump(jobs, f, indent=2)


def submit_batch_reclassify(session_id: str, filenames: list) -> dict:
    """Submits one Gemini batch job covering every filename the free local
    heuristics (video/known-credential-filename/OCR) could not resolve.

    Returns the batch job name (None if everything resolved locally - no
    billable call was needed), the filenames resolved locally, and the ones
    handed to the batch job.
    """
    from google.genai import types

    session_storage = StorageHelper(session_id=session_id)
    upload_dir = os.path.join(session_storage.local_base, "uploads")

    resolved_locally = {}
    pending_requests = []
    pending_filenames = []

    for f in filenames:
        full_path = os.path.join(upload_dir, f)
        if not os.path.exists(full_path):
            continue

        classification = classify_video_file(f)
        if classification is None:
            classification = classify_known_credential_filename(f)
        if classification is None:
            classification = run_local_ocr_credential_check(full_path, f, session_id)

        if classification is not None:
            resolved_locally[f] = classification
            continue

        try:
            decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
            img = Image.open(io.BytesIO(decrypted_bytes))
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
        except Exception as exc:
            logger.warning("%s: best-effort step failed, continuing: %s", "batch_reclassify", exc)
            continue

        pending_requests.append(
            types.InlinedRequest(
                model=PIPELINE_MODEL,
                contents=[img, CATEGORIZER_PROMPT],
                config=text_config(),
                metadata={"filename": f},
            )
        )
        pending_filenames.append(f)

    job_name = None
    if pending_requests:
        client = get_gemini_client()
        job = client.batches.create(
            model=PIPELINE_MODEL,
            src=pending_requests,
            config=types.CreateBatchJobConfig(display_name=f"reclassify-{session_id}"),
        )
        job_name = job.name

        jobs = _load_jobs(session_storage)
        jobs[job_name] = {"filenames": pending_filenames, "state": "PENDING"}
        _save_jobs(session_storage, jobs)

    return {
        "job_name": job_name,
        "resolved_locally": resolved_locally,
        "submitted": pending_filenames,
    }


def poll_batch_reclassify(session_id: str, job_name: str) -> dict:
    """Checks a submitted batch job and, once it has finished, applies every
    successful response to the shared vault's classifications.

    Safe to call repeatedly while the job is still running - batch jobs
    commonly take minutes to hours, so this is meant to be polled by an
    unattended/cron caller, not awaited inside a live request.
    """
    session_storage = StorageHelper(session_id=session_id)
    client = get_gemini_client()
    job = client.batches.get(name=job_name)

    if not job.done:
        return {"status": "pending", "state": job.state.name if job.state else "UNKNOWN"}

    jobs = _load_jobs(session_storage)
    job_record = jobs.get(job_name, {"filenames": []})

    if not job.state or job.state.name != "JOB_STATE_SUCCEEDED":
        job_record["state"] = job.state.name if job.state else "FAILED"
        jobs[job_name] = job_record
        _save_jobs(session_storage, jobs)
        return {"status": "failed", "state": job_record["state"]}

    vault_file = get_global_vault_file_path()
    vault = {"notes": {}, "locked_photos": {}, "classifications": {}}
    if os.path.exists(vault_file):
        try:
            with open(vault_file) as f:
                vault = json.load(f)
        except Exception as exc:
            logger.warning("%s: best-effort step failed, continuing: %s", "batch_reclassify", exc)
    if "classifications" not in vault:
        vault["classifications"] = {}

    applied = []
    responses = (job.dest.inlined_responses if job.dest else None) or []
    for item in responses:
        filename = (item.metadata or {}).get("filename")
        if not filename:
            continue
        if item.error or not item.response:
            logger.warning("Batch reclassify item failed for %s: %s", filename, item.error)
            continue

        track_ai_call(
            "classification_caption_combined_batch",
            session_id,
            response=item.response,
            is_escalation=True,
        )
        classification = parse_categorizer_response(item.response.text)
        if classification.get("reason"):
            classification["reason"] = humanize_caption(classification["reason"])
        vault["classifications"][filename] = classification
        applied.append(filename)

    with open(vault_file, "w") as f_out:
        json.dump(vault, f_out, indent=4)

    job_record["state"] = "APPLIED"
    jobs[job_name] = job_record
    _save_jobs(session_storage, jobs)

    return {"status": "done", "applied": applied}
