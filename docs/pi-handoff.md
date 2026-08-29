# Raspberry Pi 5 deployment handoff

Verified 2026-08-29 on the private Pi deployment. This file records public-safe
runtime facts only. Credentials, private source files, model weights, generated
indexes, and captured screens are not stored in Git.

## Active runtime

| Layer | Verified revision/state |
|---|---|
| Pi checkout | `32f8f19` (`Expand graphs across the live canvas`) |
| Browser behavior | `32f8f19` (`Expand graphs across the live canvas`) |
| Model | `Qwen3-4B-Q4_K_M.gguf`, 2,497,280,256 bytes |
| Model SHA-256 | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| llama.cpp package | pinned `v0.3.0`, source commit `c1d0e7a004015f23bc0233470b747b596f29b264` |
| llama-server self-report | `0.3.0-dev`, GNU 12.2.0, Linux aarch64 |
| Kernel | Raspberry Pi aarch64 `6.6.51+rpt-rpi-2712` |
| Inference profile | one slot, 3,072-token context, batch 128, micro-batch 64 |
| Background policy | one low-priority atomic job at a time; memory gate before every queued job; alternate book/lexical seeding only at queue idle |

`lkt-web`, `lkt-llm`, and `lkt-worker` were active. The live Python and browser
code matched the runtime revision above, and the Pi worktree was clean.
`/api/health` returned `ready`, with Qwen reported as local and ready.

The model service is bounded by systemd at `MemoryHigh=5 GiB`,
`MemoryMax=6 GiB`, `MemorySwapMax=128 MiB`, and `OOMPolicy=stop`. At final audit
the Pi had about 4.3 GiB available memory. A deliberately observed pathological
background inference reached the hard ceiling and was restarted without a Pi
reboot; web data and network state were unaffected.

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
  memory. At audit, `/api/health` reported 58 of 609 accepted: 30 Answers and
  28 Questions, with 551 remaining. The same compact progress appears in the
  browser header, and the decks were still growing.
- Missing-only lexical backfill walks 6,944 simple Word Origins headwords. It
  excludes every term already accepted, queued, or attempted, and queues one
  shared 16-job atomic plan only after the existing queue becomes idle. The
  resulting accepted atoms feed independent Word Card, Word Origin, Root, and
  Affix projections; no mode regenerates the same lexical knowledge.
- The book and lexical seeders alternate at queue idle. This keeps finite
  Answer/Question coverage moving without starving the four lexical views.
  `/api/health` exposes lexical planned, accepted, total, and remaining counts
  separately from the book deck.
- Each selected tab loads all its accepted cards, keeps the newest first, and
  shuffles the remainder without replacement for each carousel pass. Lexical
  modes are inquiry/queue driven, then reuse accepted atoms rather than
  regenerating them on every view.
- The selected deck now polls accepted cards every 30 seconds without
  interrupting the current card. A newly published card is placed next, then
  traversal resumes through the existing shuffled pass. Queued, running,
  rejected, and dirty candidates never enter this browser projection.
- Question and Answer inner slides are deterministic: all English sentence
  slides, then Japanese, then Chinese, with the optional final Explore/word
  teaching slide last. Each receives 18 seconds. The outer card timer derives
  its dwell from the full inner count, so a new random card cannot cut off a
  language.
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
- A live real-book retrieval after this deployment returned Question card
  `72506e8f-bfa3-4a1e-892c-4b10ac60c7f7` with source-owned citation
  `question-115` and non-empty accepted English, Japanese, and Chinese text.
  The `predecessor` Word Origin returned eight graph nodes and six focus areas.
  The served browser bundle contained the 18-second sequencer and morphology
  corner-metadata renderer.
- The first bounded autonomous lexical smoke selected `alive` from Word Origins
  entry `entry-0171` (source page 28). Retrieval owns the record describing Old
  English *on life* and the historical pronunciation relationship between
  *life* and *alive*. Prompt revision `autonomous-lexical-v1` queued exactly 16
  missing-only jobs; no card text or database atom was entered by hand.
- Existing accepted content was retained. At final audit, 114 independent
  missing enrichment jobs remained queued and were draining one at a time; no
  existing accepted deck was regenerated.
- Book-language synchronization is now a bulk, missing-only database pass. On
  the live 58-card deck it reused all 174 accepted EN/JA/ZH content atoms,
  found one card with missing enrichment, queued two jobs, and completed in one
  second. It did not reacquire book records or rewrite accepted knowledge.
- Terminal dependency failure now propagates through queued descendants, so no
  job can remain permanently queued behind a failed prerequisite. The live
  audit found zero such blocked jobs. Atomic grammar review accepts an exact
  source-preserving model segmentation even when Qwen omits wrapper metadata,
  while rewrites and partial coverage remain rejected.
- The worker now runs the same 1.5 GiB available-memory check before every
  queued inference, not only before seeding new deck items. After deployment,
  three consecutive grammar jobs completed as independent accepted artifacts;
  available memory remained about 4.3 GiB and all services stayed active.

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

A fresh 1920×1080 headless Chromium audit against deployed revision `20c77b9`
captured Question card `question-115` on Japanese inner slide 3 of 8. Furigana
occupied a separate annotation band above the large base characters; the full
sentence, evidence rail, and controls remained visible without overlap, clipping,
document scroll, or blocked content. The temporary audit image was removed after
inspection and was not committed.

Revision `2fc32cd` widens that band again after the first pass still felt tight:
it moves the ruby annotation itself away from the base glyph, increases the
reserved line height, and gives adjacent ruby groups more horizontal air. A new
live 1920×1080 audit used the accepted Question card “Heat and Help” with one
book-owned evidence record, 62 Japanese ruby tokens, and 64 Chinese ruby tokens.
Japanese slide 3 of 8 and Chinese slide 6 of 8 both had zero document or stage
overflow; their complete text remained inside the sentence stage. The temporary
capture was removed and the one Chromium target was restored to the bare
`?display` route.

Revision `37d4520` gives graph nodes a deliberate two-row hierarchy. Root,
prefix, suffix, word, and historical type badges occupy the top-left corner;
source-language badges occupy the top-right and use distinct colors for EN,
Latin, Greek, French, Proto-Indo-European, Germanic, Japanese, Chinese, and
Arabic. The focal term is black with a one-pixel white outline and centered
above a large wrapped explanation instead of being joined to it by a middle
dot. Standard terms render at 21.96 px and the center at 26.84 px in the live
fit; explanations render at 16.47-18.3 px and may use three lines. The accepted
`predecessor` Origin (eight nodes) keeps a measured 40 px boundary gap between
its morphology row and center word. Live 1920x1080 Origin, Root (five nodes),
and Affix (two nodes) audits reported zero document or graph overflow, zero
pairwise node overlap, and every node inside the graph. The Root word-node
definition was fully visible without ellipsis. Temporary captures were
inspected and removed.

Revisions `77dbfea` and `32f8f19` replace fixed graph boxes with measured
content geometry and distribute the semantic rows from the current canvas
width. The renderer applies bounded collision repulsion, removes term/meaning
line clamps, refits on card, focus, resize, and fullscreen changes, and exposes
a **FIT** control that restores the complete best view. A dedicated lower graph
panel presents Japanese, Chinese, French, and Arabic; Arabic graphemes cycle six
letter colors while retaining right-to-left rendering.

A live 1920x1080 audit on `32f8f19` used real accepted `predecessor` cards.
Word Origin occupied 96.2% of its graph width, Root 80.4% width / 88.4% height,
and Affix 82.1% width / 88.6% height. Every mode had eight visible nodes, four
multilingual annotations, zero clipped node labels, zero node collisions, and
zero nodes outside the canvas. FIT, branch focus, normal-window resize, and the
actual Fullscreen API all retained a complete in-canvas view. The committed
README captures were taken from this live Pi endpoint rather than mock data.

Deployed revision `f1299e8` adds a true cross-mode ambient journey without
merging the six collections. A live Chromium protocol audit on the Pi traversed
these accepted card modes and IDs in order:

1. Answer `af040b8d-7d26-4436-a04d-58c41af0e2bc` (initial draw)
2. Question `72506e8f-bfa3-4a1e-892c-4b10ac60c7f7`
3. Answer `49e919df-d9ba-4967-a846-e8823d047bad`
4. Word Card `78844f32-31a8-4d81-91b4-2f001fdb6ef0`
5. Word Origin `a47f2207-4455-405d-a7b8-24deca3705e3`
6. Root `7646e7ed-33da-40ef-9ed9-2221d84bd1ca`
7. Affix `d90d7365-dd17-4a09-b132-4bdc1e5aa0a7`

The same single kiosk target was then returned to
`http://127.0.0.1:8090/?display` and left on the Answer-first ambient start.
Each mode retains an independent non-repeating accepted-card pass; a newly
accepted card is inserted first for its mode. Explicit tabs and `?mode=` URLs
remain mode-local. Pointer, touch, key, or focus activity restarts the current
card's full dwell before cross-mode motion can resume. Model Lab is excluded.

The `LXDE-pi-labwc` desktop now owns a validated XDG autostart entry at
`~/.config/autostart/lkt-kiosk.desktop`. It launches the repository-controlled
script installed as `/usr/local/bin/lkt-open-kiosk`, waits for `/api/health`,
selects Wayland in the current session, and opens the bare `?display` URL with a
dedicated profile. The profile is also its duplicate lock: invoking the launcher
while the kiosk was open kept the Chromium page-target count at exactly 1 → 1.
The installed launcher and desktop entry matched their repository SHA-256 values
and had modes `0755` and `0644`. No logout or reboot was forced during the audit;
the validated autostart entry takes effect on the next normal graphical login.

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

- Windows development checkout at `32f8f19`: 126 unit tests passed in 97.820
  seconds; `compileall`, JavaScript syntax, and diff checks passed.
- Pi checkout after fast-forward to `32f8f19`: 126 full-suite tests passed in
  35.922 seconds; `compileall` passed. Earlier launcher validation at `6712508`
  also passed `bash -n` and `desktop-file-validate`.
  The runtime image does not install Node.js; JavaScript syntax was therefore
  checked in the Windows development gate before deployment.
- Live `/api/health`: ready, including all book, morphology, model, knowledge,
  FreeDict correction, and autonomous deck progress status.
- Accelerated Chromium used the deployed browser code with the Pi's real
  accepted-card API: the bare route requested Answer → Question → Answer → Word
  Card → Word Origin → Root → Affix, while an explicit Question route requested
  Question only. The temporary read-only audit proxy was removed afterward.
- Real book card: `question-115` was retrieved with source-owned evidence and
  accepted EN/JA/ZH content.
- Real origin graph: `predecessor` returned eight nodes and six focus areas.
- One-page kiosk: visually audited at 1920×1080; exactly one Chromium page left
  open.
