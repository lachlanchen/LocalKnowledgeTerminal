# Mode roadmap

LKT separates a user experience from its corpus adapter and retrieval policy.
That keeps new books additive instead of turning the service into a collection
of source-specific conditionals.

## Available now

| Experience | Corpus | Retrieval policy | Grounded |
| --- | --- | --- | --- |
| Word Origin | Word Origins | exact headword, then lexical FTS5 | yes |
| Word Card | Word Origins | multi-entry lexical FTS5 | yes |
| Book Answer | Paul-edition Answer cards | query-seeded reproducible draw | yes |
| Book Question | Question cards | multilingual FTS5, then seeded draw | yes |
| Chat / Benchmark | Qwen3-4B | direct bounded conversation | no |

## Prepared extension points

Future morphology material can add separate **Suffix**, **Affix**, and **Root**
experiences. Each source should provide stable record IDs, source text, location
metadata, and reviewed language fields when available. The adapter then returns
the existing `Evidence` contract; the card service and GUI can reuse the current
multilingual card schema, ruby renderer, deterministic citations, model client,
history, and future e-ink/audio outputs.

The forthcoming book’s exact title, author spelling, license, schema, and hash
will be recorded only after its prepared export is supplied and validated. No
empty buttons or fabricated attribution are shipped before then.
