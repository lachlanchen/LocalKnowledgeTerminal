# Architecture

LKT is organized around a versioned card document, not a display device.

## Boundaries

1. `corpus` and `card_books` import structured sources and return immutable
   evidence records through one contract.
2. `llm` receives only the query, mode, and retrieved evidence. It returns an
   untrusted draft object.
3. `service` validates and normalizes the draft, then attaches deterministic
   evidence and a schema version.
4. `store` persists complete card documents for history and re-rendering.
5. `outputs` transform a card document into media. The browser uses JSON;
   future e-ink and audio implementations sit behind the same protocol.

The browser does not call llama.cpp directly. The e-ink adapter will not call
retrieval or the model. This keeps slow generation, source fidelity, display
refresh, and speech synthesis independently testable.

The browser treats saved cards as a carousel over the acquired-knowledge
ledger. Its fullscreen `display` mode removes navigation/composer chrome but
renders the same card JSON, so screen, print, and future e-ink output share
content semantics without sharing device code.

Raw Chat is a deliberate diagnostic side path: the web service forwards bounded
conversation history to the same local model and returns timing/token metrics.
Its response has `grounded: false`, receives no citation payload, and is not
written to card history. It is stored separately as an uncited observation with
model, prompt, response, timestamp, and timing metrics. Repeating a request
always invokes the model again; saved knowledge is an audit/history layer, not
an inference cache. This prevents a quality benchmark from masquerading as RAG
output.

When Model Lab is opened through **Discuss this card**, the server resolves the
card ID from SQLite and supplies its concise fields plus at most two retrieved
excerpts as bounded context. The browser cannot invent or alter that context.
The resulting observation records its `context_card_id` but remains distinct
from a cited card.

## Retrieval choice

Word Origins is a structured dictionary with 6,994 validated entries. Exact
headword lookup plus SQLite FTS5 is more transparent and resource-efficient than
an embedding model for this Pi release. The 318 Answer cards use a stable
query-seeded draw. The 291 Question cards use multilingual FTS5 search with a
stable draw as fallback. All three paths make citations reproducible. A future
semantic retriever can implement the same evidence contract and be evaluated
against this lexical baseline.

## Trust boundary

Model output is normalized text, never executable markup. Japanese ruby is
constructed by the browser from separate term/reading fields or reviewed
token-level furigana. Answer and Question translations are copied from corpus
records after inference, so the model cannot rewrite them. Citation pages,
locators, and excerpts are never accepted from model output. The GUI ships no
CDN scripts, fonts, analytics, or cloud calls.

## Extension contract

`Card.schema_version` begins at `1.0`. E-ink should render a card to an image at
a device-specific resolution/color profile and audio should synthesize selected
language fields. Both adapters should fail explicitly until configured rather
than silently degrade the core card.
