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
