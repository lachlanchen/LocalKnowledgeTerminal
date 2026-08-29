# Mode roadmap

LKT separates a user experience from its corpus adapter and retrieval policy.
That keeps new books additive instead of turning the service into a collection
of source-specific conditionals.

## Available now

| Experience | Corpus | Retrieval policy | Grounded |
| --- | --- | --- | --- |
| Word Origin | Word Origins + Root + Affix | origin-first exact/FTS, then morphology support | yes |
| Word Card | Word Origins + Root + Affix | multi-entry lexical FTS5 + exact morphology | yes |
| Book Answer | Paul-edition Answer cards | query-seeded reproducible draw | yes |
| Book Question | Question cards | multilingual FTS5, then seeded draw | yes |
| Root Graph | Root + Affix dictionaries | root-primary exact/FTS, then affix support | yes |
| Affix Graph | Affix + Root dictionaries | affix-primary exact/FTS, then root support | yes |
| Chat / Benchmark | Qwen3-8B / 4B | direct bounded conversation | no |

## Prepared enrichment points

The normalized knowledge schema and dependency-aware planner are implemented.
For words, the bounded worker executes and checkpoints retrieval, one validated
meaning, conservative morpheme splitting, bounded origin expansion, one
language and pronunciation at a time, grammar properties, and deterministic
Word Card/Origin composition. Phoneme segments, Arabic grapheme segments, and
the connected ancestry graph are stored independently and reused.

For accepted Answer and Question cards, exact reviewed English, Japanese, and
Chinese text is now acquired as evidence-linked content atoms; the migration is
idempotent across the accepted card-book collection. The worker now prepares
English, Japanese, and Chinese sentence grammar as three independent local-model
jobs. Its validator requires one to eight ordered phrases to reconstruct every
character of the reviewed text and caps model confidence before acceptance.
Each accepted analysis links back to retrieval-owned book evidence, records a
revision, supersedes its earlier accepted analysis, and is projected into the
shared card JSON for quiet grammar colors. Linked investigation terms remain a
separate reusable task. New translation and content-card composition handlers
remain future enrichment work—not claimed runtime capabilities.

## Future: LazyBook Reader

LazyBook Reader is a restrained sequential-reading mode, not a second product
or a new core pipeline. It will reuse retrieval, local inference, persistence,
and the shared card JSON to present one sourced book paragraph at a time.

- Retrieval owns the exact paragraph, stable entry identifier, book and section
  metadata, sequence position, locator, and citation. Model output never becomes
  the quotation or its source.
- Previous and next navigate paragraphs deterministically and may persist a
  local resume cursor. Long paragraphs become ordered inner slides rather than
  a scrolling page.
- Optional jobs run independently: organize the passage, polish a clearly
  labeled reading copy without replacing the original, and translate one
  language at a time. English, Japanese with ruby, and Chinese with pinyin use
  the same language presentation rules as existing book cards.
- Accepted transformations are validated, cached, revisioned, and backfilled
  only when missing. The browser renders them through the normal card contract;
  future e-ink and audio adapters can consume the identical JSON.
- The original paragraph and its citation remain accessible even when a compact
  organized, polished, or translated view is active.

Implementation should begin only after selecting a real sequential book corpus
and defining its paragraph record contract. Until then, Reader is recorded
product direction and is not claimed as an available tab.
