import os
import datetime
import numpy as np
from PIL import Image, ImageFilter
from PIL.ExifTags import TAGS

def get_image_sharpness(img_path: str) -> float:
    """Computes edge variance using Pillow FIND_EDGES to determine image sharpness."""
    try:
        with Image.open(img_path) as im:
            # Downscale for quick processing
            im_gray = im.convert('L').resize((256, 256))
            edges = im_gray.filter(ImageFilter.FIND_EDGES)
            arr = np.array(edges)
            return float(arr.var())
    except Exception as e:
        print(f"Error calculating sharpness for {img_path}: {e}")
        return 0.0

def get_image_dhash(img_path: str, hash_size: int = 8) -> str:
    """Computes Difference Hash (dHash) to identify near-duplicate images."""
    try:
        with Image.open(img_path) as im:
            im_gray = im.convert('L').resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
            pixels = np.array(im_gray)
            # Compare adjacent pixels horizontally
            diff = pixels[:, 1:] > pixels[:, :-1]
            # Convert boolean array to hex string
            return ''.join(f'{int(val)}' for val in diff.flatten())
    except Exception as e:
        print(f"Error calculating dHash for {img_path}: {e}")
        return ""

def get_hamming_distance(hash1: str, hash2: str) -> int:
    """Calculates the Hamming distance between two binary hash strings."""
    if not hash1 or not hash2 or len(hash1) != len(hash2):
        return 999
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))

def get_exif_metadata(img_path: str) -> dict:
    """Extracts date and camera model info from image EXIF metadata."""
    meta = {
        "date": "",
        "uploader": "Unknown Camera"
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
                        # Convert to standard ISO format
                        dt = datetime.datetime.strptime(date_str, "%Y:%M:%D %H:%M:%S")
                        meta["date"] = dt.strftime("%B %d, %Y at %I:%M %p")
                    except:
                        meta["date"] = str(date_str)
                
                # Extract camera uploader mapping
                make = str(exif_data.get("Make", "")).strip()
                model = str(exif_data.get("Model", "")).strip()
                if make or model:
                    meta["uploader"] = f"{make} {model}".strip()
    except Exception as e:
        pass
    return meta

def analyze_directory(source_dir: str, blur_threshold: float = 12.0, dup_threshold: int = 8, progress_callback=None) -> dict:
    """
    Scans source_dir and filters images into accepted and rejected categories.
    No files are modified in the source folder.
    """
    if not os.path.exists(source_dir):
        raise ValueError(f"Source folder does not exist: {source_dir}")
        
    all_files = [f for f in os.listdir(source_dir) 
                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))]
                 
    accepted = []
    rejected = []
    
    # Store computed details
    computed_details = []
    
    print(f"Analyzing {len(all_files)} images from: {source_dir}")
    
    for i, filename in enumerate(all_files):
        if progress_callback:
            progress_callback(i + 1, len(all_files))
            
        full_path = os.path.join(source_dir, filename)
        sharpness = get_image_sharpness(full_path)
        dhash_str = get_image_dhash(full_path)
        meta = get_exif_metadata(full_path)
        
        computed_details.append({
            "filename": filename,
            "path": full_path,
            "sharpness": sharpness,
            "dhash": dhash_str,
            "uploader": meta["uploader"],
            "date": meta["date"]
        })
        
    # First Pass: Filter Blurry images
    blurry_filtered = []
    candidates = []
    
    for item in computed_details:
        if item["sharpness"] < blur_threshold:
            rejected.append({
                "filename": item["filename"],
                "path": item["path"],
                "uploader": item["uploader"],
                "date": item["date"],
                "sharpness": round(item["sharpness"], 2),
                "reason": f"Blurry (Sharpness score {round(item['sharpness'], 2)} < {blur_threshold})",
                "duplicate_of": ""
            })
        else:
            candidates.append(item)
            
    # Second Pass: Filter Duplicates using Perceptual Hash
    # We sort candidates by sharpness descending, so the sharpest one is kept as lead
    candidates.sort(key=lambda x: x["sharpness"], reverse=True)
    
    unique_candidates = []
    
    for item in candidates:
        is_duplicate = False
        duplicate_parent = ""
        
        for unique_item in unique_candidates:
            dist = get_hamming_distance(item["dhash"], unique_item["dhash"])
            if dist <= dup_threshold:
                is_duplicate = True
                duplicate_parent = unique_item["filename"]
                break
                
        if is_duplicate:
            rejected.append({
                "filename": item["filename"],
                "path": item["path"],
                "uploader": item["uploader"],
                "date": item["date"],
                "sharpness": round(item["sharpness"], 2),
                "reason": f"Near-duplicate burst shot",
                "duplicate_of": duplicate_parent
            })
        else:
            unique_candidates.append(item)
            
    # Map accepted list
    for item in unique_candidates:
        accepted.append({
            "filename": item["filename"],
            "path": item["path"],
            "uploader": item["uploader"],
            "date": item["date"],
            "sharpness": round(item["sharpness"], 2)
        })
        
    # Sort final accepted set by filename or date
    accepted.sort(key=lambda x: x["filename"])
    rejected.sort(key=lambda x: x["filename"])
    
    return {
        "accepted": accepted,
        "rejected": rejected
    }
