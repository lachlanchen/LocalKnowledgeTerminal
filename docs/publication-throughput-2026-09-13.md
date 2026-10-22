# Publication-first worker: 2026-09-13

## Changes

The earlier admission repair released stale source slots, but did not protect
new-word execution from semantic repairs, projection refreshes, and background
morphology work. The worker now applies publication reservations before those
maintenance selectors.

- `lkt/publication_queue.py` gives unfinished lexical publication plans durable
  affinity, scoped by subject, prompt version, source fingerprint, and compose
  job. It enforces dependencies and attempt limits transactionally. A blocked
  plan yields; completion releases its affinity. History revisits and projection
  or semantic repairs cannot consume these protected publication slots.
- `lkt/growth.py` reserves eight of twelve dispatches for publication, one for
  history, one for morphology, and two for ordinary maintenance. Empty
  publication reservations retain ordinary-request fallbacks. The reservation
  is a dispatch count, not a wall-clock or token-budget guarantee.
- `lkt/atomic.py` applies the reservation before existing repair priorities.
  Morpheme-meaning prompts request one or two evidence identifiers instead of
  reproducing every retrieved identifier in a small output budget.
- `lkt/model_json.py` recovers complete values from recoverable container syntax,
  trailing commas, and literal dictionaries. It does not generate missing
  linguistic claims or evidence. Incomplete strings still require a model retry.
- `lkt/llm.py` requests JSON output with thinking disabled for atomic tasks,
  attempts syntax recovery first, and permits at most one local formatting
  retry while retaining the task and retrieved evidence. Metrics identify
  recovery, request count, retry use, and combined request cost.

There is no cloud inference, hardcoded word answer, frontend redesign, corpus
replacement, or network configuration change in this update.

## Regression validation

The first implementation failed two existing ordinary-request fairness subcases.
It was not deployed. The subsequent fallback correction preserves those
expectations without weakening or replacing their tests.

- All 511 local unit tests passed in 374.533 seconds.
- `python -m compileall -q lkt tests` passed.
- All 22 focused scheduling and JSON-recovery tests passed on the Pi with
  Python 3.11.2.
- Explicit job-type restrictions and manual-worker behavior remain covered by
  the full suite; twenty new tests cover publication affinity and JSON recovery.

## Deployment and real-book publication

Deployment completed at **2026-09-12 22:49:23 UTC**, which is **2026-09-13
06:49:23 Hong Kong time**. The old worker finished its running morphology job
and stopped successfully before installation. The model server was not
restarted. Web, worker, and model services were active afterward.

The normal autonomous worker, without a manual priority override, processed the
existing `finish` plan:

| Event | UTC |
| --- | --- |
| Publication plan originally queued | September 11, 10:16:38 |
| New worker claimed its waiting history stage | September 12, 22:49:57 |
| Accepted history saved | September 12, 22:58:42 |
| Origin card published | September 12, 23:18:25 |
| Worker started the next word, `line` | September 12, 23:18:26 |

The retrieved input contained seven corpus records, including Word Origins
entry `entry-2557` at pages 229-230 and dictionary records. The resulting new
Origin card has six nodes, five edges, and two retrieved evidence records:

- Card: `6decc549-f325-429a-a87e-b2ebb7a93f64`.
- Publication job: `job-d421242b-59a6-5a72-87c1-19d3ab61444b`.
- History job: `job-2aeadaa2-8baa-5d8c-b0ce-1655d5cfef67`.
- Both local history calls produced valid JSON without a formatting retry.
- Generation took 266.04 seconds; the existing linguistic-review call took
  253.33 seconds. These are two model calls, not a single generation request.

The LAN `/api/health` check at approximately 23:21 UTC reported `ready`, a live
worker heartbeat, and the local Qwen model ready. The dispatch diagnostics
showed `publication-first-v1` and affinity had advanced to the next plan.

## Measured growth and remaining limits

| Metric | 22:51 UTC | 23:21 UTC |
| --- | ---: | ---: |
| Morphemes | 1,846 | 1,848 |
| Historical forms | 586 | 588 |
| History events | 573 | 575 |
| Graph edges | 19,568 | 19,586 |
| Visible Root cards | 86 | 86 |
| Visible Affix cards | 135 | 135 |

The new card verified in this observation is an Origin card, not a new Root or
Affix card. Those visible totals did not increase during this observation.
This proves that a waiting pipeline can now reach automatic publication; it
does not establish a sustained cards-per-hour rate or perfect etymological
accuracy.

One semantic-repair job, `job-e14fa24e-29a1-5562-926b-7d4dd8eca55b`, still ended
with `model response did not contain a JSON object`. Its bounded failure did
not prevent the waiting card from publishing or the next word from starting.
Not every malformed or truncated model answer is recoverable. Individual
inference and graph-write stages remain slow on the Pi, and reserved background
turns can still delay a ready card. These are remaining performance and quality
limits, not reasons to report the whole system as perfected.

## Runtime revisions and preserved evidence

- Model: `Qwen3-4B-Q4_K_M.gguf`, unchanged by this deployment. The weight checksum
  recorded in the September 11 handoff is
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- Runtime: llama.cpp `0.3.0-dev`, build 0; upstream commit unavailable.
- Running llama-server executable checksum rechecked during this deployment:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.
- The unchanged model process, PID 1341, started on September 3. Its configured
  context is 3,072 tokens, with four threads and one parallel inference slot.
- Pi deployment files, previous source archive, checksums, tests, health result,
  and publication watch are under
  `/home/lachlan/LocalKnowledgeTerminal/runtime/throughput-final-20260913/`.
- Local release, source backup, full test log, and runtime evidence are preserved
  under `C:\Users\Administrator\LKT-backups\20260913-throughput`.

Unrelated workspace changes were preserved. This repair was not bundled into a
commit or push containing preexisting unrelated work. Book files, model weights,
database snapshots, and runtime artifacts were not added to Git.
