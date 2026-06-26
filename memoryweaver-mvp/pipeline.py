import os, json, time, re, io
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from pydantic import BaseModel, Field
from typing import List, Literal, Optional

register_heif_opener()
load_dotenv()

import hashlib
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"

# Define schemas for Gemini structured output
class PhotoItem(BaseModel):
    filename: Optional[str] = Field(None, description="The exact filename of the media file")
    usable: bool
    moderation_reason: str
    score: int = Field(..., ge=0, le=10, description="A score from 0 to 10 based on value as a fan memory")
    moment: Literal['pre-match', 'in-match', 'post-match']
    caption: str
    energy: Literal['high', 'medium', 'low']
    players_visible: List[str]

class ScoreAndGroupResponse(BaseModel):
    detected_score: str
    photos: List[PhotoItem]

class SecurityCheckSchema(BaseModel):
    privacy: Literal['PROTECTED', 'ISSUE FOUND'] = Field(description="PROTECTED if no spectator names or addresses are exposed in captions, otherwise ISSUE FOUND")
    consent: Literal['CONFIRMED', 'PENDING', 'N/A'] = Field(description="CONFIRMED if user consent is given, PENDING if awaiting user confirmation, or N/A")
    ready_to_post: Literal['YES', 'NEEDS REVIEW'] = Field(description="YES if ready to post publicly, otherwise NEEDS REVIEW")

class StatsDetails(BaseModel):
    top_moment: str
    energy: Literal['⭐', '⭐⭐', '⭐⭐⭐', '⭐⭐⭐⭐', '⭐⭐⭐⭐⭐']
    atmosphere: str
    players_spotted: List[str]
    moments_breakdown: List[str]

class StoryAndStatsResponse(BaseModel):
    story: str
    stats: StatsDetails
    security_check: SecurityCheckSchema



def _uploads_fingerprint(upload_dir):
    """MD5 of sorted (filename, filesize) pairs — fast, no file reads."""
    entries = sorted(
        (f, os.path.getsize(os.path.join(upload_dir, f)))
        for f in os.listdir(upload_dir)
        if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic', '.mov', '.mp4'))
    )
    return hashlib.md5(json.dumps(entries).encode()).hexdigest()

# PRODUCTION TODO: Move match context (teams, venue, date) to a config file or
# database so MemoryWeaver can support any match without code changes.
MATCH_CONTEXT = "FIFA World Cup 2026 Group D, Levi's Stadium, San Francisco, June 25 2026"


GEMINI_MAX_PX = 1024  # Gemini needs ~1MP to understand content; 4K adds tokens with no benefit


def _image_to_part(image_path):
    """Convert image file to a Gemini Part, resized to max 1024px and normalised to JPEG."""
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((GEMINI_MAX_PX, GEMINI_MAX_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


api_calls_log = []

def _generate_with_retry(contents, name="API Call", max_retries=5, response_mime_type=None, response_schema=None):
    for attempt in range(max_retries):
        try:
            config = None
            if response_mime_type or response_schema:
                config = types.GenerateContentConfig(
                    response_mime_type=response_mime_type,
                    response_schema=response_schema
                )
            resp = client.models.generate_content(model=MODEL, contents=contents, config=config)
            
            # Extract token details from response metadata
            prompt_tokens = resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0
            candidates_tokens = resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0
            
            # Gemini 2.5 Flash June 2026 pricing
            input_cost = prompt_tokens * (0.075 / 1000000)
            output_cost = candidates_tokens * (0.30 / 1000000)
            cost_usd = input_cost + output_cost
            
            # Clean up markdown formatting and validate JSON
            text_content = resp.text.strip()
            if response_mime_type == "application/json" or response_schema is not None:
                if text_content.startswith('```json'):
                    text_content = text_content[7:]
                if text_content.endswith('```'):
                    text_content = text_content[:-3]
                text_content = text_content.strip()
                # Test parse it, throws JSONDecodeError if invalid, triggering retry
                json.loads(text_content)

            api_calls_log.append({
                "name": name,
                "model": MODEL,
                "prompt_tokens": prompt_tokens,
                "candidates_tokens": candidates_tokens,
                "cost_usd": cost_usd
            })
            
            return text_content
        except Exception as e:
            msg = str(e)
            is_retryable = (
                '429' in msg or 
                'RESOURCE_EXHAUSTED' in msg or 
                '503' in msg or 
                'UNAVAILABLE' in msg or 
                isinstance(e, json.JSONDecodeError)
            )
            if is_retryable:
                m = re.search(r'seconds["\s:]+(\d+)', msg)
                wait = int(m.group(1)) + 2 if m else 5 * (attempt + 1)
                print(f'Retryable error ({type(e).__name__}: {msg}) — waiting {wait}s before retry {attempt+1}/{max_retries}')
                time.sleep(wait)
            else:
                raise
    raise RuntimeError('Max retries exceeded on Gemini API')


# COMMENTED OUT — original per-photo moderation (1 API call per photo).
# Replaced by inline moderation in score_and_group_photos (Option B) to keep
# total pipeline cost at 2 API calls.
# PRODUCTION TODO (Capstone): Revisit as a separate async pre-filter once the
# app has paid API access and higher throughput — per-photo moderation gives
# better explainability and lets you surface rejection reasons to uploaders.
#
# def moderate_photo(image_path):
#     """Return {usable: bool, reason: str}"""
#     try:
#         part = _image_to_part(image_path)
#         raw = _generate_with_retry([part,
#             "Is this photo: (1) appropriate to share, (2) not blurry/black, "
#             "(3) a real photo not a screenshot? JSON only: {\"usable\": bool, \"reason\": string}"])
#         return json.loads(raw.strip().replace("```json","").replace("```",""))
#     except Exception as e:
#         return {"usable": False, "reason": str(e)}


def classify_by_metadata_only(upload_dir, filename):
    import os, re
    from PIL import Image
    from PIL.ExifTags import TAGS
    from datetime import datetime
    
    filepath = os.path.join(upload_dir, filename)
    KICKOFF_TS = 1782439200.0
    FULLTIME_TS = 1782446400.0
    
    capture_ts = None
    ts_source = None
    
    # 1. Try to extract from EXIF
    try:
        img = Image.open(filepath)
        exif = img._getexif()
        if exif:
            for tag, value in exif.items():
                decoded = TAGS.get(tag, tag)
                if decoded in ['DateTimeOriginal', 'DateTimeDigitized', 'DateTime']:
                    if isinstance(value, str):
                        parts = value.strip().split()
                        if len(parts) >= 2:
                            date_str = parts[0].replace(':', '-')
                            time_str = parts[1]
                            dt_str = f"{date_str}T{time_str}-07:00"
                            try:
                                capture_ts = datetime.fromisoformat(dt_str).timestamp()
                                ts_source = "exif"
                                break
                            except ValueError:
                                try:
                                    capture_ts = datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S").timestamp()
                                    ts_source = "exif"
                                    break
                                except ValueError:
                                    pass
    except Exception:
        pass
        
    # 2. Try to extract from original filename (prefix date patterns)
    if not capture_ts:
        match = re.search(r'(\d{4})[-_]?(\d{2})[-_]?(\d{2})[-_](\d{2})[-_]?(\d{2})[-_]?(\d{2})', filename)
        if match:
            try:
                year, month, day, hour, minute, second = match.groups()
                dt_str = f"{year}-{month}-{day}T{hour}:{minute}:{second}-07:00"
                capture_ts = datetime.fromisoformat(dt_str).timestamp()
                ts_source = "filename_pattern"
            except Exception:
                pass
                
    # 3. Try to extract from upload epoch timestamp in filename (e.g. _1782440376_)
    if not capture_ts:
        match_epoch = re.search(r'_(\d{10})_', filename)
        if match_epoch:
            capture_ts = int(match_epoch.group(1))
            ts_source = "upload_epoch"
            
    # 4. Fallback to mtime
    if not capture_ts:
        try:
            capture_ts = os.path.getmtime(filepath)
            ts_source = "mtime"
        except Exception:
            pass
            
    if capture_ts:
        if capture_ts < KICKOFF_TS:
            return 'pre-match', capture_ts
        elif capture_ts > FULLTIME_TS:
            return 'post-match', capture_ts
        else:
            return 'in-match', capture_ts
            
    import time
    return 'in-match', time.time()


def classify_moment(upload_dir, filename, ai_moment):
    """
    Refine classification of the moment using both metadata (from classify_by_metadata_only)
    and AI content classification.
    """
    meta_moment, _ = classify_by_metadata_only(upload_dir, filename)
    
    # If the metadata says it was taken pre-match, it is pre-match
    if meta_moment == 'pre-match':
        return 'pre-match'
        
    # If the metadata says post-match, but the AI content is game action (e.g. stoppage time goals), keep as in-match
    if meta_moment == 'post-match':
        if ai_moment in ['in-match', 'goal', 'player', 'celebration']:
            return 'in-match'
        return 'post-match'
        
    # If metadata says in-match, but the AI content is pre-match (e.g. warmups right at kickoff) or post-match
    if meta_moment == 'in-match':
        if ai_moment in ['pre-match', 'post-match']:
            return ai_moment
        return 'in-match'
        
    return meta_moment


def get_current_score():
    import time, os
    override_score = os.getenv('OVERRIDE_SCORE')
    if override_score:
        if override_score == '0-0':
            return "0-0", "No goals have been scored yet by either team. Both teams are at 0-0."
        return override_score, f"The match score is {override_score}."
        
    override_ts = os.getenv('OVERRIDE_TIMESTAMP')
    if override_ts:
        try:
            now_ts = float(override_ts)
        except ValueError:
            now_ts = time.time()
    else:
        now_ts = time.time()
        
    if now_ts < 1782439200:
        return "0-0", "No goals have been scored yet by either team. Both teams are at 0-0."
    elif now_ts < 1782440640:
        return "0-0", "No goals have been scored yet by either team. Both teams are at 0-0."
    elif now_ts < 1782444120:
        return "0-1", "Australia is leading 1-0. Craig Goodwin scored for Australia at 24'. Paraguay has not scored any goals yet."
    else:
        return "1-1", "The match ended or is tied at 1-1. Craig Goodwin scored for Australia at 24', and Miguel Almirón scored the equalizer for Paraguay at 67'."


def score_and_group_photos(session_id, log=print, limit=10):
    """
    Score and moderate all media (photos and videos) in a single batched API call.
    """
    upload_dir = f'uploads/{session_id}'
    files = [f for f in os.listdir(upload_dir)
             if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic', '.mov', '.mp4'))]

    # Sort files by modification time, most recent first, and limit to 100
    files_with_time = []
    for f in files:
        path = os.path.join(upload_dir, f)
        try:
            files_with_time.append((f, os.path.getmtime(path)))
        except Exception:
            files_with_time.append((f, 0))
    files_with_time.sort(key=lambda x: x[1], reverse=True)
    files = [x[0] for x in files_with_time[:100]]

    # Programmatic deduplication based on MD5 content hashes
    seen_hashes = set()
    deduped_files = []
    for f in files:
        path = os.path.join(upload_dir, f)
        try:
            with open(path, 'rb') as fh:
                f_hash = hashlib.md5(fh.read()).hexdigest()
            if f_hash in seen_hashes:
                log(f"Programmatic Deduplication: Skipped duplicate file content: {f}")
                continue
            seen_hashes.add(f_hash)
            deduped_files.append(f)
        except Exception as e:
            log(f"Deduplication check error for {f}: {e}")
            deduped_files.append(f)

    parts = []
    valid_files = []
    uploaded_videos = []
    
    try:
        for f in deduped_files:
            path = os.path.join(upload_dir, f)
            ext = os.path.splitext(f)[1].lower()
            try:
                # Add text label prefix before the media to help Gemini map filenames accurately
                parts.append(f"[Media Index {len(valid_files)}: filename={f}]")
                if ext in ('.jpg', '.jpeg', '.png', '.heic'):
                    parts.append(_image_to_part(path))
                    valid_files.append(f)
                elif ext in ('.mov', '.mp4'):
                    log(f"Uploading video file to Gemini: {f}...")
                    uploaded_file = client.files.upload(file=path)
                    
                    # Wait for processing
                    while uploaded_file.state.name == "PROCESSING":
                        time.sleep(1.5)
                        uploaded_file = client.files.get(name=uploaded_file.name)
                        
                    if uploaded_file.state.name == "ACTIVE":
                        parts.append(uploaded_file)
                        valid_files.append(f)
                        uploaded_videos.append(uploaded_file)
                        log(f"Video {f} successfully processed and added to payload.")
                    else:
                        log(f"Video {f} processing failed with state {uploaded_file.state.name}: {uploaded_file.error}")
                        parts.pop()
            except Exception as e:
                log(f'Could not process {f}: {e}')
                if parts and parts[-1].startswith(f"[Media Index {len(valid_files)}:"):
                    parts.pop()

        if not valid_files:
            return []

        score_str, score_details = get_current_score()
        parts.append(
            f'These are {len(valid_files)} fan photos and videos from {MATCH_CONTEXT}. '
            'This is a fan memory app — media files (photos/videos) taken by fans attending the match are the heart of this experience. '
            f'CRITICAL GUARDRAIL: The current live score of the match is {score_str}. Details: {score_details} '
            'You MUST NOT describe or mention any goals, goal celebrations, or scorers in your captions or moments '
            'that are inconsistent with these details. (For example, if no goals have been scored, '
            'do not mention any goals, scoring attempts that succeeded, or goal celebrations).\n'
            'Return a JSON object containing two fields:\n'
            '1. "detected_score": a string representing the match score (e.g., "0-0", "0-1", "1-1") detected from the scoreboard. '
            f'If no scoreboard media is present or no goals are shown, default strictly to the current live score: "{score_str}".\n'
            '2. "photos": a JSON array containing one object per media file, with these fields:\n'
            '   {"filename": "the exact filename of this media file as matching the [Media Index X: filename=Y] marker", '
            '   "usable": true/false, "moderation_reason": "why rejected or ok", '
            '   "score": 0-10, "moment": "pre-match|in-match|post-match", '
            '   "caption": "one vivid sentence from the fan perspective", "energy": "high|medium|low", '
            '   "players_visible": ["player name or empty list if none identified"]}\n'
            'For each media file, perform the following tasks:\n'
            '1. MODERATE: decide if the media is usable — it must be (a) appropriate to share, '
            '(b) not blurry or black, (c) a real match or fan attendance photo/video, not a screenshot or unrelated image.\n'
            '2. SCORE (PERSONALIZATION & VARIETY): rate the photo/video 0-10 based on its value as a personal fan memory. '
            'CRITICAL PERSONALIZATION INSTRUCTIONS:\n'
            '   - Identify if there is a core group of people who attended the match together (e.g. same recurring people like the father and daughter, or specific friends appearing across multiple photos).\n'
            '   - Assign higher scores (8-10) to personal, candid, or group photos and videos featuring these same recurring people (e.g., selfies in the stadium, family smiles). These represent key personal important moments of the match-day experience.\n'
            '   - Rate generic stadium landscape, scoreboard, or gameplay shots that do not show the core attendees lower (typically 4-7), unless they are outstanding/unique.\n'
            '   - AVOID DUPLICATES AND NEAR-DUPLICATES: If there are multiple similar photos of the same scene (e.g., multiple scoreboard photos showing 0-0, or multiple near-identical selfies), select only the single best one to have a high score, and score the others very low (1-3) or mark them as usable=false with moderation_reason="Duplicate/near-duplicate". We want a diverse journal with no repetitive highlights.\n'
            'For the caption, write one vivid sentence from the fan perspective: '
            'for fan photos/videos describe who is in the moment and what they are feeling; '
            'for player/action media use real team and player names.\n'
            'For "moment", choose strictly based on visual content:\n'
            '- "pre-match": tailgating, stadium gates, empty stadium/pitch before kickoff, team warmups, national anthems.\n'
            '- "in-match": active gameplay, players on pitch, goals, in-game fan celebrations or referee decisions.\n'
            '- "post-match": final scoreboard, final whistle celebrations, trophy ceremonies, empty stadium at night, fans leaving.'
        )

        log(f'Moderating and scoring {len(valid_files)} media files in 1 API call...')
        log('[Activating: photo-tagger skill]')
        log(f'CALLING: score_and_group_photos(files_count={len(valid_files)})')
        raw = _generate_with_retry(
            parts, 
            name="Moderation and Scoring", 
            response_schema=ScoreAndGroupResponse
        )
        log(f'RESULT: score_and_group_photos response received (len={len(raw)})')
        
    finally:
        # Cleanup uploaded files from Gemini API to prevent cluttering storage
        for uv in uploaded_videos:
            try:
                client.files.delete(name=uv.name)
                log(f"Cleaned up video from Gemini storage: {uv.name}")
            except Exception as e:
                log(f"Error cleaning up video {uv.name}: {e}")

    res_data = json.loads(raw)
    detected_score = res_data.get('detected_score', score_str)
    all_results = res_data.get('photos', [])

    # Map results by filename or backup by index
    usable = []
    results_by_filename = {item.get('filename'): item for item in all_results if item.get('filename')}
    
    for idx, f in enumerate(valid_files):
        item = None
        if f in results_by_filename:
            item = results_by_filename[f]
        elif idx < len(all_results):
            item = all_results[idx]
            item['filename'] = f
            
        if item:
            if item.get('usable', True):
                ai_moment = item.get('moment', 'fan')
                item['moment'] = classify_moment(upload_dir, item['filename'], ai_moment)
                usable.append(item)
            else:
                log(f'Moderated out: {item["filename"]} — {item.get("moderation_reason", "no reason")}')

    log(f'{len(usable)}/{len(valid_files)} media files passed moderation')

    usable.sort(key=lambda x: x['score'], reverse=True)
    os.makedirs(f'outputs/{session_id}', exist_ok=True)
    cache = {
        '_fingerprint': _uploads_fingerprint(upload_dir), 
        'detected_score': detected_score,
        'limit': limit,
        'photos': usable[:limit]
    }
    with open(f'outputs/{session_id}/highlights.json', 'w') as fh:
        json.dump(cache, fh, indent=2)
    return usable[:limit]


def generate_artefacts(session_id, scored, detected_score='0-0', log=print):
    """
    Generate match story + fan stats in a single API call.

    PRODUCTION TODO (Capstone): When multiple contributors are supported,
    pass contributor names alongside captions so the story can weave in
    personal moments ("as captured by @mitil..."). Also consider generating
    a shareable image card (using Gemini image generation) as an additional
    artefact for social sharing.
    """
    top5 = scored[:5]
    captions = '\n'.join([f"- {p['moment']}: {p['caption']}" for p in top5])
    moments = [p['moment'] for p in scored]
    # Flatten all identified players across photos, deduplicated
    all_players = list(dict.fromkeys(
        p for photo in scored for p in photo.get('players_visible', []) if p
    ))

    score_str, score_details = get_current_score()
    final_score = detected_score
    if score_str == '0-0':
        final_score = '0-0'

    combined_prompt = f'''You are a passionate football fan who attended {MATCH_CONTEXT} with a group of friends.

Using ONLY these photo captions and player data, write:
1. A vivid 2-paragraph match story in first-person plural (we/our), excited and warm tone.
   Weave together both the on-pitch action AND the fan experience — the group of friends watching together,
   their reactions, the atmosphere in the stands, and personal moments captured in photos.
   Use real team and player names — never describe jersey colours.
2. A fan stats card based strictly on what was captured in photos, containing:
   - "top_moment": a brief sentence describing the single most memorable personal or family moment.
   - "energy": one of '⭐', '⭐⭐', '⭐⭐⭐', '⭐⭐⭐⭐', '⭐⭐⭐⭐⭐' representing the overall excitement level.
   - "atmosphere": a short description of the crowd atmosphere (e.g., 'Electric and family-friendly').
   - "players_spotted": a list of player names identified in the media.
   - moments_breakdown: a list of short descriptions of milestones captured chronologically (e.g. 'Arrival at stadium', 'In-match action').
3. A security check compliance review:
   - "privacy": Evaluate if any real spectator names (e.g. "@mitil", "John Doe") or addresses are exposed in captions. If not, set to "PROTECTED". Otherwise "ISSUE FOUND".
   - "consent": Set to "PENDING" (default for live reviews).
   - "ready_to_post": Set to "NEEDS REVIEW" (since consent is pending).

Photo captions: {captions}
Moment types across all photos: {moments}
Players spotted in photos: {all_players if all_players else "none identified"}

CRITICAL GUARDRAIL: The actual match score is {final_score}. 
You MUST NOT mention any other score, and you MUST NOT describe any goals or scorers unless they are explicitly mentioned in the photo captions AND the match score is not "0-0". 
If the match score is "0-0", you MUST describe the match as a scoreless draw or a scoreless game in progress (0-0), and you MUST NOT describe any goals or scorers (do not mention Craig Goodwin scoring or Miguel Almirón scoring).

Return JSON only conforming to the schema.'''

    log('[Activating: story-weaver skill]')
    log(f"CALLING: generate_story_and_stats(detected_score='{final_score}')")
    raw = _generate_with_retry(
        combined_prompt, 
        name="Story & Stats Generation", 
        response_schema=StoryAndStatsResponse
    )
    log(f'RESULT: generate_story_and_stats response received (len={len(raw)})')
    data = json.loads(raw)

    story = str(data.get('story') or data.get('match_story', ''))
    stats = data.get('stats') or data.get('fan_stats_card', {})
    if isinstance(stats, str):
        stats = {}
        
    # Append the security check compliance dictionary to stats
    security_check = data.get('security_check') or data.get('security_check_compliance_review', {
        "privacy": "PROTECTED",
        "consent": "PENDING",
        "ready_to_post": "NEEDS REVIEW"
    })
    stats['security_check'] = security_check

    with open(f'outputs/{session_id}/story.txt', 'w') as fh: fh.write(story)
    with open(f'outputs/{session_id}/stats.json', 'w') as fh: json.dump(stats, fh, indent=2)
    return story, stats


def run_pipeline(session_id='FIFA26', log=print, limit=10):
    global api_calls_log
    api_calls_log = []
    
    log('Starting pipeline...')
    highlights_path = f'outputs/{session_id}/highlights.json'

    upload_dir = f'uploads/{session_id}'
    current_fp = _uploads_fingerprint(upload_dir)
    scored = None

    if os.path.exists(highlights_path):
        with open(highlights_path) as f:
            cache = json.load(f)
        cached_fp = cache.get('_fingerprint')
        cached_limit = cache.get('limit', 10)
        if cached_fp == current_fp and cached_limit == limit:
            log('Uploads unchanged — using cached photo scores')
            scored = cache.get('photos', [])
            
            # If scores are loaded from cache, construct a mock entry for logging
            # to let the UI know no API calls were spent on scoring
            api_calls_log.append({
                "name": "Moderation and Scoring (Loaded from Cache)",
                "model": MODEL,
                "prompt_tokens": 0,
                "candidates_tokens": 0,
                "cost_usd": 0.0
            })
        else:
            log('Uploads or limit have changed — re-scoring photos')

    if scored is None:
        scored = score_and_group_photos(session_id, log=log, limit=limit)

    if not scored:
        raise ValueError('No usable photos after moderation — ask contributors to upload clearer match photos.')

    log(f'{len(scored)} photos passed moderation and scoring')
    detected_score = '0-0'
    if os.path.exists(highlights_path):
        try:
            with open(highlights_path) as f:
                cache = json.load(f)
                detected_score = cache.get('detected_score', '0-0')
        except Exception:
            pass
    generate_artefacts(session_id, scored, detected_score=detected_score, log=log)
    
    # Save the accumulated cost data
    total_prompt = sum(x["prompt_tokens"] for x in api_calls_log)
    total_candidates = sum(x["candidates_tokens"] for x in api_calls_log)
    total_cost = sum(x["cost_usd"] for x in api_calls_log)
    
    cost_data = {
        "api_calls": api_calls_log,
        "total_prompt_tokens": total_prompt,
        "total_candidates_tokens": total_candidates,
        "total_cost_usd": total_cost
    }
    
    with open(f'outputs/{session_id}/cost.json', 'w') as fh:
        json.dump(cost_data, fh, indent=2)
        
    log('Done. Artefacts written to outputs/' + session_id)
    return True
