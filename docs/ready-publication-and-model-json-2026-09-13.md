# Ready publication and local-model JSON recovery

## Status

The system-wide growth goal remains active. These backend changes are deployed
on the Pi. This is not a claim that every card is complete or every relationship
is correct. The frontend candidate described below is **not deployed**.

No cloud inference, network/DHCP changes, model restart, database deletion, Git
commit, or push was performed. Existing unrelated workspace changes remain.

## Deployed changes

- `lkt/growth.py` publishes dependency-ready origin cards before another slow
  model dispatch, including completed continuous-history work.
- `lkt/publication_queue.py` permits one additional cached composition refresh
  per normal dispatch. Its durable quota prevents refresh work from repeatedly
  consuming the same inference turn after restarts.
- `lkt/llm.py` and `lkt/model_json.py` retain the last two failed JSON responses
  and bounded request diagnostics in a `ValueError`-compatible exception.
- `lkt/atomic.py` records those replies as rejected, nonreusable
  `model-output-failure` artifacts, then releases the job through the existing
  failure handling. A diagnostic write failure must not retain the lease.
- `lkt/model_json.py` subsequently gained a narrow repair for omitted brackets
  around multiple `evidence_ids`. Other malformed fields are not reinterpreted
  as lists. The repair does not create meanings, IDs, or citations.

Ready-publication deployment: **2026-09-13 02:53:45 UTC**.
Evidence-array deployment: **2026-09-13 03:16:50 UTC**.

Workers were stopped gracefully for Python replacement. The web service was
restarted after installation, and all three services were active afterward.
The existing model process was left running.

## Measured growth and publication

| Measurement | Previous observation, Sep 12 23:21 UTC | Sep 13 03:26 UTC |
| --- | ---: | ---: |
| Visible root cards | 86 | 89 |
| Visible affix cards | 135 | 139 |
| Morphemes | 1,848 | 1,860 |
| Historical forms | 588 | 625 |
| History events | 575 | 612 |
| Graph edges | 19,586 | 19,810 |

The visible root/affix totals were already 89/139 at 02:58 UTC. Later updates
include revisions of existing cards, not necessarily additional distinct cards.
Do not describe revision timestamps or total preparation jobs as vocabulary
growth.

The previously delayed `edict` composition job
`job-dff3bf29-9e93-50be-949f-9de0a6689020`, queued since Sep 12 22:49 UTC,
completed at 02:54:31 after ready-publication deployment. `deprecate` and `tact`
also published at 02:54 without waiting through another model task.

At 03:23 UTC the autonomous worker published refreshed `averse` cards using
retrieved root-dictionary entry `p0031-t001-r001` (source page 31) and
affix-dictionary entry `p0020-t003-r005` (source page 20). Example origin-card
revision: `78086135-612f-4343-9dff-f2be838041bd`.
Saved morphology snapshots and live canonical graph projections have different
node counts; do not conflate those representations when diagnosing a graph.

At 03:26:40 UTC `/api/health` was ready, the worker was running without a
reported blocker, and its heartbeat was about two seconds old.

## Captured model failure and verified recovery

Job `job-44bd5d3d-558a-5e6b-a99d-70886a910e0f` produced a meaning and confidence
but formatted its two retrieved evidence IDs as adjacent object values instead
of an array. The formatting retry repeated the same mistake.

The two original requests took 216.71 and 101.20 seconds, with 107 completion
tokens each. Both ended normally, with no reasoning text in the response.
This observed failure was not an unfinished reasoning block or token cutoff.

Both recorded responses now parse on the deployed Pi with recovery method
`evidence-array-repair`. The replay preserved both IDs and made **zero model
calls and zero database writes**. It demonstrates syntax recovery, not a new
linguistic validation or acceptance of the old rejected artifacts. Citations
continue to come from retrieved records through the normal pipeline.

## Validation

- Final local full suite: **529 tests passed**, 408.659 seconds.
- Local `compileall` passed after the evidence-array change.
- Ready-publication focused suites: 73 local tests and 34 Pi tests passed.
- Evidence-array regression suite: six tests passed locally and on the Pi.
- Four local failure-trace tests passed after the parser update.
- Web suite: 28 tests passed; JavaScript syntax check passed.
- Live browser arrow navigation changed tabs vertically and cards horizontally.
- Live default ambient routing visited Question, Answer, Word card, Origin,
  Root, Affix, then Question again, with no JavaScript page errors.

The ambient test advanced browser timers by 31 seconds and waited for each live
card to finish loading. An earlier fixed-delay sample missed the Origin load;
the settled test passed. This is timer-controlled browser validation, not a
multi-hour wall-clock soak test.

## Remaining gaps

### Multilingual completion

A readonly audit found **23 of 282 active word cards missing Chinese**. `hymen`
has an empty saved Chinese object, and its Chinese translation and pronunciation
jobs have been queued since Sep 11 10:16 UTC. This is unfinished backend work,
not hidden browser text. The saved `stout` card does contain Chinese.

Queued translation jobs: Japanese 39, Chinese 39, French 40, Arabic 41. Some
have waited since Sep 4. There are also 140 failed entity-meaning jobs. The
publication fix therefore does not establish fair multilingual completion or
successful repair of all historical failures.

The next backend priority is bounded, age-aware completion of missing languages
and republication of their cards, while preserving new-word and history growth.
Recovery must continue to use local inference and retrieved material rather
than filling answers with fixed vocabulary.

### Frontend candidate held locally

Local changes in `lkt/static/app.css` and `lkt/static/app.js` address the mobile
shell's min-content overflow, widen graph headings, use the selected morpheme's
meaning for overview headings, and tighten root/affix layout rows. Node terms
remain on one line. The existing visual language is retained.

Browser candidate checks at widths 320, 390, 900, and 1280 showed no document
width overflow. Word-card language blocks stayed within their panels. The
1280px root example gained substantially larger node text.

However, visual inspection found that the enlarged `in-` node was covered by
the overview minimap. The candidate was **withheld rather than deployed**.
Phone-sized morphology graphs also still have very small explanatory text.
These UI edits remain local pending a follow-up decision; passing web tests
does not make the candidate ready to release.

The headless Linux browser lacks the Windows CJK fonts, so square glyphs in its
screenshots are not proof that those language strings are missing from data.

Graph role conflicts and missing derived-word meanings remain visible and need
continued corpus-backed polishing. Layout improvement alone does not repair
those saved relationships.

## Runtime revisions and artifacts

- Python: 3.11.2 on the Pi.
- Model: local `Qwen3-4B-Q4_K_M.gguf`.
- Model SHA-256, last verified Sep 11:
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- Running llama-server SHA-256, verified in the preceding deployment:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.
- Runtime: llama.cpp 0.3.0-dev, build 0; upstream commit unavailable.
- Context 3072, four threads, one inference slot, port 8081.
- Application: `http://192.168.70.131:8090/`.

Pi release records, checksums, source backups, tests, and health snapshots:

```text
/home/lachlan/LocalKnowledgeTerminal/runtime/ready-publication-20260913/
/home/lachlan/LocalKnowledgeTerminal/runtime/evidence-array-20260913/
```

The latter includes `replay.json`. Observation snapshots remain under
`runtime/growth-update-20260911-final/`, including `ready-installed.json`,
`ui-followup.json`, and `evidence-array-live.json`.

Local test and browser artifacts:

```text
/tmp/lkt-ready-focused.log
/tmp/lkt-ready-full.log
/tmp/lkt-evidence-array-tests.log
/tmp/lkt-evidence-full.log
/tmp/lkt-ui-web-tests.log
/tmp/lkt-browser-audit/
```

Persistent Windows handoff location:

```text
/mnt/c/Users/Administrator/LKT-backups/20260913-ready-publication/
```

Books, databases, screenshots, browser installations, and runtime release
archives stay outside Git. The prior throughput handoff is
`docs/publication-throughput-2026-09-13.md`.
