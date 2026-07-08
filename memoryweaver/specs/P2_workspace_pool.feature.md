# P2 · Workspace pool (browse thousands of mixed items)

## 1. Intent (why)
A 1,000+ item event must stay browsable. The organizer needs to filter by media type and contributor, see auto-grouped bursts/dupes/blur, and toggle items in/out without the page choking. Wireframe: `1c`.

## 2. Scope
**In:** paginated/virtualized media grid; type filters (photo/video/voice/note/doc); per-contributor filter; auto-group badges (burst count, duplicate, blurry); include/exclude toggle; live collect counter + share QR/link panel.
**Out:** the scoring that decides "best of burst" (P2 curation); viewer (P3).

## 3. Contract (schema)
```yaml
endpoints:
  - method: GET
    path: /api/media
    auth: admin_token
    request:
      event_id: str
      type: enum[all, photo, video, voice, note, doc]?
      contributor_id: str?
      group: enum[none, burst, duplicate, blurry]?
      page: int
      page_size: int            # default 60 (virtualization page)
    response:
      total: int
      counts_by_type: { photo: int, video: int, voice: int, note: int, doc: int }
      flagged: { bursts: int, duplicates: int, blurry: int }
      items: MediaItem[]
  - method: POST
    path: /api/exclude-media
    auth: admin_token
    request: { event_id: str, ids: str[], excluded: bool }
```

## 4. Behavior (BDD)
```gherkin
Feature: Browsable mixed-media pool at scale

  Scenario: Type filter
    Given an event with 980 photos and 142 videos
    When the organizer filters by "video"
    Then only video items return and counts_by_type is unchanged

  Scenario: Virtualized paging
    Given an event with 1,204 items
    When the grid loads
    Then it requests page_size 60 and stays responsive while scrolling

  Scenario: Non-destructive exclude
    When the organizer excludes an item
    Then it moves to the hidden view and is removed from curation input
    And it is NOT deleted from storage
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [pytest, browser]
  golden_dataset: eval/datasets/p2-pool.json
  cases:
    - id: filter_correctness
      input: { type: video }
      expected_output: { only_type: video }
    - id: perf_1000_items
      input: { count: 1000 }
      expected_output: { p95_scroll_frame_ms_max: 32 }
  budget: { first_page_load_ms: 800 }
```

## 6. Security
- `/api/media` and exclude endpoints are admin-token gated.
- Thumbnails served through EXIF-stripping `/media`; never expose raw namespace.

## 7. Done when
- [x] Gherkin green
- [x] filters + counts correct
- [x] 1,000-item scroll within budget
- [x] exclude is non-destructive
- [x] Authority tier: **action_allowed**
