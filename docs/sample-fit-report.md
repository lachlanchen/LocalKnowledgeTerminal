# Sample collection-fit report

This is a concrete example of the report format used by the **USD 250 Local
Knowledge Terminal collection-fit sprint**. It applies the format to LKT's own
reference collection using facts already recorded in this repository. It is
not a customer result, testimonial, benchmark, sale, or claim that the
reference collection was produced through a paid sprint.

Reference snapshot: **2026-08-31**.

## Decision summary

**GO, with a bounded scope:** the existing structured records fit LKT's local
exact/lexical retrieval and cited-card workflow on the current Raspberry Pi 5
8 GB target. The useful first proof is a browser card backed by one retrieved
record and its stored provenance.

**Do not expand the same scope** to image-only PDFs, custom OCR, unreviewed
rights, hardware supply, or a production deployment. Those require a separate
decision after this fit report.

## 1. Collection and data map

The current-code reference snapshot contains **16,800 structured records** across five
collection exports. Raw books and editorial workspaces remain outside Git.

| Collection export | Records | Runtime treatment |
| --- | ---: | --- |
| Word Origins `entries.jsonl` | 6,994 | exact headword and lexical FTS5 |
| Book of Answers multilingual items | 318 | deterministic query-seeded draw |
| Book of Questions multilingual items | 291 | multilingual FTS5 and deterministic fallback |
| English Root Dictionary editorial JSONL | 4,018 | exact headword and morphology FTS5 |
| English Affix Dictionary editorial JSONL | 5,179 | exact headword and morphology FTS5 |

The indexed JSONL hashes and source-specific authority rules are recorded in
[`corpora.md`](corpora.md), including the index-only rows excluded by the current
morphology parser. Full PDFs, TeX, images, model weights, generated indexes, and
editorial queues do not belong in the public repository.

Compact correction sources are separate from the private collection. The
current evidence ledger records Open Multilingual Wordnet, pinned FreeDict
English-Arabic, and pinned full JMdict. JMdict contributes 327,737 exact
form-reading rows in an approximately 66 MB local database. CC-CEDICT remains
listed as a future source rather than being implied as installed.

## 2. Privacy and citation map

```text
owned source files
  -> reviewed structured export
  -> local SQLite exact/FTS index
  -> retrieved record with source ID, page/excerpt, and indexed-file hash
  -> bounded local-model composition
  -> normalized card JSON
  -> browser display
```

- The source files and indexes remain local.
- Retrieval supplies citation facts; the model cannot invent or modify the
  citation payload.
- Accepted cards retain the corpus ID, record ID, source title, excerpt/page
  when present, and indexed-file hash.
- Generated context is labelled as model context unless it is tied to an exact
  retrieved record.
- Browser, e-paper, and audio are output adapters; the knowledge core does not
  depend on one display device.

## 3. Representative browser proof

The repository contains working browser software and two reviewable interface
captures from the same card contract:

- [multilingual Word Card](assets/word-card.png);
- [cited Word Origin graph](assets/word-origin.png).

For a real sprint, the proof would use a small customer-approved sample only
after source rights, format, privacy constraints, intended readers, language
goal, and existing hardware pass the free fit check. The proof is not a bulk
ingestion, production deployment, or promise that every source format works.

## 4. Go/no-go criteria

### Go when

- the customer controls the source and has the right to use it;
- the collection is bounded and a representative sample can be reviewed;
- text is already extractable or structured;
- the intended output is a small cited browser proof on an existing machine;
- one language goal and intended reader group can be stated clearly.

### No-go or separate scope when

- source rights are unclear;
- the material is mainly scanned images and requires custom OCR;
- the request is an unbounded migration or production knowledge platform;
- shipped hardware, an uptime commitment, or a production SLA is required;
- the sample cannot be handled without copying confidential material into an
  unapproved environment.

## 5. Reproduce the public checks

From a clean checkout with the project dependencies installed:

```bash
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

The tests cover the separations that matter to this decision: corpus indexing,
retrieval, provenance, card storage, preparation, model boundaries, outputs,
and the browser service. Passing tests demonstrate the public implementation;
they do not substitute for checking a customer's actual sample.

## What the paid sprint would deliver

For one accepted customer collection, the sprint delivers the same three
bounded artifacts shown here:

1. a written data, citation, and privacy map;
2. evaluation of an agreed sample capped at 12 source units and 20 test
   questions, with up to two cited browser cards when the material is usable;
3. a written go/no-go decision and the boundary of any larger engagement.

One factual correction pass is included. Hardware, shipping, custom OCR, bulk
conversion, production deployment, and ongoing support are excluded. Start
with the [free collection fit check](https://lazying.art/lkt/fit-check/); the
page stores and sends nothing automatically.
