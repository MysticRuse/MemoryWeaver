"""Photo editing: metadata, enhancement, transformations, renaming.

Split out of fast_api_app.py, which had grown to 6,474 lines with all 69
routes in one module.
"""

import hashlib
import io
import json
import os

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from PIL import Image

from app.app_utils.errors import AppError, NotFoundError, UpstreamError
from app.app_utils.genai_client import (
    IMAGE_MODEL,
    PIPELINE_MODEL,
    get_gemini_client,
    text_config,
)
from app.app_utils.logging_config import get_logger
from app.app_utils.storage import StorageHelper
from app.deps import enforce_ai_budget, require_admin_token
from app.schemas import (
    MagicEnhanceRequest,
    NanoSuggestionsRequest,
    RenamePhotoRequest,
    SaveTransformRequest,
    SelectPhotoVersionRequest,
    TransformPhotoRequest,
)
from app.services.media_store import (
    apply_enhancements,
    draw_kids_stickers_and_banner,
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


# Runs a Gemini Vision critique when the analysis is not cached, so it is a
# billable call and belongs behind the spend cap like every other AI route.
@router.get(
    "/api/photo-metadata",
    dependencies=[Depends(require_admin_token), Depends(enforce_ai_budget)],
)
def get_photo_metadata(filename: str, session_id: str = "default", force_refresh: bool = False):
    """Returns detailed EXIF and file metadata for a specific uploaded photo."""
    try:
        session_storage = StorageHelper(session_id=session_id)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        full_path = os.path.join(upload_dir, filename)

        if not os.path.exists(full_path):
            raise NotFoundError("Photo not found")

        # 1. Run EXIF extraction.
        #
        # Read through the decryption helper, not straight off disk. Stored
        # uploads are encrypted at rest, so passing `full_path` handed PIL
        # ciphertext: extract_exif swallowed the "cannot identify image file"
        # error and returned all-defaults, which is why this panel reported
        # "Unknown Device" with no date, GPS, lens or ISO for every
        # web-uploaded photo. extract_exif calls Image.open, which accepts a
        # file object, so a BytesIO of the plaintext is enough.
        from agents.collector.tools.upload import extract_exif
        decrypted_for_exif = load_image_bytes_decrypted(full_path, session_id)
        metadata = extract_exif(io.BytesIO(decrypted_for_exif)) if decrypted_for_exif \
            else extract_exif(full_path)

        # 2. Get file details
        from PIL import Image
        width, height = 0, 0
        img_format = "Unknown"
        try:
            decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
            with Image.open(io.BytesIO(decrypted_bytes)) as img:
                width, height = img.size
                img_format = img.format
        except Exception as exc:
            logger.warning("%s: best-effort step failed, continuing: %s", "photo", exc)

        file_size_kb = round(os.path.getsize(full_path) / 1024, 1)

        # 3. Resolve location name via GPS if exists
        location_desc = "Unknown Location"
        lat = metadata["gps"]["latitude"]
        lon = metadata["gps"]["longitude"]
        if lat is not None and lon is not None:
            location_desc = f"{lat:.4f}° N, {lon:.4f}° W"

        # 4. Load caption/transcription metadata if exists
        transcription = ""
        voice_note = None
        voice_duration = 0.0
        gemini_analysis = None
        is_enhanced = False
        try:
            import json
            session_dir = os.path.join(session_storage.local_base, "sessions", session_id)
            metadata_file = os.path.join(session_dir, "photos_metadata.json")
            if os.path.exists(metadata_file):
                with open(metadata_file) as f:
                    photos_meta = json.load(f)
                    if filename in photos_meta:
                        transcription = photos_meta[filename].get("transcription", "")
                        voice_note = photos_meta[filename].get("voice_note")
                        voice_duration = photos_meta[filename].get("voice_duration", 0.0)
                        gemini_analysis = photos_meta[filename].get("gemini_analysis")
                        is_enhanced = photos_meta[filename].get("is_enhanced", False)
        except Exception as exc:
            logger.warning("%s: best-effort step failed, continuing: %s", "photo", exc)

        # Run dynamic Gemini Vision analysis if not cached or force_refresh is True
        if not gemini_analysis or force_refresh:
            api_key = os.getenv("GEMINI_API_KEY")
            if api_key:
                try:
                    from PIL import Image
                    client = get_gemini_client()
                    decrypted_bytes = load_image_bytes_decrypted(full_path, session_id)
                    with Image.open(io.BytesIO(decrypted_bytes)) as img:
                        if img.mode not in ('RGB', 'RGBA'):
                            img = img.convert('RGB')

                        prompt = (
                            "You are a professional photographer reviewing a user's travel photo. "
                            "Analyze this photo and provide your feedback in JSON format. Keep the feedback concise, short and punchy. Use these exact keys:\n"
                            "{\n"
                            "  \"summary\": \"Brief description of what is seen in the photo (max 2 sentences)\",\n"
                            "  \"rating\": \"A rating out of 10 (e.g. 8.5/10)\",\n"
                            "  \"critique\": \"A short, encouraging critique highlighting composition/mood (max 3 sentences)\",\n"
                            "  \"tips\": [\n"
                            "    \"Short actionable pro tip 1\",\n"
                            "    \"Short actionable pro tip 2\"\n"
                            "  ]\n"
                            "}"
                        )
                        response = client.models.generate_content(
                            model=PIPELINE_MODEL, config=text_config(),
                            contents=[img, prompt]
                        )
                        text = response.text
                        if "```json" in text:
                            text = text.split("```json")[1].split("```")[0].strip()
                        elif "```" in text:
                            text = text.split("```")[1].split("```")[0].strip()
                        gemini_analysis = json.loads(text.strip())

                        # Cache the analysis result
                        try:
                            os.makedirs(session_dir, exist_ok=True)
                            photos_meta = {}
                            if os.path.exists(metadata_file):
                                with open(metadata_file) as f:
                                    photos_meta = json.load(f)
                            if filename not in photos_meta:
                                photos_meta[filename] = {}
                            photos_meta[filename]["gemini_analysis"] = gemini_analysis
                            with open(metadata_file, "w") as f:
                                json.dump(photos_meta, f, indent=4)
                        except Exception as exc:
                            logger.warning("%s: best-effort step failed, continuing: %s", "photo", exc)
                except Exception as e:
                    logger.warning(f"Gemini Vision analysis failed: {e}")
                    gemini_analysis = {
                        "summary": "Could not complete visual summary.",
                        "rating": "N/A",
                        "critique": f"Analysis failed: {e!s}",
                        "tips": []
                    }
            else:
                gemini_analysis = {
                    "summary": "Gemini API key is not set.",
                    "rating": "N/A",
                    "critique": "Please set GEMINI_API_KEY environment variable to enable automatic visual ratings.",
                    "tips": []
                }

        return {
            "status": "success",
            "filename": filename,
            "size_kb": file_size_kb,
            "dimensions": f"{width} × {height} px",  # noqa: RUF001 - U+00D7 is intentional typography
            "format": img_format,
            "device": metadata.get("device") or "Unknown Device",
            "timestamp": metadata.get("timestamp") or "Unknown Time",
            "gps": {
                "latitude": lat,
                "longitude": lon
            },
            "location": location_desc,
            "altitude": metadata.get("altitude"),
            "heading": metadata.get("heading"),
            "aperture": metadata.get("aperture"),
            "shutter_speed": metadata.get("shutter_speed"),
            "iso": metadata.get("iso"),
            "focal_length": metadata.get("focal_length"),
            "lens": metadata.get("lens"),
            "software": metadata.get("software"),
            "color_space": metadata.get("color_space"),
            "transcription": transcription,
            "voice_note": voice_note,
            "voice_duration": voice_duration,
            "gemini_analysis": gemini_analysis,
            "is_enhanced": is_enhanced
        }
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e


@router.post("/api/magic-enhance", dependencies=[Depends(require_admin_token), Depends(enforce_ai_budget)])
def run_magic_enhance(req: MagicEnhanceRequest):
    """Dynamically analyzes and applies professional editing enhancements to a photo using Gemini and Pillow."""
    try:
        import json
        session_storage = StorageHelper(session_id=req.session_id)
        safe_filename = os.path.basename(req.filename)
        upload_dir = os.path.join(session_storage.local_base, "uploads")
        original_path = os.path.join(upload_dir, safe_filename)

        if not os.path.exists(original_path):
            raise HTTPException(status_code=404, detail="Original photo not found")

        # Define enhanced output filename
        base, _ext = os.path.splitext(safe_filename)
        enhanced_filename = f"{base}_enhanced.jpg"
        enhanced_path = os.path.join(upload_dir, enhanced_filename)

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return {"status": "error", "message": "Gemini API key is not set."}

        from PIL import Image as PILImage
        client = get_gemini_client()

        decrypted_bytes = load_image_bytes_decrypted(original_path, req.session_id)
        with PILImage.open(io.BytesIO(decrypted_bytes)) as img:
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')

            prompt = (
                "You are an expert digital photo editor. Analyze this photo and suggest the optimal "
                "Lightroom-style adjustment factors to make it look visually stunning, vibrant, and balanced. "
                "Respond in JSON format with these exact keys:\n"
                "{\n"
                "  \"brightness\": 1.0, # factor between 0.75 and 1.25\n"
                "  \"contrast\": 1.0, # factor between 0.8 and 1.25\n"
                "  \"saturation\": 1.0, # factor between 0.7 and 1.3\n"
                "  \"sharpness\": 1.0, # factor between 0.9 and 1.4\n"
                "  \"warmth\": 1.0, # factor between 0.9 and 1.1\n"
                "  \"enhanced_rating\": \"A rating out of 10 for the enhanced photo (e.g. 9.2/10)\",\n"
                "  \"explanation\": \"A short description of why these edits were made (e.g. 'Boosted shadows to recover details and added warmth for a golden hour look')\"\n"
                "}"
            )
            response = client.models.generate_content(
                model=PIPELINE_MODEL, config=text_config(),
                contents=[img, prompt]
            )
            text = response.text
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            try:
                factors = json.loads(text.strip())
            except (ValueError, TypeError) as parse_err:
                # A malformed tuning response used to abort the whole enhance.
                # Neutral factors still produce a usable image (and the sketch
                # builder already degrades this way), so fall back rather than
                # failing the user's edit.
                logger.info(f"Magic enhance tuning unparseable, using neutral factors: {parse_err}")
                factors = {}

            brightness = float(factors.get("brightness", 1.0))
            contrast = float(factors.get("contrast", 1.0))
            saturation = float(factors.get("saturation", 1.0))
            sharpness = float(factors.get("sharpness", 1.0))
            warmth = float(factors.get("warmth", 1.0))
            enhanced_rating = str(factors.get("enhanced_rating", "9.0/10"))
            explanation = factors.get("explanation", "Photo enhanced successfully.")

            # Apply dynamic enhancements to the image copy
            apply_enhancements(original_path, enhanced_path, brightness, contrast, saturation, sharpness, warmth, req.session_id)

            return {
                "status": "success",
                "original_filename": safe_filename,
                "enhanced_filename": enhanced_filename,
                "explanation": explanation,
                "enhanced_rating": enhanced_rating,
                "factors": {
                    "brightness": brightness,
                    "contrast": contrast,
                    "saturation": saturation,
                    "sharpness": sharpness,
                    "warmth": warmth
                }
            }
    except Exception as e:
        return {"status": "error", "message": f"Magic enhance failed: {e!s}"}

@router.post("/api/select-photo-version", dependencies=[Depends(require_admin_token)])
def select_photo_version(req: SelectPhotoVersionRequest):
    """Saves the selected version (original vs enhanced) as the primary photo."""
    try:
        import json
        import shutil
        session_storage = StorageHelper(session_id=req.session_id)
        safe_filename = os.path.basename(req.filename)
        upload_dir = os.path.join(session_storage.local_base, "uploads")

        original_path = os.path.join(upload_dir, safe_filename)
        backup_filename = f"{os.path.splitext(safe_filename)[0]}_original.jpg"
        backup_path = os.path.join(upload_dir, backup_filename)
        enhanced_filename = f"{os.path.splitext(safe_filename)[0]}_enhanced.jpg"
        enhanced_path = os.path.join(upload_dir, enhanced_filename)

        is_enhanced_flag = False
        if req.version == "enhanced":
            if not os.path.exists(enhanced_path):
                raise HTTPException(status_code=404, detail="Enhanced version not found. Run enhance first.")
            # Backup original if not already backed up
            if not os.path.exists(backup_path):
                shutil.copy2(original_path, backup_path)
            # Copy enhanced over original
            shutil.copy2(enhanced_path, original_path)
            is_enhanced_flag = True
        else:
            # Restore original if backup exists
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, original_path)

        # Save choice to photos_metadata.json
        try:
            session_dir = os.path.join(session_storage.local_base, "sessions", req.session_id)
            os.makedirs(session_dir, exist_ok=True)
            metadata_file = os.path.join(session_dir, "photos_metadata.json")
            photos_meta = {}
            if os.path.exists(metadata_file):
                with open(metadata_file) as f:
                    photos_meta = json.load(f)
            if safe_filename not in photos_meta:
                photos_meta[safe_filename] = {}
            photos_meta[safe_filename]["is_enhanced"] = is_enhanced_flag
            with open(metadata_file, "w") as f:
                json.dump(photos_meta, f, indent=4)
        except Exception as err:
            logger.warning(f"Error caching select_photo_version metadata: {err}")
        return {"status": "success", "message": f"Successfully switched to {req.version} version."}
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e

@router.post("/api/photo/transformations", dependencies=[Depends(require_admin_token), Depends(enforce_ai_budget)])
def get_photo_transformations(req: TransformPhotoRequest):
    """Renders the requested stylistic variants of one photo.

    Was a single 625-line function containing five inlined image pipelines.
    The variants now live in app/services/transforms.py; this handler resolves
    the source image and assembles the response.
    """
    safe_filename = os.path.basename(req.filename)
    session_storage = StorageHelper(session_id=req.session_id)
    original_path = os.path.join(session_storage.local_base, "uploads", safe_filename)
    if not os.path.exists(original_path):
        raise NotFoundError("Original photo not found.")

    decrypted_bytes = load_image_bytes_decrypted(original_path, req.session_id)
    if not decrypted_bytes:
        raise UpstreamError("Failed to decrypt image.")

    temp_dir = os.path.join(session_storage.local_base, "transform_temp")
    os.makedirs(temp_dir, exist_ok=True)
    file_id = hashlib.md5(safe_filename.encode()).hexdigest()[:8]

    try:
        orig_img = Image.open(io.BytesIO(decrypted_bytes)).convert("RGB")
    except Exception as e:
        raise UpstreamError(f"Failed to open image: {e!s}") from e

    def _url(filename: str) -> str:
        return f"/media?filename={filename}&session_id={req.session_id}"

    transformations = {}
    for key, label, build in TRANSFORM_BUILDERS:
        if req.type not in ("all", key):
            continue
        result = build(orig_img, temp_dir, file_id, req)
        if isinstance(result, dict):
            filename = result.pop("filename")
            transformations[key] = {"name": label, "filename": filename,
                                    "url": _url(filename), **result}
        else:
            transformations[key] = {"name": label, "filename": result,
                                    "url": _url(result)}

    return {"status": "success", "transformations": transformations}

@router.post("/api/photo/rename", dependencies=[Depends(require_admin_token)])
def rename_photo(req: RenamePhotoRequest):
    debug_log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rename_debug.log")
    with open(debug_log_path, "a") as f:
        f.write(f"\n--- [LOG START] {req.old_filename} -> {req.new_filename} (session: {req.session_id}) ---\n")
    try:
        session_storage = StorageHelper(session_id=req.session_id)
        safe_old = os.path.basename(req.old_filename)
        safe_new_input = os.path.basename(req.new_filename)

        # Extract backend session/hash prefix from safe_old if present
        parts = safe_old.split('_')
        prefix = "_".join(parts[:2]) + "_" if len(parts) > 2 else ""

        # Ensure the target name is clean (does not contain the prefix itself)
        safe_new_clean = safe_new_input
        if prefix and safe_new_clean.startswith(prefix):
            safe_new_clean = safe_new_clean[len(prefix):]

        safe_new = prefix + safe_new_clean

        if not safe_new or safe_old == safe_new:
            with open(debug_log_path, "a") as f:
                f.write("No change in filename. Success.\n")
            return {"status": "success", "new_filename": safe_old}

        old_path = os.path.join(session_storage.local_base, "uploads", safe_old)
        new_path = os.path.join(session_storage.local_base, "uploads", safe_new)

        with open(debug_log_path, "a") as f:
            f.write(f"Checking paths: old={old_path} (exists: {os.path.exists(old_path)}), new={new_path} (exists: {os.path.exists(new_path)})\n")

        if not os.path.exists(old_path):
            raise HTTPException(status_code=404, detail="Photo not found")

        if os.path.exists(new_path):
            raise HTTPException(status_code=400, detail="A file with the new name already exists")

        # Rename the file on disk (backend uploads copy)
        os.rename(old_path, new_path)

        # Also rename on local device/drive target folder and update sync_registry.json if configured
        try:
            from app.sync_manager import SyncManager
            sync_manager = SyncManager(session_id=req.session_id)
            registry = sync_manager.load_registry()
            if registry and "files" in registry:
                files_dict = registry["files"]
                updated_registry = False
                for rel_path, item in list(files_dict.items()):
                    if item.get("upload_filename") == safe_old:
                        target_folder = sync_manager.get_config().get("target_folder")
                        if target_folder and os.path.exists(target_folder):
                            old_device_path = os.path.join(target_folder, rel_path)
                            if os.path.exists(old_device_path):
                                # Determine new relative path using the clean filename (without prefix)
                                dir_name = os.path.dirname(rel_path)
                                new_rel_path = os.path.join(dir_name, safe_new_clean) if dir_name else safe_new_clean
                                new_device_path = os.path.join(target_folder, new_rel_path)
                                try:
                                    os.rename(old_device_path, new_device_path)
                                    item["upload_filename"] = safe_new
                                    files_dict[new_rel_path] = files_dict.pop(rel_path)
                                    updated_registry = True
                                except Exception as dev_err:
                                    logger.warning(f"Failed to rename device/drive file {old_device_path} to {new_device_path}: {dev_err}")
                if updated_registry:
                    sync_manager.save_registry(registry)
        except Exception as sync_err:
            logger.warning(f"Failed to process sync manager rename update: {sync_err}")
        # Also rename in classifications.json if it exists
        classifications_file = os.path.join(session_storage.local_base, "classifications.json")
        if os.path.exists(classifications_file):
            try:
                with open(classifications_file) as f:
                    data = json.load(f)
                if safe_old in data:
                    data[safe_new] = data.pop(safe_old)
                    with open(classifications_file, "w") as f:
                        json.dump(data, f, indent=4)
            except Exception as e:
                logger.warning(f"Failed to update classifications.json: {e}")
        # Also rename in global cleaner_vault.json
        try:
            vault_file = get_global_vault_file_path()
            if os.path.exists(vault_file):
                with open(vault_file) as f:
                    vault_data = json.load(f)
                updated_vault = False

                # Rename key in classifications
                if "classifications" in vault_data and safe_old in vault_data["classifications"]:
                    vault_data["classifications"][safe_new] = vault_data["classifications"].pop(safe_old)
                    updated_vault = True

                # Rename key in locked_photos
                if "locked_photos" in vault_data and safe_old in vault_data["locked_photos"]:
                    vault_data["locked_photos"][safe_new] = vault_data["locked_photos"].pop(safe_old)
                    updated_vault = True

                # Rename key in notes
                if "notes" in vault_data and safe_old in vault_data["notes"]:
                    vault_data["notes"][safe_new] = vault_data["notes"].pop(safe_old)
                    updated_vault = True

                if updated_vault:
                    with open(vault_file, "w") as f:
                        json.dump(vault_data, f, indent=4)
        except Exception as e:
            logger.warning(f"Failed to update cleaner_vault.json: {e}")
        # Also rename in manifest.json if it exists
        manifest_file = os.path.join(session_storage.local_base, "artefacts", "manifest.json")
        if os.path.exists(manifest_file):
            try:
                with open(manifest_file) as f:
                    manifest_data = json.load(f)
                if isinstance(manifest_data, list):
                    for item in manifest_data:
                        if item.get("filename") == safe_old:
                            item["filename"] = safe_new
                    with open(manifest_file, "w") as f:
                        json.dump(manifest_data, f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to update manifest.json: {e}")
                logger.warning(f"Failed to update manifest.json: {e}")
        return {"status": "success", "new_filename": safe_new}
    except Exception as e:
        import traceback
        with open(debug_log_path, "a") as f:
            f.write(f"EXCEPTION: {e!s}\n{traceback.format_exc()}\n")
        return {"status": "error", "message": str(e)}

@router.post("/api/photo/nano-suggestions", dependencies=[Depends(require_admin_token), Depends(enforce_ai_budget)])
def get_nano_suggestions(req: NanoSuggestionsRequest):
    import io
    import json
    import os

    try:
        safe_filename = os.path.basename(req.filename)
        session_storage = StorageHelper(session_id=req.session_id)
        original_path = os.path.join(session_storage.local_base, "uploads", safe_filename)

        decrypted_bytes = load_image_bytes_decrypted(original_path, req.session_id)
        api_key = os.getenv("GEMINI_API_KEY")

        if api_key and decrypted_bytes:
            try:
                client = get_gemini_client()
                img = Image.open(io.BytesIO(decrypted_bytes)).convert("RGB")

                prompt = (
                    f"Analyze this photo. Imagine a kid or meme creator is decorating/editing this photo ({req.type} theme).\n"
                    "1. Suggest exactly 10 funny, creative, short caption ideas tailored specifically to what is happening in the photo.\n"
                    "2. Suggest exactly 10 context-relevant stickers/emojis matching the subjects or context (e.g. 👑, 🎈, 🌟, 🍕, 🥳, 💤, 🐾, 💖, 🚀, 🍦).\n"
                    "Output EXACTLY a JSON object with this schema and nothing else:\n"
                    "{\n"
                    "  \"suggested_captions\": [\"Caption 1\", \"Caption 2\", \"Caption 3\", \"Caption 4\", \"Caption 5\", \"Caption 6\", \"Caption 7\", \"Caption 8\", \"Caption 9\", \"Caption 10\"],\n"
                    "  \"suggested_stickers\": [\n"
                    "    {\"type\": \"👑\", \"label\": \"Crown\"},\n"
                    "    {\"type\": \"💤\", \"label\": \"Sleeping Zzz\"}\n"
                    "  ]\n"
                    "}"
                )

                response = client.models.generate_content(
                    model=PIPELINE_MODEL, config=text_config(),
                    contents=[img, prompt]
                )

                text = response.text.strip()
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                data = json.loads(text)

                # Limit to 10 choices each to guarantee constraints
                if "suggested_captions" in data:
                    data["suggested_captions"] = data["suggested_captions"][:10]
                if "suggested_stickers" in data:
                    data["suggested_stickers"] = data["suggested_stickers"][:10]
                return {"status": "success", "suggestions": data}
            except Exception as ex:
                logger.warning(f"Nano suggestions API call failed: {ex}")
        fallback_stickers = [
            {"type": "👑", "label": "Crown"},
            {"type": "🌟", "label": "Star"},
            {"type": "🎈", "label": "Balloon"},
            {"type": "🥳", "label": "Party"},
            {"type": "💤", "label": "Zzz"},
            {"type": "💖", "label": "Heart"},
            {"type": "🐾", "label": "Paw"},
            {"type": "🚀", "label": "Rocket"},
            {"type": "🍕", "label": "Pizza"},
            {"type": "🍦", "label": "Ice Cream"}
        ][:10]
        fallback_captions = (
            [
                "Best memories forever! ✨",
                "Living the good life! 🎉",
                "Too cute to handle! 💖",
                "Sunkissed moments ☀️",
                "Always smiling 😊",
                "My favorite human 🐾",
                "Joy in every step 🌈",
                "Unconditional love 💕",
                "Exploring the world 🌍",
                "Pure happiness ⭐️"
            ] if req.type == "kids" else [
                "Me on a Friday night at 8 PM 😴",
                "Expectation vs. Reality 😭",
                "When you realize it's Monday 🥑",
                "I have no idea what I am doing 🙃",
                "Can we pretend this didn't happen? 🙈",
                "Level of tiredness: 100 🔋",
                "Did someone say food? 🍕",
                "Plot twist: I'm still tired 🥱",
                "Current mood: Do not disturb 🚫",
                "Story of my life 📖"
            ]
        )[:10]

        return {
            "status": "success",
            "suggestions": {
                "suggested_captions": fallback_captions,
                "suggested_stickers": fallback_stickers
            }
        }
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e

@router.post("/api/photo/save-transformation", dependencies=[Depends(require_admin_token)])
def save_photo_transformation(req: SaveTransformRequest):
    import io

    from app.sync_manager import SyncManager

    try:
        session_storage = StorageHelper(session_id=req.session_id)
        orig_basename = os.path.basename(req.original_filename)
        original_path = os.path.join(session_storage.local_base, "uploads", orig_basename)

        re_drawn = False
        img_bytes = None

        if req.transformation_type in ["kids", "meme"]:
            if not req.is_edited:
                # If NOT edited, directly copy the exact high-quality generated preview bytes!
                temp_path = os.path.join(session_storage.local_base, "transform_temp", os.path.basename(req.temp_filename))
                if os.path.exists(temp_path):
                    try:
                        with open(temp_path, "rb") as f_temp:
                            img_bytes = f_temp.read()
                        re_drawn = True
                        logger.info(f"Directly copied exact generated {req.transformation_type} preview bytes on save!")
                    except Exception as copy_err:
                        logger.warning(f"Failed to copy preview bytes: {copy_err}")
            if not re_drawn and os.path.exists(original_path):
                # If edited, attempt high-quality drawing via Nano Banana 2
                try:
                    decrypted_bytes = load_image_bytes_decrypted(original_path, req.session_id)
                    if decrypted_bytes:
                        orig_img = Image.open(io.BytesIO(decrypted_bytes)).convert("RGB")
                        w, h = orig_img.size
                        scale = max(w, h) / 800.0

                        custom_caption = req.custom_caption if req.custom_caption is not None else "Hold my paw and let's go on an adventure"
                        stickers = req.stickers if req.stickers is not None else []
                        cx = req.caption_x if req.caption_x is not None else 0.5
                        cy = min(0.72, req.caption_y if req.caption_y is not None else 0.70)
                        cc = req.caption_color if req.caption_color is not None else [255, 105, 180]

                        api_key = os.getenv("GEMINI_API_KEY")
                        nano_success = False
                        if api_key:
                            try:
                                client = get_gemini_client()

                                sticker_insts = []
                                for s in stickers:
                                    stype = s.get("type", "🌟")
                                    sx = s.get("x", 0.5)
                                    sy = s.get("y", 0.5)
                                    ss = s.get("size", 1.0)
                                    sticker_insts.append(f"Place a '{stype}' sticker (size multiplier {ss}) at normalized coordinates (x={sx}, y={sy}) on the photo.")

                                edit_prompt = (
                                    f"Edit the uploaded photo in the style of Apple iOS sticker and emoji design.\n"
                                    f"1. Create a custom text sticker for the caption: \"{custom_caption}\" near the bottom center at coordinates (x={cx}, y={cy}).\n"
                                    f"Caption Text Sticker Style Requirements:\n"
                                    f"- Bold, rounded, chunky 3D bubble-letter typography\n"
                                    f"- Glossy plastic/candy-like finish, soft drop shadow, subtle rim light\n"
                                    f"- Saturated gradient fill per letter\n"
                                    f"- Slight perspective tilt for playfulness, iMessage sticker aesthetic\n"
                                    f"- Clean white sticker outline (die-cut border) around the entire text sticker block\n"
                                    f"2. Paint the stickers/emojis at the exact requested coordinates:\n"
                                    f"{chr(10).join(sticker_insts)}\n"
                                    f"Stickers/Emojis Style Requirements:\n"
                                    f"- Style of Apple iOS emoji design\n"
                                    f"- Squircle/superellipse rounding on any container shapes, no sharp corners\n"
                                    f"- Soft single-direction top-left lighting with subtle gradient shading\n"
                                    f"- Glossy highlight on the upper surface, soft ambient shadow underneath\n"
                                    f"- Saturated but harmonious color palette\n"
                                    f"- Slight 3D depth (subtle bevel/glass effect), not fully flat, not photorealistic\n"
                                    f"- Rounded, friendly proportions\n"
                                    f"- Clean vector-style edges, smooth anti-aliasing, no noise or texture grain\n"
                                    f"- Isolated object with a clean white sticker outline (die-cut border) around each sticker\n"
                                    f"Make the final photo look beautifully decorated with these Apple-style overlays blending naturally onto the image."
                                )

                                response = client.models.generate_content(
                                    model=IMAGE_MODEL,
                                    contents=[orig_img, edit_prompt]
                                )

                                temp_bytes = None
                                for part in response.candidates[0].content.parts:
                                    if hasattr(part, 'inline_data') and part.inline_data:
                                        temp_bytes = part.inline_data.data
                                        break
                                if temp_bytes:
                                    img_bytes = temp_bytes
                                    re_drawn = True
                                    nano_success = True
                                    logger.info(f"Successfully re-drawn custom {req.transformation_type} overlays using Nano Banana 2 on save!")
                            except Exception as nex:
                                logger.warning(f"Nano Banana 2 custom overlay re-draw failed: {nex}. Falling back to PIL.")
                        if not nano_success:
                            draw_kids_stickers_and_banner(orig_img, custom_caption, stickers, scale, cx, cy, cc)
                            out_buf = io.BytesIO()
                            orig_img.save(out_buf, format="PNG")
                            img_bytes = out_buf.getvalue()
                            re_drawn = True
                except Exception as rde:
                    logger.warning(f"Failed to re-draw transformation on save: {rde}")
        if not re_drawn:
            temp_path = os.path.join(session_storage.local_base, "transform_temp", os.path.basename(req.temp_filename))
            if not os.path.exists(temp_path):
                raise HTTPException(status_code=404, detail="Temporary transformation file not found")

            with open(temp_path, "rb") as f:
                img_bytes = f.read()

        import hashlib

        # Determine clean original basename without session & hash prefixes
        parts = orig_basename.split('_')
        if len(parts) > 2:
            clean_orig_name = "_".join(parts[2:])
        else:
            clean_orig_name = orig_basename

        clean_orig_base, _clean_ext = os.path.splitext(clean_orig_name)
        new_local_filename = f"{clean_orig_base}_{req.transformation_type}.png"

        # Load sync manager config to find target local folder
        manager = SyncManager(session_id=req.session_id)
        config = manager.get_config()
        target_folder = config.get("target_folder")

        rel_dir = ""
        if target_folder and os.path.exists(target_folder):
            # Locate relative folder of the original photo in the registry
            registry = manager.load_registry()
            for rel_path, item in registry.get("files", {}).items():
                if item.get("upload_filename") == os.path.basename(req.original_filename):
                    rel_dir = os.path.dirname(rel_path)
                    break

        new_rel_path = os.path.join(rel_dir, new_local_filename)
        new_hash = hashlib.md5(new_rel_path.encode('utf-8')).hexdigest()[:8]
        new_upload_filename = f"{req.session_id}_{new_hash}_{new_local_filename}"

        if target_folder and os.path.exists(target_folder):
            local_dest_path = os.path.join(target_folder, new_rel_path)
            try:
                os.makedirs(os.path.dirname(local_dest_path), exist_ok=True)
                with open(local_dest_path, "wb") as f_local:
                    f_local.write(img_bytes)
            except Exception as le:
                logger.warning(f"Failed to write to local folder: {le}")
        # Also write directly to session uploads directory encrypted
        encrypted_bytes = encrypt_file_bytes(img_bytes, req.session_id)
        uploads_path = os.path.join(session_storage.local_base, "uploads", new_upload_filename)
        with open(uploads_path, "wb") as f_out:
            f_out.write(encrypted_bytes)

        # Create thumbnail
        try:
            with Image.open(io.BytesIO(img_bytes)) as im:
                im.thumbnail((300, 300), Image.Resampling.LANCZOS)
                thumb_buf = io.BytesIO()
                im.save(thumb_buf, format="JPEG", quality=75)
                encrypted_thumb_bytes = encrypt_file_bytes(thumb_buf.getvalue(), req.session_id)
                thumb_path = os.path.join(session_storage.local_base, "thumbs", new_upload_filename)
                os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
                with open(thumb_path, "wb") as f_thumb:
                    f_thumb.write(encrypted_thumb_bytes)
        except Exception as e:
            logger.warning(f"Failed to generate thumbnail for transformation save: {e}")
        # Trigger sync scan registry update to recognize this new local file
        try:
            manager.scan_folder()
        except Exception as se:
            logger.warning(f"Post-save sync scan failed: {se}")
        # Register new item in the classifications vault
        try:
            vault_file = get_global_vault_file_path()
            if os.path.exists(vault_file):
                with open(vault_file) as f:
                    vault = json.load(f)

                orig_class = vault.get("classifications", {}).get(os.path.basename(req.original_filename))
                if orig_class:
                    reason_suffix = {
                        "bw": " (Black & White Edition)",
                        "sketch": " (Sketch Edition)",
                        "meme": " (Meme Edition)",
                        "kids": " (Kids Edition)",
                        "enhanced": " (Enhanced Edition)"
                    }.get(req.transformation_type, "")

                    vault["classifications"][new_upload_filename] = {
                        "category": orig_class.get("category", "photo"),
                        "subcategory": orig_class.get("subcategory", "Transformed"),
                        "extracted_text": orig_class.get("extracted_text", ""),
                        "reason": orig_class.get("reason", "A beautiful memory") + reason_suffix,
                        "confidence": orig_class.get("confidence", 8)
                    }

                    with open(vault_file, "w") as f_out:
                        json.dump(vault, f_out, indent=4)
        except Exception as ev:
            logger.warning(f"Failed to copy classifications: {ev}")
        # Update local classifications.json if it exists
        classifications_file = os.path.join(session_storage.local_base, "classifications.json")
        if os.path.exists(classifications_file):
            try:
                with open(classifications_file) as f:
                    class_data = json.load(f)
                orig_class = class_data.get(os.path.basename(req.original_filename))
                if orig_class:
                    reason_suffix = {
                        "bw": " (Black & White Edition)",
                        "sketch": " (Sketch Edition)",
                        "meme": " (Meme Edition)",
                        "kids": " (Kids Edition)",
                        "enhanced": " (Enhanced Edition)"
                    }.get(req.transformation_type, "")

                    class_data[new_upload_filename] = {
                        "category": orig_class.get("category", "photo"),
                        "subcategory": orig_class.get("subcategory", "Transformed"),
                        "extracted_text": orig_class.get("extracted_text", ""),
                        "reason": orig_class.get("reason", "A beautiful memory") + reason_suffix,
                        "confidence": orig_class.get("confidence", 8)
                    }
                    with open(classifications_file, "w") as f:
                        json.dump(class_data, f, indent=4)
            except Exception as e:
                logger.warning(f"Failed to update local classifications.json: {e}")
        # Update local manifest.json if it exists
        manifest_file = os.path.join(session_storage.local_base, "artefacts", "manifest.json")
        try:
            manifest_data = []
            if os.path.exists(manifest_file):
                with open(manifest_file) as f:
                    manifest_data = json.load(f)
                if not isinstance(manifest_data, list):
                    manifest_data = []

            target_caption = req.custom_caption if req.custom_caption else "KIDS ZONE: Playtime!"
            found = False
            for item in manifest_data:
                if item.get("filename") == new_upload_filename:
                    item["caption"] = target_caption
                    found = True
                    break
            if not found:
                manifest_data.append({
                    "filename": new_upload_filename,
                    "caption": target_caption
                })

            # Clean up old original name if present
            manifest_data = [item for item in manifest_data if item.get("filename") != os.path.basename(req.original_filename)]

            os.makedirs(os.path.dirname(manifest_file), exist_ok=True)
            with open(manifest_file, "w") as f:
                json.dump(manifest_data, f, indent=2)
            logger.info("Successfully updated local manifest.json with", new_upload_filename)
        except Exception as e:
            logger.warning(f"Failed to update local manifest.json: {e}")
        return {"status": "success", "filename": new_upload_filename}
    except AppError:
        raise
    except Exception as e:
        raise UpstreamError(str(e)) from e
