import json
import os
import re
from google import genai
from google.genai import types

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)

def generate_moment_journal(scene_label: str, photos_info_json: str, voice_note_text: str = "") -> str:
    """
    Generates a warm, personal 2-3 sentence journal entry for a specific travel moment.
    """
    client = get_gemini_client()

    # ==========================================
    # TODO (USER): Refine this system prompt to adjust the narration tone
    # (e.g. funny, nostalgic, or kid-friendly family writing style).
    # ==========================================
    narration_prompt = (
        f"You are the family archivist writing a day-by-day travel journal for a family trip.\n"
        f"Moment Setting: {scene_label}\n"
        f"Photo Details (captions and scores): {photos_info_json}\n"
    )
    if voice_note_text:
        narration_prompt += f"Voice note transcript from participant: \"{voice_note_text}\"\n"

    narration_prompt += (
        "\nWrite a warm, nostalgic, and personal journal entry (exactly 2-3 sentences) in the first-person plural (we/our).\n"
        "Weave together details visible in the photos and any thoughts from the voice note. Do not mention scores or file names.\n"
        "Narrative Entry:"
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=narration_prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"Error during journal narration: {e}")
        return f"We spent some time at the {scene_label.replace('_', ' ')} capturing beautiful memories together."
