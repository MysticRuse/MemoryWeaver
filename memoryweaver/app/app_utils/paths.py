"""Filesystem path validation for the local-browsing endpoints.

The Cleaner feature lets an operator browse their own machine to pick photo
folders, so several endpoints accept a caller-supplied absolute path. Before
this module those paths were passed straight to ``open()`` / ``FileResponse``,
which made ``GET /api/cleaner/local-video?path=/Users/me/.ssh/id_rsa`` an
arbitrary file read. ``require_admin_token`` is a no-op whenever
``MW_ADMIN_TOKEN`` is unset - the documented default for local use - so those
endpoints were effectively unauthenticated too.

Every caller-supplied path now goes through :func:`resolve_browsable_path`,
which resolves symlinks first and then requires the result to sit inside an
allowlisted root. The default root is the user's home directory (the feature's
actual purpose); deployments can narrow or widen it with ``MW_BROWSE_ROOTS``.
Sensitive dotfile directories are denied even when they fall inside a root.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

# Directory names that never make sense for a photo browser and commonly hold
# credentials. Checked against every component of the resolved path.
_DENIED_COMPONENTS = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".config",
        ".kube",
        ".docker",
        ".git",
        ".password-store",
        "node_modules",
        "site-packages",
    }
)


def _allowed_roots() -> list[Path]:
    """Returns the roots a caller may browse.

    ``MW_BROWSE_ROOTS`` is an os.pathsep-separated list of absolute paths.
    Unset means "the current user's home directory", which is what the Cleaner
    UI actually needs.
    """
    configured = os.environ.get("MW_BROWSE_ROOTS", "").strip()
    if not configured:
        return [Path.home().resolve()]

    roots: list[Path] = []
    for entry in configured.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        try:
            roots.append(Path(entry).expanduser().resolve(strict=False))
        except OSError:
            continue
    # An unusable configuration must not silently fall back to "/".
    return roots or [Path.home().resolve()]


def is_browsable(candidate: str | os.PathLike[str]) -> bool:
    """True when ``candidate`` resolves inside an allowed root and is not denied."""
    try:
        resolved = Path(candidate).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False

    if any(part in _DENIED_COMPONENTS for part in resolved.parts):
        return False

    return any(resolved == root or root in resolved.parents for root in _allowed_roots())


def resolve_browsable_path(
    candidate: str | os.PathLike[str],
    *,
    must_exist: bool = True,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> Path:
    """Validates a caller-supplied path and returns it fully resolved.

    Resolution happens before the containment check, so a symlink pointing out
    of an allowed root is rejected rather than followed.

    Raises:
        HTTPException: 400 if the path is unusable, 403 if it escapes the
            allowed roots, 404 if it is required to exist and does not.
    """
    if candidate is None or str(candidate).strip() == "":
        raise HTTPException(status_code=400, detail="A path is required.")

    try:
        resolved = Path(candidate).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed path.") from exc

    if not is_browsable(resolved):
        # Deliberately vague: do not confirm whether the path exists.
        raise HTTPException(
            status_code=403,
            detail="Path is outside the permitted browsing roots.",
        )

    if must_exist and not resolved.exists():
        raise HTTPException(status_code=404, detail="Path not found.")
    if must_be_file and resolved.exists() and not resolved.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file.")
    if must_be_dir and resolved.exists() and not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory.")

    return resolved


def safe_storage_join(base_dir: str | os.PathLike[str], filename: str) -> Path:
    """Joins ``filename`` onto ``base_dir``, refusing anything that escapes it.

    Used for session-scoped media where only a bare filename is ever legitimate.
    ``os.path.basename`` alone was the previous guard; this also catches the
    resolved-symlink case.
    """
    base = Path(base_dir).resolve(strict=False)
    target = (base / os.path.basename(str(filename))).resolve(strict=False)
    if base != target and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return target
