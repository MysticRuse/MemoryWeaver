import pytest
import os
import sys

# Ensure project app is in sys path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from agents.collector.tools.upload import process_and_save_upload

def test_oversized_file_rejection():
    """Asserts that uploading a file larger than 20MB raises a ValueError."""
    # Create mock byte content of 21 MB
    oversized_bytes = b"0" * (21 * 1024 * 1024)
    with pytest.raises(ValueError) as excinfo:
        process_and_save_upload(
            file_bytes=oversized_bytes,
            original_filename="large_photo.jpg",
            contributor_name="Test User"
        )
    assert "exceeds the 20MB limit" in str(excinfo.value)

def test_malicious_filename_sanitization():
    """Asserts that malicious characters in filenames are sanitized safely."""
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
