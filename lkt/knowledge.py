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
                ("schema_version", "2"),
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
            entity_id = self._upsert_entity(
                connection,
                "term",
                canonical_key,
                text,
                payload,
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
            connection.commit()
        self.add_edge(subject_entity_id, entity_id, "has-grammar", basis="reviewed")
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
                   ORDER BY priority, created_at LIMIT 1""",
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
            connection.commit()

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
