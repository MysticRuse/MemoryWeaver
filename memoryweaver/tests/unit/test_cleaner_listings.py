"""Content tests for the Cleaner's four listing endpoints.

These exist because of a specific regression. Every one of these endpoints
filtered its directory listing with ``f.startswith(session_id + "_")``. When the
library switcher was removed and the app settled on a single library, files whose
names carried an *older* library's id were silently filtered out - videos, Live
Photos, duplicate clusters and the reclaimable-space figure all reported empty
while 13 real files sat in the pool.

The generic smoke sweep in test_endpoint_smoke.py did not catch it: it asserts
routes do not return 5xx, and an endpoint returning ``[]`` passes that happily.

So every fixture below deliberately uses filenames that do **not** begin with the
session id. If someone reintroduces a prefix filter, these fail immediately.
"""

import io

import pytest
from PIL import Image

cv2 = pytest.importorskip("cv2")
import numpy as np

from app.app_utils.crypto import encrypt_bytes

# Names from a different library than the session under test. This is the whole
# point of the fixture - see the module docstring.
FOREIGN = "old-library-abc123"


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A populated library whose filenames carry a foreign library id."""
    import app.app_utils.storage as storage_mod
    import app.routers.cleaner as cleaner_mod

    base = tmp_path / "local_storage"
    uploads = base / "uploads"
    uploads.mkdir(parents=True)
    (base / "thumbs").mkdir(parents=True)

    class TmpStorage(storage_mod.StorageHelper):
        def __init__(self, session_id="default"):
            self.session_id = session_id
            self.local_base = str(base)
            self.bucket_name = None
            self.project_id = None
            self.use_gcs = False

    for mod in (storage_mod, cleaner_mod):
        monkeypatch.setattr(mod, "StorageHelper", TmpStorage, raising=False)
    # The vault auto-runs a folder sync; keep it out of the way.
    monkeypatch.setattr("app.sync_manager.SyncManager.scan_folder",
                        lambda self: {"status": "skipped", "stats": {"new": 0}},
                        raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return base


def _photo(uploads, name, colour, size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG")
    (uploads / name).write_bytes(encrypt_bytes(buf.getvalue(), "default"))


def _video(uploads, name, frames=12, size=(96, 72)):
    raw = uploads.parent / f"raw_{name}"
    out = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, size)
    for i in range(frames):
        out.write(np.full((size[1], size[0], 3), (i * 11) % 255, dtype=np.uint8))
    out.release()
    (uploads / name).write_bytes(encrypt_bytes(raw.read_bytes(), "default"))
    raw.unlink()


# --- /api/cleaner/videos ---------------------------------------------------


def test_videos_listing_finds_videos(library):
    from app.routers.cleaner import get_cleaner_videos

    uploads = library / "uploads"
    _video(uploads, f"{FOREIGN}_aaa_clip_one.mp4")
    _video(uploads, f"{FOREIGN}_bbb_clip_two.mov")
    _photo(uploads, f"{FOREIGN}_ccc_still.jpg", "red")

    result = get_cleaner_videos(session_id="default")
    assert result["status"] == "success"
    names = [v["filename"] for v in result["videos"]]
    assert len(names) == 2, f"expected 2 videos, got {names}"
    assert f"{FOREIGN}_ccc_still.jpg" not in names, "a photo leaked into the video list"


def test_videos_listing_is_empty_when_there_are_none(library):
    from app.routers.cleaner import get_cleaner_videos

    _photo(library / "uploads", f"{FOREIGN}_only_a_photo.jpg", "blue")
    result = get_cleaner_videos(session_id="default")
    assert result["status"] == "success"
    assert result["videos"] == []


# --- /api/cleaner/live-photos ---------------------------------------------


def test_live_photos_pairs_still_and_motion(library):
    """A Live Photo is a still plus a same-named clip."""
    from app.routers.cleaner import get_cleaner_live_photos

    uploads = library / "uploads"
    _photo(uploads, f"{FOREIGN}_ddd_beach.jpg", "teal")
    _video(uploads, f"{FOREIGN}_ddd_beach.mov")

    result = get_cleaner_live_photos(session_id="default")
    assert result["status"] == "success"
    assert len(result["live_photos"]) >= 1, result


def test_live_photos_ignores_unpaired_files(library):
    from app.routers.cleaner import get_cleaner_live_photos

    uploads = library / "uploads"
    _photo(uploads, f"{FOREIGN}_eee_lonely.jpg", "olive")
    _video(uploads, f"{FOREIGN}_fff_different.mov")

    result = get_cleaner_live_photos(session_id="default")
    assert result["live_photos"] == []


# --- /api/cleaner/similar-photos -----------------------------------------


def test_similar_photos_clusters_near_duplicates(library):
    """Duplicate detection is a local 8x8 average hash - no API needed."""
    from app.routers.cleaner import get_cleaner_similar_photos

    uploads = library / "uploads"
    # Two visually identical frames, one clearly different.
    _photo(uploads, f"{FOREIGN}_ggg_dupe_a.jpg", "navy")
    _photo(uploads, f"{FOREIGN}_hhh_dupe_b.jpg", "navy")
    _photo(uploads, f"{FOREIGN}_iii_unique.jpg", "white")

    result = get_cleaner_similar_photos(session_id="default")
    assert result["status"] == "success"
    clustered = {fn for c in result["clusters"] for fn in
                 (c.get("photos") or c.get("files") or [])
                 if isinstance(fn, str)}
    assert result["clusters"], "no duplicate cluster found for two identical photos"
    if clustered:
        assert f"{FOREIGN}_iii_unique.jpg" not in clustered


def test_similar_photos_handles_a_single_photo(library):
    from app.routers.cleaner import get_cleaner_similar_photos

    _photo(library / "uploads", f"{FOREIGN}_jjj_alone.jpg", "maroon")
    result = get_cleaner_similar_photos(session_id="default")
    assert result["status"] == "success"
    assert result["clusters"] == []


# --- /api/cleaner/storage-summary ----------------------------------------


def test_storage_summary_counts_real_files(library):
    from app.routers.cleaner import get_cleaner_storage_summary

    uploads = library / "uploads"
    _photo(uploads, f"{FOREIGN}_kkk_one.jpg", "purple", size=(200, 200))
    _photo(uploads, f"{FOREIGN}_lll_two.jpg", "green", size=(200, 200))
    _video(uploads, f"{FOREIGN}_mmm_clip.mp4")

    result = get_cleaner_storage_summary(session_id="default")
    assert result["status"] == "success"
    assert result["total_files"] == 3, result
    assert result["total_size_bytes"] > 0, "library reported zero bytes"


def test_storage_summary_on_empty_library(library):
    from app.routers.cleaner import get_cleaner_storage_summary

    result = get_cleaner_storage_summary(session_id="default")
    assert result["status"] == "success"
    assert result["total_files"] == 0
    assert result["total_size_bytes"] == 0


# --- the vault itself ------------------------------------------------------


def test_vault_lists_media_regardless_of_filename_prefix(library):
    """The headline regression: 13 files present, grid showed none."""
    from app.routers.cleaner import get_cleaner_vault

    uploads = library / "uploads"
    for i in range(4):
        _photo(uploads, f"{FOREIGN}_p{i}_shot.jpg", "orange")
    _video(uploads, f"{FOREIGN}_v0_clip.mp4")

    result = get_cleaner_vault(session_id="default")
    assert result["status"] == "success"
    assert len(result["existing_photos"]) == 5, result["existing_photos"]


def test_vault_excludes_enhanced_and_original_variants(library):
    """Edit by-products must not appear as separate library entries."""
    from app.routers.cleaner import get_cleaner_vault

    uploads = library / "uploads"
    _photo(uploads, f"{FOREIGN}_q0_shot.jpg", "coral")
    _photo(uploads, f"{FOREIGN}_q0_shot_enhanced.jpg", "coral")
    _photo(uploads, f"{FOREIGN}_q0_shot_original.jpg", "coral")

    listed = get_cleaner_vault(session_id="default")["existing_photos"]
    assert listed == [f"{FOREIGN}_q0_shot.jpg"], listed


def test_no_listing_endpoint_filters_by_session_prefix():
    """Guards the whole regression class at the source level."""
    import inspect

    import app.routers.cleaner as cleaner_mod

    source = inspect.getsource(cleaner_mod)
    assert 'startswith(session_id + "_")' not in source, (
        "a session-id filename prefix filter is back; it hides media carried "
        "over from an earlier library (see this module's docstring)"
    )
