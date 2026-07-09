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
    with Image.open(out_path) as out_img:
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
