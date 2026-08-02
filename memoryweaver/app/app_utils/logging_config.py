"""Application logging.

The codebase used bare ``print()`` in 77 places and swallowed 54 exceptions with
a silent ``pass``. Neither is recoverable in production: print bypasses levels,
handlers and formatting, and a silent pass destroys the only evidence that
something failed.

This module gives one configured logger tree. Call :func:`configure_logging`
once at startup; everywhere else use ``get_logger(__name__)``.

Levels used in this codebase:

``debug``
    Expected, uninteresting misses - a thumbnail that isn't cached yet.
``warning``
    A best-effort step failed and the request continued with a fallback. This is
    the level for the "operation degraded but did not fail" case, which is what
    most of the old silent handlers actually were.
``error``
    The request could not do what was asked. Usually paired with raising an
    AppError so the client sees a real status code.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"


def configure_logging() -> None:
    """Installs a single stderr handler on the root logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.environ.get("MW_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("memoryweaver")
    root.setLevel(level)

    # Attach a handler only when nothing upstream will render our records.
    # Under uvicorn the root logger already has one, so adding ours would print
    # everything twice. Propagation is deliberately left enabled: disabling it
    # silences pytest's caplog and anything else that captures at the root,
    # which is a poor trade for avoiding duplicate lines.
    if not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        root.handlers.clear()
        root.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Returns a namespaced logger, configuring the tree on first use."""
    configure_logging()
    # Normalise module paths onto the memoryweaver tree so one level controls all.
    suffix = name.split(".", 1)[-1] if name.startswith(("app.", "pipeline.", "agents.")) else name
    return logging.getLogger(f"memoryweaver.{suffix}")
