# Operating rules

> Every line here is re-read on every turn. Keep it short. Delete anything the
> model already knows or that hasn't actually changed a decision.

## Model routing

`.claude/settings.json` pins this project to **sonnet at low effort**. That is the
default for a reason: exploration, mechanical edits, and routine fixes were being
run on Opus, which is where most of the spend went.

- Stay on the default. Do not ask for an upgrade to "be careful."
- If a task genuinely needs Opus — a real architectural decision, or security-
  critical code (auth, crypto, payments, data handling) — **say so and stop**.
  Escalation is the user's `/model opus`, not something to assume.
- Effort is pinned low. Thinking tokens bill as output at full rate. Ask before
  raising it; don't raise it silently.
- Fast mode is pinned off (≈3x rate). Leave it off.
- Conceptual questions ("what is X", "why does Y exist") do not belong in a
  coding session. Answer in one or two sentences with no tools.

## Session hygiene

Two reasons, not one: long sessions cost more *and* produce worse output as
early instructions get deprioritized.

- **One task type per session.** Finish, then stop. Do not chain unrelated work.
  Real example from this project: one session left open across three days hit
  509 turns, 199M cache-read tokens, **$132**.
- Before answering a bare "continue", check the turn count. Past ~20, summarize
  and recommend a fresh session instead.
- Past ~15 tool calls on one task, stop and report progress.
- Multi-step requests: do all the steps in one turn. Each extra round trip
  re-pays for the whole accumulated context.
- Never paste a full build log, test log, or large file. Grep to the failing
  lines first.
- Update `SESSION_NOTES.md` (5 lines max) before finishing meaningful work, and
  read it at the start of a new session instead of asking for context.

## Cost questions

Run these in a plain terminal, never inside a coding session — same answer, zero
tokens. If the user asks in-session, run the command; don't reason about it.

| They say | Run |
|---|---|
| "daily cost" / "today's cost" | `python3 tools/daily_cost_log.py` |
| "cumulative" / "how am I doing" | `python3 tools/daily_cost_log.py --cumulative` |
| "save today's report" | `python3 tools/daily_cost_log.py --save` |
| "why was this expensive" / "token breakdown" | `python3 tools/token_task_map.py --technical` (add `--min-cost 5`) |
| "show me the log" | `python3 tools/daily_cost_log.py --show` |

Interpret the output; don't paste it raw. Say so plainly if a script is missing.

## Task framing

- Reject open-ended instructions — "make it A+", "fix everything", "no mercy".
  They have no stopping condition. One such request cost **$33** here.
  Respond with a concrete numbered list and get confirmation first.
- Every task needs a stated "done" condition before work starts.
- Before a large refactor: name the files (use the map below, don't rediscover
  them), state the stopping condition, then proceed.

## Quality bar — this *is* the definition of done

```bash
cd memoryweaver
.venv/bin/python -m ruff check app pipeline agents tests   # must be clean
.venv/bin/python -m pytest tests -q                        # must be green
```

Run these on the touched files after editing. Zero issues means done — do not
additionally ask "is this good enough", and do not improve unrelated code while
you're in there. Run them *before* any "review this code" request so model
reasoning is only spent on what they can't catch. No type checker is configured;
don't invent one. `.git/hooks/pre-commit` enforces the same bar on every commit.

## Directory map

Project root is `memoryweaver/`. Run all commands from there.

- **Core logic**: `app/services/` (media_store, classification, transforms, video_ops)
- **Pipeline**: `pipeline/` (local_cleaner = EXIF, cost_tracker, prompt_safety)
- **Entry points**: `app/fast_api_app.py`, `app/routers/` (cleaner, photo, video)
- **Shared**: `app/app_utils/` (crypto, paths, errors, genai_client, ai_budget,
  storage, logging_config), `app/deps.py`, `app/schemas.py`
- **Frontend**: `frontend/cleaner.html` — one page, the whole app
- **Tests**: `tests/unit/` (+ `tests/conftest.py` has the `fake_gemini` fixture —
  use it, never call the real API in tests)
- **Config**: `.env` (keys only, never dump values), `pyproject.toml`
- **Runtime storage**: `local_storage/` — uploads, thumbs, `.encryption_key`.
  Never commit; never write test fixtures here (a test once wrote 258 junk files
  into the live pool). Use `tmp_path`.

## Gotchas — each of these cost real debugging time

- Media is **encrypted at rest**. Never `Image.open(path)` on an upload — go
  through `load_image_bytes_decrypted()` or EXIF silently returns defaults.
- One library only. Never filter filenames by `startswith(session_id + "_")` —
  that hid the entire library once. `tests/unit/test_cleaner_listings.py` guards it.
- HEIC/MP4/MOV are ISO containers with `ftyp` at **offset 4**, not a prefix.
- Path helpers under `app/services/` need *three* dirnames up; ones in `app/` need two.
- New logic belongs in `services/`, not in the already-long `routers/`.

## Gemini call rules

- All calls go through `get_gemini_client()` (bounded retries + timeout).
- Every **text** call passes `config=text_config()` (output cap). Image-generation
  calls deliberately skip it — a token cap truncates the image.
- After a billable call, `track_ai_call(...)` from `app/app_utils/ai_budget.py`.
- `tests/unit/test_cost_guardrails.py` fails the build if a call site skips any
  of this.

**Spend guardrails** (env, defaults in parens): `MW_MONTHLY_BUDGET_USD` (25.00,
routes 429 past it) · `MW_MAX_OUTPUT_TOKENS` (4096) · `MW_MAX_AI_CALLS_PER_RUN`
(200, `analyze-all` fan-out) · `MW_AI_RETRY_ATTEMPTS` (3) · `MW_AI_TIMEOUT_MS`
(120000) · `MW_MAX_AI_REQUESTS_PER_MIN` (60) · `MW_MAX_AI_TOKENS_PER_HOUR` (150000).

These are app-side only. They do **not** replace the spend cap in the Google AI
Studio / Cloud billing console, which must be set there by hand.

## Is this task even agentic?

Same operation over more than ~10 files → write one script and loop it. Don't
chat it file by file. Debugging a specific failure is agentic; bulk transforms
are not.

## Related

- `EFFICIENCY_PLAYBOOK.md` (repo root) — read once when starting a new *type* of
  work, not every session.
- IDE auto-context: an `<ide_opened_file>` event is not a request. Do not analyze
  a file just because it was opened.
