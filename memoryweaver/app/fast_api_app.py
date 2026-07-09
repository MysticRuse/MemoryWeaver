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
import os
import sys
import datetime
from dotenv import load_dotenv
# Resolve parent directory to locate the .env file in project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, ".env"))

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import google.auth
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentExtension
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    EXTENDED_AGENT_CARD_PATH,
)
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.cloud import logging as google_cloud_logging

from agents.collector.tools.upload import process_and_save_upload
from pipeline.orchestrator import execute_trip_pipeline

from app.agent import app as adk_app
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback
from app.app_utils.sessions import SessionStore, public_view
from app.app_utils.storage import StorageHelper

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
    print(f"Warning: Telemetry setup failed, falling back to standard logger: {e}")

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
artifact_service = (
    GcsArtifactService(bucket_name=logs_bucket_name)
    if logs_bucket_name
    else InMemoryArtifactService()
)

runner = Runner(
    app=adk_app,
    artifact_service=artifact_service,
    session_service=InMemorySessionService(),
)

request_handler = DefaultRequestHandler(
    agent_executor=A2aAgentExecutor(runner=runner),
    task_store=InMemoryTaskStore(),
)

A2A_RPC_PATH = f"/a2a/{adk_app.name}"


async def build_dynamic_agent_card() -> AgentCard:
    """Builds the Agent Card dynamically from the root_agent."""
    agent_card_builder = AgentCardBuilder(
        agent=adk_app.root_agent,
        capabilities=AgentCapabilities(
            streaming=True,
            extensions=[
                AgentExtension(
                    uri="https://google.github.io/adk-docs/a2a/a2a-extension/",
                    description="Ability to use the new agent executor implementation",
                ),
            ],
        ),
        rpc_url=f"{os.getenv('APP_URL', 'http://0.0.0.0:8000')}{A2A_RPC_PATH}",
        agent_version=os.getenv("AGENT_VERSION", "0.1.0"),
    )
    agent_card = await agent_card_builder.build()
    return agent_card


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    agent_card = await build_dynamic_agent_card()
    a2a_app = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)
    a2a_app.add_routes_to_app(
        app_instance,
        agent_card_url=f"{A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}",
        rpc_url=A2A_RPC_PATH,
        extended_agent_card_url=f"{A2A_RPC_PATH}{EXTENDED_AGENT_CARD_PATH}",
    )
    yield


app = FastAPI(
    title="memoryweaver",
    description="API for interacting with the Agent memoryweaver",
    lifespan=lifespan,
)

# Absolute path to local_storage folder
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
local_storage_dir = os.path.join(project_root, "local_storage")

# Ensure local directories exist to prevent mount error
os.makedirs(os.path.join(local_storage_dir, "uploads"), exist_ok=True)
os.makedirs(os.path.join(local_storage_dir, "thumbs"), exist_ok=True)
os.makedirs(os.path.join(local_storage_dir, "artefacts"), exist_ok=True)

# Ensure the session registry exists and 'default' is registered, so
# GET /api/sessions works immediately for pre-existing single-session installs.
SessionStore()

# ------------------------------------------------------------------
# Access control (STRIDE: spoofing/tampering - see CONTEXT.md)
#
# Destructive or billable endpoints (/delete, /generate, pre-clean/ingest,
# session creation) require a shared admin token when MW_ADMIN_TOKEN is set
# in the environment. When it is unset (local development, demo on a trusted
# machine) the check is a no-op so the app stays frictionless. Deployments
# MUST set MW_ADMIN_TOKEN - the Terraform/Cloud Run docs call this out.
# The token is accepted via the X-MW-Token header.
# ------------------------------------------------------------------
def require_admin_token(x_mw_token: str | None = Header(None)):
    expected = os.environ.get("MW_ADMIN_TOKEN")
    if expected and x_mw_token != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-MW-Token header.")


# NOTE (STRIDE: information disclosure - see CONTEXT.md): raw uploads are
# deliberately NOT static-mounted. Originals keep their EXIF (exact GPS,
# device ids); serving them verbatim would leak location data to anyone with
# a URL. The frontend gets imagery only through /media below, which re-encodes
# to JPEG and drops all metadata. Artifacts (journal/story/highlights JSON)
# are served through /api/trip-book rather than a directory mount so the
# curation cache and memory bank internals aren't browsable either.


@app.get("/media")
def serve_media(filename: str, session_id: str = "default"):
    """Serves curated media. Non-images (video/voice/docs) are returned directly,
    while photos are re-encoded to JPEG to drop metadata (EXIF).
    """
    from PIL import Image
    import io
    from fastapi.responses import FileResponse

    safe_filename = os.path.basename(filename)  # path-traversal guard
    session_storage = StorageHelper(session_id=session_id)
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
            ".webm": "audio/webm",
            ".wav": "audio/wav",
            ".pdf": "application/pdf",
            ".txt": "text/plain; charset=utf-8"
        }
        media_type = mime_types.get(ext, "application/octet-stream")
        return FileResponse(path=full_path, media_type=media_type)

    try:
        with Image.open(full_path) as im:
            im = im.convert("RGB")
            im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)  # PIL save without exif= drops metadata
        return Response(
            content=buf.getvalue(),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )
    except Exception:
        raise HTTPException(status_code=415, detail="Could not decode image")


@app.get("/api/trip-book")
def get_trip_book(session_id: str = "default"):
    """Returns all generated artifacts for one session in a single response.

    Replaces direct static access to artefacts/*.json and memory_bank.json so
    only the curated outputs (not caches or raw storage) are reachable.
    """
    session_storage = StorageHelper(session_id=session_id)
    artefacts_dir = os.path.join(session_storage.local_base, "artefacts")

    def read_json(path):
        try:
            with open(path) as f:
                import json as _json
                return _json.load(f)
        except Exception:
            return None

    highlights = read_json(os.path.join(artefacts_dir, "highlights.json"))
    journal = read_json(os.path.join(artefacts_dir, "journal.json"))
    story = None
    story_path = os.path.join(artefacts_dir, "story.txt")
    if os.path.exists(story_path):
        with open(story_path) as f:
            story = f.read()

    manifest = read_json(os.path.join(artefacts_dir, "manifest.json"))

    if highlights is None or journal is None or story is None:
        return {"status": "empty", "message": "Curation pipeline has not been run for this session."}

    # Contributor names are needed for the viewer's per-person filter; expose
    # only name/upload_count/moments - never raw EXIF or storage paths.
    memory = read_json(os.path.join(session_storage.local_base, "memory_bank.json")) or {}
    contributors = {
        cid: {
            "name": prof.get("name"),
            "upload_count": prof.get("upload_count", 0),
            "moments_present_in": prof.get("moments_present_in", []),
        }
        for cid, prof in (memory.get("contributors") or {}).items()
    }

    return {
        "status": "success",
        "highlights": highlights,
        "journal": journal,
        "story": story,
        "manifest": manifest,
        "memory_bank": {"contributors": contributors, "trip_context": memory.get("trip_context", {})},
    }


@app.get("/", response_class=HTMLResponse)
async def serve_upload_page():
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "upload.html"))
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(content="<h1>Upload page not found</h1>", status_code=404)


# ------------------------------------------------------------------
# Contributor persona: shareable upload page
#
# Family members get a /join/<event>?code=<share_code> link (or QR). The
# share_code is the upload credential - no accounts, no admin token. The
# admin-facing Curator Hub stays on "/" and never appears on this page.
# ------------------------------------------------------------------

@app.get("/join/{session_id}", response_class=HTMLResponse)
async def serve_contribute_page(session_id: str):
    store = SessionStore()
    if not store.get_session(session_id):
        return HTMLResponse(content="<h1>Event not found</h1><p>Check the link you were sent.</p>", status_code=404)
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "contribute.html"))
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(content="<h1>Contribute page not found</h1>", status_code=404)


@app.get("/api/session-public/{session_id}")
def get_session_public(session_id: str, code: str = ""):
    """Event info for the contributor page: name/type, photo count, and whether
    the journal is ready (so the page can show a viewer link once generated).
    Requires the share code - this endpoint deliberately reveals nothing
    without the link credential."""
    store = SessionStore()
    session = store.get_session(session_id)
    if not session or code != session.get("share_code"):
        raise HTTPException(status_code=403, detail="Invalid or missing share code")

    storage = StorageHelper(session_id=session_id)
    uploads_dir = os.path.join(storage.local_base, "uploads")
    photo_count = len([f for f in os.listdir(uploads_dir) if not f.startswith(".")]) if os.path.isdir(uploads_dir) else 0
    journal_ready = os.path.exists(os.path.join(storage.local_base, "artefacts", "journal.json"))

    return {
        "status": "success",
        "name": session["name"],
        "event_type": session["event_type"],
        "photo_count": photo_count,
        "journal_ready": journal_ready,
        "viewer_url": f"/viewer?session={session_id}" if journal_ready else None,
    }


@app.get("/api/share-info", dependencies=[Depends(require_admin_token)])
def get_share_info(session_id: str = "default"):
    """Admin-only: the shareable contributor link + QR code for one event."""
    store = SessionStore()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    base = os.getenv("APP_URL", "").rstrip("/")  # deployed URL; falls back to relative
    share_path = f"/join/{session_id}?code={session['share_code']}"
    share_url = f"{base}{share_path}" if base else share_path

    # Inline QR as a data URI so the admin card needs no extra storage round-trip
    qr_data_uri = None
    try:
        import base64
        import io as _io
        import qrcode
        img = qrcode.make(share_url if base else f"http://localhost:8000{share_path}")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"QR generation failed: {e}")

    return {"status": "success", "share_url": share_url, "share_path": share_path, "qr_data_uri": qr_data_uri}


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
        if os.path.exists(upload_path):
            os.remove(upload_path)
            deleted.append("original")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
            deleted.append("thumbnail")
            
        if not deleted:
            return {"status": "error", "message": "File not found"}
        return {"status": "success", "message": f"Successfully deleted {safe_filename} ({', '.join(deleted)})"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to delete file: {str(e)}"}

@app.post("/api/clear-session", dependencies=[Depends(require_admin_token)])
def clear_session(session_id: str = "default"):
    """Admin-only: clears all uploaded photos, thumbnails, and generated artefacts/memory bank for a session."""
    try:
        session_storage = StorageHelper(session_id=session_id)
        
        # Clear Google Cloud Storage prefix contents if GCS is enabled
        if session_storage.use_gcs:
            for folder in ("uploads/", "thumbs/", "artefacts/"):
                prefix = session_storage.gcs_path(folder)
                try:
                    blobs = session_storage.bucket.list_blobs(prefix=prefix)
                    for blob in blobs:
                        blob.delete()
                except Exception as e:
                    print(f"Error deleting GCS prefix {prefix}: {e}")
            try:
                mb_blob = session_storage.bucket.blob(session_storage.gcs_path("memory_bank.json"))
                if mb_blob.exists():
                    mb_blob.delete()
            except Exception as e:
                print(f"Error deleting GCS memory_bank.json: {e}")
        
        import shutil
        shutil.rmtree(os.path.join(session_storage.local_base, "uploads"), ignore_errors=True)
        shutil.rmtree(os.path.join(session_storage.local_base, "thumbs"), ignore_errors=True)
        shutil.rmtree(os.path.join(session_storage.local_base, "artefacts"), ignore_errors=True)
        # Recreate empty uploads/thumbs directories
        os.makedirs(os.path.join(session_storage.local_base, "uploads"), exist_ok=True)
        os.makedirs(os.path.join(session_storage.local_base, "thumbs"), exist_ok=True)
        
        # Delete local memory_bank.json if exists
        mb_path = os.path.join(session_storage.local_base, "memory_bank.json")
        if os.path.exists(mb_path):
            try:
                os.remove(mb_path)
            except Exception as e:
                print(f"Error deleting local memory_bank.json: {e}")
        
        # Reset progress states
        state = _get_progress_state(session_id)
        state["pipeline"] = {"current": 0, "total": 0, "status": "idle", "phase": "initiating", "start_time": 0.0}
        
        return {"status": "success", "message": "Session storage cleared successfully."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to clear session: {str(e)}"}

@app.post("/api/delete-session", dependencies=[Depends(require_admin_token)])
def delete_session(session_id: str = Form(...)):
    """Admin-only: Deletes the entire session metadata and all of its uploaded files."""
    if session_id == "default":
        return {"status": "error", "message": "The default session cannot be deleted."}
    try:
        store = SessionStore()
        store.delete_session(session_id)
        
        # Reset progress state & logs
        if session_id in progress_state_by_session:
            del progress_state_by_session[session_id]
        if session_id in pipeline_logs_by_session:
            del pipeline_logs_by_session[session_id]
            
        return {"status": "success", "message": f"Session '{session_id}' has been deleted successfully."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to delete session: {str(e)}"}


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
progress_state_by_session: dict[str, dict] = {}
pipeline_logs_by_session: dict[str, list] = {}


def _get_progress_state(session_id: str) -> dict:
    if session_id not in progress_state_by_session:
        progress_state_by_session[session_id] = {
            "pre_clean": {"current": 0, "total": 0, "status": "idle", "start_time": 0.0},
            "pipeline": {"current": 0, "total": 0, "status": "idle", "phase": "idle", "start_time": 0.0},
        }
    return progress_state_by_session[session_id]


def log_pipeline_step(session_id: str, message: str):
    """Appends a timestamped line to one session's in-memory pipeline log."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    pipeline_logs_by_session.setdefault(session_id, []).append(log_line)
    print(f"[{session_id}] {log_line}")


from pydantic import BaseModel

class IngestRequest(BaseModel):
    filenames: list[str]
    session_id: str = "default"


class CreateSessionRequest(BaseModel):
    name: str
    event_type: str = "trip"


@app.post("/api/sessions", dependencies=[Depends(require_admin_token)])
def create_session(req: CreateSessionRequest):
    """Creates a new, fully isolated event session (own uploads/thumbs/artefacts/memory).

    share_code is stripped from the response: the sanctioned way to obtain it
    is /api/share-info (the UI fetches it there right after creating)."""
    store = SessionStore()
    session = store.create_session(req.name, req.event_type)
    return {"status": "success", "session": public_view(session)}


class UpdateSessionRequest(BaseModel):
    session_id: str
    name: str
    event_type: str = "trip"


@app.post("/api/update-session", dependencies=[Depends(require_admin_token)])
def update_session(req: UpdateSessionRequest):
    """Admin-only: updates name and event_type of a session."""
    store = SessionStore()
    session = store.update_session(req.session_id, {"name": req.name.strip(), "event_type": req.event_type})
    if not session:
        return {"status": "error", "message": "Session not found"}
    return {"status": "success", "session": public_view(session)}



@app.get("/api/sessions")
def list_sessions():
    """Lists every saved event session, most recent first. share_code excluded -
    this endpoint is unauthenticated, and leaking codes here would let anyone
    who can list events also upload into them."""
    store = SessionStore()
    return {
        "status": "success",
        "sessions": [public_view(s) for s in store.list_sessions()],
        "event_types": list(SessionStore.EVENT_TYPES),
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    store = SessionStore()
    session = store.get_session(session_id)
    if not session:
        return {"status": "error", "message": "Session not found"}
    return {"status": "success", "session": public_view(session)}




@app.get("/api/progress")
def get_progress(session_id: str = "default"):
    """Returns the current progress state of long-running operations for a session."""
    # Inject active elapsed time calculations
    import time
    state = _get_progress_state(session_id)
    resp = {}
    for key, val in state.items():
        resp[key] = val.copy()
        if val["status"] == "running" and val["start_time"] > 0:
            resp[key]["elapsed"] = round(time.time() - val["start_time"], 1)
        else:
            resp[key]["elapsed"] = 0.0
    resp["folder"] = None
    resp["session_id"] = session_id
    resp["is_local"] = False
    return resp



@app.get("/trip-stats")
def get_trip_stats(session_id: str = "default"):
    """Returns the total number of uploaded photos in a session's active photo pool."""
    try:
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        count = 0
        active_count = 0
        if os.path.exists(upload_dir):
            files = [
                f for f in os.listdir(upload_dir)
                if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith('.')
            ]
            count = len(files)
            
            # Fetch exclusions
            from app.app_utils.sessions import SessionStore
            store = SessionStore()
            session = store.get_session(session_id)
            excluded = set(session.get("excluded_photos", [])) if session else set()
            
            active_count = len([f for f in files if f not in excluded])
            
        return {
            "status": "success",
            "total_uploaded": count,
            "active_count": active_count,
            "session_id": session_id
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_photo_date(file_path: str) -> str:
    """Helper to extract the photo capture date or file date for uploader display."""
    # 1. Try parsing from EXIF
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        with Image.open(file_path) as img:
            exif = img._getexif()
            if exif:
                for tag, value in exif.items():
                    decoded = TAGS.get(tag, tag)
                    if decoded in ("DateTimeOriginal", "DateTime"):
                        # Format: 'YYYY:MM:DD HH:MM:SS'
                        parts = value.split(" ")[0].split(":")
                        if len(parts) == 3:
                            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        pass

    # 2. Try parsing from filename prefix (format: hash_20260709_055450_name.jpg)
    basename = os.path.basename(file_path)
    parts = basename.split("_")
    if len(parts) >= 3 and len(parts[1]) == 8 and parts[1].isdigit():
        # YYYYMMDD -> YYYY-MM-DD
        date_str = parts[1]
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    # 3. Fallback to file modification date
    try:
        mtime = os.path.getmtime(file_path)
        import datetime
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except Exception:
        return "Unknown Date"


@app.get("/api/list-uploads", dependencies=[Depends(require_admin_token)])
def list_uploads(session_id: str = "default"):
    """Lists the filenames and metadata of all uploaded photos in a session."""
    try:
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        photos = []
        if os.path.exists(upload_dir):
            file_list = sorted([
                f for f in os.listdir(upload_dir)
                if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith('.')
            ])
            for f in file_list:
                full_path = os.path.join(upload_dir, f)
                date_str = get_photo_date(full_path)
                photos.append({
                    "filename": f,
                    "date": date_str
                })
        return {"status": "success", "photos": photos, "session_id": session_id}
@app.get("/api/photo-metadata", dependencies=[Depends(require_admin_token)])
def get_photo_metadata(filename: str, session_id: str = "default"):
    """Returns detailed EXIF and file metadata for a specific uploaded photo."""
    try:
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        full_path = os.path.join(upload_dir, filename)
        
        if not os.path.exists(full_path):
            return {"status": "error", "message": "Photo not found"}
            
        # 1. Run EXIF extraction
        from agents.collector.tools.upload import extract_exif
        metadata = extract_exif(full_path)
        
        # 2. Get file details
        from PIL import Image
        width, height = 0, 0
        img_format = "Unknown"
        try:
            with Image.open(full_path) as img:
                width, height = img.size
                img_format = img.format
        except Exception:
            pass
            
        file_size_kb = round(os.path.getsize(full_path) / 1024, 1)
        
        # 3. Resolve location name via GPS if exists
        location_desc = "Unknown Location"
        lat = metadata["gps"]["latitude"]
        lon = metadata["gps"]["longitude"]
        if lat is not None and lon is not None:
            location_desc = f"{lat:.4f}° N, {lon:.4f}° W"
            
        return {
            "status": "success",
            "filename": filename,
            "size_kb": file_size_kb,
            "dimensions": f"{width} × {height} px",
            "format": img_format,
            "device": metadata.get("device") or "Unknown Device",
            "timestamp": metadata.get("timestamp") or "Unknown Time",
            "gps": {
                "latitude": lat,
                "longitude": lon
            },
            "location": location_desc
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/memory-bank", dependencies=[Depends(require_admin_token)])
def get_memory_bank(session_id: str = "default"):
    """Returns the raw memory bank for a session, containing contributor profiles."""
    try:
        from agents.memory.tools.memory_bank import MemoryBankStore
        store = MemoryBankStore(session_id)
        return {"status": "success", "contributors": store.data.get("contributors", {})}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/exclude-photos", dependencies=[Depends(require_admin_token)])
def exclude_photos(session_id: str = Form(...), filenames: str = Form("")):
    """Saves the list of excluded filenames for a session (comma-separated)."""
    try:
        store = SessionStore()
        # Parse comma-separated filenames
        exclude_list = [f.strip() for f in filenames.split(",") if f.strip()]
        store.update_session(session_id, {"excluded_photos": exclude_list})
        return {"status": "success", "message": f"Updated excluded list with {len(exclude_list)} photos."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
@app.post("/api/curation-chat")
async def curation_chat(request: Request):
    """
    Handles interactive curation chat queries.
    Uses gemini-2.5-flash to guide the user on curating their photos.
    """
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")
    chat_history = body.get("history", [])
    
    from google import genai
    from google.genai import types
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {"response": "GEMINI_API_KEY is not set. Curation Chat is currently disabled."}
        
    client = genai.Client(api_key=api_key)
    
    contents = []
    system_instruction = (
        "You are the MemoryWeaver Curation Chat Assistant. Your job is to guide the user "
        "on how they want to curate their photos and what highlights are important for their keepsake book.\n"
        "Ask friendly, concise, clarifying questions (2 sentences max) about the details of the journal, "
        "which memories are most important to print, and photo captions."
    )
    
    for h in chat_history:
        contents.append(
            types.Content(
                role=h["role"],
                parts=[types.Part.from_text(text=h["text"])]
            )
        )
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=message)]
        )
    )
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction
            )
        )
        return {"response": response.text.strip()}
    except Exception as e:
        return {"response": f"Sorry, I ran into an issue: {e}"}

@app.post("/upload")
async def handle_photo_upload(photos: list[UploadFile] = File(...), contributor_name: str = Form("Anonymous"), session_id: str = Form("default"), share_code: str = Form("")):
    # Uploads are credentialed by the event's share_code (embedded in the /join
    # link) rather than the admin token - family members need zero setup, but
    # strangers can't push photos into an event by guessing its session_id.
    store_check = SessionStore()
    session = store_check.get_session(session_id)
    if not session or share_code != session.get("share_code"):
        raise HTTPException(status_code=403, detail="Invalid or missing share code for this event.")

    results = []
    errors = []

    # Register contributor name in this session's Memory Bank
    from agents.memory.tools.memory_bank import MemoryBankStore
    store = MemoryBankStore(session_id)

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
            # Register the name mapping with 0 initial photos/moments
            store.upsert_contributor(info["contributor_id"], contributor_name, [], 0)
        except Exception as e:
            errors.append(f"Error uploading {photo.filename}: {str(e)}")
            
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


@app.get("/viewer", response_class=HTMLResponse)
async def serve_viewer_page():
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "viewer.html"))
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(content="<h1>Viewer page not found</h1>", status_code=404)


@app.get("/logs")
def get_pipeline_logs(session_id: str = "default"):
    """Returns the in-memory logs of the current or most recent curation run for a session."""
    return {"status": "success", "logs": pipeline_logs_by_session.get(session_id, [])}


def run_generation_background(session_id: str, limit: int, stage: str = "curate"):
    """Asynchronous background worker for the 5-agent pipeline, scoped to one session."""
    import time
    start_time = time.time()
    state = _get_progress_state(session_id)
    try:
        pipeline_logs_by_session[session_id] = []
        log_pipeline_step(session_id, f"Initiating pipeline orchestration (stage: {stage})...")

        def pipeline_progress(current, total, phase):
            state["pipeline"]["current"] = current
            state["pipeline"]["total"] = total
            state["pipeline"]["phase"] = phase

        stats = execute_trip_pipeline(
            project_root,
            session_id=session_id,
            limit=limit,
            log=lambda msg: log_pipeline_step(session_id, msg),
            progress_callback=pipeline_progress,
            stage=stage
        )

        # ==========================================
        # ### DEBUG / PERFORMANCE TRACKING SECTION ###
        # Comment this section out in production.
        # ==========================================
        elapsed = round(time.time() - start_time, 2)
        uncached_mod = stats.get("uncached_moderated", 0)
        uncached_score = stats.get("uncached_scored", 0)

        moments_count = stats.get("moments_count", 0)
        est_input_tokens = (uncached_mod * 1000) + (uncached_score * 1500) + (moments_count * 800)
        est_output_tokens = (uncached_mod * 100) + (uncached_score * 150) + (moments_count * 250)
        est_cost = (est_input_tokens * 0.000075 / 1000) + (est_output_tokens * 0.0003 / 1000)

        state["pipeline"]["summary"] = {
            "elapsed_seconds": round(elapsed, 1),
            "uncached_moderated": uncached_mod,
            "uncached_scored": uncached_score,
            "estimated_cost": round(est_cost, 4),
            "moments_count": moments_count
        }

        state["pipeline"]["status"] = "complete"

        log_pipeline_step(session_id, f"PERFORMANCE REPORT (UNCACHED RUNS ONLY):")
        log_pipeline_step(session_id, f"  - Total Elapsed Time: {elapsed} seconds")
        log_pipeline_step(session_id, f"  - Actual API Calls Made (Mod/Score): {uncached_mod}/{uncached_score}")
        log_pipeline_step(session_id, f"  - Estimated Input Tokens: {est_input_tokens}")
        log_pipeline_step(session_id, f"  - Estimated Output Tokens: {est_output_tokens}")
        log_pipeline_step(session_id, f"  - Estimated API Cost: ${est_cost:.6f} USD")
        # ==========================================

    except Exception as e:
        state["pipeline"]["status"] = "error"
        log_pipeline_step(session_id, f"CRITICAL ERROR: {str(e)}")

@app.post("/generate", dependencies=[Depends(require_admin_token)])
def run_generation_pipeline(background_tasks: BackgroundTasks, session_id: str = "default", limit: int = 50, stage: str = "curate"):
    """Triggers the curation stages (moderation, deduplication, scoring) for one session."""
    import time
    state = _get_progress_state(session_id)
    state["pipeline"] = {
        "current": 0,
        "total": 0,
        "status": "running",
        "phase": "initiating",
        "start_time": time.time()
    }
    background_tasks.add_task(run_generation_background, session_id, limit, stage)
    return {"status": "success", "message": f"Pipeline stage {stage} started with target limit {limit} in the background.", "session_id": session_id}


@app.post("/generate-narrative", dependencies=[Depends(require_admin_token)])
def run_narrative_pipeline(background_tasks: BackgroundTasks, session_id: str = "default", limit: int = 50):
    """Triggers Phase 4 and Phase 5 narrative compilation after user approves curated highlights."""
    import time
    state = _get_progress_state(session_id)
    state["pipeline"] = {
        "current": 0,
        "total": 0,
        "status": "running",
        "phase": "journaling",
        "start_time": time.time()
    }
    background_tasks.add_task(run_generation_background, session_id, limit, "narrate")
    return {"status": "success", "message": "Narrator journaling and story generation started in the background.", "session_id": session_id}


@app.get("/api/local-fs/list")
def local_fs_list(path: str = ""):
    """Lists directories and image files at a given local path to support a web-based file picker."""
    try:
        # Default to user's home directory if path is empty
        if not path:
            path = os.path.expanduser("~")
        
        path = os.path.abspath(path)
        if not os.path.exists(path) or not os.path.isdir(path):
            return {"status": "error", "message": "Invalid directory path"}
            
        items = []
        try:
            for f in os.listdir(path):
                if f.startswith('.'):
                    continue
                full_path = os.path.join(path, f)
                is_dir = os.path.isdir(full_path)
                # Filter files to only show image files, or folders
                if is_dir:
                    items.append({"name": f, "path": full_path, "is_dir": True})
                elif f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic')):
                    items.append({"name": f, "path": full_path, "is_dir": False})
        except PermissionError:
            return {"status": "error", "message": "Permission denied for this folder"}
            
        # Sort folders first, then files
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        
        parent = os.path.dirname(path) if path != "/" else "/"
        return {
            "status": "success",
            "current_path": path,
            "parent_path": parent,
            "items": items
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/local-fs/ingest", dependencies=[Depends(require_admin_token)])
def local_fs_ingest(session_id: str = Form(...), folder_path: str = Form(...)):
    """Ingests all images from a local directory path on the backend directly, avoiding HTTP browser uploads."""
    try:
        if not folder_path or not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return {"status": "error", "message": "Invalid directory path"}
            
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        
        import shutil
        supported = ('.jpg', '.jpeg', '.png', '.heic')
        copied = 0
        skipped = 0
        
        for f in os.listdir(folder_path):
            if f.startswith('.'):
                continue
            if f.lower().endswith(supported):
                src = os.path.join(folder_path, f)
                dst = os.path.join(upload_dir, f)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                    copied += 1
                else:
                    skipped += 1
                    
        return {
            "status": "success",
            "copied": copied,
            "skipped": skipped,
            "message": f"Successfully ingested {copied} photo(s) directly from disk. {skipped} duplicates skipped."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


class IngestFilesRequest(AsyncIterator): # just dummy if we want but standard JSON body is simpler:
    pass

from pydantic import BaseModel
class IngestFilesBody(BaseModel):
    session_id: str
    file_paths: list[str]

@app.post("/api/local-fs/ingest-files", dependencies=[Depends(require_admin_token)])
def local_fs_ingest_files(body: IngestFilesBody):
    """Ingests a list of specific local image file paths on the backend directly."""
    try:
        session_storage = StorageHelper(session_id=body.session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        
        import shutil
        copied = 0
        skipped = 0
        
        for src in body.file_paths:
            if not os.path.exists(src) or not os.path.isfile(src):
                continue
            f = os.path.basename(src)
            dst = os.path.join(upload_dir, f)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied += 1
            else:
                skipped += 1
                
        return {
            "status": "success",
            "copied": copied,
            "skipped": skipped,
            "message": f"Successfully ingested {copied} selected photo(s) directly from disk. {skipped} duplicates skipped."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


class SwapPhotoBody(BaseModel):
    session_id: str
    old_photo: str
    new_photo: str


@app.post("/api/swap-curated-photo", dependencies=[Depends(require_admin_token)])
def swap_curated_photo(body: SwapPhotoBody):
    import json
    try:
        session_storage = StorageHelper(session_id=body.session_id)
        artefacts_dir = os.path.join(session_storage.local_base, "artefacts")
        
        journal_path = os.path.join(artefacts_dir, "journal.json")
        highlights_path = os.path.join(artefacts_dir, "highlights.json")
        manifest_path = os.path.join(artefacts_dir, "manifest.json")
        
        if not os.path.exists(journal_path) or not os.path.exists(highlights_path):
            raise HTTPException(status_code=400, detail="Curation has not been run for this session.")
            
        with open(journal_path, "r") as f:
            journal = json.load(f)
            
        with open(highlights_path, "r") as f:
            highlights = json.load(f)
            
        # 1. Update journal.json
        for entry in journal:
            if "photos" in entry:
                entry["photos"] = [
                    body.new_photo if p == body.old_photo else p
                    for p in entry["photos"]
                ]
                
        # 2. Update highlights.json
        # Find new photo details in manifest.json
        new_photo_record = None
        if os.path.exists(manifest_path):
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
                for item in manifest:
                    if item.get("filename") == body.new_photo:
                        new_photo_record = item
                        break
                        
        if not new_photo_record:
            new_photo_record = {
                "filename": body.new_photo,
                "score": 0.0,
                "scene_label": "other",
                "caption": "User swapped highlight"
            }
            
        # Replace old photo record in highlights
        for i, hl in enumerate(highlights):
            if hl.get("filename") == body.old_photo:
                highlights[i] = new_photo_record
                
        # Save back
        with open(journal_path, "w") as f:
            json.dump(journal, f, indent=2)
            
        with open(highlights_path, "w") as f:
            json.dump(highlights, f, indent=2)
            
        return {"status": "success", "message": f"Successfully swapped {body.old_photo} with {body.new_photo}."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class MovePhotoBody(BaseModel):
    session_id: str
    filename: str
    action: str  # "promote" or "demote"


@app.post("/api/move-curated-photo", dependencies=[Depends(require_admin_token)])
def move_curated_photo(body: MovePhotoBody):
    import json
    try:
        session_storage = StorageHelper(session_id=body.session_id)
        artefacts_dir = os.path.join(session_storage.local_base, "artefacts")
        
        journal_path = os.path.join(artefacts_dir, "journal.json")
        highlights_path = os.path.join(artefacts_dir, "highlights.json")
        manifest_path = os.path.join(artefacts_dir, "manifest.json")
        
        if not os.path.exists(journal_path) or not os.path.exists(highlights_path):
            raise HTTPException(status_code=400, detail="Curation has not been run for this session.")
            
        with open(journal_path, "r") as f:
            journal = json.load(f)
            
        with open(highlights_path, "r") as f:
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
                with open(manifest_path, "r") as f:
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
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
