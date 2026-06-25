import os, json, time, re
from dotenv import load_dotenv
import google.generativeai as genai
from PIL import Image
from pillow_heif import register_heif_opener
register_heif_opener()  # handles iPhone HEIC photos
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-2.5-flash")

def _generate_with_retry(parts, max_retries=5):
    for attempt in range(max_retries):
        try:
            return model.generate_content(parts)
        except Exception as e:
            msg = str(e)
            if '429' in msg:
                m = re.search(r'retry.*?(\d+)\s*s', msg, re.IGNORECASE)
                wait = int(m.group(1)) + 2 if m else 20 * (attempt + 1)
                print(f'Rate limited — waiting {wait}s before retry {attempt+1}/{max_retries}')
                time.sleep(wait)
            else:
                raise
    raise RuntimeError('Max retries exceeded on Gemini API')

def moderate_photo(image_path):
    """Return {usable: bool, reason: str}"""
    try:
        img = Image.open(image_path)
        resp = _generate_with_retry([img,
            "Is this photo: (1) appropriate to share, (2) not blurry/black, (3) a real photo not a screenshot? JSON only: {\"usable\": bool, \"reason\": string}"])
        raw = resp.text.strip().replace("```json","").replace("```","")
        return json.loads(raw)
    except Exception as e:
        return {"usable": False, "reason": str(e)}
    

def score_and_group_photos(session_id):
    upload_dir = f'uploads/{session_id}'
    files = [f for f in os.listdir(upload_dir) if f.lower().endswith(('.jpg','.jpeg','.png','.heic','.mp4'))]
    scored = []
    for f in files:
        path = os.path.join(upload_dir, f)
        # if not moderate_photo(path)['usable']:
        #     print(f'Skipped: {f}')
        #     continue
        try:
            img = Image.open(path)
            resp = _generate_with_retry([img,
                'Score this World Cup fan photo. JSON only: {"score":0-10,"moment":"pre-match|goal|celebration|crowd|player|stadium|post-match","caption":"one vivid sentence","energy":"high|medium|low"}'])
            raw = resp.text.strip().replace('```json','').replace('```','')
            data = json.loads(raw)
            data['filename'] = f
            scored.append(data)
        except Exception as e:
            print(f'Score error {f}: {e}')
    scored.sort(key=lambda x: x['score'], reverse=True)
    os.makedirs(f'outputs/{session_id}', exist_ok=True)
    with open(f'outputs/{session_id}/highlights.json','w') as fh:
        json.dump(scored[:10], fh, indent=2)
    return scored


def generate_artefacts(session_id, scored):
    top5 = scored[:5]
    captions = '\n'.join([f"- {p['moment']}: {p['caption']}" for p in top5])

    # Artefact 1: Match story
    story_prompt = f'''You are a passionate football fan who attended Paraguay vs Australia
at Levi's Stadium, San Francisco Bay Area, June 25 2026 (FIFA World Cup Group D).
Using ONLY these photo captions from fans at the match, write a vivid 2-paragraph
match story. Use first-person plural (we/our). Be specific to the captions.
Tone: excited, warm, present-tense as if reliving the moment.
Captions: {captions}'''
    story = _generate_with_retry(story_prompt).text
    with open(f'outputs/{session_id}/story.txt','w') as fh: fh.write(story)

    # Artefact 2: Fan stats
    moments = [p['moment'] for p in scored]
    energies = [p['energy'] for p in scored]
    stats_prompt = f'''Based on these photo moment types from a World Cup match: {moments}
Write a fun 3-line fan stats card. Format exactly:
Most photographed moment: [answer]
Crowd energy level: [1-5 stars]
Memorable moments captured: [comma list of top 3 moment types]'''
    stats = _generate_with_retry(stats_prompt).text
    with open(f'outputs/{session_id}/stats.txt','w') as fh: fh.write(stats)
    return story, stats

def run_pipeline(session_id='FIFA26'):
    print('Starting pipeline...')
    scored = score_and_group_photos(session_id)
    print(f'{len(scored)} photos scored')
    story, stats = generate_artefacts(session_id, scored)
    print('Artefacts written to outputs/'+ session_id)
    return True