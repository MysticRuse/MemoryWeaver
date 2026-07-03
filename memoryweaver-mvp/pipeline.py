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

def get_match_context(session_id, metadata=None):
    if metadata:
        team1 = metadata.get('team1_name', '')
        team2 = metadata.get('team2_name', '')
        stadium = metadata.get('stadium_name', '')
        city = metadata.get('city', '')
        return f"FIFA World Cup 2026, {team1} vs {team2}, {stadium}, {city}"
        
    if session_id in ("FIFA26", "FIFA26_PARAGUAY_AUSTRALIA"):
        return "FIFA World Cup 2026 Group D, Levi's Stadium, San Francisco, June 25 2026"
    match_name = session_id.replace("_", " ").title()
    return f"FIFA World Cup 2026, {match_name} match"


GEMINI_MAX_PX = 1024  # Gemini needs ~1MP to recognize tiny players on the pitch from fan photos


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
                # Always force JSON mode when a schema is provided so the model
                # generates schema-conformant JSON instead of free-form markdown
                effective_mime = response_mime_type or ("application/json" if response_schema else None)
                config = types.GenerateContentConfig(
                    response_mime_type=effective_mime,
                    response_schema=response_schema,
                    http_options=types.HttpOptions(timeout=600000) # 10 minute timeout
                )
            else:
                config = types.GenerateContentConfig(
                    http_options=types.HttpOptions(timeout=600000)
                )
            resp = client.models.generate_content(
                model=MODEL, 
                contents=contents, 
                config=config
            )
            
            # Extract token details from response metadata
            prompt_tokens = resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0
            candidates_tokens = resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0
            
            # Gemini 2.5 Flash June 2026 pricing
            input_cost = prompt_tokens * (0.075 / 1000000)
            output_cost = candidates_tokens * (0.30 / 1000000)
            cost_usd = input_cost + output_cost
            
            # If the SDK already parsed the response into a Pydantic object, serialise
            # it back to JSON so callers always receive a JSON string.
            parsed = getattr(resp, 'parsed', None)
            if parsed is not None:
                if hasattr(parsed, 'model_dump'):
                    text_content = json.dumps(parsed.model_dump())
                else:
                    text_content = json.dumps(dict(parsed))
            else:
                # Fall back to resp.text and strip any markdown fences
                text_content = (resp.text or '').strip()
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
                'timed out' in msg.lower() or
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


def classify_by_metadata_only(upload_dir, filename, kickoff_ts=1782439200.0, fulltime_ts=1782446400.0):
    import os, re
    from PIL import Image
    from PIL.ExifTags import TAGS
    from datetime import datetime
    
    filepath = os.path.join(upload_dir, filename)
    KICKOFF_TS = float(kickoff_ts)
    FULLTIME_TS = float(fulltime_ts)
    
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
    
    # We completely trust the AI's visual classification (ai_moment) over metadata.
    # The AI can physically see if it's tailgating (pre-match) or active gameplay (in-match).
    # Metadata timestamps are often corrupted by timezone issues or bulk upload timings.
    return ai_moment


def get_current_score(session_id, latest_ts=None):
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
            now_ts = latest_ts if latest_ts else time.time()
    else:
        now_ts = latest_ts if latest_ts else time.time()
        
    if session_id not in ('FIFA26', 'FIFA26_PARAGUAY_AUSTRALIA'):
        return "Unknown", "The live score for this match is not strictly tracked in this prototype. Focus on the fan atmosphere and energy in the photos rather than hallucinating specific goals or scorers."
        
    if now_ts < 1782439200:
        return "0-0", "No goals have been scored yet by either team. Both teams are at 0-0."
    elif now_ts < 1782440640:
        return "0-0", "No goals have been scored yet by either team. Both teams are at 0-0."
    elif now_ts < 1782444120:
        return "0-1", "Australia is leading 1-0. Craig Goodwin scored for Australia at 24'. Paraguay has not scored any goals yet."
    else:
        return "1-1", "The match ended or is tied at 1-1. Craig Goodwin scored for Australia at 24', and Miguel Almirón scored the equalizer for Paraguay at 67'."


def score_and_group_photos(session_id, log=print, limit=10, consider_limit=100, include_videos=True, metadata=None):
    """
    Score and moderate all media (photos and videos) in a single batched API call.
    """
    upload_dir = f'uploads/{session_id}'
    valid_exts = ('.jpg', '.jpeg', '.png', '.heic', '.mov', '.mp4') if include_videos else ('.jpg', '.jpeg', '.png', '.heic')
    files = [f for f in os.listdir(upload_dir)
             if f.lower().endswith(valid_exts)]

    # Sort files by modification time, most recent first, and limit to consider_limit
    files_with_time = []
    for f in files:
        path = os.path.join(upload_dir, f)
        try:
            files_with_time.append((f, os.path.getmtime(path)))
        except Exception:
            files_with_time.append((f, 0))
    files_with_time.sort(key=lambda x: x[1], reverse=True)
    files = [x[0] for x in files_with_time[:consider_limit]]

    # Programmatic deduplication based on MD5 content hashes and original filenames
    import re
    import imagehash
    from PIL import Image

    # 1. Deduplicate by base filename (preferring photos over videos for Live Photos)
    base_groups = {}
    for f in files:
        base_match = re.search(r'^(.*?)_\d{10}_[0-9a-f]{6}(\.[a-zA-Z0-9]+)$', f)
        base_name = base_match.group(1).lower() if base_match else os.path.splitext(f)[0].lower()
        if base_name not in base_groups:
            base_groups[base_name] = []
        base_groups[base_name].append(f)

    selected_by_base = []
    for base_name, group in base_groups.items():
        photos = [f for f in group if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))]
        if photos:
            selected_by_base.append(photos[0]) # files was sorted by mtime, so [0] is newest
        else:
            selected_by_base.append(group[0])
            
    # Restore mtime ordering for the selected files (oldest to newest for clustering)
    selected_by_base.sort(key=lambda f: os.path.getmtime(os.path.join(upload_dir, f)))

    # 2. Perceptual Hashing (Burst Filter) with strict Hamming distance
    import imagehash
    from PIL import Image
    deduped_files = []
    seen_hashes = []
    
    for f in selected_by_base:
        path = os.path.join(upload_dir, f)
        try:
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic')):
                img = Image.open(path)
                phash = imagehash.phash(img)
                
                # Check Hamming distance against all seen hashes. 
                # A distance of <= 4 means it's an exact burst duplicate.
                is_duplicate = False
                for seen in seen_hashes:
                    if not isinstance(seen, str): # ensure it's an imagehash, not an MD5 string
                        if abs(phash - seen) <= 4:
                            is_duplicate = True
                            break
                        
                if is_duplicate:
                    log(f"Perceptual Deduplication: Skipped visually identical burst photo: {f}")
                    continue
                    
                seen_hashes.append(phash)
            else:
                # Fallback to MD5 for videos
                with open(path, 'rb') as fh:
                    f_hash = hashlib.md5(fh.read()).hexdigest()
                # Store as string so we don't try to subtract from imagehash
                if f_hash in seen_hashes:
                    continue
                seen_hashes.append(f_hash)

            deduped_files.append(f)
        except Exception as e:
            log(f"Deduplication check error for {f}: {e}")
            deduped_files.append(f)

    # Sort deduplicated files back to newest-first
    deduped_files.sort(key=lambda f: os.path.getmtime(os.path.join(upload_dir, f)), reverse=True)

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

        # Find the latest timestamp across all valid files to determine the current score state
        max_ts = 0
        for f in valid_files:
            _, ts = classify_by_metadata_only(upload_dir, f)
            if ts > max_ts:
                max_ts = ts
                
        score_str, score_details = get_current_score(session_id, latest_ts=max_ts if max_ts > 0 else None)
        parts.append(
            f'These are {len(valid_files)} fan photos and videos from {get_match_context(session_id, metadata)}. '
            'This is a fan memory app — media files (photos/videos) taken by fans attending the match are the heart of this experience. '
            f'CRITICAL GUARDRAIL: The current live score of the match is {score_str}. Details: {score_details} '
            'You MUST NOT describe or mention any goals, goal celebrations, or scorers in your captions or moments '
            'that are inconsistent with these details. (For example, if no goals have been scored, '
            'do not mention any goals, scoring attempts that succeeded, or goal celebrations).\n'
            'Return a JSON object containing two fields:\n'
            '1. "detected_score": a string representing the match score (e.g., "0-0", "0-1", "1-1") ONLY IF explicitly detected from the photos (e.g., a scoreboard is visible). '
            'If no scoreboard media is present or no score is visible in the photos, you MUST set this strictly to "HIDDEN". Do NOT guess the score or use the live score.\n'
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
            '   - Identify the core group of people who attended the match together.\n'
            '   - CELEBRATE DIFFERENT GROUPINGS: You should give high scores (8-10) to distinct subsets of the group. For example, give a high score to ONE great photo of the entire 6-member group, ONE great photo of just the 4-member family, ONE photo of the father and son, and ONE photo of the father and daughter. These are all distinct, highly valuable personal moments!\n'
            '   - Ensure a BROADER VARIETY of the match experience. Try to find these different groupings across different moments: `pre-match`, `in-match`, AND `post-match`.\n'
            '   - MAXIMUM 1 PHOTO PER GROUPING: You are strictly forbidden from giving a score of 8 or higher to more than ONE photo of the exact same people. If you find two great photos of the 4-member family, you MUST pick the BEST one (score 8-10) and deliberately score the second one poorly (score 1-4). No exceptions!\n'
            '   - PREFER LARGER GROUPS FOR SIMILAR PHOTOS: When evaluating multiple similar photos of your core group, you MUST prioritize and keep the photo with the HIGHEST number of people in the group versus the lowest (e.g. keep the 6-person photo and penalize the 4-person photo taken at the same spot).\n'
            '   - AVOID VISUAL REDUNDANCY FOR ALL EVENTS: If there are multiple photos of the exact same event (e.g., two photos of teams entering the pitch, two photos of the same free kick), you MUST pick the single best one and deliberately score the duplicates poorly (score 1-4). Never give high scores to visually similar scenes.\n'
            '   - PRIORITIZE ACTUAL GAMEPLAY ACTION: You MUST give a score of 9 or 10 to ANY photo that shows the green pitch with players actively playing the match! Even if the players are tiny dots from the upper stands, if the green field and active match are visible, it gets a 9 or 10. No exceptions!\n'
            '   - PENALIZE BORING FILLER: Rate generic crowd photos without the core family, substitutions (like electronic substitution boards), scoreboards, or completely empty pitches very low (1-3). NEVER score a scoreboard or substitution board higher than a 3.\n'
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
    
    # Force distribution across the timeline
    moments_map = {'pre-match': [], 'in-match': [], 'post-match': []}
    for item in usable:
        m = item.get('moment', 'in-match')
        if m in moments_map:
            moments_map[m].append(item)
        else:
            moments_map['in-match'].append(item)
            
    final_selection = []
    while len(final_selection) < limit and any(moments_map.values()):
        for m in ['pre-match', 'in-match', 'post-match']:
            if moments_map[m] and len(final_selection) < limit:
                final_selection.append(moments_map[m].pop(0))
                
    # Sort the final selection chronologically using exact timestamps embedded in filenames
    import re
    def get_timestamp(item):
        m = re.search(r'_(\d{10})_', item.get('filename', ''))
        if m:
            return int(m.group(1))
        # Fallback to moment order if no timestamp in filename
        moment_order = {'pre-match': 0, 'in-match': 1, 'post-match': 2}
        return 9999999999 + moment_order.get(item.get('moment', 'in-match'), 1)
        
    final_selection.sort(key=get_timestamp)

    os.makedirs(f'outputs/{session_id}', exist_ok=True)
    cache = {
        '_fingerprint': _uploads_fingerprint(upload_dir), 
        'detected_score': detected_score,
        'limit': limit,
        'photos': final_selection
    }
    with open(f'outputs/{session_id}/highlights.json', 'w') as fh:
        json.dump(cache, fh, indent=2)
    return final_selection


def generate_artefacts(session_id, scored, detected_score='0-0', log=print, metadata=None):
    """
    Generate match story + fan stats in a single API call.

    PRODUCTION TODO (Capstone): When multiple contributors are supported,
    pass contributor names alongside captions so the story can weave in
    personal moments ("as captured by @mitil..."). Also consider generating
    a shareable image card (using Gemini image generation) as an additional
    artefact for social sharing.
    """
    captions = '\n'.join([f"- {p['moment']}: {p['caption']}" for p in scored])
    moments = [p['moment'] for p in scored]
    # Flatten all identified players across photos, deduplicated
    all_players = list(dict.fromkeys(
        p for photo in scored for p in photo.get('players_visible', []) if p
    ))

    score_str, score_details = get_current_score(session_id)
    final_score = detected_score
    if score_str == '0-0':
        final_score = '0-0'

    combined_prompt = f'''You are a passionate football fan who attended {get_match_context(session_id, metadata)} with a group of friends.

Using ONLY these photo captions and player data, write:
1. A vivid 2-paragraph match story in first-person plural (we/our), excited and warm tone.
   Weave together both the on-pitch action AND the fan experience — the group of friends watching together,
   their reactions, the atmosphere in the stands, and personal moments captured in photos.
   Use real team and player names — never describe jersey colours.
2. A fan stats card based strictly on what was captured in photos, containing:
   - "top_moment": a brief sentence describing the single most exciting match action or stadium vibe moment. Do NOT focus on specific people or family members attending.
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
    log(f'CALLING: generate_story_and_stats(detected_score={final_score!r})')
    raw = _generate_with_retry(
        combined_prompt, 
        name="Story & Stats Generation", 
        response_schema=StoryAndStatsResponse
    )
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


def run_pipeline(session_id='FIFA26', log=print, limit=10, consider_limit=100, include_videos=True, metadata=None):
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
        scored = score_and_group_photos(session_id, log=log, limit=limit, consider_limit=consider_limit, include_videos=include_videos, metadata=metadata)

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
    generate_artefacts(session_id, scored, detected_score=detected_score, log=log, metadata=metadata)
    
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
