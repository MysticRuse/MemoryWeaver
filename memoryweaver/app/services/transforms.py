"""Photo transformation builders.

Extracted from `get_photo_transformations`, a single 625-line handler that
inlined five unrelated image pipelines plus their model calls, fallbacks and
response assembly. Each variant is now an independent function with one job,
and the router just picks the ones the request asked for.

Every builder takes the decoded source image plus the scratch directory and
returns the filename it wrote (the kids builder also returns its caption and
sticker metadata, which the client needs to re-render the overlay).
"""

import io
import json
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from app.app_utils.genai_client import IMAGE_MODEL, PIPELINE_MODEL, get_gemini_client
from app.app_utils.logging_config import get_logger
from app.services.media_store import draw_kids_stickers_and_banner

logger = get_logger(__name__)


def build_black_and_white(orig_img, temp_dir, file_id, req) -> str:
    """Grayscale conversion. Pure PIL, no model call."""
    # 1. Black & White
    bw_name = f"temp_transform_{file_id}_bw.png"
    if req.type in ("all", "bw"):
        bw_img = ImageOps.grayscale(orig_img)
        bw_img.save(os.path.join(temp_dir, bw_name), format="PNG")
    return bw_name


def _sketch_via_image_model(orig_img):
    """Asks the image model for a colored-pencil rendering.

    Returns the generated image, or None if no key is configured or the call
    fails - the caller then uses the local OpenCV pipeline.
    """
    if not os.getenv("GEMINI_API_KEY"):
        return None
    prompt = (
        "Use the uploaded photo as the exact reference. Keep the subject's face, expression, "
        "features, textures, colors, and body proportions 100% identical. "
        "Convert it into a high-quality premium hand-drawn colored pencil drawing on textured white paper.\n"
        "Requirements:\n"
        "- Authentic colored pencil strokes with visible layering, soft blending, and direction following the form\n"
        "- Natural skin/fur colors with individual hair and texture detail\n"
        "- Detailed eyes with life and reflection\n"
        "- High detail, professional fine-art quality, sharp and natural\n"
        "- Clean, simplified background with soft paper grain texture visible\n"
        "Style: Realistic hand-drawn colored pencil portrait, not digital painting, not cartoon, no text or logos."
    )
    try:
        response = get_gemini_client().models.generate_content(
            model=IMAGE_MODEL, contents=[orig_img, prompt]
        )
        for part in response.candidates[0].content.parts:
            if getattr(part, "inline_data", None):
                return Image.open(io.BytesIO(part.inline_data.data))
    except Exception as e:
        logger.warning(f"Image-model sketch generation failed: {e}")
    return None


SKETCH_DEFAULTS = {
    "threshold_block_size": 9,
    "threshold_c": 9,
    "gamma": 2.4,
    "color_smoothing_d": 9,
    "saturation_boost": 1.2,
    "warmth_red_boost": 1.06,
    "paper_grain_intensity": 12,
}


def _sketch_parameters(orig_img):
    """Returns OpenCV pipeline parameters, model-tuned when a key is present.

    Note: the original code also solicited and clamped a `blur_ksize` and then
    never used it - the renderer derives its blur from image scale. The dead
    parameter was dropped rather than left in the prompt.

    Every value is clamped to a safe range, so a bad model response degrades
    to something renderable rather than breaking the filter chain.
    """
    if not os.getenv("GEMINI_API_KEY"):
        return dict(SKETCH_DEFAULTS)

    prompt = (
        "You are a master sketch artist. Suggest the optimal OpenCV and PIL parameters "
        "to convert this reference photo into a premium, highly detailed, realistic hand-drawn "
        "colored pencil drawing on textured white paper. Respond in JSON with keys: "
        "threshold_block_size, threshold_c, gamma, color_smoothing_d, "
        "saturation_boost, warmth_red_boost, paper_grain_intensity."
    )
    try:
        response = get_gemini_client().models.generate_content(
            model=PIPELINE_MODEL, contents=[orig_img, prompt]
        )
        text = response.text
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        raw = json.loads(text.strip())
    except Exception as e:
        logger.warning(f"Sketch parameter tuning failed, using defaults: {e}")
        return dict(SKETCH_DEFAULTS)

    def _odd(value, lo, hi, default):
        v = int(raw.get(value, default))
        if v % 2 == 0:
            v += 1
        return max(lo, min(hi, v))

    return {
        "threshold_block_size": _odd("threshold_block_size", 5, 15, 9),
        "threshold_c": max(3, min(12, int(raw.get("threshold_c", 9)))),
        "gamma": max(1.5, min(3.2, float(raw.get("gamma", 2.4)))),
        "color_smoothing_d": max(5, min(15, int(raw.get("color_smoothing_d", 9)))),
        "saturation_boost": max(1.0, min(1.45, float(raw.get("saturation_boost", 1.2)))),
        "warmth_red_boost": max(1.0, min(1.12, float(raw.get("warmth_red_boost", 1.06)))),
        "paper_grain_intensity": max(5, min(20, int(raw.get("paper_grain_intensity", 12)))),
    }


def build_sketch(orig_img, temp_dir, file_id, req) -> str:
    """Colored-pencil rendering.

    Tries the image model first and falls back to a local OpenCV pipeline whose
    parameters are optionally model-tuned. Was one 246-line block covering all
    three paths.
    """
    sketch_name = f"temp_transform_{file_id}_sketch.png"
    if req.type not in ("all", "sketch"):
        return sketch_name

    generated = _sketch_via_image_model(orig_img)
    if generated is not None:
        generated.save(os.path.join(temp_dir, sketch_name), format="PNG")
        return sketch_name

    logger.info("Falling back to local OpenCV sketch rendering pipeline...")
    params = _sketch_parameters(orig_img)
    threshold_block_size = params["threshold_block_size"]
    threshold_c = params["threshold_c"]
    gamma = params["gamma"]
    color_smoothing_d = params["color_smoothing_d"]
    saturation_boost = params["saturation_boost"]
    warmth_red_boost = params["warmth_red_boost"]
    paper_grain_intensity = params["paper_grain_intensity"]

    try:
        cv_img = np.array(orig_img)
        gray = cv2.cvtColor(cv_img, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape

        # Calculate resolution-independent scale factor based on image dimensions
        scale_factor = max(w, h)

        # 1. Proportional Spacing, Thickness, and Blur sizes
        base_spacing = max(6, int(scale_factor * 0.015))
        hspacing = max(6, int(base_spacing * (threshold_block_size / 9.0)))
        hthickness = max(1, int(scale_factor * 0.0015))

        line_blur = int(scale_factor * 0.004)
        if line_blur % 2 == 0:
            line_blur += 1
        line_blur = max(3, line_blur)

        # 2. Procedural Pencil Hatching Generation (4 directions)
        def generate_hatch_texture(height, width, angle, spacing, thickness, blur):
            canvas = np.full((height, width), 255, dtype=np.uint8)
            for i in range(-max(height, width), max(height, width), spacing):
                if angle == 0:
                    cv2.line(canvas, (0, i), (width, i), 190, thickness=thickness, lineType=cv2.LINE_AA)
                elif angle == 90:
                    cv2.line(canvas, (i, 0), (i, height), 190, thickness=thickness, lineType=cv2.LINE_AA)
                elif angle == 45:
                    cv2.line(canvas, (i, 0), (i - height, height), 190, thickness=thickness, lineType=cv2.LINE_AA)
                else:
                    cv2.line(canvas, (i, 0), (i + height, height), 190, thickness=thickness, lineType=cv2.LINE_AA)
            return cv2.GaussianBlur(canvas, (blur, blur), 0)

        hatch_0 = generate_hatch_texture(h, w, 0, spacing=hspacing, thickness=hthickness, blur=line_blur)
        hatch_45 = generate_hatch_texture(h, w, 45, spacing=hspacing, thickness=hthickness, blur=line_blur)
        hatch_90 = generate_hatch_texture(h, w, 90, spacing=hspacing, thickness=hthickness, blur=line_blur)
        hatch_135 = generate_hatch_texture(h, w, 135, spacing=hspacing, thickness=hthickness, blur=line_blur)

        # 3. Form-following Tangent Gradient calculation
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        angles = np.arctan2(gy, gx)
        tangent_angles = angles + np.pi / 2
        degrees = np.rad2deg(tangent_angles) % 180

        # Map pixels to directional textures based on contour flow
        hatch = np.full((h, w), 255, dtype=np.uint8)
        hatch_cross = np.full((h, w), 255, dtype=np.uint8)

        mask_0 = ((degrees >= 0) & (degrees < 22.5)) | ((degrees >= 157.5) & (degrees < 180))
        mask_45 = (degrees >= 22.5) & (degrees < 67.5)
        mask_90 = (degrees >= 67.5) & (degrees < 112.5)
        mask_135 = (degrees >= 112.5) & (degrees < 157.5)

        hatch[mask_0] = hatch_0[mask_0]
        hatch[mask_45] = hatch_45[mask_45]
        hatch[mask_90] = hatch_90[mask_90]
        hatch[mask_135] = hatch_135[mask_135]

        # Perpendicular cross-hatching for shadow depth
        hatch_cross[mask_0] = hatch_90[mask_0]
        hatch_cross[mask_45] = hatch_135[mask_45]
        hatch_cross[mask_90] = hatch_0[mask_90]
        hatch_cross[mask_135] = hatch_45[mask_135]

        # 4. Dynamic Luminance-Aware Shading Blend
        gray_norm = gray / 255.0
        blend1 = np.uint8(gray_norm * 255.0 + (1.0 - gray_norm) * hatch)
        shadow_mask = (1.0 - gray_norm) ** gamma
        shading = np.uint8((1.0 - shadow_mask) * blend1 + shadow_mask * hatch_cross)

        # 5. Proportional Line Outline Extraction Block Size
        base_block = max(5, int(scale_factor * 0.006))
        block_size = int(base_block * (threshold_block_size / 9.0))
        if block_size % 2 == 0:
            block_size += 1
        block_size = max(5, block_size)

        outlines = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, block_size, threshold_c
        )
        # Proportional Human double-stroke jitter warp
        shift = max(1.0, scale_factor * 0.0015)
        M = np.float32([[1, 0, shift], [0, 1, shift]])
        outlines_shifted = cv2.warpAffine(outlines, M, (w, h), borderValue=255)
        outlines_human = cv2.multiply(outlines, outlines_shifted, scale=1.0/255.0)

        # 6. Composite Outline & Shading Blend
        sketch_raw = cv2.multiply(shading, outlines_human, scale=1.0/255.0)

        # 7. Heavy-weight Watercolor Drawing Paper Grain Texture overlay
        grain_h = max(100, int(h / 6))
        grain_w = max(100, int(w / 6))
        np.random.seed(42)
        noise_small = np.random.randint(0, paper_grain_intensity, (grain_h, grain_w), dtype=np.uint8)
        noise = cv2.resize(noise_small, (w, h), interpolation=cv2.INTER_LINEAR)
        grain = cv2.GaussianBlur(noise, (5, 5), 0)
        paper = 255 - grain

        # Apply drawing onto textured paper
        sketch_cv = cv2.multiply(sketch_raw, paper, scale=1.0/255.0)

        # Stretch contrast to absolute deep charcoal blacks
        min_val, max_val = float(np.min(sketch_cv)), float(np.max(sketch_cv))
        if max_val > min_val:
            sketch_cv = np.uint8(255.0 * (sketch_cv - min_val) / (max_val - min_val))

        # Smooth the original color image to create hand-drawn colored pencil regions
        color_smoothing_d = max(5, min(15, color_smoothing_d))
        color_smoothed = cv2.bilateralFilter(cv_img, d=color_smoothing_d, sigmaColor=75, sigmaSpace=75)

        # Apply warm undertone tint to capture skin tones accurately
        warm_img = color_smoothed.astype(np.float32)
        warm_img[:, :, 0] *= warmth_red_boost
        warm_img[:, :, 1] *= (1.0 + (warmth_red_boost - 1.0) * 0.4)
        warm_img = np.clip(warm_img, 0, 255).astype(np.uint8)

        # Convert sketch_cv to float normalized [0.0, 1.0] representing paper intensity
        S = sketch_cv.astype(np.float32) / 255.0
        S_3d = np.expand_dims(S, axis=2)

        # Map colors exclusively to strokes, leaving paper background white
        C = warm_img.astype(np.float32)
        color_sketch_cv = C + (255.0 - C) * S_3d
        color_sketch_cv = np.clip(color_sketch_cv, 0, 255).astype(np.uint8)

        # Convert back to PIL Image and enhance color saturation slightly
        sketch_img = Image.fromarray(color_sketch_cv)
        from PIL import ImageEnhance
        sketch_img = ImageEnhance.Color(sketch_img).enhance(saturation_boost)
    except Exception as ex:
        logger.warning(f"Sketch generation failed, falling back: {ex}")
        sketch_img = ImageOps.grayscale(orig_img)
    sketch_img.save(os.path.join(temp_dir, sketch_name), format="PNG")
    return sketch_name



def build_meme(orig_img, temp_dir, file_id, req) -> str:
    """Impact-caption meme layout with a generated top/bottom line."""
    # 3. Meme
    meme_name = f"temp_transform_{file_id}_meme.png"
    if req.type in ("all", "meme"):
        w, h = orig_img.size
        api_key = os.getenv("GEMINI_API_KEY")
        meme_generated_by_api = False

        if api_key:
            try:
                import json

                client = get_gemini_client()

                # Step 1: Design the meme layout and content
                logger.info("Step 1 (Meme): Planning layout via gemini-3.5-flash...")
                plan_prompt = (
                    "Analyze this photo and design a funny, clever, short internet meme for it.\n"
                    "1. Create a hilarious meme caption matching the photo context (e.g. 'When you...', 'Me trying to...', 'Nobody:', etc.). Keep it under 80 characters.\n"
                    "2. Suggest optimal center coordinates (caption_x, caption_y) from 0.0 to 1.0 to overlay the text on the photo.\n"
                    "3. Suggest a high-contrast RGB text color (caption_color) that stands out clearly on top of the image region (e.g. [255, 255, 255] or [0, 0, 0]).\n"
                    "4. Suggest exactly 1 context-relevant emoji/sticker (sticker) to add, along with normalized placement coordinates (sticker_x, sticker_y) and size multiplier (sticker_size).\n"
                    "Output EXACTLY a JSON object with this schema and nothing else:\n"
                    "{\n"
                    "  \"caption\": \"Hilarious caption text\",\n"
                    "  \"caption_x\": 0.5,\n"
                    "  \"caption_y\": 0.85,\n"
                    "  \"caption_color\": [255, 255, 255],\n"
                    "  \"sticker\": \"😎\",\n"
                    "  \"sticker_x\": 0.6,\n"
                    "  \"sticker_y\": 0.3,\n"
                    "  \"sticker_size\": 1.0\n"
                    "}"
                )

                response1 = client.models.generate_content(
                    model=PIPELINE_MODEL,
                    contents=[orig_img, plan_prompt]
                )

                text = response1.text
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()

                data = json.loads(text.strip())
                meme_caption = data.get("caption", "ME ON THIS TRIP: ✈️🥳")
                caption_x = data.get("caption_x", 0.5)
                caption_y = data.get("caption_y", 0.85)
                caption_color = data.get("caption_color", [255, 255, 255])
                sticker = data.get("sticker", "😎")
                sticker_x = data.get("sticker_x", 0.6)
                sticker_y = data.get("sticker_y", 0.3)
                sticker_size = data.get("sticker_size", 1.0)

                # Step 2: Render using Nano Banana 2
                logger.info("Step 2 (Meme): Rendering meme image using Nano Banana 2...")
                edit_prompt = (
                    f"Edit the uploaded photo to turn it into an amazing, funny internet meme.\n"
                    f"1. Draw the text: \"{meme_caption}\" in a bold, clean sans-serif/impact font at normalized coordinates (x={caption_x}, y={caption_y}) with color {caption_color}. The text should have a bold black outline or dropshadow to be highly readable.\n"
                    f"2. Paint a sticker of '{sticker}' (size multiplier {sticker_size}) at normalized coordinates (x={sticker_x}, y={sticker_y}).\n"
                    f"Style: The added elements should look cute, playful, have a clean white sticker outline for the emoji, and blend naturally into the photo."
                )

                response2 = client.models.generate_content(
                    model=IMAGE_MODEL,
                    contents=[orig_img, edit_prompt]
                )

                img_bytes = None
                for part in response2.candidates[0].content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        img_bytes = part.inline_data.data
                        break

                if img_bytes:
                    meme_img = Image.open(io.BytesIO(img_bytes))
                    meme_img.save(os.path.join(temp_dir, meme_name), format="PNG")
                    meme_generated_by_api = True
                    logger.info("Successfully generated high-quality Meme via Nano Banana 2!")
            except Exception as ex:
                logger.warning(f"Two-step Gemini Meme generation failed: {ex}. Falling back to PIL drawing.")
        if not meme_generated_by_api:
            # PIL fallback
            # Calculate header height based on image size to fit text comfortable
            header_h = int(h * 0.16) if h > 300 else 75
            meme_img = Image.new("RGB", (w, h + header_h), (255, 255, 255))
            meme_img.paste(orig_img, (0, header_h))
            draw_meme = ImageDraw.Draw(meme_img)

            # Select system bold/impact font
            font_paths = [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica Neue Bold.ttf",
                "/System/Library/Fonts/Supplemental/Impact.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf"
            ]
            existing_fonts = [fp for fp in font_paths if os.path.exists(fp)]
            chosen_font_path = existing_fonts[0] if existing_fonts else None

            font_size = max(18, int(header_h * 0.22))
            font = None
            if chosen_font_path:
                try:
                    font = ImageFont.truetype(chosen_font_path, font_size)
                except Exception as exc:
                    logger.warning("%s: best-effort step failed, continuing: %s", "transforms", exc)

            # Wrap text
            meme_caption = "ME ON THIS TRIP: ✈️🥳"
            words = meme_caption.split(' ')
            lines = []
            current_line = []
            max_w = int(w * 0.9)

            for word in words:
                current_line.append(word)
                test_line = ' '.join(current_line)
                if font:
                    bbox = draw_meme.textbbox((0, 0), test_line, font=font)
                    line_w = bbox[2] - bbox[0]
                else:
                    line_w = len(test_line) * (font_size * 0.5)

                if line_w > max_w:
                    if len(current_line) > 1:
                        current_line.pop()
                        lines.append(' '.join(current_line))
                        current_line = [word]
                    else:
                        lines.append(test_line)
                        current_line = []
            if current_line:
                lines.append(' '.join(current_line))

            # Draw lines centered vertically and horizontally
            line_h = font_size + 6
            start_y = int((header_h - (len(lines) * line_h)) / 2)
            for i, line in enumerate(lines):
                ly = start_y + (i * line_h)
                if font:
                    bbox = draw_meme.textbbox((0, 0), line, font=font)
                    lw = bbox[2] - bbox[0]
                    lx = int((w - lw) / 2)
                    draw_meme.text((lx, ly), line, fill=(0, 0, 0), font=font)
                else:
                    lw = len(line) * (font_size * 0.5)
                    lx = int((w - lw) / 2)
                    draw_meme.text((lx, ly), line, fill=(0, 0, 0))

            meme_img.save(os.path.join(temp_dir, meme_name), format="PNG")
    return meme_name


def build_kids(orig_img, temp_dir, file_id, req) -> dict:
    """Randomised sticker overlay plus a matching caption."""
    # 4. Kids with Emojis & Captions (Randomized stickers & matching captions)
    kids_name = f"temp_transform_{file_id}_kids.png"
    kids_caption = "KIDS ZONE: Playtime!"
    kids_stickers = []
    caption_x = 0.5
    caption_y = 0.85
    caption_color = [255, 105, 180]
    if req.type in ("all", "kids"):
        import json
        import random
        kids_img = orig_img.copy()
        w, h = orig_img.size
        scale = max(w, h) / 800.0

        api_key = os.getenv("GEMINI_API_KEY")
        kids_generated_by_api = False

        if api_key:
            try:
                client = get_gemini_client()

                # Step 1: Planning Kids decoration using gemini-3.5-flash
                logger.info("Step 1: Planning Kids decoration using gemini-3.5-flash...")
                analysis_prompt = (
                    "Analyze this photo. Imagine a young kid (5-8 years old) is decorating this photo. "
                    "1. Generate a short, happy, kid-friendly caption matching the photo (no emojis, plain text only). "
                    "CRITICAL: Do NOT just literally describe background objects (like a pillow, floor, blanket, wall, or table). "
                    "Instead, write something cute, active, or imaginative.\n"
                    "2. Suggest caption center position coordinates (caption_x, caption_y) near the middle-lower region (e.g. y = 0.65 to 0.72) where it contrasts well, avoids overlapping with bottom overlays, and doesn't block subjects.\n"
                    "3. Suggest a fun RGB color for the caption text (caption_color) that contrast well with the photo behind it.\n"
                    "4. Suggest exactly 1 or 2 context-relevant emojis/stickers to add.\n"
                    "5. Provide precise normalized coordinates (x, y) from 0.0 to 1.0 for each sticker so they don't cover main subjects (e.g. crown on head, sleep next to head).\n"
                    "Output EXACTLY a JSON object with this schema and nothing else:\n"
                    "{\n"
                    "  \"caption\": \"Generated caption matching the photo\",\n"
                    "  \"caption_x\": 0.5,\n"
                    "  \"caption_y\": 0.70,\n"
                    "  \"caption_color\": [255, 105, 180],\n"
                    "  \"stickers\": [\n"
                    "    {\n"
                    "      \"type\": \"👑\",\n"
                    "      \"x\": 0.65,\n"
                    "      \"y\": 0.4,\n"
                    "      \"color\": [255, 215, 0],\n"
                    "      \"size\": 1.2\n"
                    "    }\n"
                    "  ]\n"
                    "}"
                )

                response1 = client.models.generate_content(
                    model=PIPELINE_MODEL,
                    contents=[orig_img, analysis_prompt]
                )

                text = response1.text
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                data = json.loads(text.strip())

                kids_caption = data.get("caption", "KIDS ZONE: Playtime!")
                caption_x = data.get("caption_x", 0.5)
                caption_y = data.get("caption_y", 0.70)
                caption_color = data.get("caption_color", [255, 105, 180])
                stickers_list = data.get("stickers", [])

                # Step 2: Use Nano Banana 2 (gemini-3.1-flash-image) to generate the high-quality edited image
                logger.info("Step 2: Generating high-quality decorated image using Nano Banana 2...")
                sticker_insts = []
                for s in stickers_list:
                    stype = s.get("type", "🌟")
                    sx = s.get("x", 0.5)
                    sy = s.get("y", 0.5)
                    ss = s.get("size", 1.0)
                    sticker_insts.append(f"Place a '{stype}' sticker (size multiplier {ss}) at normalized coordinates (x={sx}, y={sy}) on the photo.")

                edit_prompt = (
                    f"Edit the uploaded photo as if a kid decorated it.\n"
                    f"1. Write the caption: \"{kids_caption}\" near the bottom center at coordinates (x={caption_x}, y={caption_y}) in a fun, handwritten/cursive font with a clear contrasting color (RGB: {caption_color}).\n"
                    f"CRITICAL: Write the caption text directly onto the image pixels. Do NOT draw any background blocks, translucent bars, text banners, or dashed border lines behind/under the caption text.\n"
                    f"2. Paint the stickers/emojis at the exact requested coordinates:\n"
                    f"{chr(10).join(sticker_insts)}\n"
                    f"Style: High-quality kid-decorated photo. The added elements should look cute, playful, have a clean white sticker outline, and blend naturally."
                )

                response2 = client.models.generate_content(
                    model=IMAGE_MODEL,
                    contents=[orig_img, edit_prompt]
                )

                img_bytes = None
                for part in response2.candidates[0].content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        img_bytes = part.inline_data.data
                        break

                if img_bytes:
                    kids_img = Image.open(io.BytesIO(img_bytes))
                    kids_generated_by_api = True
                    logger.info("Successfully generated high-quality Kids decoration via Nano Banana 2!")
                    kids_stickers = stickers_list
            except Exception as ex:
                logger.warning(f"Two-step Gemini Kids decoration failed: {ex}. Falling back to PIL drawing.")
        if not kids_generated_by_api:
            if not kids_stickers:
                themes = ["balloons", "stars", "smiles", "celebration"]
                chosen_theme = random.choice(themes)
                if chosen_theme == "balloons":
                    kids_caption = "KIDS ZONE: Up, up and away!"
                    kids_stickers = [
                        {"type": "🎈", "x": 0.2, "y": 0.15, "color": [255, 99, 71], "size": 1.1},
                        {"type": "🎈", "x": 0.8, "y": 0.18, "color": [50, 205, 50], "size": 0.9}
                    ]
                elif chosen_theme == "stars":
                    kids_caption = "KIDS ZONE: Shine bright!"
                    kids_stickers = [
                        {"type": "🌟", "x": 0.15, "y": 0.1, "color": [255, 215, 0], "size": 1.0},
                        {"type": "✨", "x": 0.85, "y": 0.15, "color": [0, 255, 255], "size": 0.8}
                    ]
                elif chosen_theme == "smiles":
                    kids_caption = "KIDS ZONE: Keep smiling!"
                    kids_stickers = [
                        {"type": "😊", "x": 0.3, "y": 0.2, "color": [255, 235, 59], "size": 1.1},
                        {"type": "💖", "x": 0.7, "y": 0.25, "color": [255, 105, 180], "size": 1.0}
                    ]
                else:
                    kids_caption = "KIDS ZONE: Time to celebrate!"
                    kids_stickers = [
                        {"type": "🎉", "x": 0.2, "y": 0.15, "color": [255, 215, 0], "size": 1.0},
                        {"type": "🥳", "x": 0.8, "y": 0.2, "color": [255, 235, 59], "size": 1.0}
                    ]
            draw_kids_stickers_and_banner(kids_img, kids_caption, kids_stickers, scale, caption_x, caption_y, caption_color)

        kids_img.save(os.path.join(temp_dir, kids_name), format="PNG")
    return {
        "filename": kids_name,
        "kids_caption": kids_caption,
        "caption_x": caption_x,
        "caption_y": caption_y,
        "caption_color": caption_color,
        "stickers": kids_stickers,
    }


def build_enhanced(orig_img, temp_dir, file_id, req) -> str:
    """Contrast / colour / brightness / sharpness correction."""
    # 5. Photogenic Correction
    enhanced_name = f"temp_transform_{file_id}_enhanced.png"
    if req.type in ("all", "enhanced"):
        corrected = orig_img.copy()
        corrected = ImageEnhance.Contrast(corrected).enhance(1.25)
        corrected = ImageEnhance.Color(corrected).enhance(1.35)
        corrected = ImageEnhance.Brightness(corrected).enhance(1.08)
        corrected = ImageEnhance.Sharpness(corrected).enhance(1.15)
        corrected.save(os.path.join(temp_dir, enhanced_name), format="PNG")
    return enhanced_name
