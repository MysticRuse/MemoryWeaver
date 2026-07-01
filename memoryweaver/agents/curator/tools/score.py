import json
import os
import re
from google import genai
from google.genai import types
from PIL import Image

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)

def score_photo_as_judge(photo_path: str, others_summary: str = "") -> dict:
    """
    Evaluates an uploaded photo on four dimensions (0-10) using Gemini 2.5 Flash
    as an LLM-as-judge.
    """
    client = get_gemini_client()
    
    # 1. Resize and normalise photo to reduce token usage
    img = Image.open(photo_path).convert("RGB")
    img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")

    # ==========================================
    # TODO (USER): Customize the scoring rubric below to prioritize
    # specific types of trip memories (e.g. high score for candid group photos,
    # and lower score for landscape-only photos).
    # ==========================================
    judge_prompt = (
        "You are an expert travel photographer and curator judging entries for a family trip journal.\n"
        "Analyze this image and score it from 0 to 10 on the following four dimensions:\n\n"
        "1. Sharpness: Focus quality and lack of blur. (0 is completely blurry, 10 is perfectly crisp).\n"
        "2. Composition: Rule of thirds, lighting, framing, and visual balance. (0 is accidental/dark, 10 is professional quality).\n"
        "3. Uniqueness vs. Others: Assess how unique this image is compared to typical travel dumps. "
        f"Context of other photos: {others_summary}\n"
        "4. Human Presence: Genuine emotion, candid actions, and family members visible (0 is no people/static scene, 10 is beautiful candid group shot).\n\n"
        "Also generate:\n"
        "- A scene label identifying the setting (e.g., 'hotel_room', 'beach', 'hiking_trail', 'restaurant').\n"
        "- A warm, descriptive caption (1-2 sentences) capturing the emotion and setting from a family member perspective.\n\n"
        "Respond ONLY with a raw JSON object containing these exact keys:\n"
        "{\n"
        '  "sharpness": 0-10,\n'
        '  "composition": 0-10,\n'
        '  "uniqueness": 0-10,\n'
        '  "human_presence": 0-10,\n'
        '  "scene_label": "string",\n'
        '  "caption": "string"\n'
        "}"
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[img_part, judge_prompt]
        )
        
        raw_text = response.text.strip()
        clean_json = re.sub(r"^```json\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
        result = json.loads(clean_json)
        
        # Calculate composite score (weighted average: human presence has high weight for family trip)
        w_sharpness = float(result.get("sharpness", 5)) * 0.15
        w_composition = float(result.get("composition", 5)) * 0.20
        w_uniqueness = float(result.get("uniqueness", 5)) * 0.25
        w_human = float(result.get("human_presence", 5)) * 0.40
        composite_score = round(w_sharpness + w_composition + w_uniqueness + w_human, 2)
        
        return {
            "score": composite_score,
            "sharpness": float(result.get("sharpness", 5)),
            "composition": float(result.get("composition", 5)),
            "uniqueness": float(result.get("uniqueness", 5)),
            "human_presence": float(result.get("human_presence", 5)),
            "scene_label": str(result.get("scene_label", "unknown")).lower().strip(),
            "caption": str(result.get("caption", ""))
        }
    except Exception as e:
        print(f"Error during image scoring: {e}")
        return {
            "score": 5.0,
            "sharpness": 5.0,
            "composition": 5.0,
            "uniqueness": 5.0,
            "human_presence": 5.0,
            "scene_label": "unknown",
            "caption": "A trip moment."
        }
