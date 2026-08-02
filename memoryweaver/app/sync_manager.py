import hashlib
import json
import os
import shutil

from app.app_utils.logging_config import get_logger
from app.app_utils.storage import StorageHelper

logger = get_logger(__name__)


class SyncManager:
    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        self.storage = StorageHelper(session_id=session_id)

        # Ensure session storage directory exists
        self.session_dir = self.storage.local_base
        os.makedirs(self.session_dir, exist_ok=True)

        self.config_path = os.path.join(self.session_dir, "sync_config.json")
        self.registry_path = os.path.join(self.session_dir, "sync_registry.json")

    def get_config(self) -> dict:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path) as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "sync_manager", exc)
        return {"target_folder": ""}

    def save_config(self, config: dict):
        with open(self.config_path, "w") as f:
            json.dump(config, f, indent=4)

    def load_registry(self) -> dict:
        if os.path.exists(self.registry_path):
            try:
                with open(self.registry_path) as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "sync_manager", exc)
        return {"files": {}}

    def save_registry(self, registry: dict):
        with open(self.registry_path, "w") as f:
            json.dump(registry, f, indent=4)

    def scan_folder(self) -> dict:
        config = self.get_config()
        target_folder = config.get("target_folder", "")

        if not target_folder or not os.path.exists(target_folder):
            return {
                "status": "error",
                "message": f"Target folder '{target_folder}' does not exist or is not set.",
                "stats": {"new": 0, "skipped": 0, "deleted": 0}
            }

        registry = self.load_registry()
        files_dict = registry.setdefault("files", {})

        # Walk target directory recursively
        scanned_paths = set()
        new_count = 0
        skipped_count = 0

        supported_extensions = (
            '.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif',
            '.mp4', '.mov', '.avi', '.mkv'
        )

        uploads_dir = os.path.join(self.session_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)

        # Self-healing: Strip session/hash prefix from local target folder files if any were created by old code
        for root, _, files in os.walk(target_folder):
            for file in files:
                if file.startswith(self.session_id + "_"):
                    parts = file.split('_')
                    if len(parts) > 2:
                        clean_local_name = "_".join(parts[2:])
                        old_device_path = os.path.join(root, file)
                        new_device_path = os.path.join(root, clean_local_name)
                        if not os.path.exists(new_device_path):
                            try:
                                os.rename(old_device_path, new_device_path)
                                logger.info(f"Self-healed local file prefix: {file} -> {clean_local_name}")
                            except Exception as sh_err:
                                logger.warning(f"Failed to self-heal prefix for {file}: {sh_err}")
        for root, _, files in os.walk(target_folder):
            for file in files:
                if not file.lower().endswith(supported_extensions) or file.startswith('.'):
                    continue

                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, target_folder)
                scanned_paths.add(rel_path)

                try:
                    stat = os.stat(abs_path)
                    size = stat.st_size
                    mtime = stat.st_mtime
                except Exception:
                    continue

                # Check registry for matches
                reg_item = files_dict.get(rel_path)
                if reg_item and reg_item.get("size") == size and reg_item.get("mtime") == mtime:
                    # Verify if file actually exists in uploads directory
                    upload_fn = reg_item.get("upload_filename", "")
                    if upload_fn and os.path.exists(os.path.join(uploads_dir, upload_fn)):
                        skipped_count += 1
                        continue

                # Check file content checksum to prevent copying identical files
                def get_file_md5(p):
                    h = hashlib.md5()
                    try:
                        with open(p, 'rb') as f_in:
                            buf = f_in.read(65536)
                            while len(buf) > 0:
                                h.update(buf)
                                buf = f_in.read(65536)
                        return h.hexdigest()
                    except Exception:
                        return ""

                content_hash = get_file_md5(abs_path)
                renamed_existing = False
                if content_hash:
                    for r_path, r_item in list(files_dict.items()):
                        if r_path != rel_path and r_item.get("content_hash") == content_hash:
                            old_upload_fn = r_item.get("upload_filename", "")
                            old_upload_path = os.path.join(uploads_dir, old_upload_fn)
                            if old_upload_fn and os.path.exists(old_upload_path):
                                # Determine new upload filename based on the new relative path
                                ext = os.path.splitext(file)[1] or ".jpg"
                                fn_hash = hashlib.md5(rel_path.encode('utf-8')).hexdigest()[:8]
                                new_upload_fn = f"{self.session_id}_{fn_hash}_{os.path.splitext(file)[0]}{ext}"
                                new_upload_path = os.path.join(uploads_dir, new_upload_fn)

                                try:
                                    # Rename the upload file on disk
                                    os.rename(old_upload_path, new_upload_path)

                                    # Update database classifications (cleaner_vault.json)
                                    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                    vault_file = os.path.join(project_root, "local_storage", "cleaner_vault.json")
                                    if os.path.exists(vault_file):
                                        try:
                                            with open(vault_file) as vf:
                                                vault_data = json.load(vf)
                                            updated_vault = False
                                            for key in ["classifications", "locked_photos", "notes"]:
                                                if key in vault_data and old_upload_fn in vault_data[key]:
                                                    vault_data[key][new_upload_fn] = vault_data[key].pop(old_upload_fn)
                                                    updated_vault = True
                                            if updated_vault:
                                                with open(vault_file, "w") as vf:
                                                    json.dump(vault_data, vf, indent=4)
                                        except Exception as vf_err:
                                            logger.warning(f"Failed to update cleaner_vault.json during sync rename: {vf_err}")
                                    # Update registry dictionary
                                    r_item["upload_filename"] = new_upload_fn
                                    r_item["mtime"] = mtime
                                    r_item["size"] = size
                                    files_dict[rel_path] = files_dict.pop(r_path)

                                    # Add to scanned paths so we know it exists
                                    scanned_paths.add(rel_path)
                                    renamed_existing = True
                                    skipped_count += 1
                                    break
                                except Exception as rename_err:
                                    logger.warning(f"Failed to process local rename from {old_upload_fn} to {new_upload_fn}: {rename_err}")
                if renamed_existing:
                    continue

                # Check for standard duplicates (same name/size/mtime already present elsewhere)
                if content_hash:
                    is_duplicate_content = False
                    for r_path, r_item in files_dict.items():
                        if r_path != rel_path and r_item.get("content_hash") == content_hash:
                            r_fn = r_item.get("upload_filename", "")
                            if r_fn and os.path.exists(os.path.join(uploads_dir, r_fn)):
                                is_duplicate_content = True
                                break
                    if is_duplicate_content:
                        skipped_count += 1
                        continue

                # Copy file and register it
                # Generate unique clean filename: session_id + hash_prefix + original_basename
                fn_hash = hashlib.md5(rel_path.encode('utf-8')).hexdigest()[:8]
                clean_name = f"{self.session_id}_{fn_hash}_{file}"
                dest_path = os.path.join(uploads_dir, clean_name)

                try:
                    shutil.copy2(abs_path, dest_path)
                    files_dict[rel_path] = {
                        "size": size,
                        "mtime": mtime,
                        "content_hash": content_hash,
                        "upload_filename": clean_name,
                        "status": "scanned"
                    }
                    new_count += 1
                except Exception as e:
                    logger.warning(f"Failed to copy file {abs_path}: {e}")
        # Identify deleted files (in registry but no longer on device)
        deleted_count = 0
        for rel_path in list(files_dict.keys()):
            if rel_path not in scanned_paths:
                # Remove file from uploads if present
                reg_item = files_dict[rel_path]
                upload_fn = reg_item.get("upload_filename", "")
                upload_path = os.path.join(uploads_dir, upload_fn)
                if os.path.exists(upload_path):
                    try:
                        os.remove(upload_path)
                    except Exception as exc:
                        logger.warning("%s: best-effort step failed, continuing: %s", "sync_manager", exc)
                del files_dict[rel_path]
                deleted_count += 1

        # Prune unregistered untracked files from uploads directory
        registered_upload_filenames = {
            item.get("upload_filename") for item in files_dict.values() if item.get("upload_filename")
        }
        for file in os.listdir(uploads_dir):
            if file.startswith('.') or file.lower().endswith(('_enhanced.jpg', '_original.jpg')):
                continue
            if file not in registered_upload_filenames:
                try:
                    os.remove(os.path.join(uploads_dir, file))
                except Exception as exc:
                    logger.warning("%s: best-effort step failed, continuing: %s", "sync_manager", exc)

        self.save_registry(registry)

        return {
            "status": "success",
            "stats": {
                "new": new_count,
                "skipped": skipped_count,
                "deleted": deleted_count
            }
        }

    def writeback_changes(self) -> dict:
        config = self.get_config()
        target_folder = config.get("target_folder", "")

        if not target_folder or not os.path.exists(target_folder):
            return {"status": "error", "message": "Target folder not configured."}

        registry = self.load_registry()
        files_dict = registry.get("files", {})
        uploads_dir = os.path.join(self.session_dir, "uploads")

        deletions_synced = 0
        replacements_synced = 0

        # 1. Sync Deletions
        # If the file exists in the registry but is missing from the uploads folder,
        # it means the user deleted it in the Web UI. We delete it from the device folder.
        for rel_path, item in list(files_dict.items()):
            upload_fn = item.get("upload_filename", "")
            upload_path = os.path.join(uploads_dir, upload_fn)
            device_path = os.path.join(target_folder, rel_path)

            if not os.path.exists(upload_path):
                # Deleted on Web UI
                if os.path.exists(device_path):
                    try:
                        os.remove(device_path)
                        deletions_synced += 1
                    except Exception as e:
                        logger.warning(f"Failed to delete {device_path}: {e}")
                # Remove from registry
                del files_dict[rel_path]

        # 2. Sync Enhancements / Keep Both / Replacements
        # Scan uploads directory for enhanced versions or compressed videos.
        # Format for compressed: {upload_fn_no_ext}_compressed.mp4
        # Format for enhanced: {upload_fn_no_ext}_enhanced.jpg
        for rel_path, item in list(files_dict.items()):
            upload_fn = item.get("upload_filename", "")
            if not upload_fn:
                continue

            upload_base, upload_ext = os.path.splitext(upload_fn)
            device_path = os.path.join(target_folder, rel_path)
            device_dir = os.path.dirname(device_path)
            device_base, device_ext = os.path.splitext(os.path.basename(device_path))

            # Check for compressed video file
            compressed_fn = f"{upload_base}_compressed{upload_ext}"
            compressed_path = os.path.join(uploads_dir, compressed_fn)
            if os.path.exists(compressed_path):
                # Write back compressed video
                dest_compressed = os.path.join(device_dir, f"{device_base}_compressed{device_ext}")
                try:
                    shutil.copy2(compressed_path, dest_compressed)
                    replacements_synced += 1
                except Exception as e:
                    logger.warning(f"Failed to writeback compressed video: {e}")
            # Check for enhanced photo file
            enhanced_fn = f"{upload_base}_enhanced{upload_ext}"
            enhanced_path = os.path.join(uploads_dir, enhanced_fn)
            if os.path.exists(enhanced_path):
                # Write back enhanced photo
                dest_enhanced = os.path.join(device_dir, f"{device_base}_enhanced{device_ext}")
                try:
                    shutil.copy2(enhanced_path, dest_enhanced)
                    replacements_synced += 1
                except Exception as e:
                    logger.warning(f"Failed to writeback enhanced photo: {e}")
        self.save_registry(registry)

        return {
            "status": "success",
            "stats": {
                "deletions_synced": deletions_synced,
                "replacements_synced": replacements_synced
            }
        }
