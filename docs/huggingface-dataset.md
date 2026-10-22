# Hugging Face lexical graph dataset

This document defines the public artifact and private-first release process for
the Local Knowledge Terminal multilingual lexical knowledge graph. The planned
dataset page is
[`lachlanchen/local-knowledge-terminal-lexical-graph`](https://huggingface.co/datasets/lachlanchen/local-knowledge-terminal-lexical-graph).
That link is a destination, not evidence that an authenticated release already
exists.

## Public artifact contract

Create one immutable release directory from the normalized knowledge database:

```bash
export DATASET_VERSION=2026.09.02
export EXPORT_DIR="dist/lexical-graph-${DATASET_VERSION}"

lkt export-knowledge "$EXPORT_DIR" \
  --database data/knowledge.sqlite3 \
  --dataset-version "$DATASET_VERSION"
```

The command always applies the accepted-only public policy. It has no option to
include private or unsafe rows. The resulting directory contains:

```text
lexical-graph-VERSION/
|-- README.md
|-- data/
|   |-- lkt-lexical-knowledge.sqlite3
|   |-- TABLE.jsonl
|   `-- ...
|-- manifest.json
`-- SHA256SUMS
```

`README.md` is a deterministic Hugging Face dataset card. Its YAML declares
`license: other`, the five first-class languages, and one named configuration
per JSONL table. Each configuration has one `train` split and exactly one
`data/TABLE.jsonl` path, so Hugging Face never combines the heterogeneous table
schemas. `data/lkt-lexical-knowledge.sqlite3` is the supplemental portable
relational graph. `data/` also contains one UTF-8 JSON Lines representation per
exported table.

`manifest.json` records the dataset/export/schema versions, source schema and
graph revision, logical-table row counts and hashes, languages, source-status
counts, and quarantine counts. Its `tables` object is authoritative for the
logical JSONL tables, not every physical release file. `SHA256SUMS` is the
authoritative path-and-hash list for every physical release file except itself,
including the dataset card, SQLite database, JSONL tables, and manifest.

## Public schema

The graph keeps lexical concepts normalized instead of flattening a word into
one generated document. Core tables cover entities, terms, meanings, morphemes,
term-component membership, historical forms and events, pronunciations,
translations, grammar analyses, typed entity edges, relation assertions,
metadata-only citations, and entity/assertion citation links. Stable entity IDs
connect the SQLite and JSONL forms. Only accepted nodes and accepted
relationships cross the export boundary.

Public schema v2 requires source relation-authority schema v10 and exposes the
correction system without exposing its private inputs. `relation_slot_winners`
maps every current accepted assertion to its
opaque semantic slot. `relation_correction_events` is the ordered append-only
lineage for safe relationship identities and state transitions, including
archived assertions clearly marked as non-current. `relation_claim_observations`
records bounded correction outcomes, and
`relation_claim_observation_incumbents` normalizes their incumbent links. These
tables contain opaque IDs, whitelisted relationship fields, confidence/state
transitions, evidence counts, re-hashed fingerprints, and policy versions. They
never contain source `before_json`, `after_json`, `proposal_json`, reasons, job
IDs, timestamps, prompts, model responses, or arbitrary property bags.

Export is fail-closed when an accepted assertion lacks a slot or an exclusive
slot has more than one accepted winner. An assertion-controlled entity edge is
quarantined unless its exact triple has an accepted, slotted assertion, matching
the schema-v10 central authority rule. A genuine legacy edge with no assertion
history is retained until central graph reconciliation creates its assertion,
so migration does not silently discard older useful knowledge.

Inspect a release without relying on an application library:

```bash
DB="$EXPORT_DIR/data/lkt-lexical-knowledge.sqlite3"

sqlite3 "$DB" '.tables'
sqlite3 "$DB" '.schema terms'
sqlite3 -header -column "$DB" \
  'SELECT language, COUNT(*) AS terms FROM terms GROUP BY language ORDER BY language;'
sqlite3 -json "$DB" 'SELECT * FROM entity_edges LIMIT 20;'
sqlite3 -json "$DB" 'SELECT * FROM relation_slot_winners LIMIT 20;'
sqlite3 -json "$DB" 'SELECT * FROM relation_correction_events LIMIT 20;'
head -n 1 "$EXPORT_DIR/data/terms.jsonl" | jq .
jq '{dataset_version, schema_version, source_graph_revision: .source.graph_revision, tables}' \
  "$EXPORT_DIR/manifest.json"
(cd "$EXPORT_DIR" && sha256sum -c SHA256SUMS)
```

Word Card, Word Origin, Root, and Affix are bounded projections over these same
records. They are not competing databases. English, Japanese, Chinese, French,
and Arabic are current first-class profiles, while language tags and typed
translation/form relations permit additional languages in later releases.

## Provenance and privacy

Public citations are metadata-only. They may identify a corpus, source title,
source version or content hash, safe record/page locator, and the source's
declared license status. They do not contain a retrieved excerpt, a scanned book
page, or source-book text. Metadata-only citations are provenance, not a grant
to redistribute text or derived lexical records. `entity_evidence`-style links
preserve which public atom a citation supports, while revision and basis fields
distinguish book or dictionary support from uncited local-Qwen knowledge.

The release excludes private corpus content, local filesystem paths, secrets,
model weights, prompts, raw model responses, rejected and draft candidates,
job queues, checkpoints, service/runtime state, saved cards, rendered artifacts,
inquiry conversations, and private audit logs. Never upload the live
`knowledge.sqlite3`; upload only the exporter-created directory.

Correction metadata is deliberately not a second current-facts table. Consumers
must use `relation_assertions` joined through `relation_slot_winners` for current
knowledge. An opaque assertion referenced only by a correction event or claim
observation is historical, rejected, pending, or superseded unless it is also
present in `relation_assertions`.

Acceptance is not a guarantee of correctness. The graph grows continuously,
and useful local-model output may be incomplete or wrong. Consumers should
honor confidence, basis, evidence, revision, and language fields. Absence from a
snapshot means only that the fact had not crossed the accepted public boundary
at that graph revision.

## Licensing status

Use `license: other` in the Hugging Face dataset card until every included
source and derivative-data obligation has been reviewed. Preserve each source's
own license metadata and mark it `unverified` when the project has not verified
it. Do not infer permission from availability, merge different source licenses
into a new blanket license, or claim that model transformation removes source
obligations. Never claim a source-code licence covers accompanying data without
asset-level evidence. A source with unresolved publication rights must be
removed before a public release even when its text excerpts are absent.

The repository and every candidate release remain private and gated while any
overlap or licence gate is unresolved. In particular, public release requires a
recorded review of the deployed artifact and version for each source: FreeDict
`eng-ara` is conservatively `GPL-2.0-only`; JMdict Japanese/English is
`CC-BY-SA-4.0` while other gloss languages need separate clearance; OMW English
must be matched to its Princeton/other English artifact; OMW French/WOLF is
recorded as `CECILL-C` version text `v1`; and eSpeak NG source/data is
conservatively `GPL-3.0-or-later` pending asset review. Private or proprietary
books have no redistribution grant. The detailed source matrix and primary URLs
are maintained in [corpora.md](corpora.md#public-redistribution-status).

## Private-first Hub release

Install a current `huggingface_hub`, sign in with a narrowly scoped write token,
and create the dataset as private. Do not place a token in this repository, the
export directory, a shell script, command-line arguments, or the dataset
manifest. `hf auth login` stores the credential outside the project.

```bash
python -m pip install --upgrade huggingface_hub
hf auth login

export HF_DATASET_REPO=lachlanchen/local-knowledge-terminal-lexical-graph
hf repos create "$HF_DATASET_REPO" \
  --repo-type dataset \
  --private \
  --exist-ok

hf upload "$HF_DATASET_REPO" "$EXPORT_DIR" . \
  --repo-type dataset \
  --revision main \
  --commit-message "Publish lexical graph ${DATASET_VERSION} for private review"
```

The current CLI command for folders is `hf upload`; do not use the deprecated
`hf upload-large-folder`. The equivalent Python upload uses `HfApi.upload_folder`:

```python
import os

from huggingface_hub import HfApi

repo_id = "lachlanchen/local-knowledge-terminal-lexical-graph"
export_dir = os.environ["EXPORT_DIR"]
version = os.environ["DATASET_VERSION"]

api = HfApi()
api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)
commit = api.upload_folder(
    repo_id=repo_id,
    repo_type="dataset",
    folder_path=export_dir,
    path_in_repo=".",
    revision="main",
    commit_message=f"Publish lexical graph {version} for private review",
)
print(commit.oid)
```

After private review, tag the exact reviewed `main` revision rather than
silently replacing a version:

```bash
hf repos tag create "$HF_DATASET_REPO" "v${DATASET_VERSION}" \
  --repo-type dataset \
  --revision main \
  --message "Sanitized lexical graph ${DATASET_VERSION}"
```

Keep the repository private until publication is explicitly authorized. Change
visibility only after the checklist is complete. Rotate or revoke the write
token after release if it was created solely for publishing.

## Release checklist

1. Export from the intended graph revision with a new immutable dataset version.
2. Confirm the CLI reports the same public manifest contract written to disk and no unsafe mode exists.
3. Confirm the generated dataset card has `license: other` and exactly one named configuration per JSONL table, including all four correction tables.
4. Verify every entry in `SHA256SUMS` and compare manifest row counts with SQLite.
5. Search all artifacts for corpus excerpts, private paths, usernames, tokens, prompts, and conversations.
6. Review evidence rows to ensure they contain metadata only and retain correct atom links.
7. Review every deployed source artifact's rights, version, attribution, and overlap status; keep the dataset private/gated with `other/unverified`, or remove unresolved material.
8. Run representative SQLite and JSONL queries for words, meanings, morphology, history, translations, provenance, slot winners, and correction lineage; confirm exclusive-slot collisions are zero.
9. Upload to a private dataset repository and inspect every table configuration, the rendered dataset card, and downloadable files.
10. Download the private release once, verify checksums again, and confirm it works without the LKT runtime.
11. Obtain explicit publication authorization, make the repository public, then tag the reviewed revision.
12. Record the public URL, dataset version, graph revision, manifest hash, and Hub commit in the release handoff.
