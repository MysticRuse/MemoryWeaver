"""Photo Auto-Categorizer v1 — taxonomy contract.

These pin the two places the old 4-category prompt went wrong: everything
informational collapsing into `info` (which the UI labels "Credentials Vault"),
and the prompt/parser being copy-pasted into two modules that then drifted.
"""

import json

import pytest

from app.services.photo_taxonomy import (
    CATEGORIZER_PROMPT,
    PRIMARY_CATEGORIES,
    SPEC_TO_UI,
    UI_SUBCATEGORY,
    parse_categorizer_response,
)

# The pill slugs `resolvePhotoCategory` in frontend/cleaner.html filters on.
# If this list and the frontend's `validCats` ever disagree, photos silently
# vanish from every pill.
UI_PILLS = {
    "people",
    "scrap",
    "scenery",
    "food",
    "trips",
    "pets",
    "emotional",
    "info",
    "docs",
    "other",
}


def _response(**overrides) -> str:
    payload = {
        "primary_category": "food_dining",
        "secondary_tags": [],
        "confidence": 0.9,
        "needs_review": False,
        "reason": "Plated pasta on restaurant table",
        "caption": "Dinner is served! 🍝",
        "extracted_text": "",
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestTaxonomyShape:
    def test_every_spec_category_maps_to_a_real_ui_pill(self):
        assert set(SPEC_TO_UI) == set(PRIMARY_CATEGORIES)
        assert set(SPEC_TO_UI.values()) <= UI_PILLS

    def test_every_ui_pill_has_a_display_label(self):
        assert set(UI_SUBCATEGORY) == UI_PILLS

    def test_prompt_names_every_primary_category(self):
        for category in PRIMARY_CATEGORIES:
            assert category in CATEGORIZER_PROMPT

    def test_prompt_does_not_offer_the_retired_four(self):
        # "organized"/"emotional" as *model-facing* category names are gone;
        # `emotional` survives only as an internal UI slug.
        assert '"organized"' not in CATEGORIZER_PROMPT
        assert "1. credentials_vault" in CATEGORIZER_PROMPT


class TestParsing:
    def test_maps_spec_name_to_ui_slug(self):
        out = parse_categorizer_response(_response())
        assert out["category"] == "food"
        assert out["subcategory"] == "Food & Dining"
        assert out["primary_category"] == "food_dining"

    def test_strips_markdown_fences(self):
        fenced = "```json\n" + _response() + "\n```"
        assert parse_categorizer_response(fenced)["category"] == "food"

    def test_documents_no_longer_land_in_the_credentials_vault(self):
        # The exact regression this taxonomy exists to fix.
        out = parse_categorizer_response(_response(primary_category="documents_receipts"))
        assert out["category"] == "docs"

    def test_private_content_is_separate_from_credentials(self):
        assert parse_categorizer_response(_response(primary_category="private_vault"))["category"] == "emotional"
        assert parse_categorizer_response(_response(primary_category="credentials_vault"))["category"] == "info"

    def test_unknown_category_raises_rather_than_defaulting(self):
        with pytest.raises(ValueError):
            parse_categorizer_response(_response(primary_category="vacation"))

    def test_non_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_categorizer_response("Sure! Here is the classification.")


class TestTripsPromotion:
    def test_scenery_on_a_trip_fills_the_trips_pill(self):
        out = parse_categorizer_response(
            _response(primary_category="scenery_nature", secondary_tags=["trips"])
        )
        assert out["category"] == "trips"
        assert out["secondary_tags"] == ["trips"]

    def test_a_trip_selfie_stays_in_people(self):
        # Promoting these would empty the People pill on a holiday library.
        out = parse_categorizer_response(
            _response(primary_category="people", secondary_tags=["trips"])
        )
        assert out["category"] == "people"

    def test_a_boarding_pass_stays_in_documents(self):
        out = parse_categorizer_response(
            _response(primary_category="documents_receipts", secondary_tags=["trips"])
        )
        assert out["category"] == "docs"


class TestPrivacyRules:
    def test_credential_text_is_dropped_even_if_the_model_transcribes_it(self):
        out = parse_categorizer_response(
            _response(primary_category="credentials_vault", extracted_text="Pass: hunter2")
        )
        assert out["extracted_text"] == ""

    def test_receipt_text_is_kept(self):
        out = parse_categorizer_response(
            _response(primary_category="documents_receipts", extracted_text="Total $42.10")
        )
        assert out["extracted_text"] == "Total $42.10"


class TestFieldNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0.9, 9), (0.55, 6), (1.0, 10), (8, 8), (None, 5), ("high", 5), (99, 10)],
    )
    def test_confidence_is_stored_out_of_ten(self, raw, expected):
        assert parse_categorizer_response(_response(confidence=raw))["confidence"] == expected

    def test_caption_becomes_the_card_text_and_reason_is_kept(self):
        out = parse_categorizer_response(_response())
        assert out["reason"] == "Dinner is served! 🍝"
        assert out["classification_reason"] == "Plated pasta on restaurant table"

    def test_falls_back_to_reason_when_no_caption(self):
        out = parse_categorizer_response(_response(caption=""))
        assert out["reason"] == "Plated pasta on restaurant table"

    def test_needs_review_survives(self):
        assert parse_categorizer_response(_response(needs_review=True))["needs_review"] is True
        assert parse_categorizer_response(_response())["needs_review"] is False
