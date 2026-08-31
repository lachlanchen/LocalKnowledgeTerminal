from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


_LANGUAGE = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_ENTITY_TYPES = {
    "term",
    "meaning",
    "morpheme",
    "pronunciation",
    "historical-form",
    "translation",
    "grammar-analysis",
    "content-item",
    "history-event",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _language(value: str) -> str:
    language = value.strip().lower()
    if not _LANGUAGE.fullmatch(language):
        raise ValueError(f"invalid language code: {value!r}")
    return language


def _identifier(prefix: str, key: str) -> str:
    return f"{prefix}-{uuid.uuid5(uuid.NAMESPACE_URL, 'lkt:' + key)}"


class KnowledgeStore:
    """Authoritative, atomic knowledge used to reconstruct cards and graphs."""

    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialise(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    canonical_key TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'accepted'
                        CHECK(status IN ('draft', 'accepted', 'rejected', 'archived')),
                    quality_score REAL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_type_status
                    ON entities(entity_type, status);

                CREATE TABLE IF NOT EXISTS terms (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    language TEXT NOT NULL,
                    text TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'word',
                    UNIQUE(language, normalized, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_terms_lookup
                    ON terms(language, normalized);

                CREATE TABLE IF NOT EXISTS meanings (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    term_id TEXT NOT NULL REFERENCES terms(entity_id),
                    language TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    part_of_speech TEXT NOT NULL DEFAULT '',
                    register_label TEXT NOT NULL DEFAULT '',
                    domain_label TEXT NOT NULL DEFAULT '',
                    sense_order INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_meanings_term
                    ON meanings(term_id, language, sense_order);

                CREATE TABLE IF NOT EXISTS morphemes (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    language TEXT NOT NULL,
                    form TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    kind TEXT NOT NULL
                        CHECK(kind IN ('prefix', 'root', 'suffix', 'free', 'unknown')),
                    meaning TEXT NOT NULL DEFAULT '',
                    UNIQUE(language, normalized, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_morphemes_lookup
                    ON morphemes(language, normalized, kind);

                CREATE TABLE IF NOT EXISTS term_morphemes (
                    term_id TEXT NOT NULL REFERENCES terms(entity_id) ON DELETE CASCADE,
                    morpheme_id TEXT NOT NULL REFERENCES morphemes(entity_id),
                    ordinal INTEGER NOT NULL,
                    surface TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    basis TEXT NOT NULL DEFAULT 'model'
                        CHECK(basis IN ('book', 'model', 'reviewed', 'derived')),
                    PRIMARY KEY(term_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_term_morphemes_reverse
                    ON term_morphemes(morpheme_id, term_id);

                CREATE TABLE IF NOT EXISTS pronunciations (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    term_id TEXT NOT NULL REFERENCES terms(entity_id),
                    language TEXT NOT NULL,
                    system TEXT NOT NULL,
                    reading TEXT NOT NULL,
                    dialect TEXT NOT NULL DEFAULT '',
                    UNIQUE(term_id, language, system, reading, dialect)
                );
                CREATE INDEX IF NOT EXISTS idx_pronunciations_term
                    ON pronunciations(term_id, language, system);

                CREATE TABLE IF NOT EXISTS phoneme_segments (
                    segment_id TEXT PRIMARY KEY,
                    pronunciation_id TEXT NOT NULL REFERENCES pronunciations(entity_id)
                        ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    grapheme TEXT NOT NULL DEFAULT '',
                    phoneme TEXT NOT NULL,
                    syllable TEXT NOT NULL DEFAULT '',
                    color_key TEXT NOT NULL DEFAULT '',
                    features TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(pronunciation_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS historical_forms (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    language TEXT NOT NULL,
                    form TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    period_label TEXT NOT NULL DEFAULT '',
                    date_min INTEGER,
                    date_max INTEGER,
                    meaning TEXT NOT NULL DEFAULT '',
                    UNIQUE(language, normalized, period_label)
                );

                CREATE TABLE IF NOT EXISTS history_events (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    event_type TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    period_label TEXT NOT NULL DEFAULT '',
                    date_min INTEGER,
                    date_max INTEGER,
                    description TEXT NOT NULL,
                    properties TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_history_events_subject
                    ON history_events(subject_entity_id, date_min, date_max);

                CREATE TABLE IF NOT EXISTS translations (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    source_term_id TEXT NOT NULL REFERENCES terms(entity_id),
                    source_meaning_id TEXT REFERENCES meanings(entity_id),
                    target_language TEXT NOT NULL,
                    target_term_id TEXT REFERENCES terms(entity_id),
                    text TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    transliteration TEXT NOT NULL DEFAULT '',
                    usage_note TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_term_id, source_meaning_id, target_language, normalized)
                );
                CREATE INDEX IF NOT EXISTS idx_translations_source
                    ON translations(source_term_id, target_language);

                CREATE TABLE IF NOT EXISTS grammar_analyses (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    language TEXT NOT NULL,
                    analysis_type TEXT NOT NULL DEFAULT 'sentence',
                    summary TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS grammar_parts (
                    part_id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL REFERENCES grammar_analyses(entity_id)
                        ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    surface TEXT NOT NULL,
                    lemma TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    part_of_speech TEXT NOT NULL DEFAULT '',
                    reading TEXT NOT NULL DEFAULT '',
                    color_key TEXT NOT NULL DEFAULT '',
                    features TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(analysis_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS content_items (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('answer', 'question', 'sentence')),
                    language TEXT NOT NULL,
                    text TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    source_key TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_content_items_kind
                    ON content_items(kind, language);

                CREATE TABLE IF NOT EXISTS entity_edges (
                    edge_id TEXT PRIMARY KEY,
                    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    relation TEXT NOT NULL,
                    basis TEXT NOT NULL DEFAULT 'model'
                        CHECK(basis IN ('book', 'model', 'reviewed', 'derived')),
                    confidence REAL NOT NULL DEFAULT 0.5,
                    status TEXT NOT NULL DEFAULT 'accepted'
                        CHECK(status IN ('draft', 'accepted', 'rejected', 'archived')),
                    properties TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_entity_id, target_entity_id, relation)
                );
                CREATE INDEX IF NOT EXISTS idx_edges_source
                    ON entity_edges(source_entity_id, relation, status);
                CREATE INDEX IF NOT EXISTS idx_edges_target
                    ON entity_edges(target_entity_id, relation, status);

                CREATE TABLE IF NOT EXISTS entity_properties (
                    property_id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_id, name)
                );

                CREATE TABLE IF NOT EXISTS evidence_records (
                    evidence_id TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    source_entry_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL DEFAULT '',
                    locator TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(corpus_id, source_entry_id, source_hash, locator)
                );

                CREATE TABLE IF NOT EXISTS entity_evidence (
                    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
                    claim TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    PRIMARY KEY(entity_id, evidence_id, claim)
                );

                CREATE TABLE IF NOT EXISTS entity_revisions (
                    revision_id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
                    previous_revision_id TEXT REFERENCES entity_revisions(revision_id),
                    model TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    accepted INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_entity
                    ON entity_revisions(entity_id, created_at);

                CREATE TABLE IF NOT EXISTS preparation_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_key TEXT NOT NULL UNIQUE,
                    job_type TEXT NOT NULL,
                    subject_entity_id TEXT REFERENCES entities(entity_id),
                    subject_key TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'running', 'complete', 'failed', 'paused')),
                    priority INTEGER NOT NULL DEFAULT 100,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    model TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    locked_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_next
                    ON preparation_jobs(status, priority, created_at);

                CREATE TABLE IF NOT EXISTS job_dependencies (
                    job_id TEXT NOT NULL REFERENCES preparation_jobs(job_id)
                        ON DELETE CASCADE,
                    depends_on_job_id TEXT NOT NULL REFERENCES preparation_jobs(job_id),
                    PRIMARY KEY(job_id, depends_on_job_id),
                    CHECK(job_id <> depends_on_job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_job_dependencies_reverse
                    ON job_dependencies(depends_on_job_id, job_id);

                CREATE TABLE IF NOT EXISTS job_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES preparation_jobs(job_id)
                        ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    reusable INTEGER NOT NULL DEFAULT 1,
                    validation_state TEXT NOT NULL DEFAULT 'candidate'
                        CHECK(validation_state IN (
                            'candidate', 'accepted', 'rejected', 'superseded', 'legacy'
                        )),
                    quality_score REAL
                        CHECK(quality_score IS NULL OR
                              (quality_score >= 0 AND quality_score <= 1)),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_job_artifacts
                    ON job_artifacts(job_id, created_at);

                CREATE TABLE IF NOT EXISTS lexical_discovery_rounds (
                    round_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'claimed'
                        CHECK(status IN ('claimed', 'planned')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lexical_discoveries (
                    discovery_id TEXT PRIMARY KEY,
                    round_id TEXT NOT NULL REFERENCES lexical_discovery_rounds(round_id)
                        ON DELETE CASCADE,
                    language TEXT NOT NULL,
                    term TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    source_kind TEXT NOT NULL
                        CHECK(source_kind IN ('qa-investigation', 'word-origins')),
                    source_entity_id TEXT NOT NULL DEFAULT '',
                    source_artifact_id TEXT NOT NULL DEFAULT '',
                    source_entry_id TEXT NOT NULL DEFAULT '',
                    ordinal INTEGER NOT NULL,
                    planned_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(language, normalized),
                    UNIQUE(round_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_lexical_discoveries_round
                    ON lexical_discoveries(round_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_lexical_discoveries_pending
                    ON lexical_discoveries(planned_at, created_at);

                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    worker_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                        CHECK(status IN ('running', 'stopped')),
                    blocker TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inquiry_threads (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'summarized', 'archived')),
                    compact_summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inquiry_events (
                    event_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES inquiry_threads(thread_id)
                        ON DELETE CASCADE,
                    parent_event_id TEXT REFERENCES inquiry_events(event_id),
                    source_entity_id TEXT REFERENCES entities(entity_id),
                    result_entity_id TEXT REFERENCES entities(entity_id),
                    card_id TEXT NOT NULL DEFAULT '',
                    selected_text TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    response TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    compact_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'summarized', 'archived')),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inquiry_events_thread
                    ON inquiry_events(thread_id, created_at);
                """
            )
            artifact_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(job_artifacts)")
            }
            if "validation_state" not in artifact_columns:
                connection.execute(
                    """ALTER TABLE job_artifacts ADD COLUMN validation_state TEXT
                       NOT NULL DEFAULT 'candidate'
                       CHECK(validation_state IN (
                           'candidate', 'accepted', 'rejected', 'superseded', 'legacy'
                       ))"""
                )
                connection.execute(
                    """UPDATE job_artifacts SET validation_state = CASE
                           WHEN stage LIKE 'accepted-%' THEN 'accepted'
                           WHEN stage = 'retrieved-evidence' THEN 'candidate'
                           ELSE 'legacy' END"""
                )
            if "quality_score" not in artifact_columns:
                connection.execute(
                    """ALTER TABLE job_artifacts ADD COLUMN quality_score REAL
                       CHECK(quality_score IS NULL OR
                             (quality_score >= 0 AND quality_score <= 1))"""
                )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", "3"),
            )
            connection.commit()

    def _upsert_entity(
        self,
        connection: sqlite3.Connection,
        entity_type: str,
        canonical_key: str,
        label: str,
        payload: dict[str, Any] | None = None,
        status: str = "accepted",
        quality_score: float | None = None,
    ) -> str:
        if entity_type not in _ENTITY_TYPES:
            raise ValueError(f"unknown entity type: {entity_type}")
        entity_id = _identifier(entity_type, canonical_key)
        timestamp = _now()
        connection.execute(
            """INSERT INTO entities(
                   entity_id, entity_type, canonical_key, label, status,
                   quality_score, payload, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(canonical_key) DO UPDATE SET
                   label = excluded.label,
                   status = CASE
                       WHEN entities.status = 'accepted' AND excluded.status = 'draft'
                       THEN entities.status ELSE excluded.status END,
                   quality_score = COALESCE(excluded.quality_score, entities.quality_score),
                   payload = excluded.payload,
                   updated_at = excluded.updated_at""",
            (
                entity_id,
                entity_type,
                canonical_key,
                label,
                status,
                quality_score,
                json.dumps(payload or {}, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT entity_id, entity_type FROM entities WHERE canonical_key = ?",
            (canonical_key,),
        ).fetchone()
        if not row or row["entity_type"] != entity_type:
            raise ValueError(f"canonical key already belongs to another type: {canonical_key}")
        return str(row["entity_id"])

    def upsert_term(
        self,
        language: str,
        text: str,
        kind: str = "word",
        *,
        status: str = "accepted",
        quality_score: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        language = _language(language)
        text = text.strip()
        normalized = _normalise(text)
        if not normalized:
            raise ValueError("term text is empty")
        kind = kind.strip().lower() or "word"
        canonical_key = f"term:{language}:{kind}:{normalized}"
        with closing(self._connect()) as connection:
            resolved_payload = payload
            if resolved_payload is None:
                existing = connection.execute(
                    "SELECT payload FROM entities WHERE canonical_key = ?",
                    (canonical_key,),
                ).fetchone()
                if existing is not None:
                    resolved_payload = json.loads(existing["payload"])
            entity_id = self._upsert_entity(
                connection,
                "term",
                canonical_key,
                text,
                resolved_payload,
                status,
                quality_score,
            )
            connection.execute(
                """INSERT INTO terms(entity_id, language, text, normalized, kind)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET text = excluded.text""",
                (entity_id, language, text, normalized, kind),
            )
            connection.commit()
        return entity_id

    def upsert_morpheme(
        self,
        language: str,
        form: str,
        kind: str,
        meaning: str = "",
        *,
        status: str = "accepted",
        quality_score: float | None = None,
    ) -> str:
        language = _language(language)
        form = form.strip()
        normalized = _normalise(form)
        kind = kind.strip().lower()
        if not normalized:
            raise ValueError("morpheme form is empty")
        if kind not in {"prefix", "root", "suffix", "free", "unknown"}:
            raise ValueError(f"invalid morpheme kind: {kind}")
        canonical_key = f"morpheme:{language}:{kind}:{normalized}"
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection,
                "morpheme",
                canonical_key,
                form,
                {"meaning": meaning},
                status,
                quality_score,
            )
            connection.execute(
                """INSERT INTO morphemes(
                       entity_id, language, form, normalized, kind, meaning
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                       form = excluded.form, meaning = excluded.meaning""",
                (entity_id, language, form, normalized, kind, meaning.strip()),
            )
            connection.commit()
        return entity_id

    def add_edge(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        *,
        basis: str = "model",
        confidence: float = 0.5,
        properties: dict[str, Any] | None = None,
        status: str = "accepted",
    ) -> str:
        relation = relation.strip().lower().replace(" ", "-")
        if not relation:
            raise ValueError("edge relation is empty")
        if source_entity_id == target_entity_id:
            raise ValueError("self edges are not accepted knowledge")
        edge_key = f"{source_entity_id}:{relation}:{target_entity_id}"
        edge_id = _identifier("edge", edge_key)
        timestamp = _now()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO entity_edges(
                       edge_id, source_entity_id, target_entity_id, relation,
                       basis, confidence, status, properties, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_entity_id, target_entity_id, relation)
                   DO UPDATE SET basis = excluded.basis,
                       confidence = excluded.confidence,
                       status = excluded.status,
                       properties = excluded.properties,
                       updated_at = excluded.updated_at""",
                (
                    edge_id,
                    source_entity_id,
                    target_entity_id,
                    relation,
                    basis,
                    max(0.0, min(float(confidence), 1.0)),
                    status,
                    json.dumps(properties or {}, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        return edge_id

    def link_morpheme(
        self,
        term_id: str,
        morpheme_id: str,
        ordinal: int,
        surface: str,
        *,
        basis: str = "model",
        confidence: float = 0.5,
    ) -> str:
        if ordinal < 0:
            raise ValueError("morpheme ordinal must be non-negative")
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO term_morphemes(
                       term_id, morpheme_id, ordinal, surface, confidence, basis
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(term_id, ordinal) DO UPDATE SET
                       morpheme_id = excluded.morpheme_id,
                       surface = excluded.surface,
                       confidence = excluded.confidence,
                       basis = excluded.basis""",
                (
                    term_id,
                    morpheme_id,
                    ordinal,
                    surface,
                    max(0.0, min(float(confidence), 1.0)),
                    basis,
                ),
            )
            connection.commit()
        return self.add_edge(
            term_id,
            morpheme_id,
            "has-component",
            basis=basis,
            confidence=confidence,
            properties={"ordinal": ordinal, "surface": surface},
        )

    def retire_morpheme_analysis(self, term_id: str, reason: str) -> dict[str, int]:
        """Quarantine one rejected decomposition while retaining its provenance."""
        reason = reason.strip() or "morpheme analysis rejected by validation"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            morpheme_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT morpheme_id FROM term_morphemes WHERE term_id = ?",
                    (term_id,),
                )
            ]
            component_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM term_morphemes WHERE term_id = ?", (term_id,)
                ).fetchone()[0]
            )
            connection.execute("DELETE FROM term_morphemes WHERE term_id = ?", (term_id,))
            edge_cursor = connection.execute(
                """UPDATE entity_edges SET status = 'archived', updated_at = ?
                   WHERE source_entity_id = ? AND relation = 'has-component'
                     AND status = 'accepted'""",
                (_now(), term_id),
            )
            archived_entities = 0
            for morpheme_id in morpheme_ids:
                remaining = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM term_morphemes WHERE morpheme_id = ?",
                        (morpheme_id,),
                    ).fetchone()[0]
                )
                if not remaining:
                    archived_entities += int(
                        connection.execute(
                            """UPDATE entities SET status = 'archived', updated_at = ?
                               WHERE entity_id = ? AND status = 'accepted'""",
                            (_now(), morpheme_id),
                        ).rowcount
                    )
            artifact_cursor = connection.execute(
                """UPDATE job_artifacts SET validation_state = 'rejected'
                   WHERE stage = 'accepted-morpheme-split'
                     AND validation_state = 'accepted'
                     AND job_id IN (
                         SELECT job_id FROM preparation_jobs
                         WHERE subject_entity_id = ?
                     )""",
                (term_id,),
            )
            connection.commit()
        self.record_revision(
            term_id,
            {"retired_morpheme_ids": morpheme_ids, "reason": reason},
            model="validator",
            reason=reason,
            accepted=False,
        )
        return {
            "components_removed": component_rows,
            "edges_archived": int(edge_cursor.rowcount),
            "morphemes_archived": archived_entities,
            "artifacts_rejected": int(artifact_cursor.rowcount),
        }

    def retire_origin_analysis(self, term_id: str, reason: str) -> dict[str, int]:
        """Quarantine one term's historical branch but keep its fixed parts."""

        reason = reason.strip() or "origin analysis rejected by validation"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            accepted_artifacts = list(
                connection.execute(
                    """SELECT a.payload
                       FROM job_artifacts a
                       JOIN preparation_jobs j ON j.job_id = a.job_id
                       WHERE j.subject_entity_id = ?
                         AND a.stage = 'accepted-origin-branches'
                         AND a.validation_state = 'accepted'""",
                    (term_id,),
                )
            )
            owned_historical_ids: set[str] = set()
            for artifact in accepted_artifacts:
                try:
                    payload = json.loads(artifact["payload"])
                except (TypeError, json.JSONDecodeError):
                    continue
                for branch in payload.get("branches", []):
                    if not isinstance(branch, dict):
                        continue
                    for step in branch.get("steps", []):
                        if not isinstance(step, dict):
                            continue
                        historical_id = str(step.get("historical_form_id", ""))
                        if historical_id:
                            owned_historical_ids.add(historical_id)
            component_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT morpheme_id FROM term_morphemes WHERE term_id = ?",
                    (term_id,),
                )
            }
            relevant: list[sqlite3.Row] = []
            for row in connection.execute(
                """SELECT edge_id, source_entity_id, target_entity_id, properties
                   FROM entity_edges
                   WHERE relation = 'developed-into' AND status = 'accepted'"""
            ):
                try:
                    properties = json.loads(row["properties"])
                except (TypeError, json.JSONDecodeError):
                    properties = {}
                explicitly_owned = str(properties.get("term_id", "")) == term_id
                legacy_owned = (
                    not properties.get("term_id")
                    and str(properties.get("component_id", "")) in component_ids
                    and str(row["source_entity_id"]) in owned_historical_ids
                )
                if explicitly_owned or legacy_owned:
                    relevant.append(row)
            timestamp = _now()
            for row in relevant:
                connection.execute(
                    """UPDATE entity_edges SET status = 'archived', updated_at = ?
                       WHERE edge_id = ?""",
                    (timestamp, row["edge_id"]),
                )
            candidate_ids = {
                str(row[column])
                for row in relevant
                for column in ("source_entity_id", "target_entity_id")
            }
            archived_entities = 0
            for entity_id in candidate_ids:
                entity = connection.execute(
                    "SELECT entity_type FROM entities WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()
                if entity is None or entity["entity_type"] != "historical-form":
                    continue
                still_used = connection.execute(
                    """SELECT 1 FROM entity_edges
                       WHERE status = 'accepted'
                         AND (source_entity_id = ? OR target_entity_id = ?)
                       LIMIT 1""",
                    (entity_id, entity_id),
                ).fetchone()
                if still_used is None:
                    archived_entities += int(
                        connection.execute(
                            """UPDATE entities SET status = 'archived', updated_at = ?
                               WHERE entity_id = ? AND status = 'accepted'""",
                            (timestamp, entity_id),
                        ).rowcount
                    )
            artifact_cursor = connection.execute(
                """UPDATE job_artifacts SET validation_state = 'rejected'
                   WHERE stage IN ('accepted-origin-branches', 'accepted-origin-card')
                     AND validation_state = 'accepted'
                     AND job_id IN (
                         SELECT job_id FROM preparation_jobs
                         WHERE subject_entity_id = ?
                     )""",
                (term_id,),
            )
            connection.commit()
        self.record_revision(
            term_id,
            {
                "reason": reason,
                "origin_edges_archived": len(relevant),
                "historical_forms_archived": archived_entities,
            },
            model="validator",
            reason=reason,
            accepted=False,
        )
        return {
            "edges_archived": len(relevant),
            "historical_forms_archived": archived_entities,
            "artifacts_rejected": int(artifact_cursor.rowcount),
        }

    def retire_language_analysis(
        self, term_id: str, language: str, reason: str
    ) -> dict[str, int]:
        """Quarantine one bad translation/pronunciation without touching others."""

        language = _language(language)
        reason = reason.strip() or "language analysis rejected by validation"
        subject_key = f"term:{term_id}"
        artifacts = [
            artifact
            for stage in ("accepted-translation", "accepted-pronunciation")
            for artifact in self.artifacts_for_subject(
                subject_key, stage=stage, validation_state="accepted"
            )
            if artifact["language"] == language
        ]
        owned_ids: set[str] = set()
        target_ids: set[str] = set()
        for artifact in artifacts:
            payload = artifact["payload"]
            for field in ("translation_id", "pronunciation_id"):
                if str(payload.get(field, "")):
                    owned_ids.add(str(payload[field]))
            if str(payload.get("target_term_id", "")):
                target_ids.add(str(payload["target_term_id"]))

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            artifact_cursor = connection.execute(
                """UPDATE job_artifacts SET validation_state = 'rejected'
                   WHERE validation_state = 'accepted' AND language = ?
                     AND stage IN ('accepted-translation', 'accepted-pronunciation')
                     AND job_id IN (
                         SELECT job_id FROM preparation_jobs
                         WHERE subject_entity_id = ?
                     )""",
                (language, term_id),
            )
            entities_rejected = 0
            edges_archived = 0
            if owned_ids:
                placeholders = ",".join("?" for _ in owned_ids)
                parameters = tuple(owned_ids)
                edges_archived += int(
                    connection.execute(
                        f"""UPDATE entity_edges SET status = 'archived', updated_at = ?
                            WHERE status = 'accepted'
                              AND (source_entity_id IN ({placeholders})
                                   OR target_entity_id IN ({placeholders}))""",
                        (_now(), *parameters, *parameters),
                    ).rowcount
                )
                entities_rejected += int(
                    connection.execute(
                        f"""UPDATE entities SET status = 'rejected', updated_at = ?
                            WHERE status = 'accepted' AND entity_id IN ({placeholders})""",
                        (_now(), *parameters),
                    ).rowcount
                )
            orphan_terms_rejected = 0
            for target_id in target_ids:
                remaining_edges = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM entity_edges
                           WHERE status = 'accepted'
                             AND (source_entity_id = ? OR target_entity_id = ?)""",
                        (target_id, target_id),
                    ).fetchone()[0]
                )
                if not remaining_edges:
                    orphan_terms_rejected += int(
                        connection.execute(
                            """UPDATE entities SET status = 'rejected', updated_at = ?
                               WHERE entity_id = ? AND status = 'accepted'""",
                            (_now(), target_id),
                        ).rowcount
                    )
            connection.commit()
        self.record_revision(
            term_id,
            {
                "language": language,
                "retired_entity_ids": sorted(owned_ids),
                "retired_target_term_ids": sorted(target_ids),
                "reason": reason,
            },
            model="validator",
            reason=reason,
            accepted=False,
        )
        return {
            "artifacts_rejected": int(artifact_cursor.rowcount),
            "entities_rejected": entities_rejected,
            "edges_archived": edges_archived,
            "orphan_terms_rejected": orphan_terms_rejected,
        }

    def add_meaning(
        self,
        term_id: str,
        language: str,
        definition: str,
        *,
        part_of_speech: str = "",
        register_label: str = "",
        domain_label: str = "",
        sense_order: int = 0,
        status: str = "accepted",
    ) -> str:
        language = _language(language)
        definition = definition.strip()
        if not definition:
            raise ValueError("meaning definition is empty")
        canonical_key = (
            f"meaning:{term_id}:{language}:{sense_order}:{_normalise(definition)}"
        )
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection, "meaning", canonical_key, definition, status=status
            )
            connection.execute(
                """INSERT INTO meanings(
                       entity_id, term_id, language, definition, part_of_speech,
                       register_label, domain_label, sense_order
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                       definition = excluded.definition,
                       part_of_speech = excluded.part_of_speech,
                       register_label = excluded.register_label,
                       domain_label = excluded.domain_label""",
                (
                    entity_id,
                    term_id,
                    language,
                    definition,
                    part_of_speech.strip(),
                    register_label.strip(),
                    domain_label.strip(),
                    sense_order,
                ),
            )
            connection.commit()
        self.add_edge(term_id, entity_id, "has-meaning", basis="reviewed")
        return entity_id

    def add_pronunciation(
        self,
        term_id: str,
        language: str,
        system: str,
        reading: str,
        segments: Iterable[dict[str, Any]] = (),
        *,
        dialect: str = "",
        status: str = "accepted",
        quality_score: float | None = None,
    ) -> str:
        language = _language(language)
        system = system.strip().lower()
        reading = reading.strip()
        if not system or not reading:
            raise ValueError("pronunciation system and reading are required")
        canonical_key = (
            f"pronunciation:{term_id}:{language}:{system}:{_normalise(reading)}:"
            f"{_normalise(dialect)}"
        )
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection,
                "pronunciation",
                canonical_key,
                reading,
                status=status,
                quality_score=quality_score,
            )
            connection.execute(
                """INSERT INTO pronunciations(
                       entity_id, term_id, language, system, reading, dialect
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET reading = excluded.reading""",
                (entity_id, term_id, language, system, reading, dialect.strip()),
            )
            connection.execute(
                "DELETE FROM phoneme_segments WHERE pronunciation_id = ?", (entity_id,)
            )
            for ordinal, item in enumerate(segments):
                segment_id = _identifier("segment", f"{entity_id}:{ordinal}")
                connection.execute(
                    """INSERT INTO phoneme_segments(
                           segment_id, pronunciation_id, ordinal, grapheme, phoneme,
                           syllable, color_key, features
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        segment_id,
                        entity_id,
                        ordinal,
                        str(item.get("grapheme", "")),
                        str(item.get("phoneme", "")),
                        str(item.get("syllable", "")),
                        str(item.get("color_key", "")),
                        json.dumps(item.get("features", {}), ensure_ascii=False),
                    ),
                )
            connection.commit()
        self.add_edge(term_id, entity_id, "has-pronunciation", basis="reviewed")
        return entity_id

    def add_translation(
        self,
        source_term_id: str,
        target_language: str,
        text: str,
        *,
        transliteration: str = "",
        usage_note: str = "",
        source_meaning_id: str | None = None,
        target_term_id: str | None = None,
        status: str = "accepted",
        quality_score: float | None = None,
    ) -> str:
        target_language = _language(target_language)
        text = text.strip()
        normalized = _normalise(text)
        if not normalized:
            raise ValueError("translation text is empty")
        sense_key = source_meaning_id or "all"
        canonical_key = (
            f"translation:{source_term_id}:{sense_key}:{target_language}:{normalized}"
        )
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection,
                "translation",
                canonical_key,
                text,
                {"transliteration": transliteration, "usage_note": usage_note},
                status,
                quality_score,
            )
            connection.execute(
                """INSERT INTO translations(
                       entity_id, source_term_id, source_meaning_id,
                       target_language, target_term_id, text, normalized,
                       transliteration, usage_note
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                       target_term_id = excluded.target_term_id,
                       text = excluded.text,
                       transliteration = excluded.transliteration,
                       usage_note = excluded.usage_note""",
                (
                    entity_id,
                    source_term_id,
                    source_meaning_id,
                    target_language,
                    target_term_id,
                    text,
                    normalized,
                    transliteration.strip(),
                    usage_note.strip(),
                ),
            )
            connection.commit()
        self.add_edge(source_term_id, entity_id, "has-translation", basis="reviewed")
        if target_term_id:
            self.add_edge(entity_id, target_term_id, "renders-as", basis="reviewed")
        return entity_id

    def add_historical_form(
        self,
        language: str,
        form: str,
        *,
        period_label: str = "",
        date_min: int | None = None,
        date_max: int | None = None,
        meaning: str = "",
        status: str = "accepted",
        quality_score: float | None = None,
    ) -> str:
        language = _language(language)
        form = form.strip()
        normalized = _normalise(form)
        if not normalized:
            raise ValueError("historical form is empty")
        canonical_key = (
            f"historical-form:{language}:{normalized}:{_normalise(period_label)}"
        )
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection,
                "historical-form",
                canonical_key,
                form,
                {"period": period_label, "meaning": meaning},
                status=status,
                quality_score=quality_score,
            )
            connection.execute(
                """INSERT INTO historical_forms(
                       entity_id, language, form, normalized, period_label,
                       date_min, date_max, meaning
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                       form = excluded.form, date_min = excluded.date_min,
                       date_max = excluded.date_max, meaning = excluded.meaning""",
                (
                    entity_id,
                    language,
                    form,
                    normalized,
                    period_label.strip(),
                    date_min,
                    date_max,
                    meaning.strip(),
                ),
            )
            connection.commit()
        return entity_id

    def add_history_event(
        self,
        subject_entity_id: str,
        event_type: str,
        description: str,
        *,
        language: str = "",
        period_label: str = "",
        date_min: int | None = None,
        date_max: int | None = None,
        properties: dict[str, Any] | None = None,
        status: str = "accepted",
    ) -> str:
        language = _language(language) if language else ""
        event_type = event_type.strip().lower().replace(" ", "-")
        description = description.strip()
        if not event_type or not description:
            raise ValueError("history event type and description are required")
        canonical_key = (
            f"history:{subject_entity_id}:{event_type}:{language}:"
            f"{period_label.strip()}:{date_min}:{date_max}:{_normalise(description)}"
        )
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection,
                "history-event",
                canonical_key,
                period_label.strip() or event_type,
                properties,
                status=status,
            )
            connection.execute(
                """INSERT INTO history_events(
                       entity_id, subject_entity_id, event_type, language,
                       period_label, date_min, date_max, description, properties
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                       description = excluded.description,
                       properties = excluded.properties""",
                (
                    entity_id,
                    subject_entity_id,
                    event_type,
                    language,
                    period_label.strip(),
                    date_min,
                    date_max,
                    description,
                    json.dumps(properties or {}, ensure_ascii=False),
                ),
            )
            connection.commit()
        self.add_edge(subject_entity_id, entity_id, "has-history", basis="reviewed")
        return entity_id

    def upsert_content_item(
        self,
        kind: str,
        language: str,
        text: str,
        *,
        source_key: str = "",
        status: str = "accepted",
    ) -> str:
        kind = kind.strip().lower()
        if kind not in {"answer", "question", "sentence"}:
            raise ValueError(f"invalid content kind: {kind}")
        language = _language(language)
        text = text.strip()
        normalized = _normalise(text)
        if not normalized:
            raise ValueError("content text is empty")
        canonical_key = f"content:{kind}:{language}:{source_key}:{normalized}"
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection, "content-item", canonical_key, text, status=status
            )
            connection.execute(
                """INSERT INTO content_items(
                       entity_id, kind, language, text, normalized, source_key
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET text = excluded.text""",
                (entity_id, kind, language, text, normalized, source_key.strip()),
            )
            connection.commit()
        return entity_id

    def acquire_card_book_card(self, card: dict[str, Any]) -> dict[str, Any]:
        """Persist one accepted Answer/Question card as reusable reviewed atoms.

        The card ledger remains the versioned presentation record. This method
        mirrors only the reviewed book text, language relationships, and
        retrieval-owned citations into the normalized knowledge store so later
        grammar or word investigations can reuse them without parsing the UI.
        It is deliberately idempotent and never treats model reflection as book
        evidence.
        """

        kind = str(card.get("mode", "")).strip().lower()
        if kind not in {"answer", "question"}:
            return {}
        card_id = str(card.get("card_id", "")).strip()
        evidence_values = card.get("evidence")
        evidence_records = (
            [item for item in evidence_values if isinstance(item, dict)]
            if isinstance(evidence_values, list)
            else []
        )
        if not card_id or not evidence_records:
            raise ValueError("card-book acquisition requires a card ID and evidence")
        source_key = str(evidence_records[0].get("entry_id", "")).strip()
        if not source_key:
            raise ValueError("card-book acquisition requires a stable source entry ID")

        language_values = {
            "en": str((card.get("english") or {}).get("term", "")).strip(),
            "ja": str((card.get("japanese") or {}).get("term", "")).strip(),
            "zh": str((card.get("chinese") or {}).get("simplified", "")).strip(),
        }
        if any(not value for value in language_values.values()):
            raise ValueError("card-book acquisition requires reviewed en, ja, and zh text")

        entities = {
            language: self.upsert_content_item(
                kind,
                language,
                text,
                source_key=source_key,
                status="accepted",
            )
            for language, text in language_values.items()
        }
        evidence_ids: list[str] = []
        for record in evidence_records:
            corpus_id = str(record.get("corpus_id", "")).strip()
            entry_id = str(record.get("entry_id", "")).strip()
            if not corpus_id or not entry_id:
                continue
            pages = record.get("pages")
            locator = str(record.get("locator", "")).strip()
            if not locator and isinstance(pages, list):
                locator = ", ".join(str(page) for page in pages)
            evidence_id = self.add_evidence(
                corpus_id,
                entry_id,
                source_hash=str(record.get("source_hash", "")),
                locator=locator,
                excerpt=str(record.get("excerpt", "")),
                payload=record,
            )
            evidence_ids.append(evidence_id)
            for language, entity_id in entities.items():
                self.link_evidence(
                    entity_id,
                    evidence_id,
                    claim=f"reviewed {kind} text ({language})",
                    confidence=1.0,
                )

        if not evidence_ids:
            raise ValueError("card-book acquisition found no usable citation records")
        source_entity_id = entities["en"]
        for language in ("ja", "zh"):
            self.add_edge(
                source_entity_id,
                entities[language],
                "reviewed-translation",
                basis="book",
                confidence=1.0,
                properties={
                    "card_id": card_id,
                    "source_entry_id": source_key,
                    "target_language": language,
                },
            )
        for language, entity_id in entities.items():
            self.set_property(entity_id, "source_card_id", card_id)
            self.set_property(entity_id, "source_entry_id", source_key)
            self.set_property(entity_id, "reviewed_language", language)
        return {
            "source_entity_id": source_entity_id,
            "language_entity_ids": entities,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
        }

    def add_grammar_analysis(
        self,
        subject_entity_id: str,
        language: str,
        summary: str,
        parts: Iterable[dict[str, Any]],
        *,
        analysis_type: str = "sentence",
        basis: str = "reviewed",
        status: str = "accepted",
        quality_score: float | None = None,
    ) -> str:
        language = _language(language)
        part_values = [dict(item) for item in parts]
        content_key = json.dumps(part_values, ensure_ascii=False, sort_keys=True)
        canonical_key = (
            f"grammar:{subject_entity_id}:{language}:{analysis_type}:"
            f"{uuid.uuid5(uuid.NAMESPACE_URL, content_key)}"
        )
        with closing(self._connect()) as connection:
            entity_id = self._upsert_entity(
                connection,
                "grammar-analysis",
                canonical_key,
                summary.strip() or analysis_type,
                status=status,
                quality_score=quality_score,
            )
            connection.execute(
                """INSERT INTO grammar_analyses(
                       entity_id, subject_entity_id, language, analysis_type, summary
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET summary = excluded.summary""",
                (
                    entity_id,
                    subject_entity_id,
                    language,
                    analysis_type.strip(),
                    summary.strip(),
                ),
            )
            connection.execute(
                "DELETE FROM grammar_parts WHERE analysis_id = ?", (entity_id,)
            )
            for ordinal, item in enumerate(part_values):
                part_id = _identifier("grammar-part", f"{entity_id}:{ordinal}")
                connection.execute(
                    """INSERT INTO grammar_parts(
                           part_id, analysis_id, ordinal, surface, lemma, role,
                           part_of_speech, reading, color_key, features
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        part_id,
                        entity_id,
                        ordinal,
                        str(item.get("surface", "")),
                        str(item.get("lemma", "")),
                        str(item.get("role", "")),
                        str(item.get("part_of_speech", "")),
                        str(item.get("reading", "")),
                        str(item.get("color_key", "")),
                        json.dumps(item.get("features", {}), ensure_ascii=False),
                    ),
                )
            if status == "accepted":
                previous_ids = [
                    str(row["entity_id"])
                    for row in connection.execute(
                        """SELECT entity_id FROM grammar_analyses
                           WHERE subject_entity_id = ? AND language = ?
                             AND analysis_type = ? AND entity_id <> ?""",
                        (
                            subject_entity_id,
                            language,
                            analysis_type.strip(),
                            entity_id,
                        ),
                    )
                ]
                if previous_ids:
                    placeholders = ",".join("?" for _ in previous_ids)
                    timestamp = _now()
                    connection.execute(
                        f"UPDATE entities SET status = 'archived', updated_at = ? "
                        f"WHERE entity_id IN ({placeholders})",
                        (timestamp, *previous_ids),
                    )
                    connection.execute(
                        f"UPDATE entity_edges SET status = 'archived', updated_at = ? "
                        f"WHERE target_entity_id IN ({placeholders}) "
                        "AND relation = 'has-grammar'",
                        (timestamp, *previous_ids),
                    )
            connection.commit()
        self.add_edge(subject_entity_id, entity_id, "has-grammar", basis=basis)
        return entity_id

    def set_property(self, entity_id: str, name: str, value: Any) -> str:
        name = name.strip()
        if not name:
            raise ValueError("property name is empty")
        property_id = _identifier("property", f"{entity_id}:{name}")
        timestamp = _now()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO entity_properties(
                       property_id, entity_id, name, value, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id, name) DO UPDATE SET
                       value = excluded.value, updated_at = excluded.updated_at""",
                (
                    property_id,
                    entity_id,
                    name,
                    json.dumps(value, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        return property_id

    def record_revision(
        self,
        entity_id: str,
        payload: Any,
        *,
        model: str = "",
        prompt_version: str = "",
        reason: str = "",
        accepted: bool = False,
    ) -> str:
        revision_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            previous = connection.execute(
                """SELECT revision_id FROM entity_revisions
                   WHERE entity_id = ? ORDER BY created_at DESC LIMIT 1""",
                (entity_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO entity_revisions(
                       revision_id, entity_id, previous_revision_id, model,
                       prompt_version, reason, accepted, payload, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    entity_id,
                    previous["revision_id"] if previous else None,
                    model.strip(),
                    prompt_version.strip(),
                    reason.strip(),
                    int(accepted),
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
            connection.commit()
        return revision_id

    def create_inquiry_thread(self, title: str) -> str:
        title = title.strip()
        if not title:
            raise ValueError("inquiry title is empty")
        thread_id = str(uuid.uuid4())
        timestamp = _now()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO inquiry_threads(
                       thread_id, title, created_at, updated_at
                   ) VALUES (?, ?, ?, ?)""",
                (thread_id, title, timestamp, timestamp),
            )
            connection.commit()
        return thread_id

    def has_inquiry_thread(self, thread_id: str) -> bool:
        thread_id = thread_id.strip()
        if not thread_id:
            return False
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM inquiry_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return row is not None

    def save_inquiry_event(
        self,
        thread_id: str,
        query: str,
        *,
        response: str = "",
        parent_event_id: str | None = None,
        source_entity_id: str | None = None,
        result_entity_id: str | None = None,
        card_id: str = "",
        selected_text: str = "",
        model: str = "",
        compact_summary: str = "",
    ) -> str:
        query = query.strip()
        if not query:
            raise ValueError("inquiry query is empty")
        event_id = str(uuid.uuid4())
        timestamp = _now()
        with closing(self._connect()) as connection:
            thread = connection.execute(
                "SELECT 1 FROM inquiry_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise KeyError(thread_id)
            if parent_event_id:
                parent = connection.execute(
                    "SELECT thread_id FROM inquiry_events WHERE event_id = ?",
                    (parent_event_id,),
                ).fetchone()
                if parent is None or str(parent["thread_id"]) != thread_id:
                    raise ValueError("parent inquiry event is not in this thread")
            connection.execute(
                """INSERT INTO inquiry_events(
                       event_id, thread_id, parent_event_id, source_entity_id,
                       result_entity_id, card_id, selected_text, query, response,
                       model, compact_summary, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    thread_id,
                    parent_event_id,
                    source_entity_id,
                    result_entity_id,
                    card_id.strip(),
                    selected_text.strip(),
                    query,
                    response,
                    model.strip(),
                    compact_summary.strip(),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE inquiry_threads SET updated_at = ? WHERE thread_id = ?",
                (timestamp, thread_id),
            )
            connection.commit()
        return event_id

    def add_evidence(
        self,
        corpus_id: str,
        source_entry_id: str,
        *,
        source_hash: str = "",
        locator: str = "",
        excerpt: str = "",
        payload: dict[str, Any] | None = None,
    ) -> str:
        key = f"{corpus_id}:{source_entry_id}:{source_hash}:{locator}"
        evidence_id = _identifier("evidence", key)
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO evidence_records(
                       evidence_id, corpus_id, source_entry_id, source_hash,
                       locator, excerpt, payload
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(corpus_id, source_entry_id, source_hash, locator)
                   DO UPDATE SET excerpt = excluded.excerpt, payload = excluded.payload""",
                (
                    evidence_id,
                    corpus_id.strip(),
                    source_entry_id.strip(),
                    source_hash.strip(),
                    locator.strip(),
                    excerpt.strip(),
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            connection.commit()
        return evidence_id

    def link_evidence(
        self,
        entity_id: str,
        evidence_id: str,
        *,
        claim: str = "",
        confidence: float = 0.5,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO entity_evidence(
                       entity_id, evidence_id, claim, confidence
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(entity_id, evidence_id, claim) DO UPDATE SET
                       confidence = excluded.confidence""",
                (
                    entity_id,
                    evidence_id,
                    claim.strip(),
                    max(0.0, min(float(confidence), 1.0)),
                ),
            )
            connection.commit()

    def evidence_records(self, evidence_ids: Iterable[str]) -> list[dict[str, Any]]:
        ordered = list(dict.fromkeys(str(item) for item in evidence_ids if str(item)))
        if not ordered:
            return []
        placeholders = ",".join("?" for _ in ordered)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT evidence_id, corpus_id, source_entry_id, source_hash,
                           locator, excerpt, payload
                    FROM evidence_records WHERE evidence_id IN ({placeholders})""",
                ordered,
            ).fetchall()
        by_id = {str(row["evidence_id"]): row for row in rows}
        return [
            {
                **dict(by_id[evidence_id]),
                "payload": json.loads(by_id[evidence_id]["payload"]),
            }
            for evidence_id in ordered
            if evidence_id in by_id
        ]

    def record_worker_heartbeat(
        self,
        blocker: str = "",
        *,
        status: str = "running",
        worker_key: str = "atomic",
    ) -> None:
        """Persist one cheap liveness row owned by the long-running worker."""

        if status not in {"running", "stopped"}:
            raise ValueError(f"invalid worker status: {status!r}")
        worker_key = worker_key.strip()
        if not worker_key:
            raise ValueError("worker key is required")
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO worker_heartbeat(worker_key, status, blocker, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(worker_key) DO UPDATE SET
                       status = excluded.status,
                       blocker = excluded.blocker,
                       updated_at = excluded.updated_at""",
                (worker_key, status, blocker.strip()[:500], _now()),
            )
            connection.commit()

    def worker_status(
        self, *, worker_key: str = "atomic", max_age_seconds: float = 15.0
    ) -> dict[str, Any]:
        """Report process liveness separately from its current safety blocker."""

        max_age_seconds = max(5.0, min(float(max_age_seconds), 3600.0))
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT status, blocker, updated_at,
                          (julianday('now') - julianday(updated_at)) * 86400.0
                              AS age_seconds
                   FROM worker_heartbeat WHERE worker_key = ?""",
                (worker_key.strip(),),
            ).fetchone()
        if row is None:
            return {
                "ready": False,
                "generation_ready": False,
                "status": "missing",
                "blocker": "atomic worker heartbeat unavailable",
                "last_blocker": "",
                "updated_at": "",
                "age_seconds": None,
            }
        raw_age = row["age_seconds"]
        age_seconds = max(0.0, float(raw_age)) if raw_age is not None else None
        reported_blocker = str(row["blocker"])
        running = str(row["status"]) == "running"
        fresh = running and age_seconds is not None and age_seconds <= max_age_seconds
        if not running:
            blocker = reported_blocker or "atomic worker is stopped"
        elif not fresh:
            blocker = "atomic worker heartbeat is stale"
        else:
            blocker = reported_blocker
        return {
            "ready": fresh,
            "generation_ready": fresh and not blocker,
            "status": str(row["status"]),
            "blocker": blocker,
            "last_blocker": reported_blocker,
            "updated_at": str(row["updated_at"]),
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        }

    def requeue_failed_jobs(self, job_ids: Iterable[str]) -> int:
        """Atomically retry only terminal jobs explicitly named by one plan."""

        selected = tuple(
            dict.fromkeys(str(job_id).strip() for job_id in job_ids if str(job_id).strip())
        )
        if not selected:
            return 0
        if len(selected) > 256:
            raise ValueError("a retry plan cannot contain more than 256 jobs")
        placeholders = ",".join("?" for _ in selected)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""UPDATE preparation_jobs
                    SET status = 'queued', attempts = 0, error = '', locked_at = '',
                        updated_at = ?
                    WHERE status = 'failed' AND job_id IN ({placeholders})""",
                (_now(), *selected),
            )
            connection.commit()
        return int(cursor.rowcount)

    def enqueue_job(
        self,
        job_type: str,
        subject_key: str,
        *,
        subject_entity_id: str | None = None,
        language: str = "",
        priority: int = 100,
        model: str = "",
        prompt_version: str = "",
        source_fingerprint: str = "",
        max_attempts: int = 2,
        depends_on: Iterable[str] = (),
    ) -> str:
        language = _language(language) if language else ""
        job_key = ":".join(
            (
                job_type.strip(),
                subject_key.strip(),
                language,
                prompt_version.strip(),
                source_fingerprint.strip(),
            )
        )
        job_id = _identifier("job", job_key)
        prerequisites = tuple(
            dict.fromkeys(str(item).strip() for item in depends_on if str(item).strip())
        )
        if job_id in prerequisites:
            raise ValueError("a job cannot depend on itself")
        timestamp = _now()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO preparation_jobs(
                       job_id, job_key, job_type, subject_entity_id, subject_key,
                       language, priority, max_attempts, model, prompt_version,
                       source_fingerprint, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_key) DO UPDATE SET
                       priority = MIN(preparation_jobs.priority, excluded.priority),
                       model = CASE WHEN preparation_jobs.status = 'complete'
                                    THEN preparation_jobs.model ELSE excluded.model END,
                       updated_at = excluded.updated_at""",
                (
                    job_id,
                    job_key,
                    job_type.strip(),
                    subject_entity_id,
                    subject_key.strip(),
                    language,
                    priority,
                    max(1, max_attempts),
                    model.strip(),
                    prompt_version.strip(),
                    source_fingerprint.strip(),
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO job_dependencies(job_id, depends_on_job_id)
                   VALUES (?, ?)""",
                ((job_id, prerequisite) for prerequisite in prerequisites),
            )
            connection.commit()
            row = connection.execute(
                "SELECT job_id FROM preparation_jobs WHERE job_key = ?", (job_key,)
            ).fetchone()
        return str(row["job_id"])

    def claim_next_job(
        self, job_types: Iterable[str] | None = None
    ) -> dict[str, Any] | None:
        allowed = tuple(dict.fromkeys(str(item).strip() for item in (job_types or ())))
        type_clause = ""
        parameters: tuple[Any, ...] = ()
        if allowed:
            type_clause = " AND job.job_type IN (" + ",".join("?" for _ in allowed) + ")"
            parameters = allowed
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._fail_blocked_jobs(connection)
            row = connection.execute(
                """SELECT job.* FROM preparation_jobs AS job
                   WHERE job.status = 'queued' AND job.attempts < job.max_attempts
                     """ + type_clause + """
                     AND NOT EXISTS (
                         SELECT 1 FROM job_dependencies AS dependency
                         JOIN preparation_jobs AS prerequisite
                           ON prerequisite.job_id = dependency.depends_on_job_id
                         WHERE dependency.job_id = job.job_id
                           AND prerequisite.status <> 'complete'
                     )
                   ORDER BY
                       CASE WHEN job.subject_key LIKE 'term:%' THEN 0 ELSE 1 END,
                       priority, created_at
                   LIMIT 1""",
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            timestamp = _now()
            connection.execute(
                """UPDATE preparation_jobs
                   SET status = 'running', attempts = attempts + 1,
                       locked_at = ?, updated_at = ?
                   WHERE job_id = ?""",
                (timestamp, timestamp, row["job_id"]),
            )
            connection.commit()
            claimed = connection.execute(
                "SELECT * FROM preparation_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        return dict(claimed) if claimed else None

    def term_record(self, entity_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT term.entity_id, term.language, term.text, term.normalized,
                          term.kind, entity.status, entity.quality_score,
                          entity.payload
                   FROM terms AS term JOIN entities AS entity
                     ON entity.entity_id = term.entity_id
                   WHERE term.entity_id = ?""",
                (entity_id,),
            ).fetchone()
        if row is None:
            raise KeyError(entity_id)
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def content_record(self, entity_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT content.entity_id, content.kind, content.language,
                          content.text, content.normalized, content.source_key,
                          entity.status, entity.quality_score, entity.payload
                   FROM content_items AS content JOIN entities AS entity
                     ON entity.entity_id = content.entity_id
                   WHERE content.entity_id = ?""",
                (entity_id,),
            ).fetchone()
        if row is None:
            raise KeyError(entity_id)
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def content_for_card(
        self, card_id: str, language: str = "en"
    ) -> dict[str, Any] | None:
        card_id = card_id.strip()
        if not card_id:
            return None
        language = _language(language)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT content.entity_id
                   FROM content_items AS content
                   JOIN entity_properties AS property
                     ON property.entity_id = content.entity_id
                   WHERE property.name = 'source_card_id' AND property.value = ?
                     AND content.language = ?
                   LIMIT 1""",
                (json.dumps(card_id, ensure_ascii=False), language),
            ).fetchone()
        return self.content_record(str(row["entity_id"])) if row else None

    def card_book_enrichment_state(self) -> dict[str, set[str]]:
        """Find fully acquired cards and only their genuinely missing grammar.

        This is deliberately one read transaction. A full-database sync must
        not replay hundreds of idempotent writes or open a connection for every
        card/language pair on a small Pi.
        """

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT property.value AS card_id, content.entity_id,
                          content.language,
                          EXISTS (
                              SELECT 1 FROM grammar_analyses AS analysis
                              JOIN entities AS grammar_entity
                                ON grammar_entity.entity_id = analysis.entity_id
                              WHERE analysis.subject_entity_id = content.entity_id
                                AND grammar_entity.status = 'accepted'
                          ) AS grammar_ready,
                          EXISTS (
                              SELECT 1 FROM preparation_jobs AS job
                              WHERE job.subject_entity_id = content.entity_id
                                AND job.job_type = 'prepare-grammar-parts'
                                AND job.status IN ('queued', 'running')
                          ) AS grammar_active
                   FROM entity_properties AS property
                   JOIN content_items AS content
                     ON content.entity_id = property.entity_id
                   JOIN entities AS content_entity
                     ON content_entity.entity_id = content.entity_id
                   WHERE property.name = 'source_card_id'
                     AND content.language IN ('en', 'ja', 'zh')
                     AND content_entity.status = 'accepted'
                   ORDER BY property.value, content.language"""
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            try:
                card_id = str(json.loads(row["card_id"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if card_id:
                grouped.setdefault(card_id, []).append(row)
        reviewed: set[str] = set()
        needs_grammar: set[str] = set()
        for card_id, items in grouped.items():
            languages = {str(item["language"]) for item in items}
            if languages != {"en", "ja", "zh"}:
                continue
            reviewed.add(card_id)
            if any(
                not bool(item["grammar_ready"]) and not bool(item["grammar_active"])
                for item in items
            ):
                needs_grammar.add(card_id)
        return {"reviewed": reviewed, "needs_grammar": needs_grammar}

    def grammar_for_content(self, content_entity_id: str) -> dict[str, Any] | None:
        """Return the current accepted sentence analysis and its exact parts."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT analysis.entity_id, analysis.language,
                          analysis.analysis_type, analysis.summary,
                          entity.quality_score
                   FROM grammar_analyses AS analysis
                   JOIN entities AS entity ON entity.entity_id = analysis.entity_id
                   WHERE analysis.subject_entity_id = ?
                     AND entity.status = 'accepted'
                   ORDER BY entity.updated_at DESC, entity.created_at DESC
                   LIMIT 1""",
                (content_entity_id,),
            ).fetchone()
            if row is None:
                return None
            parts = [
                {
                    **dict(part),
                    "features": json.loads(part["features"]),
                }
                for part in connection.execute(
                    """SELECT ordinal, surface, lemma, role, part_of_speech,
                              reading, color_key, features
                       FROM grammar_parts WHERE analysis_id = ?
                       ORDER BY ordinal""",
                    (row["entity_id"],),
                )
            ]
        return {**dict(row), "parts": parts}

    def evidence_for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT evidence.evidence_id, evidence.corpus_id,
                          evidence.source_entry_id, evidence.source_hash,
                          evidence.locator, evidence.excerpt, evidence.payload,
                          link.claim, link.confidence
                   FROM entity_evidence AS link
                   JOIN evidence_records AS evidence
                     ON evidence.evidence_id = link.evidence_id
                   WHERE link.entity_id = ?
                   ORDER BY evidence.corpus_id, evidence.source_entry_id""",
                (entity_id,),
            ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])} for row in rows
        ]

    def investigation_terms(self, content_entity_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT term.entity_id, term.text, term.normalized, term.kind,
                          edge.confidence, edge.properties
                   FROM entity_edges AS edge
                   JOIN terms AS term ON term.entity_id = edge.target_entity_id
                   JOIN entities AS entity ON entity.entity_id = term.entity_id
                   WHERE edge.source_entity_id = ?
                     AND edge.relation = 'contains-investigation-term'
                     AND edge.status = 'accepted' AND entity.status = 'accepted'""",
                (content_entity_id,),
            ).fetchall()
        values = [
            {
                "entity_id": str(row["entity_id"]),
                "term": str(row["text"]),
                "kind": str(row["kind"]),
                "confidence": float(row["confidence"]),
                **json.loads(row["properties"]),
            }
            for row in rows
        ]
        return sorted(values, key=lambda item: (int(item.get("ordinal", 999)), item["term"]))

    def investigation_suggestion_groups(
        self,
        excluded: Iterable[str] = (),
        *,
        language: str = "en",
    ) -> list[dict[str, Any]]:
        """Return accepted Q/A suggestion groups that still contain unseen terms."""

        language = _language(language)
        excluded_keys = {_normalise(str(value)) for value in excluded if str(value).strip()}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT content.entity_id AS source_entity_id,
                          content.source_key, content.kind,
                          source.created_at AS source_created_at,
                          artifact.artifact_id AS source_artifact_id,
                          artifact.payload AS artifact_payload,
                          term.entity_id AS term_id, term.text AS term,
                          term.normalized, edge.confidence, edge.properties
                   FROM content_items AS content
                   JOIN entities AS source ON source.entity_id = content.entity_id
                   JOIN entity_edges AS edge
                     ON edge.source_entity_id = content.entity_id
                    AND edge.relation = 'contains-investigation-term'
                    AND edge.status = 'accepted'
                   JOIN terms AS term ON term.entity_id = edge.target_entity_id
                   JOIN entities AS target ON target.entity_id = term.entity_id
                   JOIN job_artifacts AS artifact ON artifact.artifact_id = (
                       SELECT candidate.artifact_id
                       FROM job_artifacts AS candidate
                       JOIN preparation_jobs AS extraction
                         ON extraction.job_id = candidate.job_id
                       WHERE extraction.subject_entity_id = content.entity_id
                         AND extraction.job_type = 'extract-investigation-terms'
                         AND candidate.stage = 'accepted-investigation-terms'
                         AND candidate.validation_state = 'accepted'
                       ORDER BY candidate.created_at DESC, candidate.artifact_id DESC
                       LIMIT 1
                   )
                   WHERE content.language = ?
                     AND content.kind IN ('answer', 'question')
                     AND source.status = 'accepted' AND target.status = 'accepted'
                     AND term.language = ? AND term.kind = 'word'
                   ORDER BY source.created_at, content.entity_id,
                            artifact.artifact_id, term.normalized""",
                (language, language),
            ).fetchall()

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["artifact_payload"])
            accepted_ids = {
                str(item.get("term_id", ""))
                for item in payload.get("terms", [])
                if isinstance(item, dict)
            }
            term_id = str(row["term_id"])
            normalized = _normalise(str(row["normalized"]))
            if term_id not in accepted_ids or normalized in excluded_keys:
                continue
            key = (str(row["source_entity_id"]), str(row["source_artifact_id"]))
            group = groups.setdefault(
                key,
                {
                    "source_entity_id": key[0],
                    "source_artifact_id": key[1],
                    "source_key": str(row["source_key"]),
                    "kind": str(row["kind"]),
                    "source_created_at": str(row["source_created_at"]),
                    "terms": [],
                },
            )
            properties = json.loads(row["properties"])
            group["terms"].append(
                {
                    "term_id": term_id,
                    "term": str(row["term"]),
                    "normalized": normalized,
                    "confidence": float(row["confidence"]),
                    "ordinal": int(properties.get("ordinal", 999)),
                }
            )
        output = list(groups.values())
        for group in output:
            group["terms"].sort(
                key=lambda item: (item["ordinal"], item["normalized"])
            )
        return sorted(
            output,
            key=lambda item: (
                item["source_created_at"],
                item["source_entity_id"],
                item["source_artifact_id"],
            ),
        )

    def discovered_or_planned_term_keys(self, language: str = "en") -> set[str]:
        """Return durable discovery claims plus every term with a real plan."""

        language = _language(language)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT normalized FROM lexical_discoveries WHERE language = ?
                   UNION
                   SELECT term.normalized FROM terms AS term
                   JOIN preparation_jobs AS job
                     ON job.subject_entity_id = term.entity_id
                    AND job.subject_key = 'term:' || term.entity_id
                   WHERE term.language = ? AND term.kind = 'word'""",
                (language, language),
            ).fetchall()
        return {str(row["normalized"]) for row in rows}

    def claim_lexical_discovery_round(
        self, selections: Iterable[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Atomically reserve one complete, globally unique lexical batch."""

        prepared: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in selections:
            language = _language(str(raw.get("language", "en")))
            term = unicodedata.normalize("NFKC", str(raw.get("term", ""))).strip()
            normalized = _normalise(term)
            source_kind = str(raw.get("source_kind", "")).strip()
            if not normalized:
                raise ValueError("a lexical discovery needs a non-empty term")
            if source_kind not in {"qa-investigation", "word-origins"}:
                raise ValueError(f"invalid lexical discovery source: {source_kind!r}")
            key = (language, normalized)
            if key in seen:
                raise ValueError(f"duplicate lexical discovery: {term!r}")
            seen.add(key)
            prepared.append(
                {
                    "language": language,
                    "term": term,
                    "normalized": normalized,
                    "source_kind": source_kind,
                    "source_entity_id": str(raw.get("source_entity_id", "")).strip(),
                    "source_artifact_id": str(raw.get("source_artifact_id", "")).strip(),
                    "source_entry_id": str(raw.get("source_entry_id", "")).strip(),
                }
            )
        if not 1 <= len(prepared) <= 5:
            raise ValueError("a lexical discovery round needs one to five terms")

        timestamp = _now()
        round_id = f"lexical-round-{uuid.uuid4()}"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in prepared:
                collision = connection.execute(
                    """SELECT 1 FROM lexical_discoveries
                       WHERE language = ? AND normalized = ?
                       UNION ALL
                       SELECT 1 FROM terms AS term
                       JOIN preparation_jobs AS job
                         ON job.subject_entity_id = term.entity_id
                        AND job.subject_key = 'term:' || term.entity_id
                       WHERE term.language = ? AND term.normalized = ?
                         AND term.kind = 'word'
                       LIMIT 1""",
                    (
                        item["language"],
                        item["normalized"],
                        item["language"],
                        item["normalized"],
                    ),
                ).fetchone()
                if collision is not None:
                    connection.rollback()
                    return None
            connection.execute(
                """INSERT INTO lexical_discovery_rounds(
                       round_id, status, created_at, updated_at
                   ) VALUES (?, 'claimed', ?, ?)""",
                (round_id, timestamp, timestamp),
            )
            discoveries: list[dict[str, Any]] = []
            for ordinal, item in enumerate(prepared):
                discovery_id = _identifier(
                    "lexical-discovery",
                    f"{item['language']}:{item['normalized']}",
                )
                connection.execute(
                    """INSERT INTO lexical_discoveries(
                           discovery_id, round_id, language, term, normalized,
                           source_kind, source_entity_id, source_artifact_id,
                           source_entry_id, ordinal, planned_at, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)""",
                    (
                        discovery_id,
                        round_id,
                        item["language"],
                        item["term"],
                        item["normalized"],
                        item["source_kind"],
                        item["source_entity_id"],
                        item["source_artifact_id"],
                        item["source_entry_id"],
                        ordinal,
                        timestamp,
                    ),
                )
                discoveries.append(
                    {"discovery_id": discovery_id, "round_id": round_id, **item}
                )
            connection.commit()
        return {"round_id": round_id, "discoveries": discoveries}

    def unplanned_lexical_discoveries(self) -> list[dict[str, Any]]:
        """Return the oldest claimed round until every claim has a plan."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT round_id FROM lexical_discoveries
                   WHERE planned_at = '' ORDER BY created_at, round_id LIMIT 1"""
            ).fetchone()
            if row is None:
                return []
            rows = connection.execute(
                """SELECT discovery_id, round_id, language, term, normalized,
                          source_kind, source_entity_id, source_artifact_id,
                          source_entry_id, ordinal, created_at
                   FROM lexical_discoveries
                   WHERE round_id = ? AND planned_at = ''
                   ORDER BY ordinal""",
                (row["round_id"],),
            ).fetchall()
        return [dict(item) for item in rows]

    def mark_lexical_discovery_planned(self, discovery_id: str) -> None:
        """Checkpoint one claimed term after its idempotent DAG exists."""

        timestamp = _now()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT round_id FROM lexical_discoveries WHERE discovery_id = ?",
                (discovery_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown lexical discovery: {discovery_id!r}")
            round_id = str(row["round_id"])
            connection.execute(
                """UPDATE lexical_discoveries SET planned_at = ?
                   WHERE discovery_id = ?""",
                (timestamp, discovery_id),
            )
            remaining = connection.execute(
                """SELECT 1 FROM lexical_discoveries
                   WHERE round_id = ? AND planned_at = '' LIMIT 1""",
                (round_id,),
            ).fetchone()
            if remaining is None:
                connection.execute(
                    """UPDATE lexical_discovery_rounds
                       SET status = 'planned', updated_at = ? WHERE round_id = ?""",
                    (timestamp, round_id),
                )
            connection.commit()

    def artifacts_for_subject(
        self,
        subject_key: str,
        *,
        stage: str = "",
        validation_state: str = "",
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [subject_key]
        filters = ""
        if stage:
            filters += " AND artifact.stage = ?"
            parameters.append(stage)
        if validation_state:
            filters += " AND artifact.validation_state = ?"
            parameters.append(validation_state)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT artifact.artifact_id, artifact.job_id, job.job_type,
                          artifact.stage, artifact.language, artifact.payload,
                          artifact.reusable, artifact.validation_state,
                          artifact.quality_score, artifact.created_at
                   FROM job_artifacts AS artifact
                   JOIN preparation_jobs AS job ON job.job_id = artifact.job_id
                   WHERE job.subject_key = ?""" + filters + """
                   ORDER BY artifact.created_at""",
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload"]),
                "reusable": bool(row["reusable"]),
            }
            for row in rows
        ]

    def accepted_pronunciation_artifacts(
        self, language: str
    ) -> list[dict[str, Any]]:
        """Return accepted pronunciation checkpoints for a bounded audit."""

        language = _language(language)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT artifact.artifact_id, artifact.job_id, artifact.stage,
                          artifact.language, artifact.payload, artifact.created_at,
                          artifact.validation_state, artifact.quality_score,
                          job.subject_entity_id, job.subject_key
                   FROM job_artifacts AS artifact
                   JOIN preparation_jobs AS job ON job.job_id = artifact.job_id
                   WHERE artifact.stage = 'accepted-pronunciation'
                     AND artifact.validation_state = 'accepted'
                     AND artifact.language = ?
                   ORDER BY artifact.created_at""",
                (language,),
            ).fetchall()
        return [
            {
                "artifact_id": row[0],
                "job_id": row[1],
                "stage": row[2],
                "language": row[3],
                "payload": json.loads(row[4]),
                "created_at": row[5],
                "validation_state": row[6],
                "quality_score": row[7],
                "subject_entity_id": row[8],
                "subject_key": row[9],
            }
            for row in rows
        ]

    def supersede_pronunciation_artifacts(
        self, subject_key: str, language: str, keep_artifact_id: str
    ) -> int:
        """Quarantine older readings after a dictionary-verified replacement."""

        language = _language(language)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE job_artifacts SET validation_state = 'superseded'
                   WHERE stage = 'accepted-pronunciation'
                     AND validation_state = 'accepted' AND language = ?
                     AND artifact_id <> ? AND job_id IN (
                         SELECT job_id FROM preparation_jobs WHERE subject_key = ?
                     )""",
                (language, keep_artifact_id, subject_key.strip()),
            )
            connection.commit()
        return int(cursor.rowcount)

    def add_job_dependency(self, job_id: str, depends_on_job_id: str) -> None:
        if job_id == depends_on_job_id:
            raise ValueError("a job cannot depend on itself")
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO job_dependencies(job_id, depends_on_job_id)
                   VALUES (?, ?)""",
                (job_id, depends_on_job_id),
            )
            connection.commit()

    def jobs_for_subject(self, subject_key: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT job_id, job_type, language, status, priority, attempts,
                          max_attempts, model, prompt_version, source_fingerprint,
                          error, created_at, updated_at
                   FROM preparation_jobs WHERE subject_key = ?
                   ORDER BY priority, created_at""",
                (subject_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def planned_term_keys(self, language: str = "en") -> set[str]:
        """Return normalized words already accepted, queued, or attempted."""

        language = _language(language)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT term.normalized FROM terms AS term
                   JOIN preparation_jobs AS job
                     ON job.subject_entity_id = term.entity_id
                    AND job.subject_key = 'term:' || term.entity_id
                   WHERE term.language = ? AND term.kind = 'word'""",
                (language,),
            ).fetchall()
        return {str(row["normalized"]) for row in rows}

    def terminal_failed_term_keys(
        self,
        language: str = "en",
        *,
        exclude_prompt_version: str = "",
        source_fingerprint: str = "",
    ) -> set[str]:
        """Return failed lexical subjects with no active work or repair epoch."""

        language = _language(language)
        exclusion = ""
        parameters: list[Any] = [language]
        if exclude_prompt_version:
            exclusion = """
                AND NOT EXISTS (
                    SELECT 1 FROM preparation_jobs AS repair
                    WHERE repair.subject_entity_id = term.entity_id
                      AND repair.prompt_version = ?
                      AND repair.source_fingerprint = ?
                )"""
            parameters.extend((exclude_prompt_version, source_fingerprint))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT term.normalized FROM terms AS term
                   WHERE term.language = ? AND term.kind = 'word'
                     AND EXISTS (
                         SELECT 1 FROM preparation_jobs AS failed
                         WHERE failed.subject_entity_id = term.entity_id
                           AND failed.status = 'failed'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM preparation_jobs AS active
                         WHERE active.subject_entity_id = term.entity_id
                           AND active.status IN ('queued', 'running')
                     )
                """ + exclusion,
                tuple(parameters),
            ).fetchall()
        return {str(row["normalized"]) for row in rows}

    def active_term_preparation_count(self) -> int:
        """Return the number of lexical subjects still using the atomic worker."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(DISTINCT subject_key) FROM preparation_jobs
                   WHERE subject_key LIKE 'term:%'
                     AND status IN ('queued', 'running')"""
            ).fetchone()
        return int(row[0]) if row else 0

    def save_job_artifact(
        self,
        job_id: str,
        stage: str,
        payload: Any,
        *,
        language: str = "",
        reusable: bool = True,
        validation_state: str = "candidate",
        quality_score: float | None = None,
    ) -> str:
        language = _language(language) if language else ""
        validation_state = validation_state.strip().lower()
        if validation_state not in {
            "candidate",
            "accepted",
            "rejected",
            "superseded",
            "legacy",
        }:
            raise ValueError(f"invalid artifact validation state: {validation_state!r}")
        if quality_score is not None:
            quality_score = max(0.0, min(float(quality_score), 1.0))
        artifact_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            if validation_state in {"accepted", "candidate"}:
                connection.execute(
                    """UPDATE job_artifacts SET validation_state = 'superseded'
                       WHERE validation_state = ? AND stage = ? AND language = ?
                         AND job_id IN (
                             SELECT earlier.job_id FROM preparation_jobs AS earlier
                             JOIN preparation_jobs AS current
                               ON current.subject_key = earlier.subject_key
                             WHERE current.job_id = ?
                         )""",
                    (validation_state, stage.strip(), language, job_id),
                )
            connection.execute(
                """INSERT INTO job_artifacts(
                       artifact_id, job_id, stage, language, payload, reusable,
                       validation_state, quality_score, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    job_id,
                    stage.strip(),
                    language,
                    json.dumps(payload, ensure_ascii=False),
                    int(reusable),
                    validation_state,
                    quality_score,
                    _now(),
                ),
            )
            connection.commit()
        return artifact_id

    def finish_job(self, job_id: str, *, error: str = "") -> None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT attempts, max_attempts FROM preparation_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            status = "complete" if not error else (
                "failed" if row["attempts"] >= row["max_attempts"] else "queued"
            )
            connection.execute(
                """UPDATE preparation_jobs
                   SET status = ?, error = ?, locked_at = '', updated_at = ?
                   WHERE job_id = ?""",
                (status, error[:2000], _now(), job_id),
            )
            if status == "failed":
                self._fail_blocked_jobs(connection)
            connection.commit()

    @staticmethod
    def _fail_blocked_jobs(connection: sqlite3.Connection) -> int:
        """Mark queued descendants of terminal failures as terminal too.

        A declared dependency is required. Leaving its descendants queued makes
        progress counters lie and creates jobs that can never be claimed. Work
        through the DAG a layer at a time so transitive descendants receive the
        same truthful terminal state without consuming model attempts.
        """

        failed = 0
        while True:
            rows = connection.execute(
                """SELECT job.job_id, prerequisite.job_id AS prerequisite_id,
                          prerequisite.job_type AS prerequisite_type,
                          prerequisite.error AS prerequisite_error
                   FROM preparation_jobs AS job
                   JOIN job_dependencies AS dependency
                     ON dependency.job_id = job.job_id
                   JOIN preparation_jobs AS prerequisite
                     ON prerequisite.job_id = dependency.depends_on_job_id
                   WHERE job.status = 'queued' AND prerequisite.status = 'failed'
                   ORDER BY job.created_at, prerequisite.updated_at,
                            prerequisite.job_id"""
            ).fetchall()
            blocked: dict[str, sqlite3.Row] = {}
            for row in rows:
                blocked.setdefault(str(row["job_id"]), row)
            if not blocked:
                return failed
            timestamp = _now()
            for job_id, row in blocked.items():
                detail = str(row["prerequisite_error"] or "terminal failure")
                message = (
                    "blocked by failed prerequisite "
                    f"{row['prerequisite_type']} {row['prerequisite_id']}: {detail}"
                )[:2000]
                cursor = connection.execute(
                    """UPDATE preparation_jobs
                       SET status = 'failed', error = ?, locked_at = '', updated_at = ?
                       WHERE job_id = ? AND status = 'queued'""",
                    (message, timestamp, job_id),
                )
                failed += int(cursor.rowcount)

    def recover_running_jobs(self) -> int:
        """Requeue leases left behind by an interrupted single worker.

        The interrupted attempt is not charged: its model response was never
        validated or committed as a completed job. Accepted artifacts remain
        idempotent and will be superseded if the retried stage succeeds.
        """

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE preparation_jobs
                   SET status = 'queued',
                       attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                       error = 'worker interrupted; safely requeued',
                       locked_at = '', updated_at = ?
                   WHERE status = 'running'""",
                (_now(),),
            )
            connection.commit()
            return int(cursor.rowcount)

    def graph_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with closing(self._connect()) as connection:
            nodes = [
                {
                    "id": row["entity_id"],
                    "type": row["entity_type"],
                    "key": row["canonical_key"],
                    "label": row["label"],
                    "quality": row["quality_score"],
                }
                for row in connection.execute(
                    """SELECT entity_id, entity_type, canonical_key, label, quality_score
                       FROM entities WHERE status = 'accepted' ORDER BY entity_id"""
                )
            ]
            edges = [
                {
                    "id": row["edge_id"],
                    "source": row["source_entity_id"],
                    "target": row["target_entity_id"],
                    "relation": row["relation"],
                    "basis": row["basis"],
                    "confidence": row["confidence"],
                    "properties": json.loads(row["properties"]),
                }
                for row in connection.execute(
                    """SELECT edge_id, source_entity_id, target_entity_id, relation,
                              basis, confidence, properties
                       FROM entity_edges WHERE status = 'accepted' ORDER BY edge_id"""
                )
            ]
        return {"nodes": nodes, "edges": edges}

    def status(self) -> dict[str, Any]:
        names = (
            "entities",
            "terms",
            "meanings",
            "morphemes",
            "pronunciations",
            "phoneme_segments",
            "historical_forms",
            "history_events",
            "translations",
            "grammar_analyses",
            "grammar_parts",
            "content_items",
            "entity_edges",
            "preparation_jobs",
            "job_dependencies",
            "inquiry_events",
        )
        with closing(self._connect()) as connection:
            counts = {
                name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in names
            }
            queued = int(
                connection.execute(
                    "SELECT COUNT(*) FROM preparation_jobs WHERE status = 'queued'"
                ).fetchone()[0]
            )
        return {
            "ready": True,
            "database": str(self.database),
            "schema_version": "2",
            "counts": counts,
            "queued_jobs": queued,
        }
