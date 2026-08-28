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
  resumable sequential work with immediate checkpoints. Artifacts explicitly
  distinguish raw candidates, accepted facts, rejected output, superseded
  revisions, and migrated legacy material; accepted artifacts also retain a
  bounded quality score.
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

The first executable slice is deliberately narrow: `retrieve-evidence` combines
the private Word Origins/Root/Affix indexes with compact OMW senses, then
`prepare-meaning` asks the active local model for one short English sense. The
worker accepts only supplied evidence IDs, bounded clean text, a controlled part
of speech, and confidence at or above the acceptance threshold. Other queued
job types remain untouched until their own validators are implemented.

`prepare-translation` is the next independent handler. It works on one target
language and one accepted sense, constrains the result to an OMW candidate when
one exists, validates the target script, requires readings for Japanese,
Chinese, and Arabic, derives Chinese pinyin locally, and stores a target term,
translation atom, evidence links, revision, and checkpoint separately.
French readings are normalized away, optional usage notes pass a restrained
English-only gate, and the exact redundant `word or same-word` Arabic pattern
is collapsed deterministically and recorded; broader repetition still fails.
`plan-translation` can revisit one weak language atom with a new prompt version
without rerunning the whole word pipeline; its accepted replacement supersedes
the earlier preparation artifact.

`prepare-pronunciation` does not spend an LLM call. Japanese reuses its accepted
kana reading; Chinese pinyin and character ruby are derived deterministically;
and the small offline eSpeak NG engine provides versioned IPA for English,
French, and Arabic. Partial Arabic vowel marks are removed before phonemization
because a partly marked word is less reliable than the engine's unmarked lexical
lookup. The visible Arabic spelling is preserved, and this normalization remains
in provenance. Initial IPA is stored as one grapheme-aligned segment; finer
phoneme coloring is a later independently validated refinement.

`prepare-grammar-properties` reuses the accepted sense's controlled part of
speech and writes one evidence-linked word analysis without another model call.
This completes the factual prerequisites for a Word Card independently of
etymology. The publication boundary therefore requires a graph for Word Origin
(internal mode `word`), Root, and Affix views, but never forces that graph into
the focused Word Card (internal mode `knowledge`).

`compose-word-card` is deterministic assembly, not another generation pass. It
requires accepted meaning, grammar, four translations, and five pronunciation
artifacts; reconstructs stable evidence records; and saves a candidate through
the ordinary card publication gate. Only after that gate passes does the card
enter the Word Card carousel. A later accepted composition for the same term
supersedes the earlier card while preserving its ledger row and atomic inputs.

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
