import os
import tempfile
from pathlib import Path

from app.sync_manager import SyncManager


def test_sync_manager_flow():
    # Create temporary directories for source folder and session base
    with tempfile.TemporaryDirectory() as temp_src, tempfile.TemporaryDirectory() as temp_base:
        # Override project root storage for the test case
        session_id = "test_session_sync"
        manager = SyncManager(session_id=session_id)

        # Override manager base paths to point to our temp folder
        manager.session_dir = temp_base
        manager.config_path = os.path.join(temp_base, "sync_config.json")
        manager.registry_path = os.path.join(temp_base, "sync_registry.json")
        manager.storage.local_base = temp_base

        # 1. Config Test
        manager.save_config({"target_folder": temp_src})
        config = manager.get_config()
        assert config["target_folder"] == temp_src

        # Create a test file in the source directory
        sub_dir = os.path.join(temp_src, "sub")
        os.makedirs(sub_dir, exist_ok=True)
        test_file_path = os.path.join(sub_dir, "pic.jpg")
        with open(test_file_path, "w") as f:
            f.write("dummy-image-content")

        # 2. Scanning Test
        res = manager.scan_folder()
        assert res["status"] == "success"
        assert res["stats"]["new"] == 1
        assert res["stats"]["skipped"] == 0

        # Verify copied file exists in uploads
        uploads_dir = os.path.join(temp_base, "uploads")
        assert os.path.exists(uploads_dir)
        files = os.listdir(uploads_dir)
        assert len(files) == 1
        uploaded_fn = files[0]
        assert uploaded_fn.startswith(session_id)

        # 3. Incremental Scan Test (should skip unchanged)
        res_inc = manager.scan_folder()
        assert res_inc["status"] == "success"
        assert res_inc["stats"]["new"] == 0
        assert res_inc["stats"]["skipped"] == 1

        # 4. Sync Deletions Test
        # Remove file from uploads directory (simulates web delete action)
        os.remove(os.path.join(uploads_dir, uploaded_fn))

        res_del = manager.writeback_changes()
        assert res_del["status"] == "success"
        assert res_del["stats"]["deletions_synced"] == 1

        # Verify file is deleted from source folder
        assert not os.path.exists(test_file_path)

        # 5. Keep Both / Replacements Test
        # Re-create original file
        with open(test_file_path, "w") as f:
            f.write("new-original")

        # Re-scan to populate registry
        manager.scan_folder()
        files = os.listdir(uploads_dir)
        assert len(files) == 1
        uploaded_fn = files[0]

        # Simulate enhanced file creation in uploads
        upload_base, upload_ext = os.path.splitext(uploaded_fn)
        enhanced_fn = f"{upload_base}_enhanced{upload_ext}"
        enhanced_path = os.path.join(uploads_dir, enhanced_fn)
        with open(enhanced_path, "w") as f:
            f.write("enhanced-content")

        # Run writeback
        res_write = manager.writeback_changes()
        assert res_write["status"] == "success"
        assert res_write["stats"]["replacements_synced"] == 1

        # Verify enhanced file was copied to local folder alongside original
        expected_enhanced_device = os.path.join(sub_dir, "pic_enhanced.jpg")
        assert os.path.exists(expected_enhanced_device)
        with open(expected_enhanced_device) as f:
            assert f.read() == "enhanced-content"

def test_browse_directories(monkeypatch):
    from app.routers.cleaner import browse_directories
    with tempfile.TemporaryDirectory() as temp_dir:
        # Browsing is confined to MW_BROWSE_ROOTS; declare the temp dir as the
        # root for this test. Resolve first so macOS /var -> /private/var
        # symlinking does not make the path look like an escape.
        real_temp = str(Path(temp_dir).resolve())
        monkeypatch.setenv("MW_BROWSE_ROOTS", real_temp)

        os.makedirs(os.path.join(real_temp, "Folder A"))
        os.makedirs(os.path.join(real_temp, "Folder B"))
        os.makedirs(os.path.join(real_temp, ".hidden"))
        with open(os.path.join(real_temp, "text.txt"), "w") as f:
            f.write("text")

        res = browse_directories(path=real_temp)
        assert res["status"] == "success"
        assert res["current_path"] == real_temp
        assert len(res["directories"]) == 2
        dir_names = [d["name"] for d in res["directories"]]
        assert "Folder A" in dir_names
        assert "Folder B" in dir_names
