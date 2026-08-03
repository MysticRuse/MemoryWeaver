# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import io
import json
import os

from dotenv import load_dotenv
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()
# Resolve parent directory to locate the .env file in project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, ".env"))


import google.auth
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import HTMLResponse, Response
from google.cloud import logging as google_cloud_logging

from agents.collector.tools.upload import process_and_save_upload
from app.app_utils.ai_budget import track_ai_call
from app.app_utils.errors import (
    AppError,
    NotFoundError,
    UpstreamError,
    register_error_handlers,
)
from app.app_utils.genai_client import (
    PIPELINE_MODEL,
    TTS_MODEL,
    get_gemini_client,
    text_config,
)
from app.app_utils.logging_config import get_logger
from app.app_utils.paths import (
    safe_storage_join,
)
from app.app_utils.storage import StorageHelper
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback
from app.deps import (
    enforce_ai_budget,
    require_admin_token,
)
from app.routers import cleaner as cleaner_router
from app.routers import photo as photo_router
from app.routers import video as video_router
from app.schemas import (
    DescribePhotoBody,
    MovePhotoBody,
    UpdateCaptionBody,
)
from app.services.media_store import (
    get_global_vault_file_path,
    load_image_bytes_decrypted,
    trash_file_safely,
)

logger = get_logger(__name__)

try:
    setup_telemetry()
    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client()
    logger = logging_client.logger(__name__)
except Exception as e:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    project_id = None
    logger.warning(f"Warning: Telemetry setup failed, falling back to standard logger: {e}")
app = FastAPI(
    title="MemoryWeaver Cleaner",
    description="Photo and video library cleaner, organiser and editor.",
)

# Renders AppError subclasses and anything uncaught as JSON with a correct
# status code. Handlers should raise (see app_utils.errors) rather than
# returning {"status": "error"} with an implicit HTTP 200.
register_error_handlers(app)

# Route groups live in app/routers/. This module keeps app construction, page
# serving, media delivery, uploads and library management.
app.include_router(cleaner_router.router)
app.include_router(photo_router.router)
app.include_router(video_router.router)

# Absolute path to local_storage folder
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
local_storage_dir = os.path.join(project_root, "local_storage")

# Ensure local directories exist to prevent mount error
os.makedirs(os.path.join(local_storage_dir, "uploads"), exist_ok=True)
os.makedirs(os.path.join(local_storage_dir, "thumbs"), exist_ok=True)
os.makedirs(os.path.join(local_storage_dir, "artefacts"), exist_ok=True)

# NOTE (STRIDE: information disclosure - see CONTEXT.md): stored originals are
# deliberately NOT static-mounted. They keep their EXIF (exact GPS, device ids)
# and are encrypted at rest; serving the directory would both leak location
# data and hand out ciphertext. Every image reaches the browser through /media
# below, which decrypts and re-encodes to JPEG, dropping all metadata.
#
# Access control and the AI spend cap are dependencies, not middleware - see
# app/deps.py for require_admin_token and enforce_ai_budget.


@app.get("/media")
def serve_media(filename: str, session_id: str = "default", thumbnail: bool = False):
    """Serves stored media, decrypting it on the way out.

    Non-images (video/voice/docs) are streamed as-is; photos are re-encoded to
    JPEG so EXIF - including exact GPS - never reaches the browser.
    """

    safe_filename = os.path.basename(filename)  # path-traversal guard
    session_storage = StorageHelper(session_id=session_id)

    if safe_filename.startswith("temp_transform_"):
        temp_dir = os.path.join(session_storage.local_base, "transform_temp")
        # basename() alone misses resolved-symlink escapes; confine to temp_dir.
        full_path = str(safe_storage_join(temp_dir, safe_filename))
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="Temp transformation media not found")
        with open(full_path, "rb") as f:
            content = f.read()
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
        return Response(content=content, media_type="image/png", headers=headers)

    if thumbnail:
        # Check if thumbnail exists in thumbs directory
        thumb_path = os.path.join(session_storage.local_base, "thumbs", safe_filename)
        if os.path.exists(thumb_path):
            decrypted_bytes = load_image_bytes_decrypted(thumb_path, session_id)
            return Response(content=decrypted_bytes, media_type="image/jpeg")

    full_path = os.path.join(session_storage.local_base, "uploads", safe_filename)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="Media not found")

    _, ext = os.path.splitext(safe_filename.lower())
    if ext in (".mp4", ".mov", ".m4a", ".mp3", ".webm", ".wav", ".pdf", ".txt"):
        mime_types = {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".webm": "video/webm",
            ".wav": "audio/wav",
            ".pdf": "application/pdf",
            ".txt": "text/plain; charset=utf-8"
        }
        media_type = mime_types.get(ext, "application/octet-stream")
        decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
        return Response(content=decrypted_bytes, media_type=media_type, headers={"Accept-Ranges": "bytes"})

    try:
        decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
        with Image.open(io.BytesIO(decrypted_bytes)) as im:
            im = im.convert("RGB")
            im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)  # PIL save without exif= drops metadata
        return Response(
            content=buf.getvalue(),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )
    except Exception as exc:
        raise HTTPException(status_code=415, detail="Could not decode image") from exc


@app.get("/api/photo-captions", dependencies=[Depends(require_admin_token)])
def get_photo_captions(session_id: str = "default"):
    """Bulk caption/metadata read for the Cleaner's photo grid.

    Originally this assembled a "trip book" from the curation pipeline's
    artifacts (highlights.json, manifest.json, journal.json, story.txt,
    memory_bank.json). Those artifacts no longer exist - the curation pipeline
    and the Trip Highlights viewer were removed - but the Cleaner still needs a
    single call that returns every photo's caption for its grid.

    Captions now come from photos_metadata.json, which is the same store
    /api/photo/update-caption writes to, so an edit made in the Cleaner
    is what this returns. The response keeps its original shape (`highlights`
    and `manifest` lists) so the frontend needed no changes.
    """
    session_storage = StorageHelper(session_id=session_id)
    uploads_dir = os.path.join(session_storage.local_base, "uploads")
    session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
    metadata_file = os.path.join(session_dir, "photos_metadata.json")

    photos_meta = {}
    if os.path.exists(metadata_file):
        try:
            with open(metadata_file) as f:
                photos_meta = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning(f"[trip-book] could not read {metadata_file}: {exc}")
    MEDIA_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".mp4", ".mov", ".webm")
    manifest = []
    if os.path.exists(uploads_dir):
        for filename in sorted(os.listdir(uploads_dir)):
            if not filename.lower().endswith(MEDIA_EXT):
                continue
            entry = photos_meta.get(filename, {})
            caption = entry.get("caption") or entry.get("transcription") or ""
            manifest.append({
                "filename": filename,
                "caption": caption,
                "scene_label": "video" if filename.lower().endswith(
                    (".mp4", ".mov", ".webm")) else "photo",
            })

    return {
        "status": "success",
        "manifest": manifest,
        # Retained for response-shape compatibility with the Cleaner, which
        # reads `highlights` into a set it uses only for exclusion checks.
        "highlights": [],
    }





# ------------------------------------------------------------------
# Contributor persona: shareable upload page
#
# Family members get a /join/<event>?code=<share_code> link (or QR). The
# share_code is the upload credential - no accounts, no admin token. The
# admin-facing Curator Hub stays on "/" and never appears on this page.
# ------------------------------------------------------------------








@app.post("/delete", dependencies=[Depends(require_admin_token)])
def delete_photo(filename: str = Form(...), session_id: str = Form("default")):
    """Deletes the original photo and its thumbnail from one session's storage."""
    try:
        # Prevent path traversal attacks
        safe_filename = os.path.basename(filename)
        session_storage = StorageHelper(session_id=session_id)
        upload_path = os.path.join(session_storage.local_base, "uploads", safe_filename)
        thumb_path = os.path.join(session_storage.local_base, "thumbs", safe_filename)

        deleted = []
        size_bytes = 0
        if os.path.exists(upload_path):
            size_bytes = os.path.getsize(upload_path)
            trash_file_safely(upload_path)
            deleted.append("original")
        if os.path.exists(thumb_path):
            trash_file_safely(thumb_path)
            deleted.append("thumbnail")

        # Also remove classification record if exists in cleaner_vault.json
        try:
            vault_file = get_global_vault_file_path()
            if os.path.exists(vault_file):
                with open(vault_file) as f:
                    vault = json.load(f)
                if "classifications" in vault and safe_filename in vault["classifications"]:
                    del vault["classifications"][safe_filename]
                vault["savings_purged_bytes"] = vault.get("savings_purged_bytes", 0) + size_bytes
                with open(vault_file, "w") as f_out:
                    json.dump(vault, f_out, indent=4)
        except Exception as ve_err:
            logger.warning(f"Error removing classification on delete: {ve_err}")
        if not deleted:
            raise NotFoundError("File not found")
        return {"status": "success", "message": f"Successfully deleted {safe_filename} ({', '.join(deleted)})"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to delete file: {e!s}"}

@app.get("/api/cost-summary")
def get_cost_summary(month: str | None = None):
    """Returns itemized monthly spend, call counts, and escalation rates per feature."""
    from pipeline.cost_tracker import get_cost_tracker
    return get_cost_tracker().get_summary(month=month)




# ------------------------------------------------------------------
# Session state
#
# selected_folder_path / pre_clean_results / progress_state / pipeline_logs
# used to be single module-level globals, which meant two concurrent
# trips/events (or just two browser tabs) silently clobbered each other's
# in-progress state. They're now dicts keyed by session_id so multiple
# events can run independently. Every route below defaults session_id to
# "default", which preserves the original single-session behavior for
# callers that don't pass one yet.
# ------------------------------------------------------------------



















# Rename and delete used to be POST /api/update-session and /api/delete-session,
# reachable only from the removed Curator Hub. They are re-exposed here as REST
# verbs on the library resource so the Cleaner's library dropdown can own the
# full lifecycle - it is the only UI now, so it has to.





















@app.post("/api/save-photo-voice", dependencies=[Depends(require_admin_token)])
async def save_photo_voice(
    session_id: str = Form(...),
    filename: str = Form(...),
    transcription: str = Form(""),
    audio: UploadFile = File(None),
    delete_audio: bool = Form(False),
    duration: float = Form(0.0),
    transcribe_updated: str = Form("false"),
    is_sync: str = Form("false")
):
    """Saves transcription caption and voice recordings linked to a photo."""
    try:
        logger.info(f"DEBUG SAVE: filename={filename}, transcription={transcription}, delete_audio={delete_audio}, duration={duration}, has_audio={audio is not None}, transcribe_updated={transcribe_updated}, is_sync={is_sync}")
        session_storage = StorageHelper(session_id=session_id)
        session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
        os.makedirs(session_dir, exist_ok=True)

        # Load existing metadata registry
        import json
        metadata_file = os.path.join(session_dir, "photos_metadata.json")
        photos_meta = {}
        if os.path.exists(metadata_file):
            try:
                with open(metadata_file) as f:
                    photos_meta = json.load(f)
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "fast_api_app", exc)

        # Detect if we should synthesize audio dynamically
        old_transcription = photos_meta.get(filename, {}).get("transcription", "")
        old_voice_note = photos_meta.get(filename, {}).get("voice_note")

        # Determine if this is a background duration sync request
        is_sync_request = (is_sync == "true")

        should_synthesize = False
        if transcription and not is_sync_request and not audio and not delete_audio:
            is_edited = (transcribe_updated == "true")

            # Check if a valid playable voice file already exists
            has_valid_voice_file = False
            if old_voice_note and old_voice_note.endswith('.wav'):
                upload_dir = os.path.join(session_storage.local_base, "uploads")
                if os.path.exists(os.path.join(upload_dir, old_voice_note)):
                    has_valid_voice_file = True

            # Synthesize if: no valid file, text changed, user explicitly saved, or old file is webm (broken)
            if not has_valid_voice_file or transcription != old_transcription or is_edited:
                should_synthesize = True

        logger.info(f"DEBUG SAVE old_transcription={old_transcription}, old_voice_note={old_voice_note}, is_sync={is_sync_request}, should_synthesize={should_synthesize}")
        # If audio is uploaded, save it to the session uploads directory
        audio_filename = None
        if audio:
            base_name, _ = os.path.splitext(filename)
            audio_filename = f"voice_{base_name}.webm"
            upload_dir = os.path.join(session_storage.local_base, "uploads")
            os.makedirs(upload_dir, exist_ok=True)
            audio_path = os.path.join(upload_dir, audio_filename)
            with open(audio_path, "wb") as f:
                content = await audio.read()
                f.write(content)
        elif should_synthesize:
            # Voice Cloning / Speech Synthesis via Gemini API
            api_key = os.getenv("GEMINI_API_KEY")
            if api_key:
                try:
                    import base64

                    from google.genai import types

                    client = get_gemini_client()
                    contents = []

                    # Look for old voice note to use as cloning reference
                    ref_audio_path = None
                    if old_voice_note:
                        upload_dir = os.path.join(session_storage.local_base, "uploads")
                        ref_audio_path = os.path.join(upload_dir, old_voice_note)

                    has_reference = False
                    if ref_audio_path and os.path.exists(ref_audio_path):
                        ref_ext = os.path.splitext(ref_audio_path.lower())[1]
                        mime_type = "audio/wav" if ref_ext == ".wav" else "audio/webm"
                        with open(ref_audio_path, "rb") as f:
                            ref_bytes = f.read()
                        contents.append(
                            types.Part.from_bytes(
                                data=ref_bytes,
                                mime_type=mime_type
                            )
                        )
                        contents.append(
                            "You are a voice cloning model. Listen to the speaker's voice in the provided audio file. "
                            "Read the following text out loud in the exact same voice, gender, pitch, accent, and speed "
                            "as the speaker in the audio. Return ONLY the synthesized speech: " + transcription
                        )
                        has_reference = True
                    else:
                        contents.append(
                            "Synthesize the following text as natural spoken audio: " + transcription
                        )

                    # Use gemini-2.5-flash-preview-tts which natively supports AUDIO response modality
                    config_args = {"response_modalities": ["AUDIO"]}
                    if not has_reference:
                        config_args["speech_config"] = types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name="Puck"
                                )
                            )
                        )

                    response = client.models.generate_content(
                        model=TTS_MODEL,
                        contents=contents,
                        config=types.GenerateContentConfig(**config_args)
                    )
                    track_ai_call("voice_synthesis", session_id, response=response)

                    # Extract synthesized audio bytes
                    synthesized_bytes = None
                    for part in response.candidates[0].content.parts:
                        if part.inline_data:
                            raw_data = part.inline_data.data
                            if isinstance(raw_data, str):
                                synthesized_bytes = base64.b64decode(raw_data)
                            else:
                                synthesized_bytes = raw_data
                            break

                    if synthesized_bytes:
                        # Wrap raw PCM in a standard WAV container so browsers can play it natively
                        import io
                        import wave
                        wav_buffer = io.BytesIO()
                        with wave.open(wav_buffer, 'wb') as wav_file:
                            wav_file.setnchannels(1)      # mono
                            wav_file.setsampwidth(2)      # 16-bit
                            wav_file.setframerate(24000)  # 24kHz
                            wav_file.writeframes(synthesized_bytes)
                        wav_bytes = wav_buffer.getvalue()

                        logger.info(f"Synthesized WAV audio: {len(wav_bytes)} bytes")
                        base_name, _ = os.path.splitext(filename)
                        audio_filename = f"voice_{base_name}.wav"
                        upload_dir = os.path.join(session_storage.local_base, "uploads")
                        os.makedirs(upload_dir, exist_ok=True)
                        audio_path = os.path.join(upload_dir, audio_filename)
                        with open(audio_path, "wb") as f:
                            f.write(wav_bytes)

                        # If the old voice note has a different extension/name, delete it to avoid clutter
                        if old_voice_note and old_voice_note != audio_filename:
                            try:
                                old_path = os.path.join(upload_dir, old_voice_note)
                                if os.path.exists(old_path):
                                    os.remove(old_path)
                            except Exception as e:
                                logger.warning(f"Error removing old voice note: {e}")
                        # Re-open the WAV to calculate accurate duration
                        est_duration = 0.0
                        try:
                            with wave.open(io.BytesIO(wav_bytes), 'rb') as wav_read:
                                est_duration = wav_read.getnframes() / float(wav_read.getframerate())
                        except Exception as exc:
                            logger.warning("%s: best-effort step failed, continuing: %s", "fast_api_app", exc)

                        duration = est_duration
                    else:
                        logger.warning("Failed to find inline_data with audio bytes in Gemini response.")
                except Exception as e:
                    logger.warning(f"Failed voice synthesis: {e}")
        # Update metadata mapping
        if filename not in photos_meta:
            photos_meta[filename] = {}

        photos_meta[filename]["transcription"] = transcription
        if audio_filename:
            photos_meta[filename]["voice_note"] = audio_filename
            photos_meta[filename]["voice_duration"] = duration

        if delete_audio:
            if "voice_note" in photos_meta[filename]:
                old_voice = photos_meta[filename]["voice_note"]
                photos_meta[filename].pop("voice_note", None)
                photos_meta[filename].pop("voice_duration", None)
                try:
                    upload_dir = os.path.join(session_storage.local_base, "uploads")
                    old_path = os.path.join(upload_dir, old_voice)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except Exception as e:
                    logger.warning(f"Error removing voice note file {old_voice}: {e}")
        with open(metadata_file, "w") as f:
            json.dump(photos_meta, f, indent=4)

        return {
            "status": "success",
            "message": "Voice note and transcription saved successfully.",
            "voice_note": audio_filename or photos_meta.get(filename, {}).get("voice_note"),
            "transcription": transcription,
            "voice_duration": photos_meta.get(filename, {}).get("voice_duration", 0.0)
        }
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e

















@app.post("/upload", dependencies=[Depends(require_admin_token)])
async def handle_photo_upload(
    photos: list[UploadFile] = File(...),
    contributor_name: str = Form("Anonymous"),
    session_id: str = Form("default"),
):
    """Ingests files into the library.

    This was credentialed by a per-event share_code embedded in the /join
    contributor link, so family members needed no setup. That page is gone and
    the only caller is now this app's own Magic Scanner, which had to fetch the
    code from /api/share-info purely to satisfy a check against itself. The route
    now sits behind the same admin token as every other mutating endpoint.

    It also used to register the uploader in a Memory Bank, which belonged to the
    removed contributor-attribution feature; `contributor_name` is retained only
    because it still seeds the stored filename prefix.
    """
    results = []
    errors = []

    for photo in photos:
        try:
            file_bytes = await photo.read()
            info = process_and_save_upload(
                file_bytes=file_bytes,
                original_filename=photo.filename,
                contributor_name=contributor_name,
                session_id=session_id
            )
            results.append(info)
        except Exception as e:
            errors.append(f"Error uploading {photo.filename}: {e!s}")

    if errors:
        return {"status": "partial_success", "uploaded": results, "errors": errors}
    return {"status": "success", "message": f"Successfully uploaded {len(results)} photos!", "uploaded": results}


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    if hasattr(logger, "log_struct"):
        logger.log_struct(feedback.model_dump(), severity="INFO")
    else:
        logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


























@app.post("/api/photo/move", dependencies=[Depends(require_admin_token)])
def move_photo(body: MovePhotoBody):
    import json
    try:
        session_storage = StorageHelper(session_id=body.session_id)
        artefacts_dir = os.path.join(session_storage.local_base, "artefacts")

        journal_path = os.path.join(artefacts_dir, "journal.json")
        highlights_path = os.path.join(artefacts_dir, "highlights.json")
        manifest_path = os.path.join(artefacts_dir, "manifest.json")

        if not os.path.exists(journal_path) or not os.path.exists(highlights_path):
            raise HTTPException(status_code=400, detail="Curation has not been run for this session.")

        with open(journal_path) as f:
            journal = json.load(f)

        with open(highlights_path) as f:
            highlights = json.load(f)

        if body.action == "demote":
            # 1. Remove from journal
            for entry in journal:
                if "photos" in entry:
                    entry["photos"] = [p for p in entry["photos"] if p != body.filename]
            # 2. Remove from highlights
            highlights = [hl for hl in highlights if hl.get("filename") != body.filename]

        elif body.action == "promote":
            # 1. Get details from manifest
            photo_record = None
            if os.path.exists(manifest_path):
                with open(manifest_path) as f:
                    manifest = json.load(f)
                    for item in manifest:
                        if item.get("filename") == body.filename:
                            photo_record = item
                            break
            if not photo_record:
                photo_record = {
                    "filename": body.filename,
                    "score": 0.0,
                    "scene_label": "other",
                    "caption": "Added highlight"
                }

            # 2. Add to highlights if not present
            if not any(hl.get("filename") == body.filename for hl in highlights):
                highlights.append(photo_record)

            # 3. Add to journal entry matching date or first entry
            photo_date = photo_record.get("date")
            added_to_journal = False
            if photo_date:
                for entry in journal:
                    if entry.get("date") == photo_date:
                        if "photos" not in entry:
                            entry["photos"] = []
                        if body.filename not in entry["photos"]:
                            entry["photos"].append(body.filename)
                        added_to_journal = True
                        break

            if not added_to_journal and len(journal) > 0:
                entry = journal[0]
                if "photos" not in entry:
                    entry["photos"] = []
                if body.filename not in entry["photos"]:
                    entry["photos"].append(body.filename)

        # Save back
        with open(journal_path, "w") as f:
            json.dump(journal, f, indent=2)

        with open(highlights_path, "w") as f:
            json.dump(highlights, f, indent=2)

        return {"status": "success", "message": f"Successfully {body.action}d {body.filename}."}
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e




@app.post("/api/photo/update-caption", dependencies=[Depends(require_admin_token)])
def update_photo_caption(body: UpdateCaptionBody):
    import json
    try:
        session_storage = StorageHelper(session_id=body.session_id)

        # photos_metadata.json is the single home for a photo's caption. This
        # used to also rewrite artefacts/highlights.json and manifest.json, but
        # those were curation-pipeline outputs; with the pipeline removed they
        # never exist, so both branches were dead writes. /api/photo-captions now
        # reads captions from here.
        session_dir = os.path.join(session_storage.local_base, "sessions", body.session_id)
        metadata_file = os.path.join(session_dir, "photos_metadata.json")
        photos_meta = {}
        if os.path.exists(metadata_file):
            try:
                with open(metadata_file) as f:
                    photos_meta = json.load(f)
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "fast_api_app", exc)
        if body.filename not in photos_meta:
            photos_meta[body.filename] = {}
        photos_meta[body.filename]["transcription"] = body.caption
        with open(metadata_file, "w") as f:
            json.dump(photos_meta, f, indent=4)

        return {"status": "success", "message": "Caption updated successfully."}
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e




@app.post("/api/photo/describe", dependencies=[Depends(require_admin_token), Depends(enforce_ai_budget)])
def describe_photo(body: DescribePhotoBody):
    try:
        session_storage = StorageHelper(session_id=body.session_id)
        filepath = os.path.join(session_storage.local_base, "uploads", os.path.basename(body.filename))
        if not os.path.exists(filepath):
            raise HTTPException(status_code=404, detail="Photo not found")

        decrypted_bytes = load_image_bytes_decrypted(filepath, body.session_id)

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return {"status": "success", "caption": "A beautiful photo snapshot."}

        import io

        from PIL import Image

        client = get_gemini_client()
        img = Image.open(io.BytesIO(decrypted_bytes))

        prompt = (
            "Provide a brief, warm, descriptive caption (1 sentence, max 15 words) "
            "for this photo, written from a traveler's perspective. Do not include quotes."
        )
        response = client.models.generate_content(
            model=PIPELINE_MODEL, config=text_config(),
            contents=[img, prompt]
        )
        track_ai_call("describe_photo", body.session_id, response=response)
        caption = response.text.strip().strip('"').strip("'")
        return {"status": "success", "caption": caption}
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e


# --- Album Cleaner & Vault Feature ---













@app.get("/", response_class=HTMLResponse)
async def serve_root():
    """The Cleaner is the application's only page, so it owns the root URL.

    "/" previously served upload.html (the Curator Hub). That page and the Trip
    Highlights viewer were removed; /cleaner is kept as an alias so existing
    bookmarks and the in-page nav links keep resolving.
    """
    return await serve_cleaner_page()


@app.get("/cleaner", response_class=HTMLResponse)
async def serve_cleaner_page():
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "cleaner.html"))
    if os.path.exists(html_path):
        with open(html_path) as f:
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
            return HTMLResponse(content=f.read(), status_code=200, headers=headers)
    return HTMLResponse(content="<h1>Cleaner page not found</h1>", status_code=404)













































# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
