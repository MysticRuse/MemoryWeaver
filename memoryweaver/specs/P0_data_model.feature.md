# P0 · Data model & storage

## 1. Intent (why)
Everything downstream (multi-media ingress, scalable pool, curation, viewer, teams) depends on a model that treats every memory as a typed item inside an event inside a workspace. Build this first so no later phase has to migrate.

## 2. Scope
**In:** `Workspace → Event → MediaItem` schema; storage layout (private raw vs. public curated); EXIF-strip on public serve; migration of the existing single-event `default` pool into `Workspace(default) → Event(default)`.
**Out:** the agents that populate type-specific fields (P1+); roles (P4).

## 3. Contract (schema)
```yaml
data:
  Workspace:
    id: str
    name: str
    plan: enum[free, family_pro, teams]      # entitlement source (P5)
    created_at: iso8601
  Event:
    id: str
    workspace_id: str
    name: str
    type: enum[trip, birthday, wedding, sports_match, reunion, other]
    share_code: str                          # upload credential (no accounts)
    status: enum[collecting, curating, ready]
    recurring_parent_id: str?                # P4
    created_at: iso8601
  MediaItem:
    id: str
    event_id: str
    type: enum[photo, video, voice, note, doc]
    contributor_id: str                      # HASH of display name — never raw
    contributor_name: str                    # display only; UI + journal, never logs
    created_at: iso8601
    area_label: str?                         # derived from GPS; raw coords never stored public
    hour_of_day: int?
    duration_s: number?                      # video, voice
    transcript: str?                         # voice
    ocr_text: str?                           # doc
    doc_kind: enum[ticket, menu, map, boarding_pass, other]?
    body: str?                               # note
    excluded: bool                           # organizer hid it
    curation: { selected: bool, score: number?, caption: str?, moment_id: str? }

storage_layout:
  private_raw: "uploads/<event_id>/"         # non-public bucket namespace
  public_curated: "media/<event_id>/"        # EXIF-stripped, curated only
```

## 4. Behavior (BDD)
```gherkin
Feature: Isolated, typed storage

  Scenario: Events never mix
    Given two events in the same workspace
    Then a MediaItem queried for event A never returns items from event B

  Scenario: Public serve strips EXIF
    Given a photo with GPS EXIF in private raw storage
    When it is promoted to public curated and served via /media
    Then the served file has no GPS or device-identifier metadata

  Scenario: Legacy migration
    Given the existing 'default' single-event pool
    When P0 migration runs
    Then it becomes Workspace(default) -> Event(default) with all photos intact
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [pytest]
  golden_dataset: eval/datasets/p0-model.json
  cases:
    - id: tenant_isolation
      expected_output: { cross_event_leak: false }
    - id: exif_stripped
      input: { photo: golden/gps_photo.jpg }
      expected_output: { has_exif: false }
    - id: legacy_migration
      expected_output: { photos_preserved: true, workspace: default }
```

## 6. Security (STRIDE / 7-pillar)
- Info-disclosure: raw uploads in a private namespace; only curated artifacts public.
- Privacy: `contributor_id` is a hash; `contributor_name` used for display only, excluded from logs.
- Tenant partitioning (Pillar 2/5) enforced at the query layer, not the UI.

## 7. Done when
- [ ] Gherkin green · [ ] pytest green · [ ] migration reversible & tested
- [ ] no cross-event/workspace leakage · [ ] EXIF strip verified on /media
- [ ] Authority tier: **action_allowed** (schema + storage writes)
