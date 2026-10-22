# Responsive graph disclosure release

## Deployed

The previously held UI candidate is now deployed to the Pi.
The web service started at `2026-09-13T05:22:14Z`; readiness was confirmed at
`2026-09-13T05:23:01Z`. The worker and local inference services were not restarted.

Deployed assets:

- `lkt/static/app.js`, SHA-256
  `9358f223345e6b62270623edc6c619cfe397dc9e7b492a3964861f4ed88ffd3b`.
- `lkt/static/app.css`, SHA-256
  `d25753a609eec68852ef552cfaf3a55f6f48fb34d05b403dabffff02cb029c17`.

The existing Pi assets matched the archived pre-candidate hashes before
replacement. A Pi-side source backup was taken; each new file was installed by
an adjacent temporary file and rename, and its checksum was checked.

The first immediate health request hit connection refusal while the restarted
web service was starting. The same service was checked again, without another
restart, and was active and ready. Do not interpret that startup probe failure
as a network reconfiguration or a terminated worker.

## Changes

- Preserve the established Word Card composition while applying the held
  responsive shell and measured fitting changes.
- Hide the graph minimap when it would cover a node; keep its bounds measurable
  so it can return after a pan or slide change.
- Present phone root/affix graphs as connected pages. Use two nodes on shorter
  phone viewports and three on taller ones, preserving the selected nodes and
  relationships rather than trimming the saved graph.
- Arrange compact morphology nodes vertically and rebuild page capacity when
  the viewport crosses the relevant size boundary.
- Keep desktop graph translations visible. On phones, put them behind a native
  `details` disclosure named "Translations", initially closed for a new card.
- Preserve the user's phone disclosure state while changing slides of the same
  card. Opened translations use a bounded, manually scrollable panel.
- Remove the redundant phone graph legend so the graph has useful vertical
  space. Word Card data and fullscreen composition are not redesigned.

## Validation

The latest change passed eight compact-page helper tests and 28 web tests.
The earlier candidate passed the complete 555-test Python suite and compileall;
that full suite was not rerun for this final CSS/JavaScript disclosure change.

The controlled candidate browser audit covered 63 sampled states at widths
320, 390, 900, and 1280. It loaded the candidate assets explicitly and checked
the requested page identity after settling. There were no JavaScript exceptions,
document-width overflows, clipped sampled graph nodes, minimap/node overlaps,
or clipped sampled Word Card language regions.

Phone translation disclosures opened by pointer, closed by keyboard, and
allowed manual scrolling. Their scroll viewport was 104 pixels high, with
250-283 pixels of content in the sampled root/affix cards.

| Phone root sample | Before disclosure change | After disclosure change |
| --- | ---: | ---: |
| Width 390 | 120.2 px graph height | 292.1 px |
| Width 320 | 4.2 px, then zero on later pages | 188.1 px |

The sampled root terms had minimum sizes of approximately 20.5 pixels at width
390 and 17.9 pixels at width 320. This is not a universal text-size guarantee:
some longer meanings and compact origin pages remain small.

One initial fullscreen automation attempt timed out while the card intercepted
the fullscreen button's pointer target. A fresh hit-target diagnostic succeeded.
A separate test using normal pointer movement before clicking controls passed
native fullscreen entry and exit for Word Cards and root graphs, both with the
candidate and on the deployed site. Six states passed on each run, with no
clipped sampled regions or JavaScript errors. No forced click was used. Preserve
the initial failed log rather than treating that attempt as a pass.

The deployed phone check is recorded separately in
`live-phone-disclosure.json`; use that report for its final result. Browser
screenshots on this Linux host lack CJK fonts, so square glyphs are not evidence
of missing database translations.

## Real-book publication after deployment

The uninterrupted worker published an origin card for `tact` at
`2026-09-13T05:24:02.305245+00:00`. Its saved graph has three nodes and two edges,
with Word Origins entry `entry-6209`, page 506.

It also published origin, root, and affix cards for `reprisal` around 05:24:07.
The origin card is `709b97ff-68a4-499b-9b75-f3e58989a2f7`, with six saved nodes
and five edges. Retrieved references include root dictionary
`p0176-t037-r006`, page 176, and affix dictionary `p0394-t003-r002`, page 394,
alongside the recorded Open Multilingual Wordnet entry.

These are actual saved publications from retrieved records. This observation
does not claim that a new translation inference was run for this UI release,
or that every saved linguistic relationship is correct.

At `2026-09-13T05:25:18.746366+00:00`, the worker remained ready with a fresh
heartbeat. The knowledge database reported 1,863 morphemes, 639 historical
forms, 626 history events, 1,196 translations, and 19,902 entity edges.

## Runtime and safeguards

- Pi Python: 3.11.2.
- Model: local `Qwen3-4B-Q4_K_M.gguf`, unchanged by this release.
- Last recorded model SHA-256, verified September 11:
  `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- Last recorded llama-server SHA-256:
  `da0eb5af88a1aac4a9f7a2247e90a49e651a8a45d8110fd97066a7c3f30e081c`.
- Runtime: llama.cpp `0.3.0-dev`, build 0; upstream commit unavailable.
- Configuration remains 3,072 context tokens, four threads, one inference slot.

No weights, books, database schema, network settings, DHCP settings, or adapters
were changed. No cloud inference, commit, or push was performed.

## Remaining acceptance work

- The book-evidence header still wraps poorly on very narrow screens.
- Long meanings on compact origin views and some short desktop graph canvases
  remain small. No-clipping checks alone do not prove readable typography.
- Inspect the actual relation kinds and retrieved evidence behind questionable
  origin chains, including the previously observed `valuable` view containing
  `precious` history. Do not assume layout fixes establish data correctness.
- Missing multilingual content, role consistency, sustained root/affix
  enrichment, and fair progress across the different queues still require
  broader acceptance evidence. Rising total edge counts do not establish all
  those requirements.
- Earlier repository/database publication requests are not established as
  complete by this UI release.

The original goal remains active.

## Handoff artifacts

Pi release, old assets, readiness, and real-book publication proof:
`/home/lachlan/LocalKnowledgeTerminal/runtime/graph-disclosure-20260913/`.

Windows-side backup:
`C:\Users\Administrator\LKT-backups\20260913-graph-disclosure\`.

Browser reports and screenshots are under `/tmp/lkt-browser-audit/`, including
`graph-viewport-regressions-disclosure.json`,
`candidate-fullscreen-user-path.json`, `live-fullscreen-user-path.json`, and
`live-phone-disclosure.json`. Test output is
`/tmp/lkt-graph-disclosure-tests.log`. Keep these rendered/runtime artifacts
outside Git.
