import os
import json
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from agents.moderator.tools.vision_check import run_vision_moderation
from agents.curator.tools.score import score_photo_as_judge
from agents.curator.tools.embed import get_image_embedding, calculate_cosine_similarity
from agents.memory.tools.memory_bank import MemoryBankStore
from agents.narrator.tools.journal import generate_moment_journal
from agents.narrator.tools.story import generate_trip_story

CACHE_FILE = "curation_cache.json"

def execute_trip_pipeline(project_root: str, log=print, progress_callback=None) -> dict:
    """
    Runs the sequential 5-agent pipeline on all photos in local_storage/uploads.
    Saves outputs to local_storage/artefacts/ and returns stats.
    Uses concurrent threads, caching, and a progress callback to track states.
    """
    log("Starting MemoryWeaver Optimised Pipeline...")
    
    uploads_dir = os.path.join(project_root, "local_storage", "uploads")
    artefacts_dir = os.path.join(project_root, "local_storage", "artefacts")
    os.makedirs(artefacts_dir, exist_ok=True)
    
    cache_path = os.path.join(artefacts_dir, CACHE_FILE)
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                cache = json.load(f)
            log(f"Loaded curation cache containing {len(cache)} cached photo records.")
        except Exception as e:
            log(f"Warning: Failed to load curation cache: {e}")

    if not os.path.exists(uploads_dir):
        raise ValueError(f"Uploads directory not found at {uploads_dir}")
        
    photos = [f for f in os.listdir(uploads_dir) 
              if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))]
              
    if not photos:
        raise ValueError("No photos found in the upload pool. Upload photos first.")
        
    log(f"Found {len(photos)} photo(s). Starting Phase 1: Moderation...")
    
    approved = []
    quarantined = []
    uncached_photos = []
    
    # Filter photos using cache
    for photo in photos:
        if photo in cache:
            cached_data = cache[photo]
            if cached_data.get("moderation", {}).get("usable", False):
                approved.append((photo, os.path.join(uploads_dir, photo)))
            else:
                reason = cached_data.get("moderation", {}).get("reason", "Quarantined via cache")
                quarantined.append({"filename": photo, "reason": reason})
                log(f"  [CACHED QUARANTINE] {photo} - {reason}")
        else:
            uncached_photos.append(photo)
            
    # Run Moderation in Parallel for Uncached Photos
    if uncached_photos:
        log(f"  Processing {len(uncached_photos)} uncached photos for safety...")
        
        lock = threading.Lock()
        progress = {"count": 0}
        
        def moderate_photo(photo):
            full_path = os.path.join(uploads_dir, photo)
            mod_res = run_vision_moderation(full_path)
            
            with lock:
                progress["count"] += 1
                if progress_callback:
                    progress_callback(progress["count"], len(uncached_photos), "moderation")
                    
            return photo, full_path, mod_res

        with ThreadPoolExecutor(max_workers=8) as executor:
            mod_results = list(executor.map(moderate_photo, uncached_photos))
            
        for photo, full_path, mod_res in mod_results:
            # Initialise cache entry
            if photo not in cache:
                cache[photo] = {}
            cache[photo]["moderation"] = mod_res
            
            if mod_res["usable"]:
                approved.append((photo, full_path))
            else:
                quarantined.append({"filename": photo, "reason": mod_res["reason"]})
                log(f"  [QUARANTINED] {photo} - {mod_res['reason']}")
    else:
        if progress_callback:
            progress_callback(100, 100, "moderation")
                
    if not approved:
        raise ValueError("All photos were quarantined by the Moderator Agent.")
        
    log(f"Moderation complete: {len(approved)} approved, {len(quarantined)} quarantined.")
    
    # Run Embedding in Parallel (only for uncached approved photos)
    log("Starting Phase 2: Curator Deduplication...")
    unique_photos = []
    embeddings = []
    uncached_approved = []
    
    for photo, path in approved:
        if "embedding" in cache.get(photo, {}):
            embeddings.append((photo, path, cache[photo]["embedding"]))
        else:
            uncached_approved.append((photo, path))
            
    if uncached_approved:
        log(f"  Generating embeddings for {len(uncached_approved)} uncached photos...")
        
        lock = threading.Lock()
        progress = {"count": 0}
        
        def embed_photo(item):
            photo, path = item
            emb = get_image_embedding(path)
            
            with lock:
                progress["count"] += 1
                if progress_callback:
                    progress_callback(progress["count"], len(uncached_approved), "embedding")
                    
            return photo, path, emb

        with ThreadPoolExecutor(max_workers=8) as executor:
            embed_results = list(executor.map(embed_photo, uncached_approved))
            
        for photo, path, emb in embed_results:
            if photo not in cache:
                cache[photo] = {}
            cache[photo]["embedding"] = emb
            embeddings.append((photo, path, emb))
    else:
        if progress_callback:
            progress_callback(100, 100, "embedding")
            
    # Perform deduplication
    similarity_threshold = 0.92
    for i in range(len(embeddings)):
        photo_a, path_a, emb_a = embeddings[i]
        is_dup = False
        for unique_name, _, emb_u in unique_photos:
            sim = calculate_cosine_similarity(emb_a, emb_u)
            if sim > similarity_threshold:
                is_dup = True
                log(f"  [DUPLICATE] Dropped {photo_a} (identical to {unique_name})")
                break
        if not is_dup:
            unique_photos.append((photo_a, path_a, emb_a))
            
    log(f"Deduplication complete: Kept {len(unique_photos)}/{len(approved)} unique photos.")
    
    # Run Curation/Scoring in Parallel (only for uncached unique photos)
    log("Starting Phase 3: Curator Quality Evaluation...")
    scored_photos = []
    uncached_unique = []
    
    for photo, path, _ in unique_photos:
        if "scoring" in cache.get(photo, {}):
            scored_photos.append(cache[photo]["scoring"])
        else:
            uncached_unique.append((photo, path))
            
    if uncached_unique:
        log(f"  Scoring {len(uncached_unique)} uncached unique photos...")
        
        lock = threading.Lock()
        progress = {"count": 0}
        
        def score_photo(item):
            photo, path = item
            score_res = score_photo_as_judge(path)
            contributor_id = photo.split("_")[0] if "_" in photo else "unknown"
            
            with lock:
                progress["count"] += 1
                if progress_callback:
                    progress_callback(progress["count"], len(uncached_unique), "scoring")
                    
            return {
                "filename": photo,
                "score": score_res["score"],
                "sharpness": score_res["sharpness"],
                "composition": score_res["composition"],
                "uniqueness": score_res["uniqueness"],
                "human_presence": score_res["human_presence"],
                "scene_label": score_res["scene_label"],
                "caption": score_res["caption"],
                "contributor_id": contributor_id
            }

        with ThreadPoolExecutor(max_workers=8) as executor:
            score_results = list(executor.map(score_photo, uncached_unique))
            
        for s_res in score_results:
            photo = s_res["filename"]
            if photo not in cache:
                cache[photo] = {}
            cache[photo]["scoring"] = s_res
            scored_photos.append(s_res)
    else:
        if progress_callback:
            progress_callback(100, 100, "scoring")
            
    # Save the updated curation cache file
    try:
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        log(f"Warning: Failed to save curation cache: {e}")

    # Sort by quality score descending
    scored_photos.sort(key=lambda x: x["score"], reverse=True)
    
    # Save raw Curator manifest
    manifest_path = os.path.join(artefacts_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(scored_photos, f, indent=2)
        
    # --- Agent 4: Memory ---
    log("Starting Phase 4: Memory Agent profile indexing...")
    memory_store = MemoryBankStore()
    
    moments = {}
    for p in scored_photos:
        scene = p["scene_label"]
        if scene not in moments:
            moments[scene] = []
        moments[scene].append(p)
        
    # Update contributor profiles in Memory Bank
    contributors_activity = {}
    for p in scored_photos:
        cid = p["contributor_id"]
        if cid not in contributors_activity:
            # Look up registered name in Memory Bank first
            existing = memory_store.get_contributor_profile(cid)
            name = existing.get("name") if existing else "Contributor"
            
            # Map default or script uploads
            if cid == "unsplash":
                name = "Unsplash Stock"
                
            contributors_activity[cid] = {"name": name, "scenes": []}
        contributors_activity[cid]["scenes"].append(p["scene_label"])
        
    for cid, act in contributors_activity.items():
        moments_visited = list(set(act["scenes"]))
        memory_store.upsert_contributor(
            contributor_id=cid,
            contributor_name=act["name"],
            new_moments=moments_visited,
            photo_count=len(act["scenes"])
        )
        
    log("Memory Bank profiles updated successfully.")
    
    # Run Narrator Journaling in Parallel
    log("Starting Phase 5: Parallel Narrator Agent synthesis...")
    
    lock = threading.Lock()
    progress = {"count": 0}
    
    def generate_journal_for_moment(moment_items):
        scene, items = moment_items
        items.sort(key=lambda x: x["score"], reverse=True)
        top3_info = [{"filename": x["filename"], "caption": x["caption"], "score": x["score"]} for x in items[:3]]
        
        entry_text = generate_moment_journal(
            scene_label=scene,
            photos_info_json=json.dumps(top3_info)
        )
        
        with lock:
            progress["count"] += 1
            if progress_callback:
                progress_callback(progress["count"], len(moments), "journaling")
                
        return {
            "moment": scene,
            "entry": entry_text,
            "photos": [x["filename"] for x in items[:3]]
        }

    with ThreadPoolExecutor(max_workers=4) as executor:
        journal_entries = list(executor.map(generate_journal_for_moment, moments.items()))
        
    # Compile the final Trip Story
    log("  Compiling overall Trip Story...")
    full_story = generate_trip_story(
        journal_entries_json=json.dumps(journal_entries),
        destination=memory_store.get_trip_context().get("destination") or "our trip"
    )
    
    # Select highlights (top 12 overall photos)
    highlights = scored_photos[:12]
    
    # --- Write Final Artifacts ---
    with open(os.path.join(artefacts_dir, "highlights.json"), "w") as f:
        json.dump(highlights, f, indent=2)
        
    with open(os.path.join(artefacts_dir, "journal.json"), "w") as f:
        json.dump(journal_entries, f, indent=2)
        
    with open(os.path.join(artefacts_dir, "story.txt"), "w") as f:
        f.write(full_story)
        
    log("Pipeline execution complete! Artifacts written successfully.")
    
    return {
        "total_processed": len(photos),
        "approved": len(approved),
        "quarantined": len(quarantined),
        "unique": len(unique_photos),
        "moments_count": len(moments),
        "story_word_count": len(full_story.split()),
        "uncached_moderated": len(uncached_photos),
        "uncached_scored": len(uncached_unique)
    }
