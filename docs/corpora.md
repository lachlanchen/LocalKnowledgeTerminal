# Verified private corpus set

LKT indexes structured records, not page images or whole PDFs. The raw books
remain outside Git; only their stable identifiers, observed record counts, and
content hashes are documented here.

| Source | Records | Runtime index | SHA-256 of indexed JSONL |
| --- | ---: | --- | --- |
| Word Origins `entries.jsonl` | 6,994 | exact headword + lexical FTS5 | `b65a2845e649451a1f5d20013d150b4a7668afcb09e794756867fd843918adf5` |
| `book-of-answers-paul-card-book` multilingual items | 318 | deterministic query-seeded draw | `247f7b0e56e81703453f60462ff0bf73f6fe6b9c9feec014e5b5da670977e8f1` |
| `book-of-questions-stock-card-book` multilingual items | 291 | multilingual FTS5 + deterministic fallback | `f265ac47905cf6475027159da8821fb82e1857bee885c95d6127166e8ba73ff0` |

## Authority rules

- Word Origins excerpts and page numbers are the authority for etymology and
  historical claims.
- Answer and Question source text, reviewed translations, and Japanese ruby
  tokens are copied from the selected record after inference.
- Qwen may compose an explanation, memory hook, Chinese pinyin, or reflection.
  It cannot create or modify the citation payload.
- Answer draws are stable for a normalized user question. Question searches
  return the strongest lexical match; an unmatched theme produces a stable draw.

This keeps every displayed source passage traceable while allowing a future
embedding retriever, e-ink renderer, or audio renderer to reuse the same card
document.
