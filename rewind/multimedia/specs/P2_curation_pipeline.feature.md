# P2 · Curation pipeline (mixed-media)

## 1. Intent (why)
Extend curation beyond photos: score video and anchor voice notes to moments,
keep the deterministic batched design, and surface the run two ways — a calm
friendly progress view for production and a full A2A/eval transparency view for
development & demo. Wireframe: `1d`.

## 2. Scope
**In:** Moderator/​Curator/​Memory over mixed media; video keyframe extraction +
CLIP dedup; audio anchoring (match voice notes to nearest photo/video by
time+area); Memory "missed moments"; A2A hire of an external video-highlight
agent; friendly⇄developer transparency toggle reading the same run.
**Out:** narration/journal (P3); billing of external agent (P5/AP2).

## 3. Contract (schema)
```yaml
endpoints:
  - method: POST
    path: /generate
    auth: admin_token
    request: { event_id: str, limit: int, stage: enum[curate] }
    response: { status: str, run_id: str }
  - method: GET
    path: /api/run/{run_id}
    response:
      phase: enum[moderation, dedup, scoring, memory]
      progress: { done: int, total: int }
      friendly_steps: [{ label: str, state: enum[pending, active, done] }]
      trajectory: [{ agent: str, action: str, via: enum[local, a2a, mcp], detail: str }]  # dev view
      eval: { score: number, passed: bool }
pipeline_phases: [moderation, dedup, scoring, memory]   # narration is P3
anchoring:
  rule: "voice/note with no nearby media (time+area window) -> standalone moment"
```

## 4. Behavior (BDD)
```gherkin
Feature: Mixed-media curation with transparency

  Scenario: Video is scored
    Given an event with 142 video clips
    When curation runs
    Then each kept clip has a keyframe, a score, and a caption

  Scenario: Voice note anchors to a moment
    Given a voice note within the time+area window of a photo cluster
    Then its curation.moment_id matches that cluster
    And a note with no nearby media becomes its own standalone moment

  Scenario: Transparency toggle
    Given a completed run
    When developer view is on
    Then the A2A trajectory, MCP calls, and eval score are shown
    And when off, only friendly_steps are shown
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [pytest, llm_as_judge, trajectory]
  threshold: 0.85
  golden_dataset: eval/datasets/p2-curation.json
  cases:
    - id: dedup_bursts
      input: { bursts: golden/bursts/ }
      expected_output: { precision_min: 0.9, recall_min: 0.85 }
    - id: curation_quality
      expected_output: { llm_judge_min: 0.85 }
    - id: audio_anchor
      expected_output: { anchor_accuracy_min: 0.9 }
  budget: { p95_latency_s: 180 }
```

## 6. Security (7-pillar)
- External video agent hired over A2A: audit the card, egress governance, no raw-storage access.
- Sanitize captions/filenames before Gemini prompts (injection guard).
- Cache per item so re-runs are near-free (cost governance).

## 7. Done when
- [ ] Gherkin green · [ ] dedup + quality + anchor thresholds met · [ ] latency budget held
- [ ] trajectory + eval score visible in dev view · [ ] Authority tier: **action_allowed** (billable)
