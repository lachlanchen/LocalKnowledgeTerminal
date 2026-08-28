# Established knowledge and graph architecture

Cards are product views, not the canonical knowledge store. LKT separates three
local data responsibilities:

1. Source indexes retain immutable book/dictionary records for retrieval.
2. `knowledge.sqlite3` is the authoritative transactional store for accepted
   atomic knowledge and preparation history.
3. `knowledge-graph.lbdb` is a LadybugDB traversal projection generated only
   from accepted SQLite entities and edges.

The graph database is never an independent source of truth. Every projected
node and edge carries its SQLite ID. A projection has a deterministic source
fingerprint and can be replaced or rebuilt without model inference.

## Publication boundary

Persistence and publication are separate operations. A composed card is saved
as a candidate first. It enters a tab or carousel only after the publication
gate confirms its mode, stable RAG evidence IDs, grounded state, required
language fields, complete ruby coverage, graph integrity where applicable, and
clean Unicode text. Legacy rows migrate to `legacy-unreviewed`, so old payloads
cannot silently become visible. Rejected and legacy candidates retain a small
audit trail and may be quarantined as archived; accepted replacements are built
from current atomic knowledge and current source evidence.

## Atomic SQLite layers

- `entities` provides canonical IDs, validation status, quality, and typed
  payloads.
- `terms`, `meanings`, `morphemes`, `term_morphemes`, `historical_forms`, and
  `history_events` keep modern form, sense, morphology, etymology, attestation,
  and semantic change separable.
- `pronunciations` and `phoneme_segments` preserve IPA/kana/pinyin/
  transliteration systems and grapheme alignment without flattening them into a
  display string.
- `translations` stores one language/sense result at a time. English, Japanese,
  Chinese, French, and Arabic artifacts can be revised independently.
- `grammar_analyses` and `grammar_parts` preserve ordered sentence/word roles,
  lemmas, parts of speech, readings, and display color keys.
- `entity_edges` stores accepted directed relationships; LadybugDB mirrors this
  table for recursive traversal and focused graph views.
- `evidence_records` and `entity_evidence` attach immutable corpus IDs, hashes,
  locators, excerpts, claims, and confidence to the exact supported atom.
- `entity_revisions` retains model/prompt version and review lineage.
- `preparation_jobs`, `job_dependencies`, and `job_artifacts` implement
  resumable sequential work with immediate checkpoints.
- `inquiry_threads` and `inquiry_events` preserve parent/child investigation
  history, selected words, source/result entities, and compact summaries.

## Divide-and-conquer preparation

A word is prepared as small dependency-aware tasks:

```text
retrieve evidence
      |
prepare one meaning
   /       \
split      one translation per language
parts             |
   |        one pronunciation per language
expand each       |
origin branch     +---- grammar/properties
   |                     |
compose origin       compose word card
```

Origin expansion walks backward one accepted step at a time. Each component and
historical parent is retrieved, normalized, cited, validated, checkpointed, and
deduplicated before another parent is queued. Shared roots and forms converge on
canonical IDs. Cycles are rejected before publication.

Answer and Question preparation follows the same rule: source text, each
language, grammar parts, and investigation candidates are separate tasks. A
meaningful selected word can start a child investigation while retaining the
source card/event relationship.

## Compact lexical correction

The local model remains the writer. Dictionary retrieval supplies small factual
corrections rather than a large alternative language system:

- Open Multilingual Wordnet 2.0 aligns senses across English, Japanese,
  Mandarin Chinese, French, and Arabic through Wn.
- JMdict may correct Japanese forms/readings.
- CC-CEDICT may correct Chinese form/pinyin/gloss.

Each retrieved dictionary item remains source/version/license evidence. Full
Wiktionary dumps are out of scope unless later measurements show a real gap.
