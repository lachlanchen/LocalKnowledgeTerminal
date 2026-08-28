# Architecture

LKT is organized around a versioned card document, not a display device.

## Boundaries

1. `corpus` and `card_books` import structured sources and return immutable
   evidence records through one contract.
2. `retrieval` exposes four independent RAG engines. Word Origin and Word Card
   share a corpus but use different result limits; Answer and Question each own
   their book policy.
3. `llm` receives only the query, mode, and retrieved evidence. Four independent
   prompts return untrusted, mode-bounded draft objects tailored to their
   presentation. JSON parsing, semantic completeness checks, and one repair
   attempt prevent malformed or blank drafts from entering the ledger.
4. `service` validates and normalizes the draft, then attaches deterministic
   evidence and a schema version.
5. `store` persists complete card documents for history and re-rendering.
6. `outputs` transform a card document into media. The browser uses JSON;
   future e-ink and audio implementations sit behind the same protocol.

The browser does not call llama.cpp directly. The e-ink adapter will not call
retrieval or the model. This keeps slow generation, source fidelity, display
refresh, and speech synthesis independently testable.

The model service loads exactly one GGUF. `/etc/lkt-model.env` selects its path,
context, batch size, and public model label. The 4B profile is the default; the
optional 8B profile reduces context/batch memory and has an automatic 4B health
fallback. The web and model services read the same label file so saved cards
record the model that actually produced them.

On the 8 GB Pi profile, the 4B service uses a 3,072-token context and a smaller
batch. systemd applies a soft 5 GB and hard 6 GB model-service ceiling plus a
small swap ceiling; a pathological inference therefore restarts the model
instead of starving SSH, VNC, or the web UI. Optional Answer/Question deck
generation also pauses below 1.5 GiB available memory. Interactive requests and
already persisted cards remain independent of that background pause.

The browser exposes two carousel levels over the acquired-knowledge ledger. The
outer carousel is filtered by mode, so autoplay never changes an Origin into a
Question. Answer and Question also have an inner language carousel. It renders
English, Japanese ruby, and Chinese pinyin ruby independently and splits long
source sentences at bounded text/token boundaries. Fullscreen `display` mode
removes navigation/composer chrome but renders the same card JSON.

Word Origin renders its stored `origin_graph` with a pinned, locally vendored
Cytoscape.js build. The model returns a modern root plus ancestor/component
nodes whose parent links form a directed ancestry tree; a breadth-first layout
keeps the graph deterministic, interactive, and non-overlapping. Word Card
keeps Japanese/Chinese fixed and rotates stored French/Arabic forms in the third
panel beneath the English/IPA hero. The renderer never asks the model for data
while changing a slide.

“Grounded” means that a card has deterministic book evidence; it does not mean
the model-authored fields are quotations. Word Origin is labelled book anchor +
model context, Word Card is book anchor + model languages, and Answer/Question
identify their reviewed book translations explicitly.

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
records after inference, so the model cannot rewrite them. Chinese pinyin and
character-level ruby tokens are recomputed locally from the final normalized
Chinese string so incomplete model transliteration never reaches a card. Citation pages,
locators, and excerpts are never accepted from model output. The GUI ships no
CDN scripts, fonts, analytics, or cloud calls.

## Extension contract

`Card.schema_version` is `1.2`. It stores directed `origin_graph` node IDs and
parent links, `extra_languages`, and deterministic Chinese `ruby_tokens`. Old
payloads remain renderable: the web boundary adds ruby tokens in memory without
rewriting historical rows. E-ink should render a card to an image at a
device-specific resolution/color profile and audio should synthesize selected
language fields. Both adapters should fail explicitly until configured.

Voice input follows the same boundary in reverse: a capture/STT adapter submits
a bounded mode and query to the existing application API. It never imports the
retriever or model directly. `docs/voice-hardware.md` records the supported
prototype choice and staged acceptance tests.
