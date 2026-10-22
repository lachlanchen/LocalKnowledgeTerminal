# Lexical growth and service recovery handoff

## Objective and status

Continue building one multilingual lexical knowledge graph using local Qwen
and retrieved books/dictionaries. Word Cards, Word Origin, Root, and Affix
are projections of that shared graph. Preserve useful partial model output,
source-derived citations, bounded revisits, and responsive readable layouts.
Do not replace uncertain linguistic knowledge with hardcoded answers.

The long-term goal is **not complete**. The goal tool still reports `paused`;
its available update operation cannot resume a paused goal. The Pi worker
is running independently and continuing its existing queues.

## Changes deployed

- Preserved the approved durable worker heartbeat implementation. Limited its
  interval to five seconds and joined the heartbeat thread before publishing
  the final stopped state, preventing delayed writes from reversing shutdown.
- Added a bounded Word Card progress allocation so continuous Origin repair
  work cannot indefinitely starve translation, pronunciation, grammar, and
  Word Card composition. Included implicit sibling language prerequisites.
- Guarded graph writers against archived endpoints inside their write
  transactions. Retired five existing owned invalid morphology assertions,
  their edges, and their component slots without deleting provenance history.
- Preserved compact Origin branch pagination and measured readability work.
  Resize transitions now reapply the compact layout and text fitting.
- Acquired the payload-refresh write lock before streaming its source reads,
  avoiding SQLite read-snapshot upgrade failures during startup.
- Added a current-schema serving attachment in `KnowledgeStore`. The HTTP
  adapter no longer reruns legacy migrations and payload hydration on every
  restart while the worker is writing. Fresh, empty, and older databases
  still initialize normally. Default/worker startup retains initialization.

## Service incident and recovery

The first web restart exposed `database is locked` in the startup payload
refresh. A subsequent restart exposed contention earlier in `_initialise`,
which was replaying legacy discovery writes on an already-current database.
The serving attachment addresses that broader startup responsibility problem.

Actual HTTP readiness was confirmed after the final deployment, not merely
inferred from systemd reporting a running process. At 2026-09-05 15:13 UTC:

- `/api/health`: `ready`.
- Word Card, Word Origin, Root, and Affix list endpoints: successful.
- Worker: running, generation ready, heartbeat age 3.754 seconds.
- Web PID: 80560; automatic restarts since the final start: 0.
- Worker PID: 79615, following one intentional graceful replacement.
- Qwen PID: 1341, unchanged throughout deployment; no model restart.
- No routing, DHCP, DNS, or adapter settings were changed in this continuation.

## Observed growth

Comparison starts with the saved 14:11 UTC health baseline and ends around
15:11 UTC on 2026-09-05. These are observations, not a claim that all linguistic
content is correct.

| Measure | Baseline | Later observation |
| --- | ---: | ---: |
| Graph revision | 9534 | 9560 |
| Reported entities | 16422 | 16444 |
| Completed v7 morphology enrichment jobs | 21 | 27 |
| Active Root cards | 49 | 49 |
| Active Affix cards | 70 | 59 |

Active card counts can decrease during cleanup and replacement. Root and
Affix cards received fresh updates, but this interval does **not** demonstrate
an increase in their distinct entries. Revision growth also includes meaning
and pronunciation enrichment, not just new roots or relationships.

A new Word Card for `return` was published at 14:51:00 UTC. The previous
newest card was `stout`, published on the preceding day. This demonstrates
that the previously starved Word Card queue resumed.

An Origin projection for `revert` was published at 15:06:24 UTC. Its retrieved
evidence includes root-dictionary page 32, affix-dictionary page 386, and
Word Origins page 543. The last item concerns `vertical`; its presence is
contextual evidence and is not proof of every displayed historical relation.

## Validation

- Full growth/heartbeat build: 429 unit tests passed before the final startup
  changes.
- Final startup/serving changes: 42 focused tests passed, covering startup
  contention, fresh/old schema handling, heartbeat, growth integrity, and web
  behavior. The full suite was not rerun after those final startup changes.
- `python -m compileall -q lkt tests`: passed.
- JavaScript syntax check on the deployed layout changes: passed.
- Pi staged Python compilation: passed.
- Live SQLite `quick_check`: `ok`; foreign-key violations: 0.
- Live morphology finalization invariants: passed.
- Dangling accepted edges: 0; accepted edges on archived endpoints: 0.
- Duplicate accepted source/relation/target edge triples: 0.
- Live compact Origin browser check: three `constrain` branch pages at 820px,
  minimum measured text size 8.15px, no measured overlap or clipping; desktop
  and compact resize transitions passed. This is a sampled layout check,
  not proof that every graph is readable.
- Earlier candidate browser checks covered manual scrolling, autoplay,
  headings, and layout. Sparse graphs and a deferred alias remained flagged.

## Remaining correctness and growth gaps

1. Four Origin composition jobs failed during the observation interval: one
   incomplete Chinese ruby annotation, one `oldfrench` language-code error,
   and two failures reconstructing accepted evidence. Recover annotation and
   language-format problems without discarding otherwise useful cards. For
   missing evidence, retrieve or reconstruct real records; never invent cites.
2. The `return` card has conflicting Japanese reading and dictionary-backed
   ruby. Reading selection and projection should use consistent provenance,
   with a general repair path rather than a hardcoded correction for this word.
3. An Arabic translation exists for `return`, but its live language projection
   reports only `fr`, `zh`, `ja`, and `en`. Audit the accepted translation to
   target-term/sense to card path, including dictionary reuse and older data.
4. Verify sustained discovery of additional Root and Affix entries separately
   from enrichment and re-rendering of existing entries. Completed jobs and
   graph revisions alone are insufficient evidence for this requirement.
5. Sparse Origin graphs such as `falling` remain. An `ab`/`ab-` alias was
   deliberately deferred where evidence was incomplete. Do not merge by label
   alone or fabricate historical nodes to make a graph look larger.
6. Structural invariants do not validate etymological truth. Continue small
   evidence-grounded local-model correction tasks, retain provenance, and
   preserve useful partial results with transparent uncertainty.

## Runtime and recovery records

- Model: `Qwen3-4B-Q4_K_M`, local only.
- Runtime executable:
  `/home/lachlan/LocalKnowledgeTerminal/runtime/llama.cpp-0.3.0/build/bin/llama-server`.
- Reported runtime revision: `0.3.0-dev`, build 0, commit unknown;
  GNU 12.2.0, Linux aarch64. No more precise commit was available.
- Knowledge schema: 13.
- Morphology parser: `book-morphology-observation-v7`.
- Lexical projection contract: 5.
- Pi source: `/home/lachlan/LocalKnowledgeTerminal/source`.
- Pi backup root:
  `/home/lachlan/LocalKnowledgeTerminal/data/backups/growth-heartbeat-20260905`.
- Backups contain consistent pre-change database copies, source rollback
  files, staged sources, repair inputs/results, and final service/database
  audit JSON. The `serving-startup` directory holds the final startup patch
  and the preceding source versions.
- No commit or push was performed in this continuation.

Final deployed source hashes:

```text
e28780ea90343b7456e43ff7909ce46db0b01995591d5a5a9c88c1a56b259cd4  lkt/knowledge.py
7accfd336c688218daff9bd55c3f5cc26a0c353e5372b3483aeffe373bfe0628  lkt/web.py
```
