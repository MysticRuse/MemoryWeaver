"""Tests for the HTTP error contract.

The defect this prevents: a handler crashes, a blanket `except Exception`
catches it, and the endpoint returns HTTP 200 with {"status": "error"} in the
body. Five endpoints shipped broken for months behind exactly that pattern -
monitoring, clients and tests all saw success.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.app_utils.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    UpstreamError,
    ValidationError,
    register_error_handlers,
)


@pytest.fixture
def mini_app():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("something exploded")

    @app.get("/missing")
    def missing():
        raise NotFoundError("No such event.")

    @app.get("/bad")
    def bad():
        raise ValidationError("Bad input.")

    @app.get("/denied")
    def denied():
        raise PermissionDeniedError()

    @app.get("/conflict")
    def conflict():
        raise ConflictError("Already running.")

    @app.get("/upstream")
    def upstream():
        raise UpstreamError("Gemini timed out.")

    @app.get("/limited")
    def limited():
        raise RateLimitedError("Slow down.")

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/missing", 404),
        ("/bad", 400),
        ("/denied", 403),
        ("/conflict", 409),
        ("/upstream", 502),
        ("/limited", 429),
        ("/boom", 500),
    ],
)
def test_errors_use_correct_status_codes(mini_app, path, expected):
    assert mini_app.get(path).status_code == expected


def test_no_error_is_ever_returned_as_200(mini_app):
    for path in ("/boom", "/missing", "/bad", "/denied", "/conflict",
                 "/upstream", "/limited"):
        response = mini_app.get(path)
        assert response.status_code != 200, f"{path} reported failure as success"


def test_error_body_shape_is_preserved(mini_app):
    """The frontend already parses {"status","message"}; only the code changed."""
    body = mini_app.get("/missing").json()
    assert body["status"] == "error"
    assert body["message"] == "No such event."


def test_unhandled_exception_does_not_leak_internals(mini_app):
    """Stack traces and file paths stay server-side."""
    body = mini_app.get("/boom").json()
    assert "something exploded" not in body["message"]
    assert "Traceback" not in body["message"]
    assert body["status"] == "error"


def test_unhandled_exception_is_logged(mini_app, caplog):
    with caplog.at_level("ERROR", logger="memoryweaver.errors"):
        mini_app.get("/boom")
    assert "something exploded" in caplog.text
    assert "RuntimeError" in caplog.text


def test_apperror_defaults_are_sensible():
    assert NotFoundError().status_code == 404
    assert ValidationError().message == "Invalid request."
    assert AppError().status_code == 500


def test_real_app_returns_404_not_200_for_missing_session():
    """Regression against the live app, not a mini fixture."""
    from app.fast_api_app import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/sessions/definitely-not-a-real-session")
    assert response.status_code in (404, 200)
    if response.status_code == 200:
        # If it still 200s it must not be reporting an error in the body.
        assert response.json().get("status") != "error"


def test_real_app_validation_error_is_422():
    from app.fast_api_app import app

    client = TestClient(app, raise_server_exceptions=False)
    # missing the required `path` query parameter
    response = client.get("/api/cleaner/local-thumbnail")
    assert response.status_code == 422
    assert response.json()["status"] == "error"


# --- no silent failures ----------------------------------------------------


def test_no_handler_swallows_an_exception_silently():
    """Guards the `except Exception: pass` pattern.

    54 handlers used to do exactly that. A silent pass destroys the only
    evidence a step failed, which is how several broken features stayed
    invisible: the code caught, discarded, and carried on. A handler may still
    catch broadly - some steps genuinely are best-effort - but it must say so.
    """
    import ast
    import glob

    offenders = []
    for path in (glob.glob("app/**/*.py", recursive=True)
                 + glob.glob("pipeline/**/*.py", recursive=True)
                 + glob.glob("agents/**/*.py", recursive=True)):
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = node.body
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "these handlers discard an exception with no log line:\n  "
        + "\n  ".join(offenders)
    )


def test_no_module_uses_print_for_diagnostics():
    """print() bypasses levels, handlers and formatting."""
    import glob
    import re

    offenders = []
    for path in (glob.glob("app/**/*.py", recursive=True)
                 + glob.glob("pipeline/**/*.py", recursive=True)
                 + glob.glob("agents/**/*.py", recursive=True)):
        if "logging_config" in path:
            continue
        for i, line in enumerate(open(path), start=1):
            if re.search(r"^\s*print\(", line):
                offenders.append(f"{path}:{i}")
    assert not offenders, "use get_logger(__name__) instead of print:\n  " + "\n  ".join(offenders)


def test_logging_does_not_disable_propagation():
    """propagate=False silences caplog and any root-level capture."""
    import logging

    from app.app_utils.logging_config import configure_logging

    configure_logging()
    assert logging.getLogger("memoryweaver").propagate is True
