# P5 · Agents-as-a-Service (A2A marketplace + AP2)

## 1. Intent (why)
Expose Rewind's curation agent as a billable service on an A2A marketplace, with Agent Payments Protocol (AP2) guardrails — the course's AaaS + AP2 concepts shipped.

## 2. Scope
**In:** publish an Agent Card describing the curation skills; consumption-based listing; AP2 mandates (human-approved spend rules) + handshakes for any agent that pays to hire an external specialist (e.g. video-highlight agent from P2).
**Out:** the pipeline itself (P2); consumer billing (P5 billing).

## 3. Contract (schema)
```yaml
agent_card:                                # machine-readable CV, published to registry
  name: rewind-curator
  skills: [moderate, dedup, score, anchor_audio, narrate]
  auth: a2a_token
  pricing: { model: consumption, unit: per_100_items }
ap2:
  mandate:  { max_spend, allowed_agents, human_approved: true }   # spend rules
  handshake:{ encrypted_proof_of_approval: true }                  # per-transaction
  rule: "no agent spend without a matching, human-approved mandate"
endpoints:
  - method: GET
    path: /.well-known/agent-card.json      # discovery
  - method: POST
    path: /a2a/invoke
    auth: a2a_token
    request: { skill, event_id, mandate_ref? }
```

## 4. Behavior (BDD)
```gherkin
Feature: AaaS with guarded payments

  Scenario: Discoverable agent
    When a client fetches /.well-known/agent-card.json
    Then it returns the rewind-curator card with skills and pricing

  Scenario: Spend requires a mandate
    Given no active mandate
    When the curator tries to hire a paid external video agent
    Then the spend is refused

  Scenario: Approved spend proceeds
    Given a human-approved AP2 mandate within limits
    Then the hire proceeds and a signed handshake records the approval
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [pytest, trajectory]
  golden_dataset: eval/datasets/p5-aaas.json
  cases:
    - id: card_discovery
      expected_output: { card_valid: true }
    - id: mandate_required
      expected_output: { spend_without_mandate_blocked: true }
    - id: handshake_recorded
      expected_output: { signed_proof: true }
```

## 6. Security
- AP2 mandates are human-approved; no ambient spend authority (Confused-deputy guard).
- A2A calls authenticated; audit every invocation; egress governance on external hires.

## 7. Done when
- [x] Gherkin green
- [x] card discoverable
- [x] spend blocked without mandate
- [x] handshake signed
- [x] Authority tier: **action_allowed** (financial — full eval + HITL)
