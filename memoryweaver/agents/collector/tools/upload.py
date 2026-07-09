import os
import io
import hashlib
import datetime
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS



from pillow_heif import register_heif_opener
register_heif_opener()

def _get_decimal_coordinates(info):
    """Helper to convert degree/minute/second GPS tuples to decimal degrees."""
    gps_info = {}
    for key, val in info.items():
        tag = GPSTAGS.get(key, key)
        gps_info[tag] = val
        
    gps_latitude = gps_info.get("GPSLatitude")
    gps_latitude_ref = gps_info.get("GPSLatitudeRef")
    gps_longitude = gps_info.get("GPSLongitude")
    gps_longitude_ref = gps_info.get("GPSLongitudeRef")
    
    if not (gps_latitude and gps_latitude_ref and gps_longitude and gps_longitude_ref):
        return None, None
        
    def _to_decimal(value, ref):
        # pillow returns rational numbers as fractions or floats
        # value is typically (degrees, minutes, seconds)
        d = float(value[0])
        m = float(value[1])
        s = float(value[2])
        decimal = d + (m / 60.0) + (s / 3600.0)
        if ref in ['S', 'W']:
            decimal = -decimal
        return decimal
        
    try:
        lat = _to_decimal(gps_latitude, gps_latitude_ref)
        lon = _to_decimal(gps_longitude, gps_longitude_ref)
        return lat, lon
    except Exception:
        return None, None

def extract_exif(image_path) -> dict:
    """Extracts detailed shooting settings, GPS coordinates, and device model from photo EXIF tags."""
    result = {
        "timestamp": None,
        "gps": {"latitude": None, "longitude": None},
        "device": None,
        "altitude": None,
        "heading": None,
        "aperture": None,
        "shutter_speed": None,
        "iso": None,
        "focal_length": None,
        "lens": None,
        "software": None,
        "color_space": None
    }
    try:
        with Image.open(image_path) as img:
            exif_data = img.getexif()
            if not exif_data:
                return result
                
            # Parse main IFD tags
            for tag_id in exif_data:
                tag = TAGS.get(tag_id, tag_id)
                data = exif_data.get(tag_id)
                if tag == "DateTime":
                    try:
                        dt = datetime.datetime.strptime(str(data), "%Y:%m:%d %H:%M:%S")
                        result["timestamp"] = dt.isoformat()
                    except ValueError:
                        pass
                elif tag == "Model":
                    result["device"] = str(data).strip()
                elif tag == "Software":
                    result["software"] = str(data).strip()
                    
            # Parse nested Exif IFD block (34665)
            exif_ifd = exif_data.get_ifd(34665)
            if exif_ifd:
                # ExposureTime
                et = exif_ifd.get(33434)
                if et is not None:
                    if isinstance(et, tuple) and len(et) == 2 and et[1] != 0:
                        if et[0] == 1:
                            result["shutter_speed"] = f"1/{et[1]}s"
                        else:
                            result["shutter_speed"] = f"{et[0]/et[1]:.4f}s"
                    else:
                        result["shutter_speed"] = f"{et}s"
                        
                # FNumber (Aperture)
                fn = exif_ifd.get(33437)
                if fn is not None:
                    if isinstance(fn, tuple) and len(fn) == 2 and fn[1] != 0:
                        fn = fn[0] / fn[1]
                    result["aperture"] = f"f/{fn}"
                    
                # ISOSpeedRatings
                iso = exif_ifd.get(34855)
                if iso is not None:
                    result["iso"] = str(iso)
                    
                # FocalLength
                fl = exif_ifd.get(37386)
                if fl is not None:
                    if isinstance(fl, tuple) and len(fl) == 2 and fl[1] != 0:
                        fl = fl[0] / fl[1]
                    result["focal_length"] = f"{fl}mm"
                    
                # LensModel
                lens = exif_ifd.get(42036)
                if lens is not None:
                    result["lens"] = str(lens).strip()
                    
                # ColorSpace
                cs = exif_ifd.get(40961)
                if cs is not None:
                    if cs == 1:
                        result["color_space"] = "sRGB"
                    elif cs == 2 or cs == 65535:
                        result["color_space"] = "Adobe RGB (or Uncalibrated)"
                    else:
                        result["color_space"] = f"Code {cs}"
            
            # Parse nested GPS Info IFD block (34853)
            gps_info = exif_data.get_ifd(34853)
            if gps_info:
                lat, lon = _get_decimal_coordinates(gps_info)
                result["gps"]["latitude"] = lat
                result["gps"]["longitude"] = lon
                
                # Altitude (tag 6)
                alt = gps_info.get(6)
                if alt is not None:
                    if isinstance(alt, tuple) and len(alt) == 2 and alt[1] != 0:
                        alt = alt[0] / alt[1]
                    ref = gps_info.get(5, b'\x00')  # 0 = above sea level, 1 = below sea level
                    is_below = False
                    if isinstance(ref, int) and ref == 1:
                        is_below = True
                    elif isinstance(ref, bytes) and len(ref) > 0 and ref[0] == 1:
                        is_below = True
                    
                    val = float(alt)
                    if is_below:
                        val = -val
                    result["altitude"] = f"{val:.1f}m"
                    
                # Compass Heading / Image Direction (tag 17)
                bearing = gps_info.get(17)
                if bearing is not None:
                    if isinstance(bearing, tuple) and len(bearing) == 2 and bearing[1] != 0:
                        bearing = bearing[0] / bearing[1]
                    
                    b_val = float(bearing)
                    cardinals = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
                    idx = int((b_val + 11.25) / 22.5) % 16
                    result["heading"] = f"{b_val:.1f}° ({cardinals[idx]})"
                    
    except Exception as e:
        print(f"Error parsing EXIF: {e}")
    return result

def process_and_save_upload(file_bytes: bytes, original_filename: str, contributor_name: str, session_id: str = "default") -> dict:
    """
    Validates, extracts metadata, saves to GCS (or local storage), and returns
    a standard photo session object. session_id routes the file into the
    corresponding event's isolated storage namespace (see StorageHelper).

    Validates:
    - Max size: 20MB (20 * 1024 * 1024 bytes)
    - File extension: jpg, jpeg, png, heic
    """
    max_size = 20 * 1024 * 1024
    if len(file_bytes) > max_size:
        raise ValueError(f"File size exceeds the 20MB limit (size: {len(file_bytes)} bytes)")
        
    # Sanitize the input filename immediately to prevent path traversal
    safe_filename = os.path.basename(original_filename)
    
    _, ext = os.path.splitext(safe_filename.lower())
    SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".m4a", ".mp3", ".webm", ".wav", ".pdf", ".txt")
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"Unsupported file format: {ext}. Only images (JPEG/PNG/HEIC), videos (MP4/MOV), voice (M4A/MP3/WEBM/WAV), documents (PDF), and text notes (.txt) are allowed.")

    # Calculate anonymous contributor ID hash (no PII logging)
    contributor_id = hashlib.sha256(contributor_name.strip().lower().encode()).hexdigest()[:12]
    
    # Save the file temporarily to extract EXIF
    temp_filename = f"temp_{safe_filename}"
    with open(temp_filename, "wb") as f:
        f.write(file_bytes)
        
    try:
        exif = extract_exif(temp_filename)
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
            
    # Generate unique filename to prevent namespace collisions
    timestamp_prefix = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    unique_filename = f"{contributor_id}_{timestamp_prefix}_{safe_filename}"
    
    # Save to storage (GCS/Local fallback), scoped to this event's session
    from app.app_utils.storage import StorageHelper
    storage = StorageHelper(session_id=session_id)
    gcs_uri = storage.save_upload(file_bytes, unique_filename)
    
    # Generate and save thumbnail
    thumb_path = ""
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        thumb_buf = io.BytesIO()
        img.save(thumb_buf, format="JPEG", quality=82)
        thumb_path = storage.save_thumbnail(thumb_buf.getvalue(), unique_filename)
    except Exception as te:
        print(f"Failed to generate thumbnail: {te}")
    
    return {
        "filename": unique_filename,
        "gcs_uri": gcs_uri,
        "thumbnail_path": thumb_path,
        "upload_timestamp": datetime.datetime.utcnow().isoformat(),
        "contributor_id": contributor_id,
        "session_id": session_id,
        "exif": exif
    }
