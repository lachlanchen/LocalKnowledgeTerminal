# Origin review efficiency, 2026-09-13

## Release status

Deployed `lkt/atomic.py` to the Pi at `2026-09-13T07:02:58Z`. The worker
finished its current job normally before stopping. The worker and web services
restarted; the local model process remained PID 1341. Networking, DHCP, model
weights, runtime binaries, database schema, and frontend assets were unchanged.

Post-deployment health confirmed a fresh running worker, generation readiness,
and the local Qwen model. This release is progress toward the active goal, not
a declaration that all data, visualization, or publication work is complete.

## Measured cause and change

The origin pipeline normalized a redundant modern endpoint out of a draft,
checked the remaining history successfully, then nevertheless requested another
model call because the endpoint had originally appeared. Over the preceding
24-hour observation window, 15 such reviews consumed 3,742.5 seconds, about
62 minutes. Two other recorded reviews concerned actual evidence checks.

The extra trigger was removed. Genuine structural and citation checks remain.
Normalization warnings and raw drafts remain auditable, and useful first-draft
history is not discarded if a reviewer returns a worse structure.

Accepted origin artifacts now include `origin_review` with policy
`normalize-before-review-v1`, the review reason, and an `inference_trace` of
draft/review stage calls and their original metrics. Each metric record can
itself report multiple HTTP requests from formatting recovery. Existing
top-level metrics remain compatible; use the trace for complete stage costs.

## Tests and replay

- Added eight tests in `tests/test_origin_normalization_efficiency.py` for
  single-call cleanup, identity repair, uncertain metadata, citation ownership,
  genuine structural review, retaining useful partial output, and book-only
  zero-model-call behavior.
- The first full run had one legacy failure: a `lecher` fixture expected an
  unnecessary reviewer to add richer history. Its expectation in
  `tests/test_atomic.py` now requires a useful one-call first publication and
  growth from one to three book-backed forms on a later ordinary visit.
- Final full local run: 578 tests passed in 363.973 seconds; compilation passed.
- Final focused Pi run: 47 tests passed in 7.807 seconds. Candidate compilation
  also passed. The updated test fixture was staged separately from live code.
- Replayed the recorded first `yes` model draft against its five actual
  retrieved records in an isolated temporary database. It succeeded in one
  worker model call, retaining its three historical forms. The live database
  was read-only; this replay made zero fresh model requests.

The replay source job is `job-742cdf20-26a1-5f3a-a8f3-ed53f77f516b`.
It demonstrates that the real redundant retry is unnecessary, not that every
retained linguistic hypothesis is correct. A fresh production origin inference
under the new review policy was not yet observed in this release window.

## Live book card

After deployment, the worker generated `counterfeiter` Word Card
`07afcfc8-ea0b-4239-8309-b60c696f7c3d` at `07:03:38Z`. Its HTTP endpoint
returned 200 with nonempty English, Japanese, Chinese, French, and Arabic
terms and meanings. Its three evidence records include root dictionary entry
`p0113-t046-r003`, affix dictionary entry `p0467-t001-r005`, and an Open
Multilingual Wordnet record. This is publication from accepted knowledge;
do not describe it as five new translation inference requests.

## Next bottleneck and remaining scope

The automatic history job
`job-551b2494-c971-5210-bab4-dea151b4e88e` completed an `unequalled`
reanalysis at `07:02:57Z` with `surface-canonical-v1`. This supplies the live
structure-policy evidence missing from the previous handoff. Its quality and
cost remain inadequate:

- The initial model stage took 711.88 seconds, including two requests and a
  formatting retry. Prompt compaction removed two items and 2,526 characters.
- A further morphology review took 192.19 seconds and one request.
- Both raw drafts already represented the whole word as a single free part;
  the reviewer did not collapse an initially good split.
- Both returned a list-valued meaning, later normalized to a plain phrase.
  The review additionally proposed the synonym `unique` as a derivative.
- Inspect morphology prompt size, normalization-before-review behavior, and
  derivative-versus-synonym typing generally. The current morphology review
  artifact does not record its trigger, unlike the new origin trace.

At the health snapshot, the database contained 1,868 morphemes, 653 historical
forms, 20,001 entity edges, and 339 completed morphology enrichment clusters.
Translation records remained at 1,196. Counts alone do not prove semantic
correctness or fair progress across languages and graph regions.

The active goal still includes semantic correction, useful decomposition,
language completion, fair continuous growth, the remaining compact-layout
issues, and the earlier graph publication/export scope. Do not lose the
`valuable` false ancestry and `yes` cognate-versus-ancestor examples from the
previous handoff; neither was demonstrated corrected here.

## Runtime and preserved evidence

Runtime identity is unchanged from `lexical-refinement-release-2026-09-13.md`:
Python 3.11.2, Qwen3-4B-Q4_K_M, llama.cpp 0.3.0-dev, context 3072, four threads,
one inference slot. Previously recorded model/runtime SHA256 identities were
not rehashed in this release. Application source hashes matched the staged
manifest before installation.

Pi staging, rollback source, tests, and live evidence:
`/home/lachlan/LocalKnowledgeTerminal/runtime/origin-review-20260913/`.
Windows source backups, candidate bundle, initial failure log, final passing
logs, replay, and runtime observations:
`C:\Users\Administrator\LKT-backups\20260913-origin-review\`.

Generated evidence stays outside the repository. No database rollback,
destructive cleanup, Git operation, or forced process termination was performed.
