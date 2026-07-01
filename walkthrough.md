# MemoryWeaver Change Log & Walkthrough

This document tracks all changes made to the repository on a day-by-day basis. Use this to catch up on the project state.

## June 29 (Monday) — Repository Restructuring & Scaffolding

### 1. Repository Clean Up & Organization
* Created the `archive/` directory to store reference materials.
* Moved the legacy FIFA World Cup 2026 sports event MVP from the root folder `memoryweaver-mvp/` to `archive/sports-mvp/` to avoid polluting the workspace namespace.

### 2. Standard Configuration Files Created
* **[CONTEXT.md](file:///Users/mitilroy/Dev/src/MysticRuse/MemoryWeaver/CONTEXT.md)**: Establishes our project-wide coding guidelines, STRIDE threat models, no-PII logging rules, and input sanitization requirements.
* **[.env.example](file:///Users/mitilroy/Dev/src/MysticRuse/MemoryWeaver/.env.example)**: Set up the environment variables template (secrets, Google Cloud details, and Flask configurations).

### 3. Task Checklist Set Up
* Created a living checklist in the artifacts directory [task.md](file:///Users/mitilroy/.gemini/antigravity/brain/f31d3471-6b70-408e-b396-56f5bdbd3454/task.md) to log task status.

### 4. Agents-CLI Scaffold & Agent Directories Setup
* Run `agents-cli create memoryweaver -a adk_a2a -d cloud_run -p -k` to generate the default A2A structure.
* Created subdirectories for all 5 specialized agents under the new project layout:
  * `agents/collector/`
  * `agents/moderator/`
  * `agents/curator/`
  * `agents/memory/`
  * `agents/narrator/`

### 5. Storage Helper & QR Code Tools Created
* **`app/app_utils/storage.py`**: Created a modular storage manager supporting both GCS (via `google-cloud-storage`) and a local file fallback (`local_storage/`) if bucket names are not set.
* **`agents/collector/tools/qr.py`**: Integrated the `qrcode` library to generate upload access codes and save them directly as artifacts.

### 6. Collector Agent & UI Scaffolding Complete
* **`agents/collector/tools/upload.py`**: Implemented image type verification, 20MB file limit enforcement, anonymous SHA-256 `contributor_id` hashing, and Pillow-based EXIF tag extraction (coordinates, datetime, device).
* **`agents/collector/agent.py`**: Registered the `collector_agent` using ADK Agent wrapper.
* **`frontend/upload.html`**: Designed a clean, mobile-first touch responsive upload UI layout.
* **Dependencies**: Installed `pillow-heif` and `qrcode` packages into the virtualenv.

### 7. Storage Locations & Active Trip Album Counter
* **File Storage**: Uploaded files are stored in `memoryweaver/local_storage/uploads/` (original images) and `memoryweaver/local_storage/thumbs/` (generated thumbnails).
* **Trip Stats API**: Added `GET /trip-stats` in `app/fast_api_app.py` that reads the upload folder and returns the current total.
* **Frontend Counter Badge**: Placed a "📂 Trip Album Size" badge in `upload.html` that updates dynamically when the page loads, and whenever photos are successfully uploaded or deleted.

## June 30 (Tuesday) — Moderator & Curator Agents (Safety, Security & Scoring)

### 1. Moderator Agent Scaffolded
* **`agents/moderator/tools/vision_check.py`**: Added tool logic utilizing Gemini 2.5 Flash to inspect photos for safety rules, focus/sharpness, and screenshot filtering, returning structured JSON results.
* **`agents/moderator/agent.py`**: Registered the `moderator_agent` using ADK Agent schema.

### 2. STRIDE Security Analysis & Input Sanitization
* **STRIDE Threat Modeling**: Identified top vulnerabilities on the public upload surface (path-traversal file injection, malicious EXIF payloads, DoS with massive files).
* **Vulnerability Fix**: Modified `process_and_save_upload` in `tools/upload.py` to immediately sanitize inputs using `os.path.basename`, preventing path traversal folder generation.
* **Pytest Verification**: Created a unit test suite `tests/unit/test_security.py` validating file limits, extension blocks, and filename sanitization. Ran and verified that all 3 tests passed successfully.

### 3. Curator Agent & LLM-as-Judge Setup
* **`agents/curator/tools/embed.py`**: Built image vector generator leveraging Gemini Multimodal Embeddings (with mathematical sinus fallback for local offline testing) to handle cosine similarity deduplication.
* **`agents/curator/tools/score.py`**: Implemented `score_photo_as_judge` prompting Gemini 2.5 Flash to rate photos 0-10 on composition, sharpness, uniqueness, and candid human presence.
* **`agents/curator/agent.py`**: Registered the `curator_agent` with embedding and scoring tools active.

## July 1 (Wednesday) — Memory Agent & Narrator Agent (Core)

### 1. Memory Agent Implemented
* **`agents/memory/tools/memory_bank.py`**: Built a persistent Memory Bank JSON store with GCS storage logic and local file fallback (`memory_bank.json`), allowing cross-session tracking of contributor profiles.
* **`agents/memory/tools/memory_helpers.py`**: Implemented helper tools to upsert contributor profiles (`contributor_id`, `upload_count`, `moments_present_in`, `last_seen`) and execute "You might have missed this" recommendation queries.
* **`agents/memory/agent.py`**: Registered the `memory_agent` utilizing the profile management and recommendation tools.

### 2. Narrator Agent Implemented
* **`agents/narrator/tools/journal.py`**: Built moment journaling tool prompting Gemini 2.5 Flash to synthesize photo details and optional voice note transcripts into warm 2-3 sentence first-person plural narrative journal entries.
* **`agents/narrator/tools/story.py`**: Developed trip story compiler that weaves individual moment entries into a multi-paragraph, engaging narrative summary.
* **`agents/narrator/agent.py`**: Registered the `narrator_agent` using ADK Agent schema.

### 3. Pipeline Integration & Curation Dashboard Viewer
* **`pipeline/orchestrator.py`**: Sequenced the 5-agent logic to moderating uploads, performing deduplication and LLM-as-judge quality scoring, updating the cross-session memory bank, and summarizing moments/compiling trip journals.
* **FastAPI Router**: Added `/generate` endpoint to trigger the multi-agent curation run, and `/viewer` to serve the interactive album view.
* **`frontend/viewer.html`**: Designed an Outfit-typography powered glassmorphic dashboard showcasing a Daily Highlights carousel, side-by-side Day-by-Day Journal with thumbnails, and a sticky overall Trip Story card.
* **`frontend/upload.html`**: Integrated a prominent 'Curate & Build Trip Book' button with loading states.
* **Curation Log Console Tracker**:
  * Added `/logs` endpoint in `fast_api_app.py` serving an in-memory buffer of pipeline steps.
  * Added a collapsible terminal style console inside `upload.html` which queries `/logs` periodically during curation to display live agent activity (safety checks, deduplications, LLM-as-judge scores, and storytelling phases) directly in the UI.
  * Added time measurement and estimated token/cost calculations inside a clearly marked debug/performance section in `fast_api_app.py` for easy production configuration/removal.

### 4. Multi-Contributor Filtering & Personalized Catch-Up
* **`frontend/viewer.html`**:
  * Added a `View Journal As` drop-down selector populated dynamically from `memory_bank.json`.
  * Added client-side CSS dimming to visually emphasize a specific contributor's uploads.
  * Implemented client-side **"While You Were Away (Catch-Up)"** recommendations that compare the selected contributor's moments list with the full journal and render the top-rated photos from scenes they missed.
* **Contributor Name Mapping**:
  * Updated `/upload` endpoint in `fast_api_app.py` to write the plain text `contributor_name` mapping to the Memory Bank upon upload.
  * Updated `pipeline/orchestrator.py` Phase 4 to load contributor names dynamically from the Memory Bank (defaulting to "Unsplash Stock" for downloaded images) instead of hardcoding "Contributor".
* **Upload Dialog Removal**:
  * Removed the blocking browser `alert()` modal on successful uploads inside `upload.html`, keeping only the dynamic button spinner state that resets immediately when the upload completes.








