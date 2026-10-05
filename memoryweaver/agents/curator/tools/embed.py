import os
import io
import math
from google import genai
from google.genai import types
from PIL import Image

# Image embedding model used for near-duplicate detection.
#
# History: this used to be "multimodalembedding", a Vertex AI model name that
# the Gemini API (AI Studio key) answers with 404. Every call silently fell
# back to a fake file-size vector, so duplicate detection never worked.
# "gemini-embedding-2" accepts images on the Gemini API.
#
# 768 dims (the model's default is 3072) via output_dimensionality: measured on
# real photos it separates near-duplicates from merely-similar shots exactly as
# well as 3072, with a 4x smaller per-photo cache and faster comparisons.
EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIM = 768
# Stored next to each cached embedding. Cached vectors are only reused when this
# matches, so switching model/dimension can never mix incompatible vectors.
EMBEDDING_TAG = f"{EMBEDDING_MODEL}:{EMBEDDING_DIM}"

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)

def get_image_embedding(photo_path: str) -> list[float]:
    """
    Generates a multimodal vector embedding for an image using Gemini.
    """
    return get_image_embedding_checked(photo_path)[0]


def get_image_embedding_checked(photo_path: str) -> tuple[list[float], bool]:
    """
    Like get_image_embedding, but also reports whether the vector is the
    placeholder fallback used when the embedding API call fails.

    Returns (vector, is_fallback). Callers that persist embeddings (the
    pipeline's per-photo cache) must not store a fallback vector: it carries no
    visual information, and caching it would pin the photo to a fake vector
    even after the API problem is fixed.
    """
    client = get_gemini_client()
    
    # 1. Resize and normalise photo to reduce token usage
    img = Image.open(photo_path).convert("RGB")
    img.thumbnail((512, 512), Image.Resampling.LANCZOS)
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")

    try:
        # Request multimodal embedding
        response = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=img_part,
            config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
        )
        # Extract vector list from response
        embedding = response.embeddings[0].values
        return [float(x) for x in embedding], False
    except Exception as e:
        print(f"Embedding API error: {e}. Falling back to mock/text-based vector mapping.")
        # Fallback: Generate a pseudo-vector based on file properties to allow local testing
        file_size = os.path.getsize(photo_path)
        mock_vec = []
        for i in range(128):
            mock_vec.append(math.sin(file_size * (i + 1)) * 0.1)
        return mock_vec, True

def calculate_cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculates the cosine similarity score between two vector lists."""
    dot_product = sum(x * y for x, y in zip(vec1, vec2))
    norm_a = math.sqrt(sum(x * x for x in vec1))
    norm_b = math.sqrt(sum(y * y for y in vec2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)
