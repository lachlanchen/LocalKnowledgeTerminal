# Compact graph layout: candidate held for phone height correction

## Deployment status

This UI candidate is local only. Neither `lkt/static/app.js` nor
`lkt/static/app.css` was deployed during this step. The Pi continues running the
previous production UI and the deployed language-completion scaling fix.

Do not treat this handoff, passing helper tests, or the absence of JavaScript
exceptions as proof that the layout is ready. The real browser audit found
clipped phone graphs.

## Changes in the local candidate

- Hide the minimap when its rectangle would cover a visible node, with eight
  pixels of clearance. Using visibility preserves its measurable bounds so it
  can return when the graph moves or the slide changes.
- Split phone root/affix branches into connected pages of at most three nodes.
  The page builder preserves the source slides' selected nodes and edges,
  removes duplicate pages, and does not mutate the saved graph.
- Prefer a root/affix and a connected derived word on the first compact page.
  Long history chains remain accessible on subsequent connected pages.
- Preserve genuinely isolated source nodes without inventing relationships.
- Use vertically arranged rows for portrait graph canvases and smaller compact
  graph padding. Crossing the phone breakpoint rebuilds root/affix pages.
- Add eight production-helper tests in `tests/test_compact_morphology_pages.py`.

These changes build on the previously held responsive layout candidate. They
do not redesign the Word Card composition or change model inference.

## Evidence gathered

All eight new helper tests passed locally. They cover node and edge coverage,
connectedness, derived-word visibility, duplicate slides, disconnected input,
isolated input, explicit edge selection, and a two-node capacity.

The existing candidate preview harness confirmed that the responsive shell
prevents document-width overflow at 320, 390, 900, and 1280 pixels for its
sampled cards. That harness does not establish text readability.

The new viewport audit loaded the candidate CSS and JavaScript, visited 67
sampled graph/card states across those widths, and reported:

- No JavaScript exceptions.
- No measured minimap/node overlap.
- No measured Word Card language-block clipping in the sampled cases.
- Twenty-four clipped root/affix states at width 320.
- Very small meanings remain on the 390-pixel graph even without clipping.

The first experimental probe, `graph-clearance-audit.cjs`, used the wrong
`/static/` interception pattern. Its output labelled candidate actually used
production assets. Do not use it as candidate validation. The authoritative
viewport audit requires confirmed CSS and JavaScript interception.

The full Python suite was also launched. Its completion result must be taken
from `/tmp/lkt-graph-clearance-full.log` and its running exec handle, not inferred
from the eight focused passes. Do not restart it merely because an observation
times out.

## Remaining layout cause

Measured first root page:

| Viewport width | Graph height | Translation annotation height |
| --- | ---: | ---: |
| 1280 | 397.8 px | 73.0 px |
| 390 | 120.2 px | 161.4 px |
| 320 | 4.2 px, then 0 on subsequent pages | 173.4 px |

The four translation annotation blocks in `#graph-focus-annotations` wrap into
two tall rows on phones. Together with the graph legend, heading, controls,
and book-evidence panel, they consume the available height. Pagination alone
cannot correct this layout budget.

Next correction should preserve access to translations with a compact
disclosure or bounded strip, rather than allowing them to collapse the graph.
The redundant phone graph legend can also yield space. Short phone viewports
may need two-node pages and vertical layout even when the remaining canvas is
slightly wider than it is tall. Keep the desktop/fullscreen Word Card design.

One long browser pass requested a later root page but measured page zero;
check accepted-deck refresh behavior and strengthen the harness to confirm the
requested page identity after settling. Also add an actual text-size acceptance
criterion: no clipping by itself is insufficient.

The Linux screenshot environment lacks CJK fonts. Square glyphs in these
screenshots do not establish missing Chinese or Japanese database content.

## Runtime observation

At approximately `2026-09-13T04:54:06Z`, the Pi worker remained ready and its
heartbeat was fresh. The knowledge database reported 1,863 morphemes, 636
historical forms, 623 history events, and 19,882 entity edges, compared with
1,862, 632, 619, and 19,856 in the preceding handoff snapshot.

The latest observed language dispatch was a Chinese pronunciation job at
04:49:23. No new accepted translation after the scaling deployment was found
by the two-record translation probe. An earlier Japanese pronunciation job
also saved an accepted translation artifact at 04:12:54 using local Qwen; this
predates the scaling deployment and is not new postdeployment inference proof.

The live `valuable` origin view contains `pretiōsus`, `precios`, and `precious`.
Its actual relation kinds and retrieved source attribution need a semantic
review before deciding whether this is a valid contextual association or an
incorrect origin chain. Layout checks cannot settle that question.

## Artifacts and safeguards

- Browser report: `/tmp/lkt-browser-audit/graph-viewport-regressions.json`.
- Browser runner: `/tmp/lkt-browser-audit/graph-viewport-regressions.cjs`.
- Screenshots: `/tmp/lkt-browser-audit/viewport-<mode>-<width>.png`.
- Focused tests: `/tmp/lkt-graph-clearance-tests.log`.
- Full tests: `/tmp/lkt-graph-clearance-full.log`.
- Before-edit source archive: `/tmp/lkt-graph-clearance-before-20260913.tar.gz`.
- Persistent copies: `C:\Users\Administrator\LKT-backups\20260913-compact-graph\`.

No database mutation, service restart, network change, commit, or push was
performed in this UI step. The broader goal remains active.
