# Lexicon Atlas

A standalone, private 3D explorer for the Local Knowledge Terminal graph.
It reads a **consistent local copy** of the Pi's SQLite database. It does not
replace the card UI, alter the source database, run inference, or upload books.

## Run on Windows

Requires Node.js 24 or newer and Windows OpenSSH. From PowerShell:

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge\graph-explorer
npm ci
npm run sync
npm run build
npm start
```

Open **http://127.0.0.1:8091/**. Subsequent launches can use `launch.cmd`.
The same npm commands work on Linux with Node.js 24 and SSH.

`npm run sync` authenticates through your normal SSH configuration. Passwords
are never stored in the app. It uses SQLite's backup API on the Pi, streams
the consistent snapshot to this computer, checks it locally and records its
SHA-256. The temporary Pi snapshot is removed; the Pi worker stays running.

Every copy has a new filename. The manifest switches to the completed copy
only after validation, so a running Windows server never has to overwrite an
open SQLite file. Old local snapshots are retained. Remove obsolete copies
manually if disk space becomes an issue, keeping the file named in
`data/snapshot.json`.

To refresh: run `npm run sync` again, then click **Reload snapshot**. This is
deliberately not a live database connection or an automatic Pi write channel.

Optional settings:

```powershell
$env:LKT_PI_SSH = "lachlan@192.168.70.131"
$env:LKT_PI_DATABASE = "/home/lachlan/LocalKnowledgeTerminal/data/knowledge.sqlite3"
$env:LKT_ATLAS_PORT = "8091"
# Alternatively, open an existing local copy without the sync manifest:
$env:LKT_GRAPH_DB = "C:\path\to\knowledge.sqlite3"
```

Advanced SSH wrappers can set `LKT_SSH_COMMAND` and a JSON argument array in
`LKT_SSH_ARGS`. No password or private key belongs in these settings or Git.

## Explore

- Drag to orbit, right-drag to pan, and use the wheel to zoom.
- Search words, roots or meanings in any stored language. `/` focuses search.
- Click a point or search result for the actual record, relationships, source
  excerpts and preparation status. `F` fits the visible network.
- Isolate a one-, two- or three-hop neighborhood without changing the database.
- Filter layers, language, drafts, archived records and book-backed links.
- Switch between 3D and 2D, pause the layout, or enable a slow orbit.
- Export the visible graph as a local JSON download.
- On smaller screens, **Explore** opens the filters and selecting a node opens
  a dismissible inspector. Search provides keyboard access to graph records.

The default includes accepted and draft records, excludes archived/rejected
records, and shows materialized graph edges. Unmaterialized assertions are
available through an explicit toggle and are identified in the inspector.
There is **no card-sized node cap**. The status bar states how many records
filters hide. Isolated nodes can be kept or hidden explicitly.

Language is shown as metadata on a record, not manufactured as a language node.
Stored words and morphemes remain separate identities even when their labels
match. Duplicate support records are bundled by endpoints, relation and status;
the inspector preserves semantic relationships and their provenance. The GPU
draws one visual line per directed endpoint pair rather than overlapping arrows.
Selecting a node reveals direction arrows on its incident connections.

## Why a word such as abeyance can appear alone

The source snapshot examined on 2026-09-06 contains a whole-word free component
for `abeyance`, but its accepted origin artifact reports `no-safe-history`.
The model review proposed `abeyant` without a historical language code, so that
step was omitted. Its original redundant modern-word step was also removed.
The compact card renderer collapses the identical word/component wrapper;
there is no accepted historical chain behind that wrapper to draw.

The wider card projection can include a shared-component connection to
`abeyant`, depending on the view, but that is not an accepted historical chain.
Simply increasing the canvas size or changing the layout algorithm cannot
recover facts absent from accepted history data.

The atlas's generic **Knowledge gap** panel exposes saved origin outcomes,
omitted-step reasons and queued/failed preparation jobs for any selected node.
It does not fabricate history to make a sparse graph look fuller. Repairing the
underlying history remains the local-model/RAG worker's responsibility.

## Architecture and performance

- `lib/snapshot.mjs`: read-only SQLite attachment, compact graph serialization,
  record inspection and retrieved evidence. No LKT core import is required.
- `server.mjs`: loopback-only Node HTTP server, gzip graph responses, static
  built assets and read-only APIs. Database files are never served as assets.
- `src/layout.worker.js`: component-aware initialization and a bounded
  [D3 3D force simulation](https://github.com/vasturiano/d3-force-3d) in a Web
  Worker, with transferable position buffers. Layout work stays off the UI
  thread; changing filters does not restart the whole simulation.
- `src/scene.js`: [Three.js](https://threejs.org/) instanced node meshes and
  batched line geometry, not thousands of DOM elements or individual meshes.
  Labels are restricted to selection context, not all records simultaneously.
- `src/catalog.mjs`: pure search, taxonomy, filtering and neighborhood logic.
- `src/main.js` and `src/style.css`: responsive explorer and evidence inspector.

The build bundles the fonts and dependencies locally. After installation and
snapshot copying, normal browsing needs no CDN, external font service or cloud
model. The atlas is not Neo4j and does not require migrating the existing graph
into another database engine.

## APIs

```text
GET /api/health        Local snapshot identity and counts
GET /api/network       Full compact nodes and bundled relationship records
GET /api/node/:id       Stored record, connections, evidence and preparation state
```

The server binds to `127.0.0.1`, rejects non-loopback Host headers and foreign
Origin headers, has no mutating endpoints, and opens SQLite with `readOnly` and
`query_only`. Record text is escaped before insertion into HTML.

Evidence is read from `evidence_records`, `entity_evidence` and accepted
assertion evidence. It is not generated by the renderer or language model.
The inspector bounds evidence queries to 80 records and preparation history to
the latest 60 jobs; full underlying records remain in the private SQLite copy.

## Validation

```powershell
npm test
npm run build
```

Tests cover graph bundling, status filters, multilingual search, cyclic
neighborhoods, missing-history diagnostics, real stored evidence, read-only
SQL, path boundaries, HTTP methods, loopback access and markup escaping.

Before a repository commit, also run the root contributor checks:

```bash
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

## Repository boundary

Only app source, tests and dependency lockfiles are committed. `data/`, SQLite
copies, generated builds, screenshots, logs and `node_modules/` are ignored.
Database copying is not permission to publish the corpus or book excerpts.
Existing unrelated changes in the parent LKT checkout are not part of this app.
