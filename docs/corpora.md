# Verified private corpus set

LKT indexes structured records, not page images or whole PDFs. The raw books
remain outside Git; only their stable identifiers, observed record counts, and
content hashes are documented here.

| Source | Records | Runtime index | SHA-256 of indexed JSONL |
| --- | ---: | --- | --- |
| Word Origins `entries.jsonl` | 6,994 | exact headword + lexical FTS5 | `b65a2845e649451a1f5d20013d150b4a7668afcb09e794756867fd843918adf5` |
| `book-of-answers-paul-card-book` multilingual items | 318 | deterministic query-seeded draw | `247f7b0e56e81703453f60462ff0bf73f6fe6b9c9feec014e5b5da670977e8f1` |
| `book-of-questions-stock-card-book` multilingual items | 291 | multilingual FTS5 + deterministic fallback | `f265ac47905cf6475027159da8821fb82e1857bee885c95d6127166e8ba73ff0` |
| New Oriental English Root Dictionary `entries-polished.jsonl` | 6,327 | exact headword + morphology FTS5 | `102385447293126471ba47015a235c307f15d4473399c14203972738efe07d8f` |
| English Affix Dictionary `entries-polished.jsonl` | 5,189 | exact headword + morphology FTS5 | `fa3a395a010281055676280f44220ebf1ad244d999393b95decedd5880cee68a` |

## Compact public correction sources

| Source | Purpose | License / version policy |
| --- | --- | --- |
| Open Multilingual Wordnet | sense-aligned English/Japanese/Chinese/French/Arabic meaning correction | install exact OMW 2.0 lexicons through Wn 1.1.1; retain each included lexicon's metadata |
| JMdict | Japanese form, reading, and gloss correction | EDRDG CC BY-SA 4.0; record daily-build hash when added |
| CC-CEDICT | Chinese form, pinyin, and gloss correction | CC BY-SA 4.0; record release hash when added |

The local model remains responsible for concise explanation and composition.
These resources provide a few retrieved correction facts, not full generated
card content. Full Wiktionary dumps are intentionally excluded.

## Authority rules

- Word Origins excerpts and page numbers are the authority for etymology and
  historical claims.
- Answer and Question source text, reviewed translations, and Japanese ruby
  tokens are copied from the selected record after inference.
- Qwen may compose an explanation, memory hook, Chinese pinyin, or reflection.
  It cannot create or modify the citation payload.
- Answer draws are stable for a normalized user question. Question searches
  return the strongest lexical match; an unmatched theme produces a stable draw.
- Root, Affix, Word Origin, and Word Card may retrieve across all three lexical
  books. Every evidence object retains its own corpus ID, source title, record
  ID, page, excerpt, and indexed-file hash.
- A generated graph node is labelled `book` only when it carries an exact
  retrieved record ID. Unsupported or incorrectly cited nodes are normalized to
  `model` context before storage.

This keeps every displayed source passage traceable while allowing a future
embedding retriever, e-ink renderer, or audio renderer to reuse the same card
document.
