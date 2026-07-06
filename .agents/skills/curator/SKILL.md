---
name: "Curator Skill"
description: "Deduplicates photos using CLIP embeddings and scores photos using LLM-as-judge."
---

# Curator Agent Skill Instructions

This skill manages deduplication of photo bursts and scoring photos based on aesthetic values.

## Operations
1. **Deduplication**:
   - Generate multimodal embeddings (e.g. CLIP) for approved photos.
   - Run cosine similarity and cluster burst/duplicate groups using DBSCAN.
2. **LLM-as-Judge Scoring**:
   - Evaluate composition, lighting, focus, and candidacy.
   - Assign a composite score from 0 to 10.
   - Highlight photos featuring candid human expressions and landmark context.
