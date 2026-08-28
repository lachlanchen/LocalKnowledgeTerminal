[![Local Knowledge Terminal banner](docs/assets/banner.svg)](docs/assets/banner.svg)

# Local Knowledge Terminal

**Private, book-grounded intelligence on your own hardware.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-4B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal (LKT) turns a local book collection into cited word
origin and knowledge cards. Its first corpus is the structured **Word Origins**
edition; its first model is Qwen3-4B Q4_K_M running locally on an 8 GB Raspberry
Pi 5. The application, retrieval index, model runtime, and browser GUI operate
without a cloud API.

## Two experiences, one card contract

- **Word Origin** finds the strongest dictionary entry, explains the historical
  path, and creates English, Japanese, and Chinese memory views with Japanese
  reading/ruby and Chinese pinyin.
- **Knowledge Card** retrieves several relevant book entries for a question and
  composes a compact, evidence-bound explanation.

Both modes produce the same versioned card JSON. The web GUI renders that JSON
today; e-ink and audio adapters will consume it later without changing corpus,
retrieval, or model code.

```text
Word Origins entries.jsonl
          │
          ▼
  SQLite FTS5 retrieval ──────► deterministic excerpts + book pages
          │                                      │
          ▼                                      │
   Qwen3-4B via llama.cpp                        │
          │                                      │
          └────────► versioned card JSON ◄───────┘
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
                  Web GUI   E-ink     Audio
                  (ready)  (adapter)  (adapter)
```

## Grounding rule

The language model writes the explanation and translations, but it never writes
the citation list. LKT attaches entry IDs, excerpts, sections, and page numbers
directly from retrieval records. If the book has no matching evidence, the app
does not generate a card.

## Repository map

| Path | Responsibility |
| --- | --- |
| `lkt/corpus.py` | JSONL ingestion, atomic SQLite index, exact + FTS retrieval |
| `lkt/llm.py` | Small OpenAI-compatible llama.cpp adapter and strict JSON prompt |
| `lkt/service.py` | Card composition and normalization |
| `lkt/store.py` | Local card history |
| `lkt/web.py` | Dependency-free HTTP API and GUI server |
| `lkt/outputs.py` | Stable web/e-ink/audio output boundary |
| `lkt/static/` | Desktop-class GUI, responsive enough for later kiosk use |
| `scripts/` | Reproducible Pi runtime, install, update, and smoke-test tools |
| `systemd/` | Hardened model and application services |
| `docs/lineage.md` | Exact legacy-project and corpus provenance |

## Local development

LKT's core has no third-party Python runtime dependency:

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

Build a local index from the structured book export:

```powershell
$env:LKT_DATA_DIR="$PWD\var"
python -m lkt.cli ingest "C:\path\to\word-origins-pdf2tex\json\entries.jsonl"
python -m lkt.cli search abacus
```

With a llama.cpp server listening on port 8081:

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli serve
```

Open <http://127.0.0.1:8090>.

## Raspberry Pi 5 layout

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp checkout and ARM64 build
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

On the Pi:

```bash
./scripts/bootstrap_runtime.sh
sudo ./scripts/install_pi.sh /path/to/entries.jsonl
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

The book PDF, extracted corpus, model weights, generated indexes, and saved
cards are deliberately excluded from Git. Provide a legally obtained local
`entries.jsonl` during installation. LKT records its SHA-256 in the SQLite index
so a generated card can be traced to the exact corpus build.

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
