# Raspberry Pi 5 deployment handoff

Verified 2026-08-30 on the private Pi deployment. This file records public-safe
runtime facts only. Credentials, private source files, model weights, generated
indexes, and captured screens are not stored in Git.

## Active runtime

| Layer | Verified revision/state |
|---|---|
| Pi checkout | This repository revision, deployed and verified 2026-08-30 |
| Browser behavior | Directional tab/card navigation with stale-response protection |
| Model | `Qwen3-4B-Q4_K_M.gguf`, 2,497,280,256 bytes |
| Model SHA-256 | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| llama.cpp runtime | packaged under `llama.cpp-0.3.0`; exact source commit unavailable from the deployed runtime |
| llama-server self-report | `0.3.0-dev`, build `0`, commit `unknown`, Linux aarch64 |
| Kernel | Raspberry Pi aarch64 `6.6.51+rpt-rpi-2712` |
| Inference profile | one slot, 3,072-token context, batch 128, micro-batch 64, 256 MiB prompt-cache cap, four context checkpoints, sleep after 600 idle seconds |
| Background policy | one low-priority atomic job at a time; memory gate before every job; periodic visible-count balancing; one unfinished autonomous lexical subject after existing backlog drains |

`lkt-web`, `lkt-llm`, and `lkt-worker` were active. The live Python and browser
code matched the runtime revision above, and the Pi worktree was clean.
`/api/health` returned `ready`, with Qwen reported as local and ready.

The model service is configured with `MemoryHigh=5 GiB`, `MemoryMax=6 GiB`,
`MemorySwapMax=128 MiB`, and `OOMPolicy=stop`. These limits are defense in depth:
the deployed Pi kernel did not expose memory-controller accounting to the service,
so `systemctl` reported `MemoryCurrent=[not set]` and no cgroup `memory.*` files.
The worker's explicit available-memory gate remains the effective safety boundary.

A read-only 2026-08-31 audit found an idle llama process at 6,453,677 KiB PSS,
including 6,441,552 KiB of private anonymous memory, after 27 inference tasks.
One cold anonymous arena still held 2,873,648 KiB resident while the runtime's
default prompt-cache allowance was 8,192 MiB with 32 context checkpoints. The
service therefore sets `--cache-ram 256`, `--ctx-checkpoints 4`, and
`--sleep-idle-seconds 600`. This preserves local prompt caching and CPU weight
repacking while bounding retained cache state and reclaiming memory after idle
periods. The earlier 4,096-token profile remains rejected; the 3,072-token context,
batch 128, and micro-batch 64 are unchanged.

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

Those morphology counts describe the 2026-08-30 deployment. The current parser,
added the next day, excludes index-only cross-reference rows; a fresh build from
the same hashed JSONLs admits 4,018 root and 5,179 affix records. Until the Pi is
rebuilt, this section remains historical deployment evidence rather than the
current-code corpus total.

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

- Windows development checkout at `e3180b8`: 126 unit tests passed in 69.225
  seconds; `compileall`, JavaScript syntax, and diff checks passed.
- Pi checkout after fast-forward to `e3180b8`: 126 full-suite tests passed in
  41.520 seconds; `compileall` passed. Earlier launcher validation at `6712508`
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
- Real origin graph: `predecessor` returned five source-owned evidence records,
  eight nodes, and six focus areas.
- Morphology layout at 1920×1080: Root used 82.9% of graph width and 81.3% of
  height; Word Origin used 83.6% and 88.4%. Root, Affix, and Word Origin had no
  node collisions, no nodes outside the canvas, and no copy outside a node.
- Live display settings: book defaults EN/JA/ZH, lexical defaults
  EN/JA/ZH/FR/AR, local persistence, deterministic filtered inner slides, and
  default-on random saved cards were exercised against the deployed API.
- Desktop recovery: LightDM and WayVNC were refreshed after a stale kiosk
  session stopped accepting input. The orphaned `--kiosk` Chromium session was
  removed and the Pi was intentionally left on a normal controllable desktop;
  the duplicate-safe kiosk launcher remains installed for product display use.

## Reboot services and escapable fullscreen deployment

Revisions `270be29`, `4b38e0d`, and `d46315e` are deployed. The display now
uses Chromium app-mode `--start-fullscreen`, never locked `--kiosk`. A
display-only Escape handler closes the app window and returns to the normal
VNC desktop; ordinary browser routes do not inherit that behavior. A physical
Wayland Escape event was exercised against the live app and the transient
display process became inactive while the model, web, and worker services
remained independent. The app was then relaunched and left at a Chromium-
reported 1920x1080 `fullscreen` window with exactly one page target at the bare
`http://127.0.0.1:8090/?display` ambient route.

`scripts/install_services.sh` is now the single idempotent service installer.
It validates and installs all three units and the XDG display entry, enables
LightDM plus model/web/worker for boot, starts in model → web → worker order,
waits for the local model, and requires `/api/health` to report `ready`.
`scripts/update_pi.sh` performs fast-forward-only Git update and the complete
test gate before a controlled service restart. `scripts/update_pi_tmux.sh`
runs that path in the durable `lkt-update` session and stores its log outside
Git. The live deployment itself completed successfully from that tmux session.

Boot wiring was verified without forcing a device reboot: LightDM is enabled
with the existing `lachlan` graphical auto-login, the XDG autostart entry is
installed, and `lkt-llm`, `lkt-web`, and `lkt-worker` are enabled and active.
The installed launcher/autostart SHA-256 values match the checked-out files.
Invoking the launcher while the app is running kept the page-target count at
1 → 1. Closing the display does not affect the three system services.

The persistence path was also verified live. Immediately after service
recovery the atomic queue changed from 84 queued jobs to 83, then continued to
68 while accepted grammar artifacts accumulated. A separate real-book proof
used local Qwen plus retrieved evidence to accept Answer card
`c730803c-f98d-4ed9-b629-2b780eea2f1d` from `answer-133` (page 139, “Don't look
down on others”). The accepted Answer collection advanced from 30 to 31 and
the API returned the stored evidence and card. Existing accepted cards remain
database reads; missing lexical requests become persistent atomic plans; the
idle coordinator adds only one unseen book or lexical source at a time.

Live revisions and validation on 2026-08-29:

- Source: `d46315e`; service deployment foundation: `4b38e0d`.
- Model: `Qwen3-4B-Q4_K_M.gguf` through llama.cpp `0.3.0-dev`, Linux aarch64.
- Knowledge runtime: Python 3.11.2.
- Pi full suite: 126 tests in 37.537 seconds; `compileall` passed.
- Windows final display change: 126 tests in 73.730 seconds; `compileall` and
  JavaScript syntax passed.
- `/api/health`: `ready`; web, model, worker, LightDM, and WayVNC active.

## Configurable ambient loop deployment

Revisions `b90c016` and `fc7b564` are deployed. The bare display now starts on
Question and advances exactly Question → Answer → Word Card → Word Origin →
Root → Affix. The cursor is initialized after the visible first mode, so the
first timed transition is Answer rather than a second Question card.

The live 1920x1080 settings dialog exposed six independent mode checkboxes and
fit at 600x629 px with no internal overflow. The following browser-level cases
were exercised and then reset:

- all six selected, canonical order, Random on (default);
- Root alone, reported as `1 MODE`;
- Question + Word Origin + Affix, retained in canonical order;
- Random off, preserving stable newest → middle → oldest card order.

After restoring defaults, one actual ambient transition changed Question to
Answer card `c730803c-f98d-4ed9-b629-2b780eea2f1d`, then pointed to Word Card.
The page was reloaded and left on Question with all six modes selected, Random
on, cursor next at Answer, one Chromium target, and a 1920x1080 fullscreen
window. `/api/health` remained ready and the background queue continued down
to 53 jobs during the web-only tmux deployments.

## Balanced growth deployment

Revision `6186b9c` replaces idle-only book/lexical alternation with accepted
visible-count balancing across Question â†’ Answer â†’ Word Card â†’ Word Origin â†’
Root â†’ Affix. At deployment the accepted collections were 28 Questions, 31
Answers, 3 Word Cards, and 2 each for Word Origin, Root, and Affix. The scheduler
therefore paused new book draws and selected lexical catch-up. Existing runtime
state contained two unfinished lexical subjects; the new bounded seeder reported
that fact and did not add another. Its next claimed job was lexical
`retrieve-evidence` even though optional book grammar work remained queued.

The planned-term audit now counts only a term with an actual atomic plan, not
every vocabulary entity discovered in reviewed book text. Live health changed
from the misleading 40 planned lexical terms to 2 planned and 1 accepted out of
6,944 eligible headwords. `/api/health` remained `ready`, all five model/web/
worker/LightDM/WayVNC services were active, and the Pi checkout was clean.

The complete Pi suite passed 131 tests in 36.110 seconds; `compileall` passed.
The corresponding Windows validation passed 131 tests in 78.401 seconds,
`compileall`, JavaScript syntax, and diff checks. An isolated temporary-database
smoke used local `Qwen3-4B-Q4_K_M` plus real Book of Questions entry
`question-060`; `CardService.create_from_evidence` completed its publication
gate, then the temporary files were removed without changing the live deck.

The saved-card dot strip is now a moving window of at most 18 items. A live CDP
reload kept exactly one fullscreen `?display` target and reported 18 rendered
dots for the complete 28-card Question collection with the exact `1 / 28`
counter. Inner EN/JA/ZH slide dots remain local to the current card and are
replaced on every card change.
