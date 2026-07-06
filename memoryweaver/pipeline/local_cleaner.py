import os
import datetime
import numpy as np
from PIL import Image, ImageFilter
from PIL.ExifTags import TAGS

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

def get_clip_model():
    global _CLIP_MODEL
    if _CLIP_MODEL is None:
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(_PRECLEAN_INSTALL_HINT) from e
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"Loading local CLIP model on device: {device}...")
        _CLIP_MODEL = SentenceTransformer('clip-ViT-B-32', device=device)
    return _CLIP_MODEL

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
                    except:
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
                        except:
                            pass
                
                # Extract camera make/model
                make = str(exif_data.get("Make", "")).strip()
                model = str(exif_data.get("Model", "")).strip()
                if make or model:
                    meta["uploader"] = f"{make} {model}".strip()
    except Exception as e:
        pass
    return meta

def analyze_directory(source_dir: str, blur_threshold: float = 12.0, dup_threshold: int = 8, progress_callback=None) -> dict:
    """
    Scans source_dir, extracts CLIP embeddings, groups burst duplicates via DBSCAN, 
    and filters images into accepted and rejected categories.
    """
    if not os.path.exists(source_dir):
        raise ValueError(f"Source folder does not exist: {source_dir}")
        
    all_files = [f for f in os.listdir(source_dir) 
                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic'))]
    all_files.sort()
                 
    accepted = []
    rejected = []
    
    # Load model
    model = get_clip_model()
    
    # First Pass: Load all photos, compute sharpness, EXIF, and CLIP embeddings
    print(f"Generating CLIP embeddings and analyzing metadata for {len(all_files)} files...")
    raw_candidates = []
    
    for i, filename in enumerate(all_files):
        if progress_callback:
            progress_callback(i + 1, len(all_files))
            
        full_path = os.path.join(source_dir, filename)
        sharpness = get_image_sharpness(full_path)
        meta = get_exif_metadata(full_path)
        
        # Calculate local CLIP embedding
        embedding = None
        try:
            with Image.open(full_path) as im:
                # Convert HEIC/RGB images safely to RGB
                im_rgb = im.convert('RGB')
                embedding = model.encode(im_rgb, convert_to_numpy=True)
        except Exception as e:
            print(f"Error encoding {filename} with CLIP: {e}")
            
        raw_candidates.append({
            "filename": filename,
            "path": full_path,
            "sharpness": sharpness,
            "uploader": meta["uploader"],
            "date": meta["date"],
            "embedding": embedding
        })
        
    # Second Pass: Filter Blurry images
    clean_candidates = []
    for item in raw_candidates:
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
            clean_candidates.append(item)
            
    # Third Pass: Clustering near-duplicates using DBSCAN
    # We filter out items that failed CLIP encoding
    valid_candidates = [c for c in clean_candidates if c["embedding"] is not None]
    invalid_candidates = [c for c in clean_candidates if c["embedding"] is None]
    
    # Add invalid ones directly to accepted
    for item in invalid_candidates:
        accepted.append({
            "filename": item["filename"],
            "path": item["path"],
            "uploader": item["uploader"],
            "date": item["date"],
            "sharpness": round(item["sharpness"], 2)
        })
        
    # Extract timestamps for temporal clustering
    for item in valid_candidates:
        date_str = item["date"]
        ts = 0.0
        if date_str:
            for fmt in ("%B %d, %Y at %I:%M %p", "%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M:%S\u0000"):
                try:
                    ts = datetime.datetime.strptime(date_str.strip(), fmt).timestamp()
                    break
                except:
                    pass
        if ts == 0.0:
            try:
                ts = os.path.getmtime(item["path"])
            except:
                pass
        item["timestamp"] = ts

    # Sort valid candidates chronologically
    valid_candidates.sort(key=lambda x: x["timestamp"])

    if len(valid_candidates) > 1:
        # Determine if we have meaningful, varying timestamps in the photo set
        timestamps = [c["timestamp"] for c in valid_candidates]
        time_range = max(timestamps) - min(timestamps)
        has_varying_dates = time_range > 300.0 # spans more than 5 minutes
        
        clusters = []
        
        if has_varying_dates:
            # 1. Temporal-Semantic Burst clustering (ideal case with EXIF capture dates)
            # Group consecutive photos taken within 30 seconds of each other with similarity >= 0.88
            current_cluster = []
            for item in valid_candidates:
                if not current_cluster:
                    current_cluster.append(item)
                else:
                    prev = current_cluster[-1]
                    time_diff = abs(item["timestamp"] - prev["timestamp"])
                    
                    emb1 = item["embedding"]
                    emb2 = prev["embedding"]
                    dot = np.dot(emb1, emb2)
                    norm1 = np.linalg.norm(emb1)
                    norm2 = np.linalg.norm(emb2)
                    sim = float(dot / (norm1 * norm2)) if (norm1 > 0 and norm2 > 0) else 0.0
                    
                    if time_diff <= 30.0 and sim >= 0.88:
                        current_cluster.append(item)
                    else:
                        if len(current_cluster) > 1:
                            clusters.append(current_cluster)
                        current_cluster = [item]
            if len(current_cluster) > 1:
                clusters.append(current_cluster)
        else:
            # 2. Strict Visual-Only clustering (fallback if EXIF dates are missing/uniform due to bulk copy)
            # We cluster using DBSCAN but with a very tight threshold (eps=0.06 equivalent to similarity >= 0.94)
            from sklearn.cluster import DBSCAN  # optional-extra dep; see _PRECLEAN_INSTALL_HINT
            embeddings_matrix = np.array([c["embedding"] for c in valid_candidates])
            db = DBSCAN(eps=0.06, min_samples=2, metric='cosine')
            labels = db.fit_predict(embeddings_matrix)
            
            # Map labels to candidates
            for idx, label in enumerate(labels):
                valid_candidates[idx]["label"] = int(label)
                
            # Group by label to form clusters
            db_clusters = {}
            for item in valid_candidates:
                lbl = item.get("label", -1)
                if lbl != -1:
                    if lbl not in db_clusters:
                        db_clusters[lbl] = []
                    db_clusters[lbl].append(item)
            clusters = list(db_clusters.values())
            
        # Map cluster labels to resolved duplicates
        resolved_duplicates = {} # filename -> (duplicate_of, cluster_num, sharpest_filename)
        
        for idx, group in enumerate(clusters):
            cluster_num = idx + 1
            # Sort group by sharpness descending
            group.sort(key=lambda x: x["sharpness"], reverse=True)
            sharpest = group[0]
            
            resolved_duplicates[sharpest["filename"]] = (None, cluster_num, sharpest["filename"])
            for dup in group[1:]:
                resolved_duplicates[dup["filename"]] = (sharpest["filename"], cluster_num, sharpest["filename"])
                rejected.append({
                    "filename": dup["filename"],
                    "path": dup["path"],
                    "uploader": dup["uploader"],
                    "date": dup["date"],
                    "sharpness": round(dup["sharpness"], 2),
                    "reason": f"Near-duplicate burst shot",
                    "duplicate_of": sharpest["filename"],
                    "clusterNumber": cluster_num,
                    "sharpestOfCluster": sharpest["filename"]
                })
                
        # Fill accepted
        for item in valid_candidates:
            filename = item["filename"]
            if filename in resolved_duplicates:
                dup_info = resolved_duplicates[filename]
                if dup_info[0] is None: # Only add the sharpest master to accepted
                    accepted.append({
                        "filename": item["filename"],
                        "path": item["path"],
                        "uploader": item["uploader"],
                        "date": item["date"],
                        "sharpness": round(item["sharpness"], 2),
                        "clusterNumber": dup_info[1],
                        "sharpestOfCluster": dup_info[2]
                    })
            else:
                accepted.append({
                    "filename": item["filename"],
                    "path": item["path"],
                    "uploader": item["uploader"],
                    "date": item["date"],
                    "sharpness": round(item["sharpness"], 2)
                })
    else:
        # If 1 or 0 candidates, add all to accepted
        for item in valid_candidates:
            accepted.append({
                "filename": item["filename"],
                "path": item["path"],
                "uploader": item["uploader"],
                "date": item["date"],
                "sharpness": round(item["sharpness"], 2)
            })
            
    # Sort lists alphabetically
    accepted.sort(key=lambda x: x["filename"])
    rejected.sort(key=lambda x: x["filename"])
    
    return {
        "status": "success",
        "accepted": accepted,
        "rejected": rejected
    }
