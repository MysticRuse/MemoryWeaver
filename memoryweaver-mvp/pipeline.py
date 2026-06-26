import os, json, time, re, io
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from pillow_heif import register_heif_opener
register_heif_opener()
load_dotenv()

import hashlib
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"


def _uploads_fingerprint(upload_dir):
    """MD5 of sorted (filename, filesize) pairs — fast, no file reads."""
    entries = sorted(
        (f, os.path.getsize(os.path.join(upload_dir, f)))
        for f in os.listdir(upload_dir)
        if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))
    )
    return hashlib.md5(json.dumps(entries).encode()).hexdigest()

# PRODUCTION TODO: Move match context (teams, venue, date) to a config file or
# database so MemoryWeaver can support any match without code changes.
MATCH_CONTEXT = "FIFA World Cup 2026 Group D, Levi's Stadium, San Francisco, June 25 2026"


GEMINI_MAX_PX = 1024  # Gemini needs ~1MP to understand content; 4K adds tokens with no benefit


def _image_to_part(image_path):
    """Convert image file to a Gemini Part, resized to max 1024px and normalised to JPEG."""
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((GEMINI_MAX_PX, GEMINI_MAX_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


api_calls_log = []

def _generate_with_retry(contents, name="API Call", max_retries=5):
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(model=MODEL, contents=contents)
            
            # Extract token details from response metadata
            prompt_tokens = resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0
            candidates_tokens = resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0
            
            # Gemini 2.5 Flash June 2026 pricing
            input_cost = prompt_tokens * (0.075 / 1000000)
            output_cost = candidates_tokens * (0.30 / 1000000)
            cost_usd = input_cost + output_cost
            
            api_calls_log.append({
                "name": name,
                "model": MODEL,
                "prompt_tokens": prompt_tokens,
                "candidates_tokens": candidates_tokens,
                "cost_usd": cost_usd
            })
            
            return resp.text
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'RESOURCE_EXHAUSTED' in msg or '503' in msg or 'UNAVAILABLE' in msg:
                m = re.search(r'seconds["\s:]+(\d+)', msg)
                wait = int(m.group(1)) + 2 if m else 20 * (attempt + 1)
                print(f'Rate limited — waiting {wait}s before retry {attempt+1}/{max_retries}')
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


def score_and_group_photos(session_id, log=print):
    """
    Score and moderate all photos in a single batched API call.

    Each photo gets: usability check (moderation), a relevance score, a
    caption using real team names reasoned from jersey colours, and an
    energy rating.

    PRODUCTION TODO (Capstone): Split moderation into a separate pre-filter
    pass so rejected photos can be flagged to the uploader before scoring.
    Also add user attribution (who uploaded each photo) so the story can
    credit contributors by name.
    """
    upload_dir = f'uploads/{session_id}'
    files = [f for f in os.listdir(upload_dir)
             if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))]

    parts = []
    valid_files = []
    for f in files:
        try:
            parts.append(_image_to_part(os.path.join(upload_dir, f)))
            valid_files.append(f)
        except Exception as e:
            log(f'Could not open {f}: {e}')

    if not valid_files:
        return []

    parts.append(
        f'These are {len(valid_files)} fan photos from {MATCH_CONTEXT}. '
        'This is a fan memory app — photos taken by fans attending the match are the heart of this experience. '
        'For each photo, do two things:\n'
        '1. MODERATE: decide if the photo is usable — it must be (a) appropriate to share, '
        '(b) not blurry or black, (c) a real match or fan attendance photo, not a screenshot or unrelated image.\n'
        '2. SCORE: rate the photo 0-10 based on its value as a fan memory. '
        'Fan experience photos (selfies at the stadium, groups of friends cheering, fans in team colours, '
        'fan reactions to goals, fan gear and scarves) are equally valuable as player or action shots — '
        'score them generously if they capture genuine match-day emotion or atmosphere. '
        'Where players are visible, identify them using jersey colours, numbers, and your knowledge of '
        'FIFA World Cup 2026 squads — use real country and player names, never describe colours.\n'
        'For the caption, write one vivid sentence from the fan perspective: '
        'for fan photos describe who is in the moment and what they are feeling; '
        'for player/action photos use real team and player names.\n'
        'Return a JSON array only, no markdown, one object per photo in order:\n'
        '{"usable": true/false, "moderation_reason": "why rejected or ok", '
        '"score": 0-10, "moment": "pre-match|goal|celebration|crowd|player|stadium|fan|post-match", '
        '"caption": "one vivid sentence from the fan perspective", "energy": "high|medium|low", '
        '"players_visible": ["player name or empty list if none identified"]}'
        # PRODUCTION TODO (Capstone): pipe players_visible into a dedicated
        # player-stats aggregation step for richer per-player breakdowns.
    )

    log(f'Moderating and scoring {len(valid_files)} photos in 1 API call...')
    raw = _generate_with_retry(parts, name="Moderation and Scoring").strip().replace('```json', '').replace('```', '')
    all_results = json.loads(raw)

    # Attach filenames and filter out unusable photos
    usable = []
    for i, item in enumerate(all_results):
        item['filename'] = valid_files[i] if i < len(valid_files) else ''
        if item.get('usable', True):
            usable.append(item)
        else:
            log(f'Moderated out: {item["filename"]} — {item.get("moderation_reason", "no reason")}')

    log(f'{len(usable)}/{len(valid_files)} photos passed moderation')

    # PRODUCTION TODO (Capstone): Surface rejected photos back to uploaders via
    # a /moderation-report endpoint so they know which of their photos were excluded
    # and why, rather than silently dropping them.

    usable.sort(key=lambda x: x['score'], reverse=True)
    os.makedirs(f'outputs/{session_id}', exist_ok=True)
    cache = {'_fingerprint': _uploads_fingerprint(upload_dir), 'photos': usable[:10]}
    with open(f'outputs/{session_id}/highlights.json', 'w') as fh:
        json.dump(cache, fh, indent=2)
    return usable[:10]


def generate_artefacts(session_id, scored, log=print):
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

    combined_prompt = f'''You are a passionate football fan who attended {MATCH_CONTEXT} with a group of friends.

Using ONLY these photo captions and player data, write:
1. A vivid 2-paragraph match story in first-person plural (we/our), excited and warm tone.
   Weave together both the on-pitch action AND the fan experience — the group of friends watching together,
   their reactions, the atmosphere in the stands, and personal moments captured in photos.
   Use real team and player names — never describe jersey colours.
2. A fan stats card based strictly on what was captured in photos.

Photo captions: {captions}
Moment types across all photos: {moments}
Players spotted in photos: {all_players if all_players else "none identified"}

Return JSON only:
{{
  "story": "2 paragraph string",
  "stats": {{
    "top_moment": "one vivid sentence describing the most captured moment",
    "energy": "⭐⭐⭐⭐⭐ (1-5 stars matching crowd energy)",
    "atmosphere": "one sentence describing the overall match atmosphere",
    "players_spotted": ["up to 3 player names seen in photos, or empty list"],
    "moments_breakdown": ["3 descriptive moment labels mixing fan and match moments, e.g. Friends celebrating together, Socceroos goal eruption — not raw tags"]
  }}
}}'''

    log('Generating story + stats in 1 API call...')
    raw = _generate_with_retry(combined_prompt, name="Story & Stats Generation").strip().replace('```json', '').replace('```', '')
    data = json.loads(raw)

    story = str(data.get('story', ''))
    stats = data.get('stats', {})
    if isinstance(stats, str):
        stats = {}

    with open(f'outputs/{session_id}/story.txt', 'w') as fh: fh.write(story)
    with open(f'outputs/{session_id}/stats.json', 'w') as fh: json.dump(stats, fh, indent=2)
    return story, stats


def run_pipeline(session_id='FIFA26', log=print):
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
        if cached_fp == current_fp:
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
            log('Uploads have changed — re-scoring photos')

    if scored is None:
        scored = score_and_group_photos(session_id, log=log)

    if not scored:
        raise ValueError('No usable photos after moderation — ask contributors to upload clearer match photos.')

    log(f'{len(scored)} photos passed moderation and scoring')
    generate_artefacts(session_id, scored, log=log)
    
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
