"""Tests for media encryption at rest.

Covers the three properties the previous implementation lacked: the key is not
derivable from public data, decryption fails closed, and existing files stay
readable.
"""

import os

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.app_utils import crypto


@pytest.fixture(autouse=True)
def fixed_master_key(monkeypatch):
    """Pin a known master secret so tests never touch the on-disk key file."""
    monkeypatch.setenv("MW_ENCRYPTION_KEY", "test-master-secret-do-not-reuse")
    crypto.reset_master_key_cache()
    yield
    crypto.reset_master_key_cache()


JPEG = b"\xff\xd8\xff\xe0" + b"body-of-a-jpeg" * 8


def test_roundtrip():
    blob = crypto.encrypt_bytes(JPEG, "session-a")
    assert crypto.decrypt_bytes(blob, "session-a") == JPEG


def test_ciphertext_is_not_plaintext():
    blob = crypto.encrypt_bytes(JPEG, "session-a")
    assert JPEG not in blob
    assert crypto.is_encrypted(blob)


def test_same_input_encrypts_differently_each_time():
    """Random per-file salt and nonce: no deterministic ciphertext."""
    a = crypto.encrypt_bytes(JPEG, "session-a")
    b = crypto.encrypt_bytes(JPEG, "session-a")
    assert a != b
    assert crypto.decrypt_bytes(a, "session-a") == crypto.decrypt_bytes(b, "session-a")


def test_session_id_alone_cannot_derive_the_key(monkeypatch):
    """The core failure of v1: session_id is public, so it must not be the key.

    Re-deriving with a different master secret must fail even though the
    session_id is identical.
    """
    blob = crypto.encrypt_bytes(JPEG, "default")

    monkeypatch.setenv("MW_ENCRYPTION_KEY", "a-completely-different-secret")
    crypto.reset_master_key_cache()
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(blob, "default")


def test_blob_cannot_be_read_under_another_session():
    """session_id is bound into HKDF info as a domain separator."""
    blob = crypto.encrypt_bytes(JPEG, "wedding")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(blob, "birthday")


def test_tampered_ciphertext_fails_closed():
    """v1 returned the raw bytes on failure; v2 must refuse."""
    blob = bytearray(crypto.encrypt_bytes(JPEG, "session-a"))
    blob[-1] ^= 0xFF
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(blob), "session-a")


def test_truncated_blob_fails_closed():
    blob = crypto.encrypt_bytes(JPEG, "session-a")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(blob[:20], "session-a")


def test_garbage_is_rejected_not_returned():
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(b"\x00\x01\x02 not media, not ciphertext", "session-a")


def test_uses_owasp_iteration_count():
    assert crypto.PBKDF2_ITERATIONS >= 600_000


def test_plaintext_media_passes_through():
    """Folder-ingest copies files in unencrypted; those must still render."""
    assert crypto.decrypt_bytes(JPEG, "default") == JPEG
    png = b"\x89PNG\r\n\x1a\n" + b"pngdata"
    assert crypto.decrypt_bytes(png, "default") == png


def test_legacy_v1_blob_is_still_readable():
    """Files written by the old scheme must not become unreadable."""
    session_id = "default"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"MemoryWeaverSecureSalt_2026",
        iterations=10_000,
    )
    key = kdf.derive(session_id.encode())
    nonce = os.urandom(12)
    v1_blob = nonce + AESGCM(key).encrypt(nonce, JPEG, None)

    assert crypto.decrypt_bytes(v1_blob, session_id) == JPEG


def test_empty_input_returns_empty():
    assert crypto.decrypt_bytes(b"", "default") == b""


def test_endpoint_helper_returns_empty_on_undecryptable(tmp_path):
    """load_image_bytes_decrypted must not hand ciphertext back to the client."""
    from app.services.media_store import load_image_bytes_decrypted

    bad = tmp_path / "corrupt.jpg"
    blob = bytearray(crypto.encrypt_bytes(JPEG, "session-a"))
    blob[-1] ^= 0xFF
    bad.write_bytes(bytes(blob))

    assert load_image_bytes_decrypted(str(bad), "session-a") == b""


def test_endpoint_helper_roundtrip(tmp_path):
    from app.services.media_store import encrypt_file_bytes, load_image_bytes_decrypted

    good = tmp_path / "photo.jpg"
    good.write_bytes(encrypt_file_bytes(JPEG, "session-a"))
    assert load_image_bytes_decrypted(str(good), "session-a") == JPEG


# --- ISO base-media containers ---------------------------------------------
#
# Regression: HEIC/MP4/MOV put their `ftyp` box at offset 4, so the original
# prefix-only magic check treated an unencrypted HEIC as undecryptable and the
# /media endpoint returned 415. Caught by launching the app, not by the suite.

HEIC = b"\x00\x00\x00$ftypheic\x00\x00\x00\x00" + b"payload" * 4
MP4 = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00" + b"payload" * 4
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00" + b"payload" * 4


@pytest.mark.parametrize("blob,label", [(HEIC, "heic"), (MP4, "mp4"), (MOV, "mov")])
def test_plaintext_iso_container_passes_through(blob, label):
    assert crypto.looks_like_plaintext_media(blob) is True
    assert crypto.decrypt_bytes(blob, "default") == blob


@pytest.mark.parametrize("blob,label", [(HEIC, "heic"), (MP4, "mp4"), (MOV, "mov")])
def test_iso_container_roundtrips_when_encrypted(blob, label):
    encrypted = crypto.encrypt_bytes(blob, "session-a")
    assert crypto.is_encrypted(encrypted)
    assert crypto.decrypt_bytes(encrypted, "session-a") == blob


def test_ftyp_check_does_not_swallow_real_ciphertext():
    """The offset-4 probe must not misclassify encrypted data as plaintext."""
    for _ in range(200):
        blob = crypto.encrypt_bytes(HEIC, "session-a")
        assert crypto.looks_like_plaintext_media(blob) is False


def test_short_blob_is_not_treated_as_container():
    assert crypto.looks_like_plaintext_media(b"\x00\x00\x00$ftyp") is False
