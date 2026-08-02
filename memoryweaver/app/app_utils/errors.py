"""Error contract for the HTTP layer.

The handlers in fast_api_app.py were written as::

    try:
        ...
    except Exception as e:
        return {"status": "error", "message": str(e)}

which returns **HTTP 200** for a server-side failure. That is what allowed five
endpoints with `NameError` to ship unnoticed: the crash was caught, wrapped, and
reported as a success at the protocol level. Clients, monitoring and tests all
saw 200.

This module provides:

* a small typed exception hierarchy for the failures the app actually has, and
* exception handlers that render them as JSON with the right status code,
  preserving the ``{"status": "error", "message": ...}`` body shape the existing
  frontend already parses, so nothing in the UI needs to change.

Handlers should ``raise`` these instead of returning error dicts. Anything
uncaught becomes a 500 with the detail logged server-side and a generic message
to the client.
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("memoryweaver.errors")


class AppError(Exception):
    """Base for expected, reportable failures. Renders with ``status_code``."""

    status_code = 500
    default_message = "Unexpected server error."

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = 404
    default_message = "Not found."


class ValidationError(AppError):
    status_code = 400
    default_message = "Invalid request."


class PermissionDeniedError(AppError):
    status_code = 403
    default_message = "Not permitted."


class ConflictError(AppError):
    status_code = 409
    default_message = "Conflicting state."


class UpstreamError(AppError):
    """A dependency failed - Gemini, GCS, ffmpeg."""

    status_code = 502
    default_message = "An upstream service failed."


class RateLimitedError(AppError):
    status_code = 429
    default_message = "Rate limit exceeded."


def _payload(message: str) -> dict:
    # Matches the shape the frontend already reads, so error rendering in the
    # existing HTML keeps working - only the status code changes.
    return {"status": "error", "message": message}


def register_error_handlers(app: FastAPI) -> None:
    """Installs the handlers. Call once at app construction."""

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError):
        if exc.status_code >= 500:
            logger.error("%s: %s", type(exc).__name__, exc.message)
        return JSONResponse(status_code=exc.status_code, content=_payload(exc.message))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(status_code=exc.status_code, content=_payload(detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"status": "error", "message": "Invalid request.",
                     "errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Log the full trace server-side; return a generic message so internal
        # paths and stack details are not leaked to the client.
        logger.error(
            "Unhandled %s on %s %s\n%s",
            type(exc).__name__, request.method, request.url.path,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        return JSONResponse(
            status_code=500,
            content=_payload("Internal server error. See server logs."),
        )
