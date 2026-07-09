# P3 · Narration skills (event-type aware)

## 1. Intent (why)
A soccer final shouldn't read like a travel diary. Use Agent Skills (Day 3) so
the Narrator loads an event-type voice on demand, and let it draw on voice-note
transcripts as real source material. Wireframe: drives `1e` story text.

## 2. Scope
**In:** portable narration skills per event type; Narrator writes per-moment
entries + one trip story in one batched call; transcripts feed the prose.
**Out:** the viewer rendering (P3 viewer); scoring (P2).

## 3. Contract (schema)
```yaml
skills:                                  # progressive disclosure: metadata always, body on trigger
  - name: narrate_trip
    triggers_on: event.type == trip
  - name: narrate_sports_match
    triggers_on: event.type == sports_match
  - name: narrate_wedding
    triggers_on: event.type == wedding
narrator:
  input: { moments: [...], transcripts: [...], profiles: [...] }
  output:
    moments: [{ id, title, story }]
    trip_story: str
  calls: 1                               # one batched generation
```

## 4. Behavior (BDD)
```gherkin
Feature: Event-type-aware narration

  Scenario: Right voice for the event
    Given an event of type sports_match
    Then the narrate_sports_match skill triggers (not narrate_trip)

  Scenario: Voice notes shape the story
    Given a moment with a voice-note transcript
    Then the generated story may quote or paraphrase it, attributed to the speaker

  Scenario: Negative trigger
    Given an event of type trip
    Then narrate_sports_match does NOT load (token budget protected)
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [llm_as_judge, trajectory]
  threshold: 0.85
  golden_dataset: eval/datasets/p3-narration.json
  cases:
    - id: trigger_coverage
      expected_output: { positive_trigger: true, negative_trigger: false }
    - id: tone_matches_type
      expected_output: { llm_judge_tone_min: 0.85 }
    - id: uses_transcripts
      expected_output: { grounded_in_source: true }
  budget: { max_gemini_calls: 1 }
```

## 6. Security
- Sanitize transcripts before prompting (injection guard).
- Skills graduate read_only → draft → action per eval coverage (Day 3 governance).

## 7. Done when
- [ ] Gherkin green · [ ] positive & negative triggers pass · [ ] tone judged ≥ 0.85
- [ ] one-call budget held · [ ] Authority tier: **draft** until eval coverage complete
