# Language completion: remove repeated database scans

## Scope and deployment

Deployed to the Pi at `2026-09-13T04:27:56+00:00`.

This corrects the performance regression found after the language-completion
release. It does not replace the local model, change the corpus, reject imperfect
linguistic output, or claim that every outstanding product issue is resolved.

Changed source and regression coverage:

- `lkt/language_completion.py`
- `tests/test_language_completion_scaling.py`

The worker was stopped gracefully, the module replaced atomically, and the web
and worker services restarted. The local inference service stayed running.
Networking, DHCP, adapters, books, and saved knowledge were not changed.

## Cause and correction

The refresh scanner repeatedly searched accepted card artifacts and their JSON
input manifests for individual accepted inputs. Language selection also used
per-subject artifact scans. These queries grew disproportionately expensive as
the artifact history grew, delaying dispatch even when inference was idle.

Refresh admission now reads accepted inputs and publication manifests once,
groups them by subject, and compares input IDs in memory. A separate query reads
outstanding and failed refresh jobs. Published meanings are still required;
queued, running, and paused refreshes are not duplicated. A failed refresh needs
new accepted input before another attempt is admitted.

Language selection now groups accepted checkpoints once and joins that result
to eligible jobs. It preserves atomic claims, dependency handling, attempt
limits, durable language fairness, and the existing reserved scheduling slots.

No schema migration or new production index was needed. Refresh composition
remains model-free: it republishes accepted knowledge, not invented content.

## Validation

- All 18 focused tests passed locally, including four new scaling regressions.
- The same 18 tests passed on the Pi against the staged module.
- All 547 Python tests passed locally in 362.256 seconds.
- `python -m compileall -q lkt tests` completed successfully.
- The scaling fixtures compare 100 and 500 subjects, including superseded
  artifacts, and measure SQLite VM work rather than wall-clock timing alone.
- The refresh fixture requires two SELECTs and checks that paused work is not
  bypassed and superseded inputs and archived subjects are ignored.

Read-only profiling used the real Pi knowledge database and the staged module.
The probe intercepted mutations before any claim or enqueue and made no model
requests. It deliberately did not exercise dependency-failure mutations; the
unit tests cover those paths.

| Probe | Elapsed | Approximate SQLite VM steps |
| --- | ---: | ---: |
| Refresh admission | 0.2367 seconds | 200,000 |
| Language selection | 0.1655 seconds | 400,000 |

The previous refresh query was interrupted after 3.029 seconds and approximately
2.95 million VM steps. Its full duration was not measured, so this is not an
exact end-to-end speedup claim. The live refresh probe admitted zero additional
jobs because eligible refreshes were already queued; nonempty admission is
covered by the scaling fixtures.

## Live proof

At approximately `2026-09-13T04:33:09Z`, `/api/health` reported ready, all three
services were active, and the worker heartbeat was fresh.

After deployment, local Qwen completed the origin analysis for `obliterate`:

- Job: `job-4832dea4-dd03-50c0-a3a0-44db12819075`.
- Accepted artifact: `2026-09-13T04:32:42.944034+00:00`.
- Model: `Qwen3-4B-Q4_K_M`.
- One request, 872 prompt tokens, 369 completion tokens, 241.26 seconds elapsed.
- No formatting retry or JSON recovery was needed.
- Its origin card was published at `2026-09-13T04:32:57.288398+00:00` with
  eight saved graph nodes and seven edges.
- Retrieved references: root dictionary `p0314-t070-r003`, page 314; affix
  dictionary `p0312-t003-r003`, page 312; Word Origins `entry-3702`, page 323.

The `predecessor` origin card was also published, at
`2026-09-13T04:33:04.548617+00:00`, with eight saved nodes and seven edges.
Its references include Word Origins `entry-4845`, page 403; root dictionary
`p0075-t001-r004`, page 75; and affix dictionary `p0344-t003-r003`, page 344.
These references come from retrieved records, not model-generated citations.
Saved graph counts describe the card snapshot, not necessarily the subsequently
projected graph returned by the API.

The old French pronunciation job for `aeronautics` completed at
`2026-09-13T04:33:01.872790+00:00`, confirming a reserved language slot was
dispatched. This is not evidence of a new model-generated translation.

Word Cards for `diagram` and `feas` were republished with accepted Chinese
content. At `2026-09-13T04:34:39.653944+00:00`, direct saved-card counts were:

| Mode | Active cards |
| --- | ---: |
| Word Cards | 282 |
| Origin | 264 |
| Root | 94 |
| Affix | 143 |
| Question | 28 |
| Answer | 32 |

The earlier health snapshot had 91 root cards and 141 affix cards. These are
active card counts, not counts of distinct root spellings or affix spellings.
The earlier and later snapshots show actual publication growth, not merely a
running process.

Missing saved Chinese on active Word Cards decreased from 23 at 03:54 to 18 at
04:34 across the language-completion and scaling releases. This improvement
includes republishing already accepted translations; it must not be presented
as five new Qwen translation requests. The remaining words were `hymen`,
`insignificant`, `deducible`, `insolvency`, `irrevocable`, `line`, `avocation`,
`frail-looking`, `prepare`, `inimical`, `cossack`, `sateen`, `counterfeiter`,
`supplant`, `refection`, `accumulate`, `musician`, and `select`.

The same live snapshot contained 1,862 morphemes, 632 historical forms, 619
history events, 1,196 translations, and 19,856 entity edges. Counts alone are
not proof that every relationship or translation is linguistically correct.

## Runtime identity

- Pi Python: 3.11.2.
- Model: local `Qwen3-4B-Q4_K_M.gguf`, unchanged by this release.
- Last recorded model SHA-256, verified September 11:
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- Last recorded llama-server SHA-256:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.
- Runtime: llama.cpp `0.3.0-dev`, build 0; upstream commit unavailable.
- Configured context: 3,072 tokens; four threads; one inference slot.

The large binaries were not rehashed or replaced for this module-only release.

## Preserved artifacts and remaining work

Pi release, source backup, test and profiling artifacts, health, and publication
proof are under
`/home/lachlan/LocalKnowledgeTerminal/runtime/language-scaling-20260913/`.

A separate Windows-side archive is under
`C:\Users\Administrator\LKT-backups\20260913-language-scaling\`.
Runtime artifacts, books, and database files are not added to the repository.

The broader goal remains active:

- Eighteen active Word Cards still lack saved Chinese; language completion must
  continue, and fresh translation inference plus subsequent publication still
  needs an observed example after this release.
- Local inference remains slow on the Pi. Removing scheduler overhead does not
  remove the approximately four-minute cost of the observed origin request.
- The held `app.css` and `app.js` layout candidate is not deployed. Its enlarged
  graph can be obscured by the minimap, and phone graph text needs more work.
- Sustained root/affix coverage, graph-role correctness, and readable branch
  presentation remain broader acceptance criteria. Passing Python tests and
  rising counts do not establish complete visual or linguistic correctness.
- No commit or push was performed in this work.
