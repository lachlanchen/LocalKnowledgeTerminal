# Read-only Markdown vault index

The Markdown adapter gives Local Knowledge Terminal a deliberately small
source-of-truth boundary: ordinary UTF-8 Markdown files remain canonical, while
a local SQLite database is a disposable search projection.

## Build and search

```bash
python -m lkt.cli ingest-markdown /path/to/vault \
  --database /private/runtime/markdown-vault.sqlite3
python -m lkt.cli search-markdown "source provenance" \
  --database /private/runtime/markdown-vault.sqlite3
```

The build walks `.md` files in stable relative-path order without following
symlinks. It excludes hidden files and directories plus `.obsidian`, `.git`,
`build`, `dist`, `generated`, `node_modules`, and `__pycache__`. It never writes
inside the vault.

Each heading-delimited section stores:

- a stable section ID derived from its relative path, heading, and start line;
- the exact relative path, heading level, line range, and source excerpt;
- the SHA-256 of the complete source file;
- explicit `[[target]]` or `[[target|label]]` links found in that section.

The metadata includes a sorted vault fingerprint over every indexed relative
path and file hash. A rebuild is created in a temporary database, checked, and
swapped into place only when the vault fingerprint is still unchanged. Invalid
UTF-8, schema failures, or a source change during the build leave the earlier
database available.

Concurrent rebuilds of the same destination are rejected through a sibling
operating-system lock file; unique staging databases prevent one build from
replacing another build's work.

## Boundary

The current adapter provides exact and full-text lexical retrieval. It does not
parse all Obsidian syntax, alter notes, resolve aliases to files, infer concepts,
build embeddings, answer questions, synchronize devices, or expose a vault over
the network. A collection-fit sprint can evaluate a representative Markdown
sample, but full-vault migration, automation, deployment, and support remain
separate scope.

The committed [project-owned proof](../examples/artifacts/markdown-vault-index.json)
shows both English and Chinese retrieval plus one explicit wikilink edge. It is
sample evidence, not a customer implementation or result.
