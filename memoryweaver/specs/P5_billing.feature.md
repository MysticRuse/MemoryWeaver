# P5 · Plans, billing & printed books

## 1. Intent (why)
Turn the keepsake into revenue: gate features by plan, take payment via Stripe, and sell the high-margin printed book. Wireframe: `1f`.

## 2. Scope
**In:** plans (free / family_pro / teams) + entitlement gating; Stripe checkout & webhooks (test mode first); printed-book order flow.
**Out:** AaaS marketplace + agent payments (P5 aaas).

## 3. Contract (schema)
```yaml
plans:
  free:       { events: 1, items: 200, media: [photo], export: watermarked }
  family_pro: { events: unlimited, items: unlimited, media: all, export: [pdf], price_mo: 9 }
  teams:      { workspaces: true, roles: true, sso: true, api: true, price: custom }
endpoints:
  - method: POST
    path: /api/checkout
    auth: admin_token
    request: { workspace_id, plan }
    response: { checkout_url }              # Stripe-hosted; no card data touches us
  - method: POST
    path: /api/stripe/webhook               # signature-verified
    request: "<stripe event>"
  - method: POST
    path: /api/order-book
    request: { event_id, size, shipping }
entitlement_check:
  where: server-side on every gated action (e.g. add video requires family_pro+)
```

## 4. Behavior (BDD)
```gherkin
Feature: Plans and payment

  Scenario: Free tier gate
    Given a workspace on the free plan
    When a contributor uploads a video
    Then it is blocked with an upgrade prompt (photos only on free)

  Scenario: Upgrade unlocks media
    Given a successful Stripe test-mode payment for family_pro
    When the webhook is received and signature-verified
    Then the workspace plan becomes family_pro and video uploads are allowed

  Scenario: Order a book
    Given a ready event on a paid plan
    When the organizer orders a printed book
    Then an order is created and payment is taken via Stripe
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [pytest]
  golden_dataset: eval/datasets/p5-billing.json
  cases:
    - id: entitlement_gating
      expected_output: { free_blocks_video: true, pro_allows_video: true }
    - id: stripe_test_flow
      expected_output: { checkout_ok: true, webhook_verified: true }
    - id: no_card_data_local
      expected_output: { card_data_touches_server: false }
```

## 6. Security
- Never handle raw card data — Stripe-hosted checkout only.
- Verify webhook signatures; entitlement checks server-side (never trust the client).

## 7. Done when
- [x] Gherkin green
- [x] test-mode purchase + webhook verified
- [x] gating enforced server-side
- [x] Authority tier: **action_allowed** (billable — HITL sign-off required)
