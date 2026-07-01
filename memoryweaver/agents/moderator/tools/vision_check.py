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

def run_vision_moderation(photo_path: str) -> dict:
    """
    Sends the photo to Gemini Vision to verify safety, sharpness, and type.
    """
    client = get_gemini_client()
    
    # 1. Resize and normalise photo to reduce token usage (max 1024px)
    try:
        img = Image.open(photo_path).convert("RGB")
        img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")
    except Exception as ie:
        return {
            "usable": False,
            "appropriate": False,
            "sharp": False,
            "real_photo": False,
            "reason": f"Corrupted or invalid image file: {str(ie)}"
        }

    # ==========================================
    # TODO (USER): Refine the prompt below to enforce specific safety,
    # sharpness, and screenshot filtering rules for group trip photos.
    # ==========================================
    moderation_prompt = (
        "Analyze this uploaded image. Determine if it is:\n"
        "1. Appropriate and safe to share (no violence, nudity, or offensive content).\n"
        "2. Sharp enough to view (not excessively blurry, completely black, or corrupted).\n"
        "3. A real travel/trip photograph, NOT a meme, document scan, or screenshot.\n\n"
        "You must respond ONLY with a raw JSON object containing these exact keys:\n"
        "{\n"
        '  "appropriate": true/false,\n'
        '  "sharp": true/false,\n'
        '  "real_photo": true/false,\n'
        '  "reason": "Vivid explanation of your decisions, especially if any flag is false"\n'
        "}"
    )

    try:
        # Use gemini-2.5-flash for fast and cost-effective multimodal inference
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[img_part, moderation_prompt]
        )
        
        # Clean markdown codeblocks from response text if present
        raw_text = response.text.strip()
        clean_json = re.sub(r"^```json\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
        
        result = json.loads(clean_json)
        return {
            "usable": bool(result.get("appropriate", True) and result.get("sharp", True) and result.get("real_photo", True)),
            "appropriate": bool(result.get("appropriate", True)),
            "sharp": bool(result.get("sharp", True)),
            "real_photo": bool(result.get("real_photo", True)),
            "reason": str(result.get("reason", "Passed moderation"))
        }
    except Exception as e:
        print(f"Error during vision moderation: {e}")
        return {
            "usable": False,
            "appropriate": False,
            "sharp": False,
            "real_photo": False,
            "reason": f"Moderation pipeline failure: {str(e)}"
        }
