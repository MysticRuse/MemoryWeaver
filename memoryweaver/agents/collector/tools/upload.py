import os
import io
import hashlib
import datetime
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from pillow_heif import register_heif_opener
from app.app_utils.storage import StorageHelper

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
    """Extracts timestamp, GPS coordinates, and device model from photo EXIF tags."""
    result = {
        "timestamp": None,
        "gps": {"latitude": None, "longitude": None},
        "device": None
    }
    try:
        with Image.open(image_path) as img:
            exif_data = img.getexif()
            if not exif_data:
                return result
                
            for tag_id in exif_data:
                tag = TAGS.get(tag_id, tag_id)
                data = exif_data.get(tag_id)
                if tag == "DateTime":
                    try:
                        # Convert EXIF format (YYYY:MM:DD HH:MM:SS) to standard ISO format
                        dt = datetime.datetime.strptime(data, "%Y:%m:%d %H:%M:%S")
                        result["timestamp"] = dt.isoformat()
                    except ValueError:
                        pass
                elif tag == "Model":
                    result["device"] = str(data).strip()
            
            # GPS tags are nested under the GPS Info tag block
            # Tag ID 34853 is the standard GPStag container offset
            gps_info = exif_data.get_ifd(34853)
            if gps_info:
                lat, lon = _get_decimal_coordinates(gps_info)
                result["gps"]["latitude"] = lat
                result["gps"]["longitude"] = lon
                
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
    if ext not in (".jpg", ".jpeg", ".png", ".heic"):
        raise ValueError(f"Unsupported file format: {ext}. Only JPEG, PNG, and HEIC are allowed.")

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
