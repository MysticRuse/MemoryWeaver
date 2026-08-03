"""Storage, encryption and image helpers shared by the routers.

These lived in fast_api_app.py, which meant any router that needed them
would have had to import the application module and create an import
cycle. They are pure helpers, so they belong in a service layer.
"""

import io
import os
import re

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from app.app_utils.crypto import DecryptionError, decrypt_bytes, encrypt_bytes
from app.app_utils.logging_config import get_logger

logger = get_logger(__name__)

def get_global_vault_file_path() -> str:
    # Three levels up: app/services/media_store.py -> app/services -> app -> memoryweaver.
    # Two levels landed on app/local_storage, an empty directory the vault was never
    # written to, so every photo came back unclassified and fell into Other / Misc.
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    storage_root = os.path.join(project_root, "local_storage")
    os.makedirs(storage_root, exist_ok=True)
    return os.path.join(storage_root, "cleaner_vault.json")


def humanize_caption(caption: str) -> str:
    if not caption:
        return ""

    c_lower = caption.lower()

    # Check if the entire caption is just a technical fallback warning
    if "low sharpness score" in c_lower or "focus issue" in c_lower or "blurry" in c_lower:
        return "A beautiful memory from our trip! ✈️"
    if "junk screen capture" in c_lower or "device screenshot" in c_lower or "system prompt" in c_lower:
        return "A quick snapshot 📱"
    if "meme format" in c_lower or "joke photo" in c_lower:
        return "Meme time! 😂"
    if "general photo" in c_lower or "general picture" in c_lower:
        return "A beautiful memory from our trip! ✈️"

    cleaned = caption

    # Strip well-known technical prefixes
    prefixes = [
        "Clear, well-composed photograph of ",
        "Clear personal photo of ",
        "Clear photograph of ",
        "Clear photo of ",
        "Clear image of ",
        "Clear picture of ",
        "Well-composed photograph of ",
        "Detected screenshot containing ",
        "Detected screenshot of ",
        "Screenshot containing ",
        "Screenshot of ",
        "Detected ",
        "A photo of ",
        "A picture of ",
        "An image of ",
        "Photograph of ",
        "Picture of ",
        "Image of ",
    ]
    for p in prefixes:
        if cleaned.lower().startswith(p.lower()):
            cleaned = cleaned[len(p):]
            if cleaned:
                cleaned = cleaned[0].upper() + cleaned[1:]

    # Strip common technical suffixes or clauses
    suffixes_to_strip = [
        " for a family journal",
        " for the family album",
        " for the album",
        " for verification",
        " for safety review",
        " for quality checking",
        " for screening",
        " (Smart Heuristic Fallback)",
        " [Self-Healed]",
    ]
    for s in suffixes_to_strip:
        if cleaned.lower().endswith(s.lower()):
            cleaned = cleaned[:-len(s)]

    # Clean up inline dry technical jargon phrases
    jargon_phrases = [
        "that is appropriate and safe to share",
        "that is appropriate and safe",
        "appropriate for the album",
        "safe to share",
        "suitable for sharing",
        "suitable for the album",
        "for the trip journal",
    ]
    for phrase in jargon_phrases:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        cleaned = pattern.sub("", cleaned)

    # Strip stray commas, spaces, or periods at the end and clean up double spaces
    cleaned = cleaned.strip(" ,.")
    cleaned = re.sub(r'\s+', ' ', cleaned)

    # Ensure it starts with capital and ends with a nice period or emoji
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
        has_emoji = any(ord(char) > 0x7F for char in cleaned)
        if not cleaned.endswith((".", "!", "?")) and not has_emoji:
            cleaned += "."

    if not cleaned or len(cleaned) < 3:
        return "A beautiful memory from our trip! ✈️"

    return cleaned


def get_file_sha256(filepath: str) -> str:
    import hashlib
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            buf = f.read(65536)
            while len(buf) > 0:
                hasher.update(buf)
                buf = f.read(65536)
        return hasher.hexdigest()
    except Exception:
        return ""


def trash_file_safely(filepath: str):
    """
    Sends a file to the native system Trash on macOS (via Cocoa or send2trash),
    Recycle Bin on Windows, and fallback equivalent behaviors on iOS/Android.
    """
    if not os.path.exists(filepath):
        return False
    import send2trash
    try:
        send2trash.send2trash(filepath)
        return True
    except Exception:
        try:
            os.remove(filepath)
            return True
        except Exception:
            return False


def apply_enhancements(image_path, out_path, brightness: float, contrast: float, saturation: float, sharpness: float, warmth: float, session_id: str = "default"):
    """
    Applies brightness, contrast, saturation, sharpness, and warmth enhancements.
    All factors are floats, where 1.0 means no change.
    """

    decrypted_bytes = load_image_bytes_decrypted(image_path, session_id)
    with Image.open(io.BytesIO(decrypted_bytes)) as img:
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGB')

        # 1. Brightness
        if brightness != 1.0:
            enhancer = ImageEnhance.Brightness(img)
            img = enhancer.enhance(brightness)

        # 2. Contrast
        if contrast != 1.0:
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(contrast)

        # 3. Saturation (Color)
        if saturation != 1.0:
            enhancer = ImageEnhance.Color(img)
            img = enhancer.enhance(saturation)

        # 4. Sharpness
        if sharpness != 1.0:
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(sharpness)

        # 5. Warmth (shifting red channel up and blue channel down)
        if warmth != 1.0:
            r, g, b = img.split()
            r_data = r.point(lambda i: min(255, max(0, int(i * warmth))))
            b_data = b.point(lambda i: min(255, max(0, int(i * (2.0 - warmth)))))
            img = Image.merge('RGB', (r_data, g, b_data))

        # Save output image copy encrypted at rest
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        encrypted_bytes = encrypt_file_bytes(buf.getvalue(), session_id)
        with open(out_path, "wb") as f_out:
            f_out.write(encrypted_bytes)


def draw_kids_stickers_and_banner(img, caption_text: str, stickers: list, scale: float, caption_x: float = 0.5, caption_y: float = 0.78, caption_color: list | None = None):
    import os
    import random

    draw = ImageDraw.Draw(img)
    w, h = img.size

    # Restrict stickers to a maximum of 2 to keep the photo clean and unblocked
    active_stickers = stickers[:2] if stickers else []

    # Standard sizes for Apple Color Emoji bitmap rendering on macOS
    std_sizes = [20, 26, 32, 40, 48, 52, 64, 96, 160]
    emoji_font_path = "/System/Library/Fonts/Apple Color Emoji.ttc"

    # 1. Draw stickers
    for s in active_stickers:
        stype = s.get("type", "star")
        sx = int(s.get("x", 0.5) * w)
        sy = int(s.get("y", 0.5) * h)
        # Moderate sticker size
        ssize = int(s.get("size", 1.0) * 40 * scale)

        # Map shape names to actual high-quality color emoji characters
        emoji_map = {
            "balloon": "🎈",
            "star": "🌟",
            "smiley": "😊",
            "heart": "❤️",
            "sun": "☀️",
            "crown": "👑"
        }
        emoji_char = emoji_map.get(stype.lower(), stype)

        try:
            if os.path.exists(emoji_font_path):
                # Snap font size to valid Apple Color Emoji bitmap size
                snap_size = min(std_sizes, key=lambda val: abs(val - ssize))
                emoji_font = ImageFont.truetype(emoji_font_path, snap_size)

                # Render emoji on temporary transparent canvas to allow precise Lanczos scaling
                bbox = draw.textbbox((0, 0), emoji_char, font=emoji_font)
                bx1, by1, bx2, by2 = bbox
                bw = max(1, bx2 - bx1 + 10)
                bh = max(1, by2 - by1 + 10)

                temp_img = Image.new("RGBA", (bw * 2, bh * 2), (0, 0, 0, 0))
                temp_draw = ImageDraw.Draw(temp_img)
                temp_draw.text((bw - bx1, bh - by1), emoji_char, font=emoji_font, embedded_color=True)

                temp_bbox = temp_img.getbbox()
                if temp_bbox:
                    cropped = temp_img.crop(temp_bbox)
                    cw, ch = cropped.size
                    aspect = cw / ch
                    if cw > ch:
                        new_w = ssize
                        new_h = int(ssize / aspect)
                    else:
                        new_h = ssize
                        new_w = int(ssize * aspect)
                    resized_emoji = cropped.resize((max(1, new_w), max(1, new_h)), Image.Resampling.LANCZOS)
                    # Paste emoji onto main image with transparent alpha masking
                    img.paste(resized_emoji, (sx - new_w // 2, sy - new_h // 2), resized_emoji)
            else:
                draw.text((sx - ssize // 2, sy - ssize // 2), emoji_char)
        except Exception as ee:
            logger.warning(f"Failed to draw emoji sticker {emoji_char}: {ee}")
    # 2. Select a random fun font for the caption
    font = None
    font_paths = [
        "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
        "/System/Library/Fonts/Supplemental/Comic Sans MS Bold.ttf",
        "/System/Library/Fonts/Supplemental/Chalkboard.ttc",
        "/System/Library/Fonts/Supplemental/ChalkboardSE.ttc",
        "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf"
    ]
    existing_fonts = [fp for fp in font_paths if os.path.exists(fp)]
    chosen_font_path = random.choice(existing_fonts) if existing_fonts else "/System/Library/Fonts/Supplemental/Arial.ttf"

    font_size = max(24, int(45 * scale))
    try:
        font = ImageFont.truetype(chosen_font_path, font_size)
    except Exception as fe:
        logger.warning(f"Random font loading failed, using default: {fe}")
    # 3. Calculate text bounding box to center it
    # Clamp caption positioning to be higher (y = 0.20 to 0.72) to guarantee a significant gap from bottom pillow overlay
    safe_y = max(0.20, min(0.72, caption_y if caption_y is not None else 0.70))
    cx = int(caption_x * w)
    cy = int(safe_y * h)

    # Check if caption contains emojis, and use Apple Color Emoji if so
    has_emojis = any(ord(char) > 127 for char in caption_text)
    if has_emojis and os.path.exists(emoji_font_path):
        try:
            # Snap font size to a standard size to avoid FreeType pixel size errors!
            font_size = min(std_sizes, key=lambda val: abs(val - font_size))
            font = ImageFont.truetype(emoji_font_path, font_size)
        except Exception as exc:
            logger.warning("%s: best-effort step failed, continuing: %s", "media_store", exc)

    if font:
        bbox = draw.textbbox((0, 0), caption_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    else:
        text_w = len(caption_text) * int(14 * scale)
        text_h = int(18 * scale)

    tx = int(cx - text_w / 2)
    ty = int(cy - text_h / 2)

    # 4. Draw high-contrast text directly on the image (NO BACKGROUND)
    text_color = tuple(caption_color) if caption_color else (255, 105, 180)
    brightness = 0.299 * text_color[0] + 0.587 * text_color[1] + 0.114 * text_color[2]
    stroke_color = (0, 0, 0) if brightness > 128 else (255, 255, 255)

    draw.text((tx, ty), caption_text, fill=text_color, font=font,
              stroke_width=max(2, int(scale * 2.2)), stroke_fill=stroke_color)




def load_image_bytes_decrypted(filepath: str, session_id: str) -> bytes:
    """Reads a stored media file and returns its plaintext.

    Delegates to app_utils.crypto, which understands v2 blobs, legacy v1 blobs,
    and files the ingest paths stored unencrypted. Returns b"" on a genuine
    authentication failure rather than the old behaviour of handing back the
    raw ciphertext.
    """
    try:
        with open(filepath, "rb") as f:
            file_bytes = f.read()
    except OSError as e:
        logger.info(f"[media] unreadable file {filepath}: {e}")
        return b""

    try:
        return decrypt_bytes(file_bytes, session_id)
    except DecryptionError as e:
        # Fail closed. Serving the raw bytes here is what made the previous
        # implementation indistinguishable from having no encryption at all.
        logger.info(f"[media] refusing to serve undecryptable file {filepath}: {e}")
        return b""


def encrypt_file_bytes(file_bytes: bytes, session_id: str) -> bytes:
    """Encrypts media for storage. See app_utils.crypto for the format."""
    return encrypt_bytes(file_bytes, session_id)
