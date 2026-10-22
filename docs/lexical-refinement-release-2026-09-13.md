# Lexical refinement release, 2026-09-13

## Status

Deployed to the Pi at `2026-09-13T06:12:29Z`. This is a backend refinement
release, not a claim that the entire graph is now correct or that all remaining
product work is complete.

The worker stopped gracefully after finishing its current request. Only the
worker and web services restarted. The local model process remained PID 1341;
networking, DHCP, adapters, model weights, and runtime binaries were untouched.
The first health snapshot retained the shutdown heartbeat during startup. A
subsequent snapshot confirmed a fresh, running worker and generation readiness.

## Changes

- Added `lkt/lexical_refinement.py` with explicit structure and origin-path
  policies, bounded reanalysis eligibility, independent path replacement, and
  contextual retirement of replaced model-owned ancestry assertions.
- Updated `lkt/atomic.py` to distinguish a surface fragment from its canonical
  lexical form, allow component languages beyond English and Latin, and ignore
  punctuation separators when checking word coverage.
- Model spelling adjustments retain retrieved records as context rather than
  automatically claiming that the canonical spelling is book-attested.
- Origin revisions replace the selected path instead of concatenating new and
  old steps. Unvisited branches and uncertain empty revisions retain history.
- Superseded model-only ancestry assertions are archived through the store API.
  Book claims and other subjects are preserved. Canonical entities and old
  artifacts are not deleted.
- Updated `lkt/growth.py` so eligible history visits can reanalyse legacy model
  splits. Current-policy model splits are reconsidered every fourth eligible
  epoch. Existing cooldowns, leases, and rotation remain in force.
- The policies are `surface-canonical-v1` and `independent-origin-paths-v1`.

No production word exceptions, cloud inference, database migration, or frontend
changes were introduced in this release.

## Validation

- Full local suite: 570 tests passed in 383.128 seconds; compilation passed.
- Focused Pi candidate suite: 38 tests passed; compilation passed.
- The first Pi test invocation lacked the staged systemd service fixture.
  It produced one missing-file error, not an application regression. After
  staging the unchanged fixture, all 38 tests passed. Both logs are retained.
- A separate replacement-path integration check confirmed that both the old
  contextual assertion and its canonical graph edge became archived.
- Live `/api/health` confirmed the local Qwen model and a fresh worker heartbeat.

The changed tests are `tests/test_lexical_refinement.py` and
`tests/test_continuous_growth.py`. The latter explicitly marks an existing
book-owned fixture as book-owned. Tests were staged separately on the Pi;
only the three application modules were installed into its live source tree.

## Live book and model evidence

After deployment, the worker published:

- `annotate` origin card `09c1dc0d-ea0b-4915-9e10-b152488dec17`, using English
  Affix Dictionary records and Open Multilingual Wordnet records.
- `musician` Word Card `50d32dd3-f8ab-46bd-9b9c-50cfab999725`, using English
  Affix Dictionary entry `p0664-t001-r003`.
- `yes` origin card `5234d5f5-7e04-4512-b453-cea57f974351`, published at
  `06:21:58Z` with Word Origins entry `entry-6974`.

The fresh `yes` inference job was
`job-742cdf20-26a1-5f3a-a8f3-ed53f77f516b`. It completed at `06:21:51Z`, and
its accepted artifact contains `independent-origin-paths-v1`. This proves the
new origin path ran in production; it does not prove the proposed ancestry is
linguistically correct. The initial draft took 297.79 seconds and the review
took another 221.28 seconds, using local Qwen exclusively.

## Next gaps, do not lose these

- The existing review logic requested that second inference solely because a
  redundant modern endpoint had already been removed by normalization. Avoid
  rerunning the model for a problem that deterministic normalization resolved;
  retain genuine structural and evidence diagnostics without discarding useful
  partial work. This is a measured throughput issue, not a hung model.
- The accepted `yes` path still treats a German `ja` form as an ancestry step.
  Distinguishing cognates, translations, and direct ancestry remains semantic
  work. A form occurring in a book does not attest a directional ancestry edge.
- No live run of the new structure-revisit policy was observed in this release.
  Its scheduling and spelling behavior passed integration tests. Existing
  cooldowns mean deployment does not immediately revisit every legacy word.
- The old false `valuable` path through `precious` was not demonstrated repaired.
- Compact evidence-panel headings and very small text in some compact graphs
  remain frontend gaps from the previous handoff.
- Language completion, fair growth across the corpus, and the earlier graph
  publication/export requests remain part of the active goal.

## Runtime and recovery records

Python: `3.11.2`. Model: `Qwen3-4B-Q4_K_M.gguf`. Runtime:
`llama.cpp-0.3.0`, previously reported as `0.3.0-dev`, upstream commit unavailable.
Context 3072, four threads, one inference slot. No model/runtime revision was
changed. The following identities were last checked on September 11, not
rehashed during this release:

```text
model SHA256: 7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5
llama-server SHA256: da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c
```

Pi staging and source rollback archive:
`/home/lachlan/LocalKnowledgeTerminal/runtime/lexical-refinement-20260913/`.
Windows backups, candidate hashes, test logs, and live evidence:
`C:\Users\Administrator\LKT-backups\20260913-lexical-refinement\`.

The deployed modules matched their staged SHA256 manifest. Previous live
modules matched the pre-edit baseline before deployment and are preserved in
`before-source.tar.gz`. No database rollback or destructive cleanup was done.
The Pi had approximately 702 MiB free after staging; avoid large on-device
database copies. Generated runtime evidence remains outside the repository.
