# Photo/Video/App/Web Work — Efficiency Playbook

Built from real observed cost patterns in agentic coding sessions: the dominant cost
driver is almost always Bash-heavy exploration compounding inside long, uncleared
sessions — not the actual output work. Same root causes will show up in every category
below on any project unless pre-empted — including website/web-app development.

---

## 0. The one decision that matters most: is this task even agentic?

Before opening Claude Code for any of the four work types below, ask: **does this need
back-and-forth reasoning, or is it a deterministic operation I can script once?**

- Splitting 500 videos with the same crop/trim logic — NOT agentic. Write the ffmpeg
  command once (with Claude's help), then run it in a `for` loop over all files with
  zero further model calls. This is the difference between $0.50 and $150 for the same job.
- Annotating 2,000 photos with the same tagging schema — NOT agentic. Use the **Batch
  API** (50% discount, async, no session/context compounding) with a single well-tested
  prompt, not an interactive Claude Code loop per photo.
- Debugging why the iOS build fails once — IS agentic (genuinely needs investigation).
- "Make the app look better" — agentic but UNBOUNDED — see Task Framing rule below.

**Rule of thumb: if you're about to do the same action on more than ~10 files, stop and
ask "can this be a script + loop" before asking Claude to do it one-by-one in chat.**

---

## 1. Photo annotation (tagging, captioning, categorization)

- **Bulk (>20 photos, same schema)**: Batch API, not Claude Code. Write and test the
  prompt on 3-5 sample photos interactively first (Sonnet, not Opus — vision + tagging
  is not an Opus-tier task), then submit the rest as a batch job.
- **Default to a two-tier pipeline for any bulk classification, automatically**: Tier 1
  runs a cheap pass (Haiku) over everything against the schema. Tier 2 only re-processes
  items where Tier 1's confidence is low or ambiguous, using a stronger model. This
  should be the default architecture proposed for any "classify/tag all of X" request —
  not something the user has to specify.
- **One-off/judgment calls** (this photo grade the same as yesterday, spot-check quality):
  fine to do in-session, but on Sonnet, in a short-lived session — not the same session
  you're using for app development.
- **Never** re-upload the same image across multiple turns to "double check" — if you
  need to reference it again, note the file path, don't re-send the bytes.
- Avoid vague prompts like "make sure all photos are categorized well" — specify the
  exact taxonomy and confidence threshold up front so there's a stopping condition.

## 2. Video splitting / trimming / compression

- **This is pure ffmpeg scripting** — treat it exactly like the mechanical-work rule in
  CLAUDE.md: Haiku or Sonnet writes the command, you verify on one file, then it's a bash
  loop with zero further model involvement.
- Do NOT ask Claude to "process this video" repeatedly for each file — ask it to write
  one script parameterized over a file list.
- If frame-level scene description is needed (not just splitting), sample frames (e.g.
  1 per 5 seconds) rather than sending full video — full video analysis burns enormous
  tokens for marginal gain over sparse frame sampling in most cases.

## 3. Video annotation / scene description

- Same batching logic as photo annotation: sample frames, batch the vision calls, don't
  run an agentic loop per video unless the task genuinely requires multi-step reasoning
  (e.g. "find the exact moment X happens" — that's agentic; "describe each scene" at
  scale is not).
- Cache the extracted frames locally — if you re-run the analysis with a tweaked prompt,
  don't re-extract frames, reuse what's on disk.

## 4. Native iOS app development

This is where runaway build/debug loops most often repeat — Xcode build/debug loops
look exactly like the Bash-heavy exploration pattern that drives most avoidable cost.

- **Build error loops**: don't paste full Xcode build logs into context. Grep for the
  actual error line(s) first (`xcodebuild ... 2>&1 | grep -A5 "error:"`), pass only that.
- **Simulator screenshot debugging**: avoid repeated screenshot → look → fix → screenshot
  cycles in one long session. Batch a few fixes, then check once, not fix-check-fix-check
  every single change.
- **UI iteration**: "make it look better" is the mobile-app version of "make it A+" —
  unbounded. Specify exact changes (spacing, color, specific component) instead.
- **App Store submission / provisioning profile issues**: these are usually one-shot
  fixes once diagnosed — don't let a provisioning debugging session run past ~20 turns
  without stepping back to reassess.
- **Simulator vs device builds**: keep these as separate sessions — don't chain "fix
  simulator build" into "now fix device build" in the same context.
- Model routing: Swift/SwiftUI boilerplate and mechanical fixes → Sonnet/Haiku. Opus only
  for genuine architecture decisions (data model, navigation structure, concurrency model).

## 5. Native Android app development

Same structure as iOS, Android-specific traps:

- **Gradle build logs are enormous** — never dump a full Gradle failure into context;
  grep for the actual error/exception first.
- **Emulator vs physical device** — separate sessions, same as iOS.
- **Kotlin/Compose boilerplate** — Sonnet/Haiku. Opus reserved for architecture only.
- If building both iOS and Android versions of the same feature, do NOT do them in one
  session "to keep them consistent" — this doubles context size for no efficiency gain.
  Finish one platform, clear, then start the other with a short written summary of what
  was decided (not the full session history) as the only carryover.

## 6. Website / web-app development

Same traps as native mobile, plus a few web-specific ones:

- **Build/bundler error loops**: don't paste full webpack/Vite/Next.js build output into
  context. Grep for the actual error line(s) first, same as Xcode/Gradle.
- **Browser console/network debugging**: avoid repeated "open devtools → screenshot →
  fix → reload → screenshot" cycles in one long session — batch a few fixes, check once.
- **CSS/layout iteration**: "make it look better" / "make the UI cleaner" is the web
  version of "make it A+" — unbounded. Specify exact elements, spacing, breakpoints
  instead.
- **Cross-browser/responsive testing loops**: treat desktop, tablet, and mobile-viewport
  fixes as separate bounded passes, not one continuous "fix it everywhere" session.
- **SSR/build-time vs client-side issues**: keep these as separate sessions when
  debugging, same as simulator-vs-device on mobile — don't chain one into the other.
- **API/backend integration debugging**: grep/filter network request logs and API
  responses down to the failing call before pasting into context — full request/response
  dumps (especially paginated JSON) are a common silent token sink on web projects.
- Model routing: HTML/CSS/component boilerplate and styling tweaks → Sonnet/Haiku. Opus
  only for genuine architecture decisions (state management approach, routing structure,
  data-fetching strategy, auth flow design).
- If building a website alongside a native app version of the same product (matching
  your case: web + iOS + Android), treat each as its own session per the cross-cutting
  rule below — never chain "make the web version match" into the same session as the
  mobile work.

---

## Cross-cutting rules for ALL of the above

1. **One work-type per session.** Don't mix "annotate these photos" + "fix the iOS build"
   in one continuous session — chaining multiple unrelated tasks into one long-running
   session is consistently the pattern behind the largest single-session cost spikes.
2. **Checkpoint with `/cost` after every distinct task**, not just at the end of a session.
   If a single task is already past $5-10, stop and ask whether it should have been
   scripted/batched instead of agentic.
3. **External memory over long context.** Use git commits + a short PROGRESS.md (or
   similar) to carry state between sessions instead of keeping one session alive to
   "remember" what happened. Read the file back in fresh next session — far cheaper than
   an ever-growing cached history.
4. **Effort setting**: low for routine work (splitting, batch tagging, boilerplate,
   mechanical build fixes), high only for real architecture/design decisions.
5. **Separate CLAUDE.md per project** (photo pipeline, video pipeline, iOS app, Android
   app, website/web-app) with each project's own directory map — don't let one giant
   CLAUDE.md try to cover everything, since irrelevant sections still load into every
   session's context.
6. **Budget per category before starting.** Given today's projected costs, a reasonable
   starting budget to watch against: photo/video batch work should be single-digit
   dollars even at real volume (it's mostly Batch API + scripting); iOS/Android/web
   development is where real agentic cost lives — budget per feature, not per day, so a
   runaway session shows up against a specific feature's budget, not lost in a daily total.

---

## Quick decision checklist before starting any task today

- [ ] Is this the same operation repeated over many files? -> Script it, don't chat it.
- [ ] Does this belong in an existing session, or does it need a fresh one? -> When in
      doubt, fresh session.
- [ ] Is the task's "done" condition stated, or is it open-ended ("better", "cleaner",
      "A+")? -> Rewrite it as bounded before sending.
- [ ] Does this need Opus, or is Sonnet/Haiku enough? -> Default to Sonnet, escalate only
      if it fails.
- [ ] Am I about to paste a full log/build output/large file? -> Grep it down first.
