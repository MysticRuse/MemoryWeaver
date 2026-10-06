# MemoryWeaver — application folder

This folder holds the MemoryWeaver application code (FastAPI app, ADK agents, pipeline, MCP server, tests, deployment files).

**The project documentation lives in the repository's main README: [`../README.md`](../README.md)** — overview, architecture, quick start, security model, deployment notes, and known limitations.

Quick start, from this folder:

```bash
uv sync
cp ../.env.example .env      # then set GEMINI_API_KEY
uv run uvicorn app.fast_api_app:app --port 8000
```
