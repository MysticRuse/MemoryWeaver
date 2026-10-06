import os
import json
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from agents.moderator.tools.vision_check import run_vision_moderation_batch
from agents.curator.tools.score import score_photos_as_judge_batch
from agents.curator.tools.embed import get_image_embedding_checked, calculate_cosine_similarity, EMBEDDING_TAG
from agents.memory.tools.memory_bank import MemoryBankStore
from agents.narrator.tools.journal import generate_all_moments_journal_strict
from agents.narrator.tools.story import generate_trip_story_strict
from app.app_utils.storage import StorageHelper

CACHE_FILE = "curation_cache.json"

# Two photos whose embedding cosine similarity exceeds this are treated as
# near-duplicates (a burst) and only the first is kept.
#
# Calibrated on real photos with gemini-embedding-2 @ 768 dims: re-compressed /
# resized / brightness-shifted / cropped copies of a photo scored >= 0.943,
# while genuinely different photos of the same subject (e.g. one laptop on a
# table, shot from different angles) scored up to ~0.87 (one pair at 0.948).
# 0.92 sits in the gap. The previous 0.85 was a guess for a model that never
# actually ran; on real vectors it would merge same-subject photos that are not
# duplicates - and wrongly dropping a unique photo costs more than keeping two
# similar ones.
DUPLICATE_SIMILARITY_THRESHOLD = 0.92


class PipelineAPIError(RuntimeError):
    """A Gemini call failed (bad key, quota, network, unparseable reply).

    The pipeline raises this *before* writing any artifact, so a failed run
    leaves the event's existing highlights/journal/story untouched instead of
    reporting "complete" over an empty or filler result.
    """


def _save_cache(cache_path: str, cache: dict, log) -> None:
    """Persists the per-photo curation cache (best effort)."""
    try:
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        log(f"Warning: Failed to save curation cache: {e}")


# Placeholder results the leaf agents emit when an API call fails. Older runs
# cached these as if they were real verdicts, permanently excluding or
# mis-scoring the photo; recognise them so those cache entries get recomputed.
_MODERATION_FAILURE_REASON = "Moderation batch request failed or skipped"


def _is_poisoned_moderation(mod: dict) -> bool:
    return str(mod.get("reason", "")).startswith(_MODERATION_FAILURE_REASON)


def _is_poisoned_scoring(scoring: dict) -> bool:
    return scoring.get("scene_label") == "unknown" and scoring.get("caption") == "Exploring the sights."

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

def _attach_capture_times(photos: list, uploads_dir: str) -> int:
    """Sets "timestamp" (epoch seconds) and "date" on every photo dict that lacks them.

    Capture time (EXIF, else file mtime) is what orders the journal
    chronologically. It has to be stored on the dicts that get written to
    manifest.json, because the Proceed stage rebuilds its photo list from that
    file: times computed only in memory were lost, every photo then looked
    undated, and moments fell back to score order. Returns how many photos
    were filled in.
    """
    filled = 0
    for p in photos:
        if "timestamp" in p and "date" in p:
            continue
        ts, date_str = get_photo_date_info(os.path.join(uploads_dir, p["filename"]))
        p["timestamp"] = ts
        p["date"] = date_str
        filled += 1
    return filled


def estimate_usage(uncached_moderated: int, uncached_scored: int, moments_count: int) -> dict:
    """Rough token and cost estimate for the Gemini calls a run actually made.

    ESTIMATES, not measurements: fixed per-item token guesses times Flash-tier
    list prices. Only uncached work counts (cached photos cost nothing), and the
    per-photo embedding calls are not priced here (they are counted separately
    as "embedding_calls" in the trajectory).
    """
    input_tokens = uncached_moderated * 1000 + uncached_scored * 1500 + moments_count * 800
    output_tokens = uncached_moderated * 100 + uncached_scored * 150 + moments_count * 250
    cost_usd = input_tokens * 0.000075 / 1000 + output_tokens * 0.0003 / 1000
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost_usd}


def _write_trajectory(artefacts_dir: str, session_id: str, stage: str, trajectory: dict, usage: dict) -> None:
    """Records one stage of a run in vibe_trajectory.json, merged with the others.

    Curate and Proceed are separate runs, so each writes its own entry under
    "stages" (re-running a stage replaces only that stage's entry; a full "all"
    run replaces both). The file always holds the complete picture: every phase
    that has run, per-phase duration and cache hits, and estimated usage/cost
    per stage plus a running total.
    """
    path = os.path.join(artefacts_dir, "vibe_trajectory.json")
    stages = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                stages = json.load(f).get("stages", {})
        except Exception:
            stages = {}  # an unreadable old file is simply started over
    if stage == "all":
        stages = {}
    else:
        stages.pop("all", None)
    stages[stage] = {
        "started_at": trajectory["started_at"],
        "ended_at": trajectory["ended_at"],
        "steps": trajectory["steps"],
        "metadata": trajectory["metadata"],
        "estimated_usage": usage,
    }
    totals = {k: sum(s["estimated_usage"][k] for s in stages.values()) for k in ("input_tokens", "output_tokens", "cost_usd")}
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    document = {
        "pipeline_name": trajectory["pipeline_name"],
        "session_id": session_id,
        "stages": stages,
        "steps": sorted((step for s in stages.values() for step in s["steps"]), key=lambda step: step["phase"]),
        "estimated_usage_total": {**totals, "note": "Estimates (fixed per-item token guesses x list prices), not measured usage."},
    }
    with open(path, "w") as f:
        json.dump(document, f, indent=2)


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

        # Manifests written before capture times were persisted have none; read
        # them from the original files so those events also come out in time order.
        backfilled = _attach_capture_times(selected_photos, uploads_dir)
        if backfilled:
            log(f"  Read capture times for {backfilled} photo(s) from the original files (older manifest had none).")
        
        # Fake approved / photos count for the rest of the statistics
        photos = list(reviewed_filenames)
        approved = list(reviewed_filenames)
        quarantined = []
        unique_photos = []
        uncached_photos = []
        uncached_unique = []
        uncached_approved = []
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
            if photo in cache and _is_poisoned_moderation(cache[photo].get("moderation", {})):
                # Cached from an earlier failed API call, not a real verdict: retry it
                uncached_photos.append(photo)
            elif photo in cache:
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
                
            failed_mod = 0   # whole batch failed (API/parse error)
            skipped_mod = 0  # a good reply that simply omitted this photo
            for b_res in batch_results:
                for mod_res in b_res:
                    photo = mod_res["filename"]
                    if mod_res.get("transient"):
                        # No real verdict. Never cache it: a cached fake quarantine
                        # would exclude this photo from every future run.
                        if mod_res.get("api_error"):
                            failed_mod += 1
                        else:
                            skipped_mod += 1
                        continue
                    if photo not in cache:
                        cache[photo] = {}
                    cache[photo]["moderation"] = mod_res

                    if mod_res["usable"]:
                        approved.append((photo, os.path.join(uploads_dir, photo)))
                    else:
                        quarantined.append({"filename": photo, "reason": mod_res["reason"]})
                        log(f"  [QUARANTINE] {photo} - {mod_res['reason']}")

            if failed_mod:
                _save_cache(cache_path, cache, log)  # keep verdicts from batches that did succeed
                raise PipelineAPIError(
                    f"Photo moderation failed for {failed_mod} photo(s) - the Gemini API call errored "
                    "(check the API key, quota, and network). Nothing was changed; "
                    "run again once the API is reachable. Photos already checked are cached."
                )
            if skipped_mod:
                log(f"  Warning: {skipped_mod} photo(s) got no moderation verdict and were skipped this run; they will be retried next run.")
        else:
            if progress_callback:
                progress_callback(100, 100, "moderation")
                
        log(f"Phase 1 complete: Approved {len(approved)} photos. Quarantined {len(quarantined)} photos in {time.time() - p1_start:.1f}s.")
        if not approved:
            _save_cache(cache_path, cache, log)
            raise ValueError("No usable photos: every photo was quarantined or got no moderation verdict. Nothing was changed.")
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
            # Reuse a cached vector only if it came from the current embedding
            # model/dimension; older caches (other model, or fake fallback
            # vectors) are recomputed once so vectors are never mixed.
            cached = cache.get(photo, {})
            if "embedding" in cached and cached.get("embedding_model") == EMBEDDING_TAG:
                embeddings.append((photo, path, cached["embedding"]))
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
                emb, is_fallback = get_image_embedding_checked(path)
                with lock:
                    progress["count"] += 1
                    if progress_callback:
                        progress_callback(progress["count"], len(uncached_approved), "embedding")
                return photo, emb, is_fallback

            with ThreadPoolExecutor(max_workers=4) as executor:
                embed_results = list(executor.map(embed_photo, uncached_approved))

            fallback_embeddings = 0
            for photo, emb, is_fallback in embed_results:
                if is_fallback:
                    # Placeholder vector (embedding API failed): usable for this run only.
                    # Caching it would pin the photo to a vector with no visual meaning.
                    fallback_embeddings += 1
                else:
                    if photo not in cache:
                        cache[photo] = {}
                    cache[photo]["embedding"] = emb
                    cache[photo]["embedding_model"] = EMBEDDING_TAG
                # Find path
                path = next(p for ph, p in approved if ph == photo)
                embeddings.append((photo, path, emb))
            if fallback_embeddings:
                log(f"  Warning: embedding API unavailable for {fallback_embeddings} photo(s) - near-duplicate detection is degraded this run (placeholder vectors, not cached).")
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
                if len(emb) != len(u_emb):
                    # e.g. a placeholder fallback vector next to a real one: the
                    # similarity of vectors of different sizes is meaningless.
                    continue
                sim = calculate_cosine_similarity(emb, u_emb)
                if sim > DUPLICATE_SIMILARITY_THRESHOLD:
                    is_dupe = True
                    log(f"  [DUPLICATE DETECTED] {photo} is similar to {u_photo} (similarity: {sim:.3f})")
                    break
            if not is_dupe:
                unique_photos.append((photo, path, emb))
                
        log(f"Phase 2 complete: Retained {len(unique_photos)} unique photos out of {len(approved)} in {time.time() - p2_start:.1f}s.")
        trajectory["steps"].append({
            "phase": 2,
            "name": "Curator Agent (Image Embedding & Near-Duplicate Filtering)",
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
            if "scoring" in cache.get(photo, {}) and not _is_poisoned_scoring(cache[photo]["scoring"]):
                scored_photos.append(cache[photo]["scoring"])
            else:
                # Not cached yet, or cached from an earlier failed API call (placeholder score): (re)score
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
                        "contributor_id": contributor_id,
                        # Failure markers from the scorer; consumed (and removed) below, never cached
                        "transient": score_res.get("transient", False),
                        "api_error": score_res.get("api_error", False),
                    })
                return mapped

            with ThreadPoolExecutor(max_workers=4) as executor:
                batch_results = list(executor.map(process_scoring_batch, batches))
                
            failed_score = 0   # whole batch failed (API/parse error)
            skipped_score = 0  # a good reply that simply omitted this photo
            for b_res in batch_results:
                for s_res in b_res:
                    transient = s_res.pop("transient", False)
                    api_error = s_res.pop("api_error", False)
                    if transient:
                        # Placeholder score, not a judgment: don't cache it or rank with it.
                        if api_error:
                            failed_score += 1
                        else:
                            skipped_score += 1
                        continue
                    photo = s_res["filename"]
                    if photo not in cache:
                        cache[photo] = {}
                    cache[photo]["scoring"] = s_res
                    scored_photos.append(s_res)

            if failed_score:
                _save_cache(cache_path, cache, log)  # keep moderation/embeddings/scores that did succeed
                raise PipelineAPIError(
                    f"Photo scoring failed for {failed_score} photo(s) - the Gemini API call errored "
                    "(check the API key, quota, and network). Nothing was changed; "
                    "run again once the API is reachable. Work already done is cached."
                )
            if skipped_score:
                log(f"  Warning: {skipped_score} photo(s) got no score and were skipped this run; they will be retried next run.")
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
        _save_cache(cache_path, cache, log)

        if not scored_photos:
            raise ValueError("No photos could be scored. Nothing was changed.")

        # Sort by quality score descending
        scored_photos.sort(key=lambda x: x["score"], reverse=True)

        # Attach capture date & timestamp (for chronological sorting) BEFORE the
        # manifest is written: the Proceed/narrate stage reloads its photos from
        # manifest.json, so anything attached afterwards is lost.
        uploads_dir = os.path.join(session_storage.local_base, "uploads")
        _attach_capture_times(scored_photos, uploads_dir)

        # Save raw Curator manifest
        manifest_path = os.path.join(artefacts_dir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(scored_photos, f, indent=2)

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
                
            trajectory["ended_at"] = datetime.datetime.utcnow().isoformat()
            trajectory["metadata"] = {
                "total_processed": len(photos),
                "approved": len(approved),
                "quarantined": len(quarantined),
                "unique": len(unique_photos),
                "uncached_moderated": len(uncached_photos),
                "uncached_scored": len(uncached_unique),
                "embedding_calls": len(uncached_approved),
            }
            _write_trajectory(artefacts_dir, session_id, "curate", trajectory,
                              estimate_usage(len(uncached_photos), len(uncached_unique), 0))

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
        "name": "Memory Agent (Per-Event Contributor Profile Indexing)",
        "duration_seconds": round(time.time() - p4_start, 2),
        "inputs": len(selected_photos),
        "outputs": {
            "profiles_updated": len(contributors_activity)
        }
    })
    
    # Run Narrator Journaling in a Single Batched API Call
    log("Starting Phase 5: Batched Narrator Agent synthesis (single API call)...")
    p5_start = time.time()
    
    if progress_callback:
        progress_callback(0, 100, "journaling")
        
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
        
    # Strict variant: if the API fails we abort here, before any artifact is
    # written, instead of publishing generic filler as the journal.
    try:
        batched_entries_dict = generate_all_moments_journal_strict(batched_moments_data)
    except Exception as e:
        raise PipelineAPIError(
            f"Journal narration failed ({e}). The event's existing journal and story were not changed; "
            "run again once the API is reachable."
        ) from e
    if not batched_entries_dict:
        raise PipelineAPIError(
            "Journal narration returned no usable entries. The event's existing journal and story were not changed; run again."
        )

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
    try:
        full_story = generate_trip_story_strict(
            journal_entries_json=json.dumps(journal_entries),
            destination=memory_store.get_trip_context().get("destination") or "our trip"
        )
    except Exception as e:
        raise PipelineAPIError(
            f"Trip story generation failed ({e}). The event's existing journal and story were not changed; "
            "run again once the API is reachable."
        ) from e
    
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
        "uncached_scored": len(uncached_unique),
        "embedding_calls": len(uncached_approved),
    }

    _write_trajectory(artefacts_dir, session_id, "narrate" if stage == "narrate" else "all", trajectory,
                      estimate_usage(len(uncached_photos), len(uncached_unique), len(moments)))

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
