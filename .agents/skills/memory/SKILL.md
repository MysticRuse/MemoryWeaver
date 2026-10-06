---
name: "Memory Skill"
description: "Maintains per-event contributor profiles and recommends missed moments."
---

# Memory Agent Skill Instructions

This skill manages long-term participant logs and handles personalized highlights.

## Operations
1. **Profile Storage**:
   - Maintain a per-event database (`memory_bank.json` in the event's folder) tracking contributor profiles, upload counts, and active scenes.
2. **Personalized Recommendations**:
   - Compare a contributor's active timeline with the full album.
   - Suggest top-rated photos from moments the user was absent from ("While You Were Away").
