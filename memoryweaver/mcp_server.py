"""MemoryWeaver MCP server.

Exposes the Memory Bank and curated trip artifacts over the Model Context
Protocol, so any MCP client (Claude Desktop, another agent, an IDE) can ask
questions like "who contributed to the Bali trip?" or "which moments did
Grandma miss?" without going through the web UI.

Deliberately read-only: mutations (uploads, pipeline runs, deletions) stay
behind the FastAPI endpoints and their MW_ADMIN_TOKEN guard - exposing them
here would bypass that auth layer. See CONTEXT.md's STRIDE notes.

Run standalone (stdio transport, what MCP clients expect):

    cd memoryweaver && uv run python mcp_server.py

Claude Desktop config snippet:

    {
      "mcpServers": {
        "memoryweaver": {
          "command": "uv",
          "args": ["run", "--directory", "/path/to/memoryweaver", "python", "mcp_server.py"]
        }
      }
    }
"""

import json
import os
import sys

# Allow running as a plain script: make project-root imports (agents.*, app.*) work.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from agents.memory.tools.memory_bank import MemoryBankStore
from agents.memory.tools.memory_helpers import recommend_missed_moments as _recommend
from app.app_utils.sessions import SessionStore, public_view
from app.app_utils.storage import StorageHelper

mcp = FastMCP("memoryweaver")


@mcp.tool()
def list_event_sessions() -> str:
    """Lists every saved event session (trips, birthdays, weddings, matches...)
    with its session_id, name, event type, and creation date. Use the
    session_id with the other tools."""
    store = SessionStore()
    # public_view strips share_code - MCP clients get event metadata,
    # never the upload credential.
    return json.dumps({
        "sessions": [public_view(s) for s in store.list_sessions()],
        "supported_event_types": list(SessionStore.EVENT_TYPES),
    }, indent=2)


@mcp.tool()
def get_contributor_profiles(session_id: str = "default") -> str:
    """Returns the Memory Bank for one event session: every contributor's name,
    upload count, and which moments/scenes they were present in, plus the
    event context (destination, participants, event type)."""
    store = MemoryBankStore(session_id)
    # Trim to the fields a client needs - never raw EXIF or storage paths.
    contributors = {
        cid: {
            "name": prof.get("name"),
            "upload_count": prof.get("upload_count", 0),
            "moments_present_in": prof.get("moments_present_in", []),
        }
        for cid, prof in store.data.get("contributors", {}).items()
    }
    return json.dumps({
        "session_id": session_id,
        "trip_context": store.get_trip_context(),
        "contributors": contributors,
    }, indent=2)


@mcp.tool()
def get_event_journal(session_id: str = "default") -> str:
    """Returns the curated journal for one event session: per-moment entries
    (with dates and photo references) and the full narrative story. Returns
    an error message if the curation pipeline hasn't been run yet."""
    storage = StorageHelper(session_id=session_id)
    artefacts_dir = os.path.join(storage.local_base, "artefacts")

    journal_path = os.path.join(artefacts_dir, "journal.json")
    story_path = os.path.join(artefacts_dir, "story.txt")
    if not os.path.exists(journal_path):
        return json.dumps({"error": f"No journal generated yet for session '{session_id}'."})

    with open(journal_path) as f:
        journal = json.load(f)
    story = ""
    if os.path.exists(story_path):
        with open(story_path) as f:
            story = f.read()
    return json.dumps({"session_id": session_id, "journal": journal, "story": story}, indent=2)


@mcp.tool()
def find_missed_moments(contributor_id: str, session_id: str = "default") -> str:
    """Finds moments of an event that a specific contributor was absent from,
    returning the top-scored photo of each missed moment so they can catch up.
    Get contributor_id values from get_contributor_profiles."""
    storage = StorageHelper(session_id=session_id)
    manifest_path = os.path.join(storage.local_base, "artefacts", "manifest.json")
    if not os.path.exists(manifest_path):
        return json.dumps({"error": f"No curated manifest for session '{session_id}' - run the pipeline first."})
    with open(manifest_path) as f:
        manifest_json = f.read()
    return _recommend(session_id, contributor_id, manifest_json)


if __name__ == "__main__":
    mcp.run()  # stdio transport
