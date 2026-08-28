# Raspberry Pi 5 deployment handoff

Verified 2026-08-29 on the private Pi deployment. This file records revisions
and public-safe runtime facts only; credentials and private source files are not
stored in Git.

## Active runtime

| Layer | Verified revision/state |
|---|---|
| LKT implementation | `28a313b807441f6b29699951a2ecd36aa73764ab` |
| Model | `Qwen3-4B-Q4_K_M.gguf`, 2,497,280,256 bytes |
| Model SHA-256 | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| llama.cpp package | pinned `v0.3.0`, source commit `c1d0e7a004015f23bc0233470b747b596f29b264` |
| llama-server self-report | `0.3.0-dev`, GNU 12.2.0, Linux aarch64 |
| Kernel | Raspberry Pi aarch64 `6.6.51+rpt-rpi-2712` |
| Inference policy | one local slot, Qwen3-4B default, one idle-time deck item |

`lkt-web`, `lkt-llm`, and the low-priority `lkt-worker` were active.
`/api/health` returned `ready`, with the model reported as local and ready.

The live intent endpoint routed Word Card, Chat, Word Origin, Root, and Affix
inputs in about 1 ms. Repeated `inspection` and Answer #012 requests returned
their existing accepted card IDs in about 1 ms without inference. A real local
chat smoke test returned from Qwen3-4B in 5.54 seconds at 3.52 tokens/second.

## Real-data smoke results

- The autonomous worker prepared seven previously unseen real book records
  during deployment without a web request or hand-entered payload: Questions
  #257, #188, #258, and #250 plus Answers #096, #127, and #164. Each accepted
  card retains its exact stable source ID and reviewed EN/JA/ZH text; local
  Qwen generated the small title/reflection and then selected bounded vocabulary
  from the exact English source. The live decks reached six Answers and five
  Questions while the audit was running.
- Question #257 became card `cc8f6e34-c31c-4e86-84f0-5f8c089c8842`, titled
  “Future Travel Dilemma.” Its local investigation accepted the exact in-source
  terms `travel`, `companions`, and `future`. A 1920×1080 fullscreen audit showed
  seven clean language/sentence slides with no overlap or scrolling.
- The worker balances the percentage completed in each book, never repeats an
  accepted source entry, attempts one item per 120-second idle interval, and
  stops after all 609 reviewed book entries are accepted. Current undervoltage,
  throttling, or temperature at/above 78 C pauses background inference while
  leaving browsing available. During verification the Pi briefly reported
  `0x50005`; after recovery it reported historical-only `0x50000` at 56 C.
- The browser API now loads the complete selected mode up to 1,000 cards. Each
  tab keeps its newest card first and shuffles the rest once per carousel pass.
  Exactly one fullscreen Chromium top-level window was left open.
- The corrected accepted `breakthrough` Word Card is
  `948eecea-4ccc-4f05-b945-e004d06f9321`. Its Arabic translation was generated
  locally as `إنجاز نوعي`; strict script validation rejected earlier mixed
  output, and deterministic offline eSpeak supplied its saved pronunciation.

- Three accepted Answer cards were drawn from the real Book of Answers index:
  Answer #012 (“Learn to cherish”), Answer #031 (“Breathe fresh air”), and
  Answer #279 (“Unnecessary concession”). The newest answer opens first; the
  remaining answers are shuffled and the full-screen carousel advances every
  30 seconds without repeating a card within that pass.
- Accepted Question card `4b118a06-4820-466e-a69f-977773a9c62b` is reviewed
  Book Question #100 (“Technology and Sacrifice”), quality `0.95`. Its long
  text fits 1280×800 without scrolling as six automatic slides: two English,
  two Japanese with furigana, and two Chinese with pinyin. Sentence boundaries
  follow punctuation, and numeric counters such as `50万人` remain intact.
- `sync-card-knowledge` acquired all four accepted Answer/Question cards as 12
  independent reviewed EN/JA/ZH content atoms with book-owned evidence and
  typed translation edges. Running the migration twice left the same 12 atoms
  and 31 total knowledge edges, confirming idempotence. Reusing Question #100
  through the live API also preserved those counts.
- A real two-turn Qwen3-4B discussion of Question #100 created one durable
  inquiry thread and two events. The second event points to the first as its
  parent; both retain the Question card ID and its normalized English
  `content-item`. The replies completed in 39.97 seconds (3.01 tokens/second)
  and 7.52 seconds (2.56 tokens/second), respectively, while remaining uncited
  Model Lab observations rather than book claims.
- The accepted `inspection` Word Card uses one OMW sense plus reviewed atomic
  Japanese, Chinese, French, and Arabic outputs and offline pronunciation.
- The accepted `inspection` Word Origin card contains six nodes and five edges:
  `in-`, `spect`, `-ion`, Proto-Indo-European `*spek-`, Latin `specere`, and the
  modern word. Its source panel opens with Word Origins page 482, followed in
  stored provenance by the polished Root Dictionary and OMW sense.
- Independent accepted `inspection` Root and Affix cards reuse that graph at
  quality `0.8`. Root card `b07db9a2-e9a8-477e-8185-0b0eafbb673b` opens on
  `*spek- → specere → spect → inspection`; Affix card
  `5b323783-cfc0-4e18-876b-8d10b6ab99a0` has prefix, suffix, and overview
  slides. Both fit a 1280×800 screen without overlap; unrelated focus nodes are
  hidden from the main canvas and retained in the corner overview.
- The rebuilt LadybugDB projection contains 70 accepted nodes and 62 accepted
  edges, fingerprint
  `3ae9aa7ea8d9c440617343b25d15f7419bf51c059077c5aef70062df86e86f9f`.

## Browser entry points

- Bare/default ambient display: `http://127.0.0.1:8090/?display`
- Ambient Answer display: `http://127.0.0.1:8090/?mode=answer&display`
- Question display: `http://127.0.0.1:8090/?mode=question&display`
- Word Card display: `http://127.0.0.1:8090/?mode=knowledge&display`
- Word Origin display: `http://127.0.0.1:8090/?mode=word&display`
- Root display: `http://127.0.0.1:8090/?mode=root&display`
- Affix display: `http://127.0.0.1:8090/?mode=affix&display`

The browser GUI remains the only implemented output adapter. E-ink and audio
consume the same stored card contract later and are not imported into the core
service.

The bare ambient composer routes one English word to Word Card, a normal
sentence or question to local Chat, and explicit `origin:`, `root:`, or
`affix:` inquiries to their independent card modes. Tabs and `?mode=` URLs
remain exact overrides; intent routing does not call the model.

Root and Affix cards are composed as independent accepted views of the same
atomic origin graph. This replaces the retired monolithic Root request, which
exceeded four minutes without producing a publishable card.
