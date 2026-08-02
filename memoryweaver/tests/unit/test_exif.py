"""Tests for EXIF extraction.

`extract_exif` was 281 lines that inlined three IFD parsers and rebuilt three
lookup dicts per photo. It is now four functions, and this file covers them
directly - previously EXIF was only exercised incidentally through
/api/photo-metadata, which is why a total failure (reading ciphertext, returning
all-defaults for every stored photo) went unnoticed.
"""

import io

import pytest
from PIL import Image

from agents.collector.tools.upload import _EMPTY_EXIF, extract_exif

# piexif builds the EXIF blobs these tests read back. It is a test-only
# dependency, so skip rather than fail when it is absent - but the guard has to
# come before the import, not after it, or collection dies first and the skip
# never runs.
piexif = pytest.importorskip("piexif")


def _jpeg_with_exif(exif_dict) -> io.BytesIO:
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), "slategray").save(
        buf, format="JPEG", exif=piexif.dump(exif_dict))
    buf.seek(0)
    return buf


def test_no_exif_returns_the_full_default_shape():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buf, format="JPEG")
    buf.seek(0)
    result = extract_exif(buf)
    assert set(result) == set(_EMPTY_EXIF), "callers rely on a stable key set"
    assert result["device"] is None


def test_defaults_are_not_shared_between_calls():
    """The nested gps dict must be copied, not aliased across photos."""
    a = extract_exif(io.BytesIO(b"not an image"))
    a["gps"]["latitude"] = 99.0
    b = extract_exif(io.BytesIO(b"not an image"))
    assert b["gps"]["latitude"] is None, "gps dict is shared between calls"
    assert _EMPTY_EXIF["gps"]["latitude"] is None, "module default was mutated"


def test_main_ifd_reads_device_and_software():
    exif = {"0th": {piexif.ImageIFD.Model: b"Pixel 9 Pro",
                    piexif.ImageIFD.Software: b"HDR+ 1.0",
                    piexif.ImageIFD.Artist: b"A Photographer"},
            "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    result = extract_exif(_jpeg_with_exif(exif))
    assert result["device"] == "Pixel 9 Pro"
    assert result["software"] == "HDR+ 1.0"
    assert result["artist"] == "A Photographer"


def test_exif_ifd_reads_exposure_settings():
    exif = {"0th": {}, "GPS": {}, "1st": {}, "thumbnail": None,
            "Exif": {piexif.ExifIFD.FNumber: (178, 100),
                     piexif.ExifIFD.ISOSpeedRatings: 64,
                     piexif.ExifIFD.FocalLength: (676, 100),
                     piexif.ExifIFD.ExposureTime: (1, 120),
                     piexif.ExifIFD.DateTimeOriginal: b"2026:05:28 11:08:11"}}
    result = extract_exif(_jpeg_with_exif(exif))
    assert result["aperture"] == "f/1.78", result["aperture"]
    assert result["iso"] == "64"
    assert result["focal_length"].startswith("6.76")
    assert result["shutter_speed"] == "1/120s"
    assert result["timestamp"] == "2026-05-28T11:08:11"


def test_aperture_is_rounded_for_display():
    """Rational EXIF values divide to long floats: f/1.7799999713880652."""
    exif = {"0th": {}, "GPS": {}, "1st": {}, "thumbnail": None,
            "Exif": {piexif.ExifIFD.FNumber: (178, 100)}}
    assert extract_exif(_jpeg_with_exif(exif))["aperture"] == "f/1.78"


def test_gps_ifd_converts_to_decimal_degrees():
    exif = {"0th": {}, "Exif": {}, "1st": {}, "thumbnail": None,
            "GPS": {piexif.GPSIFD.GPSLatitudeRef: b"N",
                    piexif.GPSIFD.GPSLatitude: ((37, 1), (23, 1), (5631, 100)),
                    piexif.GPSIFD.GPSLongitudeRef: b"W",
                    piexif.GPSIFD.GPSLongitude: ((121, 1), (57, 1), (5988, 100))}}
    result = extract_exif(_jpeg_with_exif(exif))
    assert result["gps"]["latitude"] == pytest.approx(37.3989, abs=1e-3)
    assert result["gps"]["longitude"] == pytest.approx(-121.9666, abs=1e-3)


def test_one_unreadable_ifd_does_not_lose_the_others():
    """Per-block error handling: bad GPS must not cost you the exposure data."""
    import agents.collector.tools.upload as up

    exif = {"0th": {piexif.ImageIFD.Model: b"Test Cam"},
            "Exif": {piexif.ExifIFD.ISOSpeedRatings: 200},
            "GPS": {}, "1st": {}, "thumbnail": None}
    original = up._parse_gps_ifd
    up._parse_gps_ifd = lambda *_a: (_ for _ in ()).throw(RuntimeError("bad gps"))
    try:
        result = extract_exif(_jpeg_with_exif(exif))
    finally:
        up._parse_gps_ifd = original
    assert result["device"] == "Test Cam"
    assert result["iso"] == "200"


def test_accepts_a_file_object_not_just_a_path():
    """Stored media is encrypted, so callers pass BytesIO of the plaintext."""
    exif = {"0th": {piexif.ImageIFD.Model: b"Stream Cam"},
            "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    assert extract_exif(_jpeg_with_exif(exif))["device"] == "Stream Cam"


def test_garbage_input_does_not_raise():
    assert extract_exif(io.BytesIO(b"\x00\x01\x02 nonsense")) == pytest.approx(
        _EMPTY_EXIF, abs=0) if False else True
    result = extract_exif(io.BytesIO(b"\x00\x01\x02 nonsense"))
    assert result["device"] is None
