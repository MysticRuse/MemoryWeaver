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

def run_vision_moderation(path: str, filename: str) -> dict:
    """
    Runs vision moderation on a single photo to verify safety, sharpness, and type.
    
    Args:
        path: Path to the image file.
        filename: Filename of the image.
        
    Returns:
        A dict with keys appropriate, sharp, real_photo, reason, usable.
    """
    results = run_vision_moderation_batch([{"path": path, "filename": filename}])
    return results[0] if results else {
        "usable": False,
        "appropriate": False,
        "sharp": False,
        "real_photo": False,
        "reason": "Failed to run moderation."
    }

def run_vision_moderation_batch(photo_batch: list) -> list:
    """
    Sends a batch of photos (up to 50) to Gemini Vision to verify safety, sharpness, and type.
    Uses YAML formatting for the prompt and response to optimize model parsing.
    """
    client = get_gemini_client()
    
    contents = []
    prompt_intro = (
        "You are analyzing a batch of uploaded travel images for a family journal.\n"
        "Analyze each image provided in the contents list. The images are sent in order (the first image corresponds to index 0, the second to index 1, etc.).\n"
        "Determine if each image is:\n"
        "1. Appropriate and safe to share (no violence, nudity, or offensive content).\n"
        "2. Sharp enough to view (not excessively blurry, completely black, or corrupted).\n"
        "3. A real travel/trip photograph, NOT a meme, document scan, or screenshot.\n\n"
        "You must respond ONLY with a raw YAML array of objects (one for each image in order of input):\n"
        "```yaml\n"
        "-\n"
        "  index: 0\n"
        "  appropriate: true/false\n"
        "  sharp: true/false\n"
        "  real_photo: true/false\n"
        "  reason: \"Vivid explanation of your decisions, especially if any flag is false\"\n"
        "```"
    )
    
    # Pack compressed images and labels
    for idx, item in enumerate(photo_batch):
        path = item["path"]
        try:
            img = Image.open(path).convert("RGB")
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")
            
            # Append label text part before image part
            contents.append(f"\n--- IMAGE INDEX {idx} ---")
            contents.append(img_part)
        except Exception as ie:
            pass
            
    contents.append(prompt_intro)
    
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
                results_map[filename] = {
                    "usable": bool(res.get("appropriate", True) and res.get("sharp", True) and res.get("real_photo", True)),
                    "appropriate": bool(res.get("appropriate", True)),
                    "sharp": bool(res.get("sharp", True)),
                    "real_photo": bool(res.get("real_photo", True)),
                    "reason": str(res.get("reason", "Passed moderation"))
                }
    except Exception as e:
        print(f"Error during batched vision moderation: {e}")
        
    # Populate fallbacks for any missing items in batch response
    for item in photo_batch:
        filename = item["filename"]
        if filename not in results_map:
            results_map[filename] = {
                "usable": False,
                "appropriate": False,
                "sharp": False,
                "real_photo": False,
                "reason": "Moderation batch request failed or skipped for this file."
            }
            
    return [results_map[item["filename"]] for item in photo_batch]

