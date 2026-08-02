"""Tests for the filesystem confinement added to the local-browsing endpoints.

Before app_utils.paths existed, `GET /api/cleaner/local-video?path=<anything>`
handed a caller-supplied path straight to FileResponse. These tests assert the
hole is closed, so a future refactor cannot quietly reopen it.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.app_utils.paths import (
    is_browsable,
    resolve_browsable_path,
    safe_storage_join,
)


@pytest.fixture
def sandbox(monkeypatch):
    """A temp dir registered as the only allowed browse root."""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir).resolve()
        monkeypatch.setenv("MW_BROWSE_ROOTS", str(root))
        yield root


def test_path_inside_root_is_allowed(sandbox):
    target = sandbox / "photos"
    target.mkdir()
    assert resolve_browsable_path(target, must_be_dir=True) == target


def test_dotdot_traversal_is_rejected(sandbox):
    with pytest.raises(HTTPException) as exc:
        resolve_browsable_path(sandbox / ".." / ".." / "etc" / "passwd")
    assert exc.value.status_code == 403


def test_absolute_path_outside_root_is_rejected(sandbox):
    with pytest.raises(HTTPException) as exc:
        resolve_browsable_path("/etc/passwd")
    assert exc.value.status_code == 403


def test_symlink_escaping_root_is_rejected(sandbox):
    """Resolution happens before containment, so a symlink cannot tunnel out."""
    outside = Path(tempfile.mkdtemp()).resolve()
    try:
        (outside / "secret.txt").write_text("sensitive")
        link = sandbox / "escape"
        os.symlink(outside, link)

        with pytest.raises(HTTPException) as exc:
            resolve_browsable_path(link / "secret.txt", must_be_file=True)
        assert exc.value.status_code == 403
    finally:
        (outside / "secret.txt").unlink(missing_ok=True)
        outside.rmdir()


def test_credential_directories_are_denied_even_inside_root(sandbox):
    ssh_dir = sandbox / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("PRIVATE KEY")

    assert is_browsable(ssh_dir / "id_rsa") is False
    with pytest.raises(HTTPException) as exc:
        resolve_browsable_path(ssh_dir / "id_rsa", must_be_file=True)
    assert exc.value.status_code == 403


def test_missing_path_is_404_not_403(sandbox):
    with pytest.raises(HTTPException) as exc:
        resolve_browsable_path(sandbox / "nope.jpg", must_be_file=True)
    assert exc.value.status_code == 404


def test_empty_path_is_rejected(sandbox):
    with pytest.raises(HTTPException) as exc:
        resolve_browsable_path("")
    assert exc.value.status_code == 400


def test_unset_browse_roots_defaults_to_home(monkeypatch):
    """An unset/blank config must not degrade to allowing '/'."""
    monkeypatch.setenv("MW_BROWSE_ROOTS", "")
    assert is_browsable(Path.home() / "Pictures") is True
    assert is_browsable("/etc/passwd") is False


def test_safe_storage_join_confines_to_base():
    with tempfile.TemporaryDirectory() as base:
        assert safe_storage_join(base, "photo.jpg").parent == Path(base).resolve()
        # basename() strips the traversal, so this lands inside base, not above it.
        assert safe_storage_join(base, "../../etc/passwd").parent == Path(base).resolve()


def test_local_video_rejects_non_video_extension(sandbox):
    """Confinement plus an extension allowlist: no reading key material."""
    from app.routers.cleaner import get_local_video

    secret = sandbox / "id_rsa"
    secret.write_text("PRIVATE KEY")

    with pytest.raises(HTTPException) as exc:
        get_local_video(path=str(secret), range=None)
    assert exc.value.status_code == 415


def test_local_video_outside_root_is_rejected(sandbox):
    from app.routers.cleaner import get_local_video

    with pytest.raises(HTTPException) as exc:
        get_local_video(path="/etc/hosts", range=None)
    assert exc.value.status_code == 403
