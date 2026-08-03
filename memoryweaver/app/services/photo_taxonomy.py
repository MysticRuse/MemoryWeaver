"""Photo Auto-Categorizer v1 — the single source of truth for the taxonomy.

The classifier prompt and its response parser used to be copy-pasted into both
``services/classification.py`` (ingest path) and ``routers/cleaner.py``
(``analyze-all`` path), which is how they drifted. Both now import from here.

Two vocabularies are in play and must not be confused:

* the **spec** names (``credentials_vault``, ``food_dining``, ...) — what the
  model is asked to emit;
* the **UI slugs** (``info``, ``food``, ...) — what the vault stores and what
  ``resolvePhotoCategory`` in ``frontend/cleaner.html`` filters the pills on.

Only :func:`parse_categorizer_response` crosses between them.
"""

import json

# Spec priority order — first match wins, so credentials/private outrank
# everything else even when a person or pet is in frame.
PRIMARY_CATEGORIES = (
    "credentials_vault",
    "private_vault",
    "documents_receipts",
    "people",
    "pets",
    "food_dining",
    "scenery_nature",
    "junk_scrap",
    "other_misc",
)

SPEC_TO_UI = {
    "credentials_vault": "info",
    "private_vault": "emotional",
    "documents_receipts": "docs",
    "people": "people",
    "pets": "pets",
    "food_dining": "food",
    "scenery_nature": "scenery",
    "junk_scrap": "scrap",
    "other_misc": "other",
}

UI_SUBCATEGORY = {
    "info": "Credentials Vault",
    "emotional": "Private Vault",
    "docs": "Documents & Receipts",
    "people": "People",
    "pets": "Pets",
    "food": "Food & Dining",
    "scenery": "Scenery & Nature",
    "trips": "Trips",
    "scrap": "Junk & Scrap",
    "other": "Other / Misc",
}

# The UI pills are mutually exclusive, so a "trips" secondary tag can only take
# over a card that has no stronger home. Promoting a trip selfie out of People
# would empty the People pill on a holiday library.
TRIPS_PROMOTABLE = frozenset({"scenery", "other"})

# Categories where transcribing what is on screen would defeat the point of the
# category. The spec forbids emitting credential values; we enforce it locally
# too rather than trusting the model to have obeyed.
NO_TRANSCRIPTION = frozenset({"info"})

CATEGORIZER_PROMPT = """\
You are a photo classification engine for a personal photo vault app. For each
image, assign exactly ONE primary category from the fixed list below. Your output
will be parsed programmatically — return ONLY valid JSON, no prose, no markdown
fences.

CATEGORIES (in priority order — check top to bottom, first match wins):

1. credentials_vault
   - Screenshots or photos of: passwords, PINs, OTPs, 2FA/QR codes, API keys,
     seed phrases, login screens, ID cards (passport, driver's license, SSN/Aadhaar/PAN),
     credit/debit cards, bank account numbers.
   - Trigger even if the credential is only PARTIALLY visible or embedded in a
     larger screenshot.

2. private_vault
   - Content the user would not want in a general gallery but is NOT a credential:
     medical documents/scans, intimate or sensitive personal photos, screenshots
     of private conversations, financial statements without account numbers,
     anything explicitly marked "private" or "confidential" in visible text.

3. documents_receipts
   - Receipts, invoices, bills, contracts, forms, printed/scanned text documents,
     boarding passes, tickets — NOT already caught by category 1 or 2.

4. people
   - Primary subject is one or more identifiable humans (portrait, selfie, group
     photo, candid). Takes priority over pets/scenery/food if a person is the
     clear main subject.

5. pets
   - Primary subject is a domestic animal (dog, cat, bird, etc.) with no human
     as the main subject.

6. food_dining
   - Meals, dishes, restaurant/menu shots, drinks, cooking-in-progress. No human
     face as primary subject.

7. scenery_nature
   - Landscapes, cityscapes, plants, wildlife (non-pet), sky, water, architecture
     shot for its own sake — no people/food/pet as primary subject.

8. trips
   - ONLY apply if EXIF/metadata or filename indicates the photo is part of a
     travel context (different geo from home location, or supplied trip_id) AND
     it doesn't already trigger categories 1-3. Trips is a cross-cutting tag, so
     return it in the "secondary_tags" array, never as primary_category.

9. junk_scrap
   - Blurry, dark, accidental, duplicate-looking, screenshots with no informational
     value, test shots, photos of the ground/pocket/nothing.

10. other_misc
    - Fallback only. Use when nothing above confidently applies.

RULES:
- Categories 1 and 2 override everything else, even if a person or pet is also
  visible in the frame. Privacy-sensitive content always wins the classification.
- If confidence for the top match is below 0.6, still return your best guess but
  set "needs_review": true.
- Never guess a person's identity or name — just detect presence of people.
- Do not describe or transcribe any credential values you see (numbers, codes,
  passwords) in your output — category detection only, no content extraction.
- "extracted_text": transcribe readable text ONLY for documents_receipts (totals,
  merchant, dates) or other informational shots. Return "" for credentials_vault
  and for any image whose text is a credential. Return "" when there is no text.
- "caption" is shown to the user under the photo: one casual, warm, human line
  summarizing what the photo shows, emoji welcome (e.g. "Happy puppy days! 🐶🐾",
  "Dinner is served! 🍝"). Never put credential content or a person's name in it.
  For credentials_vault use a neutral line such as "Kept safe in the vault 🔐".

OUTPUT FORMAT (strict JSON, single line, no other text):
{
  "primary_category": "<one of: credentials_vault | private_vault | documents_receipts | people | pets | food_dining | scenery_nature | junk_scrap | other_misc>",
  "secondary_tags": ["trips"],
  "confidence": 0.0-1.0,
  "needs_review": true|false,
  "reason": "<max 12 words, no PII, no transcribed credential content>",
  "caption": "<warm one-line caption for the gallery card>",
  "extracted_text": "<readable text, or empty string>"
}
"""


def _strip_fences(text: str) -> str:
    """Undo the ```json fence the model adds despite being told not to."""
    stripped = text.strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:]
    elif stripped.startswith("```"):
        stripped = stripped[3:]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def _coerce_confidence(value) -> int:
    """The spec emits 0.0-1.0; the vault has always stored a score out of 10."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 5
    if num <= 1.0:
        num *= 10
    return max(0, min(10, round(num)))


def parse_categorizer_response(res_txt: str) -> dict:
    """Turn a raw model response into a vault classification record.

    Raises ``ValueError`` if the payload is not JSON or names a category
    outside the taxonomy — callers already fall back to local heuristics, and a
    silent ``other_misc`` would hide a broken prompt.
    """
    payload = json.loads(_strip_fences(res_txt))
    if not isinstance(payload, dict):
        raise ValueError("categorizer response was not a JSON object")

    primary = str(payload.get("primary_category", "")).strip().lower()
    if primary not in SPEC_TO_UI:
        raise ValueError(f"unknown primary_category: {primary!r}")

    raw_tags = payload.get("secondary_tags") or []
    tags = [str(t).strip().lower() for t in raw_tags if str(t).strip()]

    ui_category = SPEC_TO_UI[primary]
    if "trips" in tags and ui_category in TRIPS_PROMOTABLE:
        ui_category = "trips"

    extracted = str(payload.get("extracted_text") or "")
    if ui_category in NO_TRANSCRIPTION:
        extracted = ""

    caption = str(payload.get("caption") or "").strip()
    reason = str(payload.get("reason") or "").strip()

    return {
        "category": ui_category,
        "subcategory": UI_SUBCATEGORY[ui_category],
        "extracted_text": extracted,
        # `reason` is what the gallery card renders, so the warm caption wins
        # when the model supplied one; the terse spec reason is kept alongside.
        "reason": caption or reason,
        "classification_reason": reason,
        "confidence": _coerce_confidence(payload.get("confidence")),
        "primary_category": primary,
        "secondary_tags": tags,
        "needs_review": bool(payload.get("needs_review", False)),
    }
