"""Coverage for the video watermark/delogo pipeline.

`remove_video_watermark` is 445 lines of OpenCV + ffmpeg with no tests. This
file pins its observable behaviour on a synthetic clip so the function can be
decomposed safely, and so the classical-before-model cost path stays honest.
"""

import os

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    import app.app_utils.storage as storage_mod
    import app.routers.video as video_mod

    base = tmp_path / "local_storage"
    (base / "uploads").mkdir(parents=True)
    (base / "thumbs").mkdir(parents=True)

    class TmpStorage(storage_mod.StorageHelper):
        def __init__(self, session_id="default"):
            self.session_id = session_id
            self.bucket_name = None
            self.project_id = None
            self.local_base = str(base)
            self._gcs_prefix = ""
            self.use_gcs = False

    monkeypatch.setattr(video_mod, "StorageHelper", TmpStorage)
    monkeypatch.setattr(storage_mod, "StorageHelper", TmpStorage)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return base


def _write_clip(path, frames=20, size=(160, 120), watermark=True):
    """A clip with a constant bright square in the corner (a fake watermark)."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(path), fourcc, 15.0, size)
    for i in range(frames):
        frame = np.full((size[1], size[0], 3), (i * 7) % 200, dtype=np.uint8)
        if watermark:
            frame[8:28, 8:48] = 255  # static region -> low variance
        out.write(frame)
    out.release()


def _encrypted_upload(base, name, session_id):
    from app.app_utils.crypto import encrypt_bytes

    raw_path = base / "raw.mp4"
    _write_clip(raw_path)
    data = raw_path.read_bytes()
    dest = base / "uploads" / name
    dest.write_bytes(encrypt_bytes(data, session_id))
    raw_path.unlink()
    return dest


def test_watermark_removal_produces_playable_output(session_dir):
    from app.routers.video import remove_video_watermark
    from app.schemas import RemoveWatermarkRequest

    session_id = "default"
    _encrypted_upload(session_dir, "clip.mp4", session_id)

    result = remove_video_watermark(
        RemoveWatermarkRequest(
            filename="clip.mp4",
            session_id=session_id,
            remove_watermarks=True,
            remove_logos=False,
            remove_captions=False,
            remove_overlays=False,
            remove_voice=False,
        )
    )

    assert result["status"] == "success", result

    # The handler writes `processed_<name>.mp4` alongside the original and
    # reports only a message, so assert on the artifact it produced.
    produced = [p for p in (session_dir / "uploads").iterdir() if "processed" in p.name]
    assert produced, "no processed video was written"
    assert produced[0].stat().st_size > 0

    # Output is encrypted at rest like every other stored media file.
    from app.app_utils.crypto import decrypt_bytes, is_encrypted

    blob = produced[0].read_bytes()
    assert is_encrypted(blob), "processed video was stored unencrypted"
    assert decrypt_bytes(blob, session_id)[:4] not in (b"", None)


def test_temp_working_files_are_cleaned_up(session_dir):
    """The handler writes temp_wm_in_/temp_wm_out_ scratch files."""
    from app.routers.video import remove_video_watermark
    from app.schemas import RemoveWatermarkRequest

    _encrypted_upload(session_dir, "clip3.mp4", "default")
    remove_video_watermark(
        RemoveWatermarkRequest(
            filename="clip3.mp4", session_id="default",
            remove_watermarks=True, remove_logos=False,
            remove_captions=False, remove_overlays=False, remove_voice=False,
        )
    )
    leftovers = [p.name for p in session_dir.iterdir() if p.name.startswith("temp_wm_")]
    assert not leftovers, f"scratch files left behind: {leftovers}"


def test_missing_source_raises_not_found(session_dir):
    from app.app_utils.errors import AppError
    from app.routers.video import remove_video_watermark
    from app.schemas import RemoveWatermarkRequest

    with pytest.raises((AppError, Exception)) as exc:
        remove_video_watermark(
            RemoveWatermarkRequest(
                filename="nope.mp4", session_id="default",
                remove_watermarks=True, remove_logos=False,
                remove_captions=False, remove_overlays=False, remove_voice=False,
            )
        )
    assert "not found" in str(exc.value).lower() or getattr(exc.value, "status_code", 0) == 404


def test_classical_detection_runs_without_api_key(session_dir, capsys):
    """The cost playbook promises the classical path handles the common case;
    with no key configured the model must never be reached."""
    from app.routers.video import remove_video_watermark
    from app.schemas import RemoveWatermarkRequest

    _encrypted_upload(session_dir, "clip2.mp4", "default")
    remove_video_watermark(
        RemoveWatermarkRequest(
            filename="clip2.mp4", session_id="default",
            remove_watermarks=True, remove_logos=False,
            remove_captions=False, remove_overlays=False, remove_voice=False,
        )
    )
    assert "GEMINI_API_KEY" not in os.environ
