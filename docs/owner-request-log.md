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
- Later add separate books for English suffixes, affixes, and roots when those
  sources are ready.
- Treat Word Origin, Word Card, Book Answer, and Book Question as independent
  products with independent retrieval/prompt policies. Origin and Word Card may
  share Word Origins data but must present it differently.
- Save the model-prepared, formatted data in a local database. Repeating a
  request may run the model again and store another version rather than acting
  as a permanent cache.
- Keep acquired knowledge durable so the product grows into a local knowledge
  book, not a disposable chat session.

## Word Origin

- Word Origin is specifically an etymology experience, not a crowded generic
  card.
- Follow the real graph idea from `lachlanchen/WordOrigins`: use a proper
  JavaScript graph library and plot recursive ancestry/components as an actual
  directed graph.
- Branch the graph when a word contains more than one meaningful component;
  avoid presenting every origin as a decorative linear timeline.
- Use the Word Origins book reference plus model knowledge, and make the final
  graph visually proper, comprehensive enough to teach, and free of overlap.
- Show less surrounding material: a simple multilingual anchor and the main
  origin graph should dominate.

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
- Support multiple saved cards in each mode's carousel without mixing Word
  Origins, Word Cards, Answers, and Questions together.
- Make the browser display feel like the final full-screen product so it can be
  adapted to a 7.3–7.5 inch Waveshare color e-ink panel later.
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

