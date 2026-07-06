import yaml
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

def score_photo_as_judge(path: str, filename: str, others_summary: str = "") -> dict:
    """
    Scores a single photo as a judge.
    
    Args:
        path: Path to the image file.
        filename: Filename of the image.
        others_summary: Summary of other photos for uniqueness context.
        
    Returns:
        A dict with keys score, sharpness, composition, uniqueness, human_presence, scene_label, caption.
    """
    results = score_photos_as_judge_batch([{"path": path, "filename": filename}], others_summary)
    return results[0] if results else {
        "score": 5.0,
        "sharpness": 5.0,
        "composition": 5.0,
        "uniqueness": 5.0,
        "human_presence": 5.0,
        "scene_label": "unknown",
        "caption": "Exploring the sights."
    }

def score_photos_as_judge_batch(photo_batch: list, others_summary: str = "") -> list:
    """
    Evaluates a batch of uploaded photos (up to 15) in a single API call,
    guiding landmark naming via EXIF GPS / Date.
    Uses YAML formatting for the prompt and response to optimize model parsing.
    """
    client = get_gemini_client()
    
    contents = []
    metadata_context_lines = []
    
    # 1. Compress images and compile metadata contexts
    from pipeline.local_cleaner import get_exif_metadata
    
    for idx, item in enumerate(photo_batch):
        filename = item["filename"]
        path = item["path"]
        
        meta = get_exif_metadata(path)
        meta_str = f"Image Index {idx} ({filename}): "
        if meta.get("gps"):
            meta_str += f"GPS: {meta['gps']} | "
        if meta.get("date"):
            meta_str += f"Date: {meta['date']}"
        metadata_context_lines.append(meta_str)
        
        try:
            img = Image.open(path).convert("RGB")
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")
            
            contents.append(f"\n--- IMAGE INDEX {idx} ---")
            contents.append(img_part)
        except Exception as ie:
            pass

    metadata_context_str = "\n".join(metadata_context_lines)
    
    prompt = (
        "You are an expert travel photographer and curator judging entries for a family trip journal.\n"
        "Use the visual details in each image combined with the provided metadata (GPS/Date) to accurately name the specific landmarks/locations.\n"
        "Leverage your world knowledge of landmarks corresponding to these coordinates.\n\n"
        f"Photos Metadata Context:\n{metadata_context_str}\n\n"
        "Analyze each image and score it from 0 to 10 on the following dimensions:\n"
        "1. Sharpness: Focus quality and lack of blur. (0 is completely blurry, 10 is perfectly crisp).\n"
        "2. Composition: Rule of thirds, lighting, framing, and visual balance. (0 is accidental/dark, 10 is professional quality).\n"
        "3. Uniqueness vs. Others: Assess how unique this image is compared to typical travel dumps. "
        f"Context of other photos: {others_summary}\n"
        "4. Human Presence: Genuine emotion, candid actions, and family members visible (0 is no people/static scene, 10 is beautiful candid group shot).\n\n"
        "Also generate:\n"
        "- A scene label identifying the setting (e.g. 'hotel_room', 'beach', 'hiking_trail', 'restaurant' or the specific landmark name like 'manaus_city_palace', 'teatro_amazonas', 'panama_canal' if recognizable).\n"
        "- A warm, descriptive caption (1-2 sentences) capturing the emotion and setting from a family member perspective.\n\n"
        "You must respond ONLY with a raw YAML array of objects (one for each image in order of input):\n"
        "```yaml\n"
        "-\n"
        "  index: 0\n"
        "  sharpness: 0-10\n"
        "  composition: 0-10\n"
        "  uniqueness: 0-10\n"
        "  human_presence: 0-10\n"
        "  scene_label: \"string\"\n"
        "  caption: \"string\"\n"
        "```"
    )
    
    contents.append(prompt)
    
    results_map = {}
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents
        )
        
        raw_text = response.text.strip()
        if raw_text.startswith("```yaml"):
            raw_text = raw_text[7:]
        elif raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()
        
        parsed_results = yaml.safe_load(raw_text)
        for res in parsed_results:
            idx = res.get("index")
            if idx is not None and idx < len(photo_batch):
                filename = photo_batch[idx]["filename"]
                
                # Calculate composite score (weighted average: human presence has high weight for family trip)
                w_sharpness = float(res.get("sharpness", 5)) * 0.15
                w_composition = float(res.get("composition", 5)) * 0.20
                w_uniqueness = float(res.get("uniqueness", 5)) * 0.25
                w_human = float(res.get("human_presence", 5)) * 0.40
                composite_score = round(w_sharpness + w_composition + w_uniqueness + w_human, 2)
                
                results_map[filename] = {
                    "score": composite_score,
                    "sharpness": float(res.get("sharpness", 5)),
                    "composition": float(res.get("composition", 5)),
                    "uniqueness": float(res.get("uniqueness", 5)),
                    "human_presence": float(res.get("human_presence", 5)),
                    "scene_label": str(res.get("scene_label", "unknown")).lower().strip(),
                    "caption": str(res.get("caption", "Exploring the sights."))
                }
    except Exception as e:
        print(f"Error during batched vision scoring: {e}")
        
    # Populate fallbacks
    for item in photo_batch:
        filename = item["filename"]
        if filename not in results_map:
            results_map[filename] = {
                "score": 5.0,
                "sharpness": 5.0,
                "composition": 5.0,
                "uniqueness": 5.0,
                "human_presence": 5.0,
                "scene_label": "unknown",
                "caption": "Exploring the sights."
            }
            
    return [results_map[item["filename"]] for item in photo_batch]

