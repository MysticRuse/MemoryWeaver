# Session notes

**Read this instead of asking for context. Everything below is uncommitted.**

## What the product is now
MemoryWeaver is a **single-page photo/video library cleaner and editor**
(`memoryweaver/frontend/cleaner.html`, served at `/`). The original multi-agent
curation product — Curator Hub, Trip Highlights, contributor `/join` links, the
five-agent ADK pipeline, the MCP server — was **removed entirely**. All photo and
video editing was ported into the Cleaner first and verified before deletion.

## State
- `cd memoryweaver` — **131 tests green, ruff clean, app boots with 0 tracebacks, 44 routes.**
- Verified live: 13 media items, 4 videos, 2 duplicate clusters, storage summary 21.2 MB.
- Nothing is committed. `git diff` + `git status` is the full record of the change.

## Decisions worth not re-litigating
- **One library.** No session/library switcher. `getSessionId()` returns `'default'`.
  Never filter filenames by `startswith(session_id + "_")` — that hid the whole
  library once; `tests/unit/test_cleaner_listings.py` guards it.
- Media is **encrypted at rest**; always read via `load_image_bytes_decrypted()`.
- Cheap-path-first is deliberate: local aHash / Apple Vision / ffmpeg before Gemini.
- `/api/photo-action`, `/api/video/resolve-keep-delete` were dead on arrival and are
  now either wired up or left intentionally.

## Open threads (in priority order)
1. **Revoke the Gemini API key.** One key is committed in git history — see
   `git show c7536e0 -- memoryweaver-mvp/.env`. It is out of the working tree but
   stays compromised until rotated in Google AI Studio. The value is deliberately
   not reproduced here: writing it into a tracked file would put it straight back
   into the repo.
   This is the only item requiring the account owner.
2. `local_storage/sessions/family-trip-california-435cf2/` — originals were **copied**
   into the main pool, not moved. Safe to delete once the library looks right.
3. 258 test fixtures moved out of live storage to `/tmp/mw_fixture_backup`.
4. `memoryweaver/specs/` still documents the retired product. README was rewritten; specs weren't.
5. 14 functions still exceed 150 lines (worst: `analyze_all_cleaner`, 291).
