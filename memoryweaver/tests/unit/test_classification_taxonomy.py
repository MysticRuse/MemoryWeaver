"""End-to-end: `classify_new_photos` must persist the new pill slugs.

The parser is unit-tested in `test_photo_taxonomy.py`; what this file guards is
the wiring — that the ingest path actually sends the v1 prompt and writes the
mapped category into the vault, rather than the old four-value vocabulary.
"""

import io
import json

import pytest
from PIL import Image

from app.app_utils.crypto import encrypt_bytes


@pytest.fixture
def ingest_library(tmp_path, monkeypatch):
    """Storage rooted in tmp_path with one encrypted JPEG awaiting analysis."""
    import app.app_utils.storage as storage_mod
    import app.services.classification as classification_mod
    import app.services.media_store as media_mod

    base = tmp_path / "local_storage"
    (base / "uploads").mkdir(parents=True, exist_ok=True)

    class TmpStorage(storage_mod.StorageHelper):
        def __init__(self, session_id="default"):
            self.session_id = session_id
            self.bucket_name = None
            self.project_id = None
            self.local_base = str(base)
            self._gcs_prefix = ""
            self.use_gcs = False

    for mod in (storage_mod, classification_mod, media_mod):
        if hasattr(mod, "StorageHelper"):
            monkeypatch.setattr(mod, "StorageHelper", TmpStorage)

    vault_file = base / "cleaner_vault.json"
    monkeypatch.setattr(
        classification_mod, "get_global_vault_file_path", lambda: str(vault_file)
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "olive").save(buf, format="JPEG")
    # Deliberately not named screenshot/receipt/etc so the local OCR shortcut
    # is skipped and the Gemini branch is the one under test.
    (base / "uploads" / "img_4821.jpg").write_bytes(encrypt_bytes(buf.getvalue(), "default"))
    return vault_file


def _classify(vault_file):
    from app.services.classification import classify_new_photos

    classify_new_photos("default", ["img_4821.jpg"])
    return json.loads(vault_file.read_text())["classifications"]["img_4821.jpg"]


def test_ingest_sends_the_v1_prompt(ingest_library, fake_gemini):
    fake_gemini.script(
        json.dumps(
            {
                "primary_category": "pets",
                "secondary_tags": [],
                "confidence": 0.94,
                "needs_review": False,
                "reason": "Dog asleep on a sofa",
                "caption": "Peaceful sleep 💤",
                "extracted_text": "",
            }
        )
    )
    _classify(ingest_library)
    assert "credentials_vault" in fake_gemini.last_prompt
    assert "primary_category" in fake_gemini.last_prompt


def test_ingest_persists_the_ui_slug_not_the_spec_name(ingest_library, fake_gemini):
    fake_gemini.script(
        json.dumps(
            {
                "primary_category": "documents_receipts",
                "secondary_tags": [],
                "confidence": 0.81,
                "needs_review": False,
                "reason": "Itemised grocery receipt",
                "caption": "Weekly shop sorted 🧾",
                "extracted_text": "Total 18.40",
            }
        )
    )
    record = _classify(ingest_library)
    # Would have been "info" -> "Credentials Vault" under the old taxonomy.
    assert record["category"] == "docs"
    assert record["subcategory"] == "Documents & Receipts"
    assert record["extracted_text"] == "Total 18.40"
    assert record["confidence"] == 8


def test_malformed_response_falls_back_instead_of_writing_junk(ingest_library, fake_gemini):
    fake_gemini.script("I'm not going to answer in JSON.")
    record = _classify(ingest_library)
    from app.services.photo_taxonomy import UI_SUBCATEGORY

    assert record["category"] in UI_SUBCATEGORY
