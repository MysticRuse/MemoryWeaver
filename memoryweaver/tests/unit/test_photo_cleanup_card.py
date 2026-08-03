"""Cleanup Card Generator v1 — the contract the detail panel renders.

The card replaced a free-text photographer critique, so most of these tests
guard *exclusions*: what must never reach the screen (raw GPS, camera settings,
invented facts) and the one rule that protects a photo from being auto-marked
for deletion.
"""

import json

import pytest

from app.services.photo_cleanup_card import (
    CLEANUP_CARD_PROMPT,
    MAX_FACTS,
    build_facts,
    format_fact_date,
    format_fact_size,
    parse_cleanup_card,
    unavailable_card,
)


def card_json(**overrides):
    payload = {
        "headline": "Dog resting paw on lap",
        "quality_score": 7.2,
        "verdict": "keep",
        "verdict_reason": "Clear subject, good focus",
        "tag": "pet",
    }
    payload.update(overrides)
    return json.dumps(payload)


# --- parsing ---------------------------------------------------------------


def test_parses_a_well_formed_card():
    card = parse_cleanup_card(card_json(), facts=["May 24, 2026", "iPhone 16 Pro Max"])
    assert card == {
        "headline": "Dog resting paw on lap",
        "quality_score": 7.2,
        "verdict": "keep",
        "verdict_reason": "Clear subject, good focus",
        "facts": ["May 24, 2026", "iPhone 16 Pro Max"],
        "tag": "pet",
    }


def test_strips_the_code_fence_the_model_adds_anyway():
    card = parse_cleanup_card(f"```json\n{card_json()}\n```")
    assert card["verdict"] == "keep"


def test_unknown_verdict_raises_rather_than_defaulting():
    """A silent coercion to "review" would hide a broken prompt."""
    with pytest.raises(ValueError):
        parse_cleanup_card(card_json(verdict="maybe_delete"))


def test_non_object_payload_raises():
    with pytest.raises(ValueError):
        parse_cleanup_card('["keep"]')


def test_unknown_tag_falls_back_to_other():
    card = parse_cleanup_card(card_json(tag="sunset"))
    assert card["tag"] == "other"


# --- the sentimental rule --------------------------------------------------


@pytest.mark.parametrize("tag", ["people", "pet"])
def test_a_human_or_pet_subject_is_never_auto_marked_for_deletion(tag):
    """Blur loses to the sentimental rule — a person decides instead."""
    card = parse_cleanup_card(
        card_json(verdict="likely_delete", verdict_reason="Badly out of focus", tag=tag)
    )
    assert card["verdict"] == "review"
    assert card["verdict_reason"] == "Badly out of focus"


def test_likely_delete_survives_on_a_non_sentimental_subject():
    card = parse_cleanup_card(card_json(verdict="likely_delete", tag="screenshot"))
    assert card["verdict"] == "likely_delete"


def test_the_prompt_states_the_precedence_it_relies_on():
    assert "sentimental rule wins" in CLEANUP_CARD_PROMPT


# --- field limits ----------------------------------------------------------


def test_headline_is_clipped_to_eight_words():
    long_headline = " ".join(f"word{i}" for i in range(20))
    card = parse_cleanup_card(card_json(headline=long_headline))
    assert len(card["headline"].split()) == 8


def test_verdict_reason_is_clipped_to_six_words():
    card = parse_cleanup_card(card_json(verdict_reason="one two three four five six seven"))
    assert card["verdict_reason"] == "one two three four five six"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(12.5, 10.0), (-3, 0.0), ("8.25", 8.2), (None, 0.0), ("junk", 0.0)],
)
def test_quality_score_is_clamped_to_one_decimal_in_range(raw, expected):
    assert parse_cleanup_card(card_json(quality_score=raw))["quality_score"] == expected


def test_facts_never_exceed_three_entries():
    card = parse_cleanup_card(card_json(), facts=["a", "b", "c", "d", "e"])
    assert len(card["facts"]) == MAX_FACTS


def test_model_supplied_facts_are_ignored():
    """Facts come from EXIF in code, so the model cannot invent or leak one."""
    payload = json.loads(card_json())
    payload["facts"] = ["37.40005, -122.10870", "f/1.78", "made up place"]
    card = parse_cleanup_card(json.dumps(payload), facts=["May 24, 2026"])
    assert card["facts"] == ["May 24, 2026"]


# --- facts from EXIF -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026:05:24 21:27:01", "May 24, 2026"),
        ("2026-05-24T21:27:01", "May 24, 2026"),
        ("2026-01-02", "Jan 2, 2026"),
        ("Unknown Time", ""),
        ("", ""),
        (None, ""),
        ("2026:13:24 00:00:00", ""),
    ],
)
def test_date_fact_formatting(raw, expected):
    assert format_fact_date(raw) == expected


@pytest.mark.parametrize(
    ("kb", "expected"),
    [(2128.7, "2.1 MB"), (640.0, "640 KB"), (0, ""), (None, ""), ("x", "")],
)
def test_size_fact_formatting(kb, expected):
    assert format_fact_size(kb) == expected


def test_file_size_takes_the_slot_when_no_place_is_resolved():
    facts = build_facts(
        timestamp="2026:05:24 21:27:01", place=None,
        device="iPhone 16 Pro Max", size_kb=2128.7,
    )
    assert facts == ["May 24, 2026", "iPhone 16 Pro Max", "2.1 MB"]


def test_a_resolved_place_displaces_the_file_size():
    facts = build_facts(
        timestamp="2026:05:24 21:27:01", place="Palo Alto, CA",
        device="iPhone 16 Pro Max", size_kb=2128.7,
    )
    assert facts == ["May 24, 2026", "Palo Alto, CA", "iPhone 16 Pro Max"]


def test_missing_fields_are_omitted_not_guessed():
    assert build_facts(timestamp=None, device="Unknown Device", size_kb=None) == []


def test_build_facts_caps_at_three():
    facts = build_facts(
        timestamp="2026:05:24 21:27:01", place="Palo Alto, CA",
        device="iPhone 16 Pro Max", size_kb=None,
    )
    assert len(facts) <= MAX_FACTS


# --- prompt exclusions -----------------------------------------------------


@pytest.mark.parametrize(
    "banned",
    ['"summary"', '"critique"', '"tips"', '"rating"', "professional photographer"],
)
def test_the_old_critique_keys_are_gone_from_the_prompt(banned):
    """"critique" and "tips" survive only as things the card must NOT contain."""
    assert banned not in CLEANUP_CARD_PROMPT


def test_the_prompt_does_not_ask_for_facts():
    """Facts are built in code; asking for them invites invented metadata."""
    assert '"facts"' not in CLEANUP_CARD_PROMPT


# --- unavailable state -----------------------------------------------------


def test_unavailable_card_is_reviewable_and_keeps_its_facts():
    card = unavailable_card("No API key set", ["May 24, 2026"])
    assert card["verdict"] == "review"
    assert card["quality_score"] == 0.0
    assert card["facts"] == ["May 24, 2026"]
