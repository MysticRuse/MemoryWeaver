"""Video tooling: frame splitting, watermark removal, scene description.

Split out of fast_api_app.py, which had grown to 6,474 lines with all 69
routes in one module.
"""

import json
import os

import numpy as np
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from app.app_utils.ai_budget import track_ai_call
from app.app_utils.errors import AppError, NotFoundError, UpstreamError
from app.app_utils.genai_client import (
    PIPELINE_MODEL,
    get_gemini_client,
    text_config,
)
from app.app_utils.logging_config import get_logger
from app.app_utils.storage import StorageHelper
from app.deps import enforce_ai_budget, require_admin_token
from app.schemas import (
    RemoveWatermarkRequest,
    ResolveKeepDeleteRequest,
    SplitVideoRequest,
    VideoDescribeRequest,
)
from app.services.media_store import (
    encrypt_file_bytes,
    get_global_vault_file_path,
    load_image_bytes_decrypted,
)
from app.services.transforms import (
    build_black_and_white,
    build_enhanced,
    build_kids,
    build_meme,
    build_sketch,
)
from app.services.video_ops import (
    build_active_boxes,
    detect_watermark_regions,
    encode_with_delogo,
    probe_and_sample,
)

logger = get_logger(__name__)

router = APIRouter()

# (key, display label, builder). Order defines response order; the request's
# `type` field selects one or "all".
TRANSFORM_BUILDERS = (
    ("bw", "Black & White", build_black_and_white),
    ("sketch", "Color Sketch", build_sketch),
    ("meme", "Meme Version", build_meme),
    ("kids", "Kids / Emojis", build_kids),
    ("enhanced", "Enhanced Corrected", build_enhanced),
)


@router.post("/api/video/split-frames", dependencies=[Depends(require_admin_token)])
def split_video_frames(req: SplitVideoRequest):
    try:
        import io
        import os

        import cv2
        from PIL import Image

        from agents.collector.tools.upload import process_and_save_upload

        safe_filename = os.path.basename(req.filename)
        session_storage = StorageHelper(session_id=req.session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        video_path = os.path.join(upload_dir, safe_filename)

        if not os.path.exists(video_path):
            raise HTTPException(status_code=404, detail="Video file not found")

        # Decrypt video to temp file so cv2.VideoCapture can read it
        temp_video_path = os.path.join(session_storage.local_base, f"temp_split_{safe_filename}")
        decrypted_bytes = load_image_bytes_decrypted(video_path, req.session_id)
        with open(temp_video_path, "wb") as f_temp:
            f_temp.write(decrypted_bytes)

        extracted_files = []
        try:
            cap = cv2.VideoCapture(temp_video_path)
            if not cap.isOpened():
                raise ValueError("Could not open video file for splitting")

            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.get(cv2.CAP_PROP_FPS)

            if total_frames <= 0:
                raise ValueError("Video has no readable frames")

            # Determine frame indexes to extract
            num_frames = max(1, min(req.num_frames, 50)) # Cap at 50 frames
            frame_indexes = [int(i * (total_frames - 1) / (num_frames - 1)) if num_frames > 1 else 0 for i in range(num_frames)]

            # Base name for extracted frames
            base_name, _ = os.path.splitext(safe_filename)
            # Find contributor ID if possible
            parts = base_name.split("_")
            parts[0] if len(parts) > 0 else "system"

            for idx, f_idx in enumerate(frame_indexes):
                cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
                success, frame = cap.read()
                if success:
                    # Convert BGR to RGB
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(frame_rgb)

                    frame_name = f"frame_{idx + 1}_{base_name}.jpg"

                    # Convert frame to raw JPEG bytes
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=90)
                    frame_bytes = buf.getvalue()

                    # Process and save using collector tool (which encrypts automatically)
                    info = process_and_save_upload(
                        file_bytes=frame_bytes,
                        original_filename=frame_name,
                        contributor_name="Organizer",
                        session_id=req.session_id
                    )
                    extracted_files.append(info["filename"])

            cap.release()
        finally:
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)

        return {
            "status": "success",
            "message": f"Successfully split video into {len(extracted_files)} frames!",
            "files": extracted_files
        }
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e

@router.post("/api/video/remove-watermark", dependencies=[Depends(require_admin_token), Depends(enforce_ai_budget)])
def remove_video_watermark(req: RemoveWatermarkRequest):
    try:

        import cv2

        safe_filename = os.path.basename(req.filename)
        session_storage = StorageHelper(session_id=req.session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        video_path = os.path.join(upload_dir, safe_filename)

        if not os.path.exists(video_path):
            raise HTTPException(status_code=404, detail="Video file not found")

        # Construct target filename for the processed video (forcing .mp4 extension for universal browser playback support)
        parts = safe_filename.split("_")
        if len(parts) >= 3:
            raw_processed = "_".join([*parts[:3], "processed", *parts[3:]])
            name_part, _ = os.path.splitext(raw_processed)
            processed_filename = f"{name_part}.mp4"
        else:
            name_part, _ = os.path.splitext(safe_filename)
            processed_filename = f"processed_{name_part}.mp4"

        processed_video_path = os.path.join(upload_dir, processed_filename)

        # Decrypt video to temp file
        temp_input_path = os.path.join(session_storage.local_base, f"temp_wm_in_{safe_filename}")
        temp_output_path = os.path.join(session_storage.local_base, f"temp_wm_out_{processed_filename}")

        decrypted_bytes = load_image_bytes_decrypted(video_path, req.session_id)
        with open(temp_input_path, "wb") as f_in:
            f_in.write(decrypted_bytes)

        try:
            width, height, fps, _frame_count, duration, sample_frames = probe_and_sample(
                temp_input_path
            )
            detected_logos, detected_captions, _classical = detect_watermark_regions(
                sample_frames, width, height, req
            )
            active_boxes = build_active_boxes(
                req, detected_logos, detected_captions, width, height, duration
            )
            encode_with_delogo(
                temp_input_path, temp_output_path, active_boxes, width, height, fps, req
            )

            # Read output bytes, encrypt, and write to the processed_video_path file
            with open(temp_output_path, "rb") as f_out:
                processed_bytes = f_out.read()

            encrypted_bytes = encrypt_file_bytes(processed_bytes, req.session_id)
            with open(processed_video_path, "wb") as f_vid:
                f_vid.write(encrypted_bytes)

            # Generate new video thumbnail for the processed video
            try:
                thumb_dir = os.path.join(session_storage.local_base, "thumbs")
                os.makedirs(thumb_dir, exist_ok=True)
                thumb_path = os.path.join(thumb_dir, processed_filename)

                cap_t = cv2.VideoCapture(temp_output_path)
                if cap_t.isOpened():
                    ret_t, frame_t = cap_t.read()
                    if ret_t:
                        h, w = frame_t.shape[:2]
                        new_h = 180
                        new_w = int(w * (new_h / h))
                        resized = cv2.resize(frame_t, (new_w, new_h))
                        _, buffer = cv2.imencode('.jpg', resized)
                        encrypted_thumb = encrypt_file_bytes(buffer.tobytes(), req.session_id)
                        with open(thumb_path, "wb") as f_thumb:
                            f_thumb.write(encrypted_thumb)
                    cap_t.release()
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "video", exc)

        finally:
            if os.path.exists(temp_input_path):
                os.remove(temp_input_path)
            if os.path.exists(temp_output_path):
                os.remove(temp_output_path)

        # Report both filenames and sizes so the caller can offer the
        # keep-both / delete-original / delete-processed choice. The response
        # previously carried only a message, which is why the keep-delete modal
        # in the UI had no way to identify the files it was acting on.
        def _size_mb(path):
            return round(os.path.getsize(path) / (1024 * 1024), 2) if os.path.exists(path) else 0.0

        return {
            "status": "success",
            "message": "Video processed successfully side-by-side!",
            "original_filename": safe_filename,
            "processed_filename": processed_filename,
            "original_size_mb": _size_mb(video_path),
            "processed_size_mb": _size_mb(processed_video_path),
        }
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e

@router.post("/api/video/resolve-keep-delete", dependencies=[Depends(require_admin_token)])
def resolve_keep_delete(req: ResolveKeepDeleteRequest):
    try:
        session_storage = StorageHelper(session_id=req.session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")

        orig_path = os.path.join(upload_dir, os.path.basename(req.original_filename))
        proc_path = os.path.join(upload_dir, os.path.basename(req.processed_filename))

        # Clean up thumbnails for both
        for fn in [req.original_filename, req.processed_filename]:
            thumb_path = os.path.join(session_storage.local_base, "thumbs", os.path.basename(fn))
            if os.path.exists(thumb_path):
                os.remove(thumb_path)

        if req.choice == "keep_both":
            return {"status": "success", "message": "Kept both versions successfully."}

        elif req.choice == "delete_original":
            orig_size = 0
            if os.path.exists(orig_path):
                orig_size = os.path.getsize(orig_path)
                os.remove(orig_path)
            if os.path.exists(proc_path):
                os.rename(proc_path, orig_path)

            # Track savings
            try:
                import json
                vault_file = get_global_vault_file_path()
                if os.path.exists(vault_file):
                    with open(vault_file) as f:
                        vault = json.load(f)
                    vault["savings_purged_bytes"] = vault.get("savings_purged_bytes", 0) + orig_size
                    with open(vault_file, "w") as f_out:
                        json.dump(vault, f_out, indent=4)
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "video", exc)

            return {"status": "success", "message": "Deleted original and kept enhanced version."}

        elif req.choice == "delete_processed":
            if os.path.exists(proc_path):
                os.remove(proc_path)
            return {"status": "success", "message": "Discarded processed version."}

        else:
            raise ValueError("Invalid choice option")

    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e

@router.post("/api/video/describe", dependencies=[Depends(require_admin_token), Depends(enforce_ai_budget)])
def describe_video(req: VideoDescribeRequest):
    try:
        session_storage = StorageHelper(session_id=req.session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        safe_filename = os.path.basename(req.filename)
        filepath = os.path.join(upload_dir, safe_filename)

        if not os.path.exists(filepath):
            raise NotFoundError("Video not found")

        vault_file = get_global_vault_file_path()
        vault = {}
        if os.path.exists(vault_file):
            try:
                with open(vault_file) as f:
                    vault = json.load(f)
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "video", exc)

        if "video_descriptions" not in vault:
            vault["video_descriptions"] = {}

        if "video_metadata" not in vault:
            vault["video_metadata"] = {}

        description = vault["video_descriptions"].get(safe_filename)
        metadata = vault["video_metadata"].get(safe_filename)

        need_description = not description
        need_metadata = not metadata

        if need_description or need_metadata:
            decrypted_bytes = load_image_bytes_decrypted(filepath, req.session_id)
            temp_path = os.path.join(upload_dir, f"temp_meta_{safe_filename}")
            with open(temp_path, "wb") as temp_f:
                temp_f.write(decrypted_bytes)

            if need_metadata:
                metadata = {
                    "creation_time": None,
                    "device": None,
                    "location": None
                }
                try:
                    import datetime
                    stat = os.stat(filepath)
                    metadata["creation_time"] = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
                except Exception as exc:
                    logger.warning("%s: best-effort step failed, continuing: %s", "video", exc)

                try:
                    import subprocess
                    ffprobe_cmd = [
                        "ffprobe", "-v", "quiet",
                        "-print_format", "json",
                        "-show_format", "-show_streams",
                        temp_path
                    ]
                    res = subprocess.run(ffprobe_cmd, capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        data = json.loads(res.stdout)
                        fmt = data.get("format", {})
                        tags = fmt.get("tags", {})

                        c_time = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
                        if c_time:
                            metadata["creation_time"] = c_time
                        loc = tags.get("location") or tags.get("com.apple.quicktime.location.ISO6709")
                        if loc:
                            metadata["location"] = loc
                        make = tags.get("com.apple.quicktime.make") or tags.get("make")
                        model = tags.get("com.apple.quicktime.model") or tags.get("model")
                        if make or model:
                            metadata["device"] = f"{make or ''} {model or ''}".strip()
                except Exception as ex:
                    logger.warning(f"ffprobe extraction failed: {ex}")
                vault["video_metadata"][safe_filename] = metadata

            if need_description:
                parts = safe_filename.split('_')
                disp = '_'.join(parts[2:]) if len(parts) >= 3 else safe_filename
                description = f"Trip footage video file showing scene from {disp.replace('.mp4', '').replace('.mov', '')}."

                api_key = os.getenv("GEMINI_API_KEY")
                if api_key:
                    try:

                        import cv2
                        from google.genai import types

                        cap = cv2.VideoCapture(temp_path)
                        if cap.isOpened():
                            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                            cap.get(cv2.CAP_PROP_FPS) or 30.0

                            # Sample up to 60 candidate frames across video duration
                            sample_count = min(60, max(10, total_frames))
                            indices = np.linspace(0, max(0, total_frames - 1), sample_count, dtype=int)

                            key_frames = []
                            prev_gray = None

                            for idx in indices:
                                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                                ret, frame = cap.read()
                                if not ret or frame is None:
                                    continue

                                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                                if prev_gray is None:
                                    key_frames.append(frame)
                                    prev_gray = gray
                                else:
                                    # Compute classical frame-difference delta
                                    diff = cv2.absdiff(gray, prev_gray)
                                    mean_diff = np.mean(diff)
                                    # Significant scene transition threshold (mean_diff > 12.0)
                                    if mean_diff > 12.0 or len(key_frames) < 1:
                                        key_frames.append(frame)
                                        prev_gray = gray

                                if len(key_frames) >= 8: # Cap keyframe batch at 8 frames
                                    break

                            cap.release()

                            if not key_frames and sample_count > 0:
                                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                ret, frame = cap.read()
                                if ret:
                                    key_frames = [frame]

                            if key_frames:
                                parts_content = []
                                for frame in key_frames[:8]:
                                    h, w = frame.shape[:2]
                                    if h > 512:
                                        ratio = 512.0 / h
                                        frame = cv2.resize(frame, (int(w * ratio), 512))
                                    _, buffer = cv2.imencode('.jpg', frame)
                                    parts_content.append(types.Part.from_bytes(data=buffer.tobytes(), mime_type="image/jpeg"))

                                prompt = "Describe what this video scene shows in a single brief, catchy sentence based on these key scene-change frames."
                                parts_content.append(prompt)

                                client = get_gemini_client()
                                response = client.models.generate_content(
                                    model=PIPELINE_MODEL, config=text_config(),
                                    contents=parts_content
                                )
                                track_ai_call("video_scene_describe", req.session_id, response=response)
                                if response.text:
                                    description = response.text.strip()
                    except Exception as ex:
                        logger.warning(f"Gemini video describe failed: {ex}")
                vault["video_descriptions"][safe_filename] = description

            if os.path.exists(temp_path):
                os.remove(temp_path)

            try:
                os.makedirs(os.path.dirname(vault_file), exist_ok=True)
                with open(vault_file, "w") as f_out:
                    json.dump(vault, f_out, indent=4)
            except Exception as ex:
                logger.warning(f"[describe_video] failed to persist vault {vault_file}: {ex}")
        return {
            "status": "success",
            "description": description,
            "creation_time": metadata.get("creation_time"),
            "device": metadata.get("device"),
            "location": metadata.get("location")
        }
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e
