# Verified private corpus set

LKT indexes structured records, not page images or whole PDFs. The raw books
remain outside Git; only their stable identifiers, observed record counts, and
content hashes are documented here.

| Source | Records | Runtime index | SHA-256 of indexed JSONL |
| --- | ---: | --- | --- |
| Word Origins `entries.jsonl` | 6,994 | exact headword + lexical FTS5 | `b65a2845e649451a1f5d20013d150b4a7668afcb09e794756867fd843918adf5` |
| `book-of-answers-paul-card-book` multilingual items | 318 | deterministic query-seeded draw | `247f7b0e56e81703453f60462ff0bf73f6fe6b9c9feec014e5b5da670977e8f1` |
| `book-of-questions-stock-card-book` multilingual items | 291 | multilingual FTS5 + deterministic fallback | `f265ac47905cf6475027159da8821fb82e1857bee885c95d6127166e8ba73ff0` |
| New Oriental English Root Dictionary editorial `entries-editorial.jsonl` | 6,327 | exact headword + morphology FTS5 | `c5014c0d820a03bc16d0e88a5b583053c08e4c99c76665426d14ea66fb3135c5` |
| English Affix Dictionary editorial `entries-editorial.jsonl` | 5,189 | exact headword + morphology FTS5 | `a0d419c7c1a053313413cf448299f32d8adc6d0efbfef9257e6b2d481053b574` |

The two editorial sources were promoted only after their Nutstore validation
reports recorded canonical content equality, zero ungrounded text, and a passed
layout/content gate. Runtime deployment copies only these JSONL files and builds
5.8 MB of SQLite indexes; PDFs, TeX, images, and editorial work queues stay off
the Pi. The pre-editorial SQLite pair is retained as a rollback snapshot.

## Compact public correction sources

| Source | Purpose | License / version policy |
| --- | --- | --- |
| Open Multilingual Wordnet | sense-aligned English/Japanese/Chinese/French/Arabic meaning correction | install exact OMW 2.0 lexicons through Wn 1.1.1; retain each included lexicon's metadata |
| FreeDict English-Arabic | exact-headword Arabic candidates when OMW has no aligned Arabic lemma | `eng-ara` 0.6.3, conservatively `GPL-2.0-only`, source revision `5bdceeac8d0dba3298c1bebe734f60d54dad30f7`, SHA-256 `7572d3685c501975cd0d47b0dfb581b053b28fb18932d06f09d64d0479b06746` |
| JMdict full English JSON | exact Japanese form, reading, gloss, and part-of-speech correction | `3.6.2+20260824122934`, dictionary date `2026-08-24`; archive SHA-256 `d9b74539bce7df82491a57ad96a0634a988129db6ca4a362f7221bc5e736871f`; extracted JSON SHA-256 `5f3c8fc399411e49b45702a99bf99ce44b160c3e42345fb5df72629d7d1aeb87`; EDRDG JMdict / CC BY-SA 4.0 distribution |
| CC-CEDICT | Chinese form, pinyin, and gloss correction | CC BY-SA 4.0; record release hash when added |

The local model remains responsible for concise explanation and composition.
These resources provide retrieved correction facts, not full generated card
content. The FreeDict TEI and pinned JMdict archive are downloaded to temporary
directories during indexing; only their compact SQLite indexes remain on the
Pi. The full rather than common-only JMdict set produces 327,737 exact
form-reading rows, including uncommon lexical material, in a roughly 66 MB
runtime database. Full Wiktionary dumps are intentionally excluded.

## Public redistribution status

This is a conservative operational record, not a legal conclusion. A
metadata-only citation records provenance; it does not grant permission to
redistribute a source's lexical text, excerpts, or derived records. Pin and
review the actual deployed artifact before treating any row as release-ready.

| Source | Official primary license/source | Conservative identifier and public condition | Artifact/version status |
| --- | --- | --- | --- |
| FreeDict English-Arabic (`eng-ara`) | [0.6.3 archive](https://sourceforge.net/projects/freedict/files/eng-ara/0.6.3/freedict-eng-ara-0.6.3.src.tar.xz/download), upstream [`COPYING`](https://raw.githubusercontent.com/freedict/fd-dictionaries/master/eng-ara/COPYING) | The package says its Arabic Wordlist is GPL version 2. Record `GPL-2.0-only`; preserve notices and GPL terms. Do not place lexical or normalized derived content in a non-GPL public graph without a separate review. | The 0.6.3 archive-to-current-upstream correspondence and the pinned revision relationship remain unverified. |
| JMdict / EDRDG | [EDRDG licence statement](https://www.edrdg.org/edrdg/licence.html) | `CC-BY-SA-4.0` for the Japanese and English components. Attribute EDRDG, provide the required licence/documentation links, and apply the stated share-alike conditions to material based on significant extracts. | EDRDG states non-English translational equivalents, including French and German, are separately copyrighted. Those glosses are unverified unless a component-specific grant is recorded. |
| OMW English / Princeton WordNet | [OMW 2.0 release](https://github.com/omwn/omw-data/releases/tag/v2.0), OMW [package index](https://raw.githubusercontent.com/omwn/omw-data/main/index.toml), [Princeton WordNet licence](https://wordnet.princeton.edu/license-and-commercial-use) | OMW's English WordNet 3.0 bundle uses the custom Princeton WordNet 3.0 terms; record `LicenseRef-Princeton-WordNet-3.0`, retain its copyright and disclaimer on all copies, and do not use Princeton's name in advertising. | OMW v2.0 also contains several pre-3.0 English WordNets, including 2.0, with version-specific terms. The exact English asset deployed must be pinned and checked. |
| Open English WordNet, when used instead of the OMW Princeton bundle | [Open English WordNet licence](https://raw.githubusercontent.com/globalwordnet/english-wordnet/main/LICENSE.md) | `CC-BY-4.0` for OEWN additions, with underlying Princeton obligations. Attribute both Princeton WordNet and the Open English WordNet team. | Do not infer that an OMW English asset is OEWN without matching its release artifact. |
| OMW French / WOLF | OMW [French package metadata](https://raw.githubusercontent.com/omwn/omw-data/main/index.toml) | OMW identifies French as WOLF and points to [CeCILL-C v1](http://www.cecill.info/licenses/Licence_CeCILL-C_V1-en.html). Record `CECILL-C` with version text `v1`; preserve supplied notices and do not assume a proprietary derived graph is permitted. | A current primary WOLF maintainer/distribution and the license file bundled with the deployed French artifact remain unverified. |
| eSpeak NG data | [eSpeak NG README](https://raw.githubusercontent.com/espeak-ng/espeak-ng/master/README.md), [`COPYING`](https://raw.githubusercontent.com/espeak-ng/espeak-ng/master/COPYING) | The project states `GPL-3.0-or-later`. Treat included dictionary, voice, and phoneme data as GPL-covered unless an asset-level review proves otherwise; preserve notices, licence, and corresponding-source obligations when conveying covered material. | Never infer that a code licence also clears third-party data, or that it licenses source text submitted to synthesis. Asset-level third-party and generated-audio status remain unverified. |
| Private or proprietary books | No public grant follows from possession or local ingestion. | Record `LicenseRef-Proprietary-No-Redistribution-Grant` only as an internal policy label, not an SPDX licence. Do not publicly release excerpts, OCR, protected expressive text, recoverable text-derived records, or publisher-created annotations without an explicit grant. | Bibliographic/provenance metadata may still require source-specific review; it is not a redistribution grant. |

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
