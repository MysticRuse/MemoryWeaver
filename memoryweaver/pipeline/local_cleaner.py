import datetime

from PIL import Image
from PIL.ExifTags import TAGS

from app.app_utils.logging_config import get_logger

logger = get_logger(__name__)

# torch / sentence-transformers / scikit-learn are imported lazily inside the
# functions that need them: they belong to the OPTIONAL on-device pre-clean
# feature (install with `uv sync --extra local-preclean`, ~2GB+). Keeping them
# out of module scope means the light helpers here (get_exif_metadata, used by
# the orchestrator and ingest path) work on a lean install, and the default
# quick-start doesn't download torch.
_PRECLEAN_INSTALL_HINT = (
    "Local pre-cleaning requires the optional ML stack. "
    "Install it with: uv sync --extra local-preclean"
)

# Module-level cache for CLIP model to avoid reloading on every API call
_CLIP_MODEL = None



def get_exif_metadata(img_path: str) -> dict:
    """Extracts date, camera model info, and GPS coordinates from image EXIF metadata."""
    meta = {
        "date": "",
        "uploader": "Unknown Camera",
        "gps": ""
    }
    try:
        with Image.open(img_path) as im:
            exif = im.getexif()
            if exif:
                exif_data = {TAGS.get(tag_id, tag_id): val for tag_id, val in exif.items()}

                # Extract date
                date_str = exif_data.get("DateTimeOriginal") or exif_data.get("DateTime")
                if date_str:
                    try:
                        # Convert to standard format
                        dt = datetime.datetime.strptime(str(date_str).strip(), "%Y:%m:%d %H:%M:%S")
                        meta["date"] = dt.strftime("%B %d, %Y at %I:%M %p")
                    except ValueError:
                        # Non-standard EXIF date string; keep the raw value.
                        meta["date"] = str(date_str)

                # Extract GPSInfo (tag ID 34853)
                gps_data = exif.get(34853)
                if gps_data:
                    from PIL.ExifTags import GPSTAGS
                    gps_info = {}
                    for key, val in gps_data.items():
                        sub_tag = GPSTAGS.get(key, key)
                        gps_info[sub_tag] = val

                    if 'GPSLatitude' in gps_info and 'GPSLongitude' in gps_info:
                        lat_ref = gps_info.get('GPSLatitudeRef', 'N')
                        lon_ref = gps_info.get('GPSLongitudeRef', 'E')
                        lat = gps_info['GPSLatitude']
                        lon = gps_info['GPSLongitude']

                        try:
                            lat_deg = float(lat[0]) + float(lat[1])/60.0 + float(lat[2])/3600.0
                            if lat_ref == 'S':
                                lat_deg = -lat_deg
                            lon_deg = float(lon[0]) + float(lon[1])/60.0 + float(lon[2])/3600.0
                            if lon_ref == 'W':
                                lon_deg = -lon_deg
                            meta["gps"] = f"lat: {round(lat_deg, 5)}, lon: {round(lon_deg, 5)}"
                        except (ValueError, TypeError, IndexError, ZeroDivisionError) as exc:
                            # Malformed GPS rational triple; omit rather than guess.
                            logger.warning("%s: best-effort step failed, continuing: %s", "local_cleaner", exc)

                # Extract camera make/model
                make = str(exif_data.get("Make", "")).strip()
                model = str(exif_data.get("Model", "")).strip()
                if make or model:
                    meta["uploader"] = f"{make} {model}".strip()
    except Exception as exc:
        logger.warning("%s: best-effort step failed, continuing: %s", "local_cleaner", exc)
    return meta

