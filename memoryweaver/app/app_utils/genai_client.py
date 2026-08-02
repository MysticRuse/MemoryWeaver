"""Single source of truth for the Gemini client and model IDs.

Previously ``genai.Client(api_key=...)`` was constructed in 24 places (18 of
them inline inside request handlers in fast_api_app.py) and five modules each
defined their own identical ``get_gemini_client``. Model IDs were string
literals scattered across the codebase, which is how the same logical role
ended up pinned to different models - a commit claiming to "update all Gemini
model references" left several behind.

Models are grouped by *role* here, so changing the model for a role is a
one-line edit. Each is overridable by environment variable for staged rollouts
without a code change.
"""

from __future__ import annotations

import os
import threading

from google import genai

# --- Model roles -----------------------------------------------------------
# Conversational ADK agents (concierge and the five specialists).
AGENT_MODEL = os.environ.get("MW_AGENT_MODEL", "gemini-flash-latest")

# Pipeline tool calls: vision moderation, LLM-as-judge scoring, captioning,
# journal and story narration, OCR, video description.
PIPELINE_MODEL = os.environ.get("MW_PIPELINE_MODEL", "gemini-3-flash-preview")

# Text-to-speech; must be a model exposing the AUDIO response modality.
TTS_MODEL = os.environ.get("MW_TTS_MODEL", "gemini-2.5-flash-preview-tts")

# Image generation / editing (nano-banana style overlays and transformations).
IMAGE_MODEL = os.environ.get("MW_IMAGE_MODEL", "gemini-3.1-flash-image")

# Multimodal embeddings used for burst deduplication.
EMBEDDING_MODEL = os.environ.get("MW_EMBEDDING_MODEL", "multimodalembedding")

_client: genai.Client | None = None
_lock = threading.Lock()


class MissingAPIKeyError(RuntimeError):
    """Raised when GEMINI_API_KEY is absent."""


def get_gemini_client() -> genai.Client:
    """Returns a process-wide Gemini client.

    The client is cached: it holds a connection pool, so rebuilding it per
    request (the previous behaviour) wasted sockets and TLS handshakes.

    Raises:
        MissingAPIKeyError: if GEMINI_API_KEY is not set.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                api_key = os.getenv("GEMINI_API_KEY")
                if not api_key:
                    raise MissingAPIKeyError(
                        "GEMINI_API_KEY environment variable is not set."
                    )
                _client = genai.Client(api_key=api_key)
    return _client


def reset_client_cache() -> None:
    """Drops the cached client. For tests that swap the API key."""
    global _client
    with _lock:
        _client = None


