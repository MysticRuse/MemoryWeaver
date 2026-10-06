# MemoryWeaver Task Checklist

## Phase 1: Foundation (June 29)
- [ ] Agree on and document the inter-agent data contract & session state fields
- [x] Write `CONTEXT.md` defining security and privacy guidelines
- [x] Scaffold the project structure using `agents-cli-manifest.yaml`
- [x] Set up local development environment files (`.env.example`)
- [x] Set up GCS bucket connections or fallback mocks
- [x] Design mock upload UI (HTML/mobile skeleton)
- [x] Build basic `Collector Agent` tool signatures and boilerplate

## Phase 2: Core Curation & Safety (June 30)
- [x] Implement `Moderator Agent` (safety checks, blurring, screenshot filters)
- [x] Perform STRIDE threat modeling on the upload boundaries
- [x] Write pytest security checks for injection/oversized payloads
- [x] Implement `Curator Agent` (Gemini embeddings & clustering)
- [x] Integrate LLM-as-judge scoring (sharpness, composition, uniqueness)
- [x] Complete responsive mobile upload page with camera-picker & QR codes

## Phase 3: Context & Narratives (July 1 - July 2)
- [x] Build `Memory Agent` using Agent Runtime Memory Bank
- [x] Implement per-event contributor profiles & recommendation queries
- [x] Build `Narrator Agent` (Daily reels, best shots ZIP outputs)
- [x] Generate "Day-by-Day Journal" and "Trip Story" flowing narratives
- [x] Connect all agents using the A2A Protocol
- [ ] Integrate Mid-trip Digests & HITL review gate

## Phase 4: Production, Testing & Evaluation (July 3)
- [ ] Deploy multi-agent pipeline to Google Cloud Run via `agents-cli deploy`
- [x] Build final mobile-responsive "Artefact Viewer" UI
- [ ] Run full pipeline simulation (60 photos, 3 uploaders)
- [ ] Generate final `agents-cli eval` report

## Phase 5: Freeze & Submit (July 4)
- [ ] Repository freeze & bug validation
- [ ] Record demo video & draft Kaggle write-up
