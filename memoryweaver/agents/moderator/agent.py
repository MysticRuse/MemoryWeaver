# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from agents.moderator.tools.vision_check import run_vision_moderation_batch

moderator_agent = Agent(
    name="moderator_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Safety and quality checking agent that screens incoming photos.",
    instruction=(
        "You are the Moderator Agent. Your responsibility is to ensure every photo "
        "that enters the shared album is appropriate, sharp, and a real photo (not a screenshot or meme). "
        "Use the run_vision_moderation_batch tool to analyze batches of images "
        "(a list of {filename, path} dicts) and quarantine invalid uploads."
    ),
    tools=[
        run_vision_moderation_batch,
    ],
)
