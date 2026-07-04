# MemoryWeaver — Capstone Submission Sprint Plan

**Goal:** Get MemoryWeaver submission-ready for the Kaggle "AI Agents: Intensive Vibe Coding Capstone Project" within 2 days.

**Deadline reminder:** Kaggle Writeup (≤2,500 words) + Media Gallery + a public YouTube video (≤5 min) + a public project link, all attached and submitted before the competition closes. Draft/unsubmitted writeups are not considered.

---

## 1. Why this plan looks the way it does

The rubric requires demonstrating **at least 3 of these 6 concepts**, each scoreable via code or video:

| Concept | Where judged | Our current status |
|---|---|---|
| Agent / Multi-agent system (ADK) | Code | **Broken** — 3 of 5 agent modules fail to import; the live `root_agent` is an unmodified weather-bot scaffold, not MemoryWeaver |
| MCP Server | Code | **Absent** — not used anywhere yet |
| Antigravity | Video | Just needs to be shown/mentioned in the video |
| Security features | Code or Video | **Documented but not implemented** — `CONTEXT.md` promises protections the code doesn't enforce |
| Deployability | Video | Terraform/Docker exist, but a macOS-only AppleScript call is in the production request path and will break off-Mac |
| Agents CLI | Code or Video | Present via scaffold tooling |

We currently can defensibly claim ~1-2 of the 6. We need 3+. That's the binding constraint, not polish — so the plan below prioritizes fixing the agent architecture and adding a real MCP server over cosmetic work.

Score weighting for context: Category 1 "Pitch" (Core Concept 10 / Video 10 / Writeup 10 = 30 pts) and Category 2 "Implementation" (Technical 50 / Documentation 20 = 70 pts). The agent-architecture fix moves the needle on both the biggest single line item (Technical Implementation, 50 pts) and the "agent use is clear, meaningful and central" criterion in Core Concept.

**Also folded into this plan:** multi-session/multi-event support. The current app can only hold one active trip at a time (global state in `fast_api_app.py`, a single flat `MemoryBankStore` file) — this is both a real concurrency bug and a strong pitch upgrade ("an agent that manages your family's ongoing events, not a one-shot script"). We're fixing the underlying architecture for real, but only fully building out prompts/taxonomy for 1-2 event types (trip + one more); further event types are "designed for" and mentioned as roadmap in the writeup/video, not fully built and tested.

**No API keys or secrets in the submitted repo.** Verified clean as of this writing (`git ls-files` shows no `.env`, `client_secret`, or key files tracked) — keep it that way. `memoryweaver-git-crypt.key` at the repo root is currently untracked but **not** gitignored; either add it to `.gitignore` or move it out of the repo before any broad `git add`.

---

## 2. Work split

**Partner A — Backend & Agents.** Owns: agent wiring, session/event architecture, MCP server, backend security.
**Partner B — Frontend, Product & Content.** Owns: upload UX, session UI, README, Writeup, video.

Split this way to minimize file collisions — Partner A works mostly in `agents/`, `pipeline/`, and `app/fast_api_app.py` route logic; Partner B works mostly in `frontend/` and root-level docs. The one shared dependency (session-aware API endpoints) is called out below with a sync point.

---

## Day 1 (parallel — target ~10-11 focused hours each)

### Partner A — Backend & Agents

| # | Task | Est. | Notes |
|---|---|---|---|
| A1 | Fix the 3 broken agent imports | 0.5 hr | `agents/moderator/agent.py`, `agents/narrator/agent.py`, `agents/curator/agent.py` import old singular function names; the real functions are the `*_batch` versions |
| A2 | Build session/event architecture | 2.5-3 hrs | Add `session_id`; partition `local_storage/<session_id>/{uploads,thumbs,artefacts}`; make `MemoryBankStore` session-scoped instead of one flat JSON file; add an `event_type` field (trip / birthday / wedding / match / other) to session metadata |
| A3 | Wire `root_agent` as a real multi-agent orchestrator | 3-4 hrs | Replace the weather-bot stub in `app/agent.py` with a `SequentialAgent` (or equivalent) composing the fixed moderator → curator → memory → narrator agents, operating on session-scoped data. Verify via `agents-cli playground` that this is the live product, not a toy |
| A4 | Backend security pass | 2-2.5 hrs | Add auth (simple bearer/invite token is fine) on `/delete` and `/generate`; stop publicly serving raw uploads via the `/local_storage` static mount; sanitize filenames before they're interpolated into Gemini prompts (prompt-injection risk) |
| A5 | Minimal MCP server | 2-3 hrs | Expose Memory Bank data (contributor profiles, event context, missed-moments) as 2-3 MCP tools/resources. **First thing to cut if behind schedule.** |

### Partner B — Frontend, Product & Content

| # | Task | Est. | Notes |
|---|---|---|---|
| B1 | Remove/guard the AppleScript folder-picker from the public path | 1-1.5 hrs | `/api/select-folder` shells out to macOS AppleScript and will fail anywhere else. Confirm the browser-based multi-file `/upload` endpoint is the primary, working public journey |
| B2 | Minimal session UI | 2.5-3 hrs | "Create new event" (name + type + date) + a session switcher/list. Build against a mock/static API first; wire to Partner A's real session endpoints once they land (see sync point below) |
| B3 | Rewrite `README.md` | 2 hrs | Architecture description, setup instructions, diagram — this is a separate 20-point rubric line item ("Documentation") and the current README is the unedited scaffold template |
| B4 | First-pass draft of the Kaggle Writeup | 2 hrs | Problem statement, solution, architecture, track selection (see Section 4 below) |
| B5 | Video script draft | 1 hr | See required beats in Section 4 |

### End-of-day-1 sync (both, 30-60 min)

Merge both branches. Run the full flow together end-to-end: upload → moderate → curate → journal → story, through the new session-aware multi-agent path. Fix integration breaks immediately — don't let them roll into Day 2.

---

## Day 2

### Morning (both together, ~2-3 hrs)

QA pass:
- Create **two separate events side by side** and confirm they stay fully isolated (proves the session fix actually works)
- Exercise the MCP server with a client (MCP Inspector or similar)
- Confirm the new auth actually blocks unauthenticated requests to `/delete` and `/generate`
- Fix whatever breaks

### Afternoon (split again)

| Partner A (~4-5 hrs) | Partner B (~5-6 hrs) |
|---|---|
| Fix bugs found in QA | Record the video against the now-working product |
| Sanity-check Docker build / Terraform; confirm deploy docs are accurate (live deployment isn't required for judging, but documentation to reproduce it is) | Edit the video |
| Finalize technical accuracy of the architecture diagram and README | Finalize the Writeup; attach Media Gallery images + the video |
| Support Partner B on technical accuracy in the writeup | Submit: Writeup + Media Gallery + video + project link |

### Final 30 minutes (together)

Joint submission checklist — see Section 5.

---

## 3. If you fall behind

Cut in this order:
1. MCP server (drop to 1 tool, or cut entirely and lean on Agent/Multi-agent + Deployability + Agents CLI for your 3-of-6)
2. Full multi-event UI polish — keep the backend session architecture (it's real and it's the bug fix), drop the polished switcher UI, demo session isolation via API calls/logs instead
3. Security pass depth — keep it to just the two destructive/costly endpoints (`/delete`, `/generate`)
4. Video polish — rough-but-clear beats late-but-slick

---

## 4. Writeup / Video content checklist

Track: pick one of Agents for Good / Agents for Business / **Concierge Agents** (best fit — family/personal event management, "keeps personal information safe and secure" framing) / Freestyle.

Video (≤5 min, published to YouTube) should hit:
- **Problem statement** — why curating/journaling group event photos is a real, tedious problem
- **Why agents** — why this specifically needs agentic reasoning/tool use, not a plain script
- **Architecture** — show the multi-agent diagram (collector → moderator → curator → memory → narrator)
- **Demo** — the working product, ideally showing two separate events/sessions to land the multi-event story
- **The build** — ADK, Gemini, MCP server, Agents CLI, deployment tooling

Writeup (≤2,500 words) mirrors the same structure in text, plus explicit callouts of which of the 6 key concepts are demonstrated and where (code file/line or video timestamp) — make this easy for a judge to verify quickly.

---

## 5. Final submission checklist

- [ ] Writeup is ≤2,500 words
- [ ] Cover image attached to Media Gallery
- [ ] Video attached, ≤5 minutes, published on YouTube
- [ ] Project link is public and does not require login
- [ ] Track selected on the Writeup
- [ ] No API keys, secrets, or credentials anywhere in the submitted repo (re-check `git ls-files` before final push)
- [ ] `memoryweaver-git-crypt.key` is gitignored or removed from the repo
- [ ] At least 3 of the 6 key concepts are clearly demonstrable in code or video, and called out explicitly in the writeup
- [ ] Writeup saved and **Submit** button clicked (draft writeups are not considered)
