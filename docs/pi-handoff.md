# Raspberry Pi 5 deployment handoff

Verified 2026-08-29 on the private Pi deployment. This file records revisions
and public-safe runtime facts only; credentials and private source files are not
stored in Git.

## Active runtime

| Layer | Verified revision/state |
|---|---|
| LKT source | `8284155fe85398bb9306ce7104aaceaba4325f35` |
| Model | `Qwen3-4B-Q4_K_M.gguf`, 2,497,280,256 bytes |
| Model SHA-256 | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| llama.cpp package | pinned `v0.3.0`, source commit `c1d0e7a004015f23bc0233470b747b596f29b264` |
| llama-server self-report | `0.3.0-dev`, GNU 12.2.0, Linux aarch64 |
| Kernel | Raspberry Pi aarch64 `6.6.51+rpt-rpi-2712` |
| Inference policy | one local slot, Qwen3-4B default |

`lkt-web` and `lkt-llm` were both active. `/api/health` returned `ready`, with
the model reported as local and ready.

The live intent endpoint routed Word Card, Chat, Word Origin, Root, and Affix
inputs in about 1 ms. Repeated `inspection` and Answer #012 requests returned
their existing accepted card IDs in about 1 ms without inference. A real local
chat smoke test returned from Qwen3-4B in 5.54 seconds at 3.52 tokens/second.

## Real-data smoke results

- Three accepted Answer cards were drawn from the real Book of Answers index:
  Answer #012 (“Learn to cherish”), Answer #031 (“Breathe fresh air”), and
  Answer #279 (“Unnecessary concession”). The newest answer opens first; the
  remaining answers are shuffled and the full-screen carousel advances every
  30 seconds without repeating a card within that pass.
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
- The rebuilt LadybugDB projection contains 21 accepted nodes and 20 accepted
  edges, fingerprint
  `35ee27b42a50af7816e0719a4ff99326a09383abff857e52452a2c243646e45f`.

## Browser entry points

- Bare/default ambient display: `http://127.0.0.1:8090/?display`
- Ambient Answer display: `http://127.0.0.1:8090/?mode=answer&display`
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
