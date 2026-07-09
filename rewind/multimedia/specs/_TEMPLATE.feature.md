# <PHASE_ID> · <Feature name>

> Copy this file to `specs/<PHASE_ID>_<feature>.feature.md` and fill every section.
> Hybrid Markdown + YAML (Day 5): narrative in prose, structured/nested config in YAML.
> Delete these quote lines when done.

## 1. Intent (why)
One paragraph: the user problem and the outcome. Who benefits, and how we'll know it worked.

## 2. Scope
**In:** bullet list of what this slice includes.
**Out:** explicit non-goals (prevents agent scope creep).

## 3. Contract (schema)
```yaml
# API, data shapes, config. Use YAML for anything nested > 3 levels deep.
endpoints:
  - method: POST
    path: /example
    auth: share_code | admin_token | none
    request: { field: type }
    response: { field: type }
data:
  Entity:
    field: type
```

## 4. Behavior (BDD — Gherkin)
```gherkin
Feature: <feature name>

  Scenario: <happy path>
    Given <precondition>
    When <action>
    Then <observable outcome>

  Scenario: <edge / failure>
    Given <precondition>
    When <action>
    Then <graceful handling>
```

## 5. Eval cases (the gate)
```yaml
eval:
  method: [pytest, llm_as_judge, browser, trajectory]   # pick what applies
  threshold: 0.85          # LLM-judge pass bar for this feature
  golden_dataset: eval/datasets/<file>.json
  cases:
    - id: <case_id>
      input: <...>
      expected_tools: [<tool>]      # trajectory expectation
      expected_output: <...>
  budget: { p95_latency_s: <n>, max_gemini_calls: <n> }
```

## 6. Security (STRIDE / 7-pillar)
- Threats introduced by this feature and their mitigation.
- Confirm: no secrets on frontend · input validated · no PII in logs · EXIF stripped if public.

## 7. Done when
- [ ] Gherkin scenarios green
- [ ] Eval score ≥ threshold on golden dataset
- [ ] pytest green (incl. security cases)
- [ ] Trajectory clean (no drift / loops)
- [ ] Cost/latency within budget
- [ ] Vibe Diff summary produced for HITL review
- [ ] Authority tier: read_only | draft | action_allowed (justify)
