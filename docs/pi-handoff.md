# Raspberry Pi 5 deployment handoff

Verified 2026-08-29 on the private Pi deployment. This file records public-safe
runtime facts only. Credentials, private source files, model weights, generated
indexes, and captured screens are not stored in Git.

## Active runtime

| Layer | Verified revision/state |
|---|---|
| LKT runtime code | `6908cbd6a584d2a3596965f2254da1b9cf5ce98a` |
| Model | `Qwen3-4B-Q4_K_M.gguf`, 2,497,280,256 bytes |
| Model SHA-256 | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| llama.cpp package | pinned `v0.3.0`, source commit `c1d0e7a004015f23bc0233470b747b596f29b264` |
| llama-server self-report | `0.3.0-dev`, GNU 12.2.0, Linux aarch64 |
| Kernel | Raspberry Pi aarch64 `6.6.51+rpt-rpi-2712` |
| Inference profile | one slot, 3,072-token context, batch 128, micro-batch 64 |
| Background policy | one low-priority atomic/deck job at a time |

`lkt-web`, `lkt-llm`, and `lkt-worker` were active. The live Python and browser
code matched the runtime revision above, and the Pi worktree was clean.
`/api/health` returned `ready`, with Qwen reported as local and ready. A later
documentation-only handoff commit does not alter that runtime code revision.

The model service is bounded by systemd at `MemoryHigh=5 GiB`,
`MemoryMax=6 GiB`, `MemorySwapMax=128 MiB`, and `OOMPolicy=stop`. At final audit
the Pi had 2.6 GiB available memory, was 56 C, and reported `0x50000`: historical
throttling only, with no current undervoltage or throttling condition.

## Required local sources

The live health contract reported every required source as ready:

| Retrieval source | Records |
|---|---:|
| Word Origins | 6,994 |
| Book of Answers | 318 |
| Book of Questions | 291 |
| New Oriental English Root Dictionary | 6,327 |
| English Affix Dictionary | 5,189 |
| FreeDict English-Arabic 0.6.3 | 89,028 exact pairs |

The GUI therefore reports 108,147 local source records. FreeDict is a compact
SQLite correction index built from source revision
`5bdceeac8d0dba3298c1bebe734f60d54dad30f7`; its verified TEI SHA-256 is
`7572d3685c501975cd0d47b0dfb581b053b28fb18932d06f09d64d0479b06746`.
Only the generated index is present on the Pi. `/api/health` now treats this
correction source as required, so a missing index cannot silently weaken Arabic
generation.

## Autonomous generation and provenance

- Accepted card values are prepared by local Qwen, reviewed book records, or
  deterministic offline tools. They are not manually entered into the live
  SQLite databases. Retrieval code owns citations; the model cannot invent or
  rewrite them.
- Answer and Question backfill walks all 609 reviewed book records without
  repeating a source record. It balances the two completion percentages and
  pauses on current undervoltage, throttling, high temperature, or low available
  memory. At audit, the visible decks held 25 Answers and 23 Questions and were
  still growing.
- Each selected tab loads all its accepted cards, keeps the newest first, and
  shuffles the remainder without replacement for each carousel pass. Lexical
  modes are inquiry/queue driven, then reuse accepted atoms rather than
  regenerating them on every view.
- A live real-book smoke request was completed entirely on the Pi by
  Qwen3-4B in 52.75 seconds. Card
  `bae4877a-8ace-4100-aee9-a689221acb1a` preserved the exact reviewed
  EN/JA/ZH text and citation for Book of Answers entry `answer-269` (page 275).
  Its generated title and reflection remain model-owned fields, separate from
  the book-owned answer.
- The accepted `predecessor` Word Card is
  `78844f32-31a8-4d81-91b4-2f001fdb6ef0`. Its earlier incorrect Arabic atom was
  retired through the normal preparation CLI, not edited in SQL. Local Qwen then
  selected exact FreeDict candidate `السلف`; the system attached dictionary
  evidence ID `freedict-eng-ara:4de4094d8e920b618dfe` and deterministic offline
  eSpeak IPA `ʔassˈalaf` before publication.
- Independent accepted `predecessor` Word Origin, Root, and Affix views are
  `a47f2207-4455-405d-a7b8-24deca3705e3`,
  `7646e7ed-33da-40ef-9ed9-2221d84bd1ca`, and
  `d90d7365-dd17-4a09-b132-4bdc1e5aa0a7`. Each reuses the same accepted atomic
  history while keeping its own card mode and focus slides.

The LadybugDB traversal projection was rebuilt atomically after the smoke card.
It contains 286 accepted nodes and 243 accepted edges, fingerprint
`df204bd4719abc3fddb51c3ecb13c576c4de1e636850d26fd6c233bdeedd9fd1`.
The previous projection remains beside it for recovery; SQLite remains the
source of truth.

## Browser audit

Exactly one Chromium top-level page was left open at the ambient display:
`http://127.0.0.1:8090/?display`. A 1920×1080 desktop capture showed the Answer
view with no document scroll or overlap, readable reviewed text, source evidence,
slide controls, and the card discussion action. The live carousel was on slide
four of four during capture, confirming automatic slide/card rotation.

Direct entry points remain:

- Answer/default: `http://127.0.0.1:8090/?display`
- Question: `http://127.0.0.1:8090/?mode=question&display`
- Word Card: `http://127.0.0.1:8090/?mode=knowledge&display`
- Word Origin: `http://127.0.0.1:8090/?mode=word&display`
- Root: `http://127.0.0.1:8090/?mode=root&display`
- Affix: `http://127.0.0.1:8090/?mode=affix&display`

The bare composer routes one English word to Word Card, ordinary sentences to
local Chat, and explicit `origin:`, `root:`, or `affix:` inquiries to their
independent modes. Tabs and `?mode=` URLs remain exact overrides. Intent routing
itself is deterministic and does not call the model.

The browser GUI remains the only implemented output adapter. E-ink and audio
are reserved adapters that will consume the same stored card JSON later and are
not imported by the core service.

## Validation performed

- Windows development checkout: 109 unit tests passed; `compileall` passed.
- Pi checkout after fast-forward: 109 unit tests passed; `compileall` passed.
- Live `/api/health`: ready, including all book, morphology, model, knowledge,
  and FreeDict correction status.
- Real book card: generated and retrieved with its source-owned evidence.
- One-page kiosk: visually audited at 1920×1080; exactly one Chromium page left
  open.
