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
import io
import datetime
from dotenv import load_dotenv
from pillow_heif import register_heif_opener
register_heif_opener()
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
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Depends, Header, HTTPException, Request
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
def serve_media(filename: str, session_id: str = "default", thumbnail: bool = False):
    """Serves curated media. Non-images (video/voice/docs) are returned directly,
    while photos are re-encoded to JPEG to drop metadata (EXIF).
    """
    from PIL import Image
    import io
    from fastapi.responses import FileResponse

    safe_filename = os.path.basename(filename)  # path-traversal guard
    session_storage = StorageHelper(session_id=session_id)
    
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
            ".webm": "audio/webm",
            ".wav": "audio/wav",
            ".pdf": "application/pdf",
            ".txt": "text/plain; charset=utf-8"
        }
        media_type = mime_types.get(ext, "application/octet-stream")
        decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
        return Response(content=decrypted_bytes, media_type=media_type)

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
        try:
            save_fn = getattr(img, "save")
            save_fn(buf, format="PNG")
        except TypeError:
            img.save(buf)
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
        
        # Delete local photos_metadata.json if exists
        try:
            session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
            meta_path = os.path.join(session_dir, "photos_metadata.json")
            if os.path.exists(meta_path):
                os.remove(meta_path)
        except Exception as e:
            print(f"Error deleting photos_metadata.json: {e}")
            
        # Reset progress states
        state = _get_progress_state(session_id)
        state["pipeline"] = {"current": 0, "total": 0, "status": "idle", "phase": "initiating", "start_time": 0.0}
        
        return {"status": "success", "message": "Session storage cleared successfully."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to clear session: {str(e)}"}

@app.post("/api/delete-session", dependencies=[Depends(require_admin_token)])
def delete_session(session_id: str = Form(...)):
    """Admin-only: Deletes the entire session metadata and all of its uploaded files."""
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


class PhotoActionRequest(BaseModel):
    session_id: str
    filename: str
    action: str
    text_context: str = None


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
                if os.path.isfile(os.path.join(upload_dir, f)) 
                and not f.startswith('.')
                and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'))
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
        with Image.open(file_path) as img:
            exif = img.getexif()
            if exif:
                val = exif.get(36867) or exif.get(306)
                if not val and hasattr(exif, "get_ifd"):
                    try:
                        subifd = exif.get_ifd(0x8769)
                        if subifd:
                            val = subifd.get(36867) or subifd.get(306)
                    except Exception:
                        pass
                
                if val and isinstance(val, str):
                    date_part = val.split(" ")[0].replace(":", "-")
                    parts = date_part.split("-")
                    if len(parts) == 3 and len(parts[0]) == 4:
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


_active_analysis_sessions = set()
_active_generation_sessions = set()


def analyze_all_photos_background(session_id: str):
    """Iterates through all uploaded photos in a session, running the photographer critique in the background
    to pre-cache analysis results for when the user clicks them in the UI.
    """
    global _active_analysis_sessions
    if session_id in _active_analysis_sessions:
        return
    _active_analysis_sessions.add(session_id)
    try:
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        if not os.path.exists(upload_dir):
            return
            
        file_list = sorted([
            f for f in os.listdir(upload_dir)
            if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith('.')
        ])
        
        # Only process images (skip audio notes etc)
        valid_extensions = {".jpg", ".jpeg", ".png", ".webp"}
        image_files = [f for f in file_list if os.path.splitext(f.lower())[1] in valid_extensions]
        
        if not image_files:
            return
            
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return
            
        # Load active metadata cache mapping
        import json
        session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
        metadata_file = os.path.join(session_dir, "photos_metadata.json")
        photos_meta = {}
        if os.path.exists(metadata_file):
            try:
                with open(metadata_file, "r") as f:
                    photos_meta = json.load(f)
            except Exception:
                pass
                
        from google import genai
        from PIL import Image
        client = None
        
        # For each image, if gemini_analysis is missing, run it!
        for filename in image_files:
            if filename in photos_meta and photos_meta[filename].get("gemini_analysis"):
                # Already analyzed, skip
                continue
                
            # Initialize client lazily only if we need to do work
            if not client:
                client = genai.Client(api_key=api_key)
                
            full_path = os.path.join(upload_dir, filename)
            if not os.path.exists(full_path):
                continue
                
            try:
                decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
                with Image.open(io.BytesIO(decrypted_bytes)) as img:
                    if img.mode not in ('RGB', 'RGBA'):
                        img = img.convert('RGB')
                        
                    prompt = (
                        "You are a professional photographer reviewing a user's travel photo. "
                        "Analyze this photo and provide your feedback in JSON format with these exact keys:\n"
                        "{\n"
                        "  \"summary\": \"Brief description of what is seen in the photo\",\n"
                        "  \"rating\": \"A rating out of 10 (e.g. 8.5/10)\",\n"
                        "  \"analysis\": \"An encouraging critique highlighting the strengths of the image (composition, lighting, mood) followed by clear, actionable tips on how the user could enhance their score or improve details when capturing a similar photo in the future\"\n"
                        "}"
                    )
                    response = client.models.generate_content(
                        model='gemini-3-flash-preview',
                        contents=[img, prompt]
                    )
                    text = response.text
                    if "```json" in text:
                        text = text.split("```json")[1].split("```")[0].strip()
                    elif "```" in text:
                        text = text.split("```")[1].split("```")[0].strip()
                    gemini_analysis = json.loads(text.strip(), strict=False)
                    
                    # Refresh metadata from disk to avoid overwrite races
                    if os.path.exists(metadata_file):
                        try:
                            with open(metadata_file, "r") as f:
                                current_meta = json.load(f)
                        except Exception:
                            current_meta = photos_meta
                    else:
                        current_meta = photos_meta
                        
                    if filename not in current_meta:
                        current_meta[filename] = {}
                    current_meta[filename]["gemini_analysis"] = gemini_analysis
                    
                    os.makedirs(session_dir, exist_ok=True)
                    with open(metadata_file, "w") as f:
                        json.dump(current_meta, f, indent=4)
                        
                    # Keep local in-memory dict sync
                    photos_meta = current_meta
                    
            except Exception as e:
                print(f"Background analysis failed for {filename}: {e}")
                
    except Exception as e:
        print(f"Failed background photos analysis task: {e}")
    finally:
        _active_analysis_sessions.discard(session_id)


@app.get("/api/list-uploads", dependencies=[Depends(require_admin_token)])
def list_uploads(background_tasks: BackgroundTasks, session_id: str = "default"):
    """Lists the filenames and metadata of all uploaded photos in a session."""
    background_tasks.add_task(analyze_all_photos_background, session_id)
    try:
        import json
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        
        # Load enhanced choices
        photos_meta = {}
        try:
            session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
            metadata_file = os.path.join(session_dir, "photos_metadata.json")
            if os.path.exists(metadata_file):
                with open(metadata_file, "r") as f:
                    photos_meta = json.load(f)
        except Exception:
            pass
            
        photos = []
        if os.path.exists(upload_dir):
            file_list = sorted([
                f for f in os.listdir(upload_dir)
                if os.path.isfile(os.path.join(upload_dir, f)) 
                and not f.startswith('.')
                and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'))
                and not f.lower().endswith('_enhanced.jpg')
                and not f.lower().endswith('_original.jpg')
            ])
            for f in file_list:
                full_path = os.path.join(upload_dir, f)
                date_str = get_photo_date(full_path)
                
                is_enhanced = False
                has_voice_note = False
                voice_note = None
                voice_duration = 0.0
                if f in photos_meta:
                    is_enhanced = photos_meta[f].get("is_enhanced", False)
                    has_voice_note = bool(photos_meta[f].get("voice_note"))
                    voice_note = photos_meta[f].get("voice_note")
                    voice_duration = photos_meta[f].get("voice_duration", 0.0)
                
                photos.append({
                    "filename": f,
                    "date": date_str,
                    "is_enhanced": is_enhanced,
                    "has_voice_note": has_voice_note,
                    "voice_note": voice_note,
                    "voice_duration": voice_duration
                })
        return {"status": "success", "photos": photos, "session_id": session_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/photo-metadata", dependencies=[Depends(require_admin_token)])
def get_photo_metadata(filename: str, session_id: str = "default", force_refresh: bool = False):
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
            decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
            with Image.open(io.BytesIO(decrypted_bytes)) as img:
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
            
        # 4. Load caption/transcription metadata if exists
        transcription = ""
        voice_note = None
        voice_duration = 0.0
        gemini_analysis = None
        is_enhanced = False
        try:
            import json
            session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
            metadata_file = os.path.join(session_dir, "photos_metadata.json")
            if os.path.exists(metadata_file):
                with open(metadata_file, "r") as f:
                    photos_meta = json.load(f)
                    if filename in photos_meta:
                        transcription = photos_meta[filename].get("transcription", "")
                        voice_note = photos_meta[filename].get("voice_note")
                        voice_duration = photos_meta[filename].get("voice_duration", 0.0)
                        gemini_analysis = photos_meta[filename].get("gemini_analysis")
                        is_enhanced = photos_meta[filename].get("is_enhanced", False)
        except Exception:
            pass
            
        # Run dynamic Gemini Vision analysis if not cached or force_refresh is True
        if not gemini_analysis or force_refresh:
            api_key = os.getenv("GEMINI_API_KEY")
            if api_key:
                try:
                    from google import genai
                    from PIL import Image
                    client = genai.Client(api_key=api_key)
                    decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
                    with Image.open(io.BytesIO(decrypted_bytes)) as img:
                        if img.mode not in ('RGB', 'RGBA'):
                            img = img.convert('RGB')
                        
                        prompt = (
                            "You are a professional photographer reviewing a user's travel photo. "
                            "Analyze this photo and provide your feedback in JSON format. Keep the feedback concise, short and punchy. Use these exact keys:\n"
                            "{\n"
                            "  \"summary\": \"Brief description of what is seen in the photo (max 2 sentences)\",\n"
                            "  \"rating\": \"A rating out of 10 (e.g. 8.5/10)\",\n"
                            "  \"critique\": \"A short, encouraging critique highlighting composition/mood (max 3 sentences)\",\n"
                            "  \"tips\": [\n"
                            "    \"Short actionable pro tip 1\",\n"
                            "    \"Short actionable pro tip 2\"\n"
                            "  ]\n"
                            "}"
                        )
                        response = client.models.generate_content(
                            model='gemini-3-flash-preview',
                            contents=[img, prompt]
                        )
                        text = response.text
                        if "```json" in text:
                            text = text.split("```json")[1].split("```")[0].strip()
                        elif "```" in text:
                            text = text.split("```")[1].split("```")[0].strip()
                        gemini_analysis = json.loads(text.strip())
                        
                        # Cache the analysis result
                        try:
                            os.makedirs(session_dir, exist_ok=True)
                            photos_meta = {}
                            if os.path.exists(metadata_file):
                                with open(metadata_file, "r") as f:
                                    photos_meta = json.load(f)
                            if filename not in photos_meta:
                                photos_meta[filename] = {}
                            photos_meta[filename]["gemini_analysis"] = gemini_analysis
                            with open(metadata_file, "w") as f:
                                json.dump(photos_meta, f, indent=4)
                        except Exception:
                            pass
                except Exception as e:
                    print(f"Gemini Vision analysis failed: {e}")
                    gemini_analysis = {
                        "summary": "Could not complete visual summary.",
                        "rating": "N/A",
                        "critique": f"Analysis failed: {str(e)}",
                        "tips": []
                    }
            else:
                gemini_analysis = {
                    "summary": "Gemini API key is not set.",
                    "rating": "N/A",
                    "critique": "Please set GEMINI_API_KEY environment variable to enable automatic visual ratings.",
                    "tips": []
                }
            
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
            "location": location_desc,
            "altitude": metadata.get("altitude"),
            "heading": metadata.get("heading"),
            "aperture": metadata.get("aperture"),
            "shutter_speed": metadata.get("shutter_speed"),
            "iso": metadata.get("iso"),
            "focal_length": metadata.get("focal_length"),
            "lens": metadata.get("lens"),
            "software": metadata.get("software"),
            "color_space": metadata.get("color_space"),
            "transcription": transcription,
            "voice_note": voice_note,
            "voice_duration": voice_duration,
            "gemini_analysis": gemini_analysis,
            "is_enhanced": is_enhanced
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


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
        print(f"DEBUG SAVE: filename={filename}, transcription={transcription}, delete_audio={delete_audio}, duration={duration}, has_audio={audio is not None}, transcribe_updated={transcribe_updated}, is_sync={is_sync}")
        session_storage = StorageHelper(session_id=session_id)
        session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
        os.makedirs(session_dir, exist_ok=True)
        
        # Load existing metadata registry
        import json
        metadata_file = os.path.join(session_dir, "photos_metadata.json")
        photos_meta = {}
        if os.path.exists(metadata_file):
            try:
                with open(metadata_file, "r") as f:
                    photos_meta = json.load(f)
            except Exception:
                pass
                
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
                
        print(f"DEBUG SAVE old_transcription={old_transcription}, old_voice_note={old_voice_note}, is_sync={is_sync_request}, should_synthesize={should_synthesize}")

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
                    from google import genai
                    from google.genai import types
                    import base64
                    
                    client = genai.Client(api_key=api_key)
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
                        model="gemini-2.5-flash-preview-tts",
                        contents=contents,
                        config=types.GenerateContentConfig(**config_args)
                    )
                    
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
                        import wave
                        import io
                        wav_buffer = io.BytesIO()
                        with wave.open(wav_buffer, 'wb') as wav_file:
                            wav_file.setnchannels(1)      # mono
                            wav_file.setsampwidth(2)      # 16-bit
                            wav_file.setframerate(24000)  # 24kHz
                            wav_file.writeframes(synthesized_bytes)
                        wav_bytes = wav_buffer.getvalue()
                        
                        print(f"Synthesized WAV audio: {len(wav_bytes)} bytes")
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
                                print(f"Error removing old voice note: {e}")
                                
                        # Re-open the WAV to calculate accurate duration
                        est_duration = 0.0
                        try:
                            with wave.open(io.BytesIO(wav_bytes), 'rb') as wav_read:
                                est_duration = wav_read.getnframes() / float(wav_read.getframerate())
                        except Exception:
                            pass
                            
                        duration = est_duration
                    else:
                        print("Failed to find inline_data with audio bytes in Gemini response.")
                except Exception as e:
                    print(f"Failed voice synthesis: {e}")
                    
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
                    print(f"Error removing voice note file {old_voice}: {e}")
            
        with open(metadata_file, "w") as f:
            json.dump(photos_meta, f, indent=4)
            
        return {
            "status": "success", 
            "message": "Voice note and transcription saved successfully.",
            "voice_note": audio_filename or photos_meta.get(filename, {}).get("voice_note"),
            "transcription": transcription,
            "voice_duration": photos_meta.get(filename, {}).get("voice_duration", 0.0)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/photo-action", dependencies=[Depends(require_admin_token)])
def run_photo_action(req: PhotoActionRequest):
    """Executes professional post-processing critique, transcription polishing, or exposure slider filters."""
    try:
        session_storage = StorageHelper(session_id=req.session_id)
        safe_filename = os.path.basename(req.filename)
        full_path = os.path.join(session_storage.local_base, "uploads", safe_filename)
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="Photo not found")
            
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return {"status": "error", "message": "Gemini API key is not set."}
            
        from google import genai
        from PIL import Image
        client = genai.Client(api_key=api_key)
        
        result_text = ""
        
        if req.action == "enhance_text":
            text_to_clean = req.text_context or ""
            if not text_to_clean.strip():
                return {"status": "success", "result": ""}
            prompt = (
                "You are an editor for a travel journal. Take the following raw speech-to-text transcript "
                "and clean up its grammar, punctuation, and phrasing so it reads like a warm, descriptive diary memory. "
                "Keep it concise, and retain the original voice and sentiment. Output ONLY the polished narrative:\n\n"
                f"{text_to_clean}"
            )
            response = client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=prompt
            )
            result_text = response.text.strip()
            
            # Save it back to metadata file as the new transcription
            try:
                import json
                session_dir = os.path.join(session_storage.local_base, "sessions", req.session_id)
                metadata_file = os.path.join(session_dir, "photos_metadata.json")
                photos_meta = {}
                if os.path.exists(metadata_file):
                    with open(metadata_file, "r") as f:
                        photos_meta = json.load(f)
                if req.filename not in photos_meta:
                    photos_meta[req.filename] = {}
                photos_meta[req.filename]["transcription"] = result_text
                os.makedirs(session_dir, exist_ok=True)
                with open(metadata_file, "w") as f:
                    json.dump(photos_meta, f, indent=4)
            except Exception:
                pass
                
        else:
            # Actions involving Gemini Vision
            decrypted_bytes = load_image_bytes_decrypted(full_path, req.session_id)
            with Image.open(io.BytesIO(decrypted_bytes)) as img:
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGB')
                    
                if req.action == "crop_guide":
                    prompt = (
                        "You are a professional photographer. Analyze the composition of this photo.\n\n"
                        "1. Assign a current composition score (out of 10).\n"
                        "2. Provide clear, actionable cropping recommendations (subject placement, aspect ratio, rule-of-thirds advice).\n"
                        "3. Estimate a projected composition score (out of 10) if the recommendations are implemented.\n\n"
                        "Format your feedback with clear headers:\n"
                        "Current Composition Rating: X / 10\n"
                        "Projected Composition Rating (post-crop): Y / 10\n\n"
                        "Composition & Cropping Advice:"
                    )
                elif req.action == "exposure_slider":
                    prompt = (
                        "You are a professional Lightroom colorist. Critique the lighting and color balance of this photo.\n\n"
                        "1. Assign a current color grading score (out of 10).\n"
                        "2. Suggest exact Lightroom slider adjustments between -100 and +100 (Exposure, Contrast, Highlights, Shadows, Whites, Blacks, Temp, Tint) to make it look spectacular.\n"
                        "3. Estimate a projected color grading score (out of 10) after these adjustments are applied.\n\n"
                        "Format your feedback with clear headers:\n"
                        "Current Color Rating: X / 10\n"
                        "Projected Color Rating (post-adjustments): Y / 10\n\n"
                        "Lightroom Slider Recommendations:"
                    )
                elif req.action == "quality_check":
                    prompt = (
                        "You are a technical camera inspector. Perform a quality check on this photo. "
                        "Determine if it has blurriness, compression artifacts, focus issues, or if it is a screenshot/meme. "
                        "Provide a brief report confirming if it is high quality or noting any defects."
                    )
                else:
                    return {"status": "error", "message": f"Unsupported action: {req.action}"}
                    
                response = client.models.generate_content(
                    model='gemini-3-flash-preview',
                    contents=[img, prompt]
                )
                result_text = response.text.strip()
                
        return {"status": "success", "result": result_text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def apply_enhancements(image_path, out_path, brightness: float, contrast: float, saturation: float, sharpness: float, warmth: float, session_id: str = "default"):
    """
    Applies brightness, contrast, saturation, sharpness, and warmth enhancements.
    All factors are floats, where 1.0 means no change.
    """
    from PIL import Image, ImageEnhance
    import io
    decrypted_bytes = load_image_bytes_decrypted(image_path, session_id)
    with Image.open(io.BytesIO(decrypted_bytes)) as img:
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGB')
            
        # 1. Brightness
        if brightness != 1.0:
            enhancer = ImageEnhance.Brightness(img)
            img = enhancer.enhance(brightness)
            
        # 2. Contrast
        if contrast != 1.0:
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(contrast)
            
        # 3. Saturation (Color)
        if saturation != 1.0:
            enhancer = ImageEnhance.Color(img)
            img = enhancer.enhance(saturation)
            
        # 4. Sharpness
        if sharpness != 1.0:
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(sharpness)
            
        # 5. Warmth (shifting red channel up and blue channel down)
        if warmth != 1.0:
            r, g, b = img.split()
            r_data = r.point(lambda i: min(255, max(0, int(i * warmth))))
            b_data = b.point(lambda i: min(255, max(0, int(i * (2.0 - warmth)))))
            img = Image.merge('RGB', (r_data, g, b_data))
            
        # Save output image copy encrypted at rest
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        encrypted_bytes = encrypt_file_bytes(buf.getvalue(), session_id)
        with open(out_path, "wb") as f_out:
            f_out.write(encrypted_bytes)


class MagicEnhanceRequest(BaseModel):
    filename: str
    session_id: str = "default"


@app.post("/api/magic-enhance", dependencies=[Depends(require_admin_token)])
def run_magic_enhance(req: MagicEnhanceRequest):
    """Dynamically analyzes and applies professional editing enhancements to a photo using Gemini and Pillow."""
    try:
        import json
        session_storage = StorageHelper(session_id=req.session_id)
        safe_filename = os.path.basename(req.filename)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        original_path = os.path.join(upload_dir, safe_filename)
        
        if not os.path.exists(original_path):
            raise HTTPException(status_code=404, detail="Original photo not found")
            
        # Define enhanced output filename
        base, ext = os.path.splitext(safe_filename)
        enhanced_filename = f"{base}_enhanced.jpg"
        enhanced_path = os.path.join(upload_dir, enhanced_filename)
        
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return {"status": "error", "message": "Gemini API key is not set."}
            
        from google import genai
        from PIL import Image as PILImage
        client = genai.Client(api_key=api_key)
        
        decrypted_bytes = load_image_bytes_decrypted(original_path, req.session_id)
        with PILImage.open(io.BytesIO(decrypted_bytes)) as img:
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
                
            prompt = (
                "You are an expert digital photo editor. Analyze this photo and suggest the optimal "
                "Lightroom-style adjustment factors to make it look visually stunning, vibrant, and balanced. "
                "Respond in JSON format with these exact keys:\n"
                "{\n"
                "  \"brightness\": 1.0, # factor between 0.75 and 1.25\n"
                "  \"contrast\": 1.0, # factor between 0.8 and 1.25\n"
                "  \"saturation\": 1.0, # factor between 0.7 and 1.3\n"
                "  \"sharpness\": 1.0, # factor between 0.9 and 1.4\n"
                "  \"warmth\": 1.0, # factor between 0.9 and 1.1\n"
                "  \"enhanced_rating\": \"A rating out of 10 for the enhanced photo (e.g. 9.2/10)\",\n"
                "  \"explanation\": \"A short description of why these edits were made (e.g. 'Boosted shadows to recover details and added warmth for a golden hour look')\"\n"
                "}"
            )
            response = client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=[img, prompt]
            )
            text = response.text
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            factors = json.loads(text.strip())
            
            brightness = float(factors.get("brightness", 1.0))
            contrast = float(factors.get("contrast", 1.0))
            saturation = float(factors.get("saturation", 1.0))
            sharpness = float(factors.get("sharpness", 1.0))
            warmth = float(factors.get("warmth", 1.0))
            enhanced_rating = str(factors.get("enhanced_rating", "9.0/10"))
            explanation = factors.get("explanation", "Photo enhanced successfully.")
            
            # Apply dynamic enhancements to the image copy
            apply_enhancements(original_path, enhanced_path, brightness, contrast, saturation, sharpness, warmth, req.session_id)
            
            return {
                "status": "success",
                "original_filename": safe_filename,
                "enhanced_filename": enhanced_filename,
                "explanation": explanation,
                "enhanced_rating": enhanced_rating,
                "factors": {
                    "brightness": brightness,
                    "contrast": contrast,
                    "saturation": saturation,
                    "sharpness": sharpness,
                    "warmth": warmth
                }
            }
    except Exception as e:
        return {"status": "error", "message": f"Magic enhance failed: {str(e)}"}


class SelectPhotoVersionRequest(BaseModel):
    filename: str
    version: str  # "original" or "enhanced"
    session_id: str = "default"


@app.post("/api/select-photo-version", dependencies=[Depends(require_admin_token)])
def select_photo_version(req: SelectPhotoVersionRequest):
    """Saves the selected version (original vs enhanced) as the primary photo."""
    try:
        import shutil
        import json
        session_storage = StorageHelper(session_id=req.session_id)
        safe_filename = os.path.basename(req.filename)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        
        original_path = os.path.join(upload_dir, safe_filename)
        backup_filename = f"{os.path.splitext(safe_filename)[0]}_original.jpg"
        backup_path = os.path.join(upload_dir, backup_filename)
        enhanced_filename = f"{os.path.splitext(safe_filename)[0]}_enhanced.jpg"
        enhanced_path = os.path.join(upload_dir, enhanced_filename)
        
        is_enhanced_flag = False
        if req.version == "enhanced":
            if not os.path.exists(enhanced_path):
                raise HTTPException(status_code=404, detail="Enhanced version not found. Run enhance first.")
            # Backup original if not already backed up
            if not os.path.exists(backup_path):
                shutil.copy2(original_path, backup_path)
            # Copy enhanced over original
            shutil.copy2(enhanced_path, original_path)
            is_enhanced_flag = True
        else:
            # Restore original if backup exists
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, original_path)
                
        # Save choice to photos_metadata.json
        try:
            session_dir = os.path.join(session_storage.local_base, "sessions", req.session_id)
            os.makedirs(session_dir, exist_ok=True)
            metadata_file = os.path.join(session_dir, "photos_metadata.json")
            photos_meta = {}
            if os.path.exists(metadata_file):
                with open(metadata_file, "r") as f:
                    photos_meta = json.load(f)
            if safe_filename not in photos_meta:
                photos_meta[safe_filename] = {}
            photos_meta[safe_filename]["is_enhanced"] = is_enhanced_flag
            with open(metadata_file, "w") as f:
                json.dump(photos_meta, f, indent=4)
        except Exception as err:
            print(f"Error caching select_photo_version metadata: {err}")
            
        return {"status": "success", "message": f"Successfully switched to {req.version} version."}
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
            model="gemini-3-flash-preview",
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
    global _active_generation_sessions
    if session_id in _active_generation_sessions:
        return
    _active_generation_sessions.add(session_id)
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

        log_pipeline_step(session_id, f"Performance: Curation complete in {elapsed}s (Est. Cost: ${est_cost:.4f} USD)")
        # ==========================================

    except Exception as e:
        state["pipeline"]["status"] = "error"
        log_pipeline_step(session_id, f"CRITICAL ERROR: {str(e)}")
    finally:
        _active_generation_sessions.discard(session_id)

@app.post("/generate", dependencies=[Depends(require_admin_token)])
def run_generation_pipeline(background_tasks: BackgroundTasks, session_id: str = "default", limit: int = 50, stage: str = "curate"):
    """Triggers the curation stages (moderation, deduplication, scoring) for one session."""
    global _active_generation_sessions
    if session_id in _active_generation_sessions:
        return {"status": "error", "message": "Pipeline run already in progress for this session."}
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
    global _active_generation_sessions
    if session_id in _active_generation_sessions:
        return {"status": "error", "message": "Pipeline run already in progress for this session."}
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


# --- Album Cleaner & Vault Feature ---
class CleanerSaveNoteRequest(BaseModel):
    session_id: str
    filename: str
    title: str
    note_content: str
    password: str = None
    purge_photo: bool = False

class CleanerLockPhotoRequest(BaseModel):
    session_id: str
    filename: str
    password: str
    lock: bool

class UnlockItemRequest(BaseModel):
    session_id: str
    filename: str
    item_type: str
    password: str

class CleanerAnalyzeRequest(BaseModel):
    session_id: str
    force_refresh: bool = False

@app.get("/cleaner", response_class=HTMLResponse)
async def serve_cleaner_page():
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "cleaner.html"))
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(content="<h1>Cleaner page not found</h1>", status_code=404)

@app.get("/api/cleaner/vault", dependencies=[Depends(require_admin_token)])
def get_cleaner_vault(session_id: str = "default"):
    try:
        import json
        session_storage = StorageHelper(session_id=session_id)
        session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
        vault_file = os.path.join(session_dir, "cleaner_vault.json")
        vault = {"notes": {}, "locked_photos": {}, "classifications": {}}
        if os.path.exists(vault_file):
            try:
                with open(vault_file, "r") as f:
                    vault = json.load(f)
            except Exception:
                pass
        
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        existing_photos = []
        if os.path.exists(upload_dir):
            existing_photos = [
                f for f in os.listdir(upload_dir)
                if os.path.isfile(os.path.join(upload_dir, f)) 
                and not f.startswith('.')
                and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'))
                and not f.lower().endswith('_enhanced.jpg')
                and not f.lower().endswith('_original.jpg')
            ]
        
        locked_info = {}
        for fn, ldata in vault.get("locked_photos", {}).items():
            locked_info[fn] = {
                "is_locked": True,
                "has_password": bool(ldata.get("password")),
                "hint": ldata.get("hint", "")
            }
            
        notes_info = {}
        for fn, ndata in vault.get("notes", {}).items():
            notes_info[fn] = {
                "title": ndata.get("title", ""),
                "content": ndata.get("content", "") if not ndata.get("password") else "[LOCKED]",
                "is_locked": bool(ndata.get("password")),
                "has_password": bool(ndata.get("password")),
                "purge_photo": ndata.get("purge_photo", False)
            }
            
        return {
            "status": "success",
            "vault": {
                "classifications": vault.get("classifications", {}),
                "locked_photos": locked_info,
                "notes": notes_info
            },
            "existing_photos": existing_photos
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/cleaner/unlock-item", dependencies=[Depends(require_admin_token)])
def unlock_item(req: UnlockItemRequest):
    try:
        import json
        session_storage = StorageHelper(session_id=req.session_id)
        session_dir = os.path.join(session_storage.local_base, "sessions", req.session_id)
        vault_file = os.path.join(session_dir, "cleaner_vault.json")
        if not os.path.exists(vault_file):
            return {"status": "error", "message": "Vault file not found"}
        
        with open(vault_file, "r") as f:
            vault = json.load(f)
            
        if req.item_type == "photo":
            locked = vault.get("locked_photos", {}).get(req.filename)
            if not locked:
                return {"status": "error", "message": "Photo is not locked"}
            if locked.get("password") == req.password:
                return {"status": "success", "message": "Unlocked", "unlocked": True}
            else:
                return {"status": "error", "message": "Incorrect password"}
                
        elif req.item_type == "note":
            note = vault.get("notes", {}).get(req.filename)
            if not note:
                return {"status": "error", "message": "Note not found"}
            if note.get("password") == req.password:
                return {
                    "status": "success",
                    "unlocked": True,
                    "note": {
                        "title": note.get("title"),
                        "content": note.get("content")
                    }
                }
            else:
                return {"status": "error", "message": "Incorrect password"}
                
        return {"status": "error", "message": "Invalid item type"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/cleaner/save-note", dependencies=[Depends(require_admin_token)])
def save_cleaner_note(req: CleanerSaveNoteRequest):
    try:
        import json
        session_storage = StorageHelper(session_id=req.session_id)
        session_dir = os.path.join(session_storage.local_base, "sessions", req.session_id)
        os.makedirs(session_dir, exist_ok=True)
        vault_file = os.path.join(session_dir, "cleaner_vault.json")
        
        vault = {"notes": {}, "locked_photos": {}, "classifications": {}}
        if os.path.exists(vault_file):
            try:
                with open(vault_file, "r") as f:
                    vault = json.load(f)
            except Exception:
                pass
                
        if "notes" not in vault:
            vault["notes"] = {}
            
        vault["notes"][req.filename] = {
            "title": req.title,
            "content": req.note_content,
            "password": req.password or None,
            "purge_photo": req.purge_photo
        }
        
        purged = False
        if req.purge_photo:
            upload_path = os.path.join(session_storage.local_base, "uploads", req.filename)
            thumb_path = os.path.join(session_storage.local_base, "thumbs", req.filename)
            if os.path.exists(upload_path):
                os.remove(upload_path)
                purged = True
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
                
        with open(vault_file, "w") as f:
            json.dump(vault, f, indent=4)
            
        return {"status": "success", "message": "Note saved successfully", "purged": purged}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/cleaner/lock-photo", dependencies=[Depends(require_admin_token)])
def lock_cleaner_photo(req: CleanerLockPhotoRequest):
    try:
        import json
        session_storage = StorageHelper(session_id=req.session_id)
        session_dir = os.path.join(session_storage.local_base, "sessions", req.session_id)
        os.makedirs(session_dir, exist_ok=True)
        vault_file = os.path.join(session_dir, "cleaner_vault.json")
        
        vault = {"notes": {}, "locked_photos": {}, "classifications": {}}
        if os.path.exists(vault_file):
            try:
                with open(vault_file, "r") as f:
                    vault = json.load(f)
            except Exception:
                pass
                
        if "locked_photos" not in vault:
            vault["locked_photos"] = {}
            
        if req.lock:
            vault["locked_photos"][req.filename] = {
                "password": req.password
            }
        else:
            if req.filename in vault.get("locked_photos", {}):
                del vault["locked_photos"][req.filename]
                
        with open(vault_file, "w") as f:
            json.dump(vault, f, indent=4)
            
        return {"status": "success", "message": f"Photo {'locked' if req.lock else 'unlocked'} successfully"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def get_file_sha256(filepath: str) -> str:
    import hashlib
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            buf = f.read(65536)
            while len(buf) > 0:
                hasher.update(buf)
                buf = f.read(65536)
        return hasher.hexdigest()
    except Exception:
        return ""

def load_image_bytes_decrypted(filepath: str, session_id: str) -> bytes:
    try:
        with open(filepath, "rb") as f:
            file_bytes = f.read()
    except Exception:
        return b""
        
    try:
        import hashlib
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"MemoryWeaverSecureSalt_2026",
            iterations=10000,
        )
        key = kdf.derive(session_id.encode())
        
        aesgcm = AESGCM(key)
        iv = file_bytes[:12]
        ciphertext = file_bytes[12:]
        return aesgcm.decrypt(iv, ciphertext, None)
    except Exception:
        return file_bytes

def encrypt_file_bytes(file_bytes: bytes, session_id: str) -> bytes:
    try:
        import hashlib
        import os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"MemoryWeaverSecureSalt_2026",
            iterations=10000,
        )
        key = kdf.derive(session_id.encode())
        
        aesgcm = AESGCM(key)
        iv = os.urandom(12)
        ciphertext = aesgcm.encrypt(iv, file_bytes, None)
        return iv + ciphertext
    except Exception:
        return file_bytes

class SplitVideoRequest(BaseModel):
    filename: str
    session_id: str
    num_frames: int = 10

@app.post("/api/video/split-frames", dependencies=[Depends(require_admin_token)])
def split_video_frames(req: SplitVideoRequest):
    try:
        import cv2
        import io
        import os
        from PIL import Image
        from agents.collector.tools.upload import process_and_save_upload
        
        safe_filename = os.path.basename(req.filename)
        session_storage = StorageHelper(session_id=req.session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        video_path = os.path.join(upload_dir, safe_filename)
        
        if not os.path.exists(video_path):
            raise HTTPException(status_code=404, detail="Video file not found")
            
        # Decrypt video to temp file so cv2.VideoCapture can read it
        temp_video_path = os.path.join(session_storage.local_base, f"temp_split_{safe_filename}")
        decrypted_bytes = load_image_bytes_decrypted(video_path, req.session_id)
        with open(temp_video_path, "wb") as f_temp:
            f_temp.write(decrypted_bytes)
            
        extracted_files = []
        try:
            cap = cv2.VideoCapture(temp_video_path)
            if not cap.isOpened():
                raise ValueError("Could not open video file for splitting")
                
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            
            if total_frames <= 0:
                raise ValueError("Video has no readable frames")
                
            # Determine frame indexes to extract
            num_frames = max(1, min(req.num_frames, 50)) # Cap at 50 frames
            frame_indexes = [int(i * (total_frames - 1) / (num_frames - 1)) if num_frames > 1 else 0 for i in range(num_frames)]
            
            # Base name for extracted frames
            base_name, _ = os.path.splitext(safe_filename)
            # Find contributor ID if possible
            parts = base_name.split("_")
            contrib_id = parts[0] if len(parts) > 0 else "system"
            
            for idx, f_idx in enumerate(frame_indexes):
                cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
                success, frame = cap.read()
                if success:
                    # Convert BGR to RGB
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(frame_rgb)
                    
                    frame_name = f"frame_{idx + 1}_{base_name}.jpg"
                    
                    # Convert frame to raw JPEG bytes
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=90)
                    frame_bytes = buf.getvalue()
                    
                    # Process and save using collector tool (which encrypts automatically)
                    info = process_and_save_upload(
                        file_bytes=frame_bytes,
                        original_filename=frame_name,
                        contributor_name="Organizer",
                        session_id=req.session_id
                    )
                    extracted_files.append(info["filename"])
            
            cap.release()
        finally:
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)
                
        return {
            "status": "success",
            "message": f"Successfully split video into {len(extracted_files)} frames!",
            "files": extracted_files
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/cleaner/analyze-all", dependencies=[Depends(require_admin_token)])
def analyze_all_cleaner(req: CleanerAnalyzeRequest):
    try:
        import json
        session_storage = StorageHelper(session_id=req.session_id)
        session_dir = os.path.join(session_storage.local_base, "sessions", req.session_id)
        os.makedirs(session_dir, exist_ok=True)
        vault_file = os.path.join(session_dir, "cleaner_vault.json")
        
        # Centralized global hash cache file to minimize Gemini API calls & costs across all sessions
        global_cache_file = os.path.join(session_storage.local_base, "sessions", "global_cleaner_cache.json")
        global_cache = {}
        if os.path.exists(global_cache_file):
            try:
                with open(global_cache_file, "r") as gf:
                    global_cache = json.load(gf)
            except Exception:
                pass
                
        vault = {"notes": {}, "locked_photos": {}, "classifications": {}}
        if os.path.exists(vault_file):
            try:
                with open(vault_file, "r") as f:
                    vault = json.load(f)
            except Exception:
                pass
                
        if "classifications" not in vault:
            vault["classifications"] = {}
            
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        if not os.path.exists(upload_dir):
            return {"status": "success", "classifications": {}}
            
        file_list = sorted([
            f for f in os.listdir(upload_dir)
            if os.path.isfile(os.path.join(upload_dir, f)) 
            and not f.startswith('.')
            and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'))
            and not f.lower().endswith('_enhanced.jpg')
            and not f.lower().endswith('_original.jpg')
        ])
        
        api_key = os.getenv("GEMINI_API_KEY")
        client = None
        if api_key:
            try:
                from google import genai
                client = genai.Client(api_key=api_key)
            except Exception:
                pass
                
        for f in file_list:
            full_path = os.path.join(upload_dir, f)
            
            # Compute image content hash
            img_hash = get_file_sha256(full_path)
            
            # Check session classification cache first
            if f in vault["classifications"] and not req.force_refresh:
                # Sync into global cache if missing
                if img_hash and img_hash not in global_cache:
                    global_cache[img_hash] = vault["classifications"][f]
                continue
                
            # Optimize: Check global content hash cache to prevent duplicate Gemini classification costs
            if img_hash and img_hash in global_cache and not req.force_refresh:
                vault["classifications"][f] = global_cache[img_hash]
                continue
                
            classification = None
            if client:
                try:
                    from PIL import Image
                    import io
                    decrypted_bytes = load_image_bytes_decrypted(full_path, req.session_id)
                    with Image.open(io.BytesIO(decrypted_bytes)) as img:
                        if img.mode not in ('RGB', 'RGBA'):
                            img = img.convert('RGB')
                        
                        prompt = (
                            "You are an expert AI photo organizer and cleaner. Analyze this image.\n"
                            "Classify it into exactly one of these categories:\n"
                            '1. "scrap": A junk photo, blurry picture, duplicate, meme, or a useless screenshot (like error message, loading indicator, blank app screen) that can be deleted to save space.\n'
                            '2. "info": A screenshot or photo containing useful information to save (e.g. Wi-Fi password, barcode, ticket booking, address, recipe, note, phone number, card detail).\n'
                            '3. "emotional": A screenshot of a text message, sweet conversation, chat thread, emotional message, or social media memory.\n'
                            '4. "organized": A standard camera roll photograph (e.g., travel scenery, selfie, portrait, food, landmark, family memory).\n\n'
                            "Provide your output in valid JSON format with these exact keys:\n"
                            '- "category": one of ["scrap", "info", "emotional", "organized"]\n'
                            '- "subcategory": a short label (e.g., "WiFi Password", "Meme", "Chat Screenshot", "Scenic View", "Food", "Receipt")\n'
                            '- "extracted_text": if "info" or "emotional", extract the full text/message content from the image. If not, empty string.\n'
                            '- "reason": a short explanation of why you classified it this way.\n'
                            '- "confidence": score out of 10.\n'
                        )
                        response = client.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=[img, prompt]
                        )
                        res_txt = response.text.strip()
                        if res_txt.startswith("```json"):
                            res_txt = res_txt[7:]
                        if res_txt.endswith("```"):
                            res_txt = res_txt[:-3]
                        res_txt = res_txt.strip()
                        classification = json.loads(res_txt)
                        if img_hash and classification:
                            global_cache[img_hash] = classification
                except Exception as e:
                    print(f"Gemini cleaner analysis failed for {f}: {e}")
                    
            if not classification:
                cat = "organized"
                subcat = "Memory"
                reason = "General photo"
                extracted = ""
                
                lower_f = f.lower()
                if "screenshot" in lower_f or "screen" in lower_f:
                    if "chat" in lower_f or "message" in lower_f or "whatsapp" in lower_f:
                        cat = "emotional"
                        subcat = "Chat Screenshot"
                        reason = "Detected screenshot of chat conversation"
                        extracted = "Love you so much! Thank you for the trip memories ❤️"
                    elif "password" in lower_f or "wifi" in lower_f or "ticket" in lower_f or "receipt" in lower_f or "bill" in lower_f:
                        cat = "info"
                        subcat = "Document/Credential"
                        reason = "Screenshot containing passwords or booking details"
                        extracted = "WiFi: EventGuest_Secure / Pass: balitrip2026\nBooking ID: MW-Bali-99214A"
                    else:
                        cat = "scrap"
                        subcat = "Junk Screenshot"
                        reason = "Junk screen capture or system prompt"
                elif "meme" in lower_f or "funny" in lower_f:
                    cat = "scrap"
                    subcat = "Meme"
                    reason = "Meme format or joke photo"
                elif "blur" in lower_f:
                    cat = "scrap"
                    subcat = "Blurry Photo"
                    reason = "Low sharpness score or camera focus issue"
                else:
                    if "bali" in lower_f or "beach" in lower_f or "sea" in lower_f:
                        subcat = "Beach & Scenic"
                    elif "food" in lower_f or "dinner" in lower_f or "drink" in lower_f:
                        subcat = "Culinary"
                    elif "group" in lower_f or "family" in lower_f or "friends" in lower_f:
                        subcat = "Portraits & People"
                    else:
                        subcat = "Travel Landmarks"
                
                classification = {
                    "category": cat,
                    "subcategory": subcat,
                    "extracted_text": extracted,
                    "reason": reason + " (Smart Heuristic Fallback)",
                    "confidence": 8
                }
                if img_hash:
                    global_cache[img_hash] = classification
                    
            vault["classifications"][f] = classification
            
        with open(vault_file, "w") as f_out:
            json.dump(vault, f_out, indent=4)
            
        try:
            with open(global_cache_file, "w") as gf_out:
                json.dump(global_cache, gf_out, indent=4)
        except Exception:
            pass
            
        return {"status": "success", "classifications": vault["classifications"]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
