---
name: photo-tagger
description: Use when the user wants to moderate, score, analyze, or caption match-day photos.
triggers: [moderate, score, analyze, tag, categorize, caption]
---

# Photo Tagger Skill Instructions:
When this skill is active, the agent processes match-day photos:
1. **MODERATE**: Check for blurriness, darkness, PII (names, school logos), and appropriateness.
2. **SCORE**: Rate photos from 0 to 10 based on genuine fan/crowd emotions or action quality.
3. **TIMING**: Classify photos chronologically: `pre-match` (warmups, tailgating), `in-match` (active play, lineups), or `post-match` (final scores, leaving).
4. **CAPTION**: Write one vivid sentence from the fan perspective, using real player/team names. Refer to minors collectively as "the little ones".
