---
name: "Collector Skill"
description: "Accepts incoming trip files, validates them, extracts EXIF tags, hashes contributor IDs, and stores media files securely."
---

# Collector Agent Skill Instructions

This skill governs the ingestion and sanitization of trip photo uploads.

## Operations
1. **Validation**:
   - Enforce a strict size limit of 20MB per file.
   - Restrict incoming files to JPEGs, PNGs, and HEIC files.
2. **Privacy Protection**:
   - Hash raw contributor names immediately using SHA-256 to produce an anonymous `contributor_id`.
   - Never log raw contributor names or email addresses.
3. **EXIF Processing**:
   - Extract EXIF metadata tags including timestamp, GPS coordinates, and device model.
   - Standardize GPS values to decimal format.
