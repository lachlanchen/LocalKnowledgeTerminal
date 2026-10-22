# Carousel and fetch repair — 2026-10-22

This release repairs the apparent LKT stall without changing the RAG prompts,
publication gates, citations, or final card contract.

## Root causes

- The browser's 30-second deck refresh referenced an undefined variable. The
  resulting promise rejection stopped that refresh silently.
- A failed carousel fetch had no guaranteed successor timer, so one timeout
  could stop the ambient loop.
- Health statistics repeatedly scanned large operational tables. One missing
  index turned a correlated morphology lookup into a multi-minute refresh.
- Browser health, deck refresh, card hydration, and summaries fired together.
  Optional summary failure could discard an already hydrated card.
- Random-card hydration retried as many as eight live graph projections after
  the browser had already timed out.
- Worker lane choice used wall-clock modulo. A four-minute local-model job and
  two-minute scheduling interval could repeatedly select the same lane.
- Candidate discovery reopened and rescanned immutable source indexes for each
  attempt.

## Repair

- The ambient loop owns a watchdog and always rearms it after success, error,
  or a one-card dwell. Autoplay pause removes `loop=1` from the route.
- Next-mode hydration is prefetched once per target mode. Rapid interaction
  reuses it instead of starting abandoned projections.
- Card summaries are coalesced and cached. They refresh after hydration and are
  best effort, while the cached no-replacement queue still prioritizes newly
  accepted cards.
- Health and deck polling are staggered. Slow statistics use a bounded adaptive
  refresh interval while worker heartbeat and visible counts stay live.
- Server random hydration is limited to three candidates and an 18-second
  retry budget. A client disconnect cannot start an unbounded retry chain.
- Projection caches use a trigger-driven presentation revision. Meanings,
  translations, pronunciation, graph assertions, accepted artifacts, and
  citations invalidate it; queue leases and heartbeats do not.
- Durable scheduling alternates real planning decisions across growth lanes,
  surviving worker restarts and long inference calls.
- Corpus and morphology batches load source rows once while reproducing the
  former per-attempt hashed selection order. Evidence eligibility and output
  quality rules remain unchanged.
- Additive SQLite indexes cover card rotation, entity freshness, queue lookup,
  subject snapshots, and morphology job lookup. Startup tolerates the one-time
  index build and malformed legacy card JSON.

## Required verification

Run before deployment:

```bash
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
node --check lkt/static/app.js
```

On the Pi, stop web and worker before the first post-upgrade initialization so
the large card rotation index is built once. Then start model, web, and worker
in that order. Verify `/api/health`, all six random-card endpoints, one full
Question → Answer → Word Card → Word Origin → Root → Affix loop, and a card
whose citation resolves to a real local-book record.
