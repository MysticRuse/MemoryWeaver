import os
import sys

import pytest

# Ensure project app is in sys path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from agents.collector.tools.upload import process_and_save_upload


def test_oversized_file_rejection():
    """Asserts that uploading a file larger than 100MB raises a ValueError."""
    # Create mock byte content of 101 MB
    oversized_bytes = b"0" * (101 * 1024 * 1024)
    with pytest.raises(ValueError) as excinfo:
        process_and_save_upload(
            file_bytes=oversized_bytes,
            original_filename="large_photo.jpg",
            contributor_name="Test User"
        )
    assert "exceeds the 100MB limit" in str(excinfo.value)

def test_malicious_filename_sanitization(tmp_path, monkeypatch):
    """Asserts that malicious characters in filenames are sanitized safely.

    Uses tmp_path deliberately: this test writes a real file through
    process_and_save_upload, and without redirecting storage it deposited a
    `passwd_trip.jpg` into the developer's live local_storage/uploads on every
    run. That accumulated to 258 junk files, none of them valid images, which
    then showed up as the app's photo library.
    """
    import app.app_utils.storage as storage_mod

    class TmpStorage(storage_mod.StorageHelper):
        def __init__(self, session_id="default"):
            self.session_id = session_id
            self.local_base = str(tmp_path)
            self.bucket_name = None
            self.project_id = None
            self.use_gcs = False

    monkeypatch.setattr(storage_mod, "StorageHelper", TmpStorage)
    monkeypatch.setattr("agents.collector.tools.upload.StorageHelper", TmpStorage,
                        raising=False)
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)

    malicious_filename = "../../../etc/passwd_trip.jpg"
    mock_bytes = b"fake image bytes"

    photo_info = process_and_save_upload(
        file_bytes=mock_bytes,
        original_filename=malicious_filename,
        contributor_name="Test User"
    )
    # The sanitization should extract only the base filename and prefix it with hashes
    sanitized_filename = photo_info["filename"]
    assert "passwd_trip.jpg" in sanitized_filename
    assert ".." not in sanitized_filename
    assert "/" not in sanitized_filename

def test_invalid_mime_type_rejection():
    """Asserts that uploading non-image file extensions raises a ValueError."""
    invalid_filename = "exploit.exe"
    mock_bytes = b"malicious binary contents"

    with pytest.raises(ValueError) as excinfo:
        process_and_save_upload(
            file_bytes=mock_bytes,
            original_filename=invalid_filename,
            contributor_name="Test User"
        )
    assert "Unsupported file format" in str(excinfo.value)

def test_ai_cost_circuit_breaker():
    """Asserts that the AI Cost Circuit Breaker triggers when request velocity or token limits are exceeded."""
    from pipeline.prompt_safety import CircuitBreaker
    cb = CircuitBreaker(max_requests_per_min=5, max_tokens_per_hour=1000)

    # 1. Test request rate velocity cap
    for _ in range(5):
        allowed, msg = cb.check_budget(session_id="test_session", estimated_tokens=100)
        assert allowed is True

    # 6th request within 1 min should trigger circuit breaker
    allowed, msg = cb.check_budget(session_id="test_session", estimated_tokens=100)
    assert allowed is False
    assert "Exceeded max request velocity" in msg

    # 2. Reset and test token volume cap
    cb.reset()
    allowed, msg = cb.check_budget(session_id="test_session_2", estimated_tokens=900)
    assert allowed is True

    # Second call of 200 tokens exceeds 1000 token limit
    allowed, msg = cb.check_budget(session_id="test_session_2", estimated_tokens=200)
    assert allowed is False
    assert "Exceeded max token budget" in msg

