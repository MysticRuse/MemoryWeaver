"""Video watermark detection and delogo encoding.

Extracted from `remove_video_watermark`, which was a single 445-line handler
mixing frame sampling, two detection strategies, coordinate arithmetic and the
ffmpeg invocation. Split so the cheap classical detector and the billable model
escalation are separately visible - the cost distinction the playbook depends
on was previously buried mid-function.
"""

import os
import subprocess

import cv2
import imageio_ffmpeg
import numpy as np

from app.app_utils.genai_client import PIPELINE_MODEL, get_gemini_client, text_config
from app.app_utils.logging_config import get_logger

logger = get_logger(__name__)

def probe_and_sample(temp_input_path, sample_count=20):
    """Reads video geometry and samples frames for the detectors.

    Returns (width, height, fps, frame_count, duration, sample_frames).
    """
    cap = cv2.VideoCapture(temp_input_path)
    if not cap.isOpened():
        raise ValueError("Could not open video to read properties")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frame_count / fps if fps else 0.0

    n = min(sample_count, max(frame_count, 1))
    indices = np.linspace(0, max(frame_count - 1, 0), n, dtype=int)
    frames = []
    for f_idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ret, frame = cap.read()
        if ret:
            frames.append(frame)
    cap.release()
    return width, height, fps, frame_count, duration, frames


def detect_watermark_regions(sample_frames, width, height, req):
    """Finds candidate watermark/logo boxes in the sampled frames.

    Runs the free classical temporal-variance detector first and only escalates
    to the vision model when it finds nothing confident - the cheap-path-first
    behaviour the cost playbook depends on. Returns
    (detected_logos, detected_captions, used_classical).
    """
    from pipeline.cost_tracker import get_cost_tracker
    cost_tracker = get_cost_tracker()

    detected_captions = []
    detected_logos = []
    classical_success = False

    if len(sample_frames) >= 3:
        try:
            # Convert sample frames to grayscale float array
            gray_stack = np.array([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in sample_frames], dtype=np.float32)

            # Temporal variance per pixel across sample frames
            var_map = np.var(gray_stack, axis=0)
            mean_map = np.mean(gray_stack, axis=0)

            # Static regions have near-zero variance across frames (< 15.0) and non-black mean (> 20.0)
            static_mask = np.uint8((var_map < 15.0) & (mean_map > 20.0)) * 255

            # Find candidate contours in static mask
            contours, _ = cv2.findContours(static_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                # Valid static candidate region: size between 10px and 45% of width/height
                if 10 <= w <= width * 0.45 and 10 <= h <= height * 0.45:
                    # Classify position: bottom 30% = caption/watermark; top/outro = logo
                    if y > height * 0.70:
                        detected_captions.append((x, y, w, h))
                    else:
                        detected_logos.append((x, y, w, h, False))

            if detected_captions or detected_logos:
                classical_success = True
                cost_tracker.record_feature_use("watermark_remover_classical", req.session_id, is_escalation=False)
        except Exception as ex_cv:
            logger.warning(f"Classical variance detection error: {ex_cv}")
    # --- ESCALATION PATH (Rare & Logged): Fallback to Gemini Vision if static detection yields no confident candidate ---
    if not classical_success and sample_frames:
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            import json

            from google.genai import types

            try:
                # Log explicit escalation event
                cost_tracker.record_feature_use("watermark_remover_gemini_fallback", req.session_id, is_escalation=True)

                client = get_gemini_client()
                key_frames = [sample_frames[0], sample_frames[len(sample_frames)//2], sample_frames[-1]]

                for idx, frame in enumerate(key_frames):
                    _, img_buffer = cv2.imencode('.jpg', frame)
                    img_bytes = img_buffer.tobytes()

                    prompt = (
                        "Locate any hardcoded captions/subtitles and social media logos/icons/watermarks in this video frame.\n"
                        "Provide their normalized coordinates in a 0 to 1000 scale format: [ymin, xmin, ymax, xmax].\n"
                        "Return ONLY a raw JSON object:\n"
                        "{\n"
                        "  \"captions\": [[ymin, xmin, ymax, xmax]],\n"
                        "  \"logos\": [[ymin, xmin, ymax, xmax]]\n"
                        "}"
                    )

                    response = client.models.generate_content(
                        model=PIPELINE_MODEL, config=text_config(),
                        contents=[
                            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                            prompt
                        ]
                    )
                    text = response.text.strip()
                    if text.startswith("```"):
                        text = text.split("\n", 1)[1]
                    if text.endswith("```"):
                        text = text.rsplit("\n", 1)[0]
                    text = text.strip()
                    if text.startswith("json"):
                        text = text[4:].strip()

                    res_json = json.loads(text)
                    is_last_frame = (idx == len(key_frames) - 1)

                    for box in res_json.get("captions", []):
                        ymin, xmin, ymax, xmax = box
                        x = int(xmin * width / 1000)
                        y = int(ymin * height / 1000)
                        w = int((xmax - xmin) * width / 1000)
                        h = int((ymax - ymin) * height / 1000)
                        detected_captions.append((x, y, w, h))

                    for box in res_json.get("logos", []):
                        ymin, xmin, ymax, xmax = box
                        x = int(xmin * width / 1000)
                        y = int(ymin * height / 1000)
                        w = int((xmax - xmin) * width / 1000)
                        h = int((ymax - ymin) * height / 1000)
                        detected_logos.append((x, y, w, h, is_last_frame))
            except Exception as e:
                logger.warning(f"DEBUG: Gemini detection failed: {e}")
    return detected_logos, detected_captions, classical_success


def build_active_boxes(req, detected_logos, detected_captions, width, height, duration):
    """Turns detections plus the request flags into timed delogo boxes.

    Each entry is (x, y, w, h, start_t, end_t) in pixel/second space.
    """
    # Compile list of active bounding boxes: (cx, cy, cw, ch, start_t, end_t)
    active_boxes = []

    # --- Logos / Watermarks ---
    if req.remove_watermarks or req.remove_logos:
        if detected_logos:
            # Apply detected logo boxes individually
            for cx, cy, cw, ch, is_last in detected_logos:
                if cw > 5 and ch > 5 and cw < width * 0.5 and ch < height * 0.5:
                    is_centered_h = abs(cx + cw/2 - width/2) < width * 0.15
                    is_centered_v = abs(cy + ch/2 - height/2) < height * 0.15
                    if is_last and is_centered_h and is_centered_v:
                        # Centered outro watermark logo: active only during the last 5 seconds of the video
                        active_boxes.append((cx, cy, cw, ch, duration - 5.0, duration))
                    else:
                        # Corner logo/watermark: apply throughout the video
                        active_boxes.append((cx, cy, cw, ch, 0.0, duration))
        else:
            # Fallback to default corner/outro boxes
            if req.remove_watermarks:
                active_boxes.append((10, height - 70, 180, 60, 0.0, duration))
                active_boxes.append((width - 190, height - 70, 180, 60, 0.0, duration))
                if duration > 5.0:
                    cw = min(280, width - 20)
                    ch = min(180, height - 20)
                    cx = (width - cw) // 2
                    cy = (height - ch) // 2
                    active_boxes.append((cx, cy, cw, ch, duration - 5.0, duration))
            if req.remove_logos:
                active_boxes.append((10, 10, 180, 60, 0.0, duration))
                active_boxes.append((width - 190, 10, 180, 60, 0.0, duration))

    # --- Captions ---
    if req.remove_captions:
        if detected_captions:
            # Take the union of detected caption boxes to cover all caption lines in a single tight box
            xmin = min(cx for cx, cy, cw, ch in detected_captions)
            ymin = min(cy for cx, cy, cw, ch in detected_captions)
            xmax = max(cx + cw for cx, cy, cw, ch in detected_captions)
            ymax = max(cy + ch for cx, cy, cw, ch in detected_captions)

            union_w = xmax - xmin
            union_h = ymax - ymin
            # Only apply if the union box size is sane
            if union_w > 5 and union_h > 5 and union_w < width * 0.95 and union_h < height * 0.35:
                active_boxes.append((xmin, ymin, union_w, union_h, 0.0, duration))
            else:
                # Fallback if union box size is weird
                for cx, cy, cw, ch in detected_captions:
                    if cw > 5 and ch > 5 and cw < width * 0.95 and ch < height * 0.35:
                        active_boxes.append((cx, cy, cw, ch, 0.0, duration))
        else:
            # Fallback to default percentage-based coordinates
            if height > width:
                cw = int(width * 0.8)
                ch = int(height * 0.055)
                cx = int(width * 0.1)
                cy = int(height * 0.73)
            else:
                cw = int(width * 0.7)
                ch = int(height * 0.08)
                cx = int(width * 0.15)
                cy = int(height * 0.80)
            active_boxes.append((cx, cy, cw, ch, 0.0, duration))

    # --- Overlays ---
    if req.remove_overlays:
        active_boxes.append((0, 0, width, 15, 0.0, duration))
        active_boxes.append((0, height - 20, width, 15, 0.0, duration))

    return active_boxes



def encode_with_delogo(temp_input_path, temp_output_path, active_boxes,
                       width, height, fps, req):
    """Streams frames through ffmpeg applying the delogo boxes."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    # Open video and launch FFMPEG piper
    cap = cv2.VideoCapture(temp_input_path)
    if not cap.isOpened():
        raise ValueError("Could not open video to process frames")

    ffmpeg_cmd = [
        ffmpeg_exe, "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{width}x{height}",
        "-pix_fmt", "bgr24",
        "-r", str(fps),
        "-i", "-", # stdin
        "-i", temp_input_path, # audio/metadata source
        "-map", "0:v:0",
    ]

    # Audio mapping
    if req.remove_voice:
        ffmpeg_cmd.append("-an")
    else:
        ffmpeg_cmd.extend(["-map", "1:a:0?", "-c:a", "aac", "-b:a", "192k"])

    # Copy metadata
    ffmpeg_cmd.extend(["-map_metadata", "0", "-movflags", "use_metadata_tags"])

    # H264 visual encoding parameters
    ffmpeg_cmd.extend(["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-preset", "fast", temp_output_path])

    process = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def get_text_mask(roi):
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, bright_mask = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        edges = cv2.Canny(gray, 50, 150)
        combined = cv2.bitwise_or(bright_mask, edges)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated = cv2.dilate(combined, kernel, iterations=1)
        return dilated

    frame_idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            current_time = frame_idx / fps

            # Apply inpainting for any active box at this timestamp
            for cx, cy, cw, ch, start_t, end_t in active_boxes:
                if start_t <= current_time <= end_t:
                    # Safely constrain coordinates within the video frame
                    margin = 5
                    scx = max(margin, min(cx, width - margin - 1))
                    scy = max(margin, min(cy, height - margin - 1))
                    scw = max(1, min(cw, width - scx - margin))
                    sch = max(1, min(ch, height - scy - margin))

                    roi = frame[scy:scy+sch, scx:scx+scw]
                    mask = get_text_mask(roi)
                    inpainted_roi = cv2.inpaint(roi, mask, 2, cv2.INPAINT_TELEA)

                    # Smooth the inpainted region using a bilateral filter to remove pixelation/grain mismatch
                    smoothed_roi = cv2.bilateralFilter(inpainted_roi, d=5, sigmaColor=50, sigmaSpace=50)

                    # Soften the edges of the mask slightly to ensure a seamless blend
                    soft_mask = cv2.GaussianBlur(mask, (3, 3), 0)

                    # Blend the smoothed, healed pixels back using the soft mask as alpha
                    alpha = soft_mask.astype(np.float32) / 255.0
                    alpha = np.expand_dims(alpha, axis=2)
                    blended_roi = (smoothed_roi.astype(np.float32) * alpha + roi.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)

                    frame[scy:scy+sch, scx:scx+scw] = blended_roi

            process.stdin.write(frame.tobytes())
            frame_idx += 1
    finally:
        cap.release()
        _stdout, stderr = process.communicate()

    if process.returncode != 0:
        raise ValueError(f"FFMPEG encoding failed: {stderr.decode('utf-8')}")
