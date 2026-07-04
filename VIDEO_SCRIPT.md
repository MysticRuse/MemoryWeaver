# MemoryWeaver — 5-Minute Video Shooting Script

Target: 4:50 runtime (10s buffer under the 5:00 limit). Narration is written for
a natural ~140 words/min pace — read it aloud once with a timer before recording.

---

## Prep checklist (do ALL of this before recording)

- [ ] **Restore demo data**: `rm -rf local_storage && mv local_storage.backup-0703-2351 local_storage` (or keep a freshly curated event ready). The pipeline must NOT run cold on camera — a cached event completes in seconds and looks just as real.
- [ ] Delete test litter: `rm -f local_storage/uploads/*passwd_trip.jpg`, and remove empty test events from `sessions_index.json` (birthday / soccer / new-event shells) so the session dropdown looks clean.
- [ ] Start server with `APP_URL=http://<your-LAN-IP>:8000` so the QR code is phone-scannable on camera.
- [ ] **Pre-record the phone clip** (Scene 4): screen-record your iPhone scanning the QR, typing "Grandma Lena", picking 4-5 photos, seeing "✅ added". 20 seconds, done in advance, dropped in during edit.
- [ ] Open in browser tabs, in order: ① Curator Hub ② a contributor `/join` link ③ `/viewer` for the curated event ④ `adk web` playground with `app` selected, empty new session ⑤ Claude Desktop with the MCP server configured (optional).
- [ ] Have `README.md` architecture diagram, `Dockerfile`, and `deployment/terraform/` visible in your editor for Scene 7.
- [ ] Note your timestamps as you edit — they go into KAGGLE_WRITEUP.md's [T=m:ss] slots.

---

## Scene 1 — The problem (0:00–0:30) · 30s

**On screen:** A messy camera roll scroll (your own phone gallery), then family photos scattered across devices — or simple title cards.

**Narration (~68 words):**
> "Every family trip ends the same way. Three hundred photos on Mom's phone. A hundred fifty near-identical sunsets on Dad's. The only picture where everyone's smiling? On Grandma's phone, and no one will ever see it. Someone is supposed to collect them all, delete the blurry ones, and make something worth keeping. Nobody ever does. We built MemoryWeaver so nobody has to."

## Scene 2 — Why agents (0:30–0:55) · 25s

**On screen:** Title card listing the four skills, or the five agent names appearing one by one.

**Narration (~58 words):**
> "This isn't one AI task — it's five different jobs. Judging five hundred photos for safety and quality. Spotting duplicates. Scoring what's actually good. Remembering who was where. And writing a story that names the real places. So we built five specialist agents with Google's Agent Development Kit — coordinated by a concierge agent you can talk to."

## Scene 3 — Architecture (0:55–1:25) · 30s

**On screen:** The Mermaid diagram from the README (or the workflow diagram). Cursor traces the flow as you speak.

**Narration (~70 words):**
> "Here's the architecture. Contributors upload through a shareable link — no accounts. The organizer runs a five-agent pipeline: Moderator screens with Gemini vision, Curator de-duplicates with CLIP embeddings and scores with an LLM judge, Memory tracks who was present at what, and Narrator writes the journal. One deliberate decision: the per-photo work runs in batches — fifteen API calls instead of hundreds of agent turns. The agents reason where reasoning matters."

## Scene 4 — Demo part 1: collect (1:25–2:10) · 45s

**On screen:** Curator Hub. Create event "Bali Family Trip" → Share & Collect card appears with link + QR (5-8s). Cut to the **pre-recorded phone clip**: scan QR → contributor page → name → photos → "✅ added" (20s). Back to hub: photo count updated (5s).

**Narration (~100 words):**
> "Let me show you. In the Curator Hub, I create an event — every event gets its own isolated storage and memory. The Share and Collect card gives me a link and QR code — the link itself carries the upload credential, so there's nothing for grandma to sign up for. She scans it, types her name, picks her photos — done. Desktop relatives can drop in a whole folder at once. Back in the hub, the photos are arriving, tagged by contributor. When everyone's chipped in, one button starts the agents."

## Scene 5 — Demo part 2: curate & view (2:10–3:00) · 50s

**On screen:** Click **▶ Curate & Narrate Now** → live logs scroll (moderation batches, duplicates dropped, scoring, narration) — 10-15s of real log footage, sped up if needed. Then the viewer: highlights carousel, scroll two journal moments with captions/dates, the trip story, the contributor filter dropdown. End on the phone: contributor page now shows "📖 The journal is ready."

**Narration (~110 words):**
> "The pipeline runs live — you can watch the Moderator quarantine a screenshot, the Curator drop burst duplicates and score what's left, and the Narrator write. A diversity selector makes sure the journal balances people, food, and places — not fifty sunsets. And here's the result: a highlights reel, then moment-by-moment journal entries — notice it named the actual landmark from GPS and vision, with the date, in a warm, factual voice. A full trip story ties it together. You can filter to just Grandma's moments. And on her phone, the share page now says: the journal is ready. She taps, she reads. That's the product."

## Scene 6 — The agent layer (3:00–3:55) · 55s

**On screen:** `adk web` playground. Type: *"What events do I have?"* → response lists the event you just made in the browser (pause on this!). Then: *"Read me the story for the Bali trip."* → concierge quotes the journal. Open the **Traces tab** briefly — tool-call chain visible. Flash the A2A agent card JSON (23 skills). If using MCP: quick cut of Claude Desktop answering "who contributed?"

**Narration (~120 words):**
> "Everything you just saw is also drivable by conversation. This is the concierge — the root ADK agent, with all five specialists attached as sub-agents. I ask what events exist — and it sees the exact event I just created in the browser. Same sessions, same storage: the agent isn't a bolted-on chatbot, it's a second door into the same system. I ask for the story, and it fetches the real artifact through its tools — here's the trace. The whole system is exposed over the A2A protocol — twenty-three skills on the agent card — and a Model Context Protocol server makes the family memory bank queryable from any MCP client, like Claude Desktop. Read-only, so it can never bypass the auth layer."

## Scene 7 — The build (3:55–4:25) · 30s

**On screen:** Quick cuts: `agents-cli-manifest.yaml`, the security code (auth dependency + `prompt_safety.py`), `Dockerfile` + `terraform/` tree.

**Narration (~72 words):**
> "The whole project was vibecoded — scaffolded with agents-cli on ADK 2.0 and built conversationally with AI pair programming. Security is layered by persona: an admin token gates anything destructive or billable, share codes gate uploads, photos are re-encoded with all EXIF stripped so GPS never leaves the server, and filenames are sanitized against prompt injection. It ships with a Dockerfile and Terraform for Cloud Run — deployment is three commands, documented in the README."

## Scene 8 — Close (4:25–4:50) · 25s

**On screen:** The viewer's journal one more time, slow scroll. End card: MemoryWeaver + repo URL.

**Narration (~55 words):**
> "A real hundred-twenty-five photo trip costs under a dollar to curate and takes minutes — seconds on a re-run, thanks to caching. The photos were always there. The story was always in them. MemoryWeaver is the concierge that finally writes it down. Code, README, and setup — three commands — at the link below. Thanks for watching."

---

## Edit notes

- Total narration ≈ 650 words ≈ 4:40 at a relaxed pace — you have breathing room.
- If over 5:00, cut in this order: MCP cut-in (Scene 6), Scene 7's security detail (keep one sentence), Scene 1's second example.
- Record narration separately from screen capture — much easier to sync than live talking.
- 1080p minimum; zoom the browser to ~125% so text is readable on YouTube compression.
- After the edit, fill every [T=m:ss] in KAGGLE_WRITEUP.md with the final timestamps.
