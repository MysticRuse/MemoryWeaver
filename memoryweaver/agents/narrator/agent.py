# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from agents.narrator.tools.journal import generate_moment_journal
from agents.narrator.tools.story import generate_trip_story

narrator_agent = Agent(
    name="narrator_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Narrator Agent that synthesizes moments into narrative journals and full trip stories.",
    instruction=(
        "You are the Narrator Agent. Your responsibility is to weave trip highlights, contributor comments, "
        "and metadata into final artefacts. Use generate_moment_journal to summarize each setting, "
        "and use generate_trip_story to write the comprehensive flowing trip summary."
    ),
    tools=[
        generate_moment_journal,
        generate_trip_story
    ],
)
