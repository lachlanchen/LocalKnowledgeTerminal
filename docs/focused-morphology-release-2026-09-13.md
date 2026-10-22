# Focused morphology inference and live growth audit

## Deployed release

Deployed on the Pi at `2026-09-13T08:18:06Z`:

- `lkt/morpheme_prompting.py`
- `lkt/atomic.py`
- `lkt/lexical_refinement.py`

Policies: `focused-morphology-v1` and `surface-canonical-v2`.

The local model now receives bounded, balanced dictionary/book excerpts,
boundary-prefix and boundary-suffix lookup candidates, root candidates, and a
small rotated list of known neighbours rather than the full lexical graph.
Initial and review prompts both include the output schema. Derivative requests
are small batches, not a lifetime cap. A structurally worse review no longer
replaces a usable first draft. Raw retrieval candidates do not establish book
role authority. Accepted spelling-adjusted canonical forms are preserved.

Before deployment: all 594 tests and compilation passed locally. The 63 focused
Pi tests and compilation also passed. A real Pi dictionary lookup for
`unequalled` retrieved the actual UN, ED, and EQU entries and produced a
4,161-character prompt. That probe did not invoke the model.

## Runtime and restart outcome

The old worker did not stop within its 15-minute service grace. Systemd killed
PID 284700 at 08:18:06Z; startup recovered one interrupted job. Do not describe
this as a clean graceful exit. No manual force-kill was issued.

After installation: worker PID 286583, web PID 286578. Local Qwen remained PID
1341, unchanged since September 3. Health reported ready; all book indexes and
the local model were available. Networking, DHCP, model configuration, and
frontend assets were not changed in this release.

Runtime identity:

- Python 3.11.2.
- `Qwen3-4B-Q4_K_M.gguf`, local llama.cpp, context 3072, four threads.
- Runtime build directory: `llama.cpp-0.3.0`; reported version `0.3.0-dev`.
- Model SHA-256, last checked September 11:
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- Server SHA-256, last checked September 11:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.
- Upstream runtime commit was unavailable. No newer hash check is implied.

## Verified live publication and growth

The normal worker republished the `musician` Word Card at 08:18:46Z from accepted
knowledge and the actual English Affix Dictionary entry `p0664-t001-r003`.
Card `52e04a33-419c-4cfa-a82c-476af6f59cd0` returned HTTP 200 through
`/api/cards/52e04a33-419c-4cfa-a82c-476af6f59cd0`. Its Japanese and Chinese terms
and meanings were populated. This was not a fresh inference for every field.

A new local-Qwen French translation completed at 08:19:41Z in 52.72 seconds,
one request, taking the translation count from 1196 to 1197. Its `frail-looking`
translation used `frail` as the French term, which remains a semantic quality
concern. New rows alone are not proof of correct knowledge.

The root and affix card counts remained 92 and 143 during the first observation
window. Existing root projections were refreshed. Since midnight UTC the audit
found 22 newly created accepted has-component assertions, nine derived-from
assertions, and 57 developed-into assertions. These are database timestamps,
not a claim that every assertion is linguistically correct or newly visible.

At 08:31:19Z another normal `enrich-morphology-v3` job completed with two claims,
but its raw model output copied complete words into root fields and proposed
`originality` derived from `originate`. The input included a parser observation
misreading the Chinese definition of a verb as source-language evidence.
This separate enrichment path still needs better input and task framing.

## Exact live validation handles

The existing `unequalled` subject was queued through the normal worker under
`focused-morphology-v1-live-validation-20260913`:

- Retrieve: `job-89a6e1b4-fd23-5339-827f-e95b4da7f799`.
- Split: `job-62f91243-915d-5840-aae3-bbd6ea2474c0`.
- Word Card: `job-5314b162-3209-5e37-a893-0626abd61817`.
- Origin: `job-b30274a5-4457-50e8-a6f4-16adb3fade23`.
- Origin Card: `job-e2b02e96-7b89-5c0a-8dc1-da99dcbdde32`.

These remained queued at the end of the first ten-minute observation. Do not
enqueue duplicates or restart services because an observation window expired.

The normal worker independently started the new prompt policy for `surplus`
at 08:31:37Z, job `job-53d9b252-cf47-52dc-974d-297b0a626ebf`. Its saved
`morpheme-prompt-context` proves the new input path was used. A completed
inference and its semantic quality must be checked separately.

## Follow-up candidate: honest book formula authority

Inspection of the real `surplus` prompt found a general parser bug: a printed
partial formula was padded with leftover letters labelled as a mandatory
suffix. The model was required to reproduce that invented segmentation.

The local follow-up candidate in `lkt/atomic.py` only freezes surface boundaries
when the printed formula covers the entire requested word. Partial formulas
remain in RAG. Lookup-request kinds no longer force neighbouring pieces into
prefix/suffix roles. Genuine typed corpus observations remain handled by the
separate provenance layer. Regression coverage is in
`tests/test_book_formula_authority.py`.

This candidate is separate from the 08:18:06Z deployment. Do not assume it is
installed merely because this document exists. Its test/deployment results
must be recorded in the continuation handoff.

## Artifacts and remaining work

Pi runtime artifacts:
`/home/lachlan/LocalKnowledgeTerminal/runtime/focused-morphology-20260913/`.
Windows backup:
`C:\Users\Administrator\LKT-backups\20260913-focused-morphology\`.

Keep the overall goal active. Remaining work includes the exact live morphology
result, unsupported ancestry repair, enrichment input quality, missing or poor
translations, useful sustained root/affix growth, small-screen readability, and
the earlier graph/database publication requests. Passing structural tests does
not establish that all linguistic content is correct.

## Continuation evidence and regression contract

The deployed prompt completed the real `surplus` split at 08:36:25Z in one
286.77-second request, without a formatting retry or context truncation. The
normal worker published its origin card at 08:41:45Z:
`ca3e794b-6bb0-4e90-a262-01de28bb8c03`. That card and its three root/affix
projections returned HTTP 200. By 08:47:37Z the active root count was 93 and the
affix count 145, versus 92 and 143 before this release. The accepted split still
contained the parser-invented suffix, so this proves execution and growth, not
fully correct morphology.

The formula-authority candidate passed 25 focused tests on the Pi. A read-only
probe against the actual saved book records for `surplus`, `unequalled`,
`autopsy`, and `constrain` retained their excerpts without forcing unprinted
parts. The probe made no model requests or live database changes.

The first full follow-up suite exposed three obsolete test assumptions: a
partial formula was expected to supply an unprinted suffix; a lookup hint was
expected to determine component roles; and a fake model prohibited the review
needed to classify those unknown roles. The fixtures now use an explicitly
complete synthetic formula where full authority is intended, leave raw lookup
roles unknown, and let the fake model classify a complete formula. The existing
assertions for eight derivative proposals, model-only provenance, superseded
claims, unchanged prior cards, and graph idempotency remain in place. Full
validation is rerun against these updated expectations before deployment.

Another real run, `constrain`, exposed a remaining output-normalization gap:
both draft and review supplied useful `con` and `strain` parts but also included
the entire word as a redundant free part. Two requests spent 730.70 seconds
without repairing the resulting duplicate coverage. A general normalization
that preserves an otherwise complete valid split is still needed; it must not
be replaced with hardcoded word-specific decompositions.

The runtime artifacts also record restoration of a diagnostic side effect:
`morphology_enrichment_context` had reopened a completed enrichment lease merely
when inspected. Only that cluster/epoch completion state was restored; its
accepted output and cooldown were unchanged. Further inspection uses read-only
SQL. The accessor itself still needs a state-transition guard.
