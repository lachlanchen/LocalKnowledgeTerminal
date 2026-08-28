# Architecture

LKT is organized around a versioned card document, not a display device.

## Boundaries

1. `corpus` imports a structured source and returns immutable evidence records.
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

## Retrieval choice

Word Origins is already a structured dictionary with 6,994 validated entries.
Exact headword lookup plus SQLite FTS5 is more transparent and resource-efficient
than an embedding model for the first release. It also makes citations
deterministic. A future semantic retriever can implement the same `search`
contract and be evaluated against the lexical baseline.

## Trust boundary

Model output is normalized text, never executable markup. Japanese ruby is
constructed by the browser from separate `term` and `reading` fields. Citation
pages and excerpts are never accepted from model output. The GUI ships no CDN
scripts, fonts, analytics, or cloud calls.

## Extension contract

`Card.schema_version` begins at `1.0`. E-ink should render a card to an image at
a device-specific resolution/color profile and audio should synthesize selected
language fields. Both adapters should fail explicitly until configured rather
than silently degrade the core card.
