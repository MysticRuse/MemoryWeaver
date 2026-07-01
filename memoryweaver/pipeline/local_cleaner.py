import os
import datetime
import numpy as np
import torch
from PIL import Image, ImageFilter
from PIL.ExifTags import TAGS
from sentence_transformers import SentenceTransformer
from sklearn.cluster import DBSCAN

# Module-level cache for CLIP model to avoid reloading on every API call
_CLIP_MODEL = None

def get_clip_model():
    global _CLIP_MODEL
    if _CLIP_MODEL is None:
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
        
    if len(valid_candidates) > 1:
        # Extract embeddings matrix
        embeddings_matrix = np.array([c["embedding"] for c in valid_candidates])
        
        # DBSCAN clustering with cosine distance threshold of 0.15 (equivalent to >= 0.85 similarity)
        db = DBSCAN(eps=0.15, min_samples=2, metric='cosine')
        labels = db.fit_predict(embeddings_matrix)
        
        # Map labels to candidates
        for idx, label in enumerate(labels):
            valid_candidates[idx]["label"] = int(label)
            
        # Group by label to find best-shot (sharpest) in each duplicate cluster
        clusters = {}
        for item in valid_candidates:
            lbl = item["label"]
            if lbl != -1:
                if lbl not in clusters:
                    clusters[lbl] = []
                clusters[lbl].append(item)
                
        # Resolve clusters
        resolved_duplicates = {} # filename -> (duplicate_of, cluster_num, sharpest_filename)
        accepted_clustered = []
        
        for lbl, group in clusters.items():
            # Sort group by sharpness descending
            group.sort(key=lambda x: x["sharpness"], reverse=True)
            sharpest = group[0]
            
            # Map sharpest as master keep
            accepted_clustered.append(sharpest)
            resolved_duplicates[sharpest["filename"]] = (None, lbl + 1, sharpest["filename"])
            
            # Map duplicates
            for dup in group[1:]:
                resolved_duplicates[dup["filename"]] = (sharpest["filename"], lbl + 1, sharpest["filename"])
                rejected.append({
                    "filename": dup["filename"],
                    "path": dup["path"],
                    "uploader": dup["uploader"],
                    "date": dup["date"],
                    "sharpness": round(dup["sharpness"], 2),
                    "reason": f"Near-duplicate burst shot",
                    "duplicate_of": sharpest["filename"],
                    "clusterNumber": lbl + 1,
                    "sharpestOfCluster": sharpest["filename"]
                })
                
        # Add clustered keeps and unclustered items to accepted
        for item in valid_candidates:
            filename = item["filename"]
            if item["label"] == -1:
                # Unclustered
                accepted.append({
                    "filename": item["filename"],
                    "path": item["path"],
                    "uploader": item["uploader"],
                    "date": item["date"],
                    "sharpness": round(item["sharpness"], 2)
                })
            else:
                # Part of a cluster, if it's the sharpest keep it
                dup_info = resolved_duplicates.get(filename)
                if dup_info and dup_info[0] is None:
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
