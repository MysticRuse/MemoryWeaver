import json
from agents.memory.tools.memory_bank import MemoryBankStore

def upsert_contributor_profile(session_id: str, contributor_id: str, contributor_name: str, moments: list[str], photo_count: int) -> str:
    """
    Registers or updates a contributor's profile in the Memory Bank for one event session.
    """
    store = MemoryBankStore(session_id)
    profile = store.upsert_contributor(contributor_id, contributor_name, moments, photo_count)
    return json.dumps(profile)

def get_contributor_profile(session_id: str, contributor_id: str) -> str:
    """
    Retrieves a contributor's preferences, stats, and event record for one session.
    """
    store = MemoryBankStore(session_id)
    profile = store.get_contributor_profile(contributor_id)
    if not profile:
        return json.dumps({"error": f"Contributor {contributor_id} not found."})
    return json.dumps(profile)

def recommend_missed_moments(session_id: str, contributor_id: str, trip_manifest_json: str) -> str:
    """
    Compares a contributor's profile history (within one event session) with the
    full photo manifest for that session. Identifies moments where this contributor
    was absent, and returns the top-scored photo from each of those moments so they
    can catch up.
    """
    store = MemoryBankStore(session_id)
    profile = store.get_contributor_profile(contributor_id)
    if not profile:
        return json.dumps({"message": "No profile history found for recommendations."})
        
    try:
        manifest = json.loads(trip_manifest_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid manifest format: {e}"})

    user_moments = set(profile.get("moments_present_in", []))
    missed_recommendations = []
    
    # Manifest is expected to contain photos grouped by moment or a flat list of scored photos
    # Let's handle a list of photo objects
    photos = manifest if isinstance(manifest, list) else manifest.get("photos", [])
    
    # Group photos by scene_label / moment
    moments_map = {}
    for p in photos:
        moment = p.get("scene_label", p.get("moment", "general"))
        if moment not in moments_map:
            moments_map[moment] = []
        moments_map[moment].append(p)
        
    for moment, items in moments_map.items():
        if moment not in user_moments:
            # User was not present in this moment. Pick the top-scored photo
            items.sort(key=lambda x: x.get("score", 5.0), reverse=True)
            top_photo = items[0]
            missed_recommendations.append({
                "moment": moment,
                "photo": top_photo.get("filename"),
                "caption": top_photo.get("caption"),
                "score": top_photo.get("score")
            })
            
    return json.dumps(missed_recommendations)
