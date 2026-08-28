[![Local Knowledge Terminal banner](docs/assets/banner.svg)](docs/assets/banner.svg)

# Local Knowledge Terminal

**Private, book-grounded intelligence on your own hardware.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal (LKT) turns a private book collection into cited,
multilingual cards. Its first library combines structured editions of **Word
Origins**, **The Book of Answers**, **The Book of Questions**, an **English Root
Dictionary**, and an **English Affix Dictionary**. Qwen3-4B Q4_K_M runs locally
on an 8 GB Raspberry Pi 5 with Qwen3-8B as an optional slower profile; retrieval,
inference, history,
and the browser GUI operate without a cloud API.

## Six independent experiences, one card contract

- **Word Origin** uses its own one-entry retriever and prompt to make a bounded,
  interactive directed ancestry graph. Branching morphemes are preserved;
  book-supported nodes and model-supplied linguistic context are visibly
  distinguished.
- **Word Card** retrieves several relevant Word Origins entries and composes a
  compact multilingual memory view. English, Japanese, and Chinese remain fixed
  while French and Arabic rotate in a fourth panel.
- **Book Answer** makes a reproducible draw from 318 reviewed cards, preserves
  the published answer translations, and adds a reflective note.
- **Book Question** searches 291 reviewed questions by theme and falls back to a
  reproducible draw when no lexical match exists.
- **Root Graph** prioritizes 6,327 reviewed root records, then exact supporting
  affix entries, and saves a recursive word-family graph.
- **Affix Graph** reverses that priority across 5,189 reviewed affix records and
  the Root Dictionary while retaining one complete center-word graph.

Each mode has its own retrieval policy and strict model prompt. Word Origin and
Word Card deliberately share the same Word Origins index while presenting it
differently; Answer and Question use separate books and retrieval engines. All
six modes produce the same versioned card JSON. Japanese card-book text retains
token-level furigana, and Chinese views receive deterministic full tone-marked
pinyin. The web GUI renders that JSON today; e-ink and audio adapters will
consume it later without changing corpus, retrieval, or model code.

A separate **Chat / Benchmark** workspace talks directly to Qwen and reports
wall time, prompt/output tokens, and generation speed. It is visibly marked as
raw, uncited model output and is never stored as a grounded book card. Its
observations are retained in a separate table of the local knowledge ledger.
Every repeated prompt still runs Qwen again; the ledger is history, not a cache.
From any card, **Discuss this card** opens Model Lab with that saved card and its
retrieved excerpt as bounded context.
Each live Model Lab session also receives a durable inquiry thread. Successive
turns preserve parent/child lineage; a card discussion links to its normalized
source content atom while the Qwen response remains explicitly uncited.

## Product display

The browser is an editorial card stage rather than a chat dashboard. Every
visible slide is a no-scroll, one-screen composition with a large core idea and
one compact source citation. Word Origin reserves its center for a Cytoscape.js
directed graph. Word Card uses large English/IPA above fixed Japanese/Chinese
and a rotating French/Arabic panel. Answer and Question use an inner language
carousel—English, Japanese ruby, Chinese pinyin ruby—and split unusually long
sentences into additional readable slides. Saved cards form independent,
mode-local outer carousels with previous/next controls.
Root, Affix, and Word Origin share one Cytoscape graph renderer: a complete
saved graph, a corner overview map, and inner focus slides that zoom into a
root, prefix, suffix, or history branch without duplicating the graph.
Fullscreen display mode hides all application chrome, and `/?display=1` opens
the same card document as a kiosk-friendly screen surface. Print CSS and the
versioned card JSON provide clean boundaries for later e-ink rendering.

Every generated card receives a new ID and remains in the card ledger. A second,
normalized `knowledge.sqlite3` database stores accepted terms, senses,
pronunciations, phoneme/grapheme segments, morphemes, history, translations,
grammar, provenance, revisions, and inquiry lineage as reusable atoms. Cards are
reconstructable views over those atoms. A LadybugDB property graph is a derived
traversal projection and can always be rebuilt from SQLite.
Accepted Book Answer and Book Question cards also place their exact reviewed
English, Japanese, and Chinese texts in this normalized store. Each language is
an independent content atom linked to the retrieval-owned book citation; model
reflection is deliberately excluded from that book evidence.

Preparation uses small dependency-aware jobs: retrieve evidence, prepare one
meaning, split components, recursively expand each origin branch, prepare each
language/pronunciation independently, validate, then compose. Successful stages
are checkpointed immediately; one weak language or branch can be retried without
discarding the rest.

The installed low-priority worker also grows the reviewed Book Answer and Book
Question decks autonomously. When the atomic queue is idle, it chooses exactly
one source entry that has never produced an accepted card, balances progress
between the two books, asks the Pi's local Qwen to prepare the small title and
reflection, publishes only through the normal validation gate, and then queues
one bounded vocabulary investigation. Stable source entry IDs prevent repeats
across restarts. It stops after all 318 Answers and 291 Questions are accepted.

This ownership boundary is deliberate: reviewed book sentences, translations,
and citations come from the local corpus records and are never rewritten by the
model; new explanatory or lexical data is produced by the configured local
model, not hand-entered into SQLite. A bad draft stays outside the visible deck.
Autonomous generation pauses during current Raspberry Pi undervoltage,
throttling, or high temperature and resumes after the condition clears. The web
client loads the complete selected mode (up to 1,000 accepted cards), keeps the
newest first, and shuffles every other card once per carousel pass.
Word Card, Word Origin, Root, and Affix remain inquiry-driven rather than
precomputing thousands of slow model views; once a word or linked term is
selected, their persisted local pipeline runs without human data entry.

```text
 Word Origin ──► best Word Origins entry ─────┐
   Word Card ──► multi-entry Word Origins ────┤
 Book Answer ──► reproducible answer draw ────┼──► independent prompts
Book Question ─► question search / draw ──────┘              │
                                                              ▼
                                                  Qwen3-8B / 4B on llama.cpp
                                                       │
                                      ┌────────────────┴───────────────┐
                                      ▼                                ▼
                              versioned card JSON            deterministic citations
                                      │
                            ┌─────────┼─────────┐
                            ▼         ▼         ▼
                          Web GUI   E-ink     Audio
                          (ready)  (adapter)  (adapter)
```

## Grounding rule

The language model writes explanations and missing language aids, but it never
writes the citation list. LKT attaches entry IDs, excerpts, sections, page
numbers, digital locators, and reviewed card-book translations directly from
retrieval records. Word Origin may add reliable linguistic context, but every
graph node records whether it came from the book anchor or model knowledge. If
the configured book has no evidence, the app does not generate a card.

## Repository map

| Path | Responsibility |
| --- | --- |
| `lkt/corpus.py` | Word Origins ingestion, atomic SQLite index, exact + FTS retrieval |
| `lkt/morphology.py` | Root/Affix polished-JSONL ingestion, provenance, exact + FTS retrieval |
| `lkt/card_books.py` | Multilingual Answer/Question ingestion, search, and deterministic draws |
| `lkt/deck.py` | One-at-a-time autonomous reviewed-book deck preparation |
| `lkt/device.py` | Pi power/thermal readiness gate for background inference |
| `lkt/retrieval.py` | Independent Word Origin, Word Card, Answer, and Question RAG policies |
| `lkt/llm.py` | Small llama.cpp adapter and one strict prompt per experience |
| `lkt/service.py` | Card composition and normalization |
| `lkt/pronunciation.py` | Deterministic pinyin/ruby and versioned offline IPA |
| `lkt/store.py` | Versioned cards, preparation artifacts, revisions, archive, and chat ledger |
| `lkt/knowledge.py` | Atomic established knowledge, evidence, jobs, revisions, and inquiry lineage |
| `lkt/preparation.py` | Dependency-aware divide-and-conquer word/content planning |
| `lkt/atomic.py` | Bounded atomic preparation and deterministic card assembly |
| `lkt/graph.py` | Rebuildable LadybugDB traversal projection from accepted SQLite atoms |
| `lkt/lexicon.py` | Compact multilingual WordNet correction evidence |
| `lkt/web.py` | Dependency-free HTTP API and GUI server |
| `lkt/outputs.py` | Stable web/e-ink/audio output boundary |
| `lkt/static/` | Desktop-class GUI, responsive enough for later kiosk use |
| `scripts/` | Reproducible Pi runtime, install, update, and smoke-test tools |
| `systemd/` | Hardened model and application services |
| `docs/lineage.md` | Exact legacy-project and corpus provenance |
| `docs/product-brief.md` | Durable owner requirements and acceptance criteria |
| `docs/knowledge-architecture.md` | Atomic SQLite, graph projection, and staged preparation contract |
| `docs/owner-request-log.md` | Chronological, privacy-redacted owner direction |
| `docs/voice-hardware.md` | Supported microphone choice and staged audio tests |
| `docs/mode-roadmap.md` | Extension plan for future suffix, affix, and root books |

## Local development

Install the small pinned pronunciation dependency, then run the suite:

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

Build a local index from the structured book export:

```powershell
$env:LKT_DATA_DIR="$PWD\var"
python -m lkt.cli ingest "C:\path\to\word-origins-pdf2tex\json\entries.jsonl"
python -m lkt.cli ingest-card-book answer "C:\path\to\book-of-answers\json\multilingual-items.jsonl"
python -m lkt.cli ingest-card-book question "C:\path\to\book-of-questions\json\multilingual-items.jsonl"
python -m lkt.cli ingest-morphology root "C:\path\to\root-dictionary\output\json\entries-editorial.jsonl"
python -m lkt.cli ingest-morphology affix "C:\path\to\affix-dictionary\output\json\entries-editorial.jsonl"
python -m lkt.cli search abacus
python -m lkt.cli search technology --corpus question
python -m lkt.cli knowledge-status
python -m lkt.cli sync-card-knowledge
python -m lkt.cli plan-word inspection --display-languages en ja zh fr ar
python -m lkt.cli plan-translation inspection ar --prompt-version atomic-v2
python -m lkt.cli work-atomic --limit 1
python -m lkt.cli seed-deck --modes answer question
```

With a llama.cpp server listening on port 8081:

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

Open <http://127.0.0.1:8090>.

## Raspberry Pi 5 layout

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

Pinned runtime artifacts:

| Artifact | Revision | Integrity |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | Commit-pinned source archive |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| Model file | `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256 bytes |

The Pi service exposes one inference slot (`--parallel 1`). Card composition and
Model Lab requests are therefore handled sequentially, keeping memory use and
latency predictable instead of making four CPU cores compete across jobs.

Qwen3-8B is proven usable as an optional quality-first preparation model. On the
deployed Pi it produced a 120-token multilingual probe at 1.78 tokens/s with
about 6.28 GiB RSS, 1.85 GiB system memory still available, and no current
thermal throttling. Qwen3-4B is the responsive offline default. Model selection
is explicit and reversible:

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

Only one model is loaded at a time. The 8B profile uses a 3,072-token context
and smaller batch to protect the 8 GB memory boundary. If its server does not
become healthy, `select_model.sh 8b` restores the 4B profile automatically.
The downloader resumes a partial transfer, verifies the official SHA-256, and
only then atomically exposes the final GGUF.
The benchmark activates one model at a time, runs the same bounded multilingual
quality/speed probe, records wall time, llama.cpp token rate, and process memory,
then restores the model that was active before the benchmark.

Install the compact optional knowledge runtime and build the graph projection:

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

This installs eSpeak NG for local IPA, pins LadybugDB 0.19.1 and Wn 1.1.1 in an
isolated environment, then installs only the OMW 2.0 English, Japanese, Mandarin
Chinese, French, and Arabic lexicons. Full Wiktionary dumps are intentionally
excluded. IPA extraction uses quiet text mode and does not enable speech output.

On the Pi:

```bash
./scripts/bootstrap_runtime.sh
sudo ./scripts/install_pi.sh \
  /path/to/entries.jsonl \
  /path/to/answers/multilingual-items.jsonl \
  /path/to/questions/multilingual-items.jsonl \
  /path/to/root/entries-editorial.jsonl \
  /path/to/affix/entries-editorial.jsonl
./scripts/smoke_test.sh
```

For later Windows → GitHub → Pi development:

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi.sh
```

Then open `http://127.0.0.1:8090` in the Pi's VNC desktop, or
`http://<pi-lan-address>:8090` from the trusted local network.

## Data and copyright

The book PDFs, extracted corpora, model weights, generated indexes, and saved
cards are deliberately excluded from Git. Provide a legally obtained local
JSONL export during installation. LKT records each SHA-256 in its SQLite index
so a generated card can be traced to the exact corpus build. See
[`docs/corpora.md`](docs/corpora.md) for the verified reference set.

## Lineage

LKT is a clean, local-first successor informed by
[`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) and
[`WordOrigins`](https://github.com/lachlanchen/WordOrigins). It does not import
their monolithic runtime or hardware dependencies. See
[`docs/lineage.md`](docs/lineage.md) for pinned commits and retained ideas.

## Support

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |
