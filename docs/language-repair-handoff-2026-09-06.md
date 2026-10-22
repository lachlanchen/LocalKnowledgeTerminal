# Continuous language repair deployment: 2026-09-06

This supersedes `language-repair-handoff-2026-09-05.md`. The implementation is
deployed on the Pi, not merely present in the Windows checkout. The persistent
worker continues independently of this Codex session.

## Product intent preserved

- Use local Qwen with retrieved books and dictionaries, never an online model or
  hardcoded word answers.
- Preserve useful partial output. Suspected semantic imperfections trigger
  later review rather than deletion of the whole card.
- Keep inference, retrieval, durable knowledge, card JSON and browser rendering
  separate. Other output adapters can consume the same language atoms.
- Continue exploring new words and morphology while gradually repairing old
  records. One troublesome word must not monopolize the worker.
- Preserve the existing visual design and fullscreen layout. This release does
  not change CSS or browser layout code.

## Diagnosed failures

`stout` had no accepted Chinese translation. Earlier model drafts supplied an
English headword in the Chinese term field, despite supplying a usable Chinese
explanation. Its original two-attempt job exhausted its retries; the partial
card remained published without a general repair admission mechanism.

This was not a corrupt SQLite file or merely clipped browser text. Separate
problems also existed: a bounded diagram traversal could omit accepted
languages, stale translation readings could overwrite dictionary pronunciation,
and the sole Arabic dictionary candidate could force the beer sense onto a
source adjective sense.

Worker liveness had another independent problem: its heartbeat thread still
wrote into the main graph SQLite database. Long semantic transactions could
make a running worker appear stale.

## Implemented structure

- `lkt/language_projection.py` builds language atoms directly from accepted
  meaning, translation and pronunciation checkpoints, independent of diagram
  node/edge limits. Pronunciations must match the current target form.
- `lkt/language_repair.py` observes missing languages, missing canonical links,
  changed source senses, reading mismatches, suspicious scripts, unchanged
  untranslated forms, uninformative definitions and damaged text.
- Durable `lexical_language_health` and `lexical_language_scan` tables retain
  observations, queue ownership, attempts, cooldowns and a scan cursor.
- Each normal planning pass scans eight prepared subjects. At most one repair
  chain is admitted, with six other terminal jobs between repair chains.
- An unsuccessful repair enters an exponential cooldown starting at one hour
  and capped at seven days. Reusable accepted data is preserved.
- Reading-only repairs run pronunciation and composition. Structural-only
  repairs atomically restore accepted checkpoints and links without inference.
- Semantic or missing-language repairs retrieve context, prepare one target
  translation, prepare its pronunciation, then recompose the card. They do not
  depend on every other language succeeding.
- `lkt/atomic.py` supplies prior errors, the source sense and retrieved context
  on retries. A valid Arabic model result is no longer forcibly replaced by a
  possibly different dictionary candidate. Model-owned results remain marked
  as model-owned; dictionary citations are not invented.
- Dictionary-backed Japanese/Chinese reading corrections are synchronized into
  durable translation checkpoints, not only overlaid in the browser.
- `lkt/web.py` overlays accepted language atoms without a second bounded graph
  traversal. Both Word Card and Word Origin expose the repaired languages.
- `KnowledgeStore` writes liveness to `knowledge.sqlite3.heartbeat.sqlite3`,
  separately from graph transactions. Status reads fall back to the legacy
  table only before the sidecar exists; a stale sidecar cannot be masked by a
  fresh legacy row.

## Validation and actual local-model repair

- Full suite: **449 tests passed** in 591.262 seconds.
- `python -m compileall -q lkt tests` passed.
- Earlier multilingual/CLI/web repair focus: 66 tests passed.
- Pi regression run: **18 tests passed**, including graph-writer contention,
  durable cooldowns, checkpoint restoration, pronunciation consistency and
  preservation of valid Arabic model output.
- The full run emitted SQLite `ResourceWarning` messages about unclosed test
  connections. They were warnings, not test failures; warning cleanup is not
  claimed complete.
- Consistent knowledge/cards SQLite backups passed `PRAGMA quick_check`.
- Live database quick check, foreign-key check and morphology finalization
  invariants passed. There were zero duplicate accepted edge triples and zero
  accepted edges attached to archived/rejected endpoints.
- A rehearsal against a disposable copy of the real database selected `stout`
  Chinese through the general repair mechanism. No answer was injected.
- Live translation job `job-2d13f6d9-6532-5351-9bbb-b643191b9690` succeeded on
  its second bounded attempt at 2026-09-05T17:10:07Z. Qwen generated a Han term
  with pinyin `wei wu` and a Chinese explanation of the book's historical sense.
  Exact Unicode output is retained in the runtime JSON reports.
- The successful inference used local `Qwen3-4B-Q4_K_M`: 861 prompt tokens,
  59 completion tokens, 59.32 seconds. Its result is explicitly model-owned,
  not falsely represented as a dictionary quotation.
- Chinese pronunciation job `job-af83701f-d8bb-5e1e-bb38-039fc0512813` completed
  at 2026-09-05T17:15:04Z. Composition job is
  `job-d4b5c5a3-b9be-5bea-aa62-645d466fefd8`; its final persisted outcome is
  recorded in `live-repair-result.json` under the backup directory below.
- Both live Word Card `b11a90b4-2c5b-473c-8842-896f8af1adb8` and Word Origin
  `ebe70866-cc0e-4ecb-8b86-2686099c85c9` returned Chinese, correct Japanese
  dictionary reading, and all five language codes from accepted checkpoints.
- A real Edge compact Word Card at 820 x 720 had document width 820 and its
  language blocks inside the card viewport. This is a sample visual check,
  not a claim that every possible card/layout is perfect.

The first live draft still echoed the English headword. Before adding another
inference path, the existing error-informed retry succeeded. No additional
single-field inference path was introduced after the passing release tests.

## Growth and remaining backlog

Across this session's observations:

| Measure | Earlier | Later |
| --- | ---: | ---: |
| Graph revision | 9585 | 9607 |
| Entities | 16468 | 16485 |
| Translations | 843 | 846 |
| Active root cards | 49 | 50 |
| Active affix cards | 59 | 62 |
| Completed morphology enrichment clusters | 28 | 30 |

The immediate predeployment database backup was at revision 9599. The broader
growth comparison includes work performed before and after deployment; it is
not all attributed to this language repair change.

A read-only census covered **285 prepared source subjects**. It found 317
healthy language observations and these overlapping review flags:

| Flag | Count |
| --- | ---: |
| Missing translation | 313 |
| Source sense changed | 281 |
| Dictionary sense needs review | 122 |
| Reading mismatch | 117 |
| Uninformative definition | 25 |
| Unverified unchanged form | 14 |
| Missing current pronunciation | 3 |
| Unexpected meaning script | 1 |

Counts are a moving snapshot, not distinct incorrect words or proof of semantic
error. The full census is read-only; the persistent bounded scanner separately
records observations and admits repairs. It had reached 160 durable language
observations at 17:17Z, including one active repair chain.

An attempted concurrent administrative write census encountered the graph's
writer lock and was replaced with this read-only census. The worker was not
stopped or its active transaction interrupted.

The database still contains imperfect model-authored meanings/history and
older failed Origin composition jobs. This release does not claim to fix every
semantic fact, eliminate the entire backlog, or guarantee monotonically rising
card counts on every polling interval. Existing semantic and morphology growth
lanes remain enabled alongside language repair.

## Deployment, runtime identity and recovery

- Live source: `/home/lachlan/LocalKnowledgeTerminal/source`.
- Backup/reports:
  `/home/lachlan/LocalKnowledgeTerminal/data/backups/language-repair-20260906`.
- Consistent backups: `knowledge.sqlite3` and `cards.sqlite3`.
- Original source rollback: `rollback/`. Frozen deployed modules and regression
  sources: `stage/`. Exact old/new SHA-256 hashes: `deployment.json`.
- Reports include `baseline-health.json`, `real-book-rehearsal.json`,
  `live-repair-admission.json`, `live-repair-result.json`,
  `live-card-language-check.json`, `language-health-census.json`,
  `postdeploy-structural-audit.json`, `edge-audit.json`, `final-health.json`,
  `runtime-revisions.json`, and test logs.
- Worker replaced gracefully after its current work: PID 79615 -> 84021.
  Its restart counter increased from 1 to 2 for this intentional replacement.
- Web restarted once: PID 80560 -> 83883. Model server remained PID 1341,
  with no model restart. Networking/DHCP/Wi-Fi settings were untouched.
- Model artifact: `Qwen3-4B-Q4_K_M.gguf`, 2,497,280,256 bytes. Its file identity
  and modification timestamp are recorded in `runtime-revisions.json`; no
  upstream model commit hash is claimed.
- Runtime: llama.cpp `0.3.0-dev`, build 0, commit unknown, GNU 12.2.0 on Linux
  aarch64. Binary SHA-256:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.
- No Git operations, public uploads, book exports or network reconfiguration
  were performed in this deployment.

For rollback, preserve subsequent database growth. Roll back application files
from the saved source snapshot and coordinate a graceful worker replacement;
do not blindly replace the live database with the older backup. The sidecar and
repair tables contain runtime state, not corpus content or model weights.

The long-term goal tracker was paused and is not marked complete. The Pi's
continuous systemd worker is separate from that tracker and remains active.
