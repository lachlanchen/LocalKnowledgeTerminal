# Continuous lexical growth repair: 2026-09-11

## Status

Deployed to the Raspberry Pi at `192.168.70.131`. The web application at
`http://192.168.70.131:8090/` responds, and the web, worker, and local-model
services are active and enabled. Continuous growth runs in the background;
it does not depend on keeping a Codex conversation open.

This is a verified growth and recovery deployment, not a certification that
every generated etymology is correct or that every historical UI request is
complete. No browser layout assets, network adapters, DHCP configuration,
model weights, or external model providers were changed in this repair.

## Confirmed causes and changes

- History discovery depended on idle opportunities that a persistent backlog
  rarely provided. The lexical watch now reserves bounded history and
  morphology turns, with explicit ordinary-queue turns between them.
- Morphology declaration jobs could dominate component and relationship work.
  Claims now rotate among these kinds using durable attempt timestamps.
- Graph-only terms and unrelated translation jobs could prevent history
  revisits. History eligibility now allows these terms, refreshes retrieval,
  reuses accepted checkpoints, and recovers expired planning leases.
- Publication rejected otherwise useful cards for incomplete Japanese or
  Chinese reading annotations. Those two conditions are now visible warnings;
  structural, provenance, and encoding failures still block publication.
  Seven latest annotation-only failures were recovered idempotently.
- Related Word Origins passages were being discarded even when their prose
  mentioned the target word. Bounded excerpts now reach the local model as
  contextual reading, without becoming automatic claim-level evidence.
- Missing historical language or period metadata could erase useful model
  output. These values may remain unspecified with conservative confidence.
  The review limit now agrees with the prompt's one-to-five-step contract.
- Two forms appearing in the same excerpt were incorrectly treated as proof
  of a directed historical relationship. Form attestation and transition
  evidence are now separate. Model-inferred transitions retain contextual
  evidence and capped confidence. Explicit book-extracted relationships can
  retain direct evidence.
- Revisits can correct the automatic producer's legacy co-occurrence-based
  support transactionally. Other owners and reviewed relationships remain
  protected; a failed replacement rolls back. Corrections are recorded.
- Projection repair selection repeatedly scanned artifacts and queued jobs
  for each pending repair. Grouped readiness and active-composition lookups
  replace both correlated scans while retaining leases, priority, and burst
  fairness. On the real database, the old query exceeded a ten-second cutoff;
  the replacement completed in approximately 0.17 seconds.
- A terminal projection-repair path called an undefined reconciliation name.
  It now calls the store's available reconciliation method.
- The old 90-second shutdown allowance could terminate an in-flight local
  model request. The worker now has `TimeoutStopSec=15min`; subsequent
  deployment stops completed gracefully without restarting the model.

The autonomous policy is opt-in for `work-atomic --watch
--autoprepare-lexical-deck`. Manual workers and explicitly restricted workers
do not silently create autonomous inference jobs.

## Measured growth

These are database measurements, not estimates from service status. The
baseline was captured at 04:18 UTC and the following snapshot at 06:35 UTC.

| Measurement | Before | After |
| --- | ---: | ---: |
| Entities | 18,482 | 18,512 |
| Terms | 9,896 | 9,898 |
| Morphemes | 1,756 | 1,762 |
| Historical forms | 530 | 533 |
| History events | 517 | 520 |
| Graph edges | 18,671 | 18,712 |

The displayable Root and Affix card totals were still 76 and 108. Existing
cards were revised rather than necessarily adding a distinct card. The card
database has 78 active Root rows, two of which are not displayable under its
existing validation policy. Do not equate card totals, graph-node counts,
and successful enrichment runs.

The autonomous history lane completed work for `abeyance`, `aestivate`, and
`apocryphal`, rather than remaining on one subject. Declaration, component,
and link morphology work all completed. `apocryphal` produced a revised Affix
card. The four historical forms linked to `aestivate` already existed in the
shared graph; they were not four additional global nodes.

## Real-book validation

A queued `abeyance` check initially remained pending through its observation
window. The fairness defect was fixed, and the existing validation jobs were
then run single-flight through the same production worker while background
processing was paused. No separate model or hardcoded linguistic result was
used. The background worker was restarted afterwards.

- Fresh corpus retrieval completed at 06:28:48 UTC.
- Local Qwen origin expansion completed at 06:33:33 UTC.
- Composition published card `86276646-e77a-47ed-bbee-6aca156902ac`.
- The card contained five graph nodes and four edges, with no structural
  validation errors. Its published citation resolved to a stored corpus
  record, including its source hash and locator.
- The run persisted three historical forms and three history events. The
  model call took 283.66 seconds: 1,207 prompt tokens and 340 completion tokens.

This validates retrieval, local inference, persistence, provenance handling,
and publication. It is **not** factual verification of the generated history.
The model proposed uncertain dates and two stages with the form `abeance`;
those claims remain model-owned. Related forms, chronology, and repeated
historical stages still need ongoing linguistic refinement. Do not describe
the database as error-free or silently promote these hypotheses to book facts.

Earlier `pretext` Word/Root/Affix publication was also checked against stored
Root Dictionary, Affix Dictionary, and Word Origins corpus records. Reading
annotation gaps remained warnings rather than suppressing the entire card.

## Validation results

Final local validation:

```bash
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

All 482 tests passed in 314.919 seconds; compilation passed. Tests used a
RAM-backed temporary directory, avoiding the Windows mount's slow test writes.

The Pi passed a 93-test production-regression run, followed by 23 focused
tests covering the final queue policy, continuous growth, and provenance
repair. The live health endpoint reported `ready`, a ready local Qwen model,
and an active worker with no current growth error.

No fresh browser screenshot or visual redesign was performed. Existing web
regression tests are included in the full suite.

## Model and runtime revisions

- Model: `Qwen3-4B-Q4_K_M.gguf`.
- Model SHA-256:
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- Runtime: local `llama-server`, reporting `0.3.0-dev`, build 0,
  upstream commit unknown; GNU 12.2.0, Linux aarch64.
- Server binary SHA-256:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.
- Pi worker Python: 3.11.2. Context: 3,072 tokens; four model threads;
  one inference slot. The model server was kept running through deployment.

## Recovery and handoff

Pi source: `/home/lachlan/LocalKnowledgeTerminal/source`.
Pi databases: `/home/lachlan/LocalKnowledgeTerminal/data`.

Database backups, release archives, and verification logs are outside Git in
`C:\Users\Administrator\LKT-backups\20260911-growth`. Both compressed
pre-change SQL backups were restored locally and passed SQLite integrity
checks. No full database copy was made on the nearly-full Pi filesystem.

Deployment records and small source backups are under the Pi runtime's
`growth-update-20260911*` directories. The final live-book record is
`growth-update-20260911-origin/live-book-check.json`; the measured snapshot is
`growth-update-20260911-final/final-growth-check.json`.

At the final snapshot, the Pi had approximately 939 MiB free and was 97% full.
Storage capacity is a real limit on indefinite growth and must be monitored.

Preexisting workspace changes were preserved. This deployment was not bundled
into a Git commit or push containing unrelated preexisting work. The existing
LexiconAtlas downloadable database remains a static snapshot, not a live view
of this evolving Pi database.

## Follow-up: new-card admission starvation

The 09:17 UTC follow-up confirmed continuing graph growth, but not increasing
visible Root/Affix card totals. Between the 06:35 and 09:45 UTC snapshots:

| Metric | 06:35 UTC | 09:45 UTC |
| --- | ---: | ---: |
| Terms | 9,898 | 9,908 |
| Morphemes | 1,762 | 1,768 |
| Historical forms | 533 | 536 |
| History events | 520 | 523 |
| Graph edges | 18,712 | 18,810 |
| Visible Root cards | 76 | 76 |
| Visible Affix cards | 108 | 108 |

The admission gate incorrectly counted 58 old morphology subjects whose only
unfinished work was optional translation, pronunciation, grammar, or Word Card
composition. Those subjects occupied a two-subject publication budget even
though none had unfinished origin-publication work. Independent continuous
history revisits could also hold the lexical discovery gate.

`lkt/deck.py` now counts only retrieval, meaning, morpheme splitting, history
expansion, and origin composition against publication slots. Optional language
work remains queued and is not discarded. Independent continuous-history jobs
own separate slots; unfinished legacy publication jobs still retain their slots.
Mode selection favors a book with no active publication subject, and the default
two-slot budget leaves capacity for both Root and Affix sources. Outcomes are
recorded per entry rather than marking an entire batch queued together.

The first live admission repair selected real indexed book entries without
hardcoded answers:

- Root source `p0039-t001-r005`: `avocation`.
- Affix source `p0448-t021-r006`: `ambivert`.
- One active publication subject per mode, rather than 58 stale subjects.
- Retrieval completed at 09:36:25 and 09:46:01 UTC respectively.

At the 09:53 UTC observation, both new entries still awaited their remaining
model/publication stages. They are not counted as newly published cards. The
ordinary worker also admitted new lexical discovery work after deployment.
This verifies that admission resumed, not that all card growth or linguistic
quality issues are resolved. The Pi remained inside a local-model morphology
request, with its heartbeat and inference progress advancing.

### Follow-up validation and deployment

- Added nine publication-admission regressions, including optional-job backlog,
  mode fairness, independent history revisits, and legacy publication scope.
- All 491 local unit tests passed; `compileall` passed.
- All 42 focused tests passed with Python 3.11.2 on the Pi.
- Final source deployed at 09:42:23 UTC after a graceful worker checkpoint.
- LAN `/api/health` returned `ready`; web, worker, and local model were active.
- Qwen3-4B-Q4_K_M and llama-server revisions are unchanged from the handoff above.
- Final `lkt/deck.py` SHA-256:
  `f651470a49d4ff1962bf78020acc589ebf0317e25984854a7e429a46170739ae`.

Pi deployment evidence, prior source, and real-source admission records are in
`/home/lachlan/LocalKnowledgeTerminal/runtime/admission-repair-20260911/`.
The release archive, complete local test log, and runtime proof archive were
also preserved under `C:\Users\Administrator\LKT-backups\20260911-growth`.
No networking configuration, model weights, or frontend layout changed in this
follow-up. No unrelated workspace changes were committed or pushed.
