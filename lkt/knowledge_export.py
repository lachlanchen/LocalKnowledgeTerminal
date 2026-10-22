"""Deterministic, public-safe export of the lexical knowledge graph.

The internal knowledge database deliberately contains operational state and raw
retrieval material.  This module publishes a smaller, fixed contract instead of
copying that database.  Public identifiers are opaque and stable, provenance is
metadata-only, and no arbitrary JSON property bag crosses the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


PUBLIC_SCHEMA_VERSION = "2"
EXPORT_VERSION = "2"


class PublicExportError(RuntimeError):
    """Raised when a public export cannot be proven safe and consistent."""


@dataclass(frozen=True)
class ExportManifest:
    """Structured result returned after a successful public export."""

    output_directory: Path
    schema_version: str
    export_version: str
    dataset_version: str
    source_schema_version: str
    source_graph_revision: int
    counts: Mapping[str, int]
    languages: tuple[str, ...]
    source_statuses: Mapping[str, int]
    quarantine_counts: Mapping[str, int]
    table_sha256: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "dataset_version": self.dataset_version,
            "export_version": self.export_version,
            "languages": list(self.languages),
            "quarantine_counts": dict(self.quarantine_counts),
            "schema_version": self.schema_version,
            "source": {
                "graph_revision": self.source_graph_revision,
                "schema_version": self.source_schema_version,
            },
            "source_statuses": dict(self.source_statuses),
            "tables": {
                table: {
                    "logical_sha256": digest,
                    "path": f"data/{table}.jsonl",
                    "rows": self.counts[table],
                }
                for table, digest in sorted(self.table_sha256.items())
            },
        }


_ENTITY_TABLES = {
    "term": "terms",
    "meaning": "meanings",
    "morpheme": "morphemes",
    "pronunciation": "pronunciations",
    "historical-form": "historical_forms",
    "history-event": "history_events",
    "translation": "translations",
    "grammar-analysis": "grammar_analyses",
}

_ALLOWED_RELATIONS = frozenset(
    {
        "allomorph-of",
        "derived-from",
        "developed-into",
        "developed-toward",
        "explains-component",
        "has-component",
        "has-grammar",
        "has-history",
        "has-meaning",
        "has-origin-event",
        "has-pronunciation",
        "has-translation",
        "historical-form-of",
        "opposed-to",
        "renders-as",
        "reviewed-translation",
        "shares-component",
        "translated-from",
    }
)
_ALLOWED_BASES = frozenset({"book", "dictionary", "mixed", "model", "reviewed"})
_ALLOWED_MORPHEME_KINDS = frozenset({"free", "prefix", "root", "suffix", "unknown"})
_EVIDENCE_PROPERTY_KEYS = frozenset(
    {
        "citation_evidence_ids",
        "context_evidence_ids",
        "dictionary_evidence_ids",
        "evidence_ids",
    }
)

_TABLE_ORDER = (
    "entities",
    "terms",
    "meanings",
    "morphemes",
    "term_morphemes",
    "pronunciations",
    "phoneme_segments",
    "historical_forms",
    "history_events",
    "translations",
    "grammar_analyses",
    "grammar_parts",
    "entity_edges",
    "relation_assertions",
    "relation_slot_winners",
    "relation_correction_events",
    "relation_claim_observations",
    "relation_claim_observation_incumbents",
    "citations",
    "entity_citations",
    "assertion_citations",
)

_PUBLIC_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "entities": ("entity_id", "entity_type", "quality_score"),
    "terms": ("entity_id", "language", "text", "normalized", "kind"),
    "meanings": (
        "entity_id",
        "subject_entity_id",
        "term_id",
        "language",
        "definition",
        "part_of_speech",
        "register_label",
        "domain_label",
        "sense_order",
    ),
    "morphemes": ("entity_id", "language", "form", "normalized", "kind", "meaning"),
    "term_morphemes": ("term_id", "morpheme_id", "ordinal", "surface", "confidence", "basis"),
    "pronunciations": ("entity_id", "term_id", "language", "system", "reading", "dialect"),
    "phoneme_segments": ("segment_id", "pronunciation_id", "ordinal", "grapheme", "phoneme", "syllable"),
    "historical_forms": (
        "entity_id",
        "language",
        "form",
        "normalized",
        "period_label",
        "date_min",
        "date_max",
        "meaning",
    ),
    "history_events": (
        "entity_id",
        "subject_entity_id",
        "event_type",
        "language",
        "period_label",
        "date_min",
        "date_max",
        "description",
    ),
    "translations": (
        "entity_id",
        "source_term_id",
        "source_meaning_id",
        "target_language",
        "target_term_id",
        "text",
        "normalized",
        "transliteration",
        "usage_note",
    ),
    "grammar_analyses": ("entity_id", "subject_entity_id", "language", "analysis_type", "summary"),
    "grammar_parts": (
        "part_id",
        "analysis_id",
        "ordinal",
        "surface",
        "lemma",
        "role",
        "part_of_speech",
        "reading",
    ),
    "entity_edges": ("edge_id", "source_entity_id", "target_entity_id", "relation", "basis", "confidence"),
    "relation_assertions": (
        "assertion_id",
        "subject_entity_id",
        "source_entity_id",
        "target_entity_id",
        "relation",
        "basis",
        "confidence",
    ),
    "relation_slot_winners": (
        "slot_id",
        "assertion_id",
        "slot_mode",
        "policy_version",
    ),
    "relation_correction_events": (
        "correction_id",
        "decision_id",
        "assertion_id",
        "event_order",
        "is_current",
        "subject_entity_id",
        "source_entity_id",
        "target_entity_id",
        "relation",
        "action",
        "before_basis",
        "after_basis",
        "before_confidence",
        "after_confidence",
        "before_status",
        "after_status",
        "before_revision",
        "after_revision",
        "exact_evidence_count",
        "context_evidence_count",
        "input_fingerprint",
        "output_fingerprint",
        "validator_version",
    ),
    "relation_claim_observations": (
        "observation_id",
        "slot_id",
        "observation_order",
        "slot_mode",
        "proposed_assertion_id",
        "subject_entity_id",
        "source_entity_id",
        "target_entity_id",
        "relation",
        "basis",
        "confidence",
        "decision",
        "exact_evidence_count",
        "context_evidence_count",
        "input_fingerprint",
        "output_fingerprint",
        "epoch_fingerprint",
        "model_pass_version",
        "policy_version",
    ),
    "relation_claim_observation_incumbents": (
        "observation_id",
        "incumbent_assertion_id",
    ),
    "citations": (
        "citation_id",
        "source_id",
        "source_entry_id",
        "locator",
        "source_fingerprint",
        "license_status",
    ),
    "entity_citations": ("entity_id", "citation_id", "support_role"),
    "assertion_citations": ("assertion_id", "citation_id", "support_role"),
}

_SORT_KEYS: Mapping[str, tuple[str, ...]] = {
    table: columns[:1] for table, columns in _PUBLIC_COLUMNS.items()
}
_SORT_KEYS = {
    **_SORT_KEYS,
    "term_morphemes": ("term_id", "ordinal", "morpheme_id"),
    "entity_citations": ("entity_id", "citation_id", "support_role"),
    "assertion_citations": ("assertion_id", "citation_id", "support_role"),
    "relation_slot_winners": ("slot_id", "assertion_id"),
    "relation_correction_events": ("assertion_id", "event_order", "correction_id"),
    "relation_claim_observations": ("slot_id", "observation_order", "observation_id"),
    "relation_claim_observation_incumbents": ("observation_id", "incumbent_assertion_id"),
}

_SOURCE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "entities": ("entity_id", "entity_type", "status", "quality_score"),
    "terms": ("entity_id", "language", "text", "normalized", "kind"),
    "meanings": (
        "entity_id",
        "subject_entity_id",
        "term_id",
        "language",
        "definition",
        "part_of_speech",
        "register_label",
        "domain_label",
        "sense_order",
    ),
    "morphemes": ("entity_id", "language", "form", "normalized", "kind", "meaning"),
    "term_morphemes": ("term_id", "morpheme_id", "ordinal", "surface", "confidence", "basis"),
    "pronunciations": ("entity_id", "term_id", "language", "system", "reading", "dialect"),
    "phoneme_segments": ("segment_id", "pronunciation_id", "ordinal", "grapheme", "phoneme", "syllable"),
    "historical_forms": (
        "entity_id",
        "language",
        "form",
        "normalized",
        "period_label",
        "date_min",
        "date_max",
        "meaning",
    ),
    "history_events": (
        "entity_id",
        "subject_entity_id",
        "event_type",
        "language",
        "period_label",
        "date_min",
        "date_max",
        "description",
    ),
    "translations": (
        "entity_id",
        "source_term_id",
        "source_meaning_id",
        "target_language",
        "target_term_id",
        "text",
        "normalized",
        "transliteration",
        "usage_note",
    ),
    "grammar_analyses": ("entity_id", "subject_entity_id", "language", "analysis_type", "summary"),
    "grammar_parts": (
        "part_id",
        "analysis_id",
        "ordinal",
        "surface",
        "lemma",
        "role",
        "part_of_speech",
        "reading",
    ),
    "entity_edges": (
        "edge_id",
        "source_entity_id",
        "target_entity_id",
        "relation",
        "basis",
        "confidence",
        "status",
        "properties",
    ),
    "relation_assertions": (
        "assertion_id",
        "subject_entity_id",
        "source_entity_id",
        "target_entity_id",
        "relation",
        "basis",
        "confidence",
        "status",
        "properties_json",
    ),
    "relation_assertion_slots": (
        "assertion_id",
        "slot_key",
        "slot_mode",
        "policy_version",
        "created_at",
    ),
    "relation_assertion_events": (
        "event_id",
        "decision_id",
        "assertion_id",
        "action",
        "before_json",
        "after_json",
        "evidence_ids_json",
        "context_evidence_ids_json",
        "input_fingerprint",
        "output_fingerprint",
        "job_id",
        "reason",
        "validator_version",
        "created_at",
    ),
    "relation_claim_observations": (
        "observation_id",
        "slot_key",
        "slot_mode",
        "proposed_assertion_id",
        "incumbent_assertion_ids_json",
        "proposal_json",
        "decision",
        "exact_evidence_ids_json",
        "context_evidence_ids_json",
        "input_fingerprint",
        "output_fingerprint",
        "epoch_fingerprint",
        "prompt_version",
        "job_id",
        "reason",
        "policy_version",
        "created_at",
    ),
    "assertion_evidence": ("assertion_id", "evidence_id"),
    "entity_evidence": ("entity_id", "evidence_id", "claim", "confidence"),
    "evidence_records": (
        "evidence_id",
        "corpus_id",
        "source_entry_id",
        "source_hash",
        "locator",
        "excerpt",
        "payload",
    ),
}

_DDL = """
PRAGMA foreign_keys = ON;
PRAGMA application_id = 1280001073;
PRAGMA user_version = 2;
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    quality_score REAL,
    CHECK (entity_type IN ('term','meaning','morpheme','pronunciation','historical-form','history-event','translation','grammar-analysis'))
) WITHOUT ROWID;
CREATE TABLE terms (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    language TEXT NOT NULL,
    text TEXT NOT NULL,
    normalized TEXT NOT NULL,
    kind TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE meanings (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    term_id TEXT REFERENCES terms(entity_id),
    language TEXT NOT NULL,
    definition TEXT NOT NULL,
    part_of_speech TEXT NOT NULL,
    register_label TEXT NOT NULL,
    domain_label TEXT NOT NULL,
    sense_order INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE morphemes (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    language TEXT NOT NULL,
    form TEXT NOT NULL,
    normalized TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('free','prefix','root','suffix','unknown')),
    meaning TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE term_morphemes (
    term_id TEXT NOT NULL REFERENCES terms(entity_id),
    morpheme_id TEXT NOT NULL REFERENCES morphemes(entity_id),
    ordinal INTEGER NOT NULL,
    surface TEXT NOT NULL,
    confidence REAL NOT NULL,
    basis TEXT NOT NULL,
    PRIMARY KEY (term_id, ordinal, morpheme_id)
) WITHOUT ROWID;
CREATE TABLE pronunciations (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    term_id TEXT NOT NULL REFERENCES terms(entity_id),
    language TEXT NOT NULL,
    system TEXT NOT NULL,
    reading TEXT NOT NULL,
    dialect TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE phoneme_segments (
    segment_id TEXT PRIMARY KEY,
    pronunciation_id TEXT NOT NULL REFERENCES pronunciations(entity_id),
    ordinal INTEGER NOT NULL,
    grapheme TEXT NOT NULL,
    phoneme TEXT NOT NULL,
    syllable TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE historical_forms (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    language TEXT NOT NULL,
    form TEXT NOT NULL,
    normalized TEXT NOT NULL,
    period_label TEXT NOT NULL,
    date_min INTEGER,
    date_max INTEGER,
    meaning TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE history_events (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    event_type TEXT NOT NULL,
    language TEXT NOT NULL,
    period_label TEXT NOT NULL,
    date_min INTEGER,
    date_max INTEGER,
    description TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE translations (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    source_term_id TEXT NOT NULL REFERENCES terms(entity_id),
    source_meaning_id TEXT REFERENCES meanings(entity_id),
    target_language TEXT NOT NULL,
    target_term_id TEXT REFERENCES terms(entity_id),
    text TEXT NOT NULL,
    normalized TEXT NOT NULL,
    transliteration TEXT NOT NULL,
    usage_note TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE grammar_analyses (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    language TEXT NOT NULL,
    analysis_type TEXT NOT NULL,
    summary TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE grammar_parts (
    part_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES grammar_analyses(entity_id),
    ordinal INTEGER NOT NULL,
    surface TEXT NOT NULL,
    lemma TEXT NOT NULL,
    role TEXT NOT NULL,
    part_of_speech TEXT NOT NULL,
    reading TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE entity_edges (
    edge_id TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    relation TEXT NOT NULL,
    basis TEXT NOT NULL,
    confidence REAL NOT NULL
) WITHOUT ROWID;
CREATE TABLE relation_assertions (
    assertion_id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    relation TEXT NOT NULL,
    basis TEXT NOT NULL,
    confidence REAL NOT NULL
) WITHOUT ROWID;
CREATE TABLE relation_slot_winners (
    slot_id TEXT NOT NULL,
    assertion_id TEXT PRIMARY KEY REFERENCES relation_assertions(assertion_id),
    slot_mode TEXT NOT NULL CHECK (slot_mode IN ('exclusive','set-member','none')),
    policy_version TEXT NOT NULL
) WITHOUT ROWID;
CREATE UNIQUE INDEX relation_slot_one_exclusive_winner
    ON relation_slot_winners(slot_id) WHERE slot_mode = 'exclusive';
CREATE TABLE relation_correction_events (
    correction_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    is_current INTEGER NOT NULL CHECK (is_current IN (0,1)),
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    relation TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('baseline','accept','update','keep','archive','reject')),
    before_basis TEXT NOT NULL,
    after_basis TEXT NOT NULL,
    before_confidence REAL,
    after_confidence REAL,
    before_status TEXT NOT NULL,
    after_status TEXT NOT NULL,
    before_revision INTEGER,
    after_revision INTEGER,
    exact_evidence_count INTEGER NOT NULL CHECK (exact_evidence_count >= 0),
    context_evidence_count INTEGER NOT NULL CHECK (context_evidence_count >= 0),
    input_fingerprint TEXT NOT NULL,
    output_fingerprint TEXT NOT NULL,
    validator_version TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE relation_claim_observations (
    observation_id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL,
    observation_order INTEGER NOT NULL CHECK (observation_order >= 0),
    slot_mode TEXT NOT NULL CHECK (slot_mode IN ('exclusive','set-member','none')),
    proposed_assertion_id TEXT NOT NULL,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    relation TEXT NOT NULL,
    basis TEXT NOT NULL,
    confidence REAL NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('pending','keep','conflict','supersede','coexist')),
    exact_evidence_count INTEGER NOT NULL CHECK (exact_evidence_count >= 0),
    context_evidence_count INTEGER NOT NULL CHECK (context_evidence_count >= 0),
    input_fingerprint TEXT NOT NULL,
    output_fingerprint TEXT NOT NULL,
    epoch_fingerprint TEXT NOT NULL,
    model_pass_version TEXT NOT NULL,
    policy_version TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE relation_claim_observation_incumbents (
    observation_id TEXT NOT NULL REFERENCES relation_claim_observations(observation_id),
    incumbent_assertion_id TEXT NOT NULL,
    PRIMARY KEY (observation_id, incumbent_assertion_id)
) WITHOUT ROWID;
CREATE TABLE citations (
    citation_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    locator TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    license_status TEXT NOT NULL CHECK (license_status = 'unverified')
) WITHOUT ROWID;
CREATE TABLE entity_citations (
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    citation_id TEXT NOT NULL REFERENCES citations(citation_id),
    support_role TEXT NOT NULL CHECK (support_role IN ('exact','context')),
    PRIMARY KEY (entity_id, citation_id)
) WITHOUT ROWID;
CREATE TABLE assertion_citations (
    assertion_id TEXT NOT NULL REFERENCES relation_assertions(assertion_id),
    citation_id TEXT NOT NULL REFERENCES citations(citation_id),
    support_role TEXT NOT NULL CHECK (support_role IN ('exact','context')),
    PRIMARY KEY (assertion_id, citation_id)
) WITHOUT ROWID;
"""

_DATASET_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}\Z")
_BCP47_RE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z")
_SAFE_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+:'()-]{0,79}\Z")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/]|(?:^|[\s\"'])/(?:home|users|mnt|root|etc|var|tmp)/|file://|ssh://)"
)
_SECRET_RE = re.compile(
    r"(?i)(?:password|passwd|api[_ -]?key|access[_ -]?token|client[_ -]?secret|private[_ -]?key)\s*[:=]\s*[^\s,;]+"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_RAW_KEYS = frozenset(
    {
        "accepted_artifact_id",
        "artifact",
        "after_json",
        "before_json",
        "claim",
        "created_at",
        "excerpt",
        "file_path",
        "model_output",
        "payload",
        "prompt",
        "proposal_json",
        "properties",
        "properties_json",
        "raw_content",
        "raw_form",
        "reason",
        "source_path",
        "updated_at",
        "job_id",
    }
)


def export_public_lexical_graph(
    source_database: Path,
    output_directory: Path,
    dataset_version: str,
) -> ExportManifest:
    """Export a deterministic, public-safe lexical graph dataset.

    The source is opened in query-only mode and read under one SQLite snapshot.
    Output is assembled in a sibling staging directory and becomes visible only
    after integrity, closure, and content-safety checks all pass.
    """

    source_path = Path(source_database).expanduser().resolve()
    output_path = Path(output_directory).expanduser().resolve()
    if not source_path.is_file():
        raise PublicExportError(f"source database does not exist: {source_path}")
    if output_path.exists():
        raise PublicExportError(f"output directory already exists: {output_path}")
    if not _DATASET_VERSION_RE.fullmatch(dataset_version):
        raise PublicExportError("dataset_version must be a short public identifier")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lkt-public-export-", dir=output_path.parent) as temporary:
        stage = Path(temporary) / "dataset"
        stage.mkdir()
        manifest = _build_export(source_path, stage, dataset_version)
        _scan_export(stage)
        stage.rename(output_path)

    return ExportManifest(
        output_directory=output_path,
        schema_version=manifest.schema_version,
        export_version=manifest.export_version,
        dataset_version=manifest.dataset_version,
        source_schema_version=manifest.source_schema_version,
        source_graph_revision=manifest.source_graph_revision,
        counts=manifest.counts,
        languages=manifest.languages,
        source_statuses=manifest.source_statuses,
        quarantine_counts=manifest.quarantine_counts,
        table_sha256=manifest.table_sha256,
    )


def _dataset_card_text(manifest: ExportManifest) -> str:
    """Return the deterministic Hugging Face dataset card for one snapshot."""

    lines = [
        "---",
        'pretty_name: "Local Knowledge Terminal Multilingual Lexical Knowledge Graph"',
        "license: other",
        "language:",
        "- en",
        "- ja",
        "- zh",
        "- fr",
        "- ar",
        "tags:",
        "- knowledge-graph",
        "- retrieval-augmented-generation",
        "- rag",
        "- local-llm",
        "- multilingual",
        "- etymology",
        "configs:",
    ]
    for table in _TABLE_ORDER:
        lines.extend(
            (
                f"- config_name: {table}",
                "  data_files:",
                "  - split: train",
                f'    path: "data/{table}.jsonl"',
            )
        )
    lines.extend(("---", ""))

    if set(manifest.source_statuses) == {"unverified"}:
        source_status = (
            f"All {manifest.source_statuses['unverified']} included sources are recorded as "
            "`unverified`."
        )
    else:
        summary = ", ".join(
            f"`{status}`: {count}"
            for status, count in sorted(manifest.source_statuses.items())
        )
        source_status = f"The source-status summary for this snapshot is {summary or 'empty'}."

    lines.extend(
        (
            "# Local Knowledge Terminal Multilingual Lexical Knowledge Graph",
            "",
            (
                f"This is deterministic snapshot `{manifest.dataset_version}` of public schema "
                f"`{manifest.schema_version}`, exported from source graph revision "
                f"`{manifest.source_graph_revision}`."
            ),
            "",
            "## Files and loading",
            "",
            (
                "Each named Hugging Face configuration exposes one heterogeneous JSONL table "
                "as its own `train` split, so incompatible table schemas are never combined."
            ),
            (
                "`data/lkt-lexical-knowledge.sqlite3` is the supplemental portable relational "
                "graph. `manifest.json` describes the logical tables, and `SHA256SUMS` covers "
                "every release file except itself."
            ),
            "",
            "## Quality and provenance",
            "",
            (
                "The graph is built with local Qwen inference and book/dictionary RAG. Accepted "
                "model output can be useful while still incomplete or wrong; consumers should "
                "use confidence, basis, language, and citation roles."
            ),
            (
                "Citations contain metadata only. The export contains no corpus excerpts, source "
                "pages, private corpus content, prompts, raw model responses, job queues, "
                "checkpoints, service state, model weights, secrets, or local filesystem paths."
            ),
            "",
            "## License and release gate",
            "",
            source_status,
            (
                "`license: other` is a conservative metadata label, not a redistribution grant. "
                "This dataset must remain private and gated while any source right, attribution, "
                "version, or overlap review is unresolved. Public release requires explicit "
                "artifact-level authorization after those reviews are complete."
            ),
        )
    )
    return "\n".join(lines) + "\n"


def _build_export(source_path: Path, stage: Path, dataset_version: str) -> ExportManifest:
    uri = source_path.as_uri() + "?mode=ro"
    source = sqlite3.connect(uri, uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute("PRAGMA query_only = ON")
        source.execute("BEGIN")
        _validate_source_schema(source)
        meta = {
            str(row["key"]): str(row["value"])
            for row in source.execute("SELECT key, value FROM schema_meta ORDER BY key")
        }
        source_schema_version = _safe_meta(meta.get("schema_version", "unknown"), "unknown")
        source_graph_revision = _safe_integer(meta.get("relation_graph_revision"), 0)
        if _safe_integer(meta.get("schema_version"), 0) < 10:
            raise PublicExportError("source database must use relation-authority schema v10 or newer")
        _validate_relation_correction_state(source)

        source_rows = {
            table: _read_rows(source, table, columns)
            for table, columns in _SOURCE_COLUMNS.items()
            if table not in {"evidence_records"}
        }
        assertion_roles = _read_assertion_evidence_roles(source)
        evidence_ids = _referenced_evidence_ids(source_rows, assertion_roles)
        source_rows["evidence_records"] = _read_evidence_rows(source, evidence_ids)
        source.execute("COMMIT")
    finally:
        source.close()

    tables, quarantine = _project_public_rows(source_rows, assertion_roles)
    data_directory = stage / "data"
    data_directory.mkdir()
    table_hashes: dict[str, str] = {}
    for table in _TABLE_ORDER:
        rows = tables[table]
        path = data_directory / f"{table}.jsonl"
        table_hashes[table] = _write_jsonl(path, rows)

    database_path = data_directory / "lkt-lexical-knowledge.sqlite3"
    _write_sqlite(database_path, tables)

    languages = tuple(sorted(_collect_languages(tables), key=lambda item: item.casefold()))
    unique_sources = {row["source_id"] for row in tables["citations"]}
    source_statuses = {"unverified": len(unique_sources)}
    counts = {table: len(tables[table]) for table in _TABLE_ORDER}
    manifest = ExportManifest(
        output_directory=stage,
        schema_version=PUBLIC_SCHEMA_VERSION,
        export_version=EXPORT_VERSION,
        dataset_version=dataset_version,
        source_schema_version=source_schema_version,
        source_graph_revision=source_graph_revision,
        counts=counts,
        languages=languages,
        source_statuses=source_statuses,
        quarantine_counts=dict(sorted(quarantine.items())),
        table_sha256=dict(sorted(table_hashes.items())),
    )
    manifest_path = stage / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    readme_path = stage / "README.md"
    readme_path.write_text(
        _dataset_card_text(manifest),
        encoding="utf-8",
        newline="\n",
    )
    checksum_paths = [
        readme_path,
        database_path,
        manifest_path,
        *sorted(data_directory.glob("*.jsonl")),
    ]
    checksum_lines = [
        f"{_file_sha256(path)}  {path.relative_to(stage).as_posix()}"
        for path in sorted(checksum_paths, key=lambda item: item.relative_to(stage).as_posix())
    ]
    (stage / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="ascii", newline="\n")
    return manifest


def _validate_source_schema(connection: sqlite3.Connection) -> None:
    required = {"schema_meta": {"key", "value"}, **{table: set(columns) for table, columns in _SOURCE_COLUMNS.items()}}
    for table, columns in required.items():
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            raise PublicExportError(f"source database is missing required table {table}")
        actual = {str(info[1]) for info in connection.execute(f"PRAGMA table_info({table})")}
        missing = columns - actual
        if missing:
            raise PublicExportError(f"source table {table} is missing columns: {sorted(missing)}")


def _validate_relation_correction_state(connection: sqlite3.Connection) -> None:
    missing = connection.execute(
        """SELECT COUNT(*)
           FROM relation_assertions AS assertion
           LEFT JOIN relation_assertion_slots AS slot
             ON slot.assertion_id = assertion.assertion_id
           WHERE assertion.status = 'accepted'
             AND (slot.assertion_id IS NULL OR slot.slot_key = '' OR slot.policy_version = '')"""
    ).fetchone()
    if missing and int(missing[0]):
        raise PublicExportError(
            f"accepted assertions without a complete correction slot: {int(missing[0])}"
        )
    collisions = connection.execute(
        """SELECT slot.slot_key, COUNT(*) AS winners
           FROM relation_assertion_slots AS slot
           JOIN relation_assertions AS assertion
             ON assertion.assertion_id = slot.assertion_id
           WHERE assertion.status = 'accepted' AND slot.slot_mode = 'exclusive'
           GROUP BY slot.slot_key HAVING COUNT(*) > 1
           ORDER BY slot.slot_key LIMIT 3"""
    ).fetchall()
    if collisions:
        raise PublicExportError(
            "accepted exclusive correction slots have multiple winners: "
            + ", ".join(f"{row[0]} ({row[1]})" for row in collisions)
        )


def _read_rows(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> list[dict[str, Any]]:
    selected = ", ".join(columns)
    return [dict(row) for row in connection.execute(f"SELECT {selected} FROM {table}")]


def _read_assertion_evidence_roles(connection: sqlite3.Connection) -> list[dict[str, str]]:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(assertion_evidence)")}
    if "support_role" in columns:
        query = "SELECT assertion_id, evidence_id, support_role FROM assertion_evidence"
        return [
            {
                "assertion_id": str(row[0]),
                "evidence_id": str(row[1]),
                "support_role": "context" if str(row[2]).casefold() == "context" else "exact",
            }
            for row in connection.execute(query)
        ]
    return [
        {"assertion_id": str(row[0]), "evidence_id": str(row[1]), "support_role": "exact"}
        for row in connection.execute("SELECT assertion_id, evidence_id FROM assertion_evidence")
    ]


def _read_evidence_rows(connection: sqlite3.Connection, evidence_ids: set[str]) -> list[dict[str, Any]]:
    if not evidence_ids:
        return []
    rows: list[dict[str, Any]] = []
    columns = _SOURCE_COLUMNS["evidence_records"]
    selected = ", ".join(columns)
    ordered_ids = sorted(evidence_ids)
    for offset in range(0, len(ordered_ids), 400):
        group = ordered_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in group)
        rows.extend(
            dict(row)
            for row in connection.execute(
                f"SELECT {selected} FROM evidence_records WHERE evidence_id IN ({placeholders})",
                group,
            )
        )
    return rows


def _referenced_evidence_ids(
    rows: Mapping[str, list[dict[str, Any]]],
    assertion_roles: Sequence[Mapping[str, str]],
) -> set[str]:
    identifiers = {str(row["evidence_id"]) for row in assertion_roles}
    identifiers.update(str(row["evidence_id"]) for row in rows["entity_evidence"])
    for row in rows["entity_edges"]:
        identifiers.update(_property_evidence_ids(row.get("properties")))
    for row in rows["relation_assertions"]:
        identifiers.update(_property_evidence_ids(row.get("properties_json")))
    return {identifier for identifier in identifiers if identifier}


def _property_evidence_ids(raw: Any, keys: Iterable[str] = _EVIDENCE_PROPERTY_KEYS) -> set[str]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(value, dict):
        return set()
    identifiers: set[str] = set()
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str):
            identifiers.add(candidate)
        elif isinstance(candidate, list):
            identifiers.update(str(item) for item in candidate if isinstance(item, str))
    return {identifier for identifier in identifiers if identifier}


def _project_public_rows(
    rows: Mapping[str, list[dict[str, Any]]],
    assertion_roles: Sequence[Mapping[str, str]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    quarantine: dict[str, int] = {
        "broken_evidence_links": 0,
        "noncurrent_assertion_edges": 0,
        "closure_entities": 0,
        "content_item_entities": 0,
        "disallowed_relation_assertions": 0,
        "disallowed_relation_edges": 0,
        "nonaccepted_assertions": 0,
        "nonaccepted_edges": 0,
        "nonaccepted_entities": 0,
        "semantic_overlap_entities": 0,
        "semantic_overlap_fields": 0,
        "untyped_entities": 0,
        "unsafe_text_entities": 0,
        "unexportable_claim_observations": 0,
        "unexportable_correction_events": 0,
    }
    entities = {str(row["entity_id"]): row for row in rows["entities"]}
    typed = {
        entity_type: {str(row["entity_id"]): row for row in rows[table]}
        for entity_type, table in _ENTITY_TABLES.items()
    }
    evidence = {str(row["evidence_id"]): row for row in rows["evidence_records"]}
    assertion_slots = {
        str(row["assertion_id"]): row for row in rows["relation_assertion_slots"]
    }

    eligible: set[str] = set()
    for entity_id, row in entities.items():
        if str(row.get("status", "")).casefold() != "accepted":
            quarantine["nonaccepted_entities"] += 1
            continue
        entity_type = str(row.get("entity_type", ""))
        if entity_type == "content-item":
            quarantine["content_item_entities"] += 1
            continue
        if entity_type not in _ENTITY_TABLES or entity_id not in typed[entity_type]:
            quarantine["untyped_entities"] += 1
            continue
        eligible.add(entity_id)

    assertion_evidence: dict[str, set[str]] = {}
    for link in assertion_roles:
        assertion_evidence.setdefault(str(link["assertion_id"]), set()).add(str(link["evidence_id"]))
    entity_evidence: dict[str, set[str]] = {}
    for link in rows["entity_evidence"]:
        entity_evidence.setdefault(str(link["entity_id"]), set()).add(str(link["evidence_id"]))

    for assertion in rows["relation_assertions"]:
        if str(assertion.get("status", "")).casefold() != "accepted":
            continue
        evidence_ids = set(assertion_evidence.get(str(assertion["assertion_id"]), set()))
        evidence_ids.update(_property_evidence_ids(assertion.get("properties_json")))
        for entity_id in _relation_entity_ids(assertion):
            entity_evidence.setdefault(entity_id, set()).update(evidence_ids)
    for edge in rows["entity_edges"]:
        if str(edge.get("status", "")).casefold() != "accepted":
            continue
        evidence_ids = _property_evidence_ids(edge.get("properties"))
        for entity_id in (str(edge["source_entity_id"]), str(edge["target_entity_id"])):
            entity_evidence.setdefault(entity_id, set()).update(evidence_ids)

    segment_rows: dict[str, list[dict[str, Any]]] = {}
    for segment in rows["phoneme_segments"]:
        segment_rows.setdefault(str(segment["pronunciation_id"]), []).append(segment)
    grammar_part_rows: dict[str, list[dict[str, Any]]] = {}
    for part in rows["grammar_parts"]:
        grammar_part_rows.setdefault(str(part["analysis_id"]), []).append(part)

    unsafe_text: set[str] = set()
    overlap_unsafe: set[str] = set()
    for entity_id in sorted(eligible):
        entity_type = str(entities[entity_id]["entity_type"])
        row = typed[entity_type][entity_id]
        values = _all_public_text_values(
            entity_type,
            row,
            segment_rows.get(entity_id, ()),
            grammar_part_rows.get(entity_id, ()),
        )
        if any(_contains_sensitive_text(value) for value in values if value):
            unsafe_text.add(entity_id)
        semantic_values = _semantic_text_values(entity_type, row)
        raw_values: list[str] = []
        for evidence_id in sorted(entity_evidence.get(entity_id, set())):
            record = evidence.get(evidence_id)
            if record is None:
                quarantine["broken_evidence_links"] += 1
                continue
            raw_values.extend(_evidence_source_strings(record))
        overlapping_fields = sum(
            1 for value in semantic_values if value and _overlaps_source(value, raw_values)
        )
        if overlapping_fields:
            overlap_unsafe.add(entity_id)
            quarantine["semantic_overlap_fields"] += overlapping_fields

    quarantine["unsafe_text_entities"] = len(unsafe_text)
    quarantine["semantic_overlap_entities"] = len(overlap_unsafe)
    eligible.difference_update(unsafe_text | overlap_unsafe)

    while True:
        broken = {
            entity_id
            for entity_id in eligible
            if any(reference not in eligible for reference in _typed_references(entities[entity_id], typed, entity_id))
        }
        if not broken:
            break
        quarantine["closure_entities"] += len(broken)
        eligible.difference_update(broken)

    public_ids = {entity_id: _public_id("entity", entity_id) for entity_id in eligible}
    output: dict[str, list[dict[str, Any]]] = {table: [] for table in _TABLE_ORDER}
    for entity_id in sorted(eligible):
        entity = entities[entity_id]
        entity_type = str(entity["entity_type"])
        output["entities"].append(
            {
                "entity_id": public_ids[entity_id],
                "entity_type": entity_type,
                "quality_score": _safe_score(entity.get("quality_score"), nullable=True),
            }
        )
        projected = _project_typed_row(entity_type, typed[entity_type][entity_id], public_ids)
        output[_ENTITY_TABLES[entity_type]].append(projected)

    for row in rows["term_morphemes"]:
        term_id = str(row["term_id"])
        morpheme_id = str(row["morpheme_id"])
        if term_id not in eligible or morpheme_id not in eligible:
            continue
        surface = _clean_text(row.get("surface"))
        if _contains_sensitive_text(surface):
            continue
        output["term_morphemes"].append(
            {
                "term_id": public_ids[term_id],
                "morpheme_id": public_ids[morpheme_id],
                "ordinal": _safe_integer(row.get("ordinal"), 0),
                "surface": surface,
                "confidence": _safe_score(row.get("confidence")),
                "basis": _safe_basis(row.get("basis")),
            }
        )
    for pronunciation_id in eligible:
        if str(entities[pronunciation_id]["entity_type"]) != "pronunciation":
            continue
        for row in segment_rows.get(pronunciation_id, ()):
            output["phoneme_segments"].append(
                {
                    "segment_id": _public_id("phoneme-segment", row["segment_id"]),
                    "pronunciation_id": public_ids[pronunciation_id],
                    "ordinal": _safe_integer(row.get("ordinal"), 0),
                    "grapheme": _clean_text(row.get("grapheme")),
                    "phoneme": _clean_text(row.get("phoneme")),
                    "syllable": _clean_text(row.get("syllable")),
                }
            )
    for analysis_id in eligible:
        if str(entities[analysis_id]["entity_type"]) != "grammar-analysis":
            continue
        for row in grammar_part_rows.get(analysis_id, ()):
            output["grammar_parts"].append(
                {
                    "part_id": _public_id("grammar-part", row["part_id"]),
                    "analysis_id": public_ids[analysis_id],
                    "ordinal": _safe_integer(row.get("ordinal"), 0),
                    "surface": _clean_text(row.get("surface")),
                    "lemma": _clean_text(row.get("lemma")),
                    "role": _clean_text(row.get("role")),
                    "part_of_speech": _clean_text(row.get("part_of_speech")),
                    "reading": _clean_text(row.get("reading")),
                }
            )

    accepted_authority_edge_identities = {
        (str(row["source_entity_id"]), str(row["target_entity_id"]), str(row["relation"]))
        for row in rows["relation_assertions"]
        if str(row.get("status", "")).casefold() == "accepted"
        and str(row["assertion_id"]) in assertion_slots
    }
    assertion_controlled_edge_identities = {
        (str(row["source_entity_id"]), str(row["target_entity_id"]), str(row["relation"]))
        for row in rows["relation_assertions"]
    }
    accepted_assertions: dict[str, dict[str, Any]] = {}
    for row in rows["entity_edges"]:
        if str(row.get("status", "")).casefold() != "accepted":
            quarantine["nonaccepted_edges"] += 1
            continue
        relation = str(row.get("relation", ""))
        if relation not in _ALLOWED_RELATIONS:
            quarantine["disallowed_relation_edges"] += 1
            continue
        source_id = str(row["source_entity_id"])
        target_id = str(row["target_entity_id"])
        if source_id not in eligible or target_id not in eligible:
            continue
        edge_identity = (source_id, target_id, relation)
        if (
            edge_identity in assertion_controlled_edge_identities
            and edge_identity not in accepted_authority_edge_identities
        ):
            quarantine["noncurrent_assertion_edges"] += 1
            continue
        output["entity_edges"].append(
            {
                "edge_id": _public_id("edge", row["edge_id"]),
                "source_entity_id": public_ids[source_id],
                "target_entity_id": public_ids[target_id],
                "relation": relation,
                "basis": _safe_basis(row.get("basis")),
                "confidence": _safe_score(row.get("confidence")),
            }
        )
    for row in rows["relation_assertions"]:
        if str(row.get("status", "")).casefold() != "accepted":
            quarantine["nonaccepted_assertions"] += 1
            continue
        relation = str(row.get("relation", ""))
        if relation not in _ALLOWED_RELATIONS:
            quarantine["disallowed_relation_assertions"] += 1
            continue
        ids = _relation_entity_ids(row)
        if any(entity_id not in eligible for entity_id in ids):
            continue
        assertion_id = str(row["assertion_id"])
        accepted_assertions[assertion_id] = row
        output["relation_assertions"].append(
            {
                "assertion_id": _public_id("assertion", assertion_id),
                "subject_entity_id": public_ids[ids[0]],
                "source_entity_id": public_ids[ids[1]],
                "target_entity_id": public_ids[ids[2]],
                "relation": relation,
                "basis": _safe_basis(row.get("basis")),
                "confidence": _safe_score(row.get("confidence")),
            }
        )
        slot = assertion_slots.get(assertion_id)
        if slot is None:
            raise PublicExportError(f"exported assertion lacks a correction slot: {assertion_id}")
        output["relation_slot_winners"].append(
            {
                "slot_id": _public_id("slot", slot["slot_key"]),
                "assertion_id": _public_id("assertion", assertion_id),
                "slot_mode": str(slot["slot_mode"]),
                "policy_version": _safe_tag(slot.get("policy_version"), "unspecified"),
            }
        )

    if len(output["relation_slot_winners"]) != len(output["relation_assertions"]):
        raise PublicExportError("accepted assertion slot coverage changed during export")
    exclusive_slots = [
        row["slot_id"]
        for row in output["relation_slot_winners"]
        if row["slot_mode"] == "exclusive"
    ]
    if len(exclusive_slots) != len(set(exclusive_slots)):
        raise PublicExportError("public export contains an exclusive slot collision")

    output["relation_correction_events"].extend(
        _project_relation_correction_events(
            rows["relation_assertion_events"], public_ids, accepted_assertions, quarantine
        )
    )
    observations, observation_incumbents = _project_relation_claim_observations(
        rows["relation_claim_observations"], public_ids, quarantine
    )
    output["relation_claim_observations"].extend(observations)
    output["relation_claim_observation_incumbents"].extend(observation_incumbents)

    entity_citation_roles: dict[tuple[str, str], str] = {}
    for link in rows["entity_evidence"]:
        entity_id = str(link["entity_id"])
        evidence_id = str(link["evidence_id"])
        if entity_id in eligible and evidence_id in evidence:
            entity_citation_roles[(entity_id, evidence_id)] = "exact"
    assertion_citation_roles: dict[tuple[str, str], str] = {}
    for link in assertion_roles:
        assertion_id = str(link["assertion_id"])
        evidence_id = str(link["evidence_id"])
        if assertion_id in accepted_assertions and evidence_id in evidence:
            _merge_role(assertion_citation_roles, (assertion_id, evidence_id), str(link["support_role"]))
    for assertion_id, row in accepted_assertions.items():
        properties = _json_object(row.get("properties_json"))
        for evidence_id in _property_id_values(properties, "citation_evidence_ids"):
            if evidence_id in evidence:
                _merge_role(assertion_citation_roles, (assertion_id, evidence_id), "exact")
        for evidence_id in _property_id_values(properties, "context_evidence_ids"):
            if evidence_id in evidence:
                _merge_role(assertion_citation_roles, (assertion_id, evidence_id), "context")

    used_evidence_ids = {
        evidence_id for _, evidence_id in entity_citation_roles
    } | {evidence_id for _, evidence_id in assertion_citation_roles}
    citation_ids = {evidence_id: _public_id("citation", evidence_id) for evidence_id in used_evidence_ids}
    for evidence_id in sorted(used_evidence_ids):
        output["citations"].append(_project_citation(evidence[evidence_id], citation_ids[evidence_id]))
    for (entity_id, evidence_id), role in sorted(entity_citation_roles.items()):
        output["entity_citations"].append(
            {
                "entity_id": public_ids[entity_id],
                "citation_id": citation_ids[evidence_id],
                "support_role": role,
            }
        )
    for (assertion_id, evidence_id), role in sorted(assertion_citation_roles.items()):
        output["assertion_citations"].append(
            {
                "assertion_id": _public_id("assertion", assertion_id),
                "citation_id": citation_ids[evidence_id],
                "support_role": role,
            }
        )

    for table, table_rows in output.items():
        keys = _SORT_KEYS[table]
        table_rows.sort(key=lambda row: tuple(_sort_value(row[key]) for key in keys))
    return output, quarantine


def _project_relation_correction_events(
    rows: Sequence[Mapping[str, Any]],
    public_ids: Mapping[str, str],
    accepted_assertions: Mapping[str, Mapping[str, Any]],
    quarantine: dict[str, int],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    orders: dict[str, int] = {}
    allowed_actions = {"baseline", "accept", "update", "keep", "archive", "reject"}
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.get("assertion_id", "")),
            str(row.get("created_at", "")),
            str(row.get("event_id", "")),
        ),
    )
    for row in ordered:
        assertion_id = str(row.get("assertion_id", "")).strip()
        event_order = orders.get(assertion_id, 0)
        orders[assertion_id] = event_order + 1
        before = _strict_json_object(row.get("before_json"))
        after = _strict_json_object(row.get("after_json"))
        exact_ids = _strict_json_string_set(row.get("evidence_ids_json"))
        context_ids = _strict_json_string_set(row.get("context_evidence_ids_json"))
        event_id = str(row.get("event_id", "")).strip()
        decision_id = str(row.get("decision_id", "")).strip()
        action = str(row.get("action", "")).casefold()
        if (
            before is None
            or after is None
            or exact_ids is None
            or context_ids is None
            or not assertion_id
            or not event_id
            or not decision_id
            or action not in allowed_actions
        ):
            quarantine["unexportable_correction_events"] += 1
            continue
        state = after if _relation_snapshot_identity(after) is not None else before
        identity = _relation_snapshot_identity(state)
        if identity is None:
            quarantine["unexportable_correction_events"] += 1
            continue
        subject_id, source_id, target_id, relation = identity
        if relation not in _ALLOWED_RELATIONS or any(
            entity_id not in public_ids for entity_id in (subject_id, source_id, target_id)
        ):
            quarantine["unexportable_correction_events"] += 1
            continue
        output.append(
            {
                "correction_id": _public_id("correction", event_id),
                "decision_id": _public_id("decision", decision_id),
                "assertion_id": _public_id("assertion", assertion_id),
                "event_order": event_order,
                "is_current": int(assertion_id in accepted_assertions),
                "subject_entity_id": public_ids[subject_id],
                "source_entity_id": public_ids[source_id],
                "target_entity_id": public_ids[target_id],
                "relation": relation,
                "action": action,
                "before_basis": _optional_basis(before.get("basis")),
                "after_basis": _optional_basis(after.get("basis")),
                "before_confidence": _safe_score(before.get("confidence"), nullable=True),
                "after_confidence": _safe_score(after.get("confidence"), nullable=True),
                "before_status": _safe_relation_status(before.get("status")),
                "after_status": _safe_relation_status(after.get("status")),
                "before_revision": _nonnegative_integer(before.get("revision")),
                "after_revision": _nonnegative_integer(after.get("revision")),
                "exact_evidence_count": len(exact_ids),
                "context_evidence_count": len(context_ids),
                "input_fingerprint": _public_fingerprint(row.get("input_fingerprint")),
                "output_fingerprint": _public_fingerprint(row.get("output_fingerprint")),
                "validator_version": _safe_tag(row.get("validator_version"), "unspecified"),
            }
        )
    return output


def _project_relation_claim_observations(
    rows: Sequence[Mapping[str, Any]],
    public_ids: Mapping[str, str],
    quarantine: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    output: list[dict[str, Any]] = []
    incumbent_links: list[dict[str, str]] = []
    orders: dict[str, int] = {}
    allowed_modes = {"exclusive", "set-member", "none"}
    allowed_decisions = {"pending", "keep", "conflict", "supersede", "coexist"}
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.get("slot_key", "")),
            str(row.get("created_at", "")),
            str(row.get("observation_id", "")),
        ),
    )
    for row in ordered:
        slot_key = str(row.get("slot_key", "")).strip()
        observation_order = orders.get(slot_key, 0)
        orders[slot_key] = observation_order + 1
        proposal = _strict_json_object(row.get("proposal_json"))
        incumbents = _strict_json_string_set(row.get("incumbent_assertion_ids_json"))
        exact_ids = _strict_json_string_set(row.get("exact_evidence_ids_json"))
        context_ids = _strict_json_string_set(row.get("context_evidence_ids_json"))
        observation_id = str(row.get("observation_id", "")).strip()
        proposed_assertion_id = str(row.get("proposed_assertion_id", "")).strip()
        slot_mode = str(row.get("slot_mode", "")).casefold()
        decision = str(row.get("decision", "")).casefold()
        identity = _relation_snapshot_identity(proposal or {})
        if (
            proposal is None
            or incumbents is None
            or exact_ids is None
            or context_ids is None
            or not slot_key
            or not observation_id
            or not proposed_assertion_id
            or slot_mode not in allowed_modes
            or decision not in allowed_decisions
            or identity is None
        ):
            quarantine["unexportable_claim_observations"] += 1
            continue
        subject_id, source_id, target_id, relation = identity
        if relation not in _ALLOWED_RELATIONS or any(
            entity_id not in public_ids for entity_id in (subject_id, source_id, target_id)
        ):
            quarantine["unexportable_claim_observations"] += 1
            continue
        public_observation_id = _public_id("observation", observation_id)
        output.append(
            {
                "observation_id": public_observation_id,
                "slot_id": _public_id("slot", slot_key),
                "observation_order": observation_order,
                "slot_mode": slot_mode,
                "proposed_assertion_id": _public_id("assertion", proposed_assertion_id),
                "subject_entity_id": public_ids[subject_id],
                "source_entity_id": public_ids[source_id],
                "target_entity_id": public_ids[target_id],
                "relation": relation,
                "basis": _safe_basis(proposal.get("basis")),
                "confidence": _safe_score(proposal.get("confidence")),
                "decision": decision,
                "exact_evidence_count": len(exact_ids),
                "context_evidence_count": len(context_ids),
                "input_fingerprint": _public_fingerprint(row.get("input_fingerprint")),
                "output_fingerprint": _public_fingerprint(row.get("output_fingerprint")),
                "epoch_fingerprint": _public_fingerprint(row.get("epoch_fingerprint")),
                "model_pass_version": _safe_tag(row.get("prompt_version"), "unspecified"),
                "policy_version": _safe_tag(row.get("policy_version"), "unspecified"),
            }
        )
        for incumbent_id in incumbents:
            incumbent_links.append(
                {
                    "observation_id": public_observation_id,
                    "incumbent_assertion_id": _public_id("assertion", incumbent_id),
                }
            )
    return output, incumbent_links


def _relation_snapshot_identity(value: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    identity = tuple(
        str(value.get(key, "")).strip()
        for key in (
            "subject_entity_id",
            "source_entity_id",
            "target_entity_id",
            "relation",
        )
    )
    return identity if all(identity) else None


def _strict_json_object(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _strict_json_string_set(raw: Any) -> tuple[str, ...] | None:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(sorted({item.strip() for item in value if item.strip()}))


def _optional_basis(value: Any) -> str:
    return _safe_basis(value) if str(value or "").strip() else ""


def _safe_relation_status(value: Any) -> str:
    status = str(value or "").casefold()
    return status if status in {"accepted", "archived", "candidate", "draft", "rejected"} else ""


def _nonnegative_integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    parsed = _safe_integer(value, -1)
    return parsed if parsed >= 0 else None


def _public_fingerprint(value: Any) -> str:
    raw = str(value or "").strip()
    return f"sha256:{_digest_text(raw)}" if raw else ""


def _typed_references(
    entity: Mapping[str, Any],
    typed: Mapping[str, Mapping[str, Mapping[str, Any]]],
    entity_id: str,
) -> tuple[str, ...]:
    entity_type = str(entity["entity_type"])
    row = typed[entity_type][entity_id]
    if entity_type == "meaning":
        return _required_and_optional(row, ("subject_entity_id",), ("term_id",))
    if entity_type == "pronunciation":
        return _required_and_optional(row, ("term_id",), ())
    if entity_type == "history-event":
        return _required_and_optional(row, ("subject_entity_id",), ())
    if entity_type == "translation":
        return _required_and_optional(row, ("source_term_id",), ("source_meaning_id", "target_term_id"))
    if entity_type == "grammar-analysis":
        return _required_and_optional(row, ("subject_entity_id",), ())
    return ()


def _required_and_optional(
    row: Mapping[str, Any],
    required: Sequence[str],
    optional: Sequence[str],
) -> tuple[str, ...]:
    values: list[str] = []
    for key in required:
        value = str(row.get(key) or "")
        values.append(value)
    for key in optional:
        value = str(row.get(key) or "")
        if value:
            values.append(value)
    return tuple(values)


def _project_typed_row(
    entity_type: str,
    row: Mapping[str, Any],
    ids: Mapping[str, str],
) -> dict[str, Any]:
    entity_id = str(row["entity_id"])
    public_id = ids[entity_id]
    language = _normalize_language(row.get("language"))
    if entity_type == "term":
        text = _clean_text(row.get("text"))
        return {
            "entity_id": public_id,
            "language": language,
            "text": text,
            "normalized": _normalize_lexical(text),
            "kind": _safe_tag(row.get("kind"), "word"),
        }
    if entity_type == "meaning":
        return {
            "entity_id": public_id,
            "subject_entity_id": ids[str(row["subject_entity_id"])],
            "term_id": ids.get(str(row.get("term_id") or "")),
            "language": language,
            "definition": _clean_text(row.get("definition")),
            "part_of_speech": _clean_text(row.get("part_of_speech")),
            "register_label": _clean_text(row.get("register_label")),
            "domain_label": _clean_text(row.get("domain_label")),
            "sense_order": _safe_integer(row.get("sense_order"), 0),
        }
    if entity_type == "morpheme":
        form = _clean_text(row.get("form"))
        kind = str(row.get("kind", "unknown")).casefold()
        return {
            "entity_id": public_id,
            "language": language,
            "form": form,
            "normalized": _normalize_lexical(form),
            "kind": kind if kind in _ALLOWED_MORPHEME_KINDS else "unknown",
            "meaning": _clean_text(row.get("meaning")),
        }
    if entity_type == "pronunciation":
        return {
            "entity_id": public_id,
            "term_id": ids[str(row["term_id"])],
            "language": language,
            "system": _safe_tag(row.get("system"), "unspecified"),
            "reading": _clean_text(row.get("reading")),
            "dialect": _clean_text(row.get("dialect")),
        }
    if entity_type == "historical-form":
        form = _clean_text(row.get("form"))
        return {
            "entity_id": public_id,
            "language": language,
            "form": form,
            "normalized": _normalize_lexical(form),
            "period_label": _clean_text(row.get("period_label")),
            "date_min": _nullable_integer(row.get("date_min")),
            "date_max": _nullable_integer(row.get("date_max")),
            "meaning": _clean_text(row.get("meaning")),
        }
    if entity_type == "history-event":
        return {
            "entity_id": public_id,
            "subject_entity_id": ids[str(row["subject_entity_id"])],
            "event_type": _safe_tag(row.get("event_type"), "other"),
            "language": language,
            "period_label": _clean_text(row.get("period_label")),
            "date_min": _nullable_integer(row.get("date_min")),
            "date_max": _nullable_integer(row.get("date_max")),
            "description": _clean_text(row.get("description")),
        }
    if entity_type == "translation":
        text = _clean_text(row.get("text"))
        return {
            "entity_id": public_id,
            "source_term_id": ids[str(row["source_term_id"])],
            "source_meaning_id": ids.get(str(row.get("source_meaning_id") or "")),
            "target_language": _normalize_language(row.get("target_language")),
            "target_term_id": ids.get(str(row.get("target_term_id") or "")),
            "text": text,
            "normalized": _normalize_lexical(text),
            "transliteration": _clean_text(row.get("transliteration")),
            "usage_note": _clean_text(row.get("usage_note")),
        }
    if entity_type == "grammar-analysis":
        return {
            "entity_id": public_id,
            "subject_entity_id": ids[str(row["subject_entity_id"])],
            "language": language,
            "analysis_type": _safe_tag(row.get("analysis_type"), "other"),
            "summary": _clean_text(row.get("summary")),
        }
    raise PublicExportError(f"unsupported entity type: {entity_type}")


def _project_citation(row: Mapping[str, Any], citation_id: str) -> dict[str, str]:
    corpus_id = str(row.get("corpus_id") or "")
    entry_id = str(row.get("source_entry_id") or "")
    source_hash = str(row.get("source_hash") or "")
    return {
        "citation_id": citation_id,
        "source_id": _public_id("source", corpus_id or "unknown"),
        "source_entry_id": _public_id("source-entry", f"{corpus_id}\0{entry_id}") if entry_id else "",
        "locator": _sanitize_locator(row.get("locator")),
        "source_fingerprint": f"sha256:{_digest_text(source_hash)}" if source_hash else "",
        "license_status": "unverified",
    }


def _relation_entity_ids(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row["subject_entity_id"]),
        str(row["source_entity_id"]),
        str(row["target_entity_id"]),
    )


def _merge_role(mapping: dict[tuple[str, str], str], key: tuple[str, str], role: str) -> None:
    normalized = "context" if role.casefold() == "context" else "exact"
    if mapping.get(key) != "exact" or normalized == "exact":
        mapping[key] = normalized


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _property_id_values(properties: Mapping[str, Any], key: str) -> set[str]:
    value = properties.get(key)
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, list):
        return {str(item) for item in value if isinstance(item, str) and item}
    return set()


def _all_public_text_values(
    entity_type: str,
    row: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    grammar_parts: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    excluded = {
        "entity_id",
        "subject_entity_id",
        "term_id",
        "source_term_id",
        "source_meaning_id",
        "target_term_id",
        "date_min",
        "date_max",
        "sense_order",
    }
    values = [_clean_text(value) for key, value in row.items() if key not in excluded and value is not None]
    if entity_type == "pronunciation":
        for segment in segments:
            values.extend(_clean_text(segment.get(key)) for key in ("grapheme", "phoneme", "syllable"))
    if entity_type == "grammar-analysis":
        for part in grammar_parts:
            values.extend(
                _clean_text(part.get(key))
                for key in ("surface", "lemma", "role", "part_of_speech", "reading")
            )
    return tuple(values)


def _semantic_text_values(entity_type: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    fields = {
        "meaning": ("definition",),
        "morpheme": ("meaning",),
        "historical-form": ("meaning",),
        "history-event": ("description",),
        "translation": ("text", "transliteration", "usage_note"),
        "grammar-analysis": ("summary",),
    }.get(entity_type, ())
    return tuple(_clean_text(row.get(field)) for field in fields)


def _evidence_source_strings(row: Mapping[str, Any]) -> list[str]:
    values = [
        row.get("corpus_id"),
        row.get("source_entry_id"),
        row.get("locator"),
        row.get("excerpt"),
    ]
    raw_payload = row.get("payload")
    try:
        payload = json.loads(str(raw_payload or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = str(raw_payload or "")
    values.extend(_flatten_strings(payload))
    normalized = {_normalize_comparison(value) for value in values if value is not None}
    return sorted(value for value in normalized if value)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        flattened: list[str] = []
        for item in value:
            flattened.extend(_flatten_strings(item))
        return flattened
    if isinstance(value, dict):
        flattened = []
        for key in sorted(value):
            flattened.extend(_flatten_strings(value[key]))
        return flattened
    return []


def _overlaps_source(value: str, source_values: Sequence[str]) -> bool:
    candidate = _normalize_comparison(value)
    if not candidate:
        return False
    for source in source_values:
        if candidate == source:
            return True
        if len(candidate) < 60 or len(source) < 60:
            continue
        shorter, longer = (candidate, source) if len(candidate) <= len(source) else (source, candidate)
        if shorter in longer:
            return True
        for offset in range(0, len(shorter) - 59):
            if shorter[offset : offset + 60] in longer:
                return True
    return False


def _contains_sensitive_text(value: str) -> bool:
    return bool(
        value
        and (
            _EMAIL_RE.search(value)
            or _ABSOLUTE_PATH_RE.search(value)
            or _SECRET_RE.search(value)
            or _PRIVATE_KEY_RE.search(value)
        )
    )


def _clean_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(character if character >= " " else " " for character in text)
    return " ".join(text.split())


def _normalize_comparison(value: Any) -> str:
    return _clean_text(value).casefold()


def _normalize_lexical(value: Any) -> str:
    return _normalize_comparison(value)


def _normalize_language(value: Any) -> str:
    text = _clean_text(value).replace("_", "-")
    if not _BCP47_RE.fullmatch(text):
        return "und"
    parts = text.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def _safe_tag(value: Any, default: str) -> str:
    text = _clean_text(value)
    return text if _SAFE_TAG_RE.fullmatch(text) else default


def _safe_meta(value: Any, default: str) -> str:
    return _safe_tag(value, default)


def _safe_basis(value: Any) -> str:
    basis = str(value or "").casefold()
    return basis if basis in _ALLOWED_BASES else "model"


def _safe_integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _nullable_integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_score(value: Any, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None if nullable else 0.0
    if not math.isfinite(score):
        return None if nullable else 0.0
    return min(1.0, max(0.0, score))


def _sanitize_locator(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if len(text) <= 160 and not _contains_sensitive_text(text) and ".." not in text and "\\" not in text:
        return text
    return f"locator_{_digest_text(text)[:24]}"


def _public_id(kind: str, private_value: Any) -> str:
    prefixes = {
        "assertion": "ast",
        "citation": "cit",
        "correction": "cor",
        "decision": "dec",
        "edge": "edg",
        "entity": "ent",
        "grammar-part": "grp",
        "observation": "obs",
        "phoneme-segment": "phs",
        "source": "src",
        "source-entry": "sen",
        "slot": "slt",
    }
    digest = hashlib.sha256(
        f"lkt-public-v1\0{kind}\0{private_value}".encode("utf-8", errors="strict")
    ).hexdigest()
    return f"{prefixes[kind]}_{digest[:32]}"


def _digest_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="strict")).hexdigest()


def _sort_value(value: Any) -> tuple[int, str]:
    if value is None:
        return (0, "")
    return (1, str(value))


def _collect_languages(tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> set[str]:
    languages: set[str] = set()
    for table, fields in {
        "terms": ("language",),
        "meanings": ("language",),
        "morphemes": ("language",),
        "pronunciations": ("language",),
        "historical_forms": ("language",),
        "history_events": ("language",),
        "translations": ("target_language",),
        "grammar_analyses": ("language",),
    }.items():
        for row in tables[table]:
            languages.update(str(row[field]) for field in fields if row.get(field))
    return languages


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        for row in rows:
            line = (
                json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode("utf-8")
            handle.write(line)
            digest.update(line)
    return digest.hexdigest()


def _write_sqlite(path: Path, tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_DDL)
        with connection:
            for table in _TABLE_ORDER:
                columns = _PUBLIC_COLUMNS[table]
                placeholders = ",".join("?" for _ in columns)
                values = [tuple(row[column] for column in columns) for row in tables[table]]
                if values:
                    connection.executemany(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                        values,
                    )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise PublicExportError(f"public database has foreign-key violations: {violations[:3]}")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).casefold() != "ok":
            raise PublicExportError(f"public database failed integrity check: {integrity}")
        connection.execute("PRAGMA optimize")
    finally:
        connection.close()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_export(directory: Path) -> None:
    failures: list[str] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        data = path.read_bytes()
        decoded = data.decode("utf-8", errors="ignore")
        if _contains_sensitive_text(decoded):
            failures.append(f"sensitive text in {path.name}")
        if path.suffix == ".jsonl":
            for line_number, line in enumerate(decoded.splitlines(), 1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    failures.append(f"invalid JSON in {path.name}:{line_number}: {error.msg}")
                    continue
                bad_keys = _find_raw_keys(value)
                failures.extend(f"forbidden JSON key {key} in {path.name}" for key in sorted(bad_keys))
        elif path.name == "manifest.json":
            try:
                value = json.loads(decoded)
            except json.JSONDecodeError as error:
                failures.append(f"invalid manifest JSON: {error.msg}")
            else:
                failures.extend(
                    f"forbidden manifest key {key}" for key in sorted(_find_raw_keys(value))
                )
        elif path.suffix in {".sqlite", ".sqlite3"}:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                schema_objects = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
                ).fetchall()
                for (object_name,) in schema_objects:
                    if str(object_name).casefold() in _RAW_KEYS:
                        failures.append(f"forbidden SQLite object {object_name} in {path.name}")
                    quoted_name = '"' + str(object_name).replace('"', '""') + '"'
                    for column in connection.execute(f"PRAGMA table_info({quoted_name})"):
                        column_name = str(column[1])
                        if column_name.casefold() in _RAW_KEYS:
                            failures.append(
                                f"forbidden SQLite column {column_name} in {path.name}"
                            )
            finally:
                connection.close()
    if failures:
        raise PublicExportError("public export safety scan failed: " + "; ".join(sorted(set(failures))))


def _find_raw_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _RAW_KEYS:
                found.add(str(key))
            found.update(_find_raw_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_find_raw_keys(child))
    return found


__all__ = [
    "EXPORT_VERSION",
    "PUBLIC_SCHEMA_VERSION",
    "ExportManifest",
    "PublicExportError",
    "export_public_lexical_graph",
]
