import os
import json
from PIL import Image
import pytest
from app.fast_api_app import apply_enhancements, select_photo_version, SelectPhotoVersionRequest, list_uploads, StorageHelper
from fastapi import BackgroundTasks

def test_apply_enhancements(tmp_path):
    img = Image.new("RGB", (100, 100), color="blue")
    img_path = os.path.join(tmp_path, "orig.jpg")
    img.save(img_path, format="JPEG")
    
    out_path = os.path.join(tmp_path, "enhanced.jpg")
    apply_enhancements(img_path, out_path, brightness=1.1, contrast=1.2, saturation=1.3, sharpness=1.4, warmth=1.05)
    
    assert os.path.exists(out_path)
    from app.fast_api_app import load_image_bytes_decrypted
    import io
    decrypted_bytes = load_image_bytes_decrypted(out_path, "default")
    with Image.open(io.BytesIO(decrypted_bytes)) as out_img:
        assert out_img.size == (100, 100)

def test_select_photo_version_logic(tmp_path, monkeypatch):
    monkeypatch.setattr(StorageHelper, "__init__", lambda self, session_id: setattr(self, "local_base", str(tmp_path)))
    
    upload_dir = os.path.join(tmp_path, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    
    orig_path = os.path.join(upload_dir, "test.jpg")
    Image.new("RGB", (10, 10), color="blue").save(orig_path)
    
    enhanced_path = os.path.join(upload_dir, "test_enhanced.jpg")
    Image.new("RGB", (10, 10), color="red").save(enhanced_path)
    
    # 1. Select enhanced
    req = SelectPhotoVersionRequest(filename="test.jpg", version="enhanced", session_id="test")
    res = select_photo_version(req)
    assert res["status"] == "success"
    
    backup_path = os.path.join(upload_dir, "test_original.jpg")
    assert os.path.exists(backup_path)
    
    metadata_file = os.path.join(tmp_path, "sessions", "test", "photos_metadata.json")
    assert os.path.exists(metadata_file)
    with open(metadata_file, "r") as f:
        meta = json.load(f)
        assert meta["test.jpg"]["is_enhanced"] is True
    
    # 2. Select original
    req = SelectPhotoVersionRequest(filename="test.jpg", version="original", session_id="test")
    res = select_photo_version(req)
    assert res["status"] == "success"

def test_list_uploads_excludes_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(StorageHelper, "__init__", lambda self, session_id: setattr(self, "local_base", str(tmp_path)))
    
    upload_dir = os.path.join(tmp_path, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    
    # Create test files
    Image.new("RGB", (5, 5)).save(os.path.join(upload_dir, "pic1.jpg"))
    Image.new("RGB", (5, 5)).save(os.path.join(upload_dir, "pic1_enhanced.jpg"))
    Image.new("RGB", (5, 5)).save(os.path.join(upload_dir, "pic1_original.jpg"))
    
    bg = BackgroundTasks()
    res = list_uploads(background_tasks=bg, session_id="test")
    assert res["status"] == "success"
    
    filenames = [p["filename"] for p in res["photos"]]
    assert "pic1.jpg" in filenames
    assert "pic1_enhanced.jpg" not in filenames
    assert "pic1_original.jpg" not in filenames

def test_video_upload_length_and_split(tmp_path, monkeypatch):
    import numpy as np
    import cv2
    from PIL import Image
    from agents.collector.tools.upload import process_and_save_upload
    from app.fast_api_app import split_video_frames, SplitVideoRequest
    
    def mock_storage_init(self, session_id):
        self.local_base = str(tmp_path)
        self.use_gcs = False
    monkeypatch.setattr(StorageHelper, "__init__", mock_storage_init)
    
    upload_dir = os.path.join(tmp_path, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    
    # 1. Create a dummy video file using OpenCV (e.g. 5 frames, ~30 FPS -> 0.16s length)
    video_path = os.path.join(tmp_path, "test_video.mp4")
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(video_path, fourcc, 30.0, (100, 100))
    for _ in range(5):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        out.write(frame)
    out.release()
    
    with open(video_path, "rb") as f:
        video_bytes = f.read()
        
    # Test valid short video upload
    res = process_and_save_upload(
        file_bytes=video_bytes,
        original_filename="test_video.mp4",
        contributor_name="Test User",
        session_id="test_session"
    )
    
    assert res["filename"].endswith("test_video.mp4")
    assert os.path.exists(os.path.join(upload_dir, res["filename"]))
    
    # Test splitting frames
    req = SplitVideoRequest(filename=res["filename"], session_id="test_session", num_frames=3)
    split_res = split_video_frames(req)
    
    assert split_res["status"] == "success"
    assert len(split_res["files"]) == 3
    for f in split_res["files"]:
        assert os.path.exists(os.path.join(upload_dir, f))

def test_video_duration_exceeds_limit(tmp_path, monkeypatch):
    import numpy as np
    import cv2
    from agents.collector.tools.upload import process_and_save_upload
    
    def mock_storage_init(self, session_id):
        self.local_base = str(tmp_path)
        self.use_gcs = False
    monkeypatch.setattr(StorageHelper, "__init__", mock_storage_init)
    
    # Create a dummy video file with high number of frames to pretend it's long (e.g. 4000 frames at 30 FPS -> 133s length)
    video_path = os.path.join(tmp_path, "long_video.mp4")
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(video_path, fourcc, 30.0, (10, 10))
    for _ in range(4000):
        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        out.write(frame)
    out.release()
    
    with open(video_path, "rb") as f:
        video_bytes = f.read()
        
    with pytest.raises(ValueError, match="exceeds the 2-minute limit"):
        process_and_save_upload(
            file_bytes=video_bytes,
            original_filename="long_video.mp4",
            contributor_name="Test User",
            session_id="test_session"
        )
