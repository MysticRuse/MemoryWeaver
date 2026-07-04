import json
import os
from google import genai
from google.genai import types

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)

def generate_all_moments_journal(moments_list: list) -> dict:
    """
    Generates factual, location-specific journal entries for all travel moments 
    in a single, unified Gemini API call.
    """
    client = get_gemini_client()

    # Filenames inside moments_list are user-controlled uploads; sanitize them
    # before they're embedded in the prompt (prompt-injection guard).
    from pipeline.prompt_safety import sanitize_for_prompt
    for moment in moments_list:
        for photo in moment.get("top_photos", []):
            if "filename" in photo:
                photo["filename"] = sanitize_for_prompt(photo["filename"])

    prompt = (
        "You are the family archivist writing a day-by-day travel journal for a family trip.\n"
        "Here is the chronological list of daily moments/scenes along with details of the top photos captured:\n\n"
        f"{json.dumps(moments_list, indent=2)}\n\n"
        "Generate a factual, personal journal entry for each moment in the first-person plural (we/our).\n"
        "CRITICAL INSTRUCTIONS:\n"
        "- Respond with a JSON array of objects. Each object in the array must contain exactly these two keys:\n"
        "  * 'moment': (string matching the input moment key exactly)\n"
        "  * 'entry': (string, exactly 2-3 sentences long describing the moment)\n"
        "- Do NOT use generic travel clichés or fluffy/flowery filler sentences (e.g. 'creating memories to last a lifetime', 'captivated by the beauty', 'a sight to behold').\n"
        "- Do refer to specific names of places, buildings, landmarks, or objects visible in the photo details (e.g. Manaus City Palace, Teatro Amazonas, Panama, jungle lodge).\n"
        "- Keep the narrative grounded, factual, and interesting.\n"
        "- Respond ONLY with a raw JSON array matching the schema."
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            )
        )
        
        # Clean any markdown wrapper if present
        clean_text = response.text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        clean_text = clean_text.strip()
        
        entries = json.loads(clean_text)
        # Convert list to dict mapping moment -> entry
        return {item["moment"]: item["entry"] for item in entries if "moment" in item and "entry" in item}
    except Exception as e:
        print(f"Error during batched journal narration: {e}")
        # Fallback mapping
        return {}
