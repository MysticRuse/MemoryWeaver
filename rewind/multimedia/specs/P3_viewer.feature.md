# P3 · Multimedia viewer / keepsake

## 1. Intent (why)
The journal must weave every media type into one readable, shareable, printable
keepsake — not a photo grid. Wireframes: `1e`, `2a`, `2b`, `2c`.

## 2. Scope
**In:** cover hero + sub-event filter tabs; moment sections mixing photos, inline
video, tap-to-play voice (waveform + transcript + named speaker), note cards, doc
attachments; PDF/print ("book-ready"); silent/print caption state.
**Out:** narration text generation (P3 narration_skills); ordering print (P5).

## 3. Contract (schema)
```yaml
endpoints:
  - method: GET
    path: /api/trip-book
    auth: none                 # read-only, curated artifacts only
    request: { event_id: str, sub_event: str? }
    response:
      cover: { title, dates, area_label, cover_media_id }
      sub_events: [{ id, label }]
      moments: [{ id, day, title, story, media: MediaItem[] }]
audio_directive:
  playback: tap_to_play        # never autoplay
  show: [waveform, transcript_pullquote, speaker_name]
  standalone: "render as its own voice-moment card when unanchored"
print_directive:
  audio: "transcript becomes a quote + small QR to hear original"
  video: "poster frame + QR / play affordance"
```

## 4. Behavior (BDD)
```gherkin
Feature: A woven multimedia journal

  Scenario: Voice note plays in place
    Given a moment with an anchored voice note
    Then it shows a waveform, a transcript pull-quote, and the speaker's name
    And audio plays only on tap (never autoplay)

  Scenario: Sub-event filter
    When the reader picks "Day 2 · Uluwatu"
    Then only that segment's moments show; "Highlights" shows the curated best

  Scenario: Print/PDF
    When the reader exports to PDF
    Then video shows a poster + QR and audio shows its transcript quote + QR
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [llm_as_judge, browser, trajectory]
  threshold: 0.85
  golden_dataset: eval/datasets/p3-viewer.json
  cases:
    - id: audio_anchor_render
      expected_output: { speaker_shown: true, autoplay: false }
    - id: print_snapshot
      method: browser
      expected_output: { visual_judge_min: 0.85 }
```

## 6. Security
- Read-only; serves curated, EXIF-stripped media only; never the raw namespace.
- Speaker display name shown in UI, absent from logs (per DESIGN_SYSTEM + CONTEXT).

## 7. Done when
- [ ] Gherkin green · [ ] all 5 media types render · [ ] print snapshot passes
- [ ] audio tap-to-play + speaker named · [ ] Authority tier: **read_only**
- [ ] Matches DESIGN_SYSTEM.md (Soft Clay, two fonts, Lucide, radii)
