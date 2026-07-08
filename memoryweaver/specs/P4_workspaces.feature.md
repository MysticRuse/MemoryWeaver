# P4 · Workspaces, recurring & templates

## 1. Intent (why)
Move from one family to many events and teams: a dashboard of events, shared workspaces, recurring series, and templates. Wireframe: `1b`.

## 2. Scope
**In:** events dashboard (status: collecting/curating/ready); workspace switcher; recurring events (a parent series spawns child events); event templates; search.
**Out:** roles/permissions & isolation tests (P4 iam_roles); billing (P5).

## 3. Contract (schema)
```yaml
endpoints:
  - method: GET
    path: /api/workspaces/{id}/events
    response: { events: Event[], counts: { collecting, curating, ready } }
  - method: POST
    path: /api/events
    request: { workspace_id, name, type, template_id?, recurring?: { cadence } }
  - method: GET
    path: /api/search
    request: { workspace_id, q }
    response: { events: Event[] }
recurring:
  parent: "series definition (e.g. Weekly soccer)"
  child: "each occurrence is a normal Event with recurring_parent_id set"
```

## 4. Behavior (BDD)
```gherkin
Feature: Workspace-level event management

  Scenario: Dashboard groups by status
    Then events are shown with collecting/curating/ready counts

  Scenario: Recurring series
    Given a recurring event "Weekly soccer" with weekly cadence
    When a new occurrence is due
    Then a child Event is created inheriting the template, with its own share_code

  Scenario: Template reuse
    Given a saved "Wedding" template
    When a new event is created from it
    Then event type, narration skill, and cover style are pre-filled
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [pytest, browser]
  golden_dataset: eval/datasets/p4-workspaces.json
  cases:
    - id: recurring_spawn
      expected_output: { child_has_own_share_code: true, parent_linked: true }
    - id: search
      input: { q: "bali" }
      expected_output: { returns_only_matching: true }
```

## 6. Security
- All workspace endpoints require membership (enforced in P4 iam_roles).
- Each child event gets a fresh `share_code`; never reuse across occurrences.

## 7. Done when
- [x] Gherkin green
- [x] recurring spawn + template prefill work
- [x] search scoped to workspace
- [x] Authority tier: **action_allowed**
