# HARNESS.md — How to build in Rewind

> Product name: **Rewind** (formerly MemoryWeaver). The code directory stays
> `memoryweaver/` unless/until the repo is renamed — treat it as the repo slug.

> Read this before implementing any `specs/*.feature.md`. This is the "factory":
> the rules, guardrails, and shape of work that keep agent-generated code shippable.
> You (the coding agent) own the keystrokes; the human owns the specs, gates, and reviews.

## 0. Golden rule
Implement **one feature spec at a time**, end to end, until its eval gate is green.
The spec is the contract. If the spec is ambiguous, stop and ask — do not guess.

## 1. Where things live
```
memoryweaver/
  app/            # FastAPI harness: routes, session/workspace store
  agents/         # 5 specialists (collector/moderator/curator/memory/narrator)
  pipeline/       # deterministic batched orchestrator (the workhorse)
  frontend/       # upload.html (hub) · contribute.html · viewer.html
  specs/          # THIS folder — the source of truth
  tests/          # pytest: unit (security) + integration (agent stream)
  eval/           # eval_config.yaml + datasets/ (LLM-as-judge gate)
```
Design principle: heavy per-item work runs as **deterministic Python that calls
Gemini tools in batches**, not as per-item LLM tool-calling. Keep it that way —
the agent layer reasons at the *task* level, not the per-photo level.

## 2. Definition of done (the gate)
A feature is done only when, for its spec:
1. All Gherkin scenarios pass (BDD).
2. `eval/` LLM-as-judge score ≥ the spec's threshold on the golden dataset.
3. `pytest` green (unit + integration), incl. the spec's security cases.
4. Trajectory recorded (OpenTelemetry) with no intent drift / infinite loops.
5. Cost/latency within the spec's budget.
6. A plain-English change summary ("Vibe Diff") is produced for human review.

## 3. Security guardrails (non-negotiable — STRIDE + 7-pillar)
- **No secrets on the frontend.** API keys, admin tokens, share codes live server-side / in env.
- **Validate every upload:** mime allowlist, ≤ 20 MB, path-traversal-safe, per-type.
- **Strip EXIF** (GPS, device IDs) on anything served to the public `/media` endpoint.
- **No PII in logs or filenames** — use `contributor_id` hashes. Speaker *display names*
  may appear in UI/journal output, never in logs.
- **Sanitize** filenames and captions before they enter any Gemini prompt (injection guard).
- **Admin/billable endpoints** stay behind the `X-MW-Token` gate.
- Run agent-generated code in the **sandbox**; never grant ambient production credentials.

## 4. Data model (Phase 0 — build first)
```
Workspace 1─* Event 1─* MediaItem
MediaItem.type ∈ {photo, video, voice, note, doc}
```
- `MediaItem` carries: `type`, `contributor_id` (hash), `created_at`, derived
  `area_label` / `hour_of_day` (never raw coords), and type-specific fields
  (e.g. `transcript` for voice, `ocr_text` for doc, `duration` for video/voice).
- Everything downstream (pool, curation, viewer) filters/branches on `type`.

## 5. How to work
1. Copy the ticket's spec; re-read its YAML schema + Gherkin + eval cases.
2. Write/extend tests **first** from the Gherkin scenarios.
3. Implement the smallest change that makes them pass.
4. Run `pytest` + `eval/`; self-repair on failure.
5. Emit the change summary; hand back for HITL review.
6. Do **not** invent scope beyond the spec. Extra ideas → note them, don't build them.

## 6. Instruction hierarchy (Day 5)
System prompt (identity) > `specs/HARNESS.md` (global rules) >
`specs/<feature>.feature.md` (task) > chat (short-lived nudges).
When they conflict, the higher level wins.

## 7. UI directives (binding)
Product name is **Rewind**; accent is **Amaranth Pink `#EB2371`**. Every screen must
read from `specs/DESIGN_SYSTEM.md` — one accent token, two fonts (Space Grotesk +
Outfit), **Lucide icons only (no emoji)**, crisp radii (card 12 / tile 10 / pill 8).
Reference implementation: turn `5a` in `Rewind Flow.dc.html`. Do not hardcode the
hex or introduce new fonts/colors/radii.
