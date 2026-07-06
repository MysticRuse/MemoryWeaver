# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from agents.collector.tools.upload import process_and_save_upload
from agents.collector.tools.qr import generate_upload_qr

collector_agent = Agent(
    name="collector_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Frictionless upload agent that accepts media files, extracts metadata, saves to GCS, and generates event QR codes.",
    instruction=(
        "You are the Collector Agent. Your responsibility is to handle the frictionless ingress "
        "of trip memories. You validate incoming files, extract EXIF data, calculate anonymous contributor IDs, "
        "and register QR access links. Standardize metadata format to fit the session state contract."
    ),
    tools=[
        process_and_save_upload,
        generate_upload_qr,
    ],
)
