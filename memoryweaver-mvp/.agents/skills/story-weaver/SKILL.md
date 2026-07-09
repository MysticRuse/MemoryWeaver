---
name: story-weaver
description: Use when synthesizing the overall match story, compiling crowd statistics, or validating the final scoreboard parameters.
triggers: [story, synthesis, stats, matchbook, diary, highlights, verify score]
---

# Story Weaver Skill Instructions:
When this skill is active, the agent generates the final match highlights:
1. **NARRATIVE**: Write a vivid 2-paragraph story in first-person plural ("we/our") with a warm, passionate fan tone.
2. **GUARDRAIL CHECK**: Enforce strict score alignment. If the match is a scoreless draw (0-0), do NOT describe goals, scorer names, or celebrations.
3. **STATS**: Compile the top moment, crowd energy rating (1-5 stars), overall atmosphere, and key match milestones.
4. **SECURITY CHECK**: Evaluate the compliance of the output story:
   - Privacy: ensure no spectator real names or addresses are disclosed (status: `PROTECTED` or `ISSUE FOUND`).
   - Consent: verify if the user has confirmed public sharing (status: `CONFIRMED`, `PENDING`, or `N/A`).
   - Ready to post: verify if the package is safe for sharing (status: `YES` or `NEEDS REVIEW`).
