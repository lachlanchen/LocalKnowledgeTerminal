# Stall resilience and bounded projection reuse — 2026-11-04

This release repairs remaining apparent-stall paths in the browser carousel and
reduces avoidable Raspberry Pi SD-card reads. It does not change retrieval,
model prompts, publication gates, citations, or the card JSON contract.

## Live diagnosis

- The Pi kiosk was not dead: its journal showed repeated successful six-mode
  cycles through Question, Answer, Word Card, Word Origin, Root, and Affix.
- Question and Answer hydration was normally below one second. Live lexical
  hydration varied from roughly two to nine seconds and could return a
  temporary `503` when concurrent clients occupied all projection slots.
- Isolated `lexical_subgraph` calls on the same database took about 0.02–0.18
  seconds. The observed latency therefore came from shared storage and process
  contention, not from reducing or bypassing graph reasoning.
- The Pi had two active browser clients, continuous Qwen/worker activity, full
  swap, a root filesystem at 89 percent, and multi-gigabyte card and knowledge
  databases. Unrelated accepted writes also advanced one global presentation
  revision, making otherwise useful subject projections cold.

## Changes

- A failed cold-start history request now arms the ambient watchdog instead of
  leaving the page without a future timer.
- Hydration and random fallback share one 20-second total browser budget. They
  can no longer stack into a 40-second wait.
- A failed prefetch gets one fresh bounded attempt when no accepted fallback is
  available.
- Each mode retains at most one previously accepted full card for ten minutes.
  During a transient fetch failure the carousel reuses that unchanged card and
  its unchanged citations rather than freezing or skipping the mode.
- Hidden browser tabs pause carousel, health, and deck polling. Foregrounding a
  tab resumes only the carousel immediately, leaving the existing staggered
  maintenance intervals intact so it does not create an I/O burst.
- The server projection LRU remains limited to 256 exact
  version/subject/mode/limits views. A different global generation may reuse
  the exact prior accepted projection for at most 60 seconds; after that it is
  recomputed. Same-generation behavior and inflight coalescing are unchanged.

## Quality invariants

- No RAG source, ranking rule, prompt, validator, publication decision, or
  citation is changed.
- The short fallback windows reuse only content that was already accepted and
  rendered. They delay freshness briefly under contention; they do not invent,
  summarize, or transform knowledge.
- Newly accepted cards retain priority through the existing deck summaries and
  no-replacement mode queues.

## Pi runtime handoff

- Python: 3.11.2
- Model: `Qwen3-4B-Q4_K_M.gguf`
- Model SHA-256:
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`
- Model size: 2,497,280,256 bytes
- llama-server: 0.3.0-dev, GNU 12.2.0, Linux aarch64

Before deployment, run the repository validation commands in `AGENTS.md` plus
`node --check lkt/static/app.js`. After deployment, verify `/api/health`, all
six random-card endpoints, a complete ambient cycle, and one real book-backed
citation.
