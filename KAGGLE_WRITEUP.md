# MemoryWeaver — Kaggle Capstone Writeup (draft)

> **Track:** Concierge Agents
> **Title:** MemoryWeaver: A Family Photo Concierge
> **Subtitle:** Five AI agents turn every family's post-trip photo chaos into a curated, narrated keepsake journal — no accounts, no apps, one shareable link.
>
> Fill before submitting: [REPO_URL], [VIDEO_URL], video timestamps marked [T=m:ss].
> Current length: ~1,900 words (limit 2,500).

---

## The problem

Every family trip, birthday, or wedding ends the same way: hundreds of photos scattered across half a dozen phones. Mom has 300, Dad has 150 near-duplicates of the same sunset, Grandma has twelve blurry gems and the only shot where everyone is smiling. Someone is supposed to gather them, delete the junk, pick the best, and turn them into something worth keeping — an album, a journal, a story.

Almost nobody does. The work is tedious (reviewing 500 photos), socially awkward (chasing relatives for their pictures), and skilled (writing captions, remembering which temple that was). The memories stay buried in camera rolls.

This is a concierge problem in the truest sense: a repetitive, multi-step, judgment-heavy chore that steals hours from busy parents — and it's exactly the shape of work agent systems are good at.

## Why agents?

A single prompt can't solve this. The job decomposes into genuinely different skills that benefit from separation:

- **Judgment at scale** — is this photo safe, sharp, and a real photo (not a meme or screenshot)? Is it a near-duplicate? Is it *good*?
- **Grounded narration** — turning GPS coordinates, timestamps, and visual content into accurate captions ("Bryce Canyon Visitor Center", not "a building") and warm, factual journal entries with no travel-blog clichés.
- **Social memory** — who contributed what, who was present at which moments, and who missed the waterfall hike and deserves a catch-up photo.
- **Orchestration** — a conversational layer that manages events end-to-end.

MemoryWeaver implements these as five specialist ADK agents — **Collector, Moderator, Curator, Memory, Narrator** — coordinated by a root **concierge agent** exposed over A2A, with an **MCP server** making the family's memory bank queryable from any MCP client.

## What we built

Two personas, deliberately on separate pages:

**Contributors** (the family) get a shareable link — `/join/<event>?code=…` — or a QR code. On their phones: type a name, pick photos from the camera roll, done. Desktop relatives can upload an entire folder at once (client-side filtered, batched). No accounts, no installs. When the journal is generated, the same page grows a "📖 The journal is ready — view it!" button. [T=m:ss]

**The organizer** gets the Curator Hub: create isolated event sessions (trip / birthday / wedding / sports match / reunion), share the link, watch photos arrive, then press one button to run the agent pipeline and watch live logs as the five agents work. [T=m:ss]

The pipeline: Gemini-vision **moderation** in batches of 50 (safety, sharpness, screenshot/meme rejection) → **CLIP-embedding deduplication** → **LLM-as-judge scoring** in batches of 15 (sharpness, composition, uniqueness, human presence — weighted toward candid human moments — plus landmark identification from GPS + vision, and caption writing) → **diversity-maximizing selection** across detected scenes so one photogenic beach doesn't crowd out the only shot of grandma's birthday cake → **memory bank update** (per-contributor presence by moment) → **batched narration** (every moment's journal entry in one call, then a flowing multi-paragraph story).

The result is a viewer page with a highlights carousel, per-moment journal entries with dates and captions, the full story, and a per-contributor filter — plus each contributor's page linking to it automatically.

The same system is drivable conversationally: ask the concierge in `agents-cli playground` to *"create an event called Summer Soccer Final"* or *"read me the story"*, and it operates on the same sessions the web UI shows — shared state, two doors into one house. [T=m:ss] The MCP server adds a third door: from Claude Desktop you can ask *"who contributed to the Bali trip and which moments did they miss?"*

## Architecture decisions worth defending

**Deterministic pipeline, agentic control plane.** The heavy per-photo work runs as orchestrated Python calling the agents' Gemini tools in *batches* — not as an LLM agent looping over photos with tool calls. For a 200-photo dump that's ~15 API calls instead of hundreds of agent turns: same output quality, a fraction of the cost and latency. The concierge reasons at the task level (which event, run or re-run, interpret results), where reasoning actually adds value. We think this division — deterministic where the work is mechanical, agentic where judgment routes the work — is the honest way to build production agent systems, and we say so in the code comments.

**Per-event session isolation.** Every event gets its own storage namespace, memory bank, and curation cache. This started as a feature (families have many events) and turned out to be the fix for a real concurrency bug: the original single global state meant two browser tabs could clobber each other's pipeline runs.

**Cost engineering throughout.** Batched vision calls; a per-photo curation cache so re-runs are near-free; and an optional on-device pre-clean for bulk imports (CLIP + blur detection on Apple Silicon) that filters 30–50% of a big folder *before* any API spend.

**Three-tier security matched to personas.** Admin token (`MW_ADMIN_TOKEN`) gates destructive/billable endpoints; a per-event `share_code` embedded in the contributor link gates uploads (strangers can't dump photos into your album by guessing an event ID); read paths are open but photos are re-encoded with **all EXIF stripped** — raw GPS coordinates and device IDs never leave the server. Filenames are sanitized before entering Gemini prompts (prompt-injection guard). The MCP server is deliberately read-only so it cannot bypass the web auth layer. These map to the STRIDE threat model we wrote in `CONTEXT.md` before building.

## The build — a vibecoding journey

The project was scaffolded with **`agents-cli`** (ADK 2.0 + A2A template) and built conversationally with AI pair-programming end to end — including this writeup. The journey had real course-corrections that shaped the final system:

1. **v0 was a sports MVP** (FIFA26 watch-party photos) — it proved the pipeline concept but hardcoded one event; the multi-event session architecture came from generalizing it.
2. **Dogfooding found the real bugs.** Testing multi-session with a real 125-photo trip surfaced a filename-instability bug where re-ingesting photos orphaned every generated artifact *and* silently invalidated the paid-for scoring cache. The fix (deterministic content-stable filenames) came from watching a real user — one of us — get confused.
3. **The two-persona split came from UX pain, not planning.** The original single page mixed "grandma uploading" with "admin running pipelines"; one confusing button-press that destructively re-ingested photos convinced us to separate `/join` from the Curator Hub entirely.
4. **An agent-layer audit mid-build** found that our five agent definitions had drifted from their refactored tools (broken imports nobody noticed, because the pipeline called tools directly). Wiring the concierge as a real ADK root agent with the specialists as `sub_agents` — 23 skills on the A2A card — made the agent system real rather than decorative.

## Course concepts demonstrated

| Concept | Evidence |
|---|---|
| Multi-agent system (ADK) | `app/agent.py` (root concierge, 6 tools, 5 sub-agents); `agents/*/agent.py`; A2A card with 23 skills — code + [T=m:ss] |
| MCP server | `mcp_server.py`, 4 read-only stdio tools; Claude Desktop config in docstring — code + [T=m:ss] |
| Security features | Token auth, share-code upload credential, EXIF-stripping media endpoint, prompt-injection sanitizer (`pipeline/prompt_safety.py`) — code |
| Agent skills (Agents CLI) | Scaffolded and driven via `agents-cli` (manifest in repo); playground demo — [T=m:ss] |
| Deployability | Dockerfile + Terraform (Cloud Run, GCS, Cloud Trace/BigQuery telemetry); reproduction steps in README — video [T=m:ss] |

## Results

On a real 125-photo family trip (mixed iPhone HEIC + DSLR JPEG): moderation, dedup, scoring, and narration completed in a few minutes cold; re-runs complete in seconds from cache. The journal named real landmarks from GPS+vision, grouped 48 distinct moments chronologically, and the diversity selector kept people-shots, food, and scenery balanced instead of returning 50 sunsets. Estimated Gemini cost for the full cold run: under a dollar — the app itself reports per-run token and cost estimates in its logs.

## Limitations and roadmap

Photos only for now (video moderation/curation is the next milestone); pipeline progress state is in-memory (single-instance; Redis/Firestore for scale); the share code travels in the URL — the right trade-off for "grandma scans a QR," acknowledged openly. Roadmap: event-type-aware narration (a soccer final shouldn't read like a travel diary), face-aware people naming with an opt-in family roster, and "you missed this moment" digest emails per contributor.

## Links

- **Code:** [REPO_URL] — README includes verified quick-start, architecture diagram, and deployment reproduction
- **Video:** [VIDEO_URL]
- **Demo:** run locally in ~3 commands (see README quick start)
