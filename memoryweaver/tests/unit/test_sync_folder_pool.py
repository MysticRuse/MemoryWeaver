"""Folder sync must reflect the watched folder without owning the whole pool.

Two regressions live here.

1. Sync state was written to ``local_storage/sessions/<session_id>/`` by the old
   library switcher. The single default library reads ``local_storage/``, so once
   the switcher was removed ``scan_folder`` found no target folder and returned
   early. The pool froze at whatever had been synced the day the switcher went
   away: five newer files on the watched folder never appeared.

2. ``scan_folder`` ended by deleting every file in uploads that the sync registry
   did not list. The vault endpoint runs a scan on *every* fetch, so a direct
   upload, a magic-edit output or an extracted video frame was destroyed on the
   next page load. Folder-driven deletions are registry-driven and tested
   separately below.
"""

import json
import os

import pytest

from app.sync_manager import SyncManager


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """A SyncManager whose storage root and watched folder are both throwaway."""
    storage_root = tmp_path / "local_storage"
    watched = tmp_path / "watched"
    (storage_root / "uploads").mkdir(parents=True)
    watched.mkdir()

    def fake_init(self, session_id="default"):
        self.session_id = session_id
        self.session_dir = str(storage_root)
        self.config_path = os.path.join(self.session_dir, "sync_config.json")
        self.registry_path = os.path.join(self.session_dir, "sync_registry.json")
        self._adopt_legacy_session_sync_state()

    monkeypatch.setattr(SyncManager, "__init__", fake_init)
    return storage_root, watched


def test_legacy_session_sync_config_is_adopted(sync_env):
    storage_root, watched = sync_env
    legacy = storage_root / "sessions" / "family-trip-california-435cf2"
    legacy.mkdir(parents=True)
    (legacy / "sync_config.json").write_text(json.dumps({"target_folder": str(watched)}))
    (legacy / "sync_registry.json").write_text(json.dumps({"files": {"a.jpg": {}}}))

    manager = SyncManager()

    assert manager.get_config()["target_folder"] == str(watched)
    assert manager.load_registry()["files"] == {"a.jpg": {}}
    assert not (legacy / "sync_config.json").exists()


def test_scan_picks_up_files_added_long_after_the_first_sync(sync_env):
    storage_root, watched = sync_env
    (storage_root / "sync_config.json").write_text(json.dumps({"target_folder": str(watched)}))
    (watched / "first.jpg").write_bytes(b"first")

    SyncManager().scan_folder()
    assert SyncManager().scan_folder()["stats"]["new"] == 0

    (watched / "much_later.jpg").write_bytes(b"much later")
    stats = SyncManager().scan_folder()["stats"]

    assert stats["new"] == 1
    uploads = os.listdir(storage_root / "uploads")
    assert any(f.endswith("first.jpg") for f in uploads)
    assert any(f.endswith("much_later.jpg") for f in uploads)


def test_scan_removes_uploads_for_files_deleted_from_the_folder(sync_env):
    storage_root, watched = sync_env
    (storage_root / "sync_config.json").write_text(json.dumps({"target_folder": str(watched)}))
    (watched / "keep.jpg").write_bytes(b"keep")
    (watched / "drop.jpg").write_bytes(b"drop")
    SyncManager().scan_folder()

    (watched / "drop.jpg").unlink()
    stats = SyncManager().scan_folder()["stats"]

    assert stats["deleted"] == 1
    uploads = os.listdir(storage_root / "uploads")
    assert any(f.endswith("keep.jpg") for f in uploads)
    assert not any(f.endswith("drop.jpg") for f in uploads)


def test_scan_leaves_media_that_did_not_come_from_the_folder(sync_env):
    storage_root, watched = sync_env
    (storage_root / "sync_config.json").write_text(json.dumps({"target_folder": str(watched)}))
    (watched / "synced.jpg").write_bytes(b"synced")

    uploads = storage_root / "uploads"
    (uploads / "default_abc123_hand_uploaded.jpg").write_bytes(b"uploaded by hand")
    (uploads / "default_abc123_hand_uploaded_bw.png").write_bytes(b"magic edit output")
    (uploads / "extracted_frame_1785480660_clip.jpg").write_bytes(b"video frame")

    SyncManager().scan_folder()

    survivors = set(os.listdir(uploads))
    assert "default_abc123_hand_uploaded.jpg" in survivors
    assert "default_abc123_hand_uploaded_bw.png" in survivors
    assert "extracted_frame_1785480660_clip.jpg" in survivors
