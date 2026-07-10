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

def transcribe_audio_file(file_path: str, log=print) -> str:
    from google import genai
    from google.genai import types
    import os
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "[Transcription Failed: GEMINI_API_KEY environment variable is not set]"
    
    log(f"  [Transcription] transcribing audio note: {os.path.basename(file_path)}...")
    try:
        client = genai.Client(api_key=api_key)
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
        part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
        
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=["Transcribe this voice memo recording text accurately. Do not add explanations or formatting, just return the transcription text.", part]
        )
        transcript = response.text.strip()
        log(f"  [Transcription] Success: \"{transcript[:50]}...\"")
        return transcript
    except Exception as e:
        log(f"  [Transcription] Error: {e}")
        return f"[Transcription Failed: {e}]"

def ocr_and_classify_document(file_path: str, log=print) -> dict:
    from google import genai
    from google.genai import types
    import os
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {"ocr_text": "[OCR Failed: API Key not set]", "doc_kind": "other"}
        
    client = genai.Client(api_key=api_key)
    log(f"  [OCR] processing document: {os.path.basename(file_path)}...")
    
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
            
        mime = "application/pdf" if file_path.lower().endswith(".pdf") else "image/jpeg"
        
        prompt = (
            "Extract all readable text from this document. Then, classify it as one of the following kinds:\n"
            "doc_kind: ticket, menu, map, boarding_pass, or other.\n"
            "Format the response exactly as: \n"
            "DOC_KIND: <kind>\n"
            "TEXT: <extracted text>"
        )
        
        res = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[
                types.Part.from_bytes(data=file_bytes, mime_type=mime),
                prompt
            ]
        )
        
        text = res.text.strip()
        doc_kind = "other"
        ocr_text = text
        
        for line in text.splitlines():
            if line.upper().startswith("DOC_KIND:"):
                candidate = line.split(":", 1)[1].strip().lower()
                if candidate in ["ticket", "menu", "map", "boarding_pass", "other"]:
                    doc_kind = candidate
            elif line.upper().startswith("TEXT:"):
                ocr_text = text.split(line, 1)[1].strip()
                
        log(f"  [OCR] Success: Classified as {doc_kind}.")
        return {"ocr_text": ocr_text, "doc_kind": doc_kind}
    except Exception as e:
        log(f"  [OCR] Error document OCR on {file_path}: {e}")
        return {"ocr_text": f"[OCR Failed: {e}]", "doc_kind": "other"}

def execute_trip_pipeline(project_root: str, session_id: str = "default", limit: int = 50, log=print, progress_callback=None, stage: str = "all") -> dict:
    """
    Runs the sequential 5-agent pipeline on all photos in local_storage/uploads.
    Saves outputs to local_storage/artefacts/ and returns stats.
    Uses concurrent threads, caching, and a progress callback to track states.
    """
    import time
    log(f"Starting MemoryWeaver Optimised Pipeline (stage: {stage})...")
    
    trajectory = {
        "pipeline_name": "MemoryWeaver Multi-Agent Pipeline",
        "started_at": datetime.datetime.utcnow().isoformat(),
        "steps": [],
        "metadata": {}
    }
    
    session_storage = StorageHelper(session_id=session_id)
    uploads_dir = os.path.join(session_storage.local_base, "uploads")
    artefacts_dir = os.path.join(session_storage.local_base, "artefacts")
    os.makedirs(artefacts_dir, exist_ok=True)
    
    if stage == "narrate":
        # Load user-reviewed highlights and scored manifest
        manifest_path = os.path.join(artefacts_dir, "manifest.json")
        if not os.path.exists(manifest_path):
            raise ValueError("Curation manifest not found. Please run curation stage first.")
        with open(manifest_path) as f:
            manifest_scored = json.load(f)
            
        highlights_path = os.path.join(artefacts_dir, "highlights.json")
        if not os.path.exists(highlights_path):
            raise ValueError("Highlights file not found. Please run curation stage first.")
        with open(highlights_path) as f:
            highlights_data = json.load(f)
            
        reviewed_filenames = set()
        for h in highlights_data:
            if isinstance(h, dict) and "filename" in h:
                reviewed_filenames.add(h["filename"])
            elif isinstance(h, str):
                reviewed_filenames.add(h)
                
        selected_photos = [p for p in manifest_scored if p["filename"] in reviewed_filenames]
        log(f"Stage 'narrate': loaded {len(selected_photos)} selected photos from highlights.json.")
        
        # Fake approved / photos count for the rest of the statistics
        photos = list(reviewed_filenames)
        approved = list(reviewed_filenames)
        quarantined = []
        unique_photos = []
        uncached_photos = []
        uncached_unique = []
        # Jump directly to Phase 4
        
    else:
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
            
        # Filter out photos excluded by the user
        from app.app_utils.sessions import SessionStore
        store = SessionStore()
        session = store.get_session(session_id)
        excluded = set(session.get("excluded_photos", [])) if session else set()

        photos = [f for f in os.listdir(uploads_dir) 
                  if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic')) and f not in excluded]
                  
        if not photos:
            raise ValueError("No photos found in the upload pool. Upload photos first.")
            
        log(f"Found {len(photos)} photo(s). Starting Phase 1: Moderation...")
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
            
            if progress_callback:
                progress_callback(0, len(uncached_photos), "moderation")
                
            def process_mod_batch(batch):
                res_list = run_vision_moderation_batch(batch)
                with lock:
                    progress["count"] += len(batch)
                    if progress_callback:
                        progress_callback(progress["count"], len(uncached_photos), "moderation")
                mapped = []
                for idx, item in enumerate(batch):
                    mod_res = res_list[idx].copy()
                    mod_res["filename"] = item["filename"]
                    mapped.append(mod_res)
                return mapped
                
            with ThreadPoolExecutor(max_workers=4) as executor:
                batch_results = list(executor.map(process_mod_batch, batches))
                
            for b_res in batch_results:
                for mod_res in b_res:
                    photo = mod_res["filename"]
                    if photo not in cache:
                        cache[photo] = {}
                    cache[photo]["moderation"] = mod_res
                    
                    if mod_res["usable"]:
                        approved.append((photo, os.path.join(uploads_dir, photo)))
                    else:
                        quarantined.append({"filename": photo, "reason": mod_res["reason"]})
                        log(f"  [QUARANTINE] {photo} - {mod_res['reason']}")
        else:
            if progress_callback:
                progress_callback(100, 100, "moderation")
                
        log(f"Phase 1 complete: Approved {len(approved)} photos. Quarantined {len(quarantined)} photos in {time.time() - p1_start:.1f}s.")
        trajectory["steps"].append({
            "phase": 1,
            "name": "Moderator Agent (Image Safety & Usability Checks)",
            "duration_seconds": round(time.time() - p1_start, 2),
            "inputs": len(photos),
            "outputs": {
                "approved": len(approved),
                "quarantined": len(quarantined)
            },
            "cached_hits": len(photos) - len(uncached_photos)
        })
        
        # --- Agent 2: Curator (Deduplication) ---
        p2_start = time.time()
        log("Starting Phase 2: Curator Agent photo deduplication...")
        
        # Run Embedding in Parallel (only for uncached approved photos)
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
            
            if progress_callback:
                progress_callback(0, len(uncached_approved), "embedding")
                
            def embed_photo(item):
                photo, path = item
                emb = get_image_embedding(path)
                with lock:
                    progress["count"] += 1
                    if progress_callback:
                        progress_callback(progress["count"], len(uncached_approved), "embedding")
                return photo, emb
                
            with ThreadPoolExecutor(max_workers=4) as executor:
                embed_results = list(executor.map(embed_photo, uncached_approved))
                
            for photo, emb in embed_results:
                if photo not in cache:
                    cache[photo] = {}
                cache[photo]["embedding"] = emb
                # Find path
                path = next(p for ph, p in approved if ph == photo)
                embeddings.append((photo, path, emb))
        else:
            if progress_callback:
                progress_callback(100, 100, "embedding")
                
        log(f"  Embeddings ready. Performing clustering deduplication...")
        # Sort chronologically by filename or mtime to ensure stable deduplication
        embeddings.sort(key=lambda x: x[0])
        
        unique_photos = []
        for i, (photo, path, emb) in enumerate(embeddings):
            is_dupe = False
            for u_photo, u_path, u_emb in unique_photos:
                sim = calculate_cosine_similarity(emb, u_emb)
                if sim > 0.85:
                    is_dupe = True
                    log(f"  [DUPLICATE DETECTED] {photo} is similar to {u_photo} (similarity: {sim:.3f})")
                    break
            if not is_dupe:
                unique_photos.append((photo, path, emb))
                
        log(f"Phase 2 complete: Retained {len(unique_photos)} unique photos out of {len(approved)} in {time.time() - p2_start:.1f}s.")
        trajectory["steps"].append({
            "phase": 2,
            "name": "Curator Agent (CLIP Embedding & Near-Duplicate Filtering)",
            "duration_seconds": round(time.time() - p2_start, 2),
            "inputs": len(approved),
            "outputs": {
                "unique": len(unique_photos)
            },
            "cached_hits": len(approved) - len(uncached_approved)
        })
        
        # --- Agent 3: Curator (LLM-as-Judge Quality Scoring) ---
        p3_start = time.time()
        log("Starting Phase 3: Curator Agent quality evaluation...")
        
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
            
            if progress_callback:
                progress_callback(0, len(uncached_unique), "scoring")
                
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
        trajectory["steps"].append({
            "phase": 3,
            "name": "Curator Agent (LLM-as-Judge Quality Scoring)",
            "duration_seconds": round(time.time() - p3_start, 2),
            "inputs": len(unique_photos),
            "outputs": {
                "scored": len(scored_photos)
            },
            "cached_hits": len(unique_photos) - len(uncached_unique)
        })
                
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

        # Extract date & timestamp for chronological sorting
        uploads_dir = os.path.join(session_storage.local_base, "uploads")
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

        if stage == "curate":
            # Write highlights.json with all selected curation photos
            with open(os.path.join(artefacts_dir, "highlights.json"), "w") as f:
                json.dump(selected_photos, f, indent=2)
                
            # Write empty journal.json and placeholder story.txt
            with open(os.path.join(artefacts_dir, "journal.json"), "w") as f:
                json.dump([], f, indent=2)
                
            with open(os.path.join(artefacts_dir, "story.txt"), "w") as f:
                f.write("Storybook synthesis pending approval...")
                
            log("Curation stage complete! Selected highlights saved to highlights.json.")
            return {
                "total_processed": len(photos),
                "approved": len(approved),
                "quarantined": len(quarantined),
                "unique": len(unique_photos),
                "moments_count": 0,
                "story_word_count": 0,
                "uncached_moderated": len(uncached_photos),
                "uncached_scored": len(uncached_unique)
            }

    # --- Agent 4: Memory ---
    p4_start = time.time()
    log("Starting Phase 4: Memory Agent profile indexing...")
    memory_store = MemoryBankStore(session_id)

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
    trajectory["steps"].append({
        "phase": 4,
        "name": "Memory Agent (Cross-Session Profile Indexing)",
        "duration_seconds": round(time.time() - p4_start, 2),
        "inputs": len(selected_photos),
        "outputs": {
            "profiles_updated": len(contributors_activity)
        }
    })
    
    # Scan for user uploaded text notes, voice memos, and calendar events
    all_files = os.listdir(uploads_dir) if os.path.exists(uploads_dir) else []
    text_files = [f for f in all_files if f.lower().endswith(".txt") and "story_note" in f.lower()]
    audio_files = [f for f in all_files if f.lower().endswith((".wav", ".mp3", ".m4a"))]
    doc_files = [f for f in all_files if f.lower().endswith(".pdf")]
    
    # Load calendar events
    calendar_events = []
    cal_path = os.path.join(uploads_dir, "calendar_events.json")
    if os.path.exists(cal_path):
        try:
            with open(cal_path, "r", encoding="utf-8") as cf:
                calendar_events = json.load(cf)
        except Exception as e:
            log(f"Warning: Failed to load calendar events: {e}")
            
    # Transcribe audio files using Gemini
    audio_transcriptions = {}
    for f in audio_files:
        f_path = os.path.join(uploads_dir, f)
        audio_transcriptions[f] = transcribe_audio_file(f_path, log)
        
    # Read text notes
    text_notes = {}
    for f in text_files:
        f_path = os.path.join(uploads_dir, f)
        try:
            with open(f_path, "r", encoding="utf-8") as tf:
                text_notes[f] = tf.read().strip()
        except Exception as e:
            log(f"Warning: Failed to read text file {f}: {e}")

    # Process document OCR and classification using Gemini
    doc_infos = {}
    for f in doc_files:
        f_path = os.path.join(uploads_dir, f)
        doc_infos[f] = ocr_and_classify_document(f_path, log)
            
    # Match extra files to the closest photo moment timestamp
    scene_notes = {}
    scene_audios = {}
    scene_calendars = {}
    scene_docs = {}
    
    # Helper to find nearest scene label
    def find_nearest_scene(timestamp):
        if not selected_photos:
            return None
        closest_p = min(selected_photos, key=lambda x: abs(x.get("timestamp", 0.0) - timestamp))
        return closest_p["scene_label"]
        
    for f in text_files:
        f_path = os.path.join(uploads_dir, f)
        mtime = os.path.getmtime(f_path)
        scene = find_nearest_scene(mtime)
        if scene:
            if scene not in scene_notes: scene_notes[scene] = []
            scene_notes[scene].append(text_notes[f])
            
    for f in audio_files:
        f_path = os.path.join(uploads_dir, f)
        mtime = os.path.getmtime(f_path)
        scene = find_nearest_scene(mtime)
        if scene:
            if scene not in scene_audios: scene_audios[scene] = []
            scene_audios[scene].append({"filename": f, "transcription": audio_transcriptions[f]})
            
    for ev in calendar_events:
        ts = ev.get("timestamp", 0.0)
        scene = find_nearest_scene(ts)
        if scene:
            if scene not in scene_calendars: scene_calendars[scene] = []
            scene_calendars[scene].append(ev.get("title", "Event"))

    for f in doc_files:
        f_path = os.path.join(uploads_dir, f)
        mtime = os.path.getmtime(f_path)
        scene = find_nearest_scene(mtime)
        if scene:
            if scene not in scene_docs: scene_docs[scene] = []
            info = doc_infos[f]
            scene_docs[scene].append({
                "filename": f,
                "doc_kind": info["doc_kind"],
                "ocr_text": info["ocr_text"]
            })

    # Run Narrator Journaling in a Single Batched API Call
    log("Starting Phase 5: Batched Narrator Agent synthesis (single API call)...")
    p5_start = time.time()
    
    if progress_callback:
        progress_callback(0, 100, "journaling")
        
    batched_moments_data = []
    for scene, items in sorted_moments:
        items.sort(key=lambda x: x["score"], reverse=True)

        # Build rich photo details including all EXIF fields for the narrator
        top3_info = []
        for x in items[:3]:
            photo_detail = {
                "filename": x["filename"],
                "caption": x["caption"],
                "score": x["score"],
            }
            # Attach EXIF metadata if available using the full rich extractor
            try:
                from agents.collector.tools.upload import extract_exif
                exif_meta = extract_exif(os.path.join(uploads_dir, x["filename"]))
                # Map rich EXIF fields that are useful for journaling
                for field in [
                    "scene_type", "light_source", "flash", "subject_distance",
                    "brightness", "exposure_program", "artist", "image_description",
                    "user_comment", "gps_speed", "gps_track", "altitude", "heading",
                    "timestamp_original", "aperture", "focal_length", "iso"
                ]:
                    val = exif_meta.get(field)
                    if val:
                        photo_detail[field] = val
                # GPS as readable coords
                gps = exif_meta.get("gps", {})
                if gps.get("latitude") and gps.get("longitude"):
                    photo_detail["gps"] = f"{gps['latitude']:.4f}, {gps['longitude']:.4f}"
            except Exception:
                pass
            top3_info.append(photo_detail)

        # Get coordinates and date
        earliest_p = min(items, key=lambda x: x.get("timestamp", 9999999999.0))
        moment_date = earliest_p.get("date", "Unknown Date")

        batched_moments_data.append({
            "moment": scene,
            "date": moment_date,
            "top_photos": top3_info,
            "user_notes": scene_notes.get(scene, []),
            "voice_transcripts": [x["transcription"] for x in scene_audios.get(scene, [])],
            "calendar_events": scene_calendars.get(scene, []),
            "documents": scene_docs.get(scene, [])
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
            "date": moment_date,
            "notes": scene_notes.get(scene, []),
            "audio_notes": scene_audios.get(scene, []),
            "calendar_events": scene_calendars.get(scene, []),
            "documents": scene_docs.get(scene, [])
        })
        
    if progress_callback:
        progress_callback(100, 100, "journaling")
        
    log(f"Phase 5 complete: Generated batched journal narrative in {time.time() - p5_start:.1f}s.")
        
    # Compile the final Trip Story
    log("  Compiling overall Trip Story...")
    curation_instructions = ""
    inst_path = os.path.join(uploads_dir, "curation_instructions.txt")
    if os.path.exists(inst_path):
        try:
            with open(inst_path, "r", encoding="utf-8") as inf:
                curation_instructions = inf.read().strip()
        except Exception as e:
            log(f"Warning: Failed to load curation instructions: {e}")

    full_story = generate_trip_story(
        journal_entries_json=json.dumps(journal_entries),
        destination=memory_store.get_trip_context().get("destination") or "our trip",
        curation_instructions=curation_instructions
    )
    
    trajectory["steps"].append({
        "phase": 5,
        "name": "Narrator Agent (Journal & Story Compilation)",
        "duration_seconds": round(time.time() - p5_start, 2),
        "inputs": {
            "moments_count": len(moments),
            "journal_entries": len(journal_entries)
        },
        "outputs": {
            "story_word_count": len(full_story.split())
        }
    })
    
    # Select highlights (top 12 overall photos from selected diverse set)
    highlights = selected_photos[:12]
    
    # Write vibe trajectory trace
    trajectory["ended_at"] = datetime.datetime.utcnow().isoformat()
    trajectory["metadata"] = {
        "total_processed": len(photos),
        "approved": len(approved),
        "quarantined": len(quarantined),
        "unique": len(unique_photos),
        "moments_count": len(moments),
        "uncached_moderated": len(uncached_photos),
        "uncached_scored": len(uncached_unique)
    }
    
    with open(os.path.join(artefacts_dir, "vibe_trajectory.json"), "w") as f:
        json.dump(trajectory, f, indent=2)
        
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
