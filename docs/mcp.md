# Read-only MCP bridge

LKT's optional MCP bridge gives a local client deterministic access to the
accepted portion of one LKT knowledge ledger. It is a separate process from the
browser service and local-Qwen runtime. It uses the official Python MCP SDK 2.x
and does not invoke a language model.

## Install and run

Install the optional extra in a virtual environment:

```bash
python -m pip install -e '.[mcp]'
```

The `lkt-mcp` entry point uses `LKT_KNOWLEDGE_DB` by default. A database can be
selected explicitly without changing it:

```bash
lkt-mcp --database /path/to/knowledge.sqlite3 --transport stdio
lkt-mcp --database /path/to/knowledge.sqlite3 \
  --transport streamable-http --host 127.0.0.1 --port 8091 --path /mcp
```

Streamable HTTP defaults to `http://127.0.0.1:8091/mcp`. The bridge refuses a
non-loopback bind address because this first version does not implement client
authentication. Put any remote access behind a separately configured,
authenticated and encrypted boundary; do not publish this endpoint directly.

## Protocol surface

The server advertises exactly two tools:

- `query_private_knowledge`: multilingual exact/prefix/substring search over
  accepted SQLite atoms. It returns bounded facts, accepted relationship nodes
  and claims, evidence excerpts, opaque collection/source IDs, validated source
  hashes, safe relative locators, and deterministic result hashes.
- `trace_private_claim`: resolves one returned assertion or evidence ID to its
  accepted relationship and source provenance.

The `lkt://collections/status` resource reports bounded accepted counts,
languages, opaque collection IDs and validated source hashes. It contains no
database location or raw corpus/source-entry identifiers.

There are no MCP prompts and no tools for ingestion, generation, SQL, review,
acceptance, rejection, archival, deletion, or configuration. Inputs, results,
graph depth, nodes, claims, evidence records, locators, excerpts, collections,
and SQLite work are bounded. SQLite is opened with `mode=ro` and
`PRAGMA query_only=ON`; merely starting or querying the bridge does not
initialize or migrate the database.
`evidence_truncated`, `graph.truncated`, `languages_truncated`, and
`collections_truncated` explicitly mark projections that reached a boundary.
Only accepted entities and accepted assertions whose endpoints are accepted
are visible. Claims must also have source evidence; model-basis assertions,
internal entity/evidence payloads, and model-sourced evidence links are not
returned.

## Privacy boundary

“Local” describes where LKT stores and reads the ledger; it is not a promise
that an invoking client keeps data on the device. Every MCP invocation reveals
the query to this server, and every response can reveal matched private text,
source excerpts, opaque collection/source IDs, safe relative evidence locators,
and validated content hashes to that client. If a remote assistant reaches the bridge through a relay,
tunnel, skill, proxy, or cloud service, those queries and results leave the LKT
device and are then governed by that system's retention and privacy terms.

Do not store credentials or secrets as knowledge excerpts. Keep the bridge on
loopback unless an authenticated transport boundary and an explicit data policy
are in place. Raw corpus/source-entry identifiers, unsafe locators, filesystem
paths, environment variables, database payload JSON, model settings, and
credentials are not part of the MCP response schema.

This is a general MCP product feature. The repository does not currently
contain an Amazon Alexa skill, account-linking flow, Alexa+ integration, remote
gateway, consent UI, or certification evidence, and it makes no Alexa support
claim.

## Validation

`tests/test_mcp_query.py` builds its temporary knowledge ledger from the
project-owned PocketPolyglot fixture. The core tests run without the MCP extra;
SDK protocol tests run when `mcp` is installed and cover tool listing, calls,
read-only annotations, and resource reads.
