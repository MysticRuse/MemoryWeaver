# ruff: noqa
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

"""MemoryWeaver root agent.

This is the conversational entry point served over A2A (and via
`agents-cli playground`). It is a concierge/orchestrator on top of the
5-agent MemoryWeaver system:

    Collector -> Moderator -> Curator -> Memory -> Narrator

Design note: the heavy per-photo work (vision moderation, LLM-as-judge
scoring, dedup embeddings, batched journaling) runs inside
pipeline.orchestrator.execute_trip_pipeline. That pipeline is deliberately
deterministic Python that *calls the specialist agents' tools directly* in
batches - routing every photo through LLM tool-calling would multiply cost
and latency for zero quality gain on a 200-photo dump. The root agent
therefore drives the system at the task level (create/select event sessions,
launch the pipeline, fetch and discuss the generated artifacts), while the
specialist agents remain attached as sub_agents so a conversation can be
handed off to any of them for fine-grained, single-tool work (e.g. "re-score
just these 3 photos", "who missed the beach day?").
"""

import json
import os

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import LongRunningFunctionTool
from google.genai import types

from agents.collector.agent import collector_agent
from agents.curator.agent import curator_agent
from agents.memory.agent import memory_agent
from agents.moderator.agent import moderator_agent
from agents.narrator.agent import narrator_agent
# public_view strips share_code (the upload credential) from session dicts.
# The concierge is reachable over unauthenticated A2A, so its tool outputs
# must never contain codes - the eval suite caught the agent happily reading
# them out in chat before this filter existed.
from app.app_utils.sessions import SessionStore, public_view
from app.app_utils.storage import StorageHelper

# Project root (the directory containing app/, agents/, pipeline/,
# local_storage/) - needed by the pipeline entry point.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Session management tools (multi-event support)
# ---------------------------------------------------------------------------

def list_event_sessions() -> str:
    """Lists every saved event session (trips, birthdays, weddings, matches...).

    Returns:
        JSON array of sessions, each with session_id, name, event_type, and
        created_at, most recent first. Use the session_id with the other tools.
    """
    store = SessionStore()
    return json.dumps({
        "sessions": [public_view(s) for s in store.list_sessions()],
        "supported_event_types": list(SessionStore.EVENT_TYPES),
    })


def create_event_session(name: str, event_type: str) -> str:
    """Creates a new, fully isolated event session with its own photo pool and memory.

    Args:
        name: Human-friendly event name, e.g. "Bali Family Trip" or "Ari's 8th Birthday".
        event_type: One of: trip, birthday, wedding, sports_match, reunion, other.

    Returns:
        JSON of the created session, including the session_id to use for uploads
        and pipeline runs.
    """
    store = SessionStore()
    session = store.create_session(name, event_type)
    return json.dumps(public_view(session))


def get_event_status(session_id: str) -> str:
    """Reports the state of one event session: photo count and which artifacts exist.

    Args:
        session_id: The event session to inspect (use list_event_sessions to find it).

    Returns:
        JSON with uploaded photo count and booleans for each generated artifact
        (journal, story, highlights), so you can tell whether the curation
        pipeline has been run yet.
    """
    store = SessionStore()
    session = store.get_session(session_id)
    if not session:
        return json.dumps({"error": f"No session named '{session_id}'. Use list_event_sessions."})

    storage = StorageHelper(session_id=session_id)
    uploads_dir = os.path.join(storage.local_base, "uploads")
    artefacts_dir = os.path.join(storage.local_base, "artefacts")

    photo_count = 0
    if os.path.isdir(uploads_dir):
        photo_count = len([
            f for f in os.listdir(uploads_dir)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".heic"))
        ])

    return json.dumps({
        "session": public_view(session),
        "uploaded_photos": photo_count,
        "artifacts": {
            "journal": os.path.exists(os.path.join(artefacts_dir, "journal.json")),
            "story": os.path.exists(os.path.join(artefacts_dir, "story.txt")),
            "highlights": os.path.exists(os.path.join(artefacts_dir, "highlights.json")),
        },
    })


# ---------------------------------------------------------------------------
# Pipeline + artifact tools
# ---------------------------------------------------------------------------

def run_curation_pipeline(session_id: str, photo_limit: int) -> str:
    """Runs the full 5-agent curation pipeline on one event session's uploaded photos.

    Executes, in order: Moderator (safety/quality screening), Curator
    (dedup + LLM-as-judge scoring), Memory (contributor profiles), and
    Narrator (per-moment journal + full event story). Results are saved as
    the session's journal/story/highlights artifacts.

    This is a long-running call (roughly 1-4 minutes for a fresh 100-photo
    pool; nearly instant on re-runs thanks to the curation cache). Tell the
    user it is running before calling it.

    Args:
        session_id: The event session whose photos should be curated.
        photo_limit: Max photos to keep in the final journal (e.g. 50).
            Diversity-maximizing selection is applied across detected scenes.

    Returns:
        JSON of pipeline stats: photos processed/approved/quarantined,
        unique photos kept, number of moments, and story word count.
    """
    # Imported lazily: the pipeline module pulls in torch/CLIP for embeddings,
    # which we don't want on the critical path of agent startup.
    from pipeline.orchestrator import execute_trip_pipeline

    try:
        stats = execute_trip_pipeline(_PROJECT_ROOT, session_id=session_id, limit=photo_limit)
        return json.dumps({"status": "complete", "stats": stats})
    except ValueError as e:
        # Pipeline raises ValueError for empty/fully-quarantined photo pools -
        # surface that as a friendly, actionable message instead of crashing.
        return json.dumps({"status": "error", "message": str(e)})


def get_event_artifact(session_id: str, artifact: str) -> str:
    """Fetches a generated artifact (journal, story, or highlights) for an event session.

    Args:
        session_id: The event session to read from.
        artifact: One of "journal" (per-moment entries with photo refs),
            "story" (the flowing multi-paragraph event story), or
            "highlights" (top curated photos with captions and scores).

    Returns:
        The artifact content (JSON for journal/highlights, plain text for story),
        or an error message if the pipeline hasn't been run for this session yet.
    """
    storage = StorageHelper(session_id=session_id)
    artefacts_dir = os.path.join(storage.local_base, "artefacts")
    filenames = {"journal": "journal.json", "story": "story.txt", "highlights": "highlights.json"}

    if artifact not in filenames:
        return json.dumps({"error": f"Unknown artifact '{artifact}'. Choose journal, story, or highlights."})

    path = os.path.join(artefacts_dir, filenames[artifact])
    if not os.path.exists(path):
        return json.dumps({
            "error": f"No {artifact} generated yet for session '{session_id}'. "
                     "Run run_curation_pipeline first."
        })
    with open(path) as f:
        return f.read()


def request_user_input(message: str) -> dict:
    """Request additional input from the user.

    Use this tool when you need more information from the user to complete a
    task (e.g. which event session they mean, or what to name a new one).
    Calling this tool will pause execution until the user responds.

    Args:
        message: The question or clarification request to show the user.
    """
    return {"status": "pending", "message": message}


root_agent = Agent(
    name="memoryweaver_concierge",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "MemoryWeaver concierge: turns a family's raw photo dumps from trips and "
        "events into curated journals, stories, and highlight reels via a 5-agent "
        "pipeline (Collector, Moderator, Curator, Memory, Narrator)."
    ),
    instruction=(
        "You are the MemoryWeaver concierge - a warm, efficient assistant that helps "
        "busy families turn overwhelming photo dumps from trips and events (birthdays, "
        "weddings, matches, reunions) into curated keepsake journals.\n\n"
        "Your capabilities:\n"
        "1. Manage event sessions: list_event_sessions, create_event_session, get_event_status. "
        "Each event is fully isolated - its own photo pool, memory, and artifacts.\n"
        "2. Run the curation pipeline with run_curation_pipeline. Warn the user it can take a "
        "few minutes on a fresh photo pool. Check get_event_status first: if there are no "
        "uploaded photos, direct the user to the upload page instead of running the pipeline.\n"
        "3. Retrieve and discuss results with get_event_artifact (journal, story, highlights). "
        "When presenting the journal or story, quote the generated text rather than summarizing "
        "it away - families want to read it.\n\n"
        "You also have specialist sub-agents (moderator, curator, memory, narrator, collector) "
        "for fine-grained follow-ups - e.g. transfer to memory_agent for 'who missed the beach "
        "day?' style questions, or narrator_agent to rewrite a specific journal entry.\n\n"
        "If the user's request is ambiguous about which event they mean and more than one "
        "session exists, ask (request_user_input) rather than guessing."
    ),
    tools=[
        list_event_sessions,
        create_event_session,
        get_event_status,
        run_curation_pipeline,
        get_event_artifact,
        LongRunningFunctionTool(func=request_user_input),
    ],
    # The five specialists stay addressable for A2A discovery and for LLM-driven
    # transfer when a request needs one agent's tools directly rather than the
    # whole batched pipeline. clone() gives this tree its own instances: ADK
    # allows one parent per agent instance, and tooling that imports the module
    # under two identities (e.g. the eval harness's module discovery) would
    # otherwise fail with "already has a parent agent".
    sub_agents=[
        collector_agent.clone(),
        moderator_agent.clone(),
        curator_agent.clone(),
        memory_agent.clone(),
        narrator_agent.clone(),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
