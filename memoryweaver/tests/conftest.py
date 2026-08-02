"""Shared fixtures.

The important one is :func:`fake_gemini`. Rather than stubbing out the agent
tools wholesale, it replaces only the Gemini *client*, so tests still execute
the real prompt assembly, response parsing, score arithmetic, and fallback
logic. That is where the bugs actually live - the previous suite mocked at too
high a level to reach any of it, which is why parsing regressions were
invisible.
"""

import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeResponse:
    """Mimics the shape of a google-genai GenerateContentResponse."""

    def __init__(self, text: str):
        self.text = text


class FakeModels:
    def __init__(self, owner: "FakeGeminiClient"):
        self._owner = owner

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        self._owner.calls.append({"model": model, "contents": contents, "config": config})
        if self._owner.error is not None:
            raise self._owner.error
        if not self._owner.responses:
            raise AssertionError("FakeGeminiClient ran out of scripted responses")
        nxt = self._owner.responses.pop(0)
        return FakeResponse(nxt)

    def embed_content(self, model=None, contents=None, **kwargs):
        self._owner.calls.append({"model": model, "contents": contents})
        if self._owner.error is not None:
            raise self._owner.error
        return self._owner.embedding


class FakeGeminiClient:
    """Scriptable stand-in for genai.Client."""

    def __init__(self):
        self.responses: list[str] = []
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.embedding = None
        self.models = FakeModels(self)

    def script(self, *texts: str) -> "FakeGeminiClient":
        self.responses.extend(texts)
        return self

    def fail_with(self, exc: Exception) -> "FakeGeminiClient":
        self.error = exc
        return self

    @property
    def last_prompt(self) -> str:
        """Flattened text of the most recent call, for assertions on prompts."""
        if not self.calls:
            return ""
        contents = self.calls[-1]["contents"]
        if isinstance(contents, str):
            return contents
        return "\n".join(c for c in contents if isinstance(c, str))


@pytest.fixture
def fake_gemini(monkeypatch):
    """Swaps the shared Gemini client for a scriptable fake, everywhere.

    Each agent tool imported `get_gemini_client` into its own namespace, so the
    patch is applied per-module as well as at the source.
    """
    client = FakeGeminiClient()

    import app.app_utils.genai_client as gc

    monkeypatch.setattr(gc, "get_gemini_client", lambda: client)

    for module_path in (
        # Routers and services call the model directly too; without these the
        # fixture silently let real API calls through and tests failed on a
        # 400 INVALID_ARGUMENT instead of exercising the code under test.
        "app.fast_api_app",
        "app.routers.cleaner",
        "app.routers.photo",
        "app.routers.video",
        "app.services.classification",
        "app.services.transforms",
        "app.services.video_ops",
    ):
        try:
            module = __import__(module_path, fromlist=["get_gemini_client"])
        except ImportError:
            continue
        if hasattr(module, "get_gemini_client"):
            monkeypatch.setattr(module, "get_gemini_client", lambda: client)

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    return client


@pytest.fixture
def photo_factory(tmp_path):
    """Creates small real JPEGs on disk; the tools open and re-encode them."""

    def _make(name: str = "photo.jpg", color: str = "red", size=(64, 64)) -> str:
        path = tmp_path / name
        Image.new("RGB", size, color).save(path, format="JPEG")
        return str(path)

    return _make


@pytest.fixture
def jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    """Keeps tests off the developer's real local_storage tree."""
    monkeypatch.setenv("MW_ENCRYPTION_KEY", "conftest-test-key")
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    yield


@pytest.fixture
def no_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import app.app_utils.genai_client as gc
    gc.reset_client_cache()
    yield
    gc.reset_client_cache()


def pytest_configure(config):
    os.environ.setdefault("MW_ENCRYPTION_KEY", "pytest-default-key")
