"""Encryption at rest for stored media.

What was wrong with the previous scheme
---------------------------------------
The key was ``PBKDF2(password=session_id, salt=b"MemoryWeaverSecureSalt_2026",
iterations=10_000)``. Every input was public: ``session_id`` defaults to
"default", appears in URLs, and is handed out by the unauthenticated
``GET /api/sessions``; the salt was a literal in three source files. Anyone who
could read the repo could derive the key for any session. Decryption failure
also fell back to returning the raw bytes, so ciphertext and plaintext were
indistinguishable to callers and a tampered file was served as-is.

The scheme here
---------------
``MWv2 || salt(16) || nonce(12) || AES-256-GCM ciphertext``

* The master key comes from ``MW_ENCRYPTION_KEY`` - a real secret, not an
  identifier. If unset, a random key is generated once and persisted to
  ``local_storage/.encryption_key`` with 0600 permissions so local installs
  keep working without configuration.
* The master key is stretched once with PBKDF2-HMAC-SHA256 at 600k iterations
  (OWASP 2023 guidance) and cached. Per-file keys are then derived with HKDF
  from a random 16-byte salt, which is cheap - important when a gallery view
  decrypts hundreds of files. Each file therefore gets a distinct key.
* ``session_id`` is bound into the HKDF ``info`` parameter, so a blob from one
  session cannot be decrypted in the context of another. It is a domain
  separator, not key material.
* Decryption fails closed: a GCM tag mismatch raises rather than returning the
  input.

Backward compatibility
----------------------
Existing installs hold three kinds of file in the same directory: v1 blobs
written by the old backend, v1 blobs written by the browser's Web Crypto path,
and genuinely unencrypted media copied in by the local-folder ingest
(``shutil.copy2``) and sync paths. :func:`decrypt_bytes` detects each case
explicitly rather than guessing, so nothing already on disk becomes unreadable.
Anything that is neither recognisable plaintext media nor decryptable raises.
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.app_utils.logging_config import get_logger

logger = get_logger(__name__)

MAGIC = b"MWv2"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32

# OWASP 2023 minimum for PBKDF2-HMAC-SHA256. Paid once per process, not per file.
PBKDF2_ITERATIONS = 600_000
_MASTER_KDF_SALT = b"memoryweaver/master-key/v2"

# Legacy (v1) parameters - read-only, never used to write new data.
_V1_SALT = b"MemoryWeaverSecureSalt_2026"
_V1_ITERATIONS = 10_000

# Leading bytes of formats the app legitimately stores unencrypted.
_PLAINTEXT_MAGICS: tuple[bytes, ...] = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",     # PNG
    b"GIF87a",
    b"GIF89a",
    b"BM",                    # BMP
    b"II*\x00",               # TIFF little-endian
    b"MM\x00*",               # TIFF big-endian
    b"%PDF",
    b"OggS",
    b"ID3",                   # MP3 with tag
    b"\x1a\x45\xdf\xa3",      # Matroska / WebM
)

_master_key: bytes | None = None
_master_lock = threading.Lock()


class DecryptionError(Exception):
    """Raised when a stored blob cannot be authenticated or decoded."""


def _key_file_path() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "local_storage" / ".encryption_key"


def _load_or_create_secret() -> bytes:
    """Returns the raw master secret, generating one on first run if needed."""
    configured = os.environ.get("MW_ENCRYPTION_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")

    key_path = _key_file_path()
    if key_path.exists():
        return key_path.read_bytes()

    # No configured secret: generate one and persist it with owner-only
    # permissions so a local install is secure by default. Production should
    # set MW_ENCRYPTION_KEY explicitly.
    key_path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_bytes(32)
    # Create with 0600 from the start rather than chmod-ing after writing.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, generated)
    finally:
        os.close(fd)
    logger.warning(
        "Generated a media encryption key at %s. Set MW_ENCRYPTION_KEY to "
        "manage this yourself; back the file up or previously encrypted media "
        "becomes unreadable.", key_path
    )
    return generated


def get_master_key() -> bytes:
    """Stretches and caches the master secret. Thread-safe, computed once."""
    global _master_key
    if _master_key is None:
        with _master_lock:
            if _master_key is None:
                kdf = PBKDF2HMAC(
                    algorithm=hashes.SHA256(),
                    length=KEY_LEN,
                    salt=_MASTER_KDF_SALT,
                    iterations=PBKDF2_ITERATIONS,
                )
                _master_key = kdf.derive(_load_or_create_secret())
    return _master_key


def reset_master_key_cache() -> None:
    """Clears the cached key. For tests that swap MW_ENCRYPTION_KEY."""
    global _master_key
    with _master_lock:
        _master_key = None


def _derive_file_key(salt: bytes, session_id: str) -> bytes:
    """Per-file key. HKDF is cheap; the expensive stretch already happened."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        info=b"memoryweaver/file/v2|" + session_id.encode("utf-8"),
    ).derive(get_master_key())


def looks_like_plaintext_media(data: bytes) -> bool:
    """True when the blob is an unencrypted media file we store as-is.

    Covers two shapes:

    * formats identified by a leading signature (JPEG, PNG, GIF, ...), and
    * ISO base-media containers - HEIC/HEIF, MP4, MOV, M4A - whose ``ftyp``
      box sits at offset 4, not at the start. Prefix matching alone missed
      every one of those, so an unencrypted HEIC (which is exactly what the
      folder-ingest path writes) failed the check, fell through to the legacy
      decrypt, and was refused as undecryptable.
    """
    if data.startswith(_PLAINTEXT_MAGICS):
        return True
    return len(data) >= 12 and data[4:8] == b"ftyp"


def encrypt_bytes(data: bytes, session_id: str = "default") -> bytes:
    """Encrypts ``data`` into a self-describing v2 blob."""
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = _derive_file_key(salt, session_id)
    ciphertext = AESGCM(key).encrypt(nonce, data, MAGIC)
    return MAGIC + salt + nonce + ciphertext


def _decrypt_v1(blob: bytes, session_id: str) -> bytes:
    """Reads a blob written by the old session_id-derived scheme."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=_V1_SALT,
        iterations=_V1_ITERATIONS,
    )
    key = kdf.derive(session_id.encode("utf-8"))
    return AESGCM(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], None)


def decrypt_bytes(blob: bytes, session_id: str = "default") -> bytes:
    """Returns the plaintext for a stored blob.

    Handles v2 blobs, legacy v1 blobs, and files that were never encrypted.

    Raises:
        DecryptionError: if the blob is none of those - authentication failed,
            so it is not returned to the caller.
    """
    if not blob:
        return b""

    if blob.startswith(MAGIC):
        header = len(MAGIC)
        # A truncated blob would otherwise yield a short nonce and raise
        # ValueError out of AESGCM rather than a DecryptionError.
        minimum = header + SALT_LEN + NONCE_LEN
        if len(blob) <= minimum:
            raise DecryptionError("Truncated v2 blob; refusing to serve it.")

        salt = blob[header:header + SALT_LEN]
        nonce = blob[header + SALT_LEN:minimum]
        ciphertext = blob[minimum:]
        key = _derive_file_key(salt, session_id)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, MAGIC)
        except (InvalidTag, ValueError) as exc:
            # Fail closed: wrong key or tampered file. Never serve raw bytes.
            raise DecryptionError(
                "Authentication failed for a v2 blob; refusing to serve it."
            ) from exc

    # Media copied in by the folder-ingest and sync paths is stored unencrypted.
    if looks_like_plaintext_media(blob):
        return blob

    try:
        return _decrypt_v1(blob, session_id)
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError(
            "Blob is neither recognisable media nor decryptable with the "
            "legacy key; refusing to serve it."
        ) from exc


def is_encrypted(blob: bytes) -> bool:
    """True when the blob carries the v2 header."""
    return blob.startswith(MAGIC)


