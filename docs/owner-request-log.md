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

## Investigation lineage and long-term history

- Treat meaningful words inside Answer and Question cards as investigation
  entry points. A follow-up can become a Word Card, Word Origin, Root, Affix,
  Answer, or Question without losing the relationship to its source card.
- Let the local model investigate an answer or question further in small,
  focused tasks; preserve the originating card, selected word or passage, and
  the resulting descendants as explicit lineage rather than an unstructured
  chat transcript.
- Save asking and investigation history for long-term use. Keep structured
  cards, raw model generations, cleaned intermediate artifacts, and compact
  history summaries distinct so storage can be managed without throwing away
  provenance.
- Provide search, archive, revision, deletion, and later compaction controls.
  Storage capacity is expected to be sufficient for years, but the database
  design must still avoid duplicating large immutable evidence or model blobs
  unnecessarily.

## Divide-and-conquer origin and language preparation

- Make divide-and-conquer the default preparation rule whenever outputs can be
  independently generated and validated. Run the small jobs sequentially on
  the Pi, persist successful stages immediately, retry only failed stages, and
  compose the final product after validation.
- Do not ask one model call to invent an entire Word Origin graph. First split
  the center word into candidate prefix, root, and suffix components; retrieve
  and validate every component independently against the Word Origins, Root,
  and Affix corpora.
- Walk history backward one attested step at a time for each accepted component.
  Recursively investigate meaningful parent forms, save each branch, deduplicate
  shared ancestors, reject cycles, and only then compose the complete graph.
  Preserve uncertainty and distinguish book-supported edges from model-led
  hypotheses at every step.
- Prepare English, Japanese, Chinese, Arabic, and rotating French output as
  separate small tasks rather than one multilingual response. Japanese must
  include token-level furigana, Chinese character-aligned pinyin, and Arabic
  transliteration plus grapheme-level segmentation.
- Give every language artifact its own prompt/model version, evidence,
  validation state, quality score, and revision lineage. Regenerating one weak
  language must not invalidate the graph or other accepted translations.
- Compose the final compact card only from the best accepted graph and language
  artifacts. This staged pipeline should work with 4B and may use 8B for slower
  offline preparation when the guarded real-data benchmark proves it stable.
- Promote validated components, historical forms, edges, and translations into
  a reusable local knowledge graph. Query established knowledge before calling
  a model, and generate only missing, stale, or rejected pieces.
- Canonicalize shared records so related words reuse the same root, affix, and
  historical nodes instead of duplicating them. Every reused artifact must keep
  provenance, source hashes, confidence, prompt/model version, validation state,
  and revision lineage so an established error can be corrected everywhere.
- Store established knowledge in a dedicated normalized SQLite database with
  separate tables for canonical terms and forms, pronunciation/phoneme/grapheme
  segments, meanings and senses, ordered morpheme links, etymology nodes/edges,
  dated history and semantic-change events, translations, grammar analyses and
  parts, typed properties, evidence, revisions, jobs, and investigation lineage.
- Keep history distinct from etymology: derivation and borrowing edges explain
  form ancestry, while dated history events can describe attestations, usage,
  and meaning shifts even when the written form does not change.
- Use one transactional knowledge database rather than a different SQLite file
  for every language. Language is data, so English, Japanese, Chinese, French,
  Arabic, and later languages can share the same extensible translation/form
  schema while retaining independent artifacts and validation.
- Treat cards as reconstructable views over established atomic knowledge. The
  database must support rebuilding a card without asking the model again.
- Build a resumable, low-priority preparation queue that eventually walks every
  reviewed Word Origins word/family, Root entry, Affix entry, Answer, and
  Question. Run one bounded job at a time, checkpoint after each accepted
  artifact, pause for interactive use, survive reboot, and skip source/model
  work that is already established and current.
- Let later model or validator versions revisit stale or low-quality artifacts.
  Answers and Questions may contribute vocabulary, grammar, and investigation
  candidates to established knowledge without crowding the visible card.
- Add compact open-source dictionary RAG for English, Japanese, Chinese, French,
  and Arabic. Prefer a shared multilingual WordNet layer plus high-quality
  language resources such as JMdict and CC-CEDICT; use processed Wiktionary
  selectively for gaps rather than placing every full dump on the 32 GB Pi.
- Track dictionary source, license, release/version, source hash, locator, and
  language in evidence. Keep each dictionary in its own rebuildable search
  index and promote only validated results into established knowledge.
- Keep dictionary RAG deliberately small: it exists to correct pronunciation,
  reading, pinyin, core meaning, and sense alignment, while the local model does
  explanation and card composition. Do not build a large dictionary zoo or
  download full Wiktionary dumps unless a demonstrated gap later requires it.
- Before the revised product becomes the default, audit existing cards and
  remove malformed, wrong-mode, uncited, stale-layout, or incorrect-language
  records from every visible tab/carousel. Rebuild current cards and slides from
  accepted atomic knowledge plus current RAG evidence; regenerate only missing
  or weak artifacts.
- Keep failed/raw generations outside visible collections only as a minimal
  provenance and debugging ledger until replacements are verified. Each tab
  must load only accepted cards of its own mode, and each inner slide must show
  the correct validated content rather than inherited dirty legacy payloads.

## Offline experience sequence

- Present the main product sequence as Question → Answer → Word Card → Word
  Origin → Root → Affix. This is a learning relationship: a prompt opens the
  experience, an answer follows, a selected word becomes a concise vocabulary
  card, then the terminal can reveal its origin and finally its roots/affixes.
- Question and Answer do not need strict synchronization. Independent or random
  draws are acceptable; a loose thematic relationship is enough when available.
- Keep the whole terminal simple and offline-first. Browsing prepared cards,
  dictionary correction, private-book RAG, and local Qwen inference must work
  without Internet access.
- Use Whisper Tiny as the later offline speech-input baseline. Local speech
  output is harder and is not required for the initial product.
- Prefer Qwen 4B as the practical default when compact dictionary correction and
  the retrieved source input give sufficient quality. Keep 8B as an optional
  slower preparation model rather than a product requirement.

## Interpreted dictation: mode responsibilities and future speech

The following points are preserved as interpreted product ideas because the
source was live dictation and may contain recognition errors:

- When microphone hardware is available, consider a full-screen listening view
  that shows transcription incrementally while the user speaks. Do not begin
  hardware integration before the microphone arrives.
- Question uses Question-book retrieval and presents the selected question with
  Japanese furigana and Chinese pinyin ruby. Answer independently draws from the
  Answer book; it may be random and need not be paired rigidly with Question.
- Word Card is the concise multilingual reading/meaning experience.
- Word Origin visualizes the history of the word and its constituent parts.
- Root and Affix each focus the graph on one central component, its related word
  family, and only the other roots/affixes necessary to explain the selected
  word. Keep these views distinct from the complete Word Origin history.
- Continue refining a calm, artistic, attractive one-page presentation while
  protecting readability and offline robustness.

## Polished Root and Affix RAG sources

- Use the completed polished/editorial versions of both the ROOT and AFFIX
  dictionaries in the Nutstore `Share/LLMRAG` editorial-polish workspace.
- Transfer only the finished JSON/JSONL needed to build retrieval indexes. Do
  not copy PDFs, TeX, page images, review tasks, or the whole book workspace to
  the Pi; storage efficiency matters.
- Treat these books as dynamic retrieval references. SQLite/JSONL is an
  implementation choice: retrieve a small relevant context, combine it with
  compact dictionary correction, and let local Qwen produce bounded dynamic
  results. Optimize for correctness, stable output, speed, and robustness.

## Restraint as a product principle

- Continue storing the owner's messages as durable product reference so design
  intent is not lost across implementation sessions.
- High-quality data comes from local Qwen plus book evidence plus dictionary
  correction, prepared as small validated tasks and retained with provenance.
- “Simple” must not mean incapable or incomplete. Keep the deep, reusable
  knowledge underneath, but reveal only the correct information needed for the
  current card or focused graph area.
- Prefer progressive disclosure, generous space, and deliberate detail over
  aggressive generation, greedy enrichment, crowded layouts, or too many facts
  on one slide.
- Keep the existing automatic header/footer hiding. The owner explicitly likes
  this behavior because it makes the terminal feel easy and gives the current
  card the full screen.
- Continue refining quietly from this foundation: keep the interface attractive
  and easy inside, without adding visible controls or detail merely because the
  underlying system can produce them.

## Default draw and intentional inquiry routing

- Treat the ambient/default terminal experience mainly as a random draw from
  the Book of Answers; it should feel immediate and require no prompt.
- User input is an intentional inquiry path. A general question may use the
  local question/chat workflow, while a word lookup can open Word Card and then
  branch into Word Origin, Root, or Affix views.
- Keep these paths as separate retrieval and presentation modes. Do not combine
  an answer draw, a general answer, a word card, and a morphology graph into one
  crowded response.

## Autonomous ownership and complete random loops

- Do not hand-enter generated knowledge or card text into the runtime database.
  Reviewed book/dictionary records remain the evidence, while new analysis,
  titles, reflections, vocabulary selection, translations, pronunciations, and
  composed card atoms are produced by the Pi's configured local model and
  deterministic local adapters.
- The terminal should work alone: gradually select unseen book records, prepare
  one bounded item at a time, validate it, save it, and continue across restarts.
  A failed or dirty result must stay out of the visible collection.
- Random playback must traverse all accepted items in the selected tab, not
  only a small recent subset. Keep each mode independent and avoid a repeat
  until the current shuffled pass is complete.
- Preserve the practical distinction between source and generation. Exact
  reviewed book text and citations are corpus-owned rather than model-generated;
  this is a correctness feature, not manual card editing.

## Oracle-style voice flow and compact origin annotations

- Treat the future microphone interaction as a calm fortune-card/oracle flow:
  the user asks aloud, transcription appears locally, and the terminal draws a
  random reviewed Book Answer as the full-screen response.
- When idle, ambient playback should eventually traverse tabs, each tab's saved
  cards, and each card's inner slides. Touch or deliberate navigation always
  takes priority over ambient motion.
- Preserve the current Word Origin graph layout; the owner explicitly considers
  it good. Keep ancestry/history clear and non-overlapping, and learn from the
  earlier `WordOrigins` language-labeled edges.
- Do not add separate multilingual graph slides. Keep English meaning primary
  and place one compact Japanese/Chinese/Arabic annotation line immediately
  beside or below that explanation.
- Keep the form and English explanation inside each Word Origin, Root, and
  Affix node as one fluid, naturally wrapping label centered both vertically
  and horizontally. Do not reserve rigid rows for the term or explanation; let
  the label use the node's available width and height.
- Treat root/prefix/suffix/word as structural metadata and the language code as
  language metadata. Float these as small opposite-corner annotations instead
  of spending normal node rows on `ROOT`, `AFFIX`, `EN`, `LA`, and similar tags.
- Data enrichment remains divide-and-conquer and missing-only. Never rerun all
  preparation merely to add a renderer feature; retain accepted atoms and
  backfill only the specific absent artifact when one is actually required.
- Keep outer card arrows and inner sentence/graph-focus arrows as two distinct
  navigation levels. Both already exist; refine their clarity without changing
  the successful layout.
- New annotations and histories remain local-Qwen output grounded by retrieved
  books/dictionaries, saved as bounded reusable JSON atoms. Rendering code may
  validate and lay out those atoms but must not hand-author runtime knowledge.
- A mode's saved cards may remain shuffled, but every card's inner sequence is
  deterministic and must finish before the next card: all English sentence
  slides, then all Japanese slides, then all Chinese slides, followed by the
  existing final Explore/word-teaching slide when available.
- Hold each inner slide for 18 seconds (twice the earlier 9 seconds). Derive the
  outer card dwell from the complete inner-slide count so no language or final
  teaching slide is cut off by an independent card timer.
- Distinguish navigation levels through motion as well as controls: inner
  sentence slides use a quiet lateral transition, while a new saved card uses a
  restrained whole-card fade/scale transition.
- The visible deck periodically reads only accepted cards. Do not interrupt the
  card currently being read; insert newly published cards directly after it so
  the newest ready result receives next priority, then resume the shuffled
  unseen deck. Never surface queued, running, rejected, or dirty candidates.
- Background completion covers both finite Answer/Question books and lexical
  views. The lexical seeder selects one unseen simple Word Origins headword only
  when the atomic queue is idle, then reuses one accepted atomic plan across
  Word Card, Word Origin, and any derived Root/Affix views. Book and lexical
  turns alternate so neither product family monopolizes idle inference.
- A term already accepted, queued, or attempted is excluded from autonomous
  selection. This is intentionally missing-only: a failed term cannot create an
  endless requeue loop, and existing accepted knowledge is never regenerated.

## Continuity and durable intent

- When work is interrupted by new product thoughts, resume from the last
  verified checkpoint instead of restarting or discarding accepted work.
- Re-read and preserve the owner's complete LKT message history as product
  reference. Record new intent here before relying on temporary conversation
  context, then continue the established implementation plan.
- Keep progress incremental, tested, committed, pushed, and deployed. Large
  stable changes deserve an immediate durable checkpoint so local knowledge,
  source provenance, and design decisions are not lost.

## Ruby breathing room

- Furigana/pinyin ruby and its base text must not feel compressed together.
  Reserve a visible annotation gap, give neighboring ruby groups modest
  horizontal air, and keep the main characters legible without sacrificing the
  one-screen, no-scroll card boundary.
- The first spacing pass was still too tight. Move the annotation itself away
  from the base glyph, not only the surrounding line box, while retaining
  enough reserved height to prevent clipping.
