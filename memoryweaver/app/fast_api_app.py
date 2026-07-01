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
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
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

# Serve local storage files so the frontend can retrieve thumbnails
app.mount("/local_storage", StaticFiles(directory=local_storage_dir), name="local_storage")


@app.get("/", response_class=HTMLResponse)
async def serve_upload_page():
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "upload.html"))
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(content="<h1>Upload page not found</h1>", status_code=404)


@app.post("/delete")
def delete_photo(filename: str = Form(...)):
    """Deletes the original photo and its thumbnail from local storage."""
    try:
        # Prevent path traversal attacks
        safe_filename = os.path.basename(filename)
        upload_path = os.path.join(local_storage_dir, "uploads", safe_filename)
        thumb_path = os.path.join(local_storage_dir, "thumbs", safe_filename)
        
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


selected_folder_path = None
pre_clean_results = {"accepted": [], "rejected": []}

from pydantic import BaseModel
class IngestRequest(BaseModel):
    filenames: list[str]

@app.post("/api/select-folder")
def select_folder():
    """Opens a native macOS Finder folder selector and returns file count."""
    global selected_folder_path
    try:
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()  # Hide main window
        root.attributes('-topmost', True)  # Bring Finder dialog to the top
        
        folder = filedialog.askdirectory(parent=root, title="Select Google Photos Folder")
        root.destroy()
        
        if folder:
            selected_folder_path = folder
            files = [f for f in os.listdir(folder) 
                     if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))]
            return {"status": "success", "folder": folder, "count": len(files)}
        return {"status": "error", "message": "No folder selected"}
    except Exception as e:
        return {"status": "error", "message": f"Folder selector failed: {str(e)}"}

@app.post("/api/pre-clean")
def run_pre_clean(blur_threshold: float = 12.0, dup_threshold: int = 8):
    """Executes local_cleaner.py to classify photos into accepted/rejected trays."""
    global selected_folder_path, pre_clean_results
    if not selected_folder_path:
        return {"status": "error", "message": "No folder selected. Please select a folder first."}
    
    from pipeline.local_cleaner import analyze_directory
    try:
        res = analyze_directory(selected_folder_path, blur_threshold, dup_threshold)
        pre_clean_results = res
        return {"status": "success", "accepted": res["accepted"], "rejected": res["rejected"]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/serve-raw")
def serve_raw_file(filename: str):
    """Streams local images from the selected folder, converting HEIC on-the-fly."""
    global selected_folder_path
    if not selected_folder_path:
        return {"status": "error", "message": "No folder selected"}
        
    safe_filename = os.path.basename(filename)
    full_path = os.path.join(selected_folder_path, safe_filename)
    
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

@app.post("/api/confirm-ingest")
def confirm_ingest(req: IngestRequest):
    """Copies the approved accepted photo list to local_storage/uploads/."""
    global selected_folder_path, pre_clean_results
    if not selected_folder_path:
        return {"status": "error", "message": "No active folder session."}
        
    try:
        import shutil
        import hashlib
        from PIL import Image
        from agents.memory.tools.memory_bank import MemoryBankStore
        from pipeline.local_cleaner import get_exif_metadata
        
        upload_dir = os.path.join(local_storage_dir, "uploads")
        thumb_dir = os.path.join(local_storage_dir, "thumbs")
        
        # Clear existing uploads/thumbs
        shutil.rmtree(upload_dir, ignore_errors=True)
        shutil.rmtree(thumb_dir, ignore_errors=True)
        os.makedirs(upload_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)
        
        store = MemoryBankStore()
        ingested = []
        
        for filename in req.filenames:
            src_path = os.path.join(selected_folder_path, filename)
            if not os.path.exists(src_path):
                continue
                
            # Resolve uploader/contributor info
            meta = get_exif_metadata(src_path)
            uploader_name = meta["uploader"]
            contributor_id = hashlib.sha256(uploader_name.strip().lower().encode()).hexdigest()[:12]
            
            # Map name in Memory Bank
            store.upsert_contributor(contributor_id, uploader_name, [], 0)
            
            # Copy file with hashing prefix
            timestamp_prefix = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            unique_name = f"{contributor_id}_{timestamp_prefix}_{filename}"
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
def get_trip_stats():
    """Returns the total number of uploaded photos in the active trip pool."""
    try:
        upload_dir = os.path.join(local_storage_dir, "uploads")
        count = 0
        if os.path.exists(upload_dir):
            count = len([
                f for f in os.listdir(upload_dir) 
                if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith('.')
            ])
        return {"status": "success", "total_uploaded": count}
    except Exception as e:
        return {"status": "error", "message": str(e)}




@app.post("/upload")
async def handle_photo_upload(photos: list[UploadFile] = File(...), contributor_name: str = Form("Anonymous")):
    results = []
    errors = []
    
    # Register contributor name in Memory Bank
    from agents.memory.tools.memory_bank import MemoryBankStore
    store = MemoryBankStore()
    
    for photo in photos:
        try:
            file_bytes = await photo.read()
            info = process_and_save_upload(
                file_bytes=file_bytes,
                original_filename=photo.filename,
                contributor_name=contributor_name
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


pipeline_logs = []

def log_pipeline_step(message: str):
    import datetime
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    pipeline_logs.append(log_line)
    print(log_line)


@app.get("/logs")
def get_pipeline_logs():
    """Returns the in-memory logs of the current or most recent curation run."""
    return {"status": "success", "logs": pipeline_logs}


@app.post("/generate")
def run_generation_pipeline():
    """Triggers the full multi-agent moderation, curation, memory and narration pipeline."""
    import time
    start_time = time.time()
    try:
        global pipeline_logs
        pipeline_logs.clear()
        
        log_pipeline_step("Initiating pipeline orchestration...")
        stats = execute_trip_pipeline(project_root, log=log_pipeline_step)
        
        # ==========================================
        # ### DEBUG / PERFORMANCE TRACKING SECTION ###
        # Comment this section out in production.
        # ==========================================
        elapsed = round(time.time() - start_time, 2)
        uncached_mod = stats.get("uncached_moderated", 0)
        uncached_score = stats.get("uncached_scored", 0)
        
        # Moderation uses approx 1000 input / 100 output per photo
        # Curation/scoring uses approx 1500 input / 150 output per photo
        # Narrative generation uses approx 800 input / 250 output per moment
        moments_count = stats.get("moments_count", 0)
        
        est_input_tokens = (uncached_mod * 1000) + (uncached_score * 1500) + (moments_count * 800)
        est_output_tokens = (uncached_mod * 100) + (uncached_score * 150) + (moments_count * 250)
        
        # Gemini 2.5 Flash Pricing (standard pricing: $0.000075 / 1k input, $0.0003 / 1k output)
        est_cost = (est_input_tokens * 0.000075 / 1000) + (est_output_tokens * 0.0003 / 1000)
        
        log_pipeline_step(f"PERFORMANCE REPORT (UNCACHED RUNS ONLY):")
        log_pipeline_step(f"  - Total Elapsed Time: {elapsed} seconds")
        log_pipeline_step(f"  - Actual API Calls Made (Mod/Score): {uncached_mod}/{uncached_score}")
        log_pipeline_step(f"  - Estimated Input Tokens: {est_input_tokens}")
        log_pipeline_step(f"  - Estimated Output Tokens: {est_output_tokens}")
        log_pipeline_step(f"  - Estimated API Cost: ${est_cost:.6f} USD")
        # ==========================================
        
        return {"status": "success", "stats": stats}
    except Exception as e:
        log_pipeline_step(f"CRITICAL ERROR: {str(e)}")
        return {"status": "error", "message": str(e)}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
