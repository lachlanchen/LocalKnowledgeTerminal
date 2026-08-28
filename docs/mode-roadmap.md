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
Phoneme segmentation, Arabic grapheme segmentation, sentence grammar, linked
word investigation, recursive origin expansion, and batch corpus preparation
are represented as separate sequential jobs. The bounded worker now executes
and checkpoints the first two jobs—combined book/dictionary retrieval and one
validated English meaning—without claiming later unsupported work. Each
remaining task gets its own handler and validator before promotion.
