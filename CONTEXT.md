# MemoryWeaver Project Standards (CONTEXT.md)

This document defines the development rules, security constraints, and privacy guardrails enforced across the MemoryWeaver multi-agent system.

## Privacy & Data Protection Rules
* **No PII Logging**: Do not print, log, or store Personally Identifiable Information (PII) including contributor names, raw email addresses, or phone numbers in application logs or session records. Use anonymous `contributor_id` hashes.
* **Metadata Exposure**: Do not expose raw EXIF metadata (exact coordinates, device serial numbers) to the public frontend. Only expose derived high-level labels (e.g. area name, relative hour of day).

## Security Guardrails
* **STRIDE Threat Mitigation**:
  * *Spoofing*: Validate all session tokens and contributor IDs.
  * *Tampering*: Validate incoming files (max 20MB, correct mime-types) and strip prompt injection vectors in filenames and captions.
  * *Information Disclosure*: Store unprocessed raw photos in a private, non-public bucket namespace (`uploads/`). Move only curated/moderated outputs to public paths.
* **Input Sanitization**: Sanitize all filename strings and caption inputs before passing them to the database or rendering them in template files.

## Code Quality Standards
* Keep agents modular and testable. Each agent should operate strictly on its defined session state inputs and return standardized outputs to the A2A messaging layer.
* Write unit tests using `pytest` for each agent's core tools.
