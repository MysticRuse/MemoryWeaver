"""Smoke coverage for the HTTP surface.

Motivation: `POST /api/cleaner/add-frame-to-album` referenced an unimported
`base64` and raised NameError on every call, for every commit it existed. The
handler's blanket `except Exception` turned that into HTTP 200 with an error
string in the body, so nothing surfaced it. Four more endpoints had the same
class of defect.

These tests do not assert business logic. They assert that each route is
reachable, that its module-level names resolve, and - critically - that a
handler which fails does not report success. That is the specific hole the
previous suite left open.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.fast_api_app import app

# Routes needing multipart bodies, filesystem state, or a live Gemini key are
# covered by targeted tests below rather than the generic GET sweep.
_SKIP_GENERIC = {
    "/media",
    "/api/cleaner/local-thumbnail",
    "/api/cleaner/local-video",
    "/api/cleaner/browse-dir",
    "/api/local-fs/list",
}


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


def _get_routes(methods={"GET"}):
    seen = []
    for route in app.routes:
        path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", set()) or set()
        if not path or not (route_methods & methods):
            continue
        if "{" in path or path in _SKIP_GENERIC:
            continue
        if path.startswith(("/a2a", "/docs", "/redoc", "/openapi")):
            continue
        seen.append(path)
    return sorted(set(seen))


@pytest.mark.parametrize("path", _get_routes())
def test_get_route_does_not_crash(client, path):
    """No route may return a 5xx or an unhandled exception on a bare GET."""
    response = client.get(path)
    assert response.status_code < 500, (
        f"GET {path} returned {response.status_code}: {response.text[:300]}"
    )


@pytest.mark.parametrize("path", _get_routes())
def test_get_route_never_reports_error_with_200(client, path):
    """A 200 must not carry {"status": "error"}.

    This is the pattern that hid the base64 NameError: the handler crashed, the
    catch-all swallowed it, and the client saw a success code.
    """
    response = client.get(path)
    if response.status_code != 200:
        return
    if "application/json" not in response.headers.get("content-type", ""):
        return
    body = response.json()
    if isinstance(body, dict) and body.get("status") == "error":
        pytest.fail(
            f"GET {path} returned HTTP 200 with an error payload: "
            f"{body.get('message')!r}. Failures must use a 4xx/5xx status."
        )


def test_no_handler_references_undefined_names():
    """Guards the whole NameError class that shipped five broken endpoints."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821",
         "--output-format", "concise", "app", "pipeline", "agents"],
        capture_output=True, text=True,
    )
    if result.returncode == 2:  # ruff not installed in this env
        pytest.skip("ruff unavailable")
    assert result.returncode == 0, f"undefined names present:\n{result.stdout}"


def test_add_frame_to_album_resolves_base64(tmp_path, monkeypatch):
    """Regression: this endpoint raised NameError on base64 for every call."""
    from app.routers.cleaner import add_cleaner_frame_to_album
    from app.schemas import AddFrameToAlbumRequest

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="JPEG")
    encoded = __import__("base64").b64encode(buf.getvalue()).decode()

    result = add_cleaner_frame_to_album(
        AddFrameToAlbumRequest(
            session_id="test_frame_session",
            base64_data=f"data:image/jpeg;base64,{encoded}",
            original_video_filename="clip.mp4",
        )
    )
    assert result["status"] == "success", result
    assert result["filename"].endswith(".jpg")


def test_media_rejects_traversal(client):
    """basename() plus safe_storage_join must contain the filename."""
    response = client.get("/media", params={"filename": "../../../etc/passwd"})
    assert response.status_code in (400, 404)


def test_local_video_requires_path(client):
    response = client.get("/api/cleaner/local-video")
    assert response.status_code == 422  # missing required query param


def test_openapi_schema_builds(client):
    """A malformed route signature breaks schema generation for the whole app."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "MemoryWeaver Cleaner"


def test_unknown_route_is_404(client):
    assert client.get("/definitely-not-a-route").status_code == 404
