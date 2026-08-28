# Local Knowledge Terminal — durable product brief

This document preserves the owner's LKT requests and decisions in a normalized,
implementation-ready form. It deliberately excludes passwords, machine login
details, private addresses, and other secrets. Update it whenever product intent
changes so future work does not depend on chat history.

## Product idea

Build an independent, elegant **Local Knowledge Terminal (LKT)** on a Raspberry
Pi 5 with 8 GB RAM. It should use a local Qwen model and private book retrieval
to create concise knowledge cards. The browser/VNC interface is the first
product display; a 7.3–7.5 inch Waveshare color e-ink display, microphone, and
audio can be attached later through decoupled output interfaces.

The guiding qualities, in priority order, are:

1. simple;
2. robust;
3. fast and efficient;
4. capable;
5. clean, vivid, elegant, and easy to use.

Do not add agent features or integrate the separate LocalLLM/AgInTiFlow projects
yet. They are future references only.

## Reference material

- `lachlanchen/WordsCardEink`: vocabulary-card semantics, typography-first
  multilingual display, pronunciation, Japanese, Chinese, French, and Arabic.
- `lachlanchen/WordOrigins`: recursive directed etymology graph, component/root
  ancestry, multilingual root context, graph layout, and visual lineage.
- Local Word Origins structured book: shared RAG source for Word Origin and Word
  Card, with different retrieval and presentation policies.
- Local Book of Answers and Book of Questions structured editions: independent
  RAG sources for their respective modes.
- Local reviewed English Root Dictionary and English Affix Dictionary exports:
  first-class morphology evidence for Word Card, Word Origin, Root, and Affix.
- Future architecture references only: `LazyingArt/AgInTiFlow` and
  `lachlanchen/LocalLLM`.

## Independent modes

The bare terminal opens the accepted Book of Answers carousel as its ambient
experience. A stable local draw is visible immediately and advances
automatically when more than one accepted Answer card exists. Question, Word
Card, Word Origin, Root, Affix, and Model Lab remain explicit user-selected
paths rather than being mixed into the ambient card.

On the bare ambient screen, one English word opens Word Card and an ordinary
sentence or question opens local Chat. Explicit `word:`, `origin:`, `root:`,
`affix:`, `question:`, `answer:`, and `ask:` prefixes choose an exact path.
Selecting a tab or using a `?mode=` URL also remains an exact override.
An inquiry first reuses an accepted card with the same mode and query; callers
can request deliberate regeneration with the API's `refresh: true` flag.

### Word Origin

- It is an origin product, not a generic word card.
- The main visual is a real directed ancestry graph rendered with a proper
  JavaScript graph library.
- Follow the recursive root/component structure of `WordOrigins`, including
  branching when a word is composed from multiple morphemes.
- The graph's modern root may show simple Japanese and Chinese equivalents.
- Keep everything else restrained: the word, one clear definition, the graph,
  and one book citation.
- Use Word Origins book evidence plus reliable model knowledge. Every node says
  whether its basis is the book or model context. Citations are never generated
  by the model.

### Word Card

- It is a true vocabulary card, not a general book-grounded essay.
- English word and IPA are dominant.
- Meanings/equivalents are the main content: English, Japanese, and Chinese stay
  stable; French and Arabic can rotate.
- Prefer established lexical equivalents, not phonetic imitation.
- Japanese kanji uses token-level furigana. Chinese Han characters use
  character-level tone-marked pinyin ruby.
- Keep meanings short, memorable, and screen-sized. Save the richer generated
  JSON in the database even when it is not shown on the primary slide.

### Book Answer

- Preserve the reviewed source answer and translations exactly.
- Present one language at a time when that preserves large, readable type.
- English, Japanese with furigana, and Chinese with per-character pinyin are
  internal slides of the same saved card.
- Add only a restrained local-model reflection or related vocabulary when it is
  useful; place extra material on another slide instead of crowding the answer.

### Book Question

- Preserve the reviewed source question and translations exactly.
- This mode must be especially restrained because questions can be long.
- Show English, Japanese with furigana, and Chinese with per-character pinyin on
  separate internal slides. If one language is still long, split it again at a
  punctuation/word boundary.
- Never solve overflow merely by hiding it or making the type tiny. All primary
  content must be reachable through the internal carousel.
- Related vocabulary may be a separate slide rather than another block on the
  main question.

### Root and Affix

- Keep Root and Affix as separate top-level collections with different primary
  retrieval order, while both consult the Root and Affix books.
- Save one complete connected morphology/history graph around the center word,
  including typed nodes, directed edges, exact evidence IDs, confidence, and
  focus areas.
- Show an overview map in the graph corner. Inner slides focus/zoom the main
  graph onto each root, prefix, suffix, or historical branch.
- Retain rich recursive JSON in SQLite while showing only one clear teaching
  point per screen.

### Model Lab

- Keep a simple raw local chat/benchmark page for testing Qwen quality and
  speed, including multilingual queries and discussion of the active card.
- Mark output as uncited and keep its ledger separate from grounded cards.
- The visual style should not imitate a cluttered ChatGPT chat page.

## Layout and carousel contract

- Every visible slide is a complete one-screen composition: no page scrolling,
  no content beneath controls, and no clipped primary text.
- `overflow: hidden` is a safety boundary, not the fitting strategy.
- Prefer fewer high-quality facts, large type, clean hierarchy, and generous
  space.
- Use an inner carousel to split the complete content of one card by language
  or sentence segment.
- Use an outer, mode-local carousel for saved cards: Origin cycles only through
  Origin, Word Card only through Word Card, and likewise for Answer/Question.
- The six knowledge tabs follow the learning path Question, Answer, Word Card,
  Word Origin, Root, and Affix. Model Lab remains a separate uncited utility.
- Question and Answer may be independent seeded draws; thematic continuity is a
  useful enhancement, not a requirement that makes offline browsing fragile.
- Autoplay should be calm and predictable, with clear manual previous/next
  controls.
- Generated, formatted cards are saved to a local SQLite knowledge ledger. A
  repeated request may run the model again and save another version.
- Save retrieved evidence, cleaned model draft, normalized card/graph, and final
  published revision as reusable preparation artifacts. Archive weak cards from
  the active carousel without destructive deletion.
- Treat model completion, database save, and visible publication as three
  separate states. Only accepted, grounded, correctly encoded cards may enter a
  mode-local carousel; legacy and rejected candidates remain invisible.

## Local inference

- Qwen3-4B Q4_K_M is the simple offline default when compact dictionary and book
  RAG provide the needed correction. Qwen3-8B remains a proven, optional
  quality-first preparation model.
- Download the official Qwen3-8B Q4_K_M as an optional deep model, verify its
  SHA-256, and benchmark it safely.
- Never run 4B and 8B together on an 8 GB Pi. Keep a one-command, automatic 4B
  fallback and do not replace the default until 8B proves stable.
- Never require one model response to build a complete morphology graph. Use
  small persisted jobs under either model and compose accepted artifacts later;
  prepared SQLite display remains fast.
- Model downloads must be resumable and must not interrupt the live 4B service.
- Use the Word Origins book RAG to correct hallucinated etymology and ground
  source claims.

## Voice and hardware direction

- Future voice path: microphone → VAD/noise handling → Whisper Tiny →
  retrieval/card engine → Qwen → card output. Speech synthesis is optional and
  deferred for the offline-first release.
- Use Raspberry Pi Codec Zero for the first Pi 5 prototype: it is an official
  HAT with a built-in MEMS microphone, EEPROM configuration, and small-speaker
  output. Use a USB Audio Class microphone as the diagnostic fallback.
- Evaluate a bare I²S/PDM MEMS microphone only for a later compact custom-board
  experiment after its Pi 5 device-tree and ALSA path is proven. Do not make the
  currently fragile ReSpeaker Pi 5 driver path the baseline.
- Do not change audio configuration until hardware is attached and a read-only
  device probe has identified the actual card/source.
- Keep microphone, e-ink, and audio work out of the core retrieval/model modules.

## Engineering and delivery rules

- Keep corpus ingestion, retrieval, model inference, card composition, storage,
  and rendering independent.
- Treat normalized SQLite knowledge as authoritative and the embedded graph
  database as a replaceable traversal projection.
- Private books, indexes, model files, saved cards, secrets, and generated
  screenshots do not enter Git.
- Develop on Windows, commit/push stable changes to GitHub, fetch/deploy on the
  Pi, and verify the real Pi service and cards.
- Commit and push frequently, especially after large stable changes.
- Do not change the Pi router/network configuration while developing LKT.
