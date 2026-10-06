# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from agents.memory.tools.memory_helpers import (
    upsert_contributor_profile,
    get_contributor_profile,
    recommend_missed_moments
)

memory_agent = Agent(
    name="memory_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Memory Agent that tracks contributor profiles, preferences, and recommends highlights of missed trip moments.",
    instruction=(
        "You are the Memory Agent. Your responsibility is to maintain per-event knowledge "
        "about an event's participants. Use upsert_contributor_profile to keep stats and scenes visited updated, "
        "and use recommend_missed_moments to find photos of events a specific contributor was absent from."
    ),
    tools=[
        upsert_contributor_profile,
        get_contributor_profile,
        recommend_missed_moments
    ],
)
