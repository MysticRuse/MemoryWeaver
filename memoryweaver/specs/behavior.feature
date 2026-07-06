Feature: MemoryWeaver Group Trip Photo Curation Pipeline
  As a travel group coordinator
  I want my uploaded photos to be automatically sanitized, filtered, scored, profiles updated, and narratives synthesized
  So that I have a beautiful, cohesive, and safe travel journal without manual effort.

  Scenario: Ingress, filter, score, index, and generate journal
    Given a set of raw photos uploaded by multiple contributors
    When the multi-agent pipeline is executed with a diversity limit
    Then the Moderator Agent should filter out screenshots, memes, and blurry images
    And the Curator Agent should deduplicate near-duplicate burst shots using embeddings
    And the Curator Agent should score remaining photos 0-10 on sharpness, composition, uniqueness, and human presence
    And the Memory Agent should update contributor profiles and scene lists in the memory bank
    And the Narrator Agent should synthesize factual, landmark-specific journal entries and an overall trip story
    And the pipeline should record a structured Vibe Trajectory trace for observability
