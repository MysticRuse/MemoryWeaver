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

setup_telemetry()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)

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
    """Serves a curated photo as a metadata-free JPEG (max 1600px long edge).

    Re-encoding through PIL drops EXIF entirely (GPS, device serials), which is
    what makes this safe to expose while the raw uploads directory stays private.
    """
    from PIL import Image
    import io

    safe_filename = os.path.basename(filename)  # path-traversal guard
    session_storage = StorageHelper(session_id=session_id)
    full_path = os.path.join(session_storage.local_base, "uploads", safe_filename)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="Photo not found")

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
selected_folder_paths: dict[str, str] = {}
pre_clean_results_by_session: dict[str, dict] = {}
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


@app.post("/api/select-folder", dependencies=[Depends(require_admin_token)])
def select_folder(session_id: str = "default"):
    """Opens a native macOS Finder folder selector and returns file count.

    Local development convenience only (bulk-import from a Google Photos export
    folder on the operator's Mac) - not part of the deployed/public upload flow,
    which is the browser-based /upload endpoint instead.
    """
    try:
        import subprocess
        # Run AppleScript to open folder picker natively on macOS
        cmd = "osascript -e 'POSIX path of (choose folder with prompt \"Select Google Photos Folder\")'"
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()

        if proc.returncode == 0:
            folder = stdout.decode('utf-8').strip()
            if folder and os.path.exists(folder):
                selected_folder_paths[session_id] = folder
                files = [f for f in os.listdir(folder)
                         if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))]
                return {"status": "success", "folder": folder, "count": len(files)}
        return {"status": "error", "message": "No folder selected or canceled"}
    except Exception as e:
        return {"status": "error", "message": f"Folder selector failed: {str(e)}"}

@app.post("/api/set-folder", dependencies=[Depends(require_admin_token)])
def set_folder(folder: str, session_id: str = "default"):
    selected_folder_paths[session_id] = folder
    return {"status": "success", "folder": folder}

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
    resp["folder"] = selected_folder_paths.get(session_id)
    resp["session_id"] = session_id
    # Capability flag: the bulk local-folder wizard shells out to the macOS
    # Finder picker, so the admin UI hides that tab anywhere it can't work.
    resp["is_local"] = sys.platform == "darwin" and not os.environ.get("K_SERVICE")
    return resp

def run_pre_clean_background(session_id: str, folder: str, blur_threshold: float, dup_threshold: int):
    state = _get_progress_state(session_id)
    pipeline_logs_by_session[session_id] = []
    log_pipeline_step(session_id, f"Starting Local Pre-Cleaning Analysis on folder: {folder}")

    import time
    start_time = time.time()
    last_log_pct = -5

    def pre_clean_progress(current, total):
        state["pre_clean"]["current"] = current
        state["pre_clean"]["total"] = total

        nonlocal last_log_pct
        pct = int((current / total) * 100) if total > 0 else 0
        if pct >= last_log_pct + 5 or current == total:
            last_log_pct = pct
            elapsed = time.time() - start_time
            log_pipeline_step(session_id, f"Pre-Clean Progress: {pct}% ({current}/{total}) • {elapsed:.1f}s elapsed")

    from pipeline.local_cleaner import analyze_directory
    try:
        res = analyze_directory(
            folder,
            blur_threshold,
            dup_threshold,
            progress_callback=pre_clean_progress
        )
        pre_clean_results_by_session[session_id] = res
        total_time = time.time() - start_time
        state["pre_clean"]["summary"] = {
            "elapsed_seconds": round(total_time, 1),
            "accepted_count": len(res['accepted']),
            "rejected_count": len(res['rejected'])
        }
        state["pre_clean"]["status"] = "complete"
        log_pipeline_step(session_id, f"Local Pre-Cleaning Analysis complete! Kept {len(res['accepted'])} accepted and excluded {len(res['rejected'])} duplicates/blurry files in {total_time:.1f}s.")
    except Exception as e:
        state["pre_clean"]["status"] = "error"
        state["pre_clean"]["error_msg"] = str(e)
        log_pipeline_step(session_id, f"Pre-Cleaning FAILED: {str(e)}")

@app.post("/api/pre-clean", dependencies=[Depends(require_admin_token)])
def run_pre_clean(background_tasks: BackgroundTasks, session_id: str = "default", blur_threshold: float = 12.0, dup_threshold: int = 8):
    """Triggers the local_cleaner.py analysis in the background for a session's selected folder."""
    folder = selected_folder_paths.get(session_id)
    if not folder:
        return {"status": "error", "message": "No folder selected. Please select a folder first."}

    # Synchronously reset to running state to prevent frontend from reading 'complete' from a previous run
    import time
    state = _get_progress_state(session_id)
    state["pre_clean"] = {
        "current": 0,
        "total": 0,
        "status": "running",
        "start_time": time.time()
    }

    background_tasks.add_task(run_pre_clean_background, session_id, folder, blur_threshold, dup_threshold)
    return {"status": "success", "message": "Pre-clean analysis started in the background."}

@app.get("/api/pre-clean-results")
def get_pre_clean_results(session_id: str = "default"):
    """Returns the completed accepted/rejected pre-clean lists for a session."""
    return pre_clean_results_by_session.get(session_id, {"accepted": [], "rejected": []})

@app.get("/api/serve-raw")
def serve_raw_file(filename: str, session_id: str = "default"):
    """Streams local images from the session's selected folder, converting HEIC on-the-fly."""
    folder = selected_folder_paths.get(session_id)
    if not folder:
        return {"status": "error", "message": "No folder selected"}

    safe_filename = os.path.basename(filename)
    full_path = os.path.join(folder, safe_filename)
    
    if os.path.exists(full_path):
        if safe_filename.lower().endswith('.heic'):
            try:
                from PIL import Image
                from pillow_heif import register_heif_opener
                import io
                from fastapi.responses import Response
                
                register_heif_opener()
                with Image.open(full_path) as im:
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=75)
                    return Response(content=buf.getvalue(), media_type="image/jpeg")
            except Exception as e:
                return {"status": "error", "message": f"HEIC conversion failed: {str(e)}"}
                
        from fastapi.responses import FileResponse
        return FileResponse(full_path)
    return {"status": "error", "message": "File not found"}

@app.post("/api/confirm-ingest", dependencies=[Depends(require_admin_token)])
def confirm_ingest(req: IngestRequest):
    """Copies the approved accepted photo list into one session's uploads/ storage."""
    folder = selected_folder_paths.get(req.session_id)
    if not folder:
        return {"status": "error", "message": "No active folder session."}

    try:
        import shutil
        import hashlib
        from PIL import Image
        from agents.memory.tools.memory_bank import MemoryBankStore
        from pipeline.local_cleaner import get_exif_metadata

        session_storage = StorageHelper(session_id=req.session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        thumb_dir = os.path.join(session_storage.local_base, "thumbs")

        # Clear existing uploads/thumbs for this session before re-ingesting
        shutil.rmtree(upload_dir, ignore_errors=True)
        shutil.rmtree(thumb_dir, ignore_errors=True)
        os.makedirs(upload_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)

        store = MemoryBankStore(req.session_id)
        ingested = []

        for filename in req.filenames:
            src_path = os.path.join(folder, filename)
            if not os.path.exists(src_path):
                continue
                
            # Resolve uploader/contributor info
            meta = get_exif_metadata(src_path)
            uploader_name = meta["uploader"]
            contributor_id = hashlib.sha256(uploader_name.strip().lower().encode()).hexdigest()[:12]
            
            # Map name in Memory Bank
            store.upsert_contributor(contributor_id, uploader_name, [], 0)
            
            # Copy file with a STABLE name: contributor hash + original filename.
            # Names must be deterministic across re-ingests - the journal,
            # highlights, and curation cache all reference photos by filename,
            # so the previous wall-clock timestamp prefix orphaned every
            # generated artifact (and invalidated the paid-for scoring cache)
            # whenever ingest was re-run.
            unique_name = f"{contributor_id}_{filename}"
            dest_path = os.path.join(upload_dir, unique_name)
            
            shutil.copy2(src_path, dest_path)
            
            # Generate thumbnail locally for viewer page performance
            try:
                with Image.open(dest_path) as img:
                    img.thumbnail((200, 200))
                    img.save(os.path.join(thumb_dir, unique_name))
            except:
                pass
                
            ingested.append(filename)
            
        return {"status": "success", "ingested_count": len(ingested)}
    except Exception as e:
        return {"status": "error", "message": f"Ingestion failed: {str(e)}"}

@app.get("/trip-stats")
def get_trip_stats(session_id: str = "default"):
    """Returns the total number of uploaded photos in a session's active photo pool."""
    try:
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        count = 0
        if os.path.exists(upload_dir):
            count = len([
                f for f in os.listdir(upload_dir)
                if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith('.')
            ])
        return {"status": "success", "total_uploaded": count, "session_id": session_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}




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
    logger.log_struct(feedback.model_dump(), severity="INFO")
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


def run_generation_background(session_id: str, limit: int):
    """Asynchronous background worker for the 5-agent pipeline, scoped to one session."""
    import time
    start_time = time.time()
    state = _get_progress_state(session_id)
    try:
        pipeline_logs_by_session[session_id] = []
        log_pipeline_step(session_id, "Initiating pipeline orchestration...")

        def pipeline_progress(current, total, phase):
            state["pipeline"]["current"] = current
            state["pipeline"]["total"] = total
            state["pipeline"]["phase"] = phase
            if current >= total and phase == "journaling":
                state["pipeline"]["status"] = "complete"

        stats = execute_trip_pipeline(
            project_root,
            session_id=session_id,
            limit=limit,
            log=lambda msg: log_pipeline_step(session_id, msg),
            progress_callback=pipeline_progress
        )

        state["pipeline"]["status"] = "complete"

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
def run_generation_pipeline(background_tasks: BackgroundTasks, session_id: str = "default", limit: int = 50):
    """Triggers the full multi-agent moderation, curation, memory and narration pipeline for one session."""
    import time
    # Synchronously reset to prevent frontend race condition
    state = _get_progress_state(session_id)
    state["pipeline"] = {
        "current": 0,
        "total": 0,
        "status": "running",
        "phase": "initiating",
        "start_time": time.time()
    }
    background_tasks.add_task(run_generation_background, session_id, limit)
    return {"status": "success", "message": f"Pipeline started with target limit {limit} in the background.", "session_id": session_id}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
