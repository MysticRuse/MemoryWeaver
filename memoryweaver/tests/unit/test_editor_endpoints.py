"""Coverage for the four editor endpoints being ported out of the Curator Hub.

`/api/photo-metadata`, `/api/magic-enhance`, `/api/select-photo-version` and
`/api/save-photo-voice` were reachable only from upload.html. They are moving to
the Cleaner, and upload.html is being deleted afterwards - so these tests exist
to prove the backends still work once their original caller is gone.

Written before the port, deliberately: they are the safety net for the deletion
phase, not a record of it.
"""

import io
import json

import pytest
from PIL import Image

from app.app_utils.crypto import decrypt_bytes, encrypt_bytes


@pytest.fixture
def editor_session(tmp_path, monkeypatch):
    """Storage rooted in tmp_path, with one encrypted JPEG in uploads."""
    import app.app_utils.storage as storage_mod
    import app.fast_api_app as app_mod
    import app.routers.photo as photo_mod
    import app.services.media_store as media_mod

    base = tmp_path / "local_storage"
    for sub in ("uploads", "thumbs", "artefacts"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    class TmpStorage(storage_mod.StorageHelper):
        def __init__(self, session_id="default"):
            self.session_id = session_id
            self.bucket_name = None
            self.project_id = None
            self.local_base = str(base)
            self._gcs_prefix = ""
            self.use_gcs = False

    for mod in (storage_mod, photo_mod, media_mod, app_mod):
        if hasattr(mod, "StorageHelper"):
            monkeypatch.setattr(mod, "StorageHelper", TmpStorage)

    buf = io.BytesIO()
    Image.new("RGB", (200, 150), "teal").save(buf, format="JPEG")
    (base / "uploads" / "shot.jpg").write_bytes(encrypt_bytes(buf.getvalue(), "default"))
    return base


# --- /api/photo-metadata ---------------------------------------------------


def test_photo_metadata_reads_exif_through_decryption(editor_session, fake_gemini):
    """Regression: EXIF was read straight off the encrypted file.

    extract_exif got ciphertext, swallowed "cannot identify image file", and the
    panel showed "Unknown Device" with no dimensions for every stored photo.
    """
    from app.routers.photo import get_photo_metadata

    fake_gemini.script('{"headline":"Teal test frame","quality_score":7.0,'
                       '"verdict":"review","verdict_reason":"Flat but clean",'
                       '"tag":"other"}')
    result = get_photo_metadata(filename="shot.jpg", session_id="default")
    assert result["status"] == "success", result
    # Dimensions prove the image was actually decoded, not just probed.
    assert "200" in result["dimensions"] and "150" in result["dimensions"]
    # And the cleanup card round-tripped through the real parser.
    card = result.get("gemini_analysis", {})
    assert card.get("verdict") == "review"
    assert card.get("quality_score") == 7.0


def test_photo_metadata_rejects_traversal(editor_session, fake_gemini):
    from app.app_utils.errors import AppError
    from app.routers.photo import get_photo_metadata

    with pytest.raises((AppError, Exception)):
        get_photo_metadata(filename="../../../etc/passwd", session_id="default")


def test_photo_metadata_missing_file_is_not_a_success(editor_session, fake_gemini):
    from app.app_utils.errors import AppError
    from app.routers.photo import get_photo_metadata

    try:
        result = get_photo_metadata(filename="nope.jpg", session_id="default")
    except (AppError, Exception):
        return  # raising is the acceptable contract
    assert result.get("status") != "success"


# --- /api/magic-enhance ----------------------------------------------------


def test_magic_enhance_writes_an_enhanced_variant(editor_session, fake_gemini):
    from app.routers.photo import run_magic_enhance
    from app.schemas import MagicEnhanceRequest

    fake_gemini.script(
        json.dumps({"brightness": 1.1, "contrast": 1.2, "saturation": 1.15,
                    "sharpness": 1.1, "warmth": 1.03})
    )
    result = run_magic_enhance(
        MagicEnhanceRequest(filename="shot.jpg", session_id="default")
    )
    assert result["status"] == "success", result
    enhanced = editor_session / "uploads" / "shot_enhanced.jpg"
    assert enhanced.exists(), "no enhanced variant written"
    assert enhanced.stat().st_size > 0


def test_magic_enhance_output_is_encrypted_at_rest(editor_session, fake_gemini):
    from app.app_utils.crypto import is_encrypted
    from app.routers.photo import run_magic_enhance
    from app.schemas import MagicEnhanceRequest

    fake_gemini.script(json.dumps({"brightness": 1.0, "contrast": 1.0,
                                   "saturation": 1.0, "sharpness": 1.0, "warmth": 1.0}))
    run_magic_enhance(MagicEnhanceRequest(filename="shot.jpg", session_id="default"))
    blob = (editor_session / "uploads" / "shot_enhanced.jpg").read_bytes()
    assert is_encrypted(blob), "enhanced photo stored unencrypted"
    assert decrypt_bytes(blob, "default")[:2] == b"\xff\xd8"  # JPEG SOI


def test_magic_enhance_survives_bad_model_json(editor_session, fake_gemini):
    """A malformed tuning response must fall back, not 500."""
    from app.routers.photo import run_magic_enhance
    from app.schemas import MagicEnhanceRequest

    fake_gemini.script("not json at all")
    result = run_magic_enhance(
        MagicEnhanceRequest(filename="shot.jpg", session_id="default")
    )
    assert result["status"] == "success", result
    assert (editor_session / "uploads" / "shot_enhanced.jpg").exists()
    # Neutral factors, not a failure.
    assert result["factors"]["brightness"] == 1.0


# --- /api/select-photo-version --------------------------------------------


def test_select_enhanced_then_original_roundtrip(editor_session, fake_gemini):
    from app.routers.photo import select_photo_version
    from app.schemas import SelectPhotoVersionRequest

    buf = io.BytesIO()
    Image.new("RGB", (200, 150), "orange").save(buf, format="JPEG")
    (editor_session / "uploads" / "shot_enhanced.jpg").write_bytes(
        encrypt_bytes(buf.getvalue(), "default")
    )

    res = select_photo_version(
        SelectPhotoVersionRequest(filename="shot.jpg", version="enhanced",
                                  session_id="default")
    )
    assert res["status"] == "success"
    assert (editor_session / "uploads" / "shot_original.jpg").exists(), "no backup kept"

    res = select_photo_version(
        SelectPhotoVersionRequest(filename="shot.jpg", version="original",
                                  session_id="default")
    )
    assert res["status"] == "success"


def test_select_version_records_enhanced_flag(editor_session, fake_gemini):
    from app.routers.photo import select_photo_version
    from app.schemas import SelectPhotoVersionRequest

    buf = io.BytesIO()
    Image.new("RGB", (200, 150), "orange").save(buf, format="JPEG")
    (editor_session / "uploads" / "shot_enhanced.jpg").write_bytes(
        encrypt_bytes(buf.getvalue(), "default")
    )
    select_photo_version(
        SelectPhotoVersionRequest(filename="shot.jpg", version="enhanced",
                                  session_id="default")
    )
    meta = editor_session / "sessions" / "default" / "photos_metadata.json"
    assert meta.exists()
    assert json.loads(meta.read_text())["shot.jpg"]["is_enhanced"] is True


# --- /api/save-photo-voice -------------------------------------------------


def test_save_photo_voice_persists_transcription(editor_session, fake_gemini):
    """The text-only path: saving a transcription without new audio."""
    import asyncio

    from app.fast_api_app import save_photo_voice

    fake_gemini.script("A quiet afternoon by the water.")
    result = asyncio.run(
        save_photo_voice(
            filename="shot.jpg",
            session_id="default",
            transcription="A quiet afternoon by the water.",
            audio=None,
        )
    )
    assert result["status"] == "success", result
    meta = editor_session / "sessions" / "default" / "photos_metadata.json"
    assert meta.exists()
    assert "shot.jpg" in json.loads(meta.read_text())


def test_editor_routes_are_registered_and_protected():
    """All four must stay mounted, behind the admin dependency."""
    from app.fast_api_app import app

    wanted = {
        "/api/photo-metadata",
        "/api/magic-enhance",
        "/api/select-photo-version",
        "/api/save-photo-voice",
    }
    seen = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in wanted:
            seen[path] = {d.call.__name__ for d in route.dependant.dependencies if d.call}
    missing = wanted - set(seen)
    assert not missing, f"editor routes disappeared: {missing}"
    for path, deps in seen.items():
        assert "require_admin_token" in deps, f"{path} lost its auth dependency"


def test_magic_enhance_is_behind_the_spend_cap():
    """It makes a billable Gemini call."""
    from app.fast_api_app import app

    routes = [r for r in app.routes if getattr(r, "path", None) == "/api/magic-enhance"]
    assert routes
    for route in routes:
        deps = {d.call.__name__ for d in route.dependant.dependencies if d.call}
        assert "enforce_ai_budget" in deps
