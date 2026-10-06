---
name: "Curator Skill"
description: "Deduplicates photos using Gemini image embeddings and scores photos using LLM-as-judge."
---

# Curator Agent Skill Instructions

This skill manages deduplication of photo bursts and scoring photos based on aesthetic values.

## Operations
1. **Deduplication**:
   - Generate image embeddings (`gemini-embedding-2`, 768 dims) for approved photos.
   - Drop a photo when its cosine similarity to an already-kept photo exceeds 0.92 (burst duplicate).
2. **LLM-as-Judge Scoring**:
   - Evaluate composition, lighting, focus, and candidacy.
   - Assign a composite score from 0 to 10.
   - Highlight photos featuring candid human expressions and landmark context.
