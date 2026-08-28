# Owner request log

This is the durable, chronological record of the owner's LKT direction from the
project conversation. It preserves product intent in the owner's terms while
excluding passwords, private network addresses, machine usernames, and other
credentials. `product-brief.md` is the normalized acceptance contract; this
file is the fuller memory ledger. Add new requests here before condensing them
into the brief.

## Project and deployment

- Turn the Raspberry Pi 5 with 8 GB RAM into a local knowledge and vocabulary
  terminal after the separate Wi-Fi-to-LAN work is finished.
- Use `LocalKnowledge` as the Windows working directory and publish the product
  as `LocalKnowledgeTerminal` / LKT on GitHub.
- Develop on Windows, push through GitHub, fetch on the Pi, and keep every large
  stable change committed and pushed so no work is lost.
- Use the Pi's VNC desktop and make the browser application easy to open and
  test there.
- Keep the project elegant, decoupled, independently useful, and suitable for a
  future physical product.
- Do not disturb either Raspberry Pi's routing/network setup while working on
  LKT.
- `LazyingArt/AgInTiFlow` and `lachlanchen/LocalLLM` are possible references for
  later agent/local-model work, but agent integration is not needed now.

## Models and local inference

- Download and run a suitable local Qwen model on the Raspberry Pi 5, and prove
  that it can produce a real response using Word Origins material.
- Qwen 4B has demonstrated acceptable quality and about three tokens per second
  in interactive multilingual/etymology tests, so keep it as the responsive
  baseline.
- Investigate whether a 7B or 8B model can run smoothly on the same 8 GB Pi.
- Download an 8B model in the background if it is safe, without stopping other
  work or damaging the working 4B setup.
- Prefer a practical, robust solution over a large model merely for its size.
- Let the local model work on one task at a time; simultaneous generation is
  unnecessary.
- Keep a simple model-chat page to test output quality and speed, including
  follow-up discussion of the currently displayed card.
- The raw model output is useful, but it must remain visibly uncited and
  separate from grounded book cards.

## Knowledge sources and RAG

- Use the local structured Word Origins book as RAG evidence for etymology and
  vocabulary cards. Combine book evidence with reliable model knowledge, while
  making the source boundary clear.
- Use the local Book of Answers and Book of Questions structured editions as
  independent RAG materials for answer and question cards.
- Add the supplied reviewed exports of *English Affix Dictionary* and *New
  Oriental English Root Dictionary* as first-class RAG sources. Use their real
  polished JSONL records and page provenance; do not ship mock morphology data.
- Treat Word Origin, Word Card, Book Answer, and Book Question as independent
  products with independent retrieval/prompt policies. Origin and Word Card may
  share Word Origins data but must present it differently.
- Save the model-prepared, formatted data in a local database. Repeating a
  request may run the model again and store another version rather than acting
  as a permanent cache.
- Keep acquired knowledge durable so the product grows into a local knowledge
  book, not a disposable chat session.
- Stored cards must support deliberate refinement: preserve versions, allow a
  chosen revision to become the active one, and allow poor revisions to be
  removed from presentation without losing provenance by accident.

## Root and affix graph cards

- Root and Affix are independent focused experiences, alongside Word Origin
  and Word Card, with their own retrieval and preparation policies.
- Build one complete morphology graph around the center word. Connect every
  evidenced prefix, root, and suffix and recursively investigate useful related
  forms with book RAG plus clearly labelled model knowledge.
- Keep a rich, complete prepared JSON record in SQLite, while presenting only a
  clean, restrained subset on any one screen.
- Persist recursively prepared word/component histories themselves—not only the
  visible summary—including nodes, edges, focus areas, cited record/page IDs,
  model-added knowledge, confidence, preparation model, and revision lineage.
- Save reusable, cleaned intermediate preparation stages as well as the final
  JSON: retrieved evidence snapshot, parsed model draft, normalized graph/card,
  published revision, and failure status when a run cannot finish.
- Use a real graph visualization. The mode-local carousel should move focus
  through the center word's root and affix regions slide by slide, like zooming
  into areas of the same complete graph, rather than rendering unrelated fake
  diagrams.
- Let Word Card, Word Origin, Root, and Affix loop independently. Each mode may
  have multiple saved cards, and one morphology card may also have multiple
  focus slides without mixing product modes.
- Keep graph behavior dynamic and informative: fit the complete graph for its
  overview, focus a selected branch for each slide, avoid node overlap, and
  retain pan/zoom for inspection.
- Aim for detailed high-quality preparation but sparse display. Source,
  confidence, relationships, meanings, examples, and recursive evidence belong
  in the data contract; only the current area's core teaching point belongs on
  the visible card.

## Preparation model policy

- Compare the verified Qwen 8B model against the working 4B model on real book
  evidence, not a synthetic prompt alone.
- If 8B loads reliably on the 8 GB Raspberry Pi, completes real structured
  morphology preparation without memory instability, and materially improves
  quality at usable speed, make it the default offline data-preparation model.
- Keep display and carousel browsing independent of model latency by serving
  prepared SQLite cards immediately.
- Run high-quality enrichment as small sequential jobs—graph, phonemes, script
  segments, grammar, translations, review—then loop gradually through real
  corpus items. Never ask the Pi to perform every enrichment in one prompt.
- Support reproducible corpus-driven selection so Answer/Question records can
  become language cards and Word Origins entries can feed random word cards.

## Word Origin

- Word Origin is specifically an etymology experience, not a crowded generic
  card.
- Follow the real graph idea from `lachlanchen/WordOrigins`: use a proper
  JavaScript graph library and plot recursive ancestry/components as an actual
  directed graph.
- Branch the graph when a word contains more than one meaningful component;
  avoid presenting every origin as a decorative linear timeline.
- Decompose the center word across all evidenced roots and affixes, then trace
  each component's useful history recursively. Word Origin should share the
  rich branching graph grammar used by Root and Affix cards, while retaining
  its distinct historical emphasis.
- Use the Word Origins book reference plus model knowledge, and make the final
  graph visually proper, comprehensive enough to teach, and free of overlap.
- Show less surrounding material: a simple multilingual anchor and the main
  origin graph should dominate.
- Add a compact overview map in a graph corner so the reader keeps spatial
  context while a carousel focus slide zooms into one component or era.

## Word Card

- Word Card must look and behave like a real vocabulary card, informed by
  `lachlanchen/WordsCardEink`, rather than repeating the generic RAG layout.
- Make the word, pronunciation, and core multilingual meanings dominant.
- Keep English, Japanese, and Chinese stable. Rotate French and Arabic in the
  additional-language position.
- Japanese kanji must have furigana ruby; Chinese characters must have pinyin
  ruby in the final output.
- Prefer concise, high-quality, memorable content sized for the physical
  screen.
- Prepare phoneme-level pronunciation segments for words and render the parts
  with a restrained, consistent color grammar informed by WordsCardEink.
- Prepare Arabic at letter/grapheme level so its visible word structure can use
  the same learnable color system without breaking right-to-left order.
- Keep pronunciation and script segmentation as independent reusable model
  tasks rather than overloading the main card-generation prompt.

## Answer and Question cards

- Show Book of Answers and Book of Questions material as focused cards, not as
  long chat output.
- Preserve multilingual English, Japanese, and Chinese content, including
  furigana and pinyin ruby.
- Questions require especially restrained layouts because source questions can
  be long.
- Split long content across carousel slides by language or sentence rather than
  clipping it, hiding it, or shrinking it into unreadable text.
- Related words may become a separate card/slide when useful, rather than
  crowding the primary question or answer.
- Treat reviewed Answer and Question sentences as reusable language-learning
  material. Prepare clear sentence grammar segments (subject, verb, object,
  complements/modifiers, and language-specific particles) and place grammar on
  its own slide when it would crowd the main reading slide.
- Let any useful word inside an Answer or Question branch into a linked local
  investigation (meaning, pronunciation, grammar, origin, root, or affix)
  without losing the source sentence it came from.

## History and long-term storage

- Save and manage the history of questions, card requests, enrichment jobs, and
  discussions so the terminal remains useful over years rather than sessions.
- Keep final structured knowledge and high-quality intermediate artifacts by
  default. Add searchable status/mode/time/card lineage, and use optional
  compression or retention rules only for redundant raw traces if storage ever
  becomes material.

## Visual product

- Replace the initial ugly ChatGPT-like page with a clean, vivid, bright,
  colorful card product.
- Use large, reasonable type and strong hierarchy. Remove excess small labels,
  metadata, filler text, and rubbish text.
- Every card view must fit a single page: no scrolling, overflow, obstruction,
  or content hidden under controls.
- Do not merely disable scrolling. Restructure and split the complete content so
  it genuinely fits and remains accessible.
- Put fewer, better facts on each slide. Focus on the core idea.
- Use independent mode-local carousels, and use inner slides when one saved card
  needs multiple languages or sentence segments.
- Keep six first-class knowledge tabs: Word Card, Word Origin, Answer,
  Question, Root, and Affix. Each tab owns its complete saved collection (all
  words, questions, answers, roots, or affixes for that experience), and each
  selected card may contain its own multi-slide carousel.
- Keep Model Lab as a separate utility rather than mixing raw chat into the six
  grounded knowledge collections.
- Support multiple saved cards in each mode's carousel without mixing Word
  Origins, Word Cards, Answers, and Questions together.
- Make the browser display feel like the final full-screen product so it can be
  adapted to a 7.3–7.5 inch Waveshare color e-ink panel later.
- Let header and footer chrome collapse automatically when the display is idle
  and expand again on deliberate pointer, touch, or keyboard activity. Use the
  reclaimed space for the card instead of leaving dead bands.
- Keep the system tidy, neat, simple, robust, fast, capable, efficient, focused,
  and elegant—in roughly that order.

## Language quality observation

- A real local-model conversation about “sycophantic” showed that Chinese
  meaning output was useful and model speed was acceptable, although the raw
  terminal transcript displayed mojibake and the Cantonese pronunciation was
  incorrect. The product should preserve Unicode correctly and rely on grounded
  language data where precision matters.
- Good RAG evidence is expected to improve the already useful local-model
  output, especially for Word Origins.

## Voice and future hardware

- Investigate a suitable microphone module for the Raspberry Pi 5 knowledge
  terminal.
- Keep voice modular: capture, voice activity/noise handling, local speech
  recognition, RAG/Qwen, card rendering, and optional speech output should stay
  separable.
- Prefer a microphone path that is actually supported on Raspberry Pi 5 and is
  easy to recover in a headless device.
- E-ink hardware is not installed yet; retain a clean adapter boundary and add
  the panel implementation after the final Waveshare model is purchased.
