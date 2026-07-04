import os
import json
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from agents.moderator.tools.vision_check import run_vision_moderation_batch
from agents.curator.tools.score import score_photos_as_judge_batch
from agents.curator.tools.embed import get_image_embedding, calculate_cosine_similarity
from agents.memory.tools.memory_bank import MemoryBankStore
from agents.narrator.tools.journal import generate_all_moments_journal
from agents.narrator.tools.story import generate_trip_story
from app.app_utils.storage import StorageHelper

CACHE_FILE = "curation_cache.json"

def get_photo_date_info(path: str) -> tuple[float, str]:
    from pipeline.local_cleaner import get_exif_metadata
    import datetime
    import os
    
    try:
        meta = get_exif_metadata(path)
        date_str = meta.get("date", "")
        if date_str:
            for fmt in ("%B %d, %Y at %I:%M %p", "%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M:%S\u0000"):
                try:
                    dt = datetime.datetime.strptime(date_str.strip(), fmt)
                    return dt.timestamp(), dt.strftime("%B %d, %Y")
                except:
                    pass
    except Exception as e:
        pass
        
    try:
        mtime = os.path.getmtime(path)
        dt = datetime.datetime.fromtimestamp(mtime)
        return mtime, dt.strftime("%B %d, %Y")
    except:
        return 0.0, "Unknown Date"

def execute_trip_pipeline(project_root: str, session_id: str = "default", limit: int = 50, log=print, progress_callback=None) -> dict:
    """
    Runs the sequential 5-agent pipeline on all photos uploaded to one event session.
    session_id selects the isolated storage namespace (see StorageHelper) so multiple
    events can be curated independently. Saves outputs to that session's artefacts/
    and returns stats. Uses concurrent threads, caching, and a progress callback to
    track states.
    """
    log("Starting MemoryWeaver Optimised Pipeline...")

    session_storage = StorageHelper(session_id=session_id)
    uploads_dir = os.path.join(session_storage.local_base, "uploads")
    artefacts_dir = os.path.join(session_storage.local_base, "artefacts")
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
    import time
    p1_start = time.time()
    
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
            
    # Run Moderation in Parallel for Uncached Photos (in batches of 50)
    if uncached_photos:
        log(f"  Processing {len(uncached_photos)} uncached photos for safety (in batches of 50)...")
        
        batch_size = 50
        batches = []
        for i in range(0, len(uncached_photos), batch_size):
            batch_items = []
            for photo in uncached_photos[i:i+batch_size]:
                batch_items.append({
                    "filename": photo,
                    "path": os.path.join(uploads_dir, photo)
                })
            batches.append(batch_items)
            
        lock = threading.Lock()
        progress = {"count": 0}
        
        def process_moderation_batch(batch):
            res_list = run_vision_moderation_batch(batch)
            
            with lock:
                progress["count"] += len(batch)
                if progress_callback:
                    progress_callback(progress["count"], len(uncached_photos), "moderation")
                    
            mapped = []
            for idx, item in enumerate(batch):
                mapped.append((item["filename"], item["path"], res_list[idx]))
            return mapped

        with ThreadPoolExecutor(max_workers=4) as executor:
            batch_results = list(executor.map(process_moderation_batch, batches))
            
        for b_res in batch_results:
            for photo, full_path, mod_res in b_res:
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
        
    log(f"Phase 1 complete: {len(approved)} approved, {len(quarantined)} quarantined in {time.time() - p1_start:.1f}s.")
    
    # Run Embedding in Parallel (only for uncached approved photos)
    log("Starting Phase 2: Curator Deduplication...")
    p2_start = time.time()
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
    similarity_threshold = 0.88
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
            
    log(f"Phase 2 complete: Kept {len(unique_photos)}/{len(approved)} unique photos in {time.time() - p2_start:.1f}s.")
    
    # Run Curation/Scoring in Parallel (only for uncached unique photos)
    log("Starting Phase 3: Curator Quality Evaluation...")
    p3_start = time.time()
    scored_photos = []
    uncached_unique = []
    
    for photo, path, _ in unique_photos:
        if "scoring" in cache.get(photo, {}):
            scored_photos.append(cache[photo]["scoring"])
        else:
            uncached_unique.append((photo, path))
            
    if uncached_unique:
        log(f"  Scoring {len(uncached_unique)} uncached unique photos (in batches of 15)...")
        
        batch_size = 15
        batches = []
        for i in range(0, len(uncached_unique), batch_size):
            batch_items = []
            for photo, path in uncached_unique[i:i+batch_size]:
                batch_items.append({
                    "filename": photo,
                    "path": path
                })
            batches.append(batch_items)
            
        lock = threading.Lock()
        progress = {"count": 0}
        
        def process_scoring_batch(batch):
            res_list = score_photos_as_judge_batch(batch)
            
            with lock:
                progress["count"] += len(batch)
                if progress_callback:
                    progress_callback(progress["count"], len(uncached_unique), "scoring")
                    
            mapped = []
            for idx, item in enumerate(batch):
                filename = item["filename"]
                contributor_id = filename.split("_")[0] if "_" in filename else "unknown"
                score_res = res_list[idx]
                
                mapped.append({
                    "filename": filename,
                    "score": score_res["score"],
                    "sharpness": score_res["sharpness"],
                    "composition": score_res["composition"],
                    "uniqueness": score_res["uniqueness"],
                    "human_presence": score_res["human_presence"],
                    "scene_label": score_res["scene_label"],
                    "caption": score_res["caption"],
                    "contributor_id": contributor_id
                })
            return mapped

        with ThreadPoolExecutor(max_workers=4) as executor:
            batch_results = list(executor.map(process_scoring_batch, batches))
            
        for b_res in batch_results:
            for s_res in b_res:
                photo = s_res["filename"]
                if photo not in cache:
                    cache[photo] = {}
                cache[photo]["scoring"] = s_res
                scored_photos.append(s_res)
    else:
        if progress_callback:
            progress_callback(100, 100, "scoring")
            
    log(f"Phase 3 complete: Evaluated {len(scored_photos)} photos in {time.time() - p3_start:.1f}s.")
            
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
    memory_store = MemoryBankStore(session_id)

    # Extract date & timestamp for chronological sorting (uploads_dir computed above)
    for p in scored_photos:
        p_path = os.path.join(uploads_dir, p["filename"])
        ts, date_str = get_photo_date_info(p_path)
        p["timestamp"] = ts
        p["date"] = date_str

    # Apply diversity-maximizing size limits if requested
    selected_photos = list(scored_photos)
    if limit > 0 and limit < len(scored_photos):
        log(f"  Applying diversity-maximizing filter to select {limit} photos out of {len(scored_photos)}...")
        scene_groups = {}
        for p in scored_photos:
            s = p["scene_label"]
            if s not in scene_groups:
                scene_groups[s] = []
            scene_groups[s].append(p)
            
        for s in scene_groups:
            scene_groups[s].sort(key=lambda x: x["score"], reverse=True)
            
        sorted_scene_keys = sorted(
            scene_groups.keys(),
            key=lambda k: min([p.get("timestamp", 9999999999.0) for p in scene_groups[k]])
        )
        
        selected_photos = []
        selected_filenames = set()
        
        added_any = True
        while len(selected_photos) < limit and added_any:
            added_any = False
            for s in sorted_scene_keys:
                group = scene_groups[s]
                for p in group:
                    if p["filename"] not in selected_filenames:
                        selected_photos.append(p)
                        selected_filenames.add(p["filename"])
                        added_any = True
                        break
                if len(selected_photos) == limit:
                    break
        log(f"  Diversity filter selected {len(selected_photos)} photos across {len(set(p['scene_label'] for p in selected_photos))} scenes.")

    moments = {}
    for p in selected_photos:
        scene = p["scene_label"]
        if scene not in moments:
            moments[scene] = []
        moments[scene].append(p)
        
    sorted_moments = sorted(
        moments.items(),
        key=lambda x: min([p.get("timestamp", 9999999999.0) for p in x[1]])
    )
        
    # Update contributor profiles in Memory Bank
    contributors_activity = {}
    for p in selected_photos:
        cid = p["contributor_id"]
        if cid not in contributors_activity:
            existing = memory_store.get_contributor_profile(cid)
            name = existing.get("name") if existing else "Contributor"
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
    
    # Run Narrator Journaling in a Single Batched API Call
    log("Starting Phase 5: Batched Narrator Agent synthesis (single API call)...")
    p5_start = time.time()
    
    batched_moments_data = []
    for scene, items in sorted_moments:
        items.sort(key=lambda x: x["score"], reverse=True)
        top3_info = [{"filename": x["filename"], "caption": x["caption"], "score": x["score"]} for x in items[:3]]
        
        # Get coordinates and date
        earliest_p = min(items, key=lambda x: x.get("timestamp", 9999999999.0))
        moment_date = earliest_p.get("date", "Unknown Date")
        
        batched_moments_data.append({
            "moment": scene,
            "date": moment_date,
            "top_photos": top3_info
        })
        
    if progress_callback:
        progress_callback(50, 100, "journaling")
        
    batched_entries_dict = generate_all_moments_journal(batched_moments_data)
    
    journal_entries = []
    for scene, items in sorted_moments:
        items.sort(key=lambda x: x["score"], reverse=True)
        earliest_p = min(items, key=lambda x: x.get("timestamp", 9999999999.0))
        moment_date = earliest_p.get("date", "Unknown Date")
        
        # Fallback if API returned missing keys
        entry_text = batched_entries_dict.get(scene)
        if not entry_text:
            entry_text = f"We spent time exploring around {scene.replace('_', ' ')}."
            
        journal_entries.append({
            "moment": scene,
            "entry": entry_text,
            "photos": [x["filename"] for x in items[:3]],
            "date": moment_date
        })
        
    if progress_callback:
        progress_callback(100, 100, "journaling")
        
    log(f"Phase 5 complete: Generated batched journal narrative in {time.time() - p5_start:.1f}s.")
        
    # Compile the final Trip Story
    log("  Compiling overall Trip Story...")
    full_story = generate_trip_story(
        journal_entries_json=json.dumps(journal_entries),
        destination=memory_store.get_trip_context().get("destination") or "our trip"
    )
    
    # Select highlights (top 12 overall photos from selected diverse set)
    highlights = selected_photos[:12]
    
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
