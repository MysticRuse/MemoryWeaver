import json
import os
from google import genai

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)

def generate_trip_story(journal_entries_json: str, destination: str = "our trip") -> str:
    """
    Weaves together individual journal entries and moments into a flowing, multi-paragraph
    trip story summarizing the entire trip experience.
    """
    client = get_gemini_client()

    try:
        entries = json.loads(journal_entries_json)
        formatted_entries = "\n".join([
            f"- {entry.get('moment')}: {entry.get('entry')}" 
            for entry in entries
        ])
    except Exception:
        formatted_entries = journal_entries_json

    story_prompt = (
        f"You are a master travel writer wrapping up a family vacation to {destination}.\n"
        f"Here are the individual moments we recorded:\n{formatted_entries}\n\n"
        "Weave these memories into a beautiful, cohesive multi-paragraph trip story (2-3 paragraphs).\n"
        "Use first-person plural (we/our). The tone should be excited, warm, and highly engaging, "
        "highlighting the journey from start to finish. Focus on the emotional connection of traveling together.\n"
        "Full Trip Story:"
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=story_prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"Error during trip story generation: {e}")
        return f"We had an amazing trip to {destination}, sharing many laughs, exploring new places, and creating memories that will last a lifetime."
