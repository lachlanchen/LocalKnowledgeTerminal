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
| Chat / Benchmark | Qwen3-4B | direct bounded conversation | no |

## Prepared enrichment points

Phoneme segmentation, Arabic grapheme segmentation, sentence grammar, linked
word investigation, and batch corpus preparation are separate sequential jobs.
Each saves reusable artifacts into the preparation ledger and enriches a card
revision without coupling retrieval, generation, or rendering.
