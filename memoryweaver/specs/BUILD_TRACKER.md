# BUILD_TRACKER.md — living board

One ticket = one feature spec. A ticket moves right only when that lane's gate passes.
States: `backlog → spec → building → eval_hitl → shipped`.
Update this file at the end of every loop (Section A, step 5).

| Ticket | Phase | Spec file | State | Authority | Eval score | Notes |
|---|---|---|---|---|---|---|
| Design system (Rewind · Amaranth Pink) | — | DESIGN_SYSTEM.md | shipped | n/a | — | locked brand directives |
| Data model (Workspace/Event/MediaItem) | P0 | P0_data_model.feature.md | shipped | action | 0.93 | schema migration done |
| Eval + trajectory harness | P0 | HARNESS.md | shipped | n/a | — | OTel wired |
| Photo parity (regression) | P1 | P1_multimedia_ingress.feature.md | shipped | action | 0.91 | existing path green |
| Voice note ingress | P1 | P1_multimedia_ingress.feature.md | building | draft | — | recorder + transcription |
| Doc ingress + OCR | P1 | P1_multimedia_ingress.feature.md | eval_hitl | draft | 0.87 | doc_kind classifier under review |
| Video ingress | P1 | P1_multimedia_ingress.feature.md | spec | — | — | mime + duration extract |
| Pool type filters + virtualized grid | P2 | P2_workspace_pool.feature.md | spec | — | — | 1,000-item perf |
| Video curation (keyframe/CLIP) | P2 | P2_curation_pipeline.feature.md | backlog | — | — | |
| Audio anchoring to moments | P2 | P2_curation_pipeline.feature.md | backlog | — | — | |
| Curation dev-transparency toggle | P2 | P2_curation_pipeline.feature.md | backlog | — | — | wireframe 1d |
| Viewer: tap-to-play audio | P3 | P3_viewer.feature.md | backlog | — | — | wireframe 2a–2c |
| Viewer: inline video + docs | P3 | P3_viewer.feature.md | backlog | — | — | |
| Print / PDF book layout | P3 | P3_viewer.feature.md | backlog | — | — | |
| Events dashboard + workspaces | P4 | P4_workspaces.feature.md | backlog | — | — | wireframe 1b |
| Roles / IAM / tenant isolation | P4 | P4_iam_roles.feature.md | backlog | — | — | |
| Recurring events + templates | P4 | P4_workspaces.feature.md | backlog | — | — | |
| Plans + Stripe billing | P5 | P5_billing.feature.md | backlog | — | — | wireframe 1f |
| Printed-book fulfillment | P5 | P5_billing.feature.md | backlog | — | — | revenue driver |
| A2A marketplace listing (AaaS) | P5 | P5_aaas.feature.md | backlog | — | — | AP2 mandates |

## How to read a row
- **Authority** — read_only / draft / action; promote only after eval coverage.
- **Eval score** — latest LLM-as-judge score vs the spec threshold.
- A ticket in `eval_hitl` is waiting on the human (Vibe Diff review), not the agent.
