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
from google.genai import types

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


# --- Cost guardrails -------------------------------------------------------
# Every text call in this app returns a small JSON blob or a one-line caption,
# so a cap well above what any of them legitimately need still bounds a
# runaway generation. On Gemini 2.5+ thinking tokens count against this too,
# which is the point: a model stuck in a reasoning loop stops costing money at
# the cap instead of at the invoice.
def _env_int(name: str, fallback: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


MAX_OUTPUT_TOKENS = _env_int("MW_MAX_OUTPUT_TOKENS", 4096)

# Unbounded retries re-bill on every attempt; half the damage in the widely
# reported "$50,000 weekend" came from exactly that. Bound it explicitly here
# rather than inheriting whatever the SDK default happens to be.
RETRY_ATTEMPTS = _env_int("MW_AI_RETRY_ATTEMPTS", 3)
REQUEST_TIMEOUT_MS = _env_int("MW_AI_TIMEOUT_MS", 120_000)

_client: genai.Client | None = None
_lock = threading.Lock()


def text_config(**overrides) -> types.GenerateContentConfig:
    """Config for a text/JSON generation call, with the output cap applied.

    Pass this as ``config=`` on every PIPELINE_MODEL call. Image-generation
    calls deliberately do not use it: their output is inline image data, not
    text, and a token cap there truncates the image rather than bounding cost.
    """
    overrides.setdefault("max_output_tokens", MAX_OUTPUT_TOKENS)
    return types.GenerateContentConfig(**overrides)


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
                _client = genai.Client(
                    api_key=api_key,
                    http_options=types.HttpOptions(
                        timeout=REQUEST_TIMEOUT_MS,
                        retry_options=types.HttpRetryOptions(
                            attempts=RETRY_ATTEMPTS,
                            initial_delay=1.0,
                            max_delay=8.0,
                            exp_base=2.0,
                            jitter=1.0,
                        ),
                    ),
                )
    return _client


def reset_client_cache() -> None:
    """Drops the cached client. For tests that swap the API key."""
    global _client
    with _lock:
        _client = None


