# Language completion follow-up, 2026-09-13

## Status

The full system goal remains active. The language-completion patch is deployed,
and its checkpoint-to-card refresh has produced a real improvement. Live
validation also exposed a **performance regression in the new refresh query**.
It has not been corrected in this turn. Do not describe this release as smooth,
complete, or fully validated merely because its unit tests pass.

The frontend candidate from the preceding handoff remains local and undeployed.
No Git operation, network/DHCP change, database deletion, cloud inference, or
model-process restart was performed.

## Diagnosed scheduling gaps

At 03:31 UTC the Pi and Qwen were ready, but Chinese translation and pronunciation
for `hymen` remained queued from Sep 11 10:16 UTC. Some other translation jobs
had waited since Sep 4.

`KnowledgeStore.claim_word_card_progress()` only starts from queued
`compose-word-card` jobs. A previously published partial card can therefore
fall outside that fallback even while its translations are unfinished.
The normal worker also checks semantic maintenance before this fallback.

Accepted translation/pronunciation artifacts are committed independently. The
ordinary translation handler does not itself republish an already completed
Word Card. The repair seeder does plan publication for its own bounded repair
plans, but this does not cover every older admitted translation job.

## Changes

- Added `lkt/language_completion.py` and integrated it into `lkt/growth.py`.
- Reserved dispatch slots 1, 3, 5, and 7 in the existing twelve-slot cycle for
  eligible language completion. Empty reservations return to publication.
- Kept origin publication slots, independent history and morphology turns, and
  maintenance opportunities. Ready accepted origin publication remains ahead
  of inference.
- Language selection considers published partial cards without requiring a
  queued composition. Missing checkpoints precede reruns; per-language service
  times persist in `schema_meta` across restarts and count failed attempts.
- The selected job must be ready and below its attempt limit. Recursive
  prerequisite selection respects the caller's allowed job types.
- Added fingerprint-bound, model-free Word Card refresh jobs when current
  accepted input artifact IDs are absent from the last accepted card snapshot.
  This can publish one language without waiting for every other translation
  or pronunciation.
- Added queue and last-dispatch diagnostics to growth status.

There are no hardcoded production words, meanings, translations, or evidence
IDs in the implementation. The existing local-RAG atomic handlers still own
inference and provenance.

## Tests and deployment

- 14 new regression tests passed locally in 3.849 seconds.
- The same 14 tests passed on the Pi in 17.221 seconds before deployment.
- 48 related growth, language-repair, publication, and fairness tests passed.
- Full local suite: **543 tests passed**, 397.661 seconds.
- `python -m compileall -q lkt tests` passed.
- The refresh integration test published a Chinese translation through the
  real composer with zero additional model calls and preserved book evidence.

Deployment completed at **03:53:04 UTC**. The worker stopped gracefully; source
checksums were checked before and after installation. The web service and worker
were restarted, while the existing Qwen server was left running. All three
services were active afterward.

Only `lkt/growth.py`, the new language-completion module, and its new test were
installed. The validation package used a temporary package search path pointing
to staged changes and the remaining existing Pi modules.

## Live evidence

The first post-deployment snapshot at 03:54:11 UTC still contained 282 active
Word Cards and 23 with an empty saved Chinese field. This snapshot is an initial
post-deployment observation, not a pre-deployment database backup.

Two model-free refreshes completed:

| Word | Completed, UTC | Composition job |
| --- | --- | --- |
| photosynthesis | 03:58:45 | `job-4b6bf002-39bd-5a09-87b6-ca4b41c78a6b` |
| aeronautics | 04:01:28 | `job-2e4c186f-d4bf-5899-80e4-ef1947adc625` |

By 04:01:51 UTC the missing-Chinese count had fallen from **23 to 22**. This
shows that at least one missing displayed language was an outdated saved card
despite an already accepted input, rather than a need for new inference.

The old `symmetry` grammar-properties job, created Sep 4, also completed at
03:56:56 UTC. Existing origin publication for `deploy` completed at 03:55:27.

At the final bounded-monitor snapshot, 04:02:51 UTC, 19 refresh jobs remained
queued. No `language_completion_dispatch_v1` record had yet appeared. Therefore
the new reserved language path was unit-tested but **not yet demonstrated by a
live completed translation in this observation window**. The refresh success
does not prove the entire translation backlog is draining.

## Performance regression requiring correction

The worker spent long CPU-bound intervals between dispatches. The refresh
scanner queued its batches around 03:55:25, 03:56:55, and 03:58:45. The actual
Word Card composition after the third scan completed within roughly a second.

A readonly probe executed the new scanner's real selection query against the
Pi database, with writes disabled and a three-second progress-handler limit.
It was interrupted after **3.029 seconds and approximately 2,950,000 SQLite VM
steps**, without completing the selection. It made no model calls or database
writes.

`EXPLAIN QUERY PLAN` showed:

- A scan of the input artifact table.
- Correlated subqueries repeatedly scanning the card artifact table.
- Repeated JSON virtual-table scans for included artifact IDs.
- Temporary B-trees for grouping and ordering.

This is a scaling failure not exercised by the small unit-test fixtures. The
next correction should replace the correlated global scans with bounded or
materialized subject/checkpoint matching, and measure the replacement against
the real database before another deployment. Refresh admission and dispatch
latency must both be considered; a per-call result limit does not bound how
much work SQLite performs to obtain those results.

Do not delete useful accepted data or disable local inference to hide this
problem. Do not infer that the worker is stopped from an observation timeout:
the worker was active, consuming CPU, and producing some completed jobs.

## Runtime and handoff

Runtime unchanged from the previous handoff:

- Pi Python 3.11.2.
- Local `Qwen3-4B-Q4_K_M.gguf`, context 3072, four threads, one inference slot.
- Model SHA-256 last verified Sep 11:
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- llama.cpp 0.3.0-dev, build 0; upstream commit unavailable.
- Previously verified running executable SHA-256:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.

Pi stage, source backup, validation results, health snapshot, and live probes:

```text
/home/lachlan/LocalKnowledgeTerminal/runtime/language-completion-20260913/
```

Important artifacts: `baseline.json`, `progress-0.json` through
`progress-6.json`, `refresh-query-profile.json`, `pi-tests.log`,
`deployed-at.txt`, `SHA256SUMS`, and `source-before.tar.gz`.

Local logs and release:

```text
/tmp/lkt-language-completion-tests.log
/tmp/lkt-language-regression.log
/tmp/lkt-language-full.log
/tmp/lkt-language-completion-release.tar.gz
/tmp/lkt-language-completion-before-20260913.tar.gz
```

Persistent handoff location:

```text
/mnt/c/Users/Administrator/LKT-backups/20260913-language-completion/
```

Previous deployment and unresolved UI/data issues are documented in
`docs/ready-publication-and-model-json-2026-09-13.md`.
