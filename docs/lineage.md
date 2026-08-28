# Project lineage and pinned references

Local Knowledge Terminal is a clean implementation informed by two earlier
Lachlan Chen projects. They are references, not runtime dependencies.

| Project | Pinned HEAD (2026-08-28) | Retained idea | Deliberately replaced |
| --- | --- | --- | --- |
| [WordsCardEink](https://github.com/lachlanchen/WordsCardEink) | `9c4116624a8cb17916bbbc3899cae745a52d1623` | Multilingual vocabulary cards, local cache, virtual preview, later Waveshare output | Coupled Tornado/OpenAI/data/hardware flow and vendored display stack |
| [WordOrigins](https://github.com/lachlanchen/WordOrigins) | `83f1500be7c2b3014b483ff7fa2881b31cb4c32a` | Etymology exploration, structured model output, multilingual lineage | Cloud-only analysis, graph/image coupling, bundled font/archive weight |

The first LKT corpus comes from the separate structured extraction at
`word-origins-pdf2tex/json/entries.jsonl`. The observed export contains 6,994
entries and source-page metadata. The verified 2026-08-28 export has SHA-256
`b65a2845e649451a1f5d20013d150b4a7668afcb09e794756867fd843918adf5`.
The corpus itself is not redistributed in this repository; the installer
records its SHA-256 at index time.

Two validated multilingual card-book exports join that dictionary in the first
private library. Their raw records are not redistributed here:

| Corpus ID | Records | Indexed JSONL SHA-256 | Grounding |
| --- | ---: | --- | --- |
| `book-of-answers-paul-card-book` | 318 | `247f7b0e56e81703453f60462ff0bf73f6fe6b9c9feec014e5b5da670977e8f1` | Chinese source text, PDF page, reviewed en/ja/zh |
| `book-of-questions-stock-card-book` | 291 | `f265ac47905cf6475027159da8821fb82e1857bee885c95d6127166e8ba73ff0` | English source text, EPUB member, reviewed en/ja/zh |

Both source projects report a passed validation with no assembly errors. LKT
uses their `multilingual-items.jsonl` exports so Japanese ruby tokenization and
reviewed translations remain source data rather than model inventions.

This structure means improvements can be ported back to the older projects when
useful, while LKT remains deployable without their hardware assets, notebooks,
legacy variants, or external API credentials.
