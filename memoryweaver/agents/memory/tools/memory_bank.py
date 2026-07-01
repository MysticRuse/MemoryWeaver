import os
import json
import datetime
from app.app_utils.storage import StorageHelper

MEMORY_FILE = "memory_bank.json"

class MemoryBankStore:
    def __init__(self):
        self.storage = StorageHelper()
        self.local_path = os.path.join(self.storage.local_base, MEMORY_FILE)
        self.data = self._load()

    def _load(self) -> dict:
        if self.storage.use_gcs:
            try:
                blob = self.storage.bucket.blob(f"memory/{MEMORY_FILE}")
                if blob.exists():
                    return json.loads(blob.download_as_string().decode())
            except Exception as e:
                print(f"Error loading memory from GCS: {e}")
        else:
            if os.path.exists(self.local_path):
                try:
                    with open(self.local_path, "r") as f:
                        return json.load(f)
                except Exception as e:
                    print(f"Error loading local memory bank: {e}")
        
        # Default empty memory structure
        return {
            "contributors": {},  # contributor_id -> profile
            "trip_context": {
                "destination": "",
                "duration_days": 0,
                "participants": [],
                "theme": ""
            }
        }

    def save(self):
        content_bytes = json.dumps(self.data, indent=2).encode()
        if self.storage.use_gcs:
            try:
                blob = self.storage.bucket.blob(f"memory/{MEMORY_FILE}")
                blob.upload_from_string(content_bytes)
            except Exception as e:
                print(f"Error saving memory to GCS: {e}")
        else:
            try:
                with open(self.local_path, "wb") as f:
                    f.write(content_bytes)
            except Exception as e:
                print(f"Error saving local memory bank: {e}")

    def upsert_contributor(self, contributor_id: str, contributor_name: str, new_moments: list[str], photo_count: int) -> dict:
        """Upserts a contributor record and returns the updated profile."""
        contributors = self.data["contributors"]
        
        if contributor_id not in contributors:
            contributors[contributor_id] = {
                "name": contributor_name,
                "upload_count": 0,
                "moments_present_in": [],
                "preferences": [],
                "last_seen": ""
            }
        else:
            # Overwrite name if the current name is a default placeholder
            curr_name = contributors[contributor_id].get("name", "Contributor")
            if curr_name in ("Contributor", "Anonymous") and contributor_name not in ("Contributor", "Anonymous"):
                contributors[contributor_id]["name"] = contributor_name
            
        profile = contributors[contributor_id]
        profile["upload_count"] += photo_count
        profile["last_seen"] = datetime.datetime.utcnow().isoformat()
        
        # Merge moments present in, deduplicated
        merged_moments = list(set(profile["moments_present_in"] + new_moments))
        profile["moments_present_in"] = merged_moments
        
        self.save()
        return profile

    def update_preferences(self, contributor_id: str, preferences: list[str]):
        """Updates preferences extracted by Gemini for a contributor."""
        if contributor_id in self.data["contributors"]:
            self.data["contributors"][contributor_id]["preferences"] = preferences
            self.save()

    def get_contributor_profile(self, contributor_id: str) -> dict:
        return self.data["contributors"].get(contributor_id)

    def get_trip_context(self) -> dict:
        return self.data["trip_context"]

    def set_trip_context(self, destination: str, duration_days: int, participants: list[str], theme: str):
        self.data["trip_context"] = {
            "destination": destination,
            "duration_days": duration_days,
            "participants": participants,
            "theme": theme
        }
        self.save()
