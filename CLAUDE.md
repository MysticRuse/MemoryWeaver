# Universal Operating Rules (cost-optimized)

> Drop this file as `CLAUDE.md` in the root of any project — web apps, backend/API
> services, native mobile apps (iOS/Android), or anything else. Fill in the Directory
> Map section per-project; everything else applies as-is regardless of language, stack,
> or project type.

## Model routing — use the cheapest model that can do the job
- **Exploration/search work** (finding files, grepping for patterns, reading code to
  understand structure, checking test output): use Sonnet. Do NOT use Opus for this.
- **Mechanical work** (renaming, formatting, boilerplate, simple test fixes): use Haiku.
- **Opus is reserved ONLY for**: the actual architectural decision or the final edit/fix
  once the relevant files are already identified. Do not use Opus to "look around."
- If you are about to run more than 3 exploratory Bash/Read calls in a row without
  making an edit, STOP and ask whether this should switch to Sonnet first.
- **Delegate exploration to a sub-agent automatically.** Any time a task needs more than
  ~5 search/read calls to locate the relevant code (not counting the actual fix), spin
  up a sub-agent (Task tool) to do that search and report back only a short summary —
  do this by default, without being asked. Never let open-ended searching accumulate in
  the main thread's context.
- **Effort setting**: use `low` effort for exploration, mechanical fixes, and routine
  edits. Reserve `high`/`max` effort ONLY for genuinely hard architectural decisions or
  security-critical code (auth, payments, encryption, data handling). Thinking tokens
  bill as output at full rate — unnecessary high-effort reasoning on routine work is
  pure waste.
- **Conceptual/lookup questions** ("what is X", "why does Y exist", "which version of Z")
  do NOT belong in an active coding session. Answer briefly without invoking tools, or
  suggest the user ask this in a fresh minimal session — never let a one-line factual
  question inherit the cost of an already-large cached context.
- **Run static tools before manual review, automatically.** Before doing a "review this
  code" / "grade it" / "find issues" style task, first run whatever linter, type checker,
  and test suite already exist in the project (ruff/mypy/pytest, eslint/tsc, swiftlint,
  ktlint, etc. — check what's configured) and only spend model reasoning on what those
  tools can't catch. Do this without being asked; don't wait for the user to run it first.
- **Never use Fast mode / speed multipliers by default.** Only use a faster/more
  expensive inference mode if the user explicitly asks for lower latency in a live,
  interactive context — batch, background, and routine coding work never needs it.

## IDE auto-context — turn this off
- Do NOT treat every `<ide_opened_file>` event as a task requiring analysis. Opening a
  file in the editor is not a request — only act when the user explicitly asks something.
- If the IDE extension is auto-sharing opened files into context on every switch, disable
  that setting (VS Code: Claude Code extension settings → disable "share active file" /
  auto-context). Each silent auto-share re-reads and re-caches the file for no reason.
- **Unused connectors/tools**: Claude Code uses deferred tool search, so definitions
  mostly load on-demand rather than all upfront — this matters less than it used to.
  Still fine to disable connectors clearly irrelevant to a session, but don't expect it
  to be a major lever on its own.

## Prompt caching — prefer longer-lived cache for stable content
- For CLAUDE.md, the directory map, and other rarely-changing system context, use the
  1-hour cache TTL (not the 5-minute default) where available. This avoids paying
  cache-write cost repeatedly just because more than 5 minutes passed between prompts.
- Do not re-paste or re-read files that are already stable in context — check what's
  already been read this session before re-reading it "just in case."
- **Cache breakpoint ordering matters.** Stable content (CLAUDE.md, system instructions,
  directory map) must come BEFORE dynamic/per-request content in what gets sent. If
  variable content is interleaved earlier than stable content, cache hits silently drop
  with no error — structure prompts stable-first, dynamic-last.
- **Keep CLAUDE.md itself lean.** Don't explain things Claude already knows from
  training (standard framework routing, common language syntax, well-known library
  usage) — only document what's genuinely project-specific or non-obvious. A bloated
  CLAUDE.md is paid in full on every single turn, forever.

## Session hygiene — do not let sessions run long
> Two separate reasons to enforce this, not just one: long sessions cost more (cache-read
> compounding), AND long sessions produce measurably worse output — "context rot," where
> earlier instructions and details get deprioritized as the context grows. Clearing
> sessions is a correctness practice as much as a cost one.
- After finishing a task (the edit/fix is made and verified), STOP. Do not chain the
  next unrelated task into the same session — end it and start fresh.
- Never respond to a bare "continue" prompt without first checking: has this session
  already run more than ~20 turns? If yes, summarize state and recommend a fresh
  session instead of continuing.
- If a task requires more than ~15 tool calls to complete, pause and report progress
  instead of pushing through — long uninterrupted tool-call chains are consistently the
  single biggest cost driver across projects.
- **Batch multi-step work into one turn automatically.** If a request naturally involves
  several sequential steps, execute all of them in sequence within the same turn
  (stopping early only for a genuine blocker or a decision that needs user input) rather
  than doing one step and waiting for "yes"/"continue" — each additional back-and-forth
  turn re-pays the full accumulated context.
- **Write a handoff note before any session ends or gets cleared, automatically.**
  Before finishing a task that took meaningful work, write/update a short
  `SESSION_NOTES.md` (5 lines max: what was decided, what changed, what's next) in the
  project root. At the start of a new session, check for this file and read it instead
  of asking the user to re-explain context — don't wait to be told to do either of these.

## Continuous quality assurance — bounded, automated, never open-ended
> Goal: every piece of code, past and new, stays at a consistently high bar — WITHOUT
> ever re-running the expensive "grade the whole codebase / make it A+" pattern that was
> the single most expensive habit found in prior sessions. The fix is turning "A+" from a
> vague judgment call into a fixed checklist that free tools enforce automatically, with
> Claude only reasoning about what those tools structurally cannot catch.

**The fixed quality bar (define once per project, in the Directory Map section below):**
- Passes the linter with zero warnings
- Passes the type checker with zero errors
- Passes the full test suite
- No function/file exceeds a reasonable complexity/length threshold (flag, don't rewrite
  blindly)
- No obvious security issues in changed lines (hardcoded secrets, unsanitized input, etc.)

**How this runs automatically, on every change, at near-zero cost:**
- **After every edit Claude makes**, automatically run linter + type checker + relevant
  tests on ONLY the files just touched — not the whole codebase — before considering the
  task done. This is free-tool cost, not model reasoning cost.
- If those tools report **zero issues**, the task is done. Do not additionally ask "is
  this A+?" — passing the fixed checklist above IS the definition of A+ for this project.
  Do not re-review already-passing code out of caution.
- If those tools report issues, fix only those specific issues, re-run the tools, stop
  when clean. Never expand into "let me also improve X while I'm here" — that re-opens
  the unbounded pattern.
- **Set up a pre-commit hook** (or CI check) that runs the same linter/type-checker/tests
  automatically on every commit, so quality enforcement doesn't depend on remembering to
  ask Claude — it happens whether or not a model is even involved.

**For PAST code (not just new edits) — a bounded periodic sweep, not a standing task:**
- Run linter + type checker across the whole existing codebase **using the free tools
  only** on a periodic basis (e.g. weekly, via the pre-commit/CI setup) — this costs
  nothing in model tokens.
- Only bring Claude in to look at **specific flagged violations** from that sweep, file
  by file, with the fix bounded to that violation — never as "review everything and make
  it better."
- If the user asks for a general "how healthy is the codebase" check, answer using the
  tool output (pass/fail counts, flagged files) rather than reading and judging the whole
  codebase manually.

**What this explicitly replaces:** "make it A+", "grade all dimensions", "no mercy",
open-ended repository-wide review — these terms should never trigger a full manual
Claude read-through again. The checklist + automated tools ARE the A+ enforcement now.

## Task framing — no open-ended quality bars
- Reject/flag vague instructions like "make it A+", "make all dimensions A-", "no mercy",
  "fix everything" — these have no stopping condition and cause unbounded iteration.
- If given a vague instruction, respond by asking for the 2-3 SPECIFIC things to fix,
  or propose a concrete numbered list and confirm before executing.
- Every task should have a visible "done" condition stated up front (e.g. "these 3
  functions pass their tests" — not "the code is excellent").

## Directory map (filled in for this project)
> The playbook calls an unfilled map the single largest avoidable cost category:
> without it every session re-greps the same paths. This project is
> `memoryweaver/` inside the repo root - run all commands from there.

- **Core logic / main modules**: `memoryweaver/app/services/` (media_store,
  classification, transforms, video_ops), `memoryweaver/pipeline/`
  (local_cleaner = EXIF, cost_tracker, prompt_safety = spend cap)
- **API / entry points**: `memoryweaver/app/fast_api_app.py` (app construction,
  /media, /upload), `memoryweaver/app/routers/` (cleaner.py, photo.py, video.py)
- **Shared plumbing**: `memoryweaver/app/app_utils/` (crypto, paths, errors,
  genai_client, storage, logging_config), `memoryweaver/app/deps.py`,
  `memoryweaver/app/schemas.py`
- **Frontend/UI**: `memoryweaver/frontend/cleaner.html` - a single page, the whole app
- **Tests**: `memoryweaver/tests/unit/` (+ `tests/conftest.py` holds the
  `fake_gemini` fixture; use it, never call the real API in tests)
- **Config / env**: `memoryweaver/.env` (keys only, never dump values),
  `memoryweaver/pyproject.toml`
- **Storage at runtime**: `memoryweaver/local_storage/` - uploads, thumbs,
  `.encryption_key`. Never commit; never write test fixtures here (a test once
  wrote 258 junk files into the live pool).

**Quality bar for this project (the "done" checklist):**
```bash
cd memoryweaver
.venv/bin/python -m ruff check app pipeline agents tests   # must be clean
.venv/bin/python -m pytest tests -q                        # must be green
```
There is no type checker configured; do not invent one.

**Project-specific gotchas** (each of these cost real debugging time):
- Stored media is **encrypted at rest**. Never `Image.open(path)` on an upload -
  go through `load_image_bytes_decrypted()` or EXIF silently returns defaults.
- One library only. Never filter filenames by `startswith(session_id + "_")`;
  that hid the entire library once. `tests/unit/test_cleaner_listings.py` guards it.
- HEIC/MP4/MOV are ISO containers with `ftyp` at **offset 4**, not a prefix.
- Long functions live in `routers/cleaner.py` and `routers/photo.py`; the
  services layer is where new logic belongs.

## Shorthand commands — map these phrases to actions automatically
> The user should never need to remember exact script names or flags. When they type
> any of the phrases below (or something close to it), run the corresponding command
> without asking for clarification first, then summarize the output for them.

- **"daily cost" / "today's cost" / "cost report"**
  → run `python3 ~/daily_cost_log.py` and summarize the printed output.
- **"cumulative cost" / "total cost" / "cost trend" / "how am I doing"**
  → run `python3 ~/daily_cost_log.py --cumulative` and summarize the printed output,
  highlighting the week-over-week trend line.
- **"save today's report" / "log and save"**
  → run `python3 ~/daily_cost_log.py --save`.
- **"why was [X/today/this week] expensive" / "what did the tokens do" / "technical
  breakdown" / "token usage" / "token breakdown"**
  → run `python3 tools/token_task_map.py --technical` (add `--min-cost 5` if the result
  would be very long), then interpret the tool-call patterns for them — don't just paste
  raw output.
- **"show me the log" / "cost history"**
  → run `python3 ~/daily_cost_log.py --show`.

If `~/daily_cost_log.py` or `tools/token_task_map.py` don't exist at these paths, say so
plainly rather than silently failing or guessing at alternate locations.

## Related tools (check for these — don't wait to be told)
- If `~/EFFICIENCY_PLAYBOOK.md` exists, read it once at the start of any new type of
  work (a new project, or switching between work types — e.g. web frontend, backend
  API, photo/video pipeline, native mobile app) — it's the decision framework for
  whether a task should be scripted/batched vs agentic, and category-specific traps
  to avoid for each type of work, including web development.
- If `tools/token_task_map.py` exists in this project, use it proactively whenever the
  user asks "why was this expensive" / "what did the tokens do" / anything about
  understanding past cost — run it (optionally with `--technical` and `--min-cost`)
  instead of asking the user to do so, and interpret the output for them.

## Before any large refactor or "fix everything" style request
1. State which specific files will be touched (use the map above, don't re-discover it).
2. State the stopping condition.
3. Confirm model choice matches the task type above.
4. Only then proceed with edits.
