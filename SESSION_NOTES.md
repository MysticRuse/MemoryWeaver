# Session notes

**Read this instead of asking for context. Everything below is uncommitted.**

## What the product is now
MemoryWeaver is a **single-page photo/video library cleaner and editor**
(`memoryweaver/frontend/cleaner.html`, served at `/`). The original multi-agent
curation product — Curator Hub, Trip Highlights, contributor `/join` links, the
five-agent ADK pipeline, the MCP server — was **removed entirely**. All photo and
video editing was ported into the Cleaner first and verified before deletion.

## Last session (2026-08-03) — AI cost guardrails
Audited the project against The AI Cost Playbook and closed the gaps it found.
- `app/app_utils/genai_client.py`: client now built with bounded retries
  (`HttpRetryOptions`, 3 attempts, exp backoff) + a timeout; new `text_config()`
  applies `MW_MAX_OUTPUT_TOKENS` (4096). Applied to all 12 PIPELINE_MODEL calls.
  Image-generation calls intentionally uncapped — a token cap truncates the image.
- New `app/app_utils/ai_budget.py::track_ai_call()` — one call updates both the
  spend ledger and the rate governor. Wired into the classification/OCR path in
  `services/classification.py` and `routers/cleaner.py`, which were metered nowhere.
- `CircuitBreaker.record_actual_usage()` replaces the flat 500-token reservation
  with `usage_metadata.total_token_count`; the hourly ceiling was a request counter before.
- `CostTracker.over_budget()` + `MW_MONTHLY_BUDGET_USD` (25.00): `enforce_ai_budget`
  now 429s past the cap. Spend was recorded but never read back.
- `analyze-all` fans out one Gemini call per image behind a single budget check;
  it now stops at `MW_MAX_AI_CALLS_PER_RUN` (200) or the spend cap and streams a
  `{"status":"budget"}` SSE event. NOT converted to the Batch API — batch is
  minutes-to-24h async and this endpoint streams live progress.
- Fixed CLAUDE.md: every shorthand command pointed at `~/daily_cost_log.py`,
  which does not exist (real path `tools/daily_cost_log.py`); same for the playbook.
- `tests/unit/test_cost_guardrails.py` (14 tests) asserts the *call sites*, not
  just the mechanisms — every one of these gaps passed a class-level unit test.
- STILL MANUAL: the Google AI Studio / Cloud billing console spend cap. App-side
  caps cannot replace it.
- Cost config: `.claude/settings.json` now pins `model: sonnet`, `effortLevel: low`,
  `fastMode: false` + `fastModePerSessionOptIn: true`, `skillListingMaxDescChars: 400`.
  Opus is now opt-in via `/model opus` per task. Existing SessionEnd hooks preserved.
- CLAUDE.md rewritten: 218 -> 133 lines (~3,570 -> ~1,630 tokens re-read every turn).
  Kept the directory map, quality bar, gotchas, spend guardrails and shorthand table;
  cut prose the model already knows. Added the real failure numbers ($132 three-day
  session, $33 "make it A+") so the rules cite evidence.


## Last session (2026-08-02)
- Removed the "Vault & Organizer" nav link from `cleaner.html` (nav is logo-only now).
- **Fixed: every photo showed under "Other / Misc" and all category pills read (0).**
  `get_global_vault_file_path()` in `app/services/media_store.py` walked two dirs up
  (→ `app/local_storage/`, empty) instead of three (→ `memoryweaver/local_storage/`,
  where the 244-entry vault actually lives). Reads silently returned `{}`.
  `tests/unit/test_vault_path.py` now pins it to `StorageHelper("default").local_base`.
- Any path helper under `app/services/` needs *three* dirnames; ones directly in `app/` need two.
- **Fixed: folder sync was frozen since the library switcher was removed.**
  `sync_config.json`/`sync_registry.json` sat in `local_storage/sessions/<old-id>/`
  while `SyncManager("default")` reads `local_storage/`, so `scan_folder()` saw no
  target and bailed — the pool stayed at the 13 files synced that day.
  `SyncManager._adopt_legacy_session_sync_state()` now migrates them up once.
  Watched folder is `/Users/hironmoy/Desktop`; pool went 9 photos → 14.
- **Fixed: `scan_folder()` deleted every upload not in the sync registry.** The vault
  endpoint scans on every fetch, so direct uploads / magic-edit outputs / extracted
  frames would vanish on the next page load. Registry-driven deletions still work.
  Both guarded by `tests/unit/test_sync_folder_pool.py`.
- Removed the **Unique Shots** category (pill, counts, labels, reassign menu, backend
  `cat_map`). The separate `similarViewMode === 'unique'` tab in Similar Photos is a
  different feature and was left alone.
- **Known gap, not fixed:** the classifier only emits `scrap|info|emotional|organized`.
  Everything informational lands in `info`, which the UI labels "Credentials Vault" —
  flight details and code screenshots are filing there. The taxonomy needs widening in
  `app/services/classification.py:145` to match the 10 UI categories.

## State
- `cd memoryweaver` — **145 tests green, ruff clean, app boots with 0 tracebacks, 44 routes.**
- Verified live: 13 media items, 4 videos, 2 duplicate clusters, storage summary 21.2 MB.
- Committed as 9a601b7a ('Simplify to single-page Cleaner; remove multi-agent
  pipeline and Curator Hub'). 7,467 files changed (7,388 were venv/cache/binary
  cleanup); 38 added, 15 modified. Pre-commit hook (ruff + pytest) passed.

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

## Auto-captured state — 2026-08-03 00:03 UTC
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `forward`
- Last commit: `9a601b7 Simplify to single-page Cleaner; remove multi-agent pipeline and Curator Hub`

**Uncommitted changes (git status --short):**
```
M SESSION_NOTES.md
```

**Unstaged diff summary:**
```
SESSION_NOTES.md | 4 +++-
 1 file changed, 3 insertions(+), 1 deletion(-)
```

**Staged diff summary:**
```
(none)
```

## Auto-captured state — 2026-08-03 00:24 UTC
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `forward`
- Last commit: `9a601b7 Simplify to single-page Cleaner; remove multi-agent pipeline and Curator Hub`

**Uncommitted changes (git status --short):**
```
M SESSION_NOTES.md
?? CHEATSHEET.md
?? EFFICIENCY_PLAYBOOK.md
?? tools/auto_session_notes.py
?? tools/claude_cost_log_hook.log
?? tools/claude_daily_log.csv
?? tools/claude_reports/
?? tools/daily_cost_log.py
```

**Unstaged diff summary:**
```
SESSION_NOTES.md | 26 +++++++++++++++++++++++++-
 1 file changed, 25 insertions(+), 1 deletion(-)
```

**Staged diff summary:**
```
(none)
```

## Auto-captured state — 2026-08-03 00:29 UTC
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `forward`
- Last commit: `9a601b7 Simplify to single-page Cleaner; remove multi-agent pipeline and Curator Hub`

**Uncommitted changes (git status --short):**
```
M SESSION_NOTES.md
?? CHEATSHEET.md
?? EFFICIENCY_PLAYBOOK.md
?? tools/auto_session_notes.py
?? tools/claude_cost_log_hook.log
?? tools/claude_daily_log.csv
?? tools/claude_reports/
?? tools/daily_cost_log.py
```

**Unstaged diff summary:**
```
SESSION_NOTES.md | 55 ++++++++++++++++++++++++++++++++++++++++++++++++++++++-
 1 file changed, 54 insertions(+), 1 deletion(-)
```

**Staged diff summary:**
```
(none)
```

## Auto-captured state — 2026-08-03 00:59 UTC
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `forward`
- Last commit: `9a601b7 Simplify to single-page Cleaner; remove multi-agent pipeline and Curator Hub`

**Uncommitted changes (git status --short):**
```
M .claude/settings.json
 M SESSION_NOTES.md
 M memoryweaver/app/services/media_store.py
 M memoryweaver/frontend/cleaner.html
?? CHEATSHEET.md
?? EFFICIENCY_PLAYBOOK.md
?? memoryweaver/tests/unit/test_vault_path.py
?? tools/auto_session_notes.py
?? tools/claude_cost_log_hook.log
?? tools/claude_daily_log.csv
?? tools/claude_reports/
?? tools/daily_cost_log.py
```

**Unstaged diff summary:**
```
.claude/settings.json                    | 19 +++++++
 SESSION_NOTES.md                         | 96 +++++++++++++++++++++++++++++++-
 memoryweaver/app/services/media_store.py |  5 +-
 memoryweaver/frontend/cleaner.html       |  3 -
 4 files changed, 117 insertions(+), 6 deletions(-)
```

**Staged diff summary:**
```
(none)
```

## Auto-captured state — 2026-08-03 02:34 UTC
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `forward`
- Last commit: `9a601b7 Simplify to single-page Cleaner; remove multi-agent pipeline and Curator Hub`

**Uncommitted changes (git status --short):**
```
M .claude/settings.json
 M SESSION_NOTES.md
 M memoryweaver/app/routers/cleaner.py
 M memoryweaver/app/services/media_store.py
 M memoryweaver/app/sync_manager.py
 M memoryweaver/frontend/cleaner.html
?? CHEATSHEET.md
?? EFFICIENCY_PLAYBOOK.md
?? memoryweaver/tests/unit/test_sync_folder_pool.py
?? memoryweaver/tests/unit/test_vault_path.py
?? tools/auto_session_notes.py
?? tools/claude_cost_log_hook.log
?? tools/claude_daily_log.csv
?? tools/claude_reports/
?? tools/daily_cost_log.py
```

**Unstaged diff summary:**
```
.claude/settings.json                    |  19 ++++
 SESSION_NOTES.md                         | 148 ++++++++++++++++++++++++++++++-
 memoryweaver/app/routers/cleaner.py      |   1 -
 memoryweaver/app/services/media_store.py |   5 +-
 memoryweaver/app/sync_manager.py         |  51 ++++++++---
 memoryweaver/frontend/cleaner.html       |  13 +--
 6 files changed, 209 insertions(+), 28 deletions(-)
```

**Staged diff summary:**
```
(none)
```

## Auto-captured state — 2026-08-03 02:35 UTC
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `forward`
- Last commit: `9a601b7 Simplify to single-page Cleaner; remove multi-agent pipeline and Curator Hub`

**Uncommitted changes (git status --short):**
```
M .claude/settings.json
 M SESSION_NOTES.md
 M memoryweaver/app/routers/cleaner.py
 M memoryweaver/app/services/media_store.py
 M memoryweaver/app/sync_manager.py
 M memoryweaver/frontend/cleaner.html
?? CHEATSHEET.md
?? EFFICIENCY_PLAYBOOK.md
?? memoryweaver/tests/unit/test_sync_folder_pool.py
?? memoryweaver/tests/unit/test_vault_path.py
?? tools/auto_session_notes.py
?? tools/claude_cost_log_hook.log
?? tools/claude_daily_log.csv
?? tools/claude_reports/
?? tools/daily_cost_log.py
```

**Unstaged diff summary:**
```
.claude/settings.json                    |  19 ++++
 SESSION_NOTES.md                         | 189 ++++++++++++++++++++++++++++++-
 memoryweaver/app/routers/cleaner.py      |   1 -
 memoryweaver/app/services/media_store.py |   5 +-
 memoryweaver/app/sync_manager.py         |  51 +++++++--
 memoryweaver/frontend/cleaner.html       |  13 +--
 6 files changed, 250 insertions(+), 28 deletions(-)
```

**Staged diff summary:**
```
(none)
```

## Auto-captured state — 2026-08-03 03:13 UTC
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `forward`
- Last commit: `9a601b7 Simplify to single-page Cleaner; remove multi-agent pipeline and Curator Hub`

**Uncommitted changes (git status --short):**
```
M .claude/settings.json
 M CLAUDE.md
 M SESSION_NOTES.md
 M memoryweaver/app/app_utils/genai_client.py
 M memoryweaver/app/deps.py
 M memoryweaver/app/fast_api_app.py
 M memoryweaver/app/routers/cleaner.py
 M memoryweaver/app/routers/photo.py
 M memoryweaver/app/routers/video.py
 M memoryweaver/app/services/classification.py
 M memoryweaver/app/services/media_store.py
 M memoryweaver/app/services/transforms.py
 M memoryweaver/app/services/video_ops.py
 M memoryweaver/app/sync_manager.py
 M memoryweaver/frontend/cleaner.html
 M memoryweaver/pipeline/cost_tracker.py
 M memoryweaver/pipeline/prompt_safety.py
?? CHEATSHEET.md
?? EFFICIENCY_PLAYBOOK.md
?? memoryweaver/app/app_utils/ai_budget.py
?? memoryweaver/tests/unit/test_cost_guardrails.py
?? memoryweaver/tests/unit/test_sync_folder_pool.py
?? memoryweaver/tests/unit/test_vault_path.py
?? tools/auto_session_notes.py
?? tools/claude_cost_log_hook.log
?? tools/claude_daily_log.csv
?? tools/claude_reports/
?? tools/daily_cost_log.py
```

**Unstaged diff summary:**
```
.claude/settings.json                       |  24 ++
 CLAUDE.md                                   | 335 +++++++++++-----------------
 SESSION_NOTES.md                            | 262 +++++++++++++++++++++-
 memoryweaver/app/app_utils/genai_client.py  |  49 +++-
 memoryweaver/app/deps.py                    |  25 ++-
 memoryweaver/app/fast_api_app.py            |   9 +-
 memoryweaver/app/routers/cleaner.py         |  51 ++++-
 memoryweaver/app/routers/photo.py           |   7 +-
 memoryweaver/app/routers/video.py           |   3 +-
 memoryweaver/app/services/classification.py |  17 +-
 memoryweaver/app/services/media_store.py    |   5 +-
 memoryweaver/app/services/transforms.py     |  13 +-
 memoryweaver/app/services/video_ops.py      |   4 +-
 memoryweaver/app/sync_manager.py            |  51 ++++-
 memoryweaver/frontend/cleaner.html          |  13 +-
 memoryweaver/pipeline/cost_tracker.py       |  31 +++
 memoryweaver/pipeline/prompt_safety.py      |  26 +++
 17 files changed, 665 insertions(+), 260 deletions(-)
```

**Staged diff summary:**
```
(none)
```
