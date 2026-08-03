"""Photo Cleanup Card Generator v1 — the keep/delete card shown in the detail panel.

Replaces the old "professional photographer" critique (summary / rating /
critique / tips). The user is scrolling a camera roll deciding keep-or-delete,
so the card has to be readable in about two seconds: one plain headline, a
score, a verdict, three facts, one tag.

Two deliberate departures from the written spec, both to stop the model
inventing metadata it cannot actually see:

* the model is **not** given the EXIF block and is **not** asked for ``facts``.
  :func:`build_facts` assembles that array in code from the EXIF the route
  already read, so raw coordinates can never leak into it and a file size can
  never be guessed;
* the "never auto-delete a human or pet subject" rule is re-applied in
  :func:`parse_cleanup_card` after parsing. The prompt states it, but a rule
  that protects the only photo of someone should not depend on the model
  having obeyed.

The vocabulary here is the card's own — ``keep``/``review``/``likely_delete``
and the nine tags below. It is independent of the vault taxonomy in
``photo_taxonomy.py``; do not merge the two.
"""

import json

VERDICTS = ("keep", "review", "likely_delete")

TAGS = (
    "people",
    "pet",
    "food",
    "scenery",
    "document",
    "screenshot",
    "blurry",
    "duplicate",
    "other",
)

# A clear human or pet subject is never auto-marked for deletion — the worst
# case is deleting the only shot of someone, so a person decides instead.
SENTIMENTAL_TAGS = frozenset({"people", "pet"})

HEADLINE_MAX_WORDS = 8
REASON_MAX_WORDS = 6
MAX_FACTS = 3

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

CLEANUP_CARD_PROMPT = """\
You are generating a compact "cleanup card" for a photo declutter tool. The user
is scrolling through their camera roll fast, deciding whether to keep or delete
each photo. Your output must be scannable in under 2 seconds — no essays, no
photography critique, no pro tips.

OUTPUT: strict JSON only. No markdown, no prose outside the JSON, no code fences.

{
  "headline": "<max 8 words, plain description of subject/scene — no adjectives like 'heartwarming' or 'beautiful'>",
  "quality_score": <number 0.0-10.0, one decimal, based on focus/exposure/composition only>,
  "verdict": "<keep | review | likely_delete>",
  "verdict_reason": "<max 6 words, the ONE deciding factor>",
  "tag": "<single word: people | pet | food | scenery | document | screenshot | blurry | duplicate | other>"
}

VERDICT RULES:
- "likely_delete": blurry, badly exposed, accidental shot (pocket/ground/dark),
  near-duplicate of a better shot in the same burst, or a test/junk screenshot.
- "review": borderline quality, OR a sentimental subject (person/pet) that's
  technically weak.
- "keep": clear subject, acceptable quality, no red flags.
- Precedence: the sentimental rule wins. If the frame has a clear human or pet
  subject, never return "likely_delete" no matter how blurry, dark or duplicated
  the shot is — return "review" and let a person decide.

STRICT EXCLUSIONS — never include in output:
- Camera settings (aperture, ISO, shutter speed, color space, lens specs)
- Raw GPS coordinates, altitude, direction/heading
- Pixel dimensions, software version, filename
- Multi-sentence critique, coaching, or "pro tips" style suggestions
- Flattering or emotional language in headline/reason — stay neutral and factual
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


def _clip_words(text: str, limit: int) -> str:
    words = str(text or "").split()
    return " ".join(words[:limit])


def _coerce_score(value) -> float:
    """Clamp to the 0.0-10.0, one-decimal contract the badge renders."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(10.0, num)), 1)


def format_fact_date(timestamp) -> str:
    """EXIF ``YYYY:MM:DD HH:MM:SS`` (or ISO) to the card's ``Mon D, YYYY``.

    Returns "" for anything unparseable so the caller drops the fact rather
    than printing a placeholder.
    """
    raw = str(timestamp or "").strip()
    if not raw or raw.lower().startswith("unknown"):
        return ""
    date_part = raw.replace(":", "-", 2).split("T")[0].split(" ")[0]
    bits = date_part.split("-")
    if len(bits) != 3:
        return ""
    try:
        year, month, day = int(bits[0]), int(bits[1]), int(bits[2])
    except ValueError:
        return ""
    if not 1 <= month <= 12:
        return ""
    return f"{_MONTHS[month - 1]} {day}, {year}"


def format_fact_size(size_kb) -> str:
    """Human file size, e.g. ``1.8 MB`` / ``640 KB``."""
    try:
        kb = float(size_kb)
    except (TypeError, ValueError):
        return ""
    if kb <= 0:
        return ""
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{round(kb)} KB"


def build_facts(timestamp=None, place=None, device=None, size_kb=None) -> list:
    """Assemble the at-most-three facts row from EXIF the route already has.

    ``place`` is a resolved place *name*. There is no reverse geocoder wired up
    yet, so callers pass ``None`` and the file size takes the slot — which is
    exactly the spec's fallback. Coordinates must never be passed here.

    Missing or unreadable fields are omitted, never guessed.
    """
    facts = []

    date_fact = format_fact_date(timestamp)
    if date_fact:
        facts.append(date_fact)

    place_fact = str(place or "").strip()
    if place_fact:
        facts.append(place_fact)

    device_fact = str(device or "").strip()
    if device_fact and not device_fact.lower().startswith("unknown"):
        facts.append(device_fact)

    if not place_fact:
        size_fact = format_fact_size(size_kb)
        if size_fact:
            facts.append(size_fact)

    return facts[:MAX_FACTS]


def unavailable_card(reason: str, facts=None) -> dict:
    """The card to show when the model could not be reached.

    Verdict is "review" rather than a blank: the EXIF facts are still worth
    showing, and an unscored photo is exactly one a person should look at.
    """
    return {
        "headline": "",
        "quality_score": 0.0,
        "verdict": "review",
        "verdict_reason": _clip_words(reason, REASON_MAX_WORDS),
        "facts": list(facts or [])[:MAX_FACTS],
        "tag": "other",
    }


def parse_cleanup_card(res_txt: str, facts=None) -> dict:
    """Turn a raw model response into the card the frontend renders.

    Raises ``ValueError`` if the payload is not a JSON object or names a
    verdict outside :data:`VERDICTS` — the caller falls back to an error card,
    and silently coercing to "review" would hide a broken prompt.
    """
    payload = json.loads(_strip_fences(res_txt))
    if not isinstance(payload, dict):
        raise ValueError("cleanup card response was not a JSON object")

    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r}")

    tag = str(payload.get("tag", "")).strip().lower()
    if tag not in TAGS:
        tag = "other"

    # Sentimental precedence, enforced here and not just asked for.
    if verdict == "likely_delete" and tag in SENTIMENTAL_TAGS:
        verdict = "review"

    return {
        "headline": _clip_words(payload.get("headline"), HEADLINE_MAX_WORDS),
        "quality_score": _coerce_score(payload.get("quality_score")),
        "verdict": verdict,
        "verdict_reason": _clip_words(payload.get("verdict_reason"), REASON_MAX_WORDS),
        "facts": list(facts or [])[:MAX_FACTS],
        "tag": tag,
    }
