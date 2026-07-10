import os
import re
import json
import uuid
import secrets
import datetime
from google.cloud import storage as gcs_storage

SESSIONS_INDEX_FILE = "sessions_index.json"
DEFAULT_SESSION_ID = "default"


def public_view(session: dict) -> dict:
    """Session dict without the share_code upload credential.

    share_code is a bearer credential - the only sanctioned way to obtain it is
    the admin-token-gated /api/share-info endpoint. Every other surface that
    lists or returns sessions (open web APIs, the concierge agent's tools, the
    MCP server) must go through this filter, otherwise anyone who can list
    events can also upload into them.
    """
    return {k: v for k, v in session.items() if k != "share_code"}


def _slugify(name: str) -> str:
    """Turns an event name into a URL/path-safe slug for the session id prefix."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "event"


class SessionStore:
    """
    Registry of trip/event sessions. Each session gets its own isolated storage
    namespace (uploads/thumbs/artefacts/memory bank, via StorageHelper) so multiple
    events - a weekend trip, a birthday party, a wedding, a soccer match - can be
    curated and saved independently instead of sharing one global photo pool.

    'default' is special-cased in StorageHelper to map onto the original flat
    local_storage/ layout, so pre-existing installs and the current frontend keep
    working unchanged with no migration step.
    """

    EVENT_TYPES = ("trip", "birthday", "wedding", "sports_match", "reunion", "other")

    def __init__(self):
        self.bucket_name = os.environ.get("GCS_BUCKET_NAME")
        self.project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.local_base = os.path.join(project_root, "local_storage")
        self.use_gcs = False

        if self.bucket_name:
            try:
                self._client = gcs_storage.Client(project=self.project_id)
                self._bucket = self._client.bucket(self.bucket_name)
                self.use_gcs = True
            except Exception as e:
                print(f"Failed to initialize GCS client for SessionStore: {e}. Falling back to local storage.")

        # Check if new install before loading
        is_new_install = False
        if self.use_gcs:
            try:
                blob = self._bucket.blob(SESSIONS_INDEX_FILE)
                is_new_install = not blob.exists()
            except Exception:
                is_new_install = True
        else:
            path = os.path.join(self.local_base, SESSIONS_INDEX_FILE)
            is_new_install = not os.path.exists(path)

        self.data = self._load()
        self._ensure_default_session(is_new_install)
        self._backfill_share_codes()

    def _load(self) -> dict:
        if self.use_gcs:
            try:
                blob = self._bucket.blob(SESSIONS_INDEX_FILE)
                if blob.exists():
                    return json.loads(blob.download_as_string().decode())
            except Exception as e:
                print(f"Error loading session index from GCS: {e}")
        else:
            path = os.path.join(self.local_base, SESSIONS_INDEX_FILE)
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        return json.load(f)
                except Exception as e:
                    print(f"Error loading local session index: {e}")
        return {"sessions": {}}

    def _save(self):
        content = json.dumps(self.data, indent=2)
        if self.use_gcs:
            try:
                self._bucket.blob(SESSIONS_INDEX_FILE).upload_from_string(content.encode())
            except Exception as e:
                print(f"Error saving session index to GCS: {e}")
        else:
            path = os.path.join(self.local_base, SESSIONS_INDEX_FILE)
            with open(path, "w") as f:
                f.write(content)

    def _ensure_default_session(self, is_new_install: bool):
        """Registers 'default' if this is a fresh install and 'default' is missing,
        so the user has a starting point, but lets them delete it later."""
        if "sessions" not in self.data:
            self.data["sessions"] = {}
        if is_new_install and DEFAULT_SESSION_ID not in self.data["sessions"]:
            self.data["sessions"][DEFAULT_SESSION_ID] = {
                "session_id": DEFAULT_SESSION_ID,
                "name": "My Event",
                "event_type": "trip",
                "created_at": datetime.datetime.utcnow().isoformat(),
            }
            self._save()

    def _backfill_share_codes(self):
        """Adds a share_code to sessions created before contributor links existed.
        The code is the upload credential embedded in the shareable /join link -
        it gates POST /upload so strangers can't dump photos into an event by
        guessing its session_id."""
        changed = False
        for session in self.data["sessions"].values():
            if not session.get("share_code"):
                session["share_code"] = secrets.token_urlsafe(6)
                changed = True
        if changed:
            self._save()

    def create_session(self, name: str, event_type: str = "trip") -> dict:
        """Creates a new, fully isolated session and provisions its storage folders."""
        if event_type not in self.EVENT_TYPES:
            event_type = "other"

        session_id = f"{_slugify(name)}-{uuid.uuid4().hex[:6]}"
        session = {
            "session_id": session_id,
            "name": name.strip() or session_id,
            "event_type": event_type,
            "created_at": datetime.datetime.utcnow().isoformat(),
            # Upload credential for the shareable contributor link (see /join)
            "share_code": secrets.token_urlsafe(6),
        }
        self.data["sessions"][session_id] = session
        self._save()

        if not self.use_gcs:
            for sub in ("uploads", "thumbs", "artefacts"):
                os.makedirs(os.path.join(self.local_base, "sessions", session_id, sub), exist_ok=True)

        return session

    def update_session(self, session_id: str, updates: dict):
        """Updates properties of a session and saves the registry."""
        if session_id in self.data["sessions"]:
            self.data["sessions"][session_id].update(updates)
            self._save()
            return self.data["sessions"][session_id]
        return None

    def list_sessions(self) -> list[dict]:
        return sorted(self.data["sessions"].values(), key=lambda s: s.get("created_at", ""), reverse=True)

    def get_session(self, session_id: str) -> dict | None:
        return self.data["sessions"].get(session_id)

    def delete_session(self, session_id: str):
        """Admin-only: Deletes the session metadata and all its local or GCS storage recursively."""
        if session_id in self.data["sessions"]:
            del self.data["sessions"][session_id]
            self._save()
        
        if self.use_gcs:
            try:
                if session_id == DEFAULT_SESSION_ID:
                    # Clean default flat directories in GCS
                    for prefix in ("uploads/", "thumbs/", "artefacts/"):
                        blobs = self._bucket.list_blobs(prefix=prefix)
                        for blob in blobs:
                            blob.delete()
                else:
                    prefix = f"sessions/{session_id}/"
                    blobs = self._bucket.list_blobs(prefix=prefix)
                    for blob in blobs:
                        blob.delete()
            except Exception as e:
                print(f"Error deleting GCS session prefix: {e}")
        else:
            import shutil
            if session_id == DEFAULT_SESSION_ID:
                # Clean default flat directories locally
                shutil.rmtree(os.path.join(self.local_base, "uploads"), ignore_errors=True)
                shutil.rmtree(os.path.join(self.local_base, "thumbs"), ignore_errors=True)
                shutil.rmtree(os.path.join(self.local_base, "artefacts"), ignore_errors=True)
                # Recreate clean local directories
                os.makedirs(os.path.join(self.local_base, "uploads"), exist_ok=True)
                os.makedirs(os.path.join(self.local_base, "thumbs"), exist_ok=True)
                os.makedirs(os.path.join(self.local_base, "artefacts"), exist_ok=True)
                mb_path = os.path.join(self.local_base, "memory_bank.json")
                if os.path.exists(mb_path):
                    try:
                        os.remove(mb_path)
                    except Exception:
                        pass
            else:
                session_path = os.path.join(self.local_base, "sessions", session_id)
                shutil.rmtree(session_path, ignore_errors=True)
