# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from agents.curator.tools.embed import get_image_embedding, calculate_cosine_similarity
from agents.curator.tools.score import score_photos_as_judge_batch

curator_agent = Agent(
    name="curator_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Curation agent that clusters near-duplicates and scores photos using LLM-as-judge.",
    instruction=(
        "You are the Curator Agent. Your responsibility is to analyze a pool of approved trip photos. "
        "Use your get_image_embedding tool to detect and discard near-duplicate burst shots, and use your "
        "score_photos_as_judge_batch tool (a list of {filename, path} dicts) to rate composition, "
        "sharpness, uniqueness, and candid value. "
        "Produce a structured photo shortlist JSON manifest for the final trip narrative."
    ),
    tools=[
        get_image_embedding,
        calculate_cosine_similarity,
        score_photos_as_judge_batch,
    ],
)
