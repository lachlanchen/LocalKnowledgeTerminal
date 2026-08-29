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

## Autonomous reviewed-book deck

The installed atomic worker owns one additional idle-time action. When no
claimable preparation job exists, it compares accepted-source coverage for the
Answer and Question books and selects one unseen record from the less-complete
mode. A seeded circular scan makes every record reachable; the stable book
entry ID in each accepted card prevents repetition across service restarts.

Retrieval chooses and owns that exact record. Local Qwen produces only the
bounded card title/reflection, after which normal composition restores the
reviewed English, Japanese, and Chinese text from evidence. Publication runs
through the same clean-Unicode, provenance, and card-schema gate as an
interactive request. Accepted text is acquired into normalized knowledge and
one exact-source vocabulary investigation is queued. Failed drafts remain
non-visible and the source stays eligible for a later retry.

Only one record is attempted per idle interval. Current Pi undervoltage,
throttling, or a temperature of 78 C or more returns a `paused` result instead
of calling the model; preparation resumes automatically after recovery.
Historical throttle flags do not block healthy work. Word Card and graph modes
remain demand-driven because their source collections contain thousands of
records, but every requested pipeline stage is likewise local and resumable.

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

`retrieve-evidence` combines the private Word Origins/Root/Affix indexes with
compact OMW senses and exact FreeDict English-Arabic candidates, then
`prepare-meaning` asks the active local model for one short English sense. The
worker accepts only supplied evidence IDs, bounded clean text, a controlled part
of speech, and confidence at or above the acceptance threshold. Every currently
scheduled word job has its own validator and accepted checkpoint; no later stage
can publish merely because an earlier model call completed.

`prepare-translation` works on one target language and one accepted sense. It
constrains the result to an aligned OMW candidate when one exists. For Arabic,
an exact English headword may additionally retrieve FreeDict candidates when
OMW lacks a lemma; Qwen must select one verbatim against the accepted English
sense, and the chosen dictionary evidence ID is attached only after validation.
The handler validates target script, requires readings for Japanese, Chinese,
and Arabic, derives Chinese pinyin locally, and stores a target term,
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

`plan-evidence` refreshes only the raw retrieval checkpoint after polished source
data or lexical filtering changes. The latest raw candidate supersedes—not
deletes—the older candidate, so downstream work consumes one current evidence
set while the provenance ledger still explains earlier decisions.

`split-morphemes` asks the local model for one conservative surface-covering
split, then enforces exact letter coverage, controlled kinds/languages, a root,
short meanings, and confidence thresholds. Each proposed component is looked up
again in the polished component book. Direct component evidence upgrades that
part to `book`; unsupported but plausible structure is capped as `model`. The
three parts of `inspection`, for example, are stored independently rather than
flattened into a prose origin story.

A decomposition that fails a later factual audit is quarantined rather than
edited in place: its artifact becomes rejected, term-component edges and
unshared morphemes become archived, and a rejected revision records the reason.
`plan-morphemes` can then enqueue only the corrected split against the current
evidence and meaning checkpoints.

The raw structured morphology draft is checkpointed as a non-publishable
candidate before validation. A failed retry therefore remains inspectable, but
cannot create terms, edges, graph nodes, or cards.

An exact root found in the reviewed root dictionary becomes a surface anchor.
The model fills small reusable properties around that fixed split; it cannot
merge, shorten, or relabel the cited root. Deep historical alternations remain a
separate recursive origin task instead of distorting the visible word structure.
Prefix/suffix hyphen direction and plain-phrase punctuation are deterministic
display normalizations and are recorded on the accepted atom.

`expand-origin-branches` starts only after a split is accepted. It retrieves
Word Origins evidence independently for each fixed component, prepares one
cited root branch per model call with at most three backwards steps, checkpoints
the raw graph draft, and then
stores each accepted historical form as its own entity. Historical edges point
from older form to newer form or component. Only evidence attached to that
exact component can make a historical node `book`; uncited model knowledge is
capped at 0.75. At least one root history is required, and a malformed branch
cannot publish an Origin card.
An explicit “Latin X … descendant of Indo-European Y” book sentence is parsed
deterministically before model fallback, avoiding a slow inference call for a
chain already stated directly by the source. `plan-origin` retries only this
stage after a validator or extractor revision.

`compose-origin-card` is also deterministic. It projects the accepted modern
sense, fixed morphemes, and historical branches into one connected graph, then
adds a whole-origin view, a word-parts view, and a focused root-history slide.
It reuses accepted Japanese/Chinese ruby plus French/Arabic atoms and passes the
same publication gate as every other visible card. `plan-origin-card` can rerun
this projection without retrieval or inference.
The full-screen graph renders relationships as quiet directed arrows; verbose
edge names stay in card JSON for future inspection instead of overlapping the
large teaching nodes. Origin-source evidence is ordered before modern lexical
evidence in the visible citation panel.

Every accepted Answer and Question card immediately acquires its exact reviewed
English, Japanese, and Chinese text as three idempotent `content_items`. All
three atoms link to the retrieval-owned card-book evidence, while typed
`reviewed-translation` edges preserve their relationship. Model reflection is
not copied into book evidence. `sync-card-knowledge` safely backfills cards that
predate this acquisition path and the LadybugDB projection remains rebuildable
from the resulting SQLite atoms.

Content enrichment follows the same rule: sentence grammar and investigation
candidates are separate tasks. `prepare-grammar-parts` makes one bounded Qwen
call for one reviewed language. The validator aligns the proposed phrases back
to the original string, rejects omissions or rewrites, links the accepted
analysis to its source evidence, conservatively normalizes incompatible
role/part-of-speech pairs while preserving the original model label, and records
the model/prompt revision. English,
Japanese, and Chinese therefore fail and retry independently. The browser reads
the accepted analysis from the shared card JSON and applies restrained role
colors directly to the existing sentence/ruby carousel; it does not create a
second presentation-only grammar store. A later selected-word action can start
a child inquiry while retaining its source content item and card.

Model Lab already uses that lineage boundary for conversation history. The
first successful turn creates an `inquiry_thread`; each later turn records its
parent event. A discussion launched from an accepted Answer or Question card
stores both the card ID and normalized English source `content_item`. The raw
Qwen observation, timing metrics, and uncited response remain in the separate
observation ledger and are never promoted into book evidence.

## Compact lexical correction

The local model remains the writer. Dictionary retrieval supplies small factual
corrections rather than a large alternative language system:

- Open Multilingual Wordnet 2.0 aligns senses across English, Japanese,
  Mandarin Chinese, French, and Arabic through Wn.
- FreeDict English-Arabic 0.6.3 supplies exact-headword Arabic candidates where
  OMW has no aligned Arabic lemma. Its TEI is temporary; only a compact local
  SQLite index and its source/license metadata remain at runtime.
- JMdict may correct Japanese forms/readings.
- CC-CEDICT may correct Chinese form/pinyin/gloss.

Each retrieved dictionary item remains source/version/license evidence. Full
Wiktionary dumps are out of scope unless later measurements show a real gap.
