from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import closing, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .morphology_contract import (
    CURRENT_MORPHOLOGY_PARSER_VERSION,
    canonical_lexical_node_identity,
    canonical_morpheme_occurrence_identity,
    canonical_morpheme_identity,
)
from .partial_projection import (
    LIVE_SOURCE_COMPONENT_SQL,
    component_projection_occurrence,
    merge_partial_component_projection,
)
from .relation_support import (
    RelationSupportSpec,
    ensure_relation_support_schema,
    reduce_relation_assertions,
    replace_relation_support_scope,
    retire_relation_support,
    upsert_relation_support,
)


_LANGUAGE = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_CURRENT_KNOWLEDGE_SCHEMA_VERSION = 13
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

_PROJECTION_REQUIRED_ARTIFACT_STAGES = (
    "accepted-meaning",
    "accepted-morpheme-split",
    "accepted-origin-branches",
)
# Projection output used to be implicitly versioned by graph data alone. Treat
# that historical format as v1 and bump this value whenever projection-only
# composition semantics change. The version participates in both the desired
# fingerprint and a durable bounded bootstrap cursor, so an upgrade repairs
# every existing projection once without manufacturing semantic graph writes.
CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION = 8
_PROJECTION_REPAIR_PROMPT_MARKER = "-projection-local-p"

# Direct, live history of the subject word, never a language-label edge or
# another word's component history. Shared by bootstrap and initial repair.
_LIVE_SOURCE_WORD_HISTORY_SQL = """(
    assertion.status = 'accepted'
    AND EXISTS (
        SELECT 1 FROM relation_assertion_supports AS history_support
        WHERE history_support.assertion_id = assertion.assertion_id
          AND history_support.status = 'accepted'
    )
    AND (
        (assertion.relation = 'has-history'
         AND assertion.source_entity_id = assertion.subject_entity_id
         AND EXISTS (
             SELECT 1 FROM entities AS history_entity
             WHERE history_entity.entity_id = assertion.target_entity_id
               AND history_entity.entity_type = 'historical-form'
               AND history_entity.status = 'accepted'
         ))
        OR
        (assertion.relation = 'developed-into'
         AND assertion.target_entity_id = assertion.subject_entity_id
         AND EXISTS (
             SELECT 1 FROM entities AS history_entity
             WHERE history_entity.entity_id = assertion.source_entity_id
               AND history_entity.entity_type = 'historical-form'
               AND history_entity.status = 'accepted'
         ))
    )
)"""



def _projection_edge_identity(
    source_entity_id: object, relation: object, target_entity_id: object
) -> tuple[str, str, str]:
    """Return the exact accepted edge identity used for projection bundles."""
    return (
        str(source_entity_id).strip(),
        str(relation).strip(),
        str(target_entity_id).strip(),
    )
_PROJECTION_REPAIR_BURST_LIMIT = 8
_PROJECTION_EXCLUDED_RELATIONS = {
    "has-grammar",
    "has-language",
    "has-meaning",
    "has-period",
    "has-pronunciation",
    "has-translation",
    "in-language",
    "language",
    "language-of",
    "period",
    "renders-as",
}
_SEMANTIC_RELATIONS = {"has-component", "shares-component"}
_SEMANTIC_JOB_TYPES = {
    "term": "prepare-meaning",
    "morpheme": "prepare-entity-meaning",
}
_SEMANTIC_MODEL_CONTEXT_RECOVERY_ERROR = (
    "semantic meaning has no exact local evidence"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _projection_bootstrap_scheduler_key() -> str:
    return (
        "lexical-projection-contract-v"
        f"{CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION}"
    )


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _language(value: str) -> str:
    language = value.strip().lower()
    if not _LANGUAGE.fullmatch(language):
        raise ValueError(f"invalid language code: {value!r}")
    return language


def _identifier(prefix: str, key: str) -> str:
    return f"{prefix}-{uuid.uuid5(uuid.NAMESPACE_URL, 'lkt:' + key)}"


def _json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class _BorrowedWriteState:
    """One outer transaction shared by nested KnowledgeStore operations."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.aborted = False


class _BorrowedConnection:
    """Keep nested store helpers inside their caller-owned transaction."""

    def __init__(self, state: _BorrowedWriteState):
        self._state = state

    def execute(self, sql: str, *parameters: Any) -> sqlite3.Cursor:
        if self._state.aborted:
            raise RuntimeError("borrowed semantic transaction was rolled back")
        if sql.lstrip().upper().startswith("BEGIN"):
            return self._state.connection.execute("SELECT 1 WHERE 0")
        return self._state.connection.execute(sql, *parameters)

    def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
        if self._state.aborted:
            raise RuntimeError("borrowed semantic transaction was rolled back")
        return self._state.connection.executemany(sql, parameters)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        if self._state.aborted:
            raise RuntimeError("borrowed semantic transaction was rolled back")
        return self._state.connection.executescript(sql_script)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self._state.aborted = True
        self._state.connection.rollback()

    def close(self) -> None:
        return None

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None:
            self.rollback()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._state.connection, name)


class KnowledgeStore:
    """Authoritative, atomic knowledge used to reconstruct cards and graphs."""

    def __init__(self, database: Path, *, serving: bool = False):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._write_context = threading.local()
        if serving and self._has_current_serving_schema():
            return
        self._initialise()
        self._hydrate_accepted_morpheme_meanings()
        self._refresh_lexical_entity_payloads()

    def _has_current_serving_schema(self) -> bool:
        """Attach the HTTP adapter without competing with active graph writes.

        Worker/default startup owns migrations and derived-payload hydration.
        A current-schema web process needs neither to serve existing cards.
        Fresh and older databases still take the normal initialization path.
        """
        if not self.database.is_file():
            return False
        uri = self.database.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            try:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                projection_revision = connection.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key='projection_content_revision'"
                ).fetchone()
                required_indexes = {
                    str(index[0])
                    for index in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND name IN ("
                        "'idx_entities_updated_at', 'idx_jobs_type_next', "
                        "'idx_jobs_subject_snapshot', "
                        "'idx_morph_enrichment_v3_last_job')"
                    )
                }
                has_morphology_enrichment = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='morphology_enrichment_clusters_v3'"
                ).fetchone() is not None
            except sqlite3.OperationalError as error:
                if "no such table: schema_meta" in str(error):
                    return False
                raise
        expected_indexes = {
            "idx_entities_updated_at",
            "idx_jobs_type_next",
            "idx_jobs_subject_snapshot",
        }
        if has_morphology_enrichment:
            expected_indexes.add("idx_morph_enrichment_v3_last_job")
        return bool(
            row
            and str(row[0]) == str(_CURRENT_KNOWLEDGE_SCHEMA_VERSION)
            and projection_revision is not None
            and expected_indexes.issubset(required_indexes)
        )

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _connect(self) -> sqlite3.Connection | _BorrowedConnection:
        state = getattr(self._write_context, "semantic", None)
        if state is not None:
            return _BorrowedConnection(state)
        return self._open_connection()

    def semantic_subject_writable(self, entity_id: str) -> bool:
        """Return an inference-time status snapshot, not a write guarantee."""

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status FROM entities WHERE entity_id=?", (entity_id,)
                ).fetchone()
                writable = bool(
                    row and str(row["status"]) in {"draft", "accepted"}
                )
                connection.commit()
                return writable
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _require_unarchived_endpoints(
        connection: sqlite3.Connection, *entity_ids: str
    ) -> None:
        """Check under the caller's write lock before creating graph structure."""
        for entity_id in set(entity_ids):
            row = connection.execute(
                "SELECT status FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is not None and row[0] == "archived":
                raise ValueError(f"archived graph endpoint: {entity_id}")

    def repair_archived_morphology_links(self) -> dict[str, int]:
        """Retire owned claims on archived endpoints, retaining support history."""
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """SELECT a.assertion_id,a.subject_entity_id,a.source_entity_id,
                              a.target_entity_id,a.relation
                       FROM relation_assertions a
                       WHERE a.status='accepted'
                         AND a.relation IN ('has-component','shares-component')
                         AND EXISTS(SELECT 1 FROM entities e
                                    WHERE e.status='archived' AND e.entity_id IN
                                      (a.source_entity_id,a.target_entity_id))"""
                ).fetchall()
                timestamp = _now()
                ids: list[str] = []
                triples: set[tuple[str, str, str]] = set()
                affected: set[str] = set()
                skipped = 0
                for row in rows:
                    owners = connection.execute(
                        """SELECT owner_type,owner_id FROM relation_assertion_supports
                           WHERE assertion_id=?""", (row["assertion_id"],)
                    ).fetchall()
                    if not owners:
                        skipped += 1
                        continue
                    for owner in owners:
                        retire_relation_support(
                            connection, assertion_id=row["assertion_id"],
                            owner_type=owner["owner_type"], owner_id=owner["owner_id"],
                            timestamp=timestamp,
                        )
                    ids.append(row["assertion_id"])
                    triples.add((row["source_entity_id"], row["target_entity_id"], row["relation"]))
                    affected.update((row["subject_entity_id"], row["source_entity_id"], row["target_entity_id"]))
                result = reduce_relation_assertions(connection, ids, timestamp=timestamp)
                if ids:
                    self._sync_relation_authority(connection, triples, timestamp=timestamp)
                    self._mark_projection_repairs_for_entities(connection, affected, timestamp=timestamp)
                    self._advance_relation_graph_revision(connection)
                connection.commit()
                return {**result, "unowned_deferred": skipped}
            except Exception:
                connection.rollback()
                raise

    def atomic_semantic_write(
        self, entity_id: str, persist: Callable[[], Any]
    ) -> Any:
        """Persist one semantic result iff its subject remains writable.

        Existing store primitives borrow this transaction's connection. Defer
        dependent projection and semantic invalidation until the complete graph
        mutation is available, then refresh each affected subject once before
        committing graph, slot, revision, artifact, and scheduling state together.
        """

        entity_id = str(entity_id).strip()
        if not entity_id:
            raise ValueError("semantic subject is empty")
        if getattr(self._write_context, "semantic", None) is not None:
            raise RuntimeError("nested semantic write transaction is not supported")
        connection = self._open_connection()
        state = _BorrowedWriteState(connection)
        state.deferred_projection_entities = set()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is None or str(row["status"]) not in {"draft", "accepted"}:
                raise ValueError(
                    f"semantic subject is not writable: {entity_id}"
                )
            self._write_context.semantic = state
            result = persist()
            if state.aborted:
                raise RuntimeError("semantic write transaction was rolled back")
            row = connection.execute(
                "SELECT status FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is None or str(row["status"]) not in {"draft", "accepted"}:
                raise ValueError(
                    f"semantic subject is not writable: {entity_id}"
                )
            pending = state.deferred_projection_entities
            state.deferred_projection_entities = None
            if pending:
                self._mark_projection_repairs_for_entities(connection, pending)
            if state.aborted:
                raise RuntimeError("semantic write transaction was rolled back")
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            if getattr(self._write_context, "semantic", None) is state:
                del self._write_context.semantic
            connection.close()

    def _refresh_lexical_entity_payloads(self) -> None:
        """Mirror normalized display metadata into generic graph-node payloads."""

        projections = (
            ("SELECT entity_id, language, kind FROM terms", ("language", "kind")),
            (
                "SELECT entity_id, language, kind, meaning FROM morphemes",
                ("language", "kind", "meaning"),
            ),
            (
                """SELECT entity_id, language, definition, part_of_speech,
                          register_label, domain_label, sense_order FROM meanings""",
                (
                    "language", "definition", "part_of_speech",
                    "register_label", "domain_label", "sense_order",
                ),
            ),
            (
                """SELECT entity_id, language, system, reading, dialect
                   FROM pronunciations""",
                ("language", "system", "reading", "dialect"),
            ),
            (
                """SELECT entity_id, target_language, transliteration, usage_note
                   FROM translations""",
                ("target_language", "transliteration", "usage_note"),
            ),
            (
                """SELECT entity_id, language, period_label, meaning,
                          date_min, date_max FROM historical_forms""",
                ("language", "period_label", "meaning", "date_min", "date_max"),
            ),
            (
                """SELECT entity_id, language, event_type, period_label,
                          description, date_min, date_max FROM history_events""",
                (
                    "language", "event_type", "period_label",
                    "description", "date_min", "date_max",
                ),
            ),
        )
        changed = 0
        with closing(self._connect()) as connection:
            # Acquire the writer slot before opening any streaming SELECT.
            # Upgrading an active read snapshot after another process writes
            # can fail immediately with SQLITE_BUSY despite busy_timeout.
            connection.execute("BEGIN IMMEDIATE")
            for query, fields in projections:
                for row in connection.execute(query):
                    entity = connection.execute(
                        "SELECT payload FROM entities WHERE entity_id = ?",
                        (row["entity_id"],),
                    ).fetchone()
                    if entity is None:
                        continue
                    payload = json.loads(entity["payload"])
                    enriched = {**payload, **{field: row[field] for field in fields}}
                    if enriched == payload:
                        continue
                    connection.execute(
                        "UPDATE entities SET payload = ?, updated_at = ? WHERE entity_id = ?",
                        (json.dumps(enriched, ensure_ascii=False), _now(), row["entity_id"]),
                    )
                    changed += 1
            # These values are a derived display mirror of the canonical subtype
            # rows above. Repairing that cache does not change semantic,
            # topology, or evidence state and must not manufacture a graph
            # revision during an otherwise idempotent startup/migration.
            connection.commit()

    @staticmethod
    def _migrate_meanings_schema_v6(connection: sqlite3.Connection) -> None:
        """Generalize meaning ownership while preserving legacy term references."""

        columns = {
            str(row["name"]): row
            for row in connection.execute("PRAGMA table_info(meanings)")
        }
        foreign_keys = {
            (str(row["from"]), str(row["table"]))
            for row in connection.execute("PRAGMA foreign_key_list(meanings)")
        }
        schema_is_current = (
            "subject_entity_id" in columns
            and bool(columns["subject_entity_id"]["notnull"])
            and "term_id" in columns
            and not bool(columns["term_id"]["notnull"])
            and ("subject_entity_id", "entities") in foreign_keys
            and ("term_id", "terms") in foreign_keys
        )
        if schema_is_current:
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_meanings_subject
                   ON meanings(subject_entity_id, language, sense_order)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_meanings_term
                   ON meanings(term_id, language, sense_order)"""
            )
            return

        source_count = int(
            connection.execute("SELECT COUNT(*) FROM meanings").fetchone()[0]
        )
        subject_expression = (
            "COALESCE(subject_entity_id, term_id)"
            if "subject_entity_id" in columns
            else "term_id"
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS meanings_schema_v6")
            connection.execute(
                """CREATE TABLE meanings_schema_v6 (
                       entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id)
                           ON DELETE CASCADE,
                       subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                       term_id TEXT REFERENCES terms(entity_id),
                       language TEXT NOT NULL,
                       definition TEXT NOT NULL,
                       part_of_speech TEXT NOT NULL DEFAULT '',
                       register_label TEXT NOT NULL DEFAULT '',
                       domain_label TEXT NOT NULL DEFAULT '',
                       sense_order INTEGER NOT NULL DEFAULT 0,
                       CHECK(term_id IS NULL OR term_id = subject_entity_id)
                   )"""
            )
            connection.execute(
                f"""INSERT INTO meanings_schema_v6(
                           entity_id, subject_entity_id, term_id, language,
                           definition, part_of_speech, register_label,
                           domain_label, sense_order
                       )
                       SELECT entity_id, {subject_expression}, term_id, language,
                              definition, part_of_speech, register_label,
                              domain_label, sense_order
                       FROM meanings"""
            )
            migrated_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM meanings_schema_v6"
                ).fetchone()[0]
            )
            if migrated_count != source_count:
                raise RuntimeError(
                    "meaning ownership migration did not preserve every row"
                )
            connection.execute("DROP TABLE meanings")
            connection.execute("ALTER TABLE meanings_schema_v6 RENAME TO meanings")
            connection.execute(
                """CREATE INDEX idx_meanings_subject
                   ON meanings(subject_entity_id, language, sense_order)"""
            )
            connection.execute(
                """CREATE INDEX idx_meanings_term
                   ON meanings(term_id, language, sense_order)"""
            )
            connection.execute(
                """INSERT INTO schema_meta(key, value) VALUES ('schema_version', '6')
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
            )
            violations = [
                *connection.execute("PRAGMA foreign_key_check(meanings)"),
                *connection.execute("PRAGMA foreign_key_check(translations)"),
            ]
            if violations:
                raise RuntimeError(
                    "meaning ownership migration left "
                    f"{len(violations)} foreign-key violations"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def _reconcile_legacy_translation_meanings_v6(self) -> None:
        """Restore only unambiguous accepted legacy target-meaning chains."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT t.entity_id, t.source_term_id, t.source_meaning_id,
                          t.target_language, t.target_term_id, t.text,
                          t.normalized, translation_entity.payload,
                          translation_entity.quality_score,
                          has_translation.basis,
                          MIN(has_translation.confidence, renders_as.confidence)
                              AS edge_confidence
                   FROM translations AS t
                   JOIN entities AS translation_entity
                     ON translation_entity.entity_id = t.entity_id
                    AND translation_entity.status = 'accepted'
                   JOIN meanings AS source_meaning
                     ON source_meaning.entity_id = t.source_meaning_id
                    AND source_meaning.subject_entity_id = t.source_term_id
                   JOIN entities AS source_meaning_entity
                     ON source_meaning_entity.entity_id = source_meaning.entity_id
                    AND source_meaning_entity.status = 'accepted'
                   JOIN terms AS target_term
                     ON target_term.entity_id = t.target_term_id
                    AND target_term.language = t.target_language
                    AND target_term.normalized = t.normalized
                   JOIN entities AS target_term_entity
                     ON target_term_entity.entity_id = target_term.entity_id
                    AND target_term_entity.status = 'accepted'
                   JOIN entity_edges AS has_translation
                     ON has_translation.source_entity_id = t.source_term_id
                    AND has_translation.target_entity_id = t.entity_id
                    AND has_translation.relation = 'has-translation'
                    AND has_translation.status = 'accepted'
                   JOIN entity_edges AS renders_as
                     ON renders_as.source_entity_id = t.entity_id
                    AND renders_as.target_entity_id = t.target_term_id
                    AND renders_as.relation = 'renders-as'
                    AND renders_as.status = 'accepted'
                    AND renders_as.basis = has_translation.basis
                   WHERE t.source_meaning_id IS NOT NULL
                     AND t.target_term_id IS NOT NULL
                     AND NOT EXISTS (
                           SELECT 1
                           FROM meanings AS target_meaning
                           JOIN entity_edges AS translated_from
                             ON translated_from.source_entity_id =
                                    target_meaning.entity_id
                            AND translated_from.target_entity_id =
                                    t.source_meaning_id
                            AND translated_from.relation = 'translated-from'
                            AND translated_from.status = 'accepted'
                           WHERE target_meaning.subject_entity_id = t.target_term_id
                             AND target_meaning.language = t.target_language
                       )
                   ORDER BY t.entity_id"""
            ).fetchall()

        for row in rows:
            try:
                entity_payload = json.loads(row["payload"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(entity_payload, dict):
                continue
            existing_provenance = entity_payload.get("provenance")
            if isinstance(existing_provenance, dict) and existing_provenance.get(
                "basis"
            ):
                continue

            with closing(self._connect()) as connection:
                revisions = connection.execute(
                    """SELECT revision_id, model, prompt_version, reason, payload
                       FROM entity_revisions
                       WHERE entity_id = ? AND accepted = 1
                       ORDER BY created_at DESC, revision_id DESC""",
                    (row["entity_id"],),
                ).fetchall()
                linked_evidence = {
                    str(item["evidence_id"])
                    for item in connection.execute(
                        """SELECT evidence_id FROM entity_evidence
                           WHERE entity_id = ?""",
                        (row["entity_id"],),
                    )
                }

            accepted_definitions: dict[str, dict[str, Any]] = {}
            for revision in revisions:
                try:
                    payload = json.loads(revision["payload"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                definition = str(payload.get("meaning", "")).strip()
                if (
                    not definition
                    or str(payload.get("translation_id", "")) != row["entity_id"]
                    or str(payload.get("target_term_id", ""))
                    != row["target_term_id"]
                    or str(payload.get("language", "")) != row["target_language"]
                    or _normalise(str(payload.get("term", "")))
                    != str(row["normalized"])
                ):
                    continue
                evidence_ids = payload.get("evidence_ids")
                selected_evidence = [
                    str(evidence_id)
                    for evidence_id in evidence_ids
                    if str(evidence_id) in linked_evidence
                ] if isinstance(evidence_ids, list) else []
                try:
                    revision_confidence = float(payload.get("confidence", 0.0))
                except (TypeError, ValueError):
                    continue
                accepted_definitions.setdefault(
                    _normalise(definition),
                    {
                        "definition": definition,
                        "evidence_ids": list(dict.fromkeys(selected_evidence)),
                        "confidence": max(
                            0.0,
                            min(
                                revision_confidence,
                                float(row["edge_confidence"]),
                                1.0,
                            ),
                        ),
                        "model": str(revision["model"]),
                        "prompt_version": str(revision["prompt_version"]),
                        "reason": str(revision["reason"]),
                        "revision_id": str(revision["revision_id"]),
                    },
                )
            if len(accepted_definitions) != 1:
                continue
            accepted = next(iter(accepted_definitions.values()))
            basis = str(row["basis"])
            if basis not in {"book", "model", "reviewed", "derived"}:
                continue
            if basis != "model" and not accepted["evidence_ids"]:
                continue

            metadata = {
                "source_meaning_id": str(row["source_meaning_id"]),
                "translation_id": str(row["entity_id"]),
                "reconciled_from_revision_id": accepted["revision_id"],
                "model": accepted["model"],
                "prompt_version": accepted["prompt_version"],
            }
            target_meaning_id = self.add_entity_meaning(
                str(row["target_term_id"]),
                accepted["definition"],
                language=str(row["target_language"]),
                status="accepted",
                quality_score=row["quality_score"],
                basis=basis,
                confidence=accepted["confidence"],
                evidence_ids=accepted["evidence_ids"],
                metadata=metadata,
                revision_model=accepted["model"],
                revision_prompt_version=accepted["prompt_version"],
                revision_reason=accepted["reason"],
            )
            self.add_edge(
                target_meaning_id,
                str(row["source_meaning_id"]),
                "translated-from",
                basis=basis,
                confidence=accepted["confidence"],
                properties={
                    **metadata,
                    "evidence_ids": accepted["evidence_ids"],
                },
                status="accepted",
            )

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
                CREATE INDEX IF NOT EXISTS idx_entities_updated_at
                    ON entities(updated_at DESC);

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
                    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    term_id TEXT REFERENCES terms(entity_id),
                    language TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    part_of_speech TEXT NOT NULL DEFAULT '',
                    register_label TEXT NOT NULL DEFAULT '',
                    domain_label TEXT NOT NULL DEFAULT '',
                    sense_order INTEGER NOT NULL DEFAULT 0,
                    CHECK(term_id IS NULL OR term_id = subject_entity_id)
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
                CREATE INDEX IF NOT EXISTS idx_edges_accepted_source_priority
                    ON entity_edges(
                        source_entity_id, confidence DESC, relation,
                        target_entity_id, edge_id
                    ) WHERE status = 'accepted';
                CREATE INDEX IF NOT EXISTS idx_edges_accepted_target_priority
                    ON entity_edges(
                        target_entity_id, confidence DESC, relation,
                        source_entity_id, edge_id
                    ) WHERE status = 'accepted';
                CREATE INDEX IF NOT EXISTS idx_edges_accepted_triple
                    ON entity_edges(source_entity_id, target_entity_id, relation)
                    WHERE status = 'accepted';

                CREATE TABLE IF NOT EXISTS relation_assertions (
                    assertion_id TEXT PRIMARY KEY,
                    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    relation TEXT NOT NULL,
                    basis TEXT NOT NULL DEFAULT 'model'
                        CHECK(basis IN ('book', 'model', 'reviewed', 'derived')),
                    confidence REAL NOT NULL DEFAULT 0.5
                        CHECK(confidence >= 0 AND confidence <= 1),
                    status TEXT NOT NULL DEFAULT 'accepted'
                        CHECK(status IN ('draft', 'accepted', 'rejected', 'archived')),
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(
                        subject_entity_id, source_entity_id,
                        target_entity_id, relation
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_relation_assertions_subject
                    ON relation_assertions(subject_entity_id, status, relation);
                CREATE INDEX IF NOT EXISTS idx_relation_assertions_source
                    ON relation_assertions(source_entity_id, status, relation);
                CREATE INDEX IF NOT EXISTS idx_relation_assertions_target
                    ON relation_assertions(target_entity_id, status, relation);
                CREATE INDEX IF NOT EXISTS idx_assertions_accepted_source_priority
                    ON relation_assertions(
                        source_entity_id, confidence DESC, relation,
                        target_entity_id, assertion_id
                    ) WHERE status = 'accepted';
                CREATE INDEX IF NOT EXISTS idx_assertions_accepted_target_priority
                    ON relation_assertions(
                        target_entity_id, confidence DESC, relation,
                        source_entity_id, assertion_id
                    ) WHERE status = 'accepted';
                CREATE INDEX IF NOT EXISTS idx_assertions_accepted_triple
                    ON relation_assertions(
                        source_entity_id, target_entity_id, relation
                    ) WHERE status = 'accepted';

                CREATE TABLE IF NOT EXISTS assertion_evidence (
                    assertion_id TEXT NOT NULL REFERENCES relation_assertions(assertion_id)
                        ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
                    PRIMARY KEY(assertion_id, evidence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_assertion_evidence_record
                    ON assertion_evidence(evidence_id, assertion_id);
                CREATE INDEX IF NOT EXISTS idx_assertion_evidence_assertion
                    ON assertion_evidence(assertion_id, evidence_id);

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
                CREATE INDEX IF NOT EXISTS idx_jobs_type_next
                    ON preparation_jobs(
                        status, job_type, priority, created_at
                    );
                CREATE INDEX IF NOT EXISTS idx_jobs_subject_snapshot
                    ON preparation_jobs(
                        subject_key, prompt_version, source_fingerprint,
                        job_type, status
                    );

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
                        CHECK(source_kind IN (
                            'qa-investigation', 'word-origins', 'derivative',
                            'card-derivative', 'historical-form',
                            'shared-morpheme', 'graph-frontier'
                        )),
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
            self._migrate_meanings_schema_v6(connection)
            discovery_schema = connection.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type = 'table' AND name = 'lexical_discoveries'"""
            ).fetchone()
            if discovery_schema and "graph-frontier" not in str(discovery_schema["sql"]):
                connection.execute(
                    """ALTER TABLE lexical_discoveries
                       RENAME TO lexical_discoveries_source_kind_legacy"""
                )
                connection.execute(
                    """CREATE TABLE lexical_discoveries (
                           discovery_id TEXT PRIMARY KEY,
                           round_id TEXT NOT NULL
                               REFERENCES lexical_discovery_rounds(round_id)
                               ON DELETE CASCADE,
                           language TEXT NOT NULL,
                           term TEXT NOT NULL,
                           normalized TEXT NOT NULL,
                           source_kind TEXT NOT NULL CHECK(source_kind IN (
                               'qa-investigation', 'word-origins', 'derivative',
                               'card-derivative', 'historical-form',
                               'shared-morpheme', 'graph-frontier'
                           )),
                           source_entity_id TEXT NOT NULL DEFAULT '',
                           source_artifact_id TEXT NOT NULL DEFAULT '',
                           source_entry_id TEXT NOT NULL DEFAULT '',
                           ordinal INTEGER NOT NULL,
                           planned_at TEXT NOT NULL DEFAULT '',
                           created_at TEXT NOT NULL,
                           UNIQUE(language, normalized),
                           UNIQUE(round_id, ordinal)
                       )"""
                )
                connection.execute(
                    """INSERT INTO lexical_discoveries(
                           discovery_id, round_id, language, term, normalized,
                           source_kind, source_entity_id, source_artifact_id,
                           source_entry_id, ordinal, planned_at, created_at
                       )
                       SELECT discovery_id, round_id, language, term, normalized,
                              source_kind, source_entity_id, source_artifact_id,
                              source_entry_id, ordinal, planned_at, created_at
                       FROM lexical_discoveries_source_kind_legacy"""
                )
                connection.execute(
                    "DROP TABLE lexical_discoveries_source_kind_legacy"
                )
                connection.execute(
                    """CREATE INDEX idx_lexical_discoveries_round
                       ON lexical_discoveries(round_id, ordinal)"""
                )
                connection.execute(
                    """CREATE INDEX idx_lexical_discoveries_pending
                       ON lexical_discoveries(planned_at, created_at)"""
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lexical_discovery_observations (
                    observation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    discovery_id TEXT REFERENCES lexical_discoveries(discovery_id)
                        ON DELETE SET NULL,
                    term_entity_id TEXT REFERENCES terms(entity_id) ON DELETE SET NULL,
                    language TEXT NOT NULL,
                    term TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    source_kind TEXT NOT NULL DEFAULT 'unknown',
                    source_entity_id TEXT NOT NULL DEFAULT '',
                    source_artifact_id TEXT NOT NULL DEFAULT '',
                    source_entry_id TEXT NOT NULL DEFAULT '',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    graph_fingerprint TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lexical_observations_term
                    ON lexical_discovery_observations(
                        language, normalized, observed_at
                    );
                CREATE INDEX IF NOT EXISTS idx_lexical_observations_entity
                    ON lexical_discovery_observations(
                        term_entity_id, observed_at
                    );

                CREATE TABLE IF NOT EXISTS lexical_revisit_epochs (
                    epoch_id TEXT PRIMARY KEY,
                    term_entity_id TEXT NOT NULL REFERENCES terms(entity_id)
                        ON DELETE CASCADE,
                    epoch_number INTEGER NOT NULL CHECK(epoch_number >= 1),
                    prompt_version TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'claimed'
                        CHECK(status IN (
                            'claimed', 'planned', 'complete', 'failed', 'expired'
                        )),
                    novelty_kind TEXT NOT NULL
                        CHECK(novelty_kind IN ('graph-changed', 'cooldown')),
                    fingerprint_before TEXT NOT NULL,
                    fingerprint_after TEXT NOT NULL DEFAULT '',
                    job_subject_key TEXT NOT NULL DEFAULT '',
                    result_summary TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(term_entity_id, epoch_number)
                );
                CREATE INDEX IF NOT EXISTS idx_lexical_revisit_epochs_status
                    ON lexical_revisit_epochs(status, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_lexical_revisit_epochs_term
                    ON lexical_revisit_epochs(term_entity_id, epoch_number DESC);

                CREATE TABLE IF NOT EXISTS lexical_revisit_state (
                    term_entity_id TEXT PRIMARY KEY REFERENCES terms(entity_id)
                        ON DELETE CASCADE,
                    last_observed_fingerprint TEXT NOT NULL DEFAULT '',
                    last_completed_fingerprint TEXT NOT NULL DEFAULT '',
                    last_observed_at TEXT NOT NULL DEFAULT '',
                    last_claimed_at TEXT NOT NULL DEFAULT '',
                    last_completed_at TEXT NOT NULL DEFAULT '',
                    next_eligible_at TEXT NOT NULL DEFAULT '',
                    revisit_count INTEGER NOT NULL DEFAULT 0
                        CHECK(revisit_count >= 0),
                    unchanged_count INTEGER NOT NULL DEFAULT 0
                        CHECK(unchanged_count >= 0),
                    active_epoch_id TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lexical_revisit_state_due
                    ON lexical_revisit_state(
                        next_eligible_at, last_observed_at, term_entity_id
                    );
                """
            )
            legacy_observations = connection.execute(
                """SELECT discovery.discovery_id, discovery.language,
                          discovery.term, discovery.normalized,
                          discovery.source_kind, discovery.source_entity_id,
                          discovery.source_artifact_id, discovery.source_entry_id,
                          discovery.created_at, term.entity_id AS term_entity_id
                   FROM lexical_discoveries AS discovery
                   LEFT JOIN terms AS term
                     ON term.language = discovery.language
                    AND term.normalized = discovery.normalized
                    AND term.kind = 'word'"""
            ).fetchall()
            for observation in legacy_observations:
                idempotency_key = f"legacy-discovery:{observation['discovery_id']}"
                connection.execute(
                    """INSERT OR IGNORE INTO lexical_discovery_observations(
                           observation_id, idempotency_key, discovery_id,
                           term_entity_id, language, term, normalized,
                           source_kind, source_entity_id, source_artifact_id,
                           source_entry_id, observed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _identifier("lexical-observation", idempotency_key),
                        idempotency_key,
                        observation["discovery_id"],
                        observation["term_entity_id"],
                        observation["language"],
                        observation["term"],
                        observation["normalized"],
                        observation["source_kind"],
                        observation["source_entity_id"],
                        observation["source_artifact_id"],
                        observation["source_entry_id"],
                        observation["created_at"],
                    ),
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
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                ("relation_graph_revision", "0"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                ("projection_content_revision", "0"),
            )
            previous_schema_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            try:
                previous_schema_version = int(previous_schema_row[0]) if previous_schema_row else 0
            except (TypeError, ValueError):
                previous_schema_version = 0
            ensure_relation_support_schema(connection)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS relation_assertion_events (
                       event_id TEXT PRIMARY KEY,
                       decision_id TEXT NOT NULL,
                       assertion_id TEXT NOT NULL REFERENCES relation_assertions(assertion_id) ON DELETE CASCADE,
                       action TEXT NOT NULL CHECK(action IN ('baseline','accept','update','keep','archive','reject')),
                       before_json TEXT NOT NULL DEFAULT '{}',
                       after_json TEXT NOT NULL DEFAULT '{}',
                       evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                       context_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                       input_fingerprint TEXT NOT NULL DEFAULT '',
                       output_fingerprint TEXT NOT NULL DEFAULT '',
                       job_id TEXT REFERENCES preparation_jobs(job_id),
                       reason TEXT NOT NULL DEFAULT '',
                       validator_version TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_relation_assertion_events_assertion
                   ON relation_assertion_events(assertion_id, created_at, event_id)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_relation_assertion_events_decision
                   ON relation_assertion_events(decision_id, action)"""
            )
            connection.execute(
                """CREATE TRIGGER IF NOT EXISTS relation_assertion_archive_event_v8
                   AFTER UPDATE OF status ON relation_assertions
                   WHEN OLD.status <> 'archived' AND NEW.status = 'archived'
                   BEGIN
                       INSERT OR IGNORE INTO relation_assertion_events(
                           event_id, decision_id, assertion_id, action,
                           before_json, after_json, evidence_ids_json,
                           context_evidence_ids_json, input_fingerprint,
                           output_fingerprint, job_id, reason, validator_version, created_at
                       ) VALUES (
                           'relation-event:archive:' || NEW.assertion_id || ':' || NEW.revision,
                           'relation-decision:archive:' || NEW.assertion_id || ':' || NEW.revision,
                           NEW.assertion_id, 'archive',
                           json_object(
                               'assertion_id', OLD.assertion_id, 'subject_entity_id', OLD.subject_entity_id,
                               'source_entity_id', OLD.source_entity_id, 'target_entity_id', OLD.target_entity_id,
                               'relation', OLD.relation, 'basis', OLD.basis, 'confidence', OLD.confidence,
                               'status', OLD.status, 'properties', json(OLD.properties_json),
                               'revision', OLD.revision, 'created_at', OLD.created_at, 'updated_at', OLD.updated_at
                           ),
                           json_object(
                               'assertion_id', NEW.assertion_id, 'subject_entity_id', NEW.subject_entity_id,
                               'source_entity_id', NEW.source_entity_id, 'target_entity_id', NEW.target_entity_id,
                               'relation', NEW.relation, 'basis', NEW.basis, 'confidence', NEW.confidence,
                               'status', NEW.status, 'properties', json(NEW.properties_json),
                               'revision', NEW.revision, 'created_at', NEW.created_at, 'updated_at', NEW.updated_at
                           ),
                           COALESCE((SELECT json_group_array(evidence_id) FROM (
                               SELECT evidence_id FROM assertion_evidence
                               WHERE assertion_id = NEW.assertion_id ORDER BY evidence_id
                           )), '[]'),
                           CASE WHEN json_type(NEW.properties_json, '$.context_evidence_ids') = 'array'
                               THEN json_extract(NEW.properties_json, '$.context_evidence_ids') ELSE '[]' END,
                           'assertion-revision:' || OLD.revision,
                           'assertion-revision:' || NEW.revision,
                           NULL,
                           'accepted assertion archived by store reconciliation',
                           'relation-assertion-archive-v1',
                           NEW.updated_at
                       );
                   END"""
            )
            if previous_schema_version < 8:
                baseline_rows = connection.execute(
                    """SELECT assertion_id, subject_entity_id, source_entity_id,
                              target_entity_id, relation, basis, confidence, status,
                              properties_json, revision, created_at, updated_at
                       FROM relation_assertions ORDER BY assertion_id"""
                ).fetchall()
                for baseline_row in baseline_rows:
                    assertion_id = str(baseline_row["assertion_id"])
                    exact_ids = self._relation_assertion_evidence_ids(connection, assertion_id)
                    baseline_properties = self._relation_assertion_properties(
                        baseline_row["properties_json"]
                    )
                    context_ids = self._relation_context_evidence_ids(baseline_properties)
                    baseline_state = self._relation_assertion_snapshot(
                        baseline_row, exact_ids, context_ids
                    )
                    state_json = self._canonical_relation_json(baseline_state)
                    state_fingerprint = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
                    connection.execute(
                        """INSERT OR IGNORE INTO relation_assertion_events(
                               event_id, decision_id, assertion_id, action,
                               before_json, after_json, evidence_ids_json,
                               context_evidence_ids_json, input_fingerprint,
                               output_fingerprint, job_id, reason, validator_version, created_at
                           ) VALUES (?, ?, ?, 'baseline', '{}', ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                        (
                            _identifier("relation-event", f"baseline:{assertion_id}"),
                            _identifier("relation-decision", f"baseline:{assertion_id}"),
                            assertion_id,
                            state_json,
                            self._canonical_relation_json(exact_ids),
                            self._canonical_relation_json(context_ids),
                            state_fingerprint,
                            state_fingerprint,
                            "schema-v8 current-state baseline; historical revisions are not reconstructed",
                            "relation-assertion-decision-v1",
                            _now(),
                        ),
                    )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS relation_assertion_slots (
                       assertion_id TEXT PRIMARY KEY
                           REFERENCES relation_assertions(assertion_id) ON DELETE CASCADE,
                       slot_key TEXT NOT NULL,
                       slot_mode TEXT NOT NULL
                           CHECK(slot_mode IN ('exclusive','set-member','none')),
                       policy_version TEXT NOT NULL,
                       created_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_relation_assertion_slots_key
                   ON relation_assertion_slots(
                       slot_key, slot_mode, assertion_id
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS relation_claim_observations (
                       observation_id TEXT PRIMARY KEY,
                       slot_key TEXT NOT NULL,
                       slot_mode TEXT NOT NULL
                           CHECK(slot_mode IN ('exclusive','set-member','none')),
                       proposed_assertion_id TEXT NOT NULL,
                       incumbent_assertion_ids_json TEXT NOT NULL DEFAULT '[]',
                       proposal_json TEXT NOT NULL,
                       decision TEXT NOT NULL
                           CHECK(decision IN ('pending','keep','conflict','supersede','coexist')),
                       exact_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                       context_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                       input_fingerprint TEXT NOT NULL DEFAULT '',
                       output_fingerprint TEXT NOT NULL DEFAULT '',
                       epoch_fingerprint TEXT NOT NULL DEFAULT '',
                       prompt_version TEXT NOT NULL DEFAULT '',
                       job_id TEXT REFERENCES preparation_jobs(job_id),
                       reason TEXT NOT NULL DEFAULT '',
                       policy_version TEXT NOT NULL,
                       created_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_relation_claim_observations_slot
                   ON relation_claim_observations(
                       slot_key, proposed_assertion_id, decision, created_at
                   )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_relation_claim_observations_epoch
                   ON relation_claim_observations(
                       slot_key, proposed_assertion_id, epoch_fingerprint
                   )"""
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lexical_projection_repairs (
                    term_entity_id TEXT PRIMARY KEY
                        REFERENCES terms(entity_id) ON DELETE CASCADE,
                    desired_fingerprint TEXT NOT NULL DEFAULT '',
                    applied_fingerprint TEXT NOT NULL DEFAULT '',
                    desired_revision INTEGER NOT NULL DEFAULT 0
                        CHECK(desired_revision >= 0),
                    desired_modes_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN (
                            'pending', 'claimed', 'planned',
                            'cooldown', 'applied'
                        )),
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    compose_job_id TEXT
                        REFERENCES preparation_jobs(job_id) ON DELETE SET NULL,
                    bound_fingerprint TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(attempt_count >= 0),
                    failure_count INTEGER NOT NULL DEFAULT 0
                        CHECK(failure_count >= 0),
                    next_eligible_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_claimed_at TEXT NOT NULL DEFAULT '',
                    last_applied_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lexical_projection_repairs_due
                    ON lexical_projection_repairs(
                        status, next_eligible_at, updated_at, term_entity_id
                    );
                CREATE INDEX IF NOT EXISTS idx_lexical_projection_repairs_job
                    ON lexical_projection_repairs(compose_job_id);

                CREATE TABLE IF NOT EXISTS lexical_projection_bootstrap_state (
                    scheduler_key TEXT PRIMARY KEY,
                    cursor_entity_id TEXT NOT NULL DEFAULT '',
                    scanned_count INTEGER NOT NULL DEFAULT 0
                        CHECK(scanned_count >= 0),
                    complete INTEGER NOT NULL DEFAULT 0
                        CHECK(complete IN (0, 1)),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lexical_semantic_enrichments (
                    entity_id TEXT NOT NULL
                        REFERENCES entities(entity_id) ON DELETE CASCADE,
                    language TEXT NOT NULL,
                    entity_type TEXT NOT NULL
                        CHECK(entity_type IN ('term', 'morpheme')),
                    source_language TEXT NOT NULL DEFAULT '',
                    entity_kind TEXT NOT NULL DEFAULT '',
                    job_type TEXT NOT NULL,
                    desired_fingerprint TEXT NOT NULL DEFAULT '',
                    applied_fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN (
                            'pending', 'claimed', 'planned',
                            'cooldown', 'applied'
                        )),
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    enrichment_job_id TEXT
                        REFERENCES preparation_jobs(job_id) ON DELETE SET NULL,
                    bound_fingerprint TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(attempt_count >= 0),
                    failure_count INTEGER NOT NULL DEFAULT 0
                        CHECK(failure_count >= 0),
                    next_eligible_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_claimed_at TEXT NOT NULL DEFAULT '',
                    last_applied_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(entity_id, language)
                );
                CREATE INDEX IF NOT EXISTS idx_lexical_semantic_due
                    ON lexical_semantic_enrichments(
                        status, next_eligible_at, updated_at, entity_id, language
                    );
                CREATE INDEX IF NOT EXISTS idx_lexical_semantic_job
                    ON lexical_semantic_enrichments(enrichment_job_id);

                CREATE TABLE IF NOT EXISTS lexical_semantic_bootstrap_state (
                    scheduler_key TEXT PRIMARY KEY,
                    cursor_entity_id TEXT NOT NULL DEFAULT '',
                    scanned_count INTEGER NOT NULL DEFAULT 0
                        CHECK(scanned_count >= 0),
                    complete INTEGER NOT NULL DEFAULT 0
                        CHECK(complete IN (0, 1)),
                    observed_graph_revision INTEGER NOT NULL DEFAULT -1,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """INSERT OR IGNORE INTO lexical_projection_bootstrap_state(
                       scheduler_key, updated_at
                   ) VALUES (?, ?)""",
                (_projection_bootstrap_scheduler_key(), _now()),
            )
            connection.execute(
                """INSERT OR IGNORE INTO lexical_semantic_bootstrap_state(
                       scheduler_key, updated_at
                   ) VALUES ('semantic-v12', ?)""",
                (_now(),),
            )
            slot_rows = connection.execute(
                """SELECT assertion.assertion_id,
                          assertion.subject_entity_id,
                          assertion.source_entity_id,
                          assertion.target_entity_id, assertion.relation,
                          assertion.properties_json
                   FROM relation_assertions AS assertion
                   LEFT JOIN relation_assertion_slots AS slot
                     ON slot.assertion_id = assertion.assertion_id
                   WHERE slot.assertion_id IS NULL
                      OR slot.policy_version <> 'relation-slot-v2'
                   ORDER BY assertion.assertion_id"""
            ).fetchall()
            for slot_row in slot_rows:
                slot = self._relation_slot_projection(
                    connection,
                    str(slot_row["subject_entity_id"]),
                    str(slot_row["source_entity_id"]),
                    str(slot_row["target_entity_id"]),
                    str(slot_row["relation"]),
                    self._relation_assertion_properties(slot_row["properties_json"]),
                )
                connection.execute(
                    """INSERT INTO relation_assertion_slots(
                           assertion_id, slot_key, slot_mode,
                           policy_version, created_at
                       ) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(assertion_id) DO UPDATE SET
                           slot_key = excluded.slot_key,
                           slot_mode = excluded.slot_mode,
                           policy_version = excluded.policy_version""",
                    (
                        slot_row["assertion_id"],
                        slot["slot_key"],
                        slot["slot_mode"],
                        slot["slot_policy_version"],
                        _now(),
                    ),
                )
            if previous_schema_version < 11:
                self._bootstrap_lexical_projection_repairs_connection(
                    connection, limit=64
                )
            if previous_schema_version < 12:
                self._bootstrap_lexical_semantic_enrichments_connection(
                    connection, limit=64
                )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(max(previous_schema_version, _CURRENT_KNOWLEDGE_SCHEMA_VERSION))),
            )
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='morphology_enrichment_clusters_v3'"
            ).fetchone() is not None:
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS idx_morph_enrichment_v3_last_job
                       ON morphology_enrichment_clusters_v3(
                           last_job_id, parser_version
                       )"""
                )
            self._install_projection_revision_triggers(connection)
            connection.commit()
        self._reconcile_legacy_translation_meanings_v6()
        self._reconcile_relation_authority_v10()

    @staticmethod
    def _install_projection_revision_triggers(
        connection: sqlite3.Connection,
        only_tables: Iterable[str] | None = None,
    ) -> None:
        """Invalidate projections only for presentation-relevant writes.

        Queue leases and worker heartbeats are intentionally absent. SQLite
        triggers cover every writer, including repair paths that do not pass
        through a single Python helper.
        """

        presentation_tables = tuple(only_tables or (
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
            "content_items",
            "entity_edges",
            "relation_assertions",
            "relation_assertion_slots",
            "assertion_evidence",
            "entity_properties",
            "evidence_records",
            "entity_evidence",
            "job_artifacts",
            "morphology_source_observations_v3",
            "morphology_enrichment_clusters_v3",
        ))
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in presentation_tables:
            if table not in existing_tables:
                continue
            for operation in ("INSERT", "UPDATE", "DELETE"):
                trigger = f"projection_revision_{table}_{operation.lower()}_v1"
                connection.execute(
                    f'''CREATE TRIGGER IF NOT EXISTS "{trigger}"
                        AFTER {operation} ON "{table}"
                        BEGIN
                            UPDATE schema_meta
                            SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
                            WHERE key = 'projection_content_revision';
                        END'''
                )

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
        before = connection.execute(
            """SELECT entity_id, label, status, quality_score, payload
               FROM entities WHERE canonical_key = ?""",
            (canonical_key,),
        ).fetchone()
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
        if before is not None:
            after = connection.execute(
                """SELECT entity_id, label, status, quality_score, payload
                   FROM entities WHERE entity_id = ?""",
                (row["entity_id"],),
            ).fetchone()
            if after is not None and any(
                before[field] != after[field]
                for field in ("label", "status", "quality_score", "payload")
            ):
                self._mark_projection_repairs_for_entities(
                    connection, (str(row["entity_id"]),), timestamp=timestamp
                )
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
            resolved_payload = {
                **(resolved_payload or {}),
                "language": language,
                "kind": kind,
            }
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
        kind = kind.strip().lower()
        if kind not in {"prefix", "root", "suffix", "free", "unknown"}:
            raise ValueError(f"invalid morpheme kind: {kind}")
        normalized, form, canonical_key = canonical_morpheme_identity(
            language, form, kind
        )
        with closing(self._connect()) as connection:
            existing = connection.execute(
                """SELECT morpheme.meaning, entity.payload
                   FROM entities AS entity
                   LEFT JOIN morphemes AS morpheme
                     ON morpheme.entity_id = entity.entity_id
                   WHERE entity.canonical_key = ?""",
                (canonical_key,),
            ).fetchone()
            existing_payload = (
                _json_object(str(existing["payload"])) if existing is not None else {}
            )
            existing_meaning = (
                str(existing["meaning"] or "").strip()
                if existing is not None
                else ""
            )
            resolved_meaning = meaning.strip() or existing_meaning or str(
                existing_payload.get("meaning", "")
            ).strip()
            entity_id = self._upsert_entity(
                connection,
                "morpheme",
                canonical_key,
                form,
                {
                    **existing_payload,
                    "language": language,
                    "kind": kind,
                    "meaning": resolved_meaning,
                },
                status,
                quality_score,
            )
            connection.execute(
                """INSERT INTO morphemes(
                       entity_id, language, form, normalized, kind, meaning
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                       form = excluded.form,
                       meaning = CASE
                           WHEN TRIM(excluded.meaning) = ''
                                AND TRIM(morphemes.meaning) <> ''
                           THEN morphemes.meaning ELSE excluded.meaning END""",
                (entity_id, language, form, normalized, kind, resolved_meaning),
            )
            self._mark_projection_repairs_for_entities(connection, (entity_id,))
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
        confidence = max(0.0, min(float(confidence), 1.0))
        properties_json = json.dumps(
            properties or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        timestamp = _now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_unarchived_endpoints(
                    connection, source_entity_id, target_entity_id
                )
                existing = connection.execute(
                    """SELECT edge_id, basis, confidence, status, properties
                       FROM entity_edges
                       WHERE source_entity_id = ? AND target_entity_id = ?
                         AND relation = ?""",
                    (source_entity_id, target_entity_id, relation),
                ).fetchone()
                controlled_triples = {
                    (
                        str(row["source_entity_id"]),
                        str(row["target_entity_id"]),
                        str(row["relation"]),
                    )
                    for row in connection.execute(
                        """SELECT source_entity_id, target_entity_id, relation
                           FROM relation_assertions
                           WHERE source_entity_id = ? AND target_entity_id = ?
                             AND relation = ?""",
                        (source_entity_id, target_entity_id, relation),
                    )
                }
                controlled_slots: set[str] = set()
                proposed_slot = self._relation_slot_projection(
                    connection,
                    source_entity_id,
                    source_entity_id,
                    target_entity_id,
                    relation,
                    self._relation_assertion_properties(properties or {}),
                )
                if proposed_slot["slot_mode"] == "exclusive":
                    slot_rows = connection.execute(
                        """SELECT assertion.source_entity_id,
                                  assertion.target_entity_id, assertion.relation
                           FROM relation_assertion_slots AS slot
                           JOIN relation_assertions AS assertion
                             ON assertion.assertion_id = slot.assertion_id
                           WHERE slot.slot_key = ?""",
                        (proposed_slot["slot_key"],),
                    ).fetchall()
                    if slot_rows:
                        controlled_slots.add(str(proposed_slot["slot_key"]))
                        controlled_triples.update(
                            (
                                str(row["source_entity_id"]),
                                str(row["target_entity_id"]),
                                str(row["relation"]),
                            )
                            for row in slot_rows
                        )
                if controlled_triples or controlled_slots:
                    controlled_triples.add(
                        (source_entity_id, target_entity_id, relation)
                    )
                    authority_changed = self._sync_relation_authority(
                        connection,
                        controlled_triples,
                        component_slot_keys=controlled_slots,
                        timestamp=timestamp,
                    )
                    if authority_changed:
                        self._advance_relation_graph_revision(connection)
                    if existing is not None:
                        edge_id = str(existing["edge_id"])
                    elif controlled_slots:
                        authority_edge = connection.execute(
                            """SELECT edge.edge_id
                               FROM relation_assertion_slots AS slot
                               JOIN relation_assertions AS assertion
                                 ON assertion.assertion_id = slot.assertion_id
                                AND assertion.status = 'accepted'
                               JOIN entity_edges AS edge
                                 ON edge.source_entity_id = assertion.source_entity_id
                                AND edge.target_entity_id = assertion.target_entity_id
                                AND edge.relation = assertion.relation
                                AND edge.status = 'accepted'
                               WHERE slot.slot_key = ?
                               ORDER BY assertion.confidence DESC,
                                        assertion.assertion_id LIMIT 1""",
                            (next(iter(controlled_slots)),),
                        ).fetchone()
                        if authority_edge is not None:
                            edge_id = str(authority_edge["edge_id"])
                    connection.commit()
                    return edge_id
                if existing is not None:
                    edge_id = str(existing["edge_id"])
                    try:
                        existing_properties = json.dumps(
                            json.loads(existing["properties"]),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_properties = str(existing["properties"])
                    unchanged = bool(
                        existing["basis"] == basis
                        and float(existing["confidence"]) == confidence
                        and existing["status"] == status
                        and existing_properties == properties_json
                    )
                    if unchanged:
                        connection.commit()
                        return edge_id
                    connection.execute(
                        """UPDATE entity_edges
                           SET basis = ?, confidence = ?, status = ?,
                               properties = ?, updated_at = ?
                           WHERE edge_id = ?""",
                        (
                            basis,
                            confidence,
                            status,
                            properties_json,
                            timestamp,
                            edge_id,
                        ),
                    )
                else:
                    connection.execute(
                        """INSERT INTO entity_edges(
                               edge_id, source_entity_id, target_entity_id,
                               relation, basis, confidence, status, properties,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            edge_id,
                            source_entity_id,
                            target_entity_id,
                            relation,
                            basis,
                            confidence,
                            status,
                            properties_json,
                            timestamp,
                            timestamp,
                        ),
                    )
                self._advance_relation_graph_revision(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return edge_id

    @staticmethod
    def _advance_relation_graph_revision(connection: sqlite3.Connection) -> int:
        connection.execute(
            """UPDATE schema_meta
               SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
               WHERE key = 'relation_graph_revision'"""
        )
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'relation_graph_revision'"
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    @staticmethod
    def _projection_subject_ids_for_entities(
        connection: sqlite3.Connection, entity_ids: Iterable[str]
    ) -> set[str]:
        """Resolve word projections governed by the changed local entities."""

        identifiers = tuple(
            sorted({str(item).strip() for item in entity_ids if str(item).strip()})
        )
        if not identifiers:
            return set()
        placeholders = ",".join("?" for _ in identifiers)
        subjects = {
            str(row["entity_id"])
            for row in connection.execute(
                f"""SELECT term.entity_id FROM terms AS term
                    JOIN entities AS entity ON entity.entity_id = term.entity_id
                    WHERE term.kind = 'word' AND entity.status = 'accepted'
                      AND term.entity_id IN ({placeholders})""",
                identifiers,
            )
        }
        excluded = tuple(sorted(_PROJECTION_EXCLUDED_RELATIONS))
        excluded_placeholders = ",".join("?" for _ in excluded)
        touching = connection.execute(
            f"""SELECT assertion.subject_entity_id,
                       assertion.source_entity_id, assertion.target_entity_id
                FROM relation_assertions AS assertion
                WHERE assertion.status = 'accepted'
                  AND assertion.relation NOT IN ({excluded_placeholders})
                  AND (
                      assertion.subject_entity_id IN ({placeholders})
                      OR assertion.source_entity_id IN ({placeholders})
                      OR assertion.target_entity_id IN ({placeholders})
                  )""",
            (*excluded, *identifiers, *identifiers, *identifiers),
        ).fetchall()
        touching_ids = {
            str(row[column])
            for row in touching
            for column in (
                "subject_entity_id", "source_entity_id", "target_entity_id"
            )
        }
        if touching_ids:
            touching_placeholders = ",".join("?" for _ in touching_ids)
            subjects.update(
                str(row["entity_id"])
                for row in connection.execute(
                    f"""SELECT term.entity_id FROM terms AS term
                        JOIN entities AS entity ON entity.entity_id = term.entity_id
                        WHERE term.kind = 'word' AND entity.status = 'accepted'
                          AND term.entity_id IN ({touching_placeholders})""",
                    tuple(sorted(touching_ids)),
                )
            )
            morpheme_ids = {
                str(row["entity_id"])
                for row in connection.execute(
                    f"""SELECT entity_id FROM morphemes
                        WHERE entity_id IN ({touching_placeholders})""",
                    tuple(sorted(touching_ids)),
                )
            }
            if morpheme_ids:
                morpheme_placeholders = ",".join("?" for _ in morpheme_ids)
                subjects.update(
                    str(row["subject_entity_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT assertion.subject_entity_id
                            FROM relation_assertions AS assertion
                            JOIN terms AS term
                              ON term.entity_id = assertion.subject_entity_id
                             AND term.kind = 'word'
                            JOIN entities AS entity
                              ON entity.entity_id = term.entity_id
                             AND entity.status = 'accepted'
                            WHERE {LIVE_SOURCE_COMPONENT_SQL}
                              AND assertion.target_entity_id IN
                                  ({morpheme_placeholders})""",
                        tuple(sorted(morpheme_ids)),
                    )
                )
        return subjects

    @staticmethod
    def _accepted_meaning_records(
        connection: sqlite3.Connection, subject_entity_ids: Iterable[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Return accepted entity-owned senses in deterministic authority order."""

        identifiers = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in subject_entity_ids
                    if str(item).strip()
                }
            )
        )
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _ in identifiers)
        rows = connection.execute(
            f"""SELECT meaning.subject_entity_id, meaning.entity_id,
                       meaning.language, meaning.definition,
                       meaning.part_of_speech, meaning.register_label,
                       meaning.domain_label, meaning.sense_order,
                       entity.quality_score, entity.payload,
                       edge.basis, edge.confidence, edge.properties,
                       COALESCE(
                           (
                               SELECT MAX(revision.created_at)
                               FROM entity_revisions AS revision
                               WHERE revision.entity_id = meaning.entity_id
                                 AND revision.accepted = 1
                           ),
                           MAX(entity.updated_at, edge.updated_at),
                           entity.created_at
                       ) AS accepted_at
                FROM meanings AS meaning
                JOIN entities AS entity
                  ON entity.entity_id = meaning.entity_id
                 AND entity.status = 'accepted'
                JOIN entity_edges AS edge
                  ON edge.source_entity_id = meaning.subject_entity_id
                 AND edge.target_entity_id = meaning.entity_id
                 AND edge.relation = 'has-meaning'
                 AND edge.status = 'accepted'
                WHERE meaning.subject_entity_id IN ({placeholders})
                  AND TRIM(meaning.definition) <> ''""",
            identifiers,
        ).fetchall()
        output: dict[str, list[dict[str, Any]]] = {}
        basis_rank = {"book": 0, "reviewed": 1, "source": 2, "model": 4}
        for row in rows:
            evidence_ids = [
                str(item[0])
                for item in connection.execute(
                    """SELECT evidence_id FROM entity_evidence
                       WHERE entity_id = ? ORDER BY evidence_id""",
                    (str(row["entity_id"]),),
                ).fetchall()
            ]
            record = {
                "subject_entity_id": str(row["subject_entity_id"]),
                "meaning_id": str(row["entity_id"]),
                "language": str(row["language"]),
                "definition": " ".join(str(row["definition"]).split()),
                "part_of_speech": str(row["part_of_speech"]),
                "register_label": str(row["register_label"]),
                "domain_label": str(row["domain_label"]),
                "sense_order": int(row["sense_order"]),
                "quality_score": row["quality_score"],
                "payload": _json_object(str(row["payload"])),
                "basis": str(row["basis"]),
                "confidence": float(row["confidence"]),
                "properties": _json_object(str(row["properties"])),
                "evidence_ids": evidence_ids,
                "_accepted_at": str(row["accepted_at"]),
            }
            output.setdefault(str(row["subject_entity_id"]), []).append(record)
        for records in output.values():
            # Stable pre-sort makes a newer accepted correction win only after
            # canonical authority, evidence, quality, language, and sense order
            # agree. The ID remains a deterministic tie-break for legacy rows.
            records.sort(
                key=lambda item: (
                    str(item["_accepted_at"]),
                    str(item["meaning_id"]),
                ),
                reverse=True,
            )
            records.sort(
                key=lambda item: (
                    basis_rank.get(str(item["basis"]).casefold(), 3),
                    0 if item["evidence_ids"] else 1,
                    -float(item["quality_score"] or 0.0),
                    -len(item["evidence_ids"]),
                    -float(item["confidence"] or 0.0),
                    0 if str(item["language"]).casefold() == "en" else 1,
                    int(item["sense_order"]),
                )
            )
            for record in records:
                record.pop("_accepted_at", None)
        return output

    def _hydrate_accepted_morpheme_meanings(self) -> int:
        """Fill only blank morpheme caches from canonical accepted senses."""

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                identifiers = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """SELECT morpheme.entity_id FROM morphemes AS morpheme
                           JOIN entities AS entity
                             ON entity.entity_id = morpheme.entity_id
                            AND entity.status = 'accepted'
                           WHERE TRIM(morpheme.meaning) = ''
                           ORDER BY morpheme.entity_id"""
                    ).fetchall()
                )
                records = self._accepted_meaning_records(connection, identifiers)
                changed_ids: list[str] = []
                timestamp = _now()
                for entity_id in identifiers:
                    candidates = records.get(entity_id, [])
                    if not candidates:
                        continue
                    definition = str(candidates[0]["definition"]).strip()
                    if not definition:
                        continue
                    connection.execute(
                        "UPDATE morphemes SET meaning = ? WHERE entity_id = ?",
                        (definition, entity_id),
                    )
                    row = connection.execute(
                        "SELECT payload FROM entities WHERE entity_id = ?",
                        (entity_id,),
                    ).fetchone()
                    payload = _json_object(str(row["payload"])) if row else {}
                    payload["meaning"] = definition
                    connection.execute(
                        """UPDATE entities SET payload = ?, updated_at = ?
                           WHERE entity_id = ?""",
                        (
                            json.dumps(payload, ensure_ascii=False),
                            timestamp,
                            entity_id,
                        ),
                    )
                    changed_ids.append(entity_id)
                if changed_ids:
                    self._mark_projection_repairs_for_entities(
                        connection, changed_ids, timestamp=timestamp
                    )
                connection.commit()
                return len(changed_ids)
            except Exception:
                connection.rollback()
                raise

    def _projection_repair_snapshot(
        self, connection: sqlite3.Connection, term_entity_id: str
    ) -> tuple[str, tuple[str, ...], int] | None:
        """Hash only accepted graph state that can alter one lexical projection."""

        term = connection.execute(
            """SELECT term.entity_id FROM terms AS term
               JOIN entities AS entity ON entity.entity_id = term.entity_id
               WHERE term.entity_id = ? AND term.kind = 'word'
                 AND entity.status = 'accepted'""",
            (term_entity_id,),
        ).fetchone()
        if term is None:
            return None
        # Use the renderer's supported, subject-scoped component projection.
        # Reuse this transaction: opening another connection here would miss
        # a partial membership that has just been accepted but not committed.
        component_rows = [
            {
                "assertion_id": component["assertion_id"],
                "revision": component["revision"],
                "target_entity_id": component["morpheme_id"],
                "kind": component["kind"],
            }
            for component in self.accepted_morphology_components(
                term_entity_id, include_partial=True, _connection=connection,
            )
        ]
        component_ids = tuple(
            sorted({str(row["target_entity_id"]) for row in component_rows})
        )
        desired_modes = tuple(
            sorted(
                {
                    "root" if str(row["kind"]) == "root" else "affix"
                    for row in component_rows
                }
            )
        )
        touch_clauses = [
            "subject_entity_id = ?",
            "source_entity_id = ?",
            "target_entity_id = ?",
        ]
        parameters: list[Any] = [
            term_entity_id, term_entity_id, term_entity_id
        ]
        if component_ids:
            placeholders = ",".join("?" for _ in component_ids)
            touch_clauses.extend(
                (
                    f"subject_entity_id IN ({placeholders})",
                    f"source_entity_id IN ({placeholders})",
                    f"target_entity_id IN ({placeholders})",
                )
            )
            parameters.extend((*component_ids, *component_ids, *component_ids))
        excluded = tuple(sorted(_PROJECTION_EXCLUDED_RELATIONS))
        assertion_rows = connection.execute(
            """SELECT assertion_id, subject_entity_id, source_entity_id,
                      target_entity_id, relation, basis, confidence, status,
                      properties_json, revision
               FROM relation_assertions
               WHERE status = 'accepted' AND relation NOT IN ("""
            + ",".join("?" for _ in excluded)
            + ") AND ("
            + " OR ".join(touch_clauses)
            + ") ORDER BY assertion_id",
            (*excluded, *parameters),
        ).fetchall()
        assertions = []
        endpoint_ids = {term_entity_id, *component_ids}
        for row in assertion_rows:
            assertion_id = str(row["assertion_id"])
            evidence_ids = [
                str(item["evidence_id"])
                for item in connection.execute(
                    """SELECT evidence_id FROM assertion_evidence
                       WHERE assertion_id = ? ORDER BY evidence_id""",
                    (assertion_id,),
                )
            ]
            endpoint_ids.update(
                (
                    str(row["subject_entity_id"]),
                    str(row["source_entity_id"]),
                    str(row["target_entity_id"]),
                )
            )
            assertions.append(
                {
                    "id": assertion_id,
                    "subject": str(row["subject_entity_id"]),
                    "source": str(row["source_entity_id"]),
                    "target": str(row["target_entity_id"]),
                    "relation": str(row["relation"]),
                    "basis": str(row["basis"]),
                    "confidence": float(row["confidence"]),
                    "status": str(row["status"]),
                    "properties": self._canonical_fingerprint_value(
                        row["properties_json"]
                    ),
                    "revision": int(row["revision"]),
                    "evidence_ids": evidence_ids,
                }
            )
        edge_rows = connection.execute(
            """SELECT edge_id, source_entity_id, target_entity_id, relation,
                      basis, confidence, status, properties
               FROM entity_edges
               WHERE status = 'accepted' AND relation NOT IN ("""
            + ",".join("?" for _ in excluded)
            + ") AND ("
            + " OR ".join(
                clause.replace("subject_entity_id", "source_entity_id")
                for clause in touch_clauses
                if not clause.startswith("target_entity_id")
            )
            + " OR "
            + " OR ".join(
                clause.replace("subject_entity_id", "target_entity_id")
                for clause in touch_clauses
                if not clause.startswith("source_entity_id")
            )
            + ") ORDER BY edge_id",
            (
                *excluded,
                term_entity_id,
                term_entity_id,
                *component_ids,
                *component_ids,
                term_entity_id,
                term_entity_id,
                *component_ids,
                *component_ids,
            ),
        ).fetchall()
        edges = []
        for row in edge_rows:
            endpoint_ids.update(
                (str(row["source_entity_id"]), str(row["target_entity_id"]))
            )
            edges.append(
                {
                    "id": str(row["edge_id"]),
                    "source": str(row["source_entity_id"]),
                    "target": str(row["target_entity_id"]),
                    "relation": str(row["relation"]),
                    "basis": str(row["basis"]),
                    "confidence": float(row["confidence"]),
                    "status": str(row["status"]),
                    "properties": self._canonical_fingerprint_value(
                        row["properties"]
                    ),
                }
            )
        identifiers = tuple(sorted(endpoint_ids))
        placeholders = ",".join("?" for _ in identifiers)
        entities = [
            {
                "id": str(row["entity_id"]),
                "type": str(row["entity_type"]),
                "key": str(row["canonical_key"]),
                "label": str(row["label"]),
                "status": str(row["status"]),
                "quality": row["quality_score"],
                "payload": self._canonical_fingerprint_value(row["payload"]),
            }
            for row in connection.execute(
                f"""SELECT entity_id, entity_type, canonical_key, label,
                           status, quality_score, payload
                    FROM entities WHERE entity_id IN ({placeholders})
                    ORDER BY entity_id""",
                identifiers,
            )
        ]
        accepted_meanings = self._accepted_meaning_records(connection, identifiers)
        semantic_meanings = [
            record
            for entity_id in identifiers
            for record in accepted_meanings.get(entity_id, [])
        ]
        checkpoint_rows = connection.execute(
            """SELECT artifact.artifact_id, artifact.job_id, artifact.stage,
                      artifact.language, artifact.payload,
                      artifact.quality_score, artifact.created_at
               FROM job_artifacts AS artifact
               JOIN preparation_jobs AS job ON job.job_id = artifact.job_id
               WHERE artifact.validation_state = 'accepted'
                 AND artifact.stage IN (?, ?, ?)
                 AND (job.subject_entity_id = ? OR job.subject_key = ?)
               ORDER BY artifact.stage, artifact.language,
                        artifact.created_at, artifact.artifact_id""",
            (
                *_PROJECTION_REQUIRED_ARTIFACT_STAGES,
                term_entity_id,
                f"term:{term_entity_id}",
            ),
        ).fetchall()
        checkpoints = [
            {
                "artifact_id": str(row["artifact_id"]),
                "job_id": str(row["job_id"]),
                "stage": str(row["stage"]),
                "language": str(row["language"]),
                "payload": self._canonical_fingerprint_value(row["payload"]),
                "quality": row["quality_score"],
                "created_at": str(row["created_at"]),
            }
            for row in checkpoint_rows
        ]
        snapshot = {
            "projection_contract_version": (
                CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION
            ),
            "term_entity_id": term_entity_id,
            "desired_modes": desired_modes,
            "components": [
                {
                    "assertion_id": str(row["assertion_id"]),
                    "revision": int(row["revision"]),
                    "target": str(row["target_entity_id"]),
                    "kind": str(row["kind"]),
                }
                for row in component_rows
            ],
            "assertions": assertions,
            "edges": edges,
            "entities": entities,
            "accepted_meanings": semantic_meanings,
            "accepted_checkpoints": checkpoints,
        }
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            hashlib.sha256(encoded).hexdigest(),
            desired_modes,
            max((int(row["revision"]) for row in assertion_rows), default=0),
        )

    def repair_model_enrichment_membership_scopes(
        self, *, limit: int = 8, term_entity_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Upgrade attributed enrichment membership without a model request.

        Run outside another semantic writer. Nested public relation helpers
        borrow this transaction, so conversion, support retirement, reduction,
        positional cleanup, and projection invalidation commit together.
        """
        from .enrichment_membership import repair_membership_batch

        if getattr(self._write_context, "semantic", None) is not None:
            return {"scanned": 0, "converted_supports": 0,
                    "changed_assertions": 0, "deferred": True}
        with closing(self._open_connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = _BorrowedWriteState(connection)
            state.deferred_projection_entities = set()
            self._write_context.semantic = state
            try:
                result = repair_membership_batch(
                    self, connection, limit=limit, term_entity_ids=term_entity_ids,
                )
                if state.aborted:
                    raise RuntimeError("enrichment membership repair was rolled back")
                pending = state.deferred_projection_entities
                state.deferred_projection_entities = None
                if pending:
                    self._mark_projection_repairs_for_entities(connection, pending)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                del self._write_context.semantic

    def accepted_word_history_edges(self, term_entity_id: str) -> list[tuple[str, str]]:
        """Return live historical transitions connected to this source word.

        Normalize has-history to earlier -> later for publication. Follow only
        subject-owned, supported historical edges, not arbitrary graph neighbors.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT assertion.source_entity_id, assertion.target_entity_id,
                          assertion.relation
                   FROM relation_assertions AS assertion
                   JOIN entities AS source
                     ON source.entity_id = assertion.source_entity_id
                    AND source.status = 'accepted'
                   JOIN entities AS target
                     ON target.entity_id = assertion.target_entity_id
                    AND target.status = 'accepted'
                   WHERE assertion.subject_entity_id = ?
                     AND assertion.status = 'accepted'
                     AND EXISTS (
                         SELECT 1 FROM relation_assertion_supports AS support
                         WHERE support.assertion_id = assertion.assertion_id
                           AND support.status = 'accepted'
                     )
                     AND (
                         (assertion.relation = 'developed-into'
                          AND source.entity_type = 'historical-form'
                          AND target.entity_type IN ('historical-form', 'term'))
                         OR
                         (assertion.relation = 'has-history'
                          AND target.entity_type = 'historical-form'
                          AND source.entity_type IN ('historical-form', 'term'))
                     )""",
                (term_entity_id,),
            ).fetchall()
        incoming: dict[str, set[str]] = {}
        for row in rows:
            earlier, later = str(row[0]), str(row[1])
            if str(row[2]) == "has-history":
                earlier, later = later, earlier
            if earlier != later:
                incoming.setdefault(later, set()).add(earlier)
        frontier = [term_entity_id]
        visited = {term_entity_id}
        edges: set[tuple[str, str]] = set()
        while frontier:
            later = frontier.pop()
            for earlier in incoming.get(later, ()):
                edges.add((earlier, later))
                if earlier not in visited:
                    visited.add(earlier)
                    frontier.append(earlier)
        return sorted(edges)

    @staticmethod
    def _has_source_word_history(
        connection: sqlite3.Connection, term_entity_id: str
    ) -> bool:
        return connection.execute(
            f"""SELECT 1 FROM relation_assertions AS assertion
                WHERE assertion.subject_entity_id = ?
                  AND {_LIVE_SOURCE_WORD_HISTORY_SQL} LIMIT 1""",
            (term_entity_id,),
        ).fetchone() is not None

    def _refresh_projection_repair_state(
        self,
        connection: sqlite3.Connection,
        term_entity_id: str,
        *,
        timestamp: str | None = None,
    ) -> bool:
        snapshot = self._projection_repair_snapshot(connection, term_entity_id)
        if snapshot is None:
            return False
        fingerprint, desired_modes, desired_revision = snapshot
        existing = connection.execute(
            """SELECT desired_fingerprint, applied_fingerprint,
                      desired_modes_json FROM lexical_projection_repairs
               WHERE term_entity_id = ?""",
            (term_entity_id,),
        ).fetchone()
        modes_json = json.dumps(desired_modes, separators=(",", ":"))
        if existing is None:
            if not desired_modes and not self._has_source_word_history(connection, term_entity_id):
                return False
            now = timestamp or _now()
            connection.execute(
                """INSERT INTO lexical_projection_repairs(
                       term_entity_id, desired_fingerprint, desired_revision,
                       desired_modes_json, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    term_entity_id,
                    fingerprint,
                    desired_revision,
                    modes_json,
                    now,
                    now,
                ),
            )
            return True
        if (
            str(existing["desired_fingerprint"]) == fingerprint
            and str(existing["desired_modes_json"]) == modes_json
        ):
            return False
        now = timestamp or _now()
        applied = str(existing["applied_fingerprint"])
        connection.execute(
            """UPDATE lexical_projection_repairs
               SET desired_fingerprint = ?, desired_revision = ?,
                   desired_modes_json = ?,
                   status = CASE WHEN applied_fingerprint = ?
                                 THEN 'applied' ELSE 'pending' END,
                   lease_token = '', lease_expires_at = '',
                   compose_job_id = NULL, bound_fingerprint = '',
                   failure_count = 0, next_eligible_at = '', last_error = '',
                   updated_at = ? WHERE term_entity_id = ?""",
            (
                fingerprint,
                desired_revision,
                modes_json,
                fingerprint,
                now,
                term_entity_id,
            ),
        )
        return True

    def _mark_projection_repairs_for_entities(
        self,
        connection: sqlite3.Connection,
        entity_ids: Iterable[str],
        *,
        timestamp: str | None = None,
    ) -> int:
        identifiers = {
            str(item).strip() for item in entity_ids if str(item).strip()
        }
        state = getattr(self._write_context, "semantic", None)
        pending = getattr(state, "deferred_projection_entities", None)
        if pending is not None:
            # The membership transaction flushes once after all support changes.
            # Do not build snapshots of intermediate, soon-to-be-retired links.
            pending.update(identifiers)
            return 0
        if identifiers:
            placeholders = ",".join("?" for _ in identifiers)
            identifiers.update(
                str(row[0])
                for row in connection.execute(
                    f"""SELECT DISTINCT subject_entity_id FROM meanings
                        WHERE entity_id IN ({placeholders})""",
                    tuple(sorted(identifiers)),
                ).fetchall()
            )
        changed = 0
        for subject_id in sorted(
            self._projection_subject_ids_for_entities(connection, identifiers)
        ):
            changed += int(
                self._refresh_projection_repair_state(
                    connection, subject_id, timestamp=timestamp
                )
            )
        self._mark_semantic_enrichments_for_entities(
            connection, identifiers, timestamp=timestamp
        )
        return changed

    def _semantic_subject_ids_for_entities(
        self, connection: sqlite3.Connection, entity_ids: Iterable[str]
    ) -> set[str]:
        identifiers = {
            str(item).strip() for item in entity_ids if str(item).strip()
        }
        if not identifiers:
            return set()
        placeholders = ",".join("?" for _ in identifiers)
        identifiers.update(
            str(row[0])
            for row in connection.execute(
                f"""SELECT DISTINCT subject_entity_id FROM meanings
                    WHERE entity_id IN ({placeholders})""",
                tuple(sorted(identifiers)),
            ).fetchall()
        )
        placeholders = ",".join("?" for _ in identifiers)
        relations = tuple(sorted(_SEMANTIC_RELATIONS))
        relation_placeholders = ",".join("?" for _ in relations)
        ordered = tuple(sorted(identifiers))
        touching = connection.execute(
            f"""SELECT subject_entity_id, source_entity_id, target_entity_id
                FROM relation_assertions
                WHERE status = 'accepted'
                  AND relation IN ({relation_placeholders})
                  AND (subject_entity_id IN ({placeholders})
                       OR source_entity_id IN ({placeholders})
                       OR target_entity_id IN ({placeholders}))""",
            (*relations, *ordered, *ordered, *ordered),
        ).fetchall()
        for row in touching:
            identifiers.update(
                (
                    str(row["subject_entity_id"]),
                    str(row["source_entity_id"]),
                    str(row["target_entity_id"]),
                )
            )
        return identifiers

    def _semantic_enrichment_snapshot(
        self,
        connection: sqlite3.Connection,
        entity_id: str,
        language: str = "en",
    ) -> dict[str, Any] | None:
        from .semantic_contract import (
            MORPHOLOGY_MEANING_PROMPT_POLICY, SEMANTIC_BOOK_RETRIEVAL_POLICY,
        )
        from .semantic_retrieval import needs_book_retrieval

        language = _language(language)
        row = connection.execute(
            """SELECT entity.entity_id, entity.entity_type,
                      entity.canonical_key, entity.label, entity.status,
                      entity.quality_score, entity.payload,
                      term.language AS term_language, term.kind AS term_kind,
                      morpheme.language AS morpheme_language,
                      morpheme.kind AS morpheme_kind,
                      morpheme.form AS morpheme_form,
                      morpheme.meaning AS morpheme_meaning
               FROM entities AS entity
               LEFT JOIN terms AS term ON term.entity_id = entity.entity_id
               LEFT JOIN morphemes AS morpheme
                 ON morpheme.entity_id = entity.entity_id
               WHERE entity.entity_id = ? AND entity.status = 'accepted'""",
            (entity_id,),
        ).fetchone()
        if row is None or str(row["entity_type"]) not in _SEMANTIC_JOB_TYPES:
            return None
        entity_type = str(row["entity_type"])
        entity_kind = str(
            row["morpheme_kind"] if entity_type == "morpheme" else row["term_kind"]
        )
        if entity_type == "morpheme":
            eligible = entity_kind in {"root", "prefix", "suffix"}
            source_language = str(row["morpheme_language"] or "")
        else:
            source_language = str(row["term_language"] or "")
            relations = tuple(sorted(_SEMANTIC_RELATIONS))
            relation_placeholders = ",".join("?" for _ in relations)
            eligible = connection.execute(
                f"""SELECT 1 FROM relation_assertions AS assertion
                    JOIN entities AS target
                      ON target.entity_id = assertion.target_entity_id
                     AND target.status = 'accepted'
                    WHERE assertion.status = 'accepted'
                      AND assertion.relation IN ({relation_placeholders})
                      AND (assertion.subject_entity_id = ?
                           OR assertion.source_entity_id = ?
                           OR assertion.target_entity_id = ?)
                      AND (target.entity_type = 'morpheme'
                           OR EXISTS (
                               SELECT 1 FROM morphemes AS source_morpheme
                               WHERE source_morpheme.entity_id =
                                     assertion.source_entity_id
                           ))
                    LIMIT 1""",
                (*relations, entity_id, entity_id, entity_id),
            ).fetchone() is not None
        if not eligible:
            return None

        relations = tuple(sorted(_SEMANTIC_RELATIONS))
        relation_placeholders = ",".join("?" for _ in relations)
        assertions = connection.execute(
            f"""SELECT assertion_id, subject_entity_id, source_entity_id,
                       target_entity_id, relation, basis, confidence,
                       properties_json, revision
                FROM relation_assertions
                WHERE status = 'accepted'
                  AND relation IN ({relation_placeholders})
                  AND (subject_entity_id = ? OR source_entity_id = ?
                       OR target_entity_id = ?)
                ORDER BY assertion_id LIMIT 96""",
            (*relations, entity_id, entity_id, entity_id),
        ).fetchall()
        context_ids = {entity_id}
        assertion_values: list[dict[str, Any]] = []
        for assertion in assertions:
            context_ids.update(
                (
                    str(assertion["subject_entity_id"]),
                    str(assertion["source_entity_id"]),
                    str(assertion["target_entity_id"]),
                )
            )
            assertion_values.append(
                {
                    "id": str(assertion["assertion_id"]),
                    "subject": str(assertion["subject_entity_id"]),
                    "source": str(assertion["source_entity_id"]),
                    "target": str(assertion["target_entity_id"]),
                    "relation": str(assertion["relation"]),
                    "basis": str(assertion["basis"]),
                    "confidence": float(assertion["confidence"]),
                    "properties": self._canonical_fingerprint_value(
                        assertion["properties_json"]
                    ),
                    "revision": int(assertion["revision"]),
                    "evidence_ids": [
                        str(item[0])
                        for item in connection.execute(
                            """SELECT evidence_id FROM assertion_evidence
                               WHERE assertion_id = ? ORDER BY evidence_id""",
                            (str(assertion["assertion_id"]),),
                        ).fetchall()
                    ],
                }
            )
        context_identifiers = tuple(sorted(context_ids))
        context_placeholders = ",".join("?" for _ in context_identifiers)
        context_entities: list[dict[str, Any]] = []
        for entity in connection.execute(
            f"""SELECT entity_id, entity_type, canonical_key, label,
                       status, quality_score, payload
                FROM entities WHERE entity_id IN ({context_placeholders})
                ORDER BY entity_id""",
            context_identifiers,
        ):
            payload = _json_object(str(entity["payload"]))
            if entity_type == "morpheme" or str(entity["entity_id"]) == entity_id:
                for output_key in ("meaning", "gloss", "definition"):
                    payload.pop(output_key, None)
            context_entities.append(
                {
                    "id": str(entity["entity_id"]),
                    "type": str(entity["entity_type"]),
                    "key": str(entity["canonical_key"]),
                    "label": str(entity["label"]),
                    "status": str(entity["status"]),
                    "quality": entity["quality_score"],
                    "payload": payload,
                }
            )
        if entity_type == "morpheme":
            # Link annotations are not model input. Select each corpus record
            # once before bounding context, preferring ownership by the target.
            evidence = [
                dict(item)
                for item in connection.execute(
                    f"""WITH source_owners AS (
                            SELECT link.evidence_id,
                                   COALESCE(
                                       MAX(CASE WHEN link.entity_id = ?
                                                THEN link.entity_id END),
                                       MIN(link.entity_id)
                                   ) AS entity_id
                            FROM entity_evidence AS link
                            WHERE link.entity_id IN ({context_placeholders})
                            GROUP BY link.evidence_id
                        )
                        SELECT owner.entity_id, evidence.evidence_id,
                               evidence.corpus_id, evidence.source_entry_id,
                               evidence.source_hash, evidence.locator,
                               evidence.excerpt
                        FROM source_owners AS owner
                        JOIN evidence_records AS evidence
                          ON evidence.evidence_id = owner.evidence_id
                        ORDER BY CASE WHEN owner.entity_id = ? THEN 0 ELSE 1 END,
                                 owner.entity_id, evidence.evidence_id
                        LIMIT 192""",
                    (entity_id, *context_identifiers, entity_id),
                )
            ]
        else:
            evidence = [
                {
                    "entity_id": str(item["entity_id"]),
                    "evidence_id": str(item["evidence_id"]),
                    "claim": str(item["claim"]),
                    "confidence": float(item["confidence"]),
                    "corpus_id": str(item["corpus_id"]),
                    "source_entry_id": str(item["source_entry_id"]),
                    "source_hash": str(item["source_hash"]),
                    "locator": str(item["locator"]),
                    "excerpt": str(item["excerpt"]),
                }
                for item in connection.execute(
                    f"""SELECT link.entity_id, link.evidence_id, link.claim,
                               link.confidence, evidence.corpus_id,
                               evidence.source_entry_id, evidence.source_hash,
                               evidence.locator, evidence.excerpt
                        FROM entity_evidence AS link
                        JOIN evidence_records AS evidence
                          ON evidence.evidence_id = link.evidence_id
                        WHERE link.entity_id IN ({context_placeholders})
                        ORDER BY link.entity_id, link.evidence_id, link.claim
                        LIMIT 192""",
                    context_identifiers,
                )
            ]
        fingerprint_payload = {
            "entity_id": entity_id,
            "language": language,
            "entity_type": entity_type,
            "source_language": source_language,
            "entity_kind": entity_kind,
            "assertions": assertion_values,
            "entities": (
                [{key: value for key, value in item.items() if key != "quality"}
                 for item in context_entities]
                if entity_type == "morpheme" else context_entities
            ),
            "evidence": evidence,
        }
        if entity_type == "morpheme":
            fingerprint_payload["meaning_policy"] = MORPHOLOGY_MEANING_PROMPT_POLICY
            fingerprint_payload["meaning_review_contract"] = 1
            if needs_book_retrieval(evidence):
                fingerprint_payload["book_retrieval_policy"] = SEMANTIC_BOOK_RETRIEVAL_POLICY
        desired_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        accepted = self._accepted_meaning_records(connection, (entity_id,)).get(
            entity_id, []
        )
        if entity_type == "morpheme":
            accepted = [item for item in accepted if str(item["language"]) == language]
        model_meanings = [
            item for item in accepted
            if entity_type == "morpheme" and str(item.get("basis", "")) == "model"
        ]
        current_meaning_jobs: dict[str, str] = {}
        if model_meanings:
            accepted_ids = {str(item["meaning_id"]) for item in model_meanings}
            for artifact in connection.execute(
                """SELECT artifact.payload, job.job_id
                   FROM preparation_jobs AS job
                   JOIN job_artifacts AS artifact ON artifact.job_id = job.job_id
                   WHERE job.subject_entity_id = ? AND job.language = ?
                     AND job.job_type = 'prepare-entity-meaning'
                     AND job.source_fingerprint = ?
                     AND artifact.stage = 'accepted-entity-meaning'
                     AND artifact.validation_state = 'accepted'
                   ORDER BY artifact.created_at DESC LIMIT 8""",
                (entity_id, language, desired_fingerprint),
            ):
                payload = _json_object(str(artifact["payload"]))
                meaning_id = str(payload.get("meaning_id", ""))
                if (
                    meaning_id in accepted_ids
                    and str(payload.get("entity_id", "")) == entity_id
                    and str(payload.get("language", "")) == language
                    and str(payload.get("source_fingerprint", "")) == desired_fingerprint
                ):
                    current_meaning_jobs[meaning_id] = str(artifact["job_id"])
        needs_review = bool(model_meanings and not current_meaning_jobs)
        return {
            "entity_id": entity_id,
            "language": language,
            "entity_type": entity_type,
            "source_language": source_language,
            "entity_kind": entity_kind,
            "job_type": _SEMANTIC_JOB_TYPES[entity_type],
            "desired_fingerprint": desired_fingerprint,
            "has_accepted_meaning": bool(accepted),
            "has_current_meaning": bool(accepted) and not needs_review,
            "needs_semantic_review": needs_review,
            "current_meaning_jobs": current_meaning_jobs,
            "accepted_meanings": accepted,
            "context": fingerprint_payload,
        }

    def _refresh_semantic_enrichment_state(
        self,
        connection: sqlite3.Connection,
        entity_id: str,
        *,
        language: str = "en",
        timestamp: str | None = None,
    ) -> bool:
        snapshot = self._semantic_enrichment_snapshot(connection, entity_id, language)
        if snapshot is None:
            return False
        now = timestamp or _now()
        desired = str(snapshot["desired_fingerprint"])
        current = bool(snapshot["has_current_meaning"])
        has_meaning = bool(snapshot["has_accepted_meaning"])
        existing = connection.execute(
            """SELECT * FROM lexical_semantic_enrichments
               WHERE entity_id = ? AND language = ?""",
            (entity_id, language),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO lexical_semantic_enrichments(
                       entity_id, language, entity_type, source_language,
                       entity_kind, job_type, desired_fingerprint,
                       applied_fingerprint, status, last_applied_at,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity_id, language, snapshot["entity_type"],
                    snapshot["source_language"], snapshot["entity_kind"],
                    snapshot["job_type"], desired,
                    desired if current else "",
                    "applied" if current else "pending",
                    now if current else "", now, now,
                ),
            )
            return True
        recorded = str(existing["applied_fingerprint"])
        if not current and recorded == desired:
            # An old existence-only receipt is not evidence of inference.
            recorded = ""
        fingerprint_changed = str(existing["desired_fingerprint"]) != desired
        identity_changed = any(
            str(existing[field]) != str(snapshot[field])
            for field in ("entity_type", "source_language", "entity_kind", "job_type")
        )
        if fingerprint_changed or identity_changed:
            connection.execute(
                """UPDATE lexical_semantic_enrichments
                   SET entity_type = ?, source_language = ?, entity_kind = ?,
                       job_type = ?, desired_fingerprint = ?,
                       applied_fingerprint = ?, status = ?, lease_token = '',
                       lease_expires_at = '', enrichment_job_id = NULL,
                       bound_fingerprint = '', failure_count = 0,
                       next_eligible_at = ?, last_error = '',
                       last_applied_at = ?, updated_at = ?
                   WHERE entity_id = ? AND language = ?""",
                (
                    snapshot["entity_type"], snapshot["source_language"],
                    snapshot["entity_kind"], snapshot["job_type"], desired,
                    desired if current else recorded,
                    "applied" if current else "pending",
                    str(existing["next_eligible_at"]) if has_meaning and not current else "",
                    now if current else str(existing["last_applied_at"]),
                    now, entity_id, language,
                ),
            )
            return True
        if current and (
            str(existing["status"]) != "applied"
            or str(existing["applied_fingerprint"]) != desired
        ):
            connection.execute(
                """UPDATE lexical_semantic_enrichments
                   SET status = 'applied', applied_fingerprint = ?,
                       lease_token = '', lease_expires_at = '',
                       bound_fingerprint = '', failure_count = 0,
                       next_eligible_at = '', last_error = '',
                       last_applied_at = ?, updated_at = ?
                   WHERE entity_id = ? AND language = ?""",
                (desired, now, now, entity_id, language),
            )
            return True
        if not current and str(existing["status"]) == "applied":
            connection.execute(
                """UPDATE lexical_semantic_enrichments
                   SET status = 'pending', applied_fingerprint = ?,
                       enrichment_job_id = NULL, bound_fingerprint = '',
                       last_error = '', updated_at = ?
                   WHERE entity_id = ? AND language = ?""",
                (recorded, now, entity_id, language),
            )
            return True
        return False

    def _mark_semantic_enrichments_for_entities(
        self,
        connection: sqlite3.Connection,
        entity_ids: Iterable[str],
        *,
        timestamp: str | None = None,
    ) -> int:
        available = connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type = 'table'
               AND name = 'lexical_semantic_enrichments'"""
        ).fetchone()
        if available is None:
            return 0
        changed = 0
        for entity_id in sorted(
            self._semantic_subject_ids_for_entities(connection, entity_ids)
        ):
            changed += int(
                self._refresh_semantic_enrichment_state(
                    connection, entity_id, timestamp=timestamp
                )
            )
        return changed

    def _bootstrap_lexical_semantic_enrichments_connection(
        self, connection: sqlite3.Connection, *, limit: int
    ) -> dict[str, Any]:
        from .semantic_contract import (
            MORPHOLOGY_MEANING_PROMPT_POLICY, SEMANTIC_BOOK_RETRIEVAL_POLICY,
        )

        limit = max(1, min(int(limit), 256))
        timestamp = _now()
        graph_row = connection.execute(
            """SELECT value FROM schema_meta
               WHERE key = 'relation_graph_revision'"""
        ).fetchone()
        graph_revision = int(graph_row[0]) if graph_row else 0
        connection.execute(
            """INSERT OR IGNORE INTO lexical_semantic_bootstrap_state(
                   scheduler_key, observed_graph_revision, updated_at
               ) VALUES ('semantic-v12', ?, ?)""",
            (graph_revision, timestamp),
        )
        policy_key = "semantic_meaning_bootstrap_policy_v1"
        policy = (
            "review-v1:" + MORPHOLOGY_MEANING_PROMPT_POLICY
            + ";book-retrieval:" + SEMANTIC_BOOK_RETRIEVAL_POLICY
        )
        prior_policy = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (policy_key,)
        ).fetchone()
        if prior_policy is None or str(prior_policy[0]) != policy:
            connection.execute(
                """UPDATE lexical_semantic_bootstrap_state
                   SET cursor_entity_id = '', scanned_count = 0, complete = 0,
                       observed_graph_revision = ?, updated_at = ?
                   WHERE scheduler_key = 'semantic-v12'""",
                (graph_revision, timestamp),
            )
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (policy_key, policy),
            )
        state = connection.execute(
            """SELECT cursor_entity_id, scanned_count, complete,
                      observed_graph_revision
               FROM lexical_semantic_bootstrap_state
               WHERE scheduler_key = 'semantic-v12'"""
        ).fetchone()
        if (
            bool(state["complete"])
            and int(state["observed_graph_revision"]) == graph_revision
        ):
            return {
                "scanned": 0,
                "marked": 0,
                "scanned_total": int(state["scanned_count"]),
                "complete": True,
                "cursor_entity_id": str(state["cursor_entity_id"]),
                "observed_graph_revision": graph_revision,
            }
        if bool(state["complete"]):
            connection.execute(
                """UPDATE lexical_semantic_bootstrap_state
                   SET cursor_entity_id = '', complete = 0,
                       observed_graph_revision = ?, updated_at = ?
                   WHERE scheduler_key = 'semantic-v12'""",
                (graph_revision, timestamp),
            )
            cursor = ""
        else:
            cursor = str(state["cursor_entity_id"])
        rows = connection.execute(
            """SELECT entity.entity_id
               FROM entities AS entity
               WHERE entity.status = 'accepted' AND entity.entity_id > ?
                 AND (
                     EXISTS (
                         SELECT 1 FROM morphemes AS morpheme
                         WHERE morpheme.entity_id = entity.entity_id
                           AND morpheme.kind IN ('root', 'prefix', 'suffix')
                     )
                     OR EXISTS (
                         SELECT 1 FROM terms AS term
                         WHERE term.entity_id = entity.entity_id
                           AND term.kind = 'word'
                           AND EXISTS (
                               SELECT 1 FROM relation_assertions AS assertion
                               WHERE assertion.status = 'accepted'
                                 AND assertion.relation IN (
                                     'has-component', 'shares-component'
                                 )
                                 AND (assertion.subject_entity_id = entity.entity_id
                                      OR assertion.source_entity_id = entity.entity_id
                                      OR assertion.target_entity_id = entity.entity_id)
                           )
                     )
                 )
               ORDER BY entity.entity_id LIMIT ?""",
            (cursor, limit),
        ).fetchall()
        marked = 0
        for row in rows:
            marked += int(
                self._refresh_semantic_enrichment_state(
                    connection, str(row["entity_id"]), timestamp=timestamp
                )
            )
        complete = len(rows) < limit
        next_cursor = str(rows[-1]["entity_id"]) if rows else cursor
        connection.execute(
            """UPDATE lexical_semantic_bootstrap_state
               SET cursor_entity_id = ?,
                   scanned_count = scanned_count + ?, complete = ?,
                   observed_graph_revision = ?, updated_at = ?
               WHERE scheduler_key = 'semantic-v12'""",
            (
                next_cursor,
                len(rows),
                int(complete),
                graph_revision,
                timestamp,
            ),
        )
        return {
            "scanned": len(rows),
            "marked": marked,
            "scanned_total": int(state["scanned_count"]) + len(rows),
            "complete": complete,
            "cursor_entity_id": next_cursor,
            "observed_graph_revision": graph_revision,
        }

    def _bootstrap_lexical_projection_repairs_connection(
        self, connection: sqlite3.Connection, *, limit: int
    ) -> dict[str, Any]:
        """Advance one durable bounded page over legacy lexical subjects."""

        limit = max(1, min(int(limit), 256))
        timestamp = _now()
        scheduler_key = _projection_bootstrap_scheduler_key()
        connection.execute(
            """INSERT OR IGNORE INTO lexical_projection_bootstrap_state(
                   scheduler_key, updated_at
               ) VALUES (?, ?)""",
            (scheduler_key, timestamp),
        )
        state = connection.execute(
            """SELECT cursor_entity_id, scanned_count, complete
               FROM lexical_projection_bootstrap_state
               WHERE scheduler_key = ?""",
            (scheduler_key,),
        ).fetchone()
        if bool(state["complete"]):
            return {
                "scanned": 0,
                "marked": 0,
                "scanned_total": int(state["scanned_count"]),
                "complete": True,
                "cursor_entity_id": str(state["cursor_entity_id"]),
            }
        cursor = str(state["cursor_entity_id"])
        rows = connection.execute(
            f"""SELECT term.entity_id
                FROM terms AS term
                JOIN entities AS term_entity
                  ON term_entity.entity_id = term.entity_id
                 AND term_entity.status = 'accepted'
                WHERE term.kind = 'word' AND term.entity_id > ?
                  AND (
                      EXISTS (
                          SELECT 1 FROM relation_assertions AS assertion
                          JOIN morphemes AS morpheme
                            ON morpheme.entity_id = assertion.target_entity_id
                           AND morpheme.kind IN ('root', 'prefix', 'suffix')
                          JOIN entities AS morpheme_entity
                            ON morpheme_entity.entity_id = morpheme.entity_id
                           AND morpheme_entity.status = 'accepted'
                          WHERE assertion.subject_entity_id = term.entity_id
                            AND {LIVE_SOURCE_COMPONENT_SQL}
                      )
                      OR EXISTS (
                          SELECT 1 FROM relation_assertions AS assertion
                          WHERE assertion.subject_entity_id = term.entity_id
                            AND {_LIVE_SOURCE_WORD_HISTORY_SQL}
                      )
                      OR EXISTS (
                          SELECT 1 FROM lexical_projection_repairs AS existing
                          WHERE existing.term_entity_id = term.entity_id
                      )
                  )
                ORDER BY term.entity_id LIMIT ?""",
            (cursor, limit),
        ).fetchall()
        marked = sum(
            int(
                self._refresh_projection_repair_state(
                    connection, str(row["entity_id"]), timestamp=timestamp
                )
            )
            for row in rows
        )
        complete = len(rows) < limit
        next_cursor = str(rows[-1]["entity_id"]) if rows else cursor
        scanned_total = int(state["scanned_count"]) + len(rows)
        connection.execute(
            """UPDATE lexical_projection_bootstrap_state
               SET cursor_entity_id = ?, scanned_count = ?, complete = ?,
                   updated_at = ? WHERE scheduler_key = ?""",
            (
                next_cursor,
                scanned_total,
                int(complete),
                timestamp,
                scheduler_key,
            ),
        )
        return {
            "scanned": len(rows),
            "marked": marked,
            "scanned_total": scanned_total,
            "complete": complete,
            "cursor_entity_id": next_cursor,
        }

    @classmethod
    def _relation_authority_rank(
        cls, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> tuple[object, ...]:
        assertion_id = str(row["assertion_id"])
        properties = cls._relation_assertion_properties(row["properties_json"])
        exact_ids = cls._relation_assertion_evidence_ids(connection, assertion_id)
        context_ids = cls._existing_relation_evidence_ids(
            connection, cls._relation_context_evidence_ids(properties)
        )
        basis = str(row["basis"])
        return (
            cls._relation_claim_strength(basis, exact_ids, context_ids),
            float(row["confidence"]),
            {"reviewed": 4, "book": 3, "derived": 2, "model": 1}.get(
                basis, 0
            ),
            int(row["revision"]),
            str(row["updated_at"]),
            assertion_id,
        )

    def _sync_component_slot_authority(
        self,
        connection: sqlite3.Connection,
        slot_key: str,
        *,
        timestamp: str,
    ) -> bool:
        rows = connection.execute(
            """SELECT assertion.assertion_id,
                      assertion.subject_entity_id,
                      assertion.source_entity_id,
                      assertion.target_entity_id, assertion.relation,
                      assertion.basis, assertion.confidence, assertion.status,
                      assertion.properties_json, assertion.revision,
                      assertion.created_at, assertion.updated_at
               FROM relation_assertion_slots AS slot
               JOIN relation_assertions AS assertion
                 ON assertion.assertion_id = slot.assertion_id
               WHERE slot.slot_key = ?
                 AND slot.slot_mode IN ('exclusive', 'set-member')
                 AND assertion.relation = 'has-component'
               ORDER BY assertion.assertion_id""",
            (slot_key,),
        ).fetchall()
        if not rows:
            return False
        primary_rows = []
        for row in rows:
            properties = self._relation_assertion_properties(
                row["properties_json"]
            )
            branch = self._relation_slot_text(
                properties,
                "branch_id",
                "decomposition_branch",
                "analysis_branch",
                "branch",
            ) or "primary"
            ordinal = self._relation_slot_ordinal(properties.get("ordinal"))
            if branch == "primary" and ordinal is not None:
                primary_rows.append((row, properties, ordinal))
        if not primary_rows:
            return False
        accepted = [item for item in primary_rows if item[0]["status"] == "accepted"]
        reference_row, _, ordinal = primary_rows[0]
        term_id = str(reference_row["subject_entity_id"])
        if accepted:
            winner, properties, ordinal = max(
                accepted,
                key=lambda item: self._relation_authority_rank(
                    connection, item[0]
                ),
            )
            term_id = str(winner["subject_entity_id"])
            morpheme_id = str(winner["target_entity_id"])
            surface = str(properties.get("surface", "") or "").strip()
            if not surface:
                form = connection.execute(
                    "SELECT form FROM morphemes WHERE entity_id = ?",
                    (morpheme_id,),
                ).fetchone()
                surface = str(form["form"]) if form is not None else ""
            current = connection.execute(
                """SELECT morpheme_id, surface, confidence, basis
                   FROM term_morphemes WHERE term_id = ? AND ordinal = ?""",
                (term_id, ordinal),
            ).fetchone()
            desired = (
                morpheme_id,
                surface,
                float(winner["confidence"]),
                str(winner["basis"]),
            )
            if current is not None and (
                str(current["morpheme_id"]),
                str(current["surface"]),
                float(current["confidence"]),
                str(current["basis"]),
            ) == desired:
                return False
            connection.execute(
                """INSERT INTO term_morphemes(
                       term_id, morpheme_id, ordinal, surface,
                       confidence, basis
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(term_id, ordinal) DO UPDATE SET
                       morpheme_id = excluded.morpheme_id,
                       surface = excluded.surface,
                       confidence = excluded.confidence,
                       basis = excluded.basis""",
                (term_id, morpheme_id, ordinal, surface, desired[2], desired[3]),
            )
            return True
        cursor = connection.execute(
            "DELETE FROM term_morphemes WHERE term_id = ? AND ordinal = ?",
            (term_id, ordinal),
        )
        return bool(cursor.rowcount)

    def _sync_relation_authority(
        self,
        connection: sqlite3.Connection,
        triples: Iterable[tuple[str, str, str]],
        *,
        component_slot_keys: Iterable[str] = (),
        timestamp: str | None = None,
    ) -> bool:
        """Mirror accepted, slotted assertions without absorbing legacy edges."""

        changed = False
        now = timestamp or _now()
        slots = {str(value) for value in component_slot_keys if str(value)}
        for source_id, target_id, relation in sorted(set(triples)):
            rows = connection.execute(
                """SELECT assertion.assertion_id,
                          assertion.subject_entity_id,
                          assertion.source_entity_id,
                          assertion.target_entity_id, assertion.relation,
                          assertion.basis, assertion.confidence,
                          assertion.properties_json, assertion.revision,
                          assertion.created_at, assertion.updated_at
                   FROM relation_assertions AS assertion
                   JOIN relation_assertion_slots AS slot
                     ON slot.assertion_id = assertion.assertion_id
                   WHERE assertion.source_entity_id = ?
                     AND assertion.target_entity_id = ?
                     AND assertion.relation = ?
                     AND assertion.status = 'accepted'
                   ORDER BY assertion.assertion_id""",
                (source_id, target_id, relation),
            ).fetchall()
            slots.update(
                str(row["slot_key"])
                for row in connection.execute(
                    """SELECT slot.slot_key
                       FROM relation_assertion_slots AS slot
                       JOIN relation_assertions AS assertion
                         ON assertion.assertion_id = slot.assertion_id
                       WHERE assertion.source_entity_id = ?
                         AND assertion.target_entity_id = ?
                         AND assertion.relation = ?
                         AND slot.slot_mode = 'exclusive'""",
                    (source_id, target_id, relation),
                )
            )
            edge = connection.execute(
                """SELECT edge_id, basis, confidence, status, properties
                   FROM entity_edges
                   WHERE source_entity_id = ? AND target_entity_id = ?
                     AND relation = ?""",
                (source_id, target_id, relation),
            ).fetchone()
            if rows:
                winner = max(
                    rows,
                    key=lambda row: self._relation_authority_rank(
                        connection, row
                    ),
                )
                properties_json = self._canonical_relation_json(
                    self._relation_assertion_properties(
                        winner["properties_json"]
                    )
                )
                desired = (
                    str(winner["basis"]),
                    float(winner["confidence"]),
                    "accepted",
                    properties_json,
                )
                if edge is None:
                    edge_id = _identifier(
                        "edge", f"{source_id}:{relation}:{target_id}"
                    )
                    connection.execute(
                        """INSERT INTO entity_edges(
                               edge_id, source_entity_id, target_entity_id,
                               relation, basis, confidence, status, properties,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)""",
                        (
                            edge_id,
                            source_id,
                            target_id,
                            relation,
                            desired[0],
                            desired[1],
                            desired[3],
                            now,
                            now,
                        ),
                    )
                    changed = True
                else:
                    try:
                        existing_properties = self._canonical_relation_json(
                            json.loads(edge["properties"])
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_properties = str(edge["properties"])
                    existing = (
                        str(edge["basis"]),
                        float(edge["confidence"]),
                        str(edge["status"]),
                        existing_properties,
                    )
                    if existing != desired:
                        connection.execute(
                            """UPDATE entity_edges
                               SET basis = ?, confidence = ?, status = 'accepted',
                                   properties = ?, updated_at = ?
                               WHERE edge_id = ?""",
                            (
                                desired[0],
                                desired[1],
                                desired[3],
                                now,
                                edge["edge_id"],
                            ),
                        )
                        changed = True
            elif edge is not None and str(edge["status"]) == "accepted":
                connection.execute(
                    """UPDATE entity_edges
                       SET status = 'archived', updated_at = ?
                       WHERE edge_id = ?""",
                    (now, edge["edge_id"]),
                )
                changed = True
        for slot_key in sorted(slots):
            changed = self._sync_component_slot_authority(
                connection, slot_key, timestamp=now
            ) or changed
        return changed

    def _reconcile_relation_authority_v10(self) -> None:
        """Reconcile exclusive claims and their assertion-controlled mirrors."""

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                timestamp = _now()
                collision_changed, collision_triples, component_slots = (
                    self._reconcile_exclusive_relation_slot_collisions_v10(
                        connection, timestamp=timestamp
                    )
                )
                triples = {
                    (
                        str(row["source_entity_id"]),
                        str(row["target_entity_id"]),
                        str(row["relation"]),
                    )
                    for row in connection.execute(
                        """SELECT DISTINCT source_entity_id,
                                  target_entity_id, relation
                           FROM relation_assertions"""
                    )
                }
                triples.update(collision_triples)
                authority_changed = self._sync_relation_authority(
                    connection,
                    triples,
                    component_slot_keys=component_slots,
                    timestamp=timestamp,
                )
                if collision_changed or authority_changed:
                    self._advance_relation_graph_revision(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _reconcile_exclusive_relation_slot_collisions_v10(
        self,
        connection: sqlite3.Connection,
        *,
        timestamp: str,
    ) -> tuple[bool, set[tuple[str, str, str]], set[str]]:
        """Resolve legacy exclusive-slot collisions without deleting knowledge.

        Only model/derived claims are safe to rank automatically. Conflicting
        source-backed claims are retained as accepted, explicitly governed as
        a non-exclusive set, and recorded as a conflict observation.
        """

        groups = connection.execute(
            """SELECT slot.slot_key
               FROM relation_assertion_slots AS slot
               JOIN relation_assertions AS assertion
                 ON assertion.assertion_id = slot.assertion_id
               WHERE slot.slot_mode = 'exclusive'
                 AND assertion.status = 'accepted'
               GROUP BY slot.slot_key
               HAVING COUNT(*) > 1
               ORDER BY slot.slot_key"""
        ).fetchall()
        changed = False
        triples: set[tuple[str, str, str]] = set()
        component_slots: set[str] = set()
        for group in groups:
            slot_key = str(group["slot_key"])
            rows = connection.execute(
                """SELECT assertion.assertion_id,
                          assertion.subject_entity_id,
                          assertion.source_entity_id,
                          assertion.target_entity_id, assertion.relation,
                          assertion.basis, assertion.confidence,
                          assertion.status, assertion.properties_json,
                          assertion.revision, assertion.created_at,
                          assertion.updated_at
                   FROM relation_assertion_slots AS slot
                   JOIN relation_assertions AS assertion
                     ON assertion.assertion_id = slot.assertion_id
                   WHERE slot.slot_key = ?
                     AND slot.slot_mode = 'exclusive'
                     AND assertion.status = 'accepted'
                   ORDER BY assertion.assertion_id""",
                (slot_key,),
            ).fetchall()
            if len(rows) < 2:
                continue
            triples.update(
                (
                    str(row["source_entity_id"]),
                    str(row["target_entity_id"]),
                    str(row["relation"]),
                )
                for row in rows
            )
            if self._relation_slot_family(slot_key) == "component":
                component_slots.add(slot_key)
            protected = [
                row
                for row in rows
                if str(row["basis"]).casefold() not in {"model", "derived"}
            ]
            if protected:
                assertion_ids = [str(row["assertion_id"]) for row in rows]
                placeholders = ",".join("?" for _ in assertion_ids)
                cursor = connection.execute(
                    f"""UPDATE relation_assertion_slots
                        SET slot_mode = 'set-member'
                        WHERE assertion_id IN ({placeholders})
                          AND slot_mode = 'exclusive'""",
                    assertion_ids,
                )
                changed = bool(cursor.rowcount) or changed
                proposed = min(rows, key=lambda row: str(row["assertion_id"]))
                self._record_relation_slot_reconciliation_observation(
                    connection,
                    slot_key,
                    rows,
                    proposed,
                    decision="conflict",
                    reason=(
                        "accepted source-backed claims disagree; all claims retained "
                        "as an explicit non-exclusive conflict"
                    ),
                )
                continue

            ordered = sorted(
                rows,
                key=lambda row: self._replaceable_relation_collision_sort_key(
                    connection, slot_key, row
                ),
            )
            winner = ordered[0]
            losers = ordered[1:]
            loser_ids = [str(row["assertion_id"]) for row in losers]
            placeholders = ",".join("?" for _ in loser_ids)
            cursor = connection.execute(
                f"""UPDATE relation_assertions
                    SET status = 'archived', revision = revision + 1,
                        updated_at = ?
                    WHERE assertion_id IN ({placeholders})
                      AND status = 'accepted'""",
                (timestamp, *loser_ids),
            )
            changed = bool(cursor.rowcount) or changed
            self._record_relation_slot_reconciliation_observation(
                connection,
                slot_key,
                rows,
                winner,
                decision="supersede",
                reason=(
                    "legacy model-only exclusive collision reconciled by relation "
                    "structure, evidence provenance, confidence, stable age, and ID"
                ),
            )
        return changed, triples, component_slots

    def _record_relation_slot_reconciliation_observation(
        self,
        connection: sqlite3.Connection,
        slot_key: str,
        rows: Sequence[sqlite3.Row],
        proposed: sqlite3.Row,
        *,
        decision: str,
        reason: str,
    ) -> tuple[str, bool]:
        assertion_id = str(proposed["assertion_id"])
        exact_ids = self._relation_assertion_evidence_ids(connection, assertion_id)
        properties = self._relation_assertion_properties(
            proposed["properties_json"]
        )
        context_ids = self._relation_context_evidence_ids(properties)
        incumbent_ids = sorted(
            str(row["assertion_id"])
            for row in rows
            if str(row["assertion_id"]) != assertion_id
        )
        fingerprint_source = self._canonical_relation_json(
            {
                "slot_key": slot_key,
                "assertion_ids": sorted(str(row["assertion_id"]) for row in rows),
                "proposed_assertion_id": assertion_id,
                "decision": decision,
                "policy": "relation-slot-reconciliation-v1",
            }
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        proposal = {
            **self._relation_assertion_snapshot(
                proposed, exact_ids, context_ids
            ),
            "slot_key": slot_key,
            "slot_mode": "exclusive",
            "slot_policy_version": "relation-slot-v2",
            "input_fingerprint": fingerprint,
            "output_fingerprint": fingerprint,
            "epoch_fingerprint": "",
            "prompt_version": "",
            "job_id": None,
            "validator_version": "relation-slot-reconciliation-v1",
        }
        synthetic = {
            "decision_id": _identifier(
                "relation-decision", f"startup-slot-reconciliation:{fingerprint}"
            ),
            "proposal": proposal,
            "slot_decision": decision,
            "slot_incumbent_ids": incumbent_ids,
            "reason": reason,
        }
        return self._insert_relation_claim_observation(connection, synthetic)

    @staticmethod
    def _canonical_relation_json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _relation_assertion_properties(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return json.loads(KnowledgeStore._canonical_relation_json(value))
        try:
            decoded = json.loads(str(value or "{}"))
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _relation_context_evidence_ids(properties: dict[str, object]) -> list[str]:
        values = properties.get("context_evidence_ids", [])
        if not isinstance(values, (list, tuple, set)):
            return []
        return sorted({str(value).strip() for value in values if str(value).strip()})

    @staticmethod
    def _relation_assertion_evidence_ids(
        connection: sqlite3.Connection, assertion_id: str
    ) -> list[str]:
        return [
            str(row["evidence_id"])
            for row in connection.execute(
                """SELECT evidence_id FROM assertion_evidence
                   WHERE assertion_id = ? ORDER BY evidence_id""",
                (assertion_id,),
            )
        ]

    @staticmethod
    def _existing_relation_evidence_ids(
        connection: sqlite3.Connection, evidence_ids
    ) -> list[str]:
        requested = sorted({str(value).strip() for value in evidence_ids if str(value).strip()})
        if not requested:
            return []
        placeholders = ",".join("?" for _ in requested)
        return [
            str(row["evidence_id"])
            for row in connection.execute(
                f"SELECT evidence_id FROM evidence_records WHERE evidence_id IN ({placeholders}) ORDER BY evidence_id",
                requested,
            )
        ]

    @staticmethod
    def _relation_assertion_snapshot(
        row: sqlite3.Row, evidence_ids: list[str], context_evidence_ids: list[str]
    ) -> dict[str, object]:
        return {
            "assertion_id": str(row["assertion_id"]),
            "subject_entity_id": str(row["subject_entity_id"]),
            "source_entity_id": str(row["source_entity_id"]),
            "target_entity_id": str(row["target_entity_id"]),
            "relation": str(row["relation"]),
            "basis": str(row["basis"]),
            "confidence": float(row["confidence"]),
            "status": str(row["status"]),
            "properties": KnowledgeStore._relation_assertion_properties(row["properties_json"]),
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "evidence_ids": list(evidence_ids),
            "context_evidence_ids": list(context_evidence_ids),
        }

    @staticmethod
    def _relation_claim_strength(
        basis: str, evidence_ids: list[str], context_evidence_ids: list[str]
    ) -> int:
        if basis == "reviewed" and evidence_ids:
            return 4
        if basis == "book" and evidence_ids:
            return 3
        if basis in {"model", "derived"} and context_evidence_ids:
            return 2
        return 1

    @staticmethod
    def _merge_relation_properties(
        incumbent: dict[str, object], proposal: dict[str, object]
    ) -> dict[str, object]:
        merged = KnowledgeStore._relation_assertion_properties(incumbent)
        merged.update(KnowledgeStore._relation_assertion_properties(proposal))
        warnings: list[str] = []
        for source in (incumbent.get("warnings", []), proposal.get("warnings", [])):
            if isinstance(source, str):
                source = [source]
            if isinstance(source, (list, tuple, set)):
                warnings.extend(str(item).strip() for item in source if str(item).strip())
        if warnings:
            merged["warnings"] = sorted(set(warnings))
        return merged

    @classmethod
    def _relation_slot_key(cls, family: str, values: dict[str, object]) -> str:
        digest = hashlib.sha256(
            cls._canonical_relation_json(
                {"family": family, "values": values}
            ).encode("utf-8")
        ).hexdigest()
        return f"relation-slot:{family}:{digest}"

    @staticmethod
    def _relation_slot_ordinal(value: object) -> int | None:
        if isinstance(value, bool) or isinstance(value, float):
            return None
        try:
            ordinal = int(value)
        except (TypeError, ValueError):
            return None
        return ordinal if ordinal >= 0 else None

    @staticmethod
    def _relation_slot_text(
        properties: dict[str, object], *names: str
    ) -> str:
        for name in names:
            value = properties.get(name)
            if isinstance(value, bool) or value is None:
                continue
            text = str(value).strip().casefold()
            if text:
                return text
        return ""

    @staticmethod
    def _relation_entity_language(
        connection: sqlite3.Connection, entity_id: str, entity_type: str
    ) -> str:
        subtype = {
            "term": ("terms", "language"),
            "morpheme": ("morphemes", "language"),
            "meaning": ("meanings", "language"),
            "translation": ("translations", "target_language"),
            "historical-form": ("historical_forms", "language"),
        }.get(entity_type)
        if subtype is None:
            return ""
        table, column = subtype
        row = connection.execute(
            f"SELECT {column} AS language FROM {table} WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        return str(row["language"]).strip().casefold() if row is not None else ""

    def _relation_slot_projection(
        self,
        connection: sqlite3.Connection,
        subject_entity_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        properties: dict[str, object],
    ) -> dict[str, str]:
        """Derive a conservative semantic slot from canonical store state."""

        policy_version = "relation-slot-v2"
        entity_ids = sorted(
            {subject_entity_id, source_entity_id, target_entity_id}
        )
        placeholders = ",".join("?" for _ in entity_ids)
        entity_types = {
            str(row["entity_id"]): str(row["entity_type"])
            for row in connection.execute(
                f"SELECT entity_id, entity_type FROM entities "
                f"WHERE entity_id IN ({placeholders})",
                entity_ids,
            )
        }
        normalized_relation = relation.strip().casefold()

        def projection(
            mode: str, family: str, values: dict[str, object]
        ) -> dict[str, str]:
            return {
                "slot_key": self._relation_slot_key(family, values),
                "slot_mode": mode,
                "slot_policy_version": policy_version,
            }

        exact_identity = {
            "subject": subject_entity_id,
            "source": source_entity_id,
            "target": target_entity_id,
            "relation": normalized_relation,
        }
        if (
            normalized_relation == "has-component"
            and entity_types.get(subject_entity_id) == "term"
            and entity_types.get(source_entity_id) == "term"
            and entity_types.get(target_entity_id) == "morpheme"
        ):
            ordinal = self._relation_slot_ordinal(properties.get("ordinal"))
            if ordinal is not None:
                branch = self._relation_slot_text(
                    properties,
                    "branch_id",
                    "decomposition_branch",
                    "analysis_branch",
                    "branch",
                ) or "primary"
                return projection(
                    "exclusive",
                    "component",
                    {
                        "subject": subject_entity_id,
                        "branch": branch,
                        "ordinal": ordinal,
                    },
                )

        if (
            normalized_relation == "has-meaning"
            and entity_types.get(target_entity_id) == "meaning"
        ):
            meaning = connection.execute(
                """SELECT subject_entity_id, language, sense_order
                   FROM meanings WHERE entity_id = ?""",
                (target_entity_id,),
            ).fetchone()
            if meaning is not None:
                owner_id = str(meaning["subject_entity_id"])
                language = str(meaning["language"]).strip().casefold()
                sense_order = self._relation_slot_ordinal(meaning["sense_order"])
                if (
                    owner_id in {subject_entity_id, source_entity_id}
                    and language
                    and sense_order is not None
                ):
                    return projection(
                        "exclusive",
                        "meaning",
                        {
                            "subject": subject_entity_id,
                            "owner": owner_id,
                            "language": language,
                            "sense_order": sense_order,
                        },
                    )

        translation_relations = {
            "has-translation",
            "renders-as",
            "translated-from",
            "reviewed-translation",
        }
        if normalized_relation in translation_relations:
            source_meaning_id = (
                source_entity_id
                if entity_types.get(source_entity_id) == "meaning"
                else self._relation_slot_text(properties, "source_meaning_id")
            )
            if source_meaning_id and source_meaning_id not in entity_types:
                source_meaning = connection.execute(
                    "SELECT entity_type FROM entities WHERE entity_id = ?",
                    (source_meaning_id,),
                ).fetchone()
                if source_meaning is None or str(source_meaning["entity_type"]) != "meaning":
                    source_meaning_id = ""
            elif source_meaning_id and entity_types.get(source_meaning_id) != "meaning":
                source_meaning_id = ""
            target_language = self._relation_slot_text(
                properties, "target_language", "language"
            ) or self._relation_entity_language(
                connection,
                target_entity_id,
                entity_types.get(target_entity_id, ""),
            )
            role = self._relation_slot_text(
                properties, "translation_role", "role"
            )
            preferred_value = properties.get("preferred")
            preferred = preferred_value is True or str(preferred_value).strip().casefold() in {
                "1", "true", "yes", "preferred", "primary"
            }
            if source_meaning_id and target_language and (
                preferred or role in {"preferred", "primary"}
            ):
                return projection(
                    "exclusive",
                    "preferred-translation",
                    {
                        "subject": subject_entity_id,
                        "source_meaning": source_meaning_id,
                        "target_language": target_language,
                        "role": role or "preferred",
                    },
                )
            return projection("set-member", "translation-member", exact_identity)

        if normalized_relation == "developed-into":
            branch = self._relation_slot_text(
                properties, "branch_id", "history_branch", "branch"
            ) or "primary"
            component_id = self._relation_slot_text(properties, "component_id")
            if component_id:
                return projection(
                    "exclusive",
                    "history-transition",
                    {
                        "subject": subject_entity_id,
                        "component": component_id,
                        "source": source_entity_id,
                        "branch": branch,
                    },
                )

        if "deriv" in normalized_relation or normalized_relation == "shares-component":
            return projection("set-member", "derivative-member", exact_identity)
        return projection("none", "unclassified", exact_identity)

    @staticmethod
    def _relation_slot_family(slot_key: str) -> str:
        parts = str(slot_key).split(":", 2)
        return parts[1] if len(parts) == 3 and parts[0] == "relation-slot" else ""

    def _relation_slot_structural_rank(
        self,
        connection: sqlite3.Connection,
        slot_key: str,
        claim: object,
    ) -> int:
        """Rank endpoint shapes only; never rank linguistic truth here."""

        def value(name: str) -> object:
            try:
                return claim[name]  # type: ignore[index]
            except (KeyError, IndexError, TypeError):
                return ""

        subject_id = str(value("subject_entity_id"))
        source_id = str(value("source_entity_id"))
        target_id = str(value("target_entity_id"))
        relation = str(value("relation")).strip().casefold()
        identifiers = sorted({subject_id, source_id, target_id} - {""})
        if not identifiers:
            return 0
        placeholders = ",".join("?" for _ in identifiers)
        entity_types = {
            str(row["entity_id"]): str(row["entity_type"])
            for row in connection.execute(
                f"SELECT entity_id, entity_type FROM entities "
                f"WHERE entity_id IN ({placeholders})",
                identifiers,
            )
        }
        family = self._relation_slot_family(slot_key)
        source_type = entity_types.get(source_id, "")
        target_type = entity_types.get(target_id, "")
        subject_type = entity_types.get(subject_id, "")
        if family == "component":
            return 5 if (
                relation == "has-component"
                and subject_id == source_id
                and subject_type == "term"
                and source_type == "term"
                and target_type == "morpheme"
            ) else 0
        if family == "meaning":
            return 5 if relation == "has-meaning" and target_type == "meaning" else 0
        if family == "preferred-translation":
            valid = {
                "has-translation": target_type == "translation",
                "renders-as": source_type == "translation" and target_type == "term",
                "translated-from": source_type == "meaning" and target_type == "meaning",
                "reviewed-translation": "translation" in {source_type, target_type},
            }.get(relation, False)
            return 5 if valid else 0
        if family == "history-transition":
            if relation != "developed-into" or source_type != "historical-form":
                return 0
            if target_type == "historical-form":
                return 5
            if target_type == "morpheme":
                return 4
            if target_type == "term":
                return 3
            return 0
        return 1

    def _replaceable_relation_collision_sort_key(
        self,
        connection: sqlite3.Connection,
        slot_key: str,
        row: sqlite3.Row,
    ) -> tuple[object, ...]:
        assertion_id = str(row["assertion_id"])
        properties = self._relation_assertion_properties(row["properties_json"])
        exact_ids = self._relation_assertion_evidence_ids(connection, assertion_id)
        context_ids = self._existing_relation_evidence_ids(
            connection, self._relation_context_evidence_ids(properties)
        )
        basis = str(row["basis"]).casefold()
        return (
            -self._relation_slot_structural_rank(connection, slot_key, row),
            -self._relation_claim_strength(basis, exact_ids, context_ids),
            -{"derived": 2, "model": 1}.get(basis, 0),
            -float(row["confidence"]),
            str(row["created_at"]),
            assertion_id,
        )

    def _decide_relation_slot(
        self,
        connection: sqlite3.Connection,
        proposal: dict[str, object],
        *,
        exclude_assertion_id: str,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "materialize": True,
            "slot_decision": "",
            "slot_incumbent_ids": [],
            "supersede_ids": [],
            "slot_reason": "",
        }
        if proposal["slot_mode"] != "exclusive":
            return result
        rows = connection.execute(
            """SELECT assertion.assertion_id, assertion.subject_entity_id,
                      assertion.source_entity_id, assertion.target_entity_id,
                      assertion.relation, assertion.basis, assertion.confidence,
                      assertion.status, assertion.properties_json,
                      assertion.revision, assertion.created_at,
                      assertion.updated_at
               FROM relation_assertion_slots AS slot
               JOIN relation_assertions AS assertion
                 ON assertion.assertion_id = slot.assertion_id
               WHERE slot.slot_key = ?
                 AND slot.slot_mode IN ('exclusive', 'set-member')
                 AND assertion.status = 'accepted'
                 AND assertion.assertion_id <> ?
               ORDER BY assertion.assertion_id""",
            (proposal["slot_key"], exclude_assertion_id),
        ).fetchall()
        if not rows:
            return result
        incumbents = [
            self._relation_assertion_snapshot(
                row,
                self._relation_assertion_evidence_ids(
                    connection, str(row["assertion_id"])
                ),
                self._relation_context_evidence_ids(
                    self._relation_assertion_properties(row["properties_json"])
                ),
            )
            for row in rows
        ]
        incumbent_ids = [str(item["assertion_id"]) for item in incumbents]
        result["slot_incumbent_ids"] = incumbent_ids
        proposal_structure = self._relation_slot_structural_rank(
            connection, str(proposal["slot_key"]), proposal
        )
        incumbent_structure = {
            str(row["assertion_id"]): self._relation_slot_structural_rank(
                connection, str(proposal["slot_key"]), row
            )
            for row in rows
        }
        strongest_incumbent_structure = max(incumbent_structure.values())
        protected = [
            item for item in incumbents
            if str(item["basis"]) in {"book", "reviewed"}
        ]
        replaceable = [
            item for item in incumbents
            if str(item["basis"]) in {"model", "derived"}
        ]
        if proposal_structure < strongest_incumbent_structure:
            protected_proposal = proposal["basis"] in {"book", "reviewed"}
            return {
                **result,
                "materialize": False,
                "slot_decision": "conflict" if protected_proposal else "keep",
                "supersede_ids": [],
                "slot_reason": (
                    "proposal retained as an observation because its endpoint "
                    "shape is weaker for this relation family"
                ),
            }
        if proposal["basis"] in {"book", "reviewed"}:
            if protected:
                return {
                    **result,
                    "materialize": False,
                    "slot_decision": "conflict",
                    "supersede_ids": [],
                    "slot_reason": (
                        "conflicting exact book/reviewed claim retained as an observation"
                    ),
                }
            if replaceable:
                result["supersede_ids"] = [
                    str(item["assertion_id"]) for item in replaceable
                ]
                result["slot_decision"] = "supersede"
                result["slot_reason"] = (
                    "exact book/reviewed evidence superseded model claims"
                )
            return result
        if protected:
            return {
                **result,
                "materialize": False,
                "slot_decision": "keep",
                "slot_reason": "model proposal cannot replace book/reviewed knowledge",
            }
        if proposal_structure > strongest_incumbent_structure:
            result["supersede_ids"] = incumbent_ids
            result["slot_decision"] = "supersede"
            result["slot_reason"] = (
                "structurally valid relation endpoints superseded weaker model endpoints"
            )
            return result
        context_evidence_ids = list(proposal["context_evidence_ids"])
        prompt_version = str(proposal["prompt_version"])
        epoch_fingerprint = str(proposal["epoch_fingerprint"])
        if not context_evidence_ids or not prompt_version or not epoch_fingerprint:
            return {
                **result,
                "materialize": False,
                "slot_decision": "pending",
                "slot_reason": (
                    "model correction needs an evidenced prompt epoch"
                ),
            }
        incumbent_confidence = max(
            float(item["confidence"]) for item in incumbents
        )
        if float(proposal["confidence"]) + 1e-12 < incumbent_confidence:
            return {
                **result,
                "materialize": False,
                "slot_decision": "keep",
                "slot_reason": "model correction would lower incumbent confidence",
            }
        # Agreement must concern the current competing state. Historical votes
        # from before a supersession cannot be reused to flip the slot back.
        incumbent_since = max(str(item["updated_at"]) for item in incumbents)
        prior_epoch = connection.execute(
            """SELECT 1 FROM relation_claim_observations
               WHERE slot_key = ? AND proposed_assertion_id = ?
                 AND epoch_fingerprint <> '' AND epoch_fingerprint <> ?
                 AND prompt_version <> ''
                 AND json_array_length(context_evidence_ids_json) > 0
                 AND decision IN ('pending','keep','conflict')
                 AND incumbent_assertion_ids_json = ?
                 AND created_at >= ?
                 AND CASE WHEN json_valid(proposal_json)
                     THEN COALESCE(json_extract(proposal_json, '$.confidence'), 0)
                     ELSE 0 END + 1e-12 >= ?
               LIMIT 1""",
            (
                proposal["slot_key"],
                proposal["assertion_id"],
                epoch_fingerprint,
                self._canonical_relation_json(sorted(incumbent_ids)),
                incumbent_since,
                incumbent_confidence,
            ),
        ).fetchone()
        if prior_epoch is None:
            return {
                **result,
                "materialize": False,
                "slot_decision": "pending",
                "slot_reason": (
                    "model correction awaits a second independent evidenced prompt epoch"
                ),
            }
        result["slot_decision"] = "supersede"
        result["supersede_ids"] = incumbent_ids
        result["slot_reason"] = (
            "two independent evidenced prompt epochs agreed without lowering confidence"
        )
        return result

    def _prepare_relation_claim(
        self,
        connection: sqlite3.Connection,
        subject_entity_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        *,
        basis: str,
        confidence: float,
        evidence_ids=(),
        context_evidence_ids=(),
        properties=None,
        input_fingerprint: str = "",
        output_fingerprint: str = "",
        job_id: str | None = None,
        reason: str = "",
        prompt_version: str = "",
        epoch_fingerprint: str = "",
        validator_version: str = "relation-assertion-decision-v1",
    ) -> dict[str, object]:
        subject_entity_id = str(subject_entity_id).strip()
        source_entity_id = str(source_entity_id).strip()
        target_entity_id = str(target_entity_id).strip()
        relation = str(relation).strip()
        if not all((subject_entity_id, source_entity_id, target_entity_id, relation)):
            raise ValueError("relation assertion identity cannot be blank")
        if source_entity_id == target_entity_id:
            raise ValueError("relation assertion cannot link an entity to itself")
        declared_basis = str(basis or "model").strip().casefold()
        if declared_basis not in {"book", "dictionary", "model", "reviewed", "derived"}:
            raise ValueError(f"invalid relation assertion basis: {basis}")
        entity_ids = sorted({subject_entity_id, source_entity_id, target_entity_id})
        placeholders = ",".join("?" for _ in entity_ids)
        found = {
            str(row["entity_id"])
            for row in connection.execute(
                f"SELECT entity_id FROM entities WHERE entity_id IN ({placeholders})",
                entity_ids,
            )
        }
        missing = [value for value in entity_ids if value not in found]
        if missing:
            raise ValueError("unknown relation assertion entities: " + ", ".join(missing))
        try:
            confidence_value = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError) as exc:
            raise ValueError("relation assertion confidence must be numeric") from exc
        raw_properties = self._relation_assertion_properties(properties or {})
        explicit_job_id = str(job_id or "").strip()
        property_job_id = str(
            raw_properties.get("enrichment_job_id", "") or ""
        ).strip()
        lookup_job_id = explicit_job_id or property_job_id
        job_row = (
            connection.execute(
                """SELECT job_id, prompt_version, source_fingerprint
                   FROM preparation_jobs WHERE job_id = ?""",
                (lookup_job_id,),
            ).fetchone()
            if lookup_job_id
            else None
        )
        resolved_job_id = explicit_job_id or (
            str(job_row["job_id"]) if job_row is not None else ""
        )
        inferred_prompt_version = (
            str(job_row["prompt_version"] or "").strip()
            if job_row is not None
            else ""
        )
        inferred_epoch_fingerprint = (
            str(job_row["source_fingerprint"] or "").strip()
            if job_row is not None
            else ""
        )
        requested_exact = sorted({str(value).strip() for value in evidence_ids if str(value).strip()})
        requested_context = sorted(
            {
                *self._relation_context_evidence_ids(raw_properties),
                *(str(value).strip() for value in context_evidence_ids if str(value).strip()),
            }
        )
        valid = set(
            self._existing_relation_evidence_ids(
                connection, [*requested_exact, *requested_context]
            )
        )
        valid_exact = sorted(set(requested_exact) & valid)
        valid_context = sorted(set(requested_context) & valid)
        properties_value = self._relation_assertion_properties(raw_properties)
        warnings = properties_value.get("warnings", [])
        if isinstance(warnings, str):
            warnings = [warnings]
        if not isinstance(warnings, (list, tuple, set)):
            warnings = []
        warning_values = [str(item).strip() for item in warnings if str(item).strip()]
        stored_basis = "book" if declared_basis == "dictionary" else declared_basis
        if declared_basis == "dictionary":
            properties_value["declared_basis"] = "dictionary"
        if stored_basis not in {"book", "reviewed"}:
            valid_context = sorted({*valid_context, *valid_exact})
            valid_exact = []
        if stored_basis in {"book", "reviewed"} and not valid_exact:
            properties_value.setdefault("original_basis", stored_basis)
            warning_values.append(
                f"{stored_basis} basis had no exact evidence and was retained as model context"
            )
            stored_basis = "model"
        unknown = sorted({*requested_exact, *requested_context} - valid)
        if unknown:
            warning_values.append("unknown evidence IDs were not materialized: " + ", ".join(unknown))
        if valid_context:
            properties_value["context_evidence_ids"] = valid_context
        else:
            properties_value.pop("context_evidence_ids", None)
        if warning_values:
            properties_value["warnings"] = sorted(set(warning_values))
        else:
            properties_value.pop("warnings", None)
        prompt_value = str(
            prompt_version
            or properties_value.get("prompt_version", "")
            or inferred_prompt_version
        ).strip()
        supplied_epoch = str(
            epoch_fingerprint
            or properties_value.get("epoch_fingerprint", "")
            or inferred_epoch_fingerprint
        ).strip()
        raw_claim = {
            "subject_entity_id": subject_entity_id,
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "relation": relation,
            "basis": declared_basis,
            "confidence": confidence_value,
            "evidence_ids": requested_exact,
            "context_evidence_ids": requested_context,
            "properties": raw_properties,
            "job_id": resolved_job_id,
            "prompt_version": prompt_value,
            "epoch_fingerprint": supplied_epoch,
        }
        slot = self._relation_slot_projection(
            connection,
            subject_entity_id,
            source_entity_id,
            target_entity_id,
            relation,
            properties_value,
        )
        if slot["slot_mode"] == "exclusive":
            structural_claim = {
                "subject_entity_id": subject_entity_id,
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "relation": relation,
            }
            if self._relation_slot_structural_rank(
                connection, slot["slot_key"], structural_claim
            ) <= 0:
                slot["slot_mode"] = "set-member"
                structural_warning = (
                    "endpoint shape retained as non-exclusive pending structural correction"
                )
                current_warnings = properties_value.get("warnings", [])
                if isinstance(current_warnings, str):
                    current_warnings = [current_warnings]
                if not isinstance(current_warnings, (list, tuple, set)):
                    current_warnings = []
                properties_value["warnings"] = sorted(
                    {
                        *(str(item).strip() for item in current_warnings if str(item).strip()),
                        structural_warning,
                    }
                )
        epoch_value = ""
        if valid_context and prompt_value:
            epoch_value = hashlib.sha256(
                self._canonical_relation_json(
                    {
                        "context_evidence_ids": valid_context,
                        "prompt_version": prompt_value,
                        "epoch": supplied_epoch,
                    }
                ).encode("utf-8")
            ).hexdigest()
        normalized_claim = {
            **{key: raw_claim[key] for key in (
                "subject_entity_id", "source_entity_id", "target_entity_id", "relation"
            )},
            "basis": stored_basis,
            "confidence": confidence_value,
            "evidence_ids": valid_exact,
            "context_evidence_ids": valid_context,
            "properties": properties_value,
            **slot,
            "prompt_version": prompt_value,
            "epoch_fingerprint": epoch_value,
        }
        input_value = str(input_fingerprint or "").strip() or hashlib.sha256(
            self._canonical_relation_json(raw_claim).encode("utf-8")
        ).hexdigest()
        output_value = str(output_fingerprint or "").strip() or hashlib.sha256(
            self._canonical_relation_json(normalized_claim).encode("utf-8")
        ).hexdigest()
        decision_fingerprint = hashlib.sha256(
            self._canonical_relation_json(
                {
                    "claim": normalized_claim,
                    "input_fingerprint": input_value,
                    "output_fingerprint": output_value,
                    "validator_version": validator_version,
                }
            ).encode("utf-8")
        ).hexdigest()
        identity = "\x1f".join(
            (subject_entity_id, source_entity_id, target_entity_id, relation)
        )
        return {
            **normalized_claim,
            "assertion_id": _identifier("relation-assertion", identity),
            "decision_id": _identifier("relation-decision", decision_fingerprint),
            "decision_fingerprint": decision_fingerprint,
            "input_fingerprint": input_value,
            "output_fingerprint": output_value,
            "job_id": resolved_job_id or None,
            "reason": str(reason or "").strip(),
            "validator_version": str(validator_version or "relation-assertion-decision-v1"),
        }

    def _decide_prepared_relation_claim(
        self, connection: sqlite3.Connection, proposal: dict[str, object]
    ) -> dict[str, object]:
        row = connection.execute(
            """SELECT assertion_id, subject_entity_id, source_entity_id,
                      target_entity_id, relation, basis, confidence, status,
                      properties_json, revision, created_at, updated_at
               FROM relation_assertions
               WHERE subject_entity_id = ? AND source_entity_id = ?
                 AND target_entity_id = ? AND relation = ?""",
            tuple(proposal[key] for key in (
                "subject_entity_id", "source_entity_id", "target_entity_id", "relation"
            )),
        ).fetchone()
        base = {
            "decision_id": proposal["decision_id"],
            "decision_fingerprint": proposal["decision_fingerprint"],
            "proposal": proposal,
        }
        if row is None:
            slot = self._decide_relation_slot(
                connection,
                proposal,
                exclude_assertion_id=str(proposal["assertion_id"]),
            )
            if not slot["materialize"]:
                return {
                    **base,
                    **slot,
                    "action": "observe",
                    "assertion_id": proposal["assertion_id"],
                    "before": {},
                    "after": None,
                    "reason": slot["slot_reason"] or proposal["reason"],
                }
            return {
                **base,
                **slot,
                "action": "accept",
                "assertion_id": proposal["assertion_id"],
                "before": {},
                "after": None,
                "reason": proposal["reason"] or "new exact-identity assertion",
            }
        assertion_id = str(row["assertion_id"])
        proposal["assertion_id"] = assertion_id
        exact = self._relation_assertion_evidence_ids(connection, assertion_id)
        incumbent_properties = self._relation_assertion_properties(row["properties_json"])
        context = self._relation_context_evidence_ids(incumbent_properties)
        valid_context = self._existing_relation_evidence_ids(connection, context)
        incumbent = self._relation_assertion_snapshot(row, exact, context)
        comparable = {
            "basis": incumbent["basis"],
            "confidence": incumbent["confidence"],
            "status": incumbent["status"],
            "properties": incumbent["properties"],
            "evidence_ids": exact,
            "context_evidence_ids": context,
        }
        candidate = {
            "basis": proposal["basis"],
            "confidence": proposal["confidence"],
            "status": "accepted",
            "properties": proposal["properties"],
            "evidence_ids": proposal["evidence_ids"],
            "context_evidence_ids": proposal["context_evidence_ids"],
        }
        if self._canonical_relation_json(comparable) == self._canonical_relation_json(candidate):
            return {
                **base,
                "action": "noop",
                "assertion_id": assertion_id,
                "before": incumbent,
                "after": incumbent,
                "reason": "identical accepted assertion replay",
            }
        old_strength = self._relation_claim_strength(str(incumbent["basis"]), exact, valid_context)
        new_strength = self._relation_claim_strength(
            str(proposal["basis"]),
            list(proposal["evidence_ids"]),
            list(proposal["context_evidence_ids"]),
        )
        protected = str(incumbent["basis"]) in {"book", "reviewed"} and str(
            proposal["basis"]
        ) in {"model", "derived"}
        same_basis = str(incumbent["basis"]) == str(proposal["basis"])
        material = same_basis and (
            bool(set(proposal["evidence_ids"]) - set(exact))
            or bool(set(proposal["context_evidence_ids"]) - set(context))
            or float(proposal["confidence"]) > float(incumbent["confidence"]) + 1e-12
        )
        if protected or new_strength < old_strength or (
            new_strength == old_strength and not material and incumbent["status"] == "accepted"
        ):
            return {
                **base,
                "action": "keep",
                "assertion_id": assertion_id,
                "before": incumbent,
                "after": incumbent,
                "reason": proposal["reason"] or (
                    "protected reviewed/book assertion retained"
                    if protected
                    else "proposal did not materially strengthen the incumbent"
                ),
            }
        merged_context = sorted({*context, *proposal["context_evidence_ids"]})
        merged_properties = self._merge_relation_properties(
            incumbent_properties, proposal["properties"]
        )
        if merged_context:
            merged_properties["context_evidence_ids"] = merged_context
        else:
            merged_properties.pop("context_evidence_ids", None)
        proposal["properties"] = merged_properties
        proposal["context_evidence_ids"] = merged_context
        proposal["evidence_ids"] = (
            sorted({*exact, *proposal["evidence_ids"]})
            if proposal["basis"] in {"book", "reviewed"}
            else []
        )
        proposal["confidence"] = max(float(incumbent["confidence"]), float(proposal["confidence"]))
        slot = self._decide_relation_slot(
            connection,
            proposal,
            exclude_assertion_id=assertion_id,
        )
        if not slot["materialize"]:
            return {
                **base,
                **slot,
                "action": "observe",
                "assertion_id": assertion_id,
                "before": incumbent,
                "after": incumbent,
                "reason": slot["slot_reason"] or proposal["reason"],
            }
        return {
            **base,
            **slot,
            "action": "update",
            "assertion_id": assertion_id,
            "before": incumbent,
            "after": None,
            "reason": proposal["reason"] or (
                "stronger provenance accepted"
                if new_strength > old_strength
                else "same-basis confidence/evidence improved"
            ),
        }

    def decide_relation_claim(
        self,
        subject_entity_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        *,
        basis: str = "model",
        confidence: float = 0.5,
        evidence_ids=(),
        context_evidence_ids=(),
        properties=None,
        input_fingerprint: str = "",
        output_fingerprint: str = "",
        job_id: str | None = None,
        reason: str = "",
        prompt_version: str = "",
        epoch_fingerprint: str = "",
        validator_version: str = "relation-assertion-decision-v1",
    ) -> dict[str, object]:
        with closing(self._connect()) as connection:
            proposal = self._prepare_relation_claim(
                connection,
                subject_entity_id,
                source_entity_id,
                target_entity_id,
                relation,
                basis=basis,
                confidence=confidence,
                evidence_ids=evidence_ids,
                context_evidence_ids=context_evidence_ids,
                properties=properties,
                input_fingerprint=input_fingerprint,
                output_fingerprint=output_fingerprint,
                job_id=job_id,
                reason=reason,
                prompt_version=prompt_version,
                epoch_fingerprint=epoch_fingerprint,
                validator_version=validator_version,
            )
            return self._decide_prepared_relation_claim(connection, proposal)

    def _insert_relation_assertion_event(
        self,
        connection: sqlite3.Connection,
        decision: dict[str, object],
        before: dict[str, object],
        after: dict[str, object],
        *,
        ignore_replay: bool,
    ) -> tuple[str, bool]:
        proposal = decision["proposal"]
        action = str(decision["action"])
        if action in {"keep", "reject"}:
            key = f"{decision['assertion_id']}:{action}:{decision['decision_id']}"
        else:
            state_hash = hashlib.sha256(
                self._canonical_relation_json({"before": before, "after": after}).encode("utf-8")
            ).hexdigest()
            key = f"{decision['assertion_id']}:{action}:{decision['decision_id']}:{state_hash}"
        event_id = _identifier("relation-event", key)
        insert = "INSERT OR IGNORE" if ignore_replay else "INSERT"
        cursor = connection.execute(
            f"""{insert} INTO relation_assertion_events(
                   event_id, decision_id, assertion_id, action,
                   before_json, after_json, evidence_ids_json,
                   context_evidence_ids_json, input_fingerprint,
                   output_fingerprint, job_id, reason,
                   validator_version, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                decision["decision_id"],
                decision["assertion_id"],
                action,
                self._canonical_relation_json(before),
                self._canonical_relation_json(after),
                self._canonical_relation_json(after.get("evidence_ids", proposal["evidence_ids"])),
                self._canonical_relation_json(
                    after.get("context_evidence_ids", proposal["context_evidence_ids"])
                ),
                proposal["input_fingerprint"],
                proposal["output_fingerprint"],
                proposal["job_id"],
                decision["reason"],
                proposal["validator_version"],
                _now(),
            ),
        )
        return event_id, bool(cursor.rowcount)

    def _insert_relation_claim_observation(
        self,
        connection: sqlite3.Connection,
        decision: dict[str, object],
    ) -> tuple[str, bool]:
        proposal = decision["proposal"]
        outcome = str(decision.get("slot_decision", ""))
        if outcome not in {"pending", "keep", "conflict", "supersede", "coexist"}:
            return "", False
        incumbent_ids = sorted(
            {
                str(value).strip()
                for value in decision.get("slot_incumbent_ids", [])
                if str(value).strip()
            }
        )
        key = self._canonical_relation_json(
            {
                "slot_key": proposal["slot_key"],
                "proposed_assertion_id": proposal["assertion_id"],
                "incumbent_assertion_ids": incumbent_ids,
                "decision": outcome,
                "decision_id": decision["decision_id"],
            }
        )
        observation_id = _identifier("relation-observation", key)
        cursor = connection.execute(
            """INSERT OR IGNORE INTO relation_claim_observations(
                   observation_id, slot_key, slot_mode,
                   proposed_assertion_id, incumbent_assertion_ids_json,
                   proposal_json, decision, exact_evidence_ids_json,
                   context_evidence_ids_json, input_fingerprint,
                   output_fingerprint, epoch_fingerprint, prompt_version,
                   job_id, reason, policy_version, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                observation_id,
                proposal["slot_key"],
                proposal["slot_mode"],
                proposal["assertion_id"],
                self._canonical_relation_json(incumbent_ids),
                self._canonical_relation_json(proposal),
                outcome,
                self._canonical_relation_json(proposal["evidence_ids"]),
                self._canonical_relation_json(proposal["context_evidence_ids"]),
                proposal["input_fingerprint"],
                proposal["output_fingerprint"],
                proposal["epoch_fingerprint"],
                proposal["prompt_version"],
                proposal["job_id"],
                decision["reason"],
                proposal["slot_policy_version"],
                _now(),
            ),
        )
        return observation_id, bool(cursor.rowcount)

    def apply_relation_decision(
        self,
        subject_entity_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        *,
        basis: str = "model",
        confidence: float = 0.5,
        evidence_ids=(),
        context_evidence_ids=(),
        properties=None,
        input_fingerprint: str = "",
        output_fingerprint: str = "",
        job_id: str | None = None,
        reason: str = "",
        prompt_version: str = "",
        epoch_fingerprint: str = "",
        validator_version: str = "relation-assertion-decision-v1",
        owner_type: str = "",
        owner_id: str = "",
        owner_version: str = "",
    ) -> dict[str, object]:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_unarchived_endpoints(
                    connection, subject_entity_id, source_entity_id, target_entity_id
                )
                proposal = self._prepare_relation_claim(
                    connection,
                    subject_entity_id,
                    source_entity_id,
                    target_entity_id,
                    relation,
                    basis=basis,
                    confidence=confidence,
                    evidence_ids=evidence_ids,
                    context_evidence_ids=context_evidence_ids,
                    properties=properties,
                    input_fingerprint=input_fingerprint,
                    output_fingerprint=output_fingerprint,
                    job_id=job_id,
                    reason=reason,
                    prompt_version=prompt_version,
                    epoch_fingerprint=epoch_fingerprint,
                    validator_version=validator_version,
                )
                decision = self._decide_prepared_relation_claim(connection, proposal)
                action = str(decision["action"])
                proposed_superseded_ids = sorted(
                    {
                        str(value).strip()
                        for value in decision.get("supersede_ids", [])
                        if str(value).strip()
                    }
                )
                if (
                    str(proposal.get("slot_mode", "")) == "exclusive"
                    and proposed_superseded_ids
                ):
                    placeholders = ",".join("?" for _ in proposed_superseded_ids)
                    unowned_legacy_ids = [
                        str(row[0])
                        for row in connection.execute(
                            f"""SELECT assertion.assertion_id
                                FROM relation_assertions AS assertion
                                WHERE assertion.assertion_id IN ({placeholders})
                                  AND assertion.status = 'accepted'
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM relation_assertion_supports AS support
                                      WHERE support.assertion_id =
                                            assertion.assertion_id
                                  )
                                ORDER BY assertion.assertion_id""",
                            proposed_superseded_ids,
                        )
                    ]
                    if unowned_legacy_ids:
                        refusal_reason = (
                            "exclusive replacement retained as an observation because "
                            "the incumbent has no attributable support owner"
                        )
                        decision = {
                            **decision,
                            "action": "observe",
                            "materialize": False,
                            "reason": refusal_reason,
                            "slot_decision": "conflict",
                            "slot_reason": refusal_reason,
                            "supersede_ids": [],
                            "unowned_legacy_assertion_ids": unowned_legacy_ids,
                        }
                        action = "observe"
                default_owner_type = (
                    "atomic-split"
                    if str(proposal["basis"]) == "model"
                    and str(proposal["relation"])
                    in {"has-component", "shares-component"}
                    else "relation-decision"
                )
                support_owner_type = str(owner_type).strip() or default_owner_type
                support_owner_id = str(owner_id).strip() or str(
                    subject_entity_id
                ).strip()
                support_owner_version = str(owner_version).strip() or str(
                    epoch_fingerprint or prompt_version or output_fingerprint
                ).strip()

                def add_live_support(assertion_id: str, timestamp: str) -> bool:
                    assertion_id = str(assertion_id).strip()
                    if not assertion_id:
                        return False
                    accepted = connection.execute(
                        """SELECT 1 FROM relation_assertions
                           WHERE assertion_id = ? AND status = 'accepted'""",
                        (assertion_id,),
                    ).fetchone()
                    if accepted is None:
                        return False
                    upsert_relation_support(
                        connection,
                        assertion_id=assertion_id,
                        owner_type=support_owner_type,
                        owner_id=support_owner_id,
                        owner_version=support_owner_version,
                        basis=str(proposal["basis"]),
                        confidence=float(proposal["confidence"]),
                        properties=proposal["properties"],
                        evidence_ids=proposal["evidence_ids"],
                        timestamp=timestamp,
                    )
                    reduce_relation_assertions(
                        connection, (assertion_id,), timestamp=timestamp
                    )
                    return True

                def has_live_support(assertion_id: str) -> bool:
                    return connection.execute(
                        """SELECT 1 FROM relation_assertion_supports
                           WHERE assertion_id = ? AND owner_type = ? AND owner_id = ?
                             AND status = 'accepted'""",
                        (assertion_id, support_owner_type, support_owner_id),
                    ).fetchone() is not None

                if action == "noop":
                    add_live_support(str(decision.get("assertion_id", "")), _now())
                    authority_changed = self._sync_relation_authority(
                        connection,
                        {(
                            str(proposal["source_entity_id"]),
                            str(proposal["target_entity_id"]),
                            str(proposal["relation"]),
                        )},
                    )
                    if authority_changed:
                        self._mark_projection_repairs_for_entities(
                            connection,
                            (
                                str(proposal["subject_entity_id"]),
                                str(proposal["source_entity_id"]),
                                str(proposal["target_entity_id"]),
                            ),
                        )
                        self._advance_relation_graph_revision(connection)
                    connection.commit()
                    return {
                        **decision,
                        "event_id": "",
                        "event_recorded": False,
                        "observation_id": "",
                        "observation_recorded": False,
                        "graph_changed": authority_changed,
                    }
                if action == "observe":
                    observation_id, recorded = self._insert_relation_claim_observation(
                        connection, decision
                    )
                    connection.commit()
                    return {
                        **decision,
                        "event_id": "",
                        "event_recorded": False,
                        "observation_id": observation_id,
                        "observation_recorded": recorded,
                        "graph_changed": False,
                    }
                if action in {"keep", "reject"}:
                    if action == "keep" and not has_live_support(
                        str(decision.get("assertion_id", ""))
                    ):
                        add_live_support(
                            str(decision.get("assertion_id", "")), _now()
                        )
                    event_id, recorded = self._insert_relation_assertion_event(
                        connection,
                        decision,
                        decision["before"],
                        decision["after"],
                        ignore_replay=True,
                    )
                    authority_changed = self._sync_relation_authority(
                        connection,
                        {(
                            str(proposal["source_entity_id"]),
                            str(proposal["target_entity_id"]),
                            str(proposal["relation"]),
                        )},
                    )
                    if authority_changed:
                        self._mark_projection_repairs_for_entities(
                            connection,
                            (
                                str(proposal["subject_entity_id"]),
                                str(proposal["source_entity_id"]),
                                str(proposal["target_entity_id"]),
                            ),
                        )
                        self._advance_relation_graph_revision(connection)
                    connection.commit()
                    return {
                        **decision,
                        "event_id": event_id,
                        "event_recorded": recorded,
                        "observation_id": "",
                        "observation_recorded": False,
                        "graph_changed": authority_changed,
                    }
                now = _now()
                assertion_id = str(decision["assertion_id"])
                if action == "accept":
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id, subject_entity_id, source_entity_id,
                               target_entity_id, relation, basis, confidence, status,
                               properties_json, revision, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, 1, ?, ?)""",
                        (
                            assertion_id,
                            proposal["subject_entity_id"],
                            proposal["source_entity_id"],
                            proposal["target_entity_id"],
                            proposal["relation"],
                            proposal["basis"],
                            proposal["confidence"],
                            self._canonical_relation_json(proposal["properties"]),
                            now,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE relation_assertions
                           SET basis = ?, confidence = ?, status = 'accepted',
                               properties_json = ?, revision = revision + 1, updated_at = ?
                           WHERE assertion_id = ?""",
                        (
                            proposal["basis"],
                            proposal["confidence"],
                            self._canonical_relation_json(proposal["properties"]),
                            now,
                            assertion_id,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM assertion_evidence WHERE assertion_id = ?", (assertion_id,)
                    )
                for evidence_id in proposal["evidence_ids"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id) VALUES (?, ?)",
                        (assertion_id, evidence_id),
                    )
                upsert_relation_support(
                    connection,
                    assertion_id=assertion_id,
                    owner_type=support_owner_type,
                    owner_id=support_owner_id,
                    owner_version=support_owner_version,
                    basis=str(proposal["basis"]),
                    confidence=float(proposal["confidence"]),
                    properties=proposal["properties"],
                    evidence_ids=proposal["evidence_ids"],
                    timestamp=now,
                )
                connection.execute(
                    """INSERT INTO relation_assertion_slots(
                           assertion_id, slot_key, slot_mode,
                           policy_version, created_at
                       ) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(assertion_id) DO UPDATE SET
                           slot_key = excluded.slot_key,
                           slot_mode = excluded.slot_mode,
                           policy_version = excluded.policy_version""",
                    (
                        assertion_id,
                        proposal["slot_key"],
                        proposal["slot_mode"],
                        proposal["slot_policy_version"],
                        now,
                    ),
                )
                superseded_ids = sorted(
                    {
                        str(value).strip()
                        for value in decision.get("supersede_ids", [])
                        if str(value).strip() and str(value).strip() != assertion_id
                    }
                )
                authority_triples = {
                    (
                        str(proposal["source_entity_id"]),
                        str(proposal["target_entity_id"]),
                        str(proposal["relation"]),
                    )
                }
                projection_entities = {
                    str(proposal["subject_entity_id"]),
                    str(proposal["source_entity_id"]),
                    str(proposal["target_entity_id"]),
                }
                if superseded_ids:
                    placeholders = ",".join("?" for _ in superseded_ids)
                    superseded_rows = connection.execute(
                        f"""SELECT subject_entity_id, source_entity_id,
                                   target_entity_id, relation
                            FROM relation_assertions
                            WHERE assertion_id IN ({placeholders})""",
                        superseded_ids,
                    ).fetchall()
                    authority_triples.update(
                        (
                            str(item["source_entity_id"]),
                            str(item["target_entity_id"]),
                            str(item["relation"]),
                        )
                        for item in superseded_rows
                    )
                    projection_entities.update(
                        str(item[column])
                        for item in superseded_rows
                        for column in (
                            "subject_entity_id",
                            "source_entity_id",
                            "target_entity_id",
                        )
                    )
                    for superseded_id in superseded_ids:
                        # Exclusive-slot arbitration supersedes the assertion, not
                        # merely the winning proposal's owner scope. The preflight
                        # above still refuses unattributed legacy incumbents.
                        active_owners = connection.execute(
                            """SELECT owner_type, owner_id
                               FROM relation_assertion_supports
                               WHERE assertion_id = ? AND status = 'accepted'
                               ORDER BY owner_type, owner_id""",
                            (superseded_id,),
                        ).fetchall()
                        for active_owner in active_owners:
                            retire_relation_support(
                                connection,
                                assertion_id=superseded_id,
                                owner_type=str(active_owner["owner_type"]),
                                owner_id=str(active_owner["owner_id"]),
                                timestamp=now,
                            )
                reduce_relation_assertions(
                    connection,
                    (assertion_id, *superseded_ids),
                    timestamp=now,
                )
                row = connection.execute(
                    """SELECT assertion_id, subject_entity_id, source_entity_id,
                              target_entity_id, relation, basis, confidence, status,
                              properties_json, revision, created_at, updated_at
                       FROM relation_assertions WHERE assertion_id = ?""",
                    (assertion_id,),
                ).fetchone()
                after = self._relation_assertion_snapshot(
                    row,
                    self._relation_assertion_evidence_ids(connection, assertion_id),
                    self._relation_context_evidence_ids(
                        self._relation_assertion_properties(row["properties_json"])
                    ),
                )
                event_id, recorded = self._insert_relation_assertion_event(
                    connection, decision, decision["before"], after, ignore_replay=False
                )
                observation_id, observation_recorded = (
                    self._insert_relation_claim_observation(connection, decision)
                )
                authority_changed = self._sync_relation_authority(
                    connection, authority_triples, timestamp=now
                )
                self._mark_projection_repairs_for_entities(
                    connection, projection_entities, timestamp=now
                )
                self._advance_relation_graph_revision(connection)
                connection.commit()
                return {
                    **decision,
                    "after": after,
                    "event_id": event_id,
                    "event_recorded": recorded,
                    "observation_id": observation_id,
                    "observation_recorded": observation_recorded,
                    "superseded_assertion_ids": superseded_ids,
                    "edge_authority_changed": authority_changed,
                    "graph_changed": True,
                }
            except Exception:
                connection.rollback()
                raise

    def accept_relation_assertion(
        self,
        subject_entity_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        *,
        basis: str = "model",
        confidence: float = 0.5,
        properties: Mapping[str, object] | None = None,
        evidence_ids: Sequence[str] = (),
        context_evidence_ids: Sequence[str] = (),
        input_fingerprint: str = "",
        output_fingerprint: str = "",
        job_id: str | None = None,
        reason: str = "",
        prompt_version: str = "",
        epoch_fingerprint: str = "",
        owner_type: str = "",
        owner_id: str = "",
        owner_version: str = "",
    ) -> str:
        decision = self.apply_relation_decision(
            subject_entity_id,
            source_entity_id,
            target_entity_id,
            relation,
            basis=basis,
            confidence=confidence,
            evidence_ids=evidence_ids,
            context_evidence_ids=context_evidence_ids,
            properties=properties,
            input_fingerprint=input_fingerprint,
            output_fingerprint=output_fingerprint,
            job_id=job_id,
            reason=reason,
            prompt_version=prompt_version,
            epoch_fingerprint=epoch_fingerprint,
            owner_type=owner_type,
            owner_id=owner_id,
            owner_version=owner_version,
        )
        return "" if decision["action"] == "observe" else str(decision["assertion_id"])

    def accept_origin_transition(
        self, subject_entity_id: str, source_entity_id: str, target_entity_id: str,
        *, component_id: str, basis: str, confidence: float,
        evidence_ids: Sequence[str] = (), context_evidence_ids: Sequence[str] = (),
        job_id: str | None = None, prompt_version: str = "",
    ) -> str:
        """Correct legacy co-occurrence provenance while retaining its audit trail."""

        def persist() -> str:
            if basis == "model":
                with closing(self._connect()) as connection:
                    rows = connection.execute(
                        """SELECT assertion_id, properties_json
                           FROM relation_assertions
                           WHERE subject_entity_id=? AND source_entity_id=?
                             AND target_entity_id=? AND relation='developed-into'
                             AND status='accepted' AND basis='book'""",
                        (subject_entity_id, source_entity_id, target_entity_id),
                    ).fetchall()
                for row in rows:
                    properties = self._relation_assertion_properties(row["properties_json"])
                    if (
                        properties.get("source_term_id") == subject_entity_id
                        and properties.get("component_id") == component_id
                        and properties.get("origin_provenance_version") != 2
                    ):
                        assertion_id = str(row["assertion_id"])
                        with closing(self._connect()) as connection:
                            owned = connection.execute(
                                """SELECT 1 FROM relation_assertion_supports
                                   WHERE assertion_id=? AND owner_type='relation-decision'
                                     AND owner_id=? AND basis='book' AND status='accepted'""",
                                (assertion_id, subject_entity_id),
                            ).fetchone()
                            if owned is None:
                                continue
                            upsert_relation_support(
                                connection, assertion_id=assertion_id,
                                owner_type="relation-decision", owner_id=subject_entity_id,
                                owner_version=prompt_version, basis="model",
                                confidence=min(confidence, 0.7), evidence_ids=(),
                                properties={**properties, "origin_provenance_version": 2},
                            )
                            reduce_relation_assertions(connection, (assertion_id,))
                            self._mark_projection_repairs_for_entities(
                                connection, (subject_entity_id, source_entity_id, target_entity_id),
                            )
                            self._advance_relation_graph_revision(connection)
                        self.record_revision(
                            subject_entity_id,
                            {"assertion_id": assertion_id, "previous_basis": "book",
                             "corrected_basis": "model", "context_evidence_ids": list(context_evidence_ids)},
                            model="system", prompt_version=prompt_version,
                            reason="corrected legacy origin co-occurrence provenance", accepted=True,
                        )
            return self.accept_relation_assertion(
                subject_entity_id, source_entity_id, target_entity_id, "developed-into",
                basis=basis, confidence=confidence, evidence_ids=evidence_ids,
                context_evidence_ids=context_evidence_ids, job_id=job_id,
                prompt_version=prompt_version,
                reason="origin transition: distinguish attestation from ancestry",
                properties={
                    "component_id": component_id,
                    "source_term_id": subject_entity_id,
                    "origin_provenance_version": 2,
                    "modes": ["knowledge", "word", "root", "affix"],
                },
            )

        return self.atomic_semantic_write(subject_entity_id, persist)

    def retire_relation_assertion(
        self, subject_entity_id: str, assertion_id: str
    ) -> bool:
        """Archive one contextual assertion without changing another subject."""

        timestamp = _now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """SELECT source_entity_id, target_entity_id, relation
                       FROM relation_assertions
                       WHERE assertion_id = ? AND subject_entity_id = ?
                         AND status = 'accepted'""",
                    (assertion_id.strip(), subject_entity_id.strip()),
                ).fetchone()
                cursor = connection.execute(
                    """UPDATE relation_assertions
                       SET status = 'archived', revision = revision + 1,
                           updated_at = ?
                       WHERE assertion_id = ? AND subject_entity_id = ?
                         AND status = 'accepted'""",
                    (timestamp, assertion_id.strip(), subject_entity_id.strip()),
                )
                changed = bool(cursor.rowcount)
                if changed:
                    self._sync_relation_authority(
                        connection,
                        {(
                            str(row["source_entity_id"]),
                            str(row["target_entity_id"]),
                            str(row["relation"]),
                        )},
                        timestamp=timestamp,
                    )
                    self._mark_projection_repairs_for_entities(
                        connection,
                        (
                            subject_entity_id,
                            str(row["source_entity_id"]),
                            str(row["target_entity_id"]),
                        ),
                        timestamp=timestamp,
                    )
                    self._advance_relation_graph_revision(connection)
                connection.commit()
                return changed
            except Exception:
                connection.rollback()
                raise

    def reconcile_accepted_lexical_split(
        self,
        subject_entity_id: str,
        *,
        active_assertion_ids: Iterable[str],
        active_component_ids: Iterable[str],
        component_count: int,
        owner_type: str = "",
        owner_id: str = "",
        owner_version: str = "",
    ) -> dict[str, int]:
        """Retire stale structural splits while retaining discovered neighbors.

        Replacement assertions and ordinal rows are written first by the
        worker. This final subject-scoped reconciliation leaves historical
        assertions and cumulative derivative proposals for retained components
        intact, while retiring proposals whose component was replaced.
        Canonical entities, artifacts, revisions, and cards remain reusable. A
        later graph pass can add evidence or supersede a retained proposal
        explicitly; omission alone is not a semantic rejection.
        """

        subject_entity_id = str(subject_entity_id).strip()
        if not subject_entity_id:
            raise ValueError("lexical split subject is empty")
        component_count = int(component_count)
        if component_count < 1:
            raise ValueError("accepted lexical split has no components")
        kept_assertions = {
            str(assertion_id).strip()
            for assertion_id in active_assertion_ids
            if str(assertion_id).strip()
        }
        kept_components = sorted(
            {
                str(component_id).strip()
                for component_id in active_component_ids
                if str(component_id).strip()
            }
        )
        if not kept_components:
            raise ValueError("accepted lexical split has no canonical components")

        timestamp = _now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if owner_type.strip() or owner_id.strip():
                    resolved_owner_type = owner_type.strip() or "atomic-split"
                    resolved_owner_id = owner_id.strip() or subject_entity_id
                    rows = []
                    if kept_assertions:
                        placeholders = ",".join("?" for _ in kept_assertions)
                        rows = connection.execute(
                            f"""SELECT assertion.assertion_id,
                                       support.support_id,
                                       support.basis, support.confidence,
                                       support.properties_json
                                FROM relation_assertions AS assertion
                                JOIN relation_assertion_supports AS support
                                  ON support.assertion_id = assertion.assertion_id
                                 AND support.owner_type = ?
                                 AND support.owner_id = ?
                                 AND support.status = 'accepted'
                                WHERE assertion.assertion_id IN ({placeholders})
                                  AND assertion.subject_entity_id = ?
                                  AND assertion.status = 'accepted'""",
                            (
                                resolved_owner_type,
                                resolved_owner_id,
                                *sorted(kept_assertions),
                                subject_entity_id,
                            ),
                        ).fetchall()
                    if {str(row["assertion_id"]) for row in rows} != kept_assertions:
                        raise ValueError(
                            "active lexical split assertion is missing owner support"
                        )
                    supports = []
                    for row in rows:
                        assertion_id = str(row["assertion_id"])
                        supports.append(
                            RelationSupportSpec(
                                assertion_id=assertion_id,
                                basis=str(row["basis"]),
                                confidence=float(row["confidence"]),
                                properties=self._relation_assertion_properties(
                                    row["properties_json"]
                                ),
                                evidence_ids=tuple(
                                    str(item[0])
                                    for item in connection.execute(
                                        """SELECT evidence_id
                                           FROM relation_assertion_support_evidence
                                           WHERE support_id = ?
                                           ORDER BY evidence_id""",
                                        (str(row["support_id"]),),
                                    )
                                ),
                            )
                        )
                    before = {
                        str(row["assertion_id"]): str(row["status"])
                        for row in connection.execute(
                            """SELECT DISTINCT support.assertion_id,
                                      assertion.status
                               FROM relation_assertion_supports AS support
                               JOIN relation_assertions AS assertion
                                 ON assertion.assertion_id = support.assertion_id
                               WHERE support.owner_type = ?
                                 AND support.owner_id = ?
                                 AND support.status = 'accepted'""",
                            (resolved_owner_type, resolved_owner_id),
                        )
                    }
                    outcome = replace_relation_support_scope(
                        connection,
                        owner_type=resolved_owner_type,
                        owner_id=resolved_owner_id,
                        owner_version=owner_version.strip(),
                        supports=tuple(supports),
                        timestamp=timestamp,
                    )
                    retired_assertions = sum(
                        1
                        for assertion_id, prior_status in before.items()
                        if prior_status == "accepted"
                        and (
                            current := connection.execute(
                                """SELECT status FROM relation_assertions
                                   WHERE assertion_id = ?""",
                                (assertion_id,),
                            ).fetchone()
                        ) is not None
                        and str(current["status"]) == "archived"
                    )
                    changed = any(
                        int(outcome.get(key, 0)) != 0
                        for key in (
                            "assertions_updated",
                            "assertions_archived",
                            "edges_changed",
                            "edges_archived",
                            "term_morphemes_removed",
                        )
                    )
                    if changed:
                        self._mark_projection_repairs_for_entities(
                            connection,
                            (subject_entity_id, *kept_components),
                            timestamp=timestamp,
                        )
                        self._advance_relation_graph_revision(connection)
                    connection.commit()
                    return {
                        "assertions_retired": retired_assertions,
                        "legacy_edges_retired": int(
                            outcome.get("edges_archived", 0)
                        ),
                        "trailing_ordinals_removed": int(
                            outcome.get("term_morphemes_removed", 0)
                        ),
                    }
                current_assertions = {
                    str(row["assertion_id"])
                    for row in connection.execute(
                        """SELECT assertion_id FROM relation_assertions
                           WHERE subject_entity_id = ? AND status = 'accepted'
                             AND (
                                 relation = 'has-component'
                                 OR (
                                     relation = 'shares-component'
                                     AND (
                                         source_entity_id IN (
                                             SELECT entity_id FROM morphemes
                                         )
                                         AND source_entity_id NOT IN
                                             ({component_placeholders})
                                         OR target_entity_id IN (
                                             SELECT entity_id FROM morphemes
                                         )
                                         AND target_entity_id NOT IN
                                             ({component_placeholders})
                                     )
                                 )
                             )""".format(
                            component_placeholders=",".join(
                                "?" for _ in kept_components
                            )
                        ),
                        (
                            subject_entity_id,
                            *kept_components,
                            *kept_components,
                        ),
                    )
                }
                stale_assertions = sorted(current_assertions - kept_assertions)
                retired_assertions = 0
                authority_triples: set[tuple[str, str, str]] = set()
                if stale_assertions:
                    placeholders = ",".join("?" for _ in stale_assertions)
                    authority_triples.update(
                        (
                            str(row["source_entity_id"]),
                            str(row["target_entity_id"]),
                            str(row["relation"]),
                        )
                        for row in connection.execute(
                            f"""SELECT source_entity_id, target_entity_id, relation
                                FROM relation_assertions
                                WHERE assertion_id IN ({placeholders})""",
                            stale_assertions,
                        )
                    )
                    cursor = connection.execute(
                        f"""UPDATE relation_assertions
                            SET status = 'archived', revision = revision + 1,
                                updated_at = ?
                            WHERE assertion_id IN ({placeholders})
                              AND subject_entity_id = ? AND status = 'accepted'""",
                        (timestamp, *stale_assertions, subject_entity_id),
                    )
                    retired_assertions = int(cursor.rowcount)

                component_placeholders = ",".join("?" for _ in kept_components)
                edge_cursor = connection.execute(
                    f"""UPDATE entity_edges
                        SET status = 'archived', updated_at = ?
                        WHERE source_entity_id = ? AND relation = 'has-component'
                          AND status = 'accepted'
                          AND target_entity_id NOT IN ({component_placeholders})
                          AND NOT EXISTS (
                              SELECT 1 FROM relation_assertions AS history
                              WHERE history.source_entity_id =
                                    entity_edges.source_entity_id
                                AND history.target_entity_id =
                                    entity_edges.target_entity_id
                                AND history.relation = entity_edges.relation
                          )""",
                    (timestamp, subject_entity_id, *kept_components),
                )
                ordinal_cursor = connection.execute(
                    """DELETE FROM term_morphemes
                       WHERE term_id = ? AND ordinal >= ?""",
                    (subject_entity_id, component_count),
                )
                authority_changed = self._sync_relation_authority(
                    connection, authority_triples, timestamp=timestamp
                )
                if (
                    retired_assertions
                    or edge_cursor.rowcount
                    or ordinal_cursor.rowcount
                    or authority_changed
                ):
                    self._mark_projection_repairs_for_entities(
                        connection,
                        (subject_entity_id, *kept_components),
                        timestamp=timestamp,
                    )
                    self._advance_relation_graph_revision(connection)
                connection.commit()
                return {
                    "assertions_retired": retired_assertions,
                    "legacy_edges_retired": int(edge_cursor.rowcount),
                    "trailing_ordinals_removed": int(ordinal_cursor.rowcount),
                }
            except Exception:
                connection.rollback()
                raise

    def lexical_subgraph(
        self,
        subject_entity_id: str,
        mode: str = "word",
        limits: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic, bounded projection for one lexical subject."""

        subject_entity_id = subject_entity_id.strip()
        canonical_subject_entity_id = subject_entity_id
        display_subject_entity_id = subject_entity_id
        collapse_subject_wrapper = False
        mode = mode.strip().lower() or "word"
        if limits is not None and not isinstance(limits, dict):
            raise ValueError("subgraph limits must be an object")
        requested_limits = limits or {}

        def bounded_limit(
            primary: str, alias: str, default: int, minimum: int, maximum: int
        ) -> int:
            raw = requested_limits.get(primary, requested_limits.get(alias, default))
            try:
                value = int(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid subgraph {primary} limit") from error
            return max(minimum, min(value, maximum))

        max_nodes = bounded_limit("nodes", "max_nodes", 64, 1, 256)
        max_edges = bounded_limit("edges", "max_edges", 96, 0, 512)
        max_depth = bounded_limit("depth", "max_depth", 4, 0, 12)
        scan_limit = min(1024, max(64, max_edges * 4))
        evidence_limit = 8
        metadata_byte_limit = 4096
        origin_projection_exclusions = {
            "has-grammar",
            "has-language",
            "has-meaning",
            "has-period",
            "has-pronunciation",
            "has-translation",
            "in-language",
            "language",
            "language-of",
            "period",
            "renders-as",
        }

        def bounded_text(value: Any, byte_limit: int) -> str:
            encoded = str(value).encode("utf-8")
            if len(encoded) <= byte_limit:
                return str(value)
            return encoded[:byte_limit].decode("utf-8", errors="ignore")

        def bounded_projection_object(value: Any) -> dict[str, Any]:
            """Keep graph metadata useful while enforcing a hard JSON byte budget."""

            if not isinstance(value, dict):
                return {}

            def compact(item: Any, depth: int = 0) -> Any:
                if item is None or isinstance(item, (bool, int, float)):
                    return item
                if isinstance(item, str):
                    return bounded_text(item, 768 if depth == 0 else 384)
                if depth >= 3:
                    return bounded_text(
                        json.dumps(item, ensure_ascii=False, sort_keys=True), 256
                    )
                if isinstance(item, (list, tuple)):
                    return [compact(member, depth + 1) for member in item[:12]]
                if isinstance(item, dict):
                    result: dict[str, Any] = {}
                    for key in sorted(item, key=lambda candidate: str(candidate))[:24]:
                        result[bounded_text(key, 64)] = compact(item[key], depth + 1)
                    return result
                return bounded_text(item, 256)

            priority = (
                "kind",
                "language",
                "target_language",
                "meaning",
                "definition",
                "reading",
                "system",
                "transliteration",
                "usage_note",
                "part_of_speech",
                "period",
                "mode",
                "modes",
                "ordinal",
            )
            ordered_keys = [key for key in priority if key in value]
            ordered_keys.extend(
                sorted(
                    (key for key in value if key not in priority),
                    key=lambda candidate: str(candidate),
                )
            )
            result: dict[str, Any] = {}
            metadata_truncated = False
            for key in ordered_keys:
                clean_key = bounded_text(key, 64)
                trial = {**result, clean_key: compact(value[key])}
                encoded = json.dumps(
                    trial, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                if len(encoded) <= metadata_byte_limit:
                    result = trial
                else:
                    metadata_truncated = True
            if metadata_truncated:
                trial = {**result, "_truncated": True}
                if len(
                    json.dumps(
                        trial,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ) <= metadata_byte_limit:
                    result = trial
            return result

        requested_branch_focus = str(
            requested_limits.get("branch_focus_id", "")
        ).strip()[:200]

        with closing(self._connect()) as connection:
            subject = connection.execute(
                """SELECT entity.entity_id, entity.entity_type,
                          COALESCE(morpheme.kind, '') AS morpheme_kind
                   FROM entities AS entity
                   LEFT JOIN morphemes AS morpheme
                     ON morpheme.entity_id = entity.entity_id
                   WHERE entity.entity_id = ? AND entity.status = 'accepted'""",
                (subject_entity_id,),
            ).fetchone()
            if subject is None:
                raise ValueError(f"unknown accepted lexical subject: {subject_entity_id!r}")

            branch_focus_id = ""
            expected_branch_kinds = (
                {"root"}
                if mode == "root"
                else {"affix", "prefix", "suffix", "infix", "circumfix"}
            )
            if mode in {"root", "affix"}:
                if requested_branch_focus:
                    focus = connection.execute(
                        """SELECT entity.entity_id, morpheme.kind
                           FROM entities AS entity
                           JOIN morphemes AS morpheme
                             ON morpheme.entity_id = entity.entity_id
                           WHERE entity.entity_id = ?
                             AND entity.status = 'accepted'""",
                        (requested_branch_focus,),
                    ).fetchone()
                    if (
                        focus is not None
                        and _normalise(str(focus["kind"]))
                        in expected_branch_kinds
                    ):
                        branch_focus_id = str(focus["entity_id"])
                elif (
                    str(subject["entity_type"]) == "morpheme"
                    and _normalise(str(subject["morpheme_kind"]))
                    in expected_branch_kinds
                ):
                    branch_focus_id = subject_entity_id

            owned_component_ids = {
                str(row["component_id"])
                for row in connection.execute(
                    """SELECT assertion.target_entity_id AS component_id
                       FROM relation_assertions AS assertion
                       JOIN relation_assertion_slots AS authority_slot
                         ON authority_slot.assertion_id = assertion.assertion_id
                       JOIN entities AS component
                         ON component.entity_id = assertion.target_entity_id
                        AND component.entity_type = 'morpheme'
                        AND component.status = 'accepted'
                       WHERE assertion.status = 'accepted'
                         AND assertion.relation = 'has-component'
                         AND assertion.source_entity_id = ?
                       UNION
                       SELECT edge.target_entity_id AS component_id
                       FROM entity_edges AS edge
                       JOIN entities AS component
                         ON component.entity_id = edge.target_entity_id
                        AND component.entity_type = 'morpheme'
                        AND component.status = 'accepted'
                       WHERE edge.status = 'accepted'
                         AND edge.relation = 'has-component'
                         AND edge.source_entity_id = ?
                         AND NOT EXISTS (
                             SELECT 1 FROM relation_assertions AS history
                             WHERE history.source_entity_id = edge.source_entity_id
                               AND history.target_entity_id = edge.target_entity_id
                               AND history.relation = edge.relation
                         )""",
                    (subject_entity_id, subject_entity_id),
                )
            }

            has_positional_components = bool(owned_component_ids)
            owned_component_ids.update(
                str(component["morpheme_id"])
                for component in self.accepted_morphology_components(
                    subject_entity_id, include_partial=True,
                )
            )

            # Safe morphology-v3 repair deliberately archives legacy unknown
            # and cross-clause links.  A projection must not resurrect those
            # rows, but it can still begin from deterministic, cited structure
            # that remains in the normalized store.  These seeds exist only in
            # the returned view; canonical storage is never changed here.
            projection_seed_candidates: list[dict[str, Any]] = []

            def add_projection_seed(
                source_id: str,
                target_id: str,
                relation: str,
                *,
                evidence_ids: Iterable[str],
                confidence: float,
                properties: dict[str, Any],
            ) -> None:
                clean_evidence_ids = list(
                    dict.fromkeys(
                        str(evidence_id).strip()
                        for evidence_id in evidence_ids
                        if str(evidence_id).strip()
                    )
                )[:evidence_limit]
                if not clean_evidence_ids or source_id == target_id:
                    return
                seed_key = "|".join(
                    (
                        subject_entity_id,
                        source_id,
                        target_id,
                        relation,
                        *clean_evidence_ids,
                    )
                )
                support = {
                    "status": "cited-projection-recovery",
                    "assertion_evidence": True,
                    "subject_context_evidence": True,
                    "accepted_unverified": False,
                    "model_generated": False,
                    "ownership": "deterministic-book-structure",
                }
                projection_seed_candidates.append(
                    {
                        "candidate_id": "projection-book-recovery-"
                        + hashlib.sha256(seed_key.encode("utf-8")).hexdigest()[:24],
                        "assertion_id": "",
                        "subject_entity_id": subject_entity_id,
                        "source_entity_id": source_id,
                        "target_entity_id": target_id,
                        "relation": relation,
                        "basis": "book",
                        "confidence": max(0.0, min(float(confidence), 1.0)),
                        "properties": bounded_projection_object(
                            {
                                **properties,
                                "modes": ["knowledge", "word", "root", "affix"],
                                "projection_only": True,
                                "projection_support": support,
                            }
                        ),
                        "support_status": support["status"],
                        "verified": True,
                        "context_supported": True,
                        "model_generated": False,
                        "declared_evidence_ids": clean_evidence_ids,
                        "revision": 1,
                        "created_at": "",
                        "updated_at": "",
                    }
                )

            if branch_focus_id:
                owned_component_ids.add(branch_focus_id)
                focus_row = connection.execute(
                    """SELECT language, normalized FROM morphemes
                       WHERE entity_id = ?""",
                    (branch_focus_id,),
                ).fetchone()
                if focus_row is not None:
                    peer_rows = connection.execute(
                        """SELECT peer.entity_id,
                                  COUNT(DISTINCT assertion.assertion_id) AS support_count,
                                  MAX(assertion.confidence) AS confidence,
                                  GROUP_CONCAT(DISTINCT evidence.evidence_id) AS evidence_ids
                           FROM morphemes AS peer
                           JOIN entities AS peer_entity
                             ON peer_entity.entity_id = peer.entity_id
                            AND peer_entity.status = 'accepted'
                           JOIN relation_assertions AS assertion
                             ON assertion.status = 'accepted'
                            AND assertion.basis IN ('book', 'reviewed')
                            AND assertion.relation IN (
                                'developed-into', 'developed-toward',
                                'explains-component', 'has-history'
                            )
                            AND (
                                assertion.source_entity_id = peer.entity_id
                                OR assertion.target_entity_id = peer.entity_id
                                OR json_extract(
                                    assertion.properties_json, '$.component_id'
                                ) = peer.entity_id
                                OR json_extract(
                                    assertion.properties_json,
                                    '$.component_entity_id'
                                ) = peer.entity_id
                                OR json_extract(
                                    assertion.properties_json, '$.morpheme_id'
                                ) = peer.entity_id
                            )
                           JOIN assertion_evidence AS evidence
                             ON evidence.assertion_id = assertion.assertion_id
                           WHERE peer.entity_id <> ?
                             AND peer.language = ? AND peer.normalized = ?
                             AND peer.kind <> 'unknown'
                           GROUP BY peer.entity_id
                           ORDER BY support_count DESC, confidence DESC,
                                    peer.entity_id
                           LIMIT 1""",
                        (
                            branch_focus_id,
                            str(focus_row["language"]),
                            str(focus_row["normalized"]),
                        ),
                    ).fetchall()
                    for peer in peer_rows:
                        peer_id = str(peer["entity_id"])
                        owned_component_ids.add(peer_id)
                        add_projection_seed(
                            branch_focus_id,
                            peer_id,
                            "has-history",
                            evidence_ids=str(peer["evidence_ids"] or "").split(","),
                            confidence=float(peer["confidence"] or 0.0),
                            properties={
                                "component_id": branch_focus_id,
                                "recovery_peer_id": peer_id,
                                "recovery_kind": "exact-form-cited-history-peer",
                            },
                        )

            v3_observations_available = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'table'
                     AND name = 'morphology_source_observations_v3'"""
            ).fetchone()
            if (
                mode == "word"
                and not has_positional_components
                and v3_observations_available is not None
            ):
                term_row = connection.execute(
                    "SELECT normalized FROM terms WHERE entity_id = ?",
                    (subject_entity_id,),
                ).fetchone()
                term_form = (
                    "".join(
                        character
                        for character in _normalise(str(term_row["normalized"]))
                        if character.isalnum()
                    )
                    if term_row is not None
                    else ""
                )
                source_rows = connection.execute(
                    """SELECT DISTINCT source.corpus_id, source.source_kind,
                                      source.source_entry_id,
                                      term.evidence_id
                       FROM morphology_term_observations_v3 AS term
                       JOIN morphology_source_observations_v3 AS source
                         ON source.source_observation_id = term.source_observation_id
                       WHERE term.materialized_entity_id = ?
                         AND term.state = 'materialized'
                       ORDER BY source.corpus_id, source.source_entry_id""",
                    (subject_entity_id,),
                ).fetchall()
                recovered_components: dict[str, dict[str, Any]] = {}
                for source_row in source_rows:
                    entry_id = str(source_row["source_entry_id"])
                    group_match = re.fullmatch(r"(.+)-r\d+", entry_id)
                    if not group_match:
                        continue
                    group_key = group_match.group(1)
                    rows = connection.execute(
                        """SELECT component.materialized_entity_id AS component_id,
                                  morpheme.normalized, morpheme.kind,
                                  MAX(link.confidence) AS confidence,
                                  GROUP_CONCAT(
                                      DISTINCT evidence.evidence_id
                                  ) AS evidence_ids,
                                  GROUP_CONCAT(
                                      DISTINCT assertion.assertion_id
                                  ) AS assertion_ids
                           FROM morphology_source_observations_v3 AS source
                           JOIN morphology_formula_links_v3 AS link
                             ON link.source_observation_id =
                                source.source_observation_id
                            AND link.state = 'materialized'
                           JOIN morphology_term_observations_v3 AS term
                             ON term.term_observation_id = link.term_observation_id
                            AND term.state = 'materialized'
                           JOIN morphology_component_observations_v3 AS component
                             ON component.component_observation_id =
                                link.component_observation_id
                            AND component.state = 'materialized'
                           JOIN morphemes AS morpheme
                             ON morpheme.entity_id =
                                component.materialized_entity_id
                            AND morpheme.kind <> 'unknown'
                           JOIN entities AS component_entity
                             ON component_entity.entity_id = morpheme.entity_id
                            AND component_entity.status = 'accepted'
                           JOIN relation_assertions AS assertion
                             ON assertion.source_entity_id =
                                term.materialized_entity_id
                            AND assertion.target_entity_id = morpheme.entity_id
                            AND assertion.relation = 'has-component'
                            AND assertion.status = 'accepted'
                            AND assertion.basis = 'book'
                           JOIN assertion_evidence AS evidence
                             ON evidence.assertion_id = assertion.assertion_id
                           WHERE source.corpus_id = ?
                             AND source.source_kind = ?
                             AND source.source_entry_id GLOB ?
                           GROUP BY component.materialized_entity_id,
                                    morpheme.normalized, morpheme.kind
                           ORDER BY confidence DESC,
                                    component.materialized_entity_id""",
                        (
                            str(source_row["corpus_id"]),
                            str(source_row["source_kind"]),
                            f"{group_key}-r[0-9]*",
                        ),
                    ).fetchall()
                    for row in rows:
                        component_form = "".join(
                            character
                            for character in _normalise(str(row["normalized"]))
                            if character.isalnum()
                        )
                        if (
                            len(component_form) < 3
                            or component_form == term_form
                            or component_form not in term_form
                        ):
                            continue
                        component_id = str(row["component_id"])
                        evidence_ids = list(
                            dict.fromkeys(
                                (
                                    str(source_row["evidence_id"]),
                                    *str(row["evidence_ids"] or "").split(","),
                                )
                            )
                        )
                        candidate = {
                            "component_id": component_id,
                            "form": component_form,
                            "kind": str(row["kind"]),
                            "confidence": float(row["confidence"] or 0.0),
                            "evidence_ids": evidence_ids,
                            "assertion_ids": str(
                                row["assertion_ids"] or ""
                            ).split(","),
                            "source_group": group_key,
                        }
                        existing = recovered_components.get(component_id)
                        if existing is None or (
                            len(component_form), candidate["confidence"]
                        ) > (len(str(existing["form"])), existing["confidence"]):
                            recovered_components[component_id] = candidate
                    # A component formula can remain deliberately Qwen-ready
                    # while the book's row-group structure independently names
                    # an already typed, accepted morpheme.  Exact containment
                    # plus entity evidence in the same editorial group is
                    # deterministic; it does not promote the ambiguous formula
                    # observation or revive its archived legacy edge.
                    typed_rows = connection.execute(
                        """SELECT morpheme.entity_id AS component_id,
                                  morpheme.normalized, morpheme.kind,
                                  MAX(COALESCE(entity.quality_score, 0.85))
                                      AS confidence,
                                  GROUP_CONCAT(
                                      DISTINCT link.evidence_id
                                  ) AS evidence_ids
                           FROM morphemes AS morpheme
                           JOIN entities AS entity
                             ON entity.entity_id = morpheme.entity_id
                            AND entity.status = 'accepted'
                           JOIN entity_evidence AS link
                             ON link.entity_id = morpheme.entity_id
                           JOIN evidence_records AS evidence
                             ON evidence.evidence_id = link.evidence_id
                           WHERE evidence.corpus_id = ?
                             AND evidence.source_entry_id GLOB ?
                             AND morpheme.kind <> 'unknown'
                           GROUP BY morpheme.entity_id, morpheme.normalized,
                                    morpheme.kind
                           ORDER BY confidence DESC, morpheme.entity_id""",
                        (
                            str(source_row["corpus_id"]),
                            f"{group_key}-r[0-9]*",
                        ),
                    ).fetchall()
                    for row in typed_rows:
                        component_form = "".join(
                            character
                            for character in _normalise(str(row["normalized"]))
                            if character.isalnum()
                        )
                        if (
                            len(component_form) < 3
                            or component_form == term_form
                            or component_form not in term_form
                        ):
                            continue
                        component_id = str(row["component_id"])
                        evidence_ids = list(
                            dict.fromkeys(
                                (
                                    str(source_row["evidence_id"]),
                                    *str(row["evidence_ids"] or "").split(","),
                                )
                            )
                        )
                        candidate = {
                            "component_id": component_id,
                            "form": component_form,
                            "kind": str(row["kind"]),
                            "confidence": float(row["confidence"] or 0.85),
                            "evidence_ids": evidence_ids,
                            "assertion_ids": [],
                            "source_group": group_key,
                        }
                        existing = recovered_components.get(component_id)
                        if existing is None or (
                            len(component_form), candidate["confidence"]
                        ) > (len(str(existing["form"])), existing["confidence"]):
                            recovered_components[component_id] = candidate
                occupied_spans: list[tuple[int, int]] = []
                for candidate in sorted(
                    recovered_components.values(),
                    key=lambda item: (
                        -len(str(item["form"])),
                        -float(item["confidence"]),
                        str(item["component_id"]),
                    ),
                ):
                    start = term_form.find(str(candidate["form"]))
                    span = (start, start + len(str(candidate["form"])))
                    if start < 0 or any(
                        span[0] < occupied[1] and occupied[0] < span[1]
                        for occupied in occupied_spans
                    ):
                        continue
                    component_id = str(candidate["component_id"])
                    owned_component_ids.add(component_id)
                    occupied_spans.append(span)
                    add_projection_seed(
                        subject_entity_id,
                        component_id,
                        "has-component",
                        evidence_ids=candidate["evidence_ids"],
                        confidence=float(candidate["confidence"]),
                        properties={
                            "component_id": component_id,
                            "component_kind": str(candidate["kind"]),
                            "source_group": str(candidate["source_group"]),
                            "supporting_assertion_ids": candidate["assertion_ids"],
                            "recovery_kind": "structured-book-group-membership",
                        },
                    )
                    if len(occupied_spans) >= 4:
                        break

            def seek_assertions(
                endpoint: str,
                endpoint_ids: Iterable[str],
                per_endpoint_limit: int,
            ) -> tuple[list[sqlite3.Row], bool]:
                other_endpoint = (
                    "target_entity_id"
                    if endpoint == "source_entity_id"
                    else "source_entity_id"
                )
                branch_clause = ""
                branch_scope_ids = sorted(
                    owned_component_ids | ({branch_focus_id} if branch_focus_id else set())
                )
                if branch_focus_id:
                    branch_placeholders = ",".join("?" for _ in branch_scope_ids)
                    branch_clause = f"""AND (
                        assertion.relation NOT IN ('has-component', 'shares-component')
                        OR assertion.source_entity_id IN ({branch_placeholders})
                        OR assertion.target_entity_id IN ({branch_placeholders})
                    )"""
                rows: list[sqlite3.Row] = []
                capped = False
                for endpoint_id in endpoint_ids:
                    parameters: list[Any] = [
                        subject_entity_id,
                        subject_entity_id,
                        endpoint_id,
                    ]
                    if branch_focus_id:
                        parameters.extend((*branch_scope_ids, *branch_scope_ids))
                    parameters.append(per_endpoint_limit + 1)
                    batch = connection.execute(
                        f"""SELECT assertion.assertion_id AS candidate_id,
                                   assertion.assertion_id,
                                   assertion.subject_entity_id,
                                   assertion.source_entity_id,
                                   assertion.target_entity_id, assertion.relation,
                                   assertion.basis, assertion.confidence,
                                   assertion.properties_json, assertion.revision,
                                   assertion.created_at, assertion.updated_at,
                                   EXISTS (
                                       SELECT 1 FROM assertion_evidence AS direct
                                       WHERE direct.assertion_id = assertion.assertion_id
                                   ) AS has_assertion_evidence,
                                   (
                                       EXISTS (
                                           SELECT 1
                                           FROM entity_evidence AS endpoint_evidence
                                           JOIN entity_evidence AS subject_evidence
                                             ON subject_evidence.evidence_id =
                                                endpoint_evidence.evidence_id
                                            AND subject_evidence.entity_id = ?
                                           WHERE endpoint_evidence.entity_id =
                                                 assertion.source_entity_id
                                       )
                                       AND EXISTS (
                                           SELECT 1
                                           FROM entity_evidence AS endpoint_evidence
                                           JOIN entity_evidence AS subject_evidence
                                             ON subject_evidence.evidence_id =
                                                endpoint_evidence.evidence_id
                                            AND subject_evidence.entity_id = ?
                                           WHERE endpoint_evidence.entity_id =
                                                 assertion.target_entity_id
                                       )
                                   ) AS has_subject_endpoint_evidence
                            FROM relation_assertions AS assertion
                            JOIN relation_assertion_slots AS authority_slot
                              ON authority_slot.assertion_id = assertion.assertion_id
                            JOIN entities AS source
                              ON source.entity_id = assertion.source_entity_id
                             AND source.status = 'accepted'
                            JOIN entities AS target
                              ON target.entity_id = assertion.target_entity_id
                             AND target.status = 'accepted'
                            WHERE assertion.status = 'accepted'
                              AND assertion.{endpoint} = ?
                              {branch_clause}
                            ORDER BY assertion.confidence DESC,
                                     assertion.relation,
                                     assertion.{other_endpoint},
                                     assertion.assertion_id
                            LIMIT ?""",
                        parameters,
                    ).fetchall()
                    if len(batch) > per_endpoint_limit:
                        capped = True
                    rows.extend(batch[:per_endpoint_limit])
                return rows, capped

            def seek_canonical_edges(
                endpoint: str,
                endpoint_ids: Iterable[str],
                per_endpoint_limit: int,
            ) -> tuple[list[sqlite3.Row], bool]:
                other_endpoint = (
                    "target_entity_id"
                    if endpoint == "source_entity_id"
                    else "source_entity_id"
                )
                branch_clause = ""
                branch_scope_ids = sorted(
                    owned_component_ids | ({branch_focus_id} if branch_focus_id else set())
                )
                if branch_focus_id:
                    branch_placeholders = ",".join("?" for _ in branch_scope_ids)
                    branch_clause = f"""AND (
                        edge.relation NOT IN ('has-component', 'shares-component')
                        OR edge.source_entity_id IN ({branch_placeholders})
                        OR edge.target_entity_id IN ({branch_placeholders})
                    )"""
                rows: list[sqlite3.Row] = []
                capped = False
                for endpoint_id in endpoint_ids:
                    parameters: list[Any] = [
                        subject_entity_id,
                        subject_entity_id,
                        endpoint_id,
                    ]
                    if branch_focus_id:
                        parameters.extend((*branch_scope_ids, *branch_scope_ids))
                    parameters.append(per_endpoint_limit + 1)
                    batch = connection.execute(
                        f"""SELECT edge.edge_id AS candidate_id,
                                   '' AS assertion_id, '' AS subject_entity_id,
                                   edge.source_entity_id, edge.target_entity_id,
                                   edge.relation, edge.basis, edge.confidence,
                                   edge.properties AS properties_json,
                                   1 AS revision, edge.created_at, edge.updated_at,
                                   0 AS has_assertion_evidence,
                                   (
                                       EXISTS (
                                           SELECT 1
                                           FROM entity_evidence AS endpoint_evidence
                                           JOIN entity_evidence AS subject_evidence
                                             ON subject_evidence.evidence_id =
                                                endpoint_evidence.evidence_id
                                            AND subject_evidence.entity_id = ?
                                           WHERE endpoint_evidence.entity_id =
                                                 edge.source_entity_id
                                       )
                                       AND EXISTS (
                                           SELECT 1
                                           FROM entity_evidence AS endpoint_evidence
                                           JOIN entity_evidence AS subject_evidence
                                             ON subject_evidence.evidence_id =
                                                endpoint_evidence.evidence_id
                                            AND subject_evidence.entity_id = ?
                                           WHERE endpoint_evidence.entity_id =
                                                 edge.target_entity_id
                                       )
                                   ) AS has_subject_endpoint_evidence
                            FROM entity_edges AS edge
                            JOIN entities AS source
                              ON source.entity_id = edge.source_entity_id
                             AND source.status = 'accepted'
                            JOIN entities AS target
                              ON target.entity_id = edge.target_entity_id
                             AND target.status = 'accepted'
                            WHERE edge.status = 'accepted'
                              AND edge.{endpoint} = ?
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM relation_assertions AS history
                                  WHERE history.source_entity_id =
                                        edge.source_entity_id
                                    AND history.target_entity_id =
                                        edge.target_entity_id
                                    AND history.relation = edge.relation
                              )
                              {branch_clause}
                            ORDER BY edge.confidence DESC, edge.relation,
                                     edge.{other_endpoint}, edge.edge_id
                            LIMIT ?""",
                        parameters,
                    ).fetchall()
                    if len(batch) > per_endpoint_limit:
                        capped = True
                    rows.extend(batch[:per_endpoint_limit])
                return rows, capped

            node_depth = {subject_entity_id: 0}
            frontier = {subject_entity_id}
            if (
                branch_focus_id
                and branch_focus_id != subject_entity_id
                and max_nodes > 1
            ):
                node_depth[branch_focus_id] = 0
                frontier.add(branch_focus_id)
            selected: dict[str, dict[str, Any]] = {}
            selected_triples: set[tuple[str, str, str]] = set()
            traversal_truncated = False
            for depth in range(max_depth):
                if not frontier or len(selected) >= max_edges:
                    break
                ordered_frontier = sorted(frontier)
                per_endpoint_limit = max(
                    8,
                    min(256, scan_limit // max(1, len(ordered_frontier)) + 8),
                )
                assertion_rows: list[sqlite3.Row] = []
                canonical_rows: list[sqlite3.Row] = []
                for endpoint in ("source_entity_id", "target_entity_id"):
                    batch, capped = seek_assertions(
                        endpoint, ordered_frontier, per_endpoint_limit
                    )
                    assertion_rows.extend(batch)
                    traversal_truncated = traversal_truncated or capped
                    batch, capped = seek_canonical_edges(
                        endpoint, ordered_frontier, per_endpoint_limit
                    )
                    canonical_rows.extend(batch)
                    traversal_truncated = traversal_truncated or capped
                candidates: list[dict[str, Any]] = []
                seen_candidate_ids: set[tuple[bool, str]] = set()
                for row in (*assertion_rows, *canonical_rows):
                    assertion_id = str(row["assertion_id"])
                    candidate_key = (
                        bool(assertion_id),
                        str(row["candidate_id"]),
                    )
                    if candidate_key in seen_candidate_ids:
                        continue
                    seen_candidate_ids.add(candidate_key)
                    relation = str(row["relation"])
                    if (
                        mode in {"word", "root", "affix"}
                        and relation in origin_projection_exclusions
                    ):
                        continue
                    source_id = str(row["source_entity_id"])
                    target_id = str(row["target_entity_id"])
                    if (
                        branch_focus_id
                        and relation in {"has-component", "shares-component"}
                        and not (
                            {source_id, target_id}
                            & owned_component_ids
                        )
                    ):
                        continue
                    raw_properties = json.loads(row["properties_json"])
                    if not isinstance(raw_properties, dict):
                        raw_properties = {}
                    declared_evidence_ids = []
                    if str(row["basis"]).strip().casefold() != "model":
                        raw_evidence_ids = raw_properties.get("evidence_ids", [])
                        if isinstance(raw_evidence_ids, (list, tuple, set)):
                            declared_evidence_ids = list(
                                dict.fromkeys(
                                    str(evidence_id).strip()
                                    for evidence_id in raw_evidence_ids
                                    if str(evidence_id).strip()
                                )
                            )[:evidence_limit]
                    direct_evidence = bool(row["has_assertion_evidence"]) or bool(
                        declared_evidence_ids
                    )
                    context_evidence = bool(row["has_subject_endpoint_evidence"])
                    basis = str(row["basis"]).strip().casefold()
                    model_generated = basis == "model"
                    if direct_evidence:
                        support_status = "cited-assertion"
                    elif context_evidence:
                        support_status = (
                            "model-context-supported"
                            if model_generated
                            else "context-supported"
                        )
                    elif model_generated:
                        support_status = "accepted-model-unverified"
                    else:
                        support_status = "accepted-uncited"
                    if mode in {"word", "root", "affix"}:
                        declared_owners = {
                            owner
                            for owner in (
                                str(row["subject_entity_id"]).strip(),
                                str(raw_properties.get("source_term_id", "")).strip(),
                                str(raw_properties.get("term_id", "")).strip(),
                            )
                            if owner
                        }
                        component_owners = {
                            str(raw_properties.get(key, "")).strip()
                            for key in (
                                "component_id",
                                "component_entity_id",
                                "morpheme_id",
                            )
                            if str(raw_properties.get(key, "")).strip()
                        }
                        structurally_owned = (
                            relation == "has-component"
                            and target_id in owned_component_ids
                        ) or (
                            bool(component_owners & owned_component_ids)
                        ) or (
                            relation
                            in {
                                "developed-into",
                                "developed-toward",
                                "explains-component",
                                "has-history",
                            }
                            and bool(
                                {source_id, target_id} & owned_component_ids
                            )
                        )
                        if (
                            any(
                                owner != subject_entity_id
                                for owner in declared_owners
                            )
                            and not structurally_owned
                        ):
                            continue
                        explicitly_owned = (
                            subject_entity_id in declared_owners
                            or structurally_owned
                        )
                        if not (
                            direct_evidence
                            or context_evidence
                            or explicitly_owned
                        ):
                            continue
                        raw_properties = dict(raw_properties)
                        raw_properties["projection_support"] = {
                            "status": support_status,
                            "assertion_evidence": direct_evidence,
                            "subject_context_evidence": context_evidence,
                            "accepted_unverified": not direct_evidence,
                            "model_generated": model_generated,
                            "ownership": (
                                "declared-subject"
                                if subject_entity_id in declared_owners
                                else (
                                    "accepted-component-path"
                                    if structurally_owned
                                    else "evidence-context"
                                )
                            ),
                        }
                    declared_modes = raw_properties.get(
                        "modes", raw_properties.get("mode", ())
                    )
                    if isinstance(declared_modes, str):
                        declared_modes = [declared_modes]
                    if isinstance(declared_modes, (list, tuple, set)):
                        normalized_modes = {
                            str(item).strip().lower()
                            for item in declared_modes
                            if str(item).strip()
                        }
                        if (
                            normalized_modes
                            and mode not in normalized_modes
                            and mode != "all"
                        ):
                            continue
                    candidates.append(
                        {
                            **dict(row),
                            "properties": bounded_projection_object(raw_properties),
                            "support_status": support_status,
                            "verified": direct_evidence,
                            "context_supported": context_evidence,
                            "model_generated": model_generated,
                            "declared_evidence_ids": declared_evidence_ids,
                        }
                    )
                if depth == 0:
                    candidates.extend(projection_seed_candidates)
                candidates.sort(
                    key=lambda item: (
                        str(item["source_entity_id"]),
                        str(item["target_entity_id"]),
                        str(item["relation"]),
                        not bool(str(item["assertion_id"])),
                        str(item["subject_entity_id"]) != subject_entity_id,
                        -float(item["confidence"]),
                        str(item["candidate_id"]),
                    )
                )
                eligible_candidates: list[dict[str, Any]] = []
                eligible_triples: set[tuple[str, str, str]] = set()
                for candidate in candidates:
                    triple = (
                        str(candidate["source_entity_id"]),
                        str(candidate["target_entity_id"]),
                        str(candidate["relation"]),
                    )
                    if triple in eligible_triples:
                        continue
                    eligible_triples.add(triple)
                    eligible_candidates.append(candidate)
                candidates = eligible_candidates

                # Root/Affix branch slides have a small node budget.  Prefer a
                # derivative whose accepted meaning can actually be taught
                # before an otherwise eligible empty derivative consumes that
                # budget.  Apply this at BFS admission, because the later
                # branch ordering cannot recover a node discarded here.
                capacity_meanings: dict[str, list[dict[str, Any]]] = {}
                if mode in {"root", "affix"}:
                    derivative_neighbor_ids = {
                        endpoint_id
                        for candidate in candidates
                        if str(candidate["relation"])
                        in {"has-component", "shares-component"}
                        for endpoint_id in (
                            str(candidate["source_entity_id"]),
                            str(candidate["target_entity_id"]),
                        )
                        if endpoint_id not in node_depth
                    }
                    capacity_meanings = self._accepted_meaning_records(
                        connection, derivative_neighbor_ids
                    )

                def derivative_capacity_rank(candidate: dict[str, Any]) -> int:
                    if (
                        mode not in {"root", "affix"}
                        or str(candidate["relation"])
                        not in {"has-component", "shares-component"}
                    ):
                        return 0
                    new_neighbors = (
                        str(candidate["source_entity_id"]),
                        str(candidate["target_entity_id"]),
                    )
                    new_neighbors = tuple(
                        entity_id
                        for entity_id in new_neighbors
                        if entity_id not in node_depth
                    )
                    if not new_neighbors:
                        return 0
                    return 0 if any(
                        capacity_meanings.get(entity_id)
                        for entity_id in new_neighbors
                    ) else 1

                candidates.sort(
                    key=lambda item: (
                        str(item["subject_entity_id"]) != subject_entity_id,
                        derivative_capacity_rank(item),
                        -float(item["confidence"]),
                        str(item["relation"]),
                        str(item["source_entity_id"]),
                        str(item["target_entity_id"]),
                        str(item["candidate_id"]),
                    )
                )
                if len(candidates) > scan_limit:
                    traversal_truncated = True
                    candidates = candidates[:scan_limit]
                next_frontier: set[str] = set()
                for candidate in candidates:
                    candidate_id = str(candidate["candidate_id"])
                    source_id = str(candidate["source_entity_id"])
                    target_id = str(candidate["target_entity_id"])
                    triple = (source_id, target_id, str(candidate["relation"]))
                    if candidate_id in selected or triple in selected_triples:
                        continue
                    if source_id in frontier:
                        other = target_id
                    elif target_id in frontier:
                        other = source_id
                    else:
                        continue
                    if other not in node_depth:
                        if len(node_depth) >= max_nodes:
                            traversal_truncated = True
                            continue
                        node_depth[other] = depth + 1
                        next_frontier.add(other)
                    selected[candidate_id] = candidate
                    selected_triples.add(triple)
                    if len(selected) >= max_edges:
                        traversal_truncated = True
                        break
                frontier = next_frontier
                if depth + 1 >= max_depth and frontier:
                    traversal_truncated = True

            selected_ids = sorted(selected)
            selected_assertion_ids = sorted(
                {
                    str(candidate["assertion_id"])
                    for candidate in selected.values()
                    if str(candidate["assertion_id"])
                }
            )
            evidence_by_assertion: dict[str, list[str]] = {}
            evidence_truncated_by_assertion: dict[str, bool] = {}
            for assertion_id in selected_assertion_ids:
                links = connection.execute(
                    """SELECT evidence_id FROM assertion_evidence
                       WHERE assertion_id = ?
                       ORDER BY evidence_id LIMIT ?""",
                    (assertion_id, evidence_limit + 1),
                ).fetchall()
                evidence_truncated_by_assertion[assertion_id] = (
                    len(links) > evidence_limit
                )
                evidence_by_assertion[assertion_id] = [
                    str(link["evidence_id"]) for link in links[:evidence_limit]
                ]

            requested_declared_evidence = sorted(
                {
                    str(evidence_id)
                    for candidate in selected.values()
                    for evidence_id in candidate.get(
                        "declared_evidence_ids", []
                    )
                    if str(evidence_id)
                }
            )
            valid_declared_evidence: set[str] = set()
            if requested_declared_evidence:
                placeholders = ",".join(
                    "?" for _ in requested_declared_evidence
                )
                valid_declared_evidence = {
                    str(row["evidence_id"])
                    for row in connection.execute(
                        f"""SELECT evidence_id FROM evidence_records
                            WHERE evidence_id IN ({placeholders})""",
                        requested_declared_evidence,
                    )
                }

            endpoint_evidence_by_entity: dict[str, list[str]] = {}
            endpoint_evidence_truncated: set[str] = set()
            if mode == "word" and selected:
                endpoint_ids = sorted(
                    {
                        str(candidate[endpoint])
                        for candidate in selected.values()
                        for endpoint in (
                            "source_entity_id",
                            "target_entity_id",
                        )
                    }
                )
                endpoint_placeholders = ",".join("?" for _ in endpoint_ids)
                support_rows = connection.execute(
                    f"""WITH support AS (
                            SELECT DISTINCT endpoint.entity_id,
                                            endpoint.evidence_id
                            FROM entity_evidence AS endpoint
                            JOIN entity_evidence AS subject
                              ON subject.evidence_id = endpoint.evidence_id
                             AND subject.entity_id = ?
                            WHERE endpoint.entity_id IN ({endpoint_placeholders})
                        ), ranked AS (
                            SELECT entity_id, evidence_id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY entity_id ORDER BY evidence_id
                                   ) AS ordinal,
                                   COUNT(*) OVER (
                                       PARTITION BY entity_id
                                   ) AS support_count
                            FROM support
                        )
                        SELECT entity_id, evidence_id, support_count
                        FROM ranked WHERE ordinal <= ?
                        ORDER BY entity_id, ordinal""",
                    (
                        subject_entity_id,
                        *endpoint_ids,
                        evidence_limit + 1,
                    ),
                ).fetchall()
                for row in support_rows:
                    entity_id = str(row["entity_id"])
                    if int(row["support_count"]) > evidence_limit:
                        endpoint_evidence_truncated.add(entity_id)
                    bucket = endpoint_evidence_by_entity.setdefault(entity_id, [])
                    if len(bucket) < evidence_limit:
                        bucket.append(str(row["evidence_id"]))

            node_ids = sorted(node_depth)
            node_placeholders = ",".join("?" for _ in node_ids)
            nodes = [
                {
                    "id": str(row["entity_id"]),
                    "type": str(row["entity_type"]),
                    "key": str(row["canonical_key"]),
                    "label": str(row["label"]),
                    "quality": row["quality_score"],
                    "payload": bounded_projection_object(json.loads(row["payload"])),
                }
                for row in connection.execute(
                    f"""SELECT entity_id, entity_type, canonical_key, label,
                               quality_score, payload
                        FROM entities WHERE entity_id IN ({node_placeholders})
                        ORDER BY entity_id""",
                    node_ids,
                )
            ]
            if mode in {"root", "affix"}:
                term_ids = [
                    str(node["id"])
                    for node in nodes
                    if str(node.get("type", "")) == "term"
                ]
                meaning_by_term: dict[str, dict[str, str]] = {}
                if term_ids:
                    accepted_meanings = self._accepted_meaning_records(
                        connection, term_ids
                    )
                    for term_id in term_ids:
                        records = accepted_meanings.get(term_id, [])
                        if not records:
                            continue
                        record = records[0]
                        definition = " ".join(
                            str(record["definition"]).split()
                        )
                        if len(definition) > 112:
                            clipped = definition[:113].rsplit(" ", 1)[0]
                            definition = f"{clipped.rstrip(' ,;:-')}..."
                        meaning_by_term[term_id] = {
                            "meaning": definition,
                            "meaning_language": str(record["language"]),
                        }
                for node in nodes:
                    grounded_meaning = meaning_by_term.get(str(node["id"]))
                    if grounded_meaning is None:
                        continue
                    payload = node.get("payload", {})
                    payload = dict(payload) if isinstance(payload, dict) else {}
                    payload.update(grounded_meaning)
                    node["payload"] = bounded_projection_object(payload)
            edges: list[dict[str, Any]] = []
            for candidate_id in selected_ids:
                candidate = selected[candidate_id]
                assertion_id = str(candidate["assertion_id"])
                evidence_ids = list(
                    evidence_by_assertion.get(assertion_id, [])
                )
                for evidence_id in candidate.get("declared_evidence_ids", []):
                    clean_id = str(evidence_id)
                    if (
                        clean_id in valid_declared_evidence
                        and clean_id not in evidence_ids
                    ):
                        evidence_ids.append(clean_id)
                evidence_ids = evidence_ids[:evidence_limit]
                verified = bool(evidence_ids)
                support_status = str(candidate["support_status"])
                if verified:
                    support_status = (
                        "cited-assertion"
                        if assertion_id
                        else "cited-book-edge"
                    )
                support_truncated = evidence_truncated_by_assertion.get(
                    assertion_id, False
                )
                if (
                    not evidence_ids
                    and mode == "word"
                    and bool(candidate.get("context_supported", False))
                ):
                    source_id = str(candidate["source_entity_id"])
                    target_id = str(candidate["target_entity_id"])
                    source_support = endpoint_evidence_by_entity.get(source_id, [])
                    target_support = endpoint_evidence_by_entity.get(target_id, [])
                    common = sorted(set(source_support) & set(target_support))
                    source_only = [
                        item for item in source_support if item not in common
                    ]
                    target_only = [
                        item for item in target_support if item not in common
                    ]
                    combined = list(common)
                    for ordinal in range(max(len(source_only), len(target_only))):
                        if ordinal < len(source_only):
                            combined.append(source_only[ordinal])
                        if ordinal < len(target_only):
                            combined.append(target_only[ordinal])
                    support_truncated = (
                        source_id in endpoint_evidence_truncated
                        or target_id in endpoint_evidence_truncated
                        or len(combined) > evidence_limit
                    )
                    evidence_ids = combined[:evidence_limit]
                edges.append(
                    {
                    "id": candidate_id,
                    "assertion_id": assertion_id,
                    "subject_entity_id": str(candidate["subject_entity_id"]),
                    "source": str(candidate["source_entity_id"]),
                    "target": str(candidate["target_entity_id"]),
                    "relation": str(candidate["relation"]),
                    "basis": str(candidate["basis"]),
                    "confidence": float(candidate["confidence"]),
                    "properties": candidate["properties"],
                    "support_status": support_status,
                    "verified": verified,
                    "context_supported": bool(candidate["context_supported"]),
                    "model_generated": bool(candidate["model_generated"]),
                    "evidence_ids": evidence_ids,
                    "evidence_count": len(evidence_ids),
                    "evidence_truncated": support_truncated,
                    "revision": int(candidate["revision"]),
                    "created_at": str(candidate["created_at"]),
                    "updated_at": str(candidate["updated_at"]),
                    }
                )
            node_by_id = {str(node["id"]): node for node in nodes}
            collapsed_translations: dict[str, str] = {}
            for edge in edges:
                if edge["relation"] != "renders-as":
                    continue
                source = node_by_id.get(str(edge["source"]))
                target = node_by_id.get(str(edge["target"]))
                if not source or not target:
                    continue
                if source["type"] != "translation" or target["type"] != "term":
                    continue
                source_label = " ".join(source["label"].casefold().split())
                target_label = " ".join(target["label"].casefold().split())
                if source_label and source_label == target_label:
                    collapsed_translations[str(source["id"])] = str(target["id"])
                    metadata = dict(source.get("payload", {}))
                    if metadata:
                        target_payload = dict(target.get("payload", {}))
                        target_payload["translation_metadata"] = metadata
                        target["payload"] = bounded_projection_object(target_payload)
            if collapsed_translations:
                collapsed_edges: list[dict[str, Any]] = []
                collapsed_triples: set[tuple[str, str, str]] = set()
                for edge in edges:
                    if (
                        edge["relation"] == "renders-as"
                        and str(edge["source"]) in collapsed_translations
                    ):
                        continue
                    rewritten = dict(edge)
                    rewritten["source"] = collapsed_translations.get(
                        str(edge["source"]), str(edge["source"])
                    )
                    rewritten["target"] = collapsed_translations.get(
                        str(edge["target"]), str(edge["target"])
                    )
                    if rewritten["source"] == rewritten["target"]:
                        continue
                    triple = (
                        str(rewritten["source"]),
                        str(rewritten["target"]),
                        str(rewritten["relation"]),
                    )
                    if triple in collapsed_triples:
                        continue
                    collapsed_triples.add(triple)
                    collapsed_edges.append(rewritten)
                edges = collapsed_edges
                nodes = [
                    node
                    for node in nodes
                    if str(node["id"]) not in collapsed_translations
                ]
            branch_slide_specs: list[dict[str, Any]] = []
            branch_history_node_ids: set[str] = set()
            branch_slides: list[dict[str, Any]] = []
            lexical_source_nodes = list(nodes)
            lexical_source_edges = list(edges)
            if mode in {"knowledge", "word", "root", "affix"}:
                # Morphology cards are focused historical projections. Knowledge
                # cards retain their semantic atoms, but all lexical modes collapse
                # storage-level period/language events into adjacent forms instead
                # of rendering labels such as "Classical Latin" as nodes.
                # Period/language events are storage-level
                # provenance containers; collapse them into their adjacent forms
                # Related words remain available to the card's derivative rails.
                node_by_id = {str(node["id"]): node for node in nodes}
                event_ids = {
                    entity_id
                    for entity_id, node in node_by_id.items()
                    if str(node["type"]) == "history-event"
                }

                def word_origin_endpoint(entity_id: str) -> bool:
                    if entity_id == subject_entity_id:
                        return True
                    node = node_by_id.get(entity_id)
                    return bool(
                        node
                        and str(node["type"]) in {"historical-form", "morpheme"}
                    )

                direct_origin_relations = {
                    "developed-into",
                    "developed-toward",
                    "explains-component",
                    "has-component",
                    "has-history",
                    "shares-component",
                }
                origin_edges: list[dict[str, Any]] = []
                origin_triples: set[tuple[str, str, str]] = set()
                for edge in edges:
                    source_id = str(edge["source"])
                    target_id = str(edge["target"])
                    relation = str(edge["relation"])
                    if (
                        mode == "knowledge"
                        and source_id not in event_ids
                        and target_id not in event_ids
                    ):
                        triple = (source_id, target_id, relation)
                        if triple not in origin_triples:
                            origin_triples.add(triple)
                            origin_edges.append(edge)
                        continue
                    if relation not in direct_origin_relations:
                        continue
                    if relation == "shares-component":
                        source_type = str(
                            node_by_id.get(source_id, {}).get("type", "")
                        )
                        target_type = str(
                            node_by_id.get(target_id, {}).get("type", "")
                        )
                        component_id = (
                            target_id
                            if target_type == "morpheme"
                            else source_id if source_type == "morpheme" else ""
                        )
                        word_id = target_id if component_id == source_id else source_id
                        if (
                            component_id not in owned_component_ids
                            or str(node_by_id.get(word_id, {}).get("type", ""))
                            != "term"
                        ):
                            continue
                    elif not (
                        word_origin_endpoint(source_id)
                        and word_origin_endpoint(target_id)
                    ):
                        continue
                    if relation == "has-component" and not (
                        source_id == subject_entity_id
                        and str(node_by_id.get(target_id, {}).get("type", ""))
                        == "morpheme"
                    ):
                        continue
                    triple = (source_id, target_id, relation)
                    if triple in origin_triples:
                        continue
                    origin_triples.add(triple)
                    origin_edges.append(edge)

                def event_context(event_id: str) -> dict[str, Any]:
                    event = node_by_id[event_id]
                    payload = event.get("payload", {})
                    if not isinstance(payload, dict):
                        payload = {}
                    context = {
                        field: payload[field]
                        for field in (
                            "language",
                            "period_label",
                            "event_type",
                            "description",
                            "date_min",
                            "date_max",
                        )
                        if payload.get(field) not in (None, "")
                    }
                    return bounded_projection_object(context)

                def merge_event_metadata(event_id: str, form_id: str) -> None:
                    form = node_by_id.get(form_id)
                    if not form or str(form["type"]) != "historical-form":
                        return
                    context = event_context(event_id)
                    if not context:
                        return
                    payload = dict(form.get("payload", {}))
                    for field in ("language", "period_label", "date_min", "date_max"):
                        if payload.get(field) in (None, "") and context.get(field) not in (
                            None,
                            "",
                        ):
                            payload[field] = context[field]
                    contexts = payload.get("history_contexts", [])
                    if not isinstance(contexts, list):
                        contexts = []
                    context_key = json.dumps(
                        context,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    existing_keys = {
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        for item in contexts
                        if isinstance(item, dict)
                    }
                    if context_key not in existing_keys:
                        contexts = [*contexts[:7], context]
                    payload["history_contexts"] = contexts
                    form["payload"] = bounded_projection_object(payload)

                def append_collapsed_event_edge(
                    source_id: str,
                    target_id: str,
                    relation: str,
                    event_id: str,
                    support: list[dict[str, Any]],
                ) -> None:
                    if source_id == target_id or not (
                        word_origin_endpoint(source_id)
                        and word_origin_endpoint(target_id)
                    ):
                        return
                    triple = (source_id, target_id, relation)
                    if triple in origin_triples:
                        return
                    evidence_ids: list[str] = []
                    evidence_truncated = False
                    for edge in support:
                        evidence_truncated = evidence_truncated or bool(
                            edge.get("evidence_truncated", False)
                        )
                        for evidence_id in edge.get("evidence_ids", []):
                            clean_id = str(evidence_id)
                            if clean_id and clean_id not in evidence_ids:
                                evidence_ids.append(clean_id)
                    if len(evidence_ids) > evidence_limit:
                        evidence_truncated = True
                    evidence_ids = evidence_ids[:evidence_limit]
                    edge_key = "|".join(
                        (subject_entity_id, source_id, target_id, relation, event_id)
                    )
                    origin_triples.add(triple)
                    origin_edges.append(
                        {
                            "id": "projection-history-"
                            + hashlib.sha256(edge_key.encode("utf-8")).hexdigest()[:24],
                            "assertion_id": "",
                            "subject_entity_id": subject_entity_id,
                            "source": source_id,
                            "target": target_id,
                            "relation": relation,
                            "basis": "projection:collapsed-history-event",
                            "confidence": min(
                                (float(edge.get("confidence", 0.0)) for edge in support),
                                default=0.0,
                            ),
                            "properties": bounded_projection_object(
                                {
                                    "collapsed_event_id": event_id,
                                    "history_context": event_context(event_id),
                                }
                            ),
                            "support_status": (
                                "accepted-model-unverified"
                                if any(
                                    bool(edge.get("model_generated", False))
                                    for edge in support
                                )
                                else "projected-history-context"
                            ),
                            "verified": False,
                            "context_supported": any(
                                bool(edge.get("context_supported", False))
                                for edge in support
                            ),
                            "model_generated": any(
                                bool(edge.get("model_generated", False))
                                for edge in support
                            ),
                            "evidence_ids": evidence_ids,
                            "evidence_count": len(evidence_ids),
                            "evidence_truncated": evidence_truncated,
                            "revision": max(
                                (int(edge.get("revision", 1)) for edge in support),
                                default=1,
                            ),
                            "created_at": min(
                                (str(edge.get("created_at", "")) for edge in support),
                                default="",
                            ),
                            "updated_at": max(
                                (str(edge.get("updated_at", "")) for edge in support),
                                default="",
                            ),
                        }
                    )

                for event_id in sorted(event_ids):
                    incoming = [
                        edge for edge in edges if str(edge["target"]) == event_id
                    ]
                    outgoing = [
                        edge for edge in edges if str(edge["source"]) == event_id
                    ]
                    adjacent_forms = {
                        endpoint
                        for edge in (*incoming, *outgoing)
                        for endpoint in (str(edge["source"]), str(edge["target"]))
                        if str(node_by_id.get(endpoint, {}).get("type", ""))
                        == "historical-form"
                    }
                    for form_id in sorted(adjacent_forms):
                        merge_event_metadata(event_id, form_id)

                    subject_origins = [
                        edge
                        for edge in incoming
                        if str(edge["source"]) == subject_entity_id
                        and str(edge["relation"]) == "has-origin-event"
                    ]
                    preceding_forms = [
                        edge
                        for edge in incoming
                        if str(edge["relation"]) == "has-history"
                        and str(
                            node_by_id.get(str(edge["source"]), {}).get("type", "")
                        )
                        == "historical-form"
                    ]
                    later_nodes = [
                        edge
                        for edge in outgoing
                        if str(edge["relation"])
                        in {"developed-toward", "explains-component"}
                        and str(
                            node_by_id.get(str(edge["target"]), {}).get("type", "")
                        )
                        in {"historical-form", "morpheme"}
                    ]
                    later_forms = [
                        edge
                        for edge in later_nodes
                        if str(
                            node_by_id.get(str(edge["target"]), {}).get("type", "")
                        )
                        == "historical-form"
                    ]
                    anchor_forms = later_forms or preceding_forms
                    for origin_edge in subject_origins:
                        for form_edge in anchor_forms:
                            form_id = (
                                str(form_edge["target"])
                                if form_edge in later_forms
                                else str(form_edge["source"])
                            )
                            append_collapsed_event_edge(
                                subject_entity_id,
                                form_id,
                                "has-history",
                                event_id,
                                [origin_edge, form_edge],
                            )
                    for preceding_edge in preceding_forms:
                        for later_edge in later_nodes:
                            append_collapsed_event_edge(
                                str(preceding_edge["source"]),
                                str(later_edge["target"]),
                                "developed-into",
                                event_id,
                                [preceding_edge, later_edge],
                            )

                # Drop isolated history branches. The surviving component must
                # be reachable from the selected word through direct morphology
                # or the collapsed, evidence-backed historical chain.
                reachable = {subject_entity_id}
                changed = True
                while changed:
                    changed = False
                    for edge in origin_edges:
                        source_id = str(edge["source"])
                        target_id = str(edge["target"])
                        if source_id in reachable or target_id in reachable:
                            before = len(reachable)
                            reachable.update((source_id, target_id))
                            changed = changed or len(reachable) != before
                nodes = [
                    node for node in nodes if str(node["id"]) in reachable
                ]
                edges = [
                    edge
                    for edge in origin_edges
                    if str(edge["source"]) in reachable
                    and str(edge["target"]) in reachable
                ]
            if mode in {"root", "affix"}:
                branch_kinds = {"root"} if mode == "root" else {"prefix", "suffix"}
                if branch_focus_id:
                    branch_components = {
                        branch_focus_id
                        for node in nodes
                        if str(node["id"]) == branch_focus_id
                    }
                else:
                    branch_components = {
                        str(node["id"])
                        for node in nodes
                        if str(node["type"]) == "morpheme"
                        and str(node.get("payload", {}).get("kind", ""))
                        in branch_kinds
                    }
                kept_branch_nodes = set(branch_components)
                history_relations = {
                    "developed-into",
                    "developed-toward",
                    "explains-component",
                    "has-history",
                }
                changed = True
                while changed:
                    changed = False
                    for edge in edges:
                        if str(edge["relation"]) not in history_relations:
                            continue
                        source_id = str(edge["source"])
                        target_id = str(edge["target"])
                        if (
                            source_id in kept_branch_nodes
                            or target_id in kept_branch_nodes
                        ):
                            before = len(kept_branch_nodes)
                            kept_branch_nodes.update((source_id, target_id))
                            changed = changed or len(kept_branch_nodes) != before
                branch_edges: list[dict[str, Any]] = []
                for edge in edges:
                    source_id = str(edge["source"])
                    target_id = str(edge["target"])
                    relation = str(edge["relation"])
                    if (
                        relation == "has-component"
                        and subject_entity_id in {source_id, target_id}
                        and bool(
                            {source_id, target_id} & branch_components
                        )
                    ):
                        kept_branch_nodes.update((source_id, target_id))
                        branch_edges.append(edge)
                    elif (
                        relation in history_relations
                        and source_id in kept_branch_nodes
                        and target_id in kept_branch_nodes
                    ):
                        branch_edges.append(edge)
                    elif relation == "has-origin-event" and (
                        source_id == subject_entity_id
                        and target_id in kept_branch_nodes
                    ):
                        kept_branch_nodes.add(source_id)
                        branch_edges.append(edge)
                kept_branch_nodes.add(subject_entity_id)
                nodes = [
                    node
                    for node in nodes
                    if str(node["id"]) in kept_branch_nodes
                ]
                edges = [
                    edge
                    for edge in branch_edges
                    if str(edge["source"]) in kept_branch_nodes
                    and str(edge["target"]) in kept_branch_nodes
                ]
                if branch_focus_id and branch_focus_id != subject_entity_id:
                    branch_node_by_id = {
                        str(node["id"]): node for node in nodes
                    }
                    subject_node = branch_node_by_id.get(subject_entity_id)
                    focus_node = branch_node_by_id.get(branch_focus_id)
                    focus_payload = (
                        focus_node.get("payload", {}) if focus_node else {}
                    )
                    focus_payload = (
                        focus_payload if isinstance(focus_payload, dict) else {}
                    )
                    focus_kind = _normalise(str(focus_payload.get("kind", "")))
                    expected_focus_kinds = (
                        {"root"} if mode == "root" else {"prefix", "suffix"}
                    )
                    direct_focus_component = any(
                        str(edge["source"]) == subject_entity_id
                        and str(edge["target"]) == branch_focus_id
                        and str(edge["relation"]) == "has-component"
                        for edge in edges
                    )
                    collapse_subject_wrapper = bool(
                        subject_node
                        and focus_node
                        and str(subject_node.get("type", "")) == "term"
                        and str(focus_node.get("type", "")) == "morpheme"
                        and focus_kind in expected_focus_kinds
                        and direct_focus_component
                        and _normalise(str(subject_node.get("label", "")))
                        == _normalise(str(focus_node.get("label", "")))
                    )
                    if collapse_subject_wrapper:
                        display_subject_entity_id = branch_focus_id

            if mode in {"root", "affix"}:
                # Root/Affix cards are projections around the morpheme itself.
                # The historical pass above intentionally removes related term
                # nodes for Word Origin, so rebuild these two modes from the
                # original, already bounded traversal. Canonical storage remains
                # untouched: this selects a compact display subgraph only.
                source_node_by_id = {
                    str(node["id"]): node for node in lexical_source_nodes
                }
                expected_branch_kinds = (
                    {"root"}
                    if mode == "root"
                    else {"affix", "prefix", "suffix", "infix", "circumfix"}
                )
                canonical_subject_entity_id = subject_entity_id
                display_subject_entity_id = subject_entity_id
                collapse_subject_wrapper = False

                direct_subject_components = {
                    str(edge["target"])
                    for edge in lexical_source_edges
                    if str(edge["source"]) == canonical_subject_entity_id
                    and str(edge["relation"]) == "has-component"
                    and str(
                        source_node_by_id.get(str(edge["target"]), {}).get(
                            "type", ""
                        )
                    )
                    == "morpheme"
                }
                subject_node = source_node_by_id.get(canonical_subject_entity_id)
                subject_form = _normalise(
                    str(subject_node.get("label", "")) if subject_node else ""
                )
                def morpheme_kind(node: dict[str, Any]) -> str:
                    payload = node.get("payload", {})
                    payload = payload if isinstance(payload, dict) else {}
                    return _normalise(str(payload.get("kind", "")))

                focus_candidates = [
                    entity_id
                    for entity_id, node in source_node_by_id.items()
                    if str(node.get("type", "")) == "morpheme"
                    and morpheme_kind(node) in expected_branch_kinds
                ]

                def component_owner_ids(edge: dict[str, Any]) -> set[str]:
                    properties = edge.get("properties", {})
                    properties = properties if isinstance(properties, dict) else {}
                    return {
                        str(properties.get(key, "")).strip()
                        for key in (
                            "component_id",
                            "component_entity_id",
                            "morpheme_id",
                        )
                        if str(properties.get(key, "")).strip()
                    }

                focus_structure: dict[str, tuple[int, int, int]] = {}
                for entity_id in focus_candidates:
                    history_links = sum(
                        1
                        for edge in origin_edges
                        if str(edge.get("relation", ""))
                        in {
                            "developed-into",
                            "developed-toward",
                            "explains-component",
                            "has-history",
                        }
                        and (
                            entity_id
                            in {str(edge["source"]), str(edge["target"])}
                            or entity_id in component_owner_ids(edge)
                        )
                    )
                    derivative_ids = {
                        endpoint
                        for edge in lexical_source_edges
                        if str(edge.get("relation", ""))
                        in {"has-component", "shares-component"}
                        and entity_id
                        in {str(edge["source"]), str(edge["target"])}
                        for endpoint in (str(edge["source"]), str(edge["target"]))
                        if endpoint != entity_id
                        and str(
                            source_node_by_id.get(endpoint, {}).get("type", "")
                        )
                        == "term"
                        and not (
                            endpoint == canonical_subject_entity_id
                            and _normalise(
                                str(
                                    source_node_by_id.get(endpoint, {}).get(
                                        "label", ""
                                    )
                                )
                            )
                            == _normalise(
                                str(
                                    source_node_by_id.get(entity_id, {}).get(
                                        "label", ""
                                    )
                                )
                            )
                        )
                    }
                    book_links = sum(
                        1
                        for edge in (*origin_edges, *lexical_source_edges)
                        if entity_id
                        in {str(edge["source"]), str(edge["target"])}
                        and str(edge.get("basis", "")).strip().casefold()
                        == "book"
                    )
                    focus_structure[entity_id] = (
                        history_links,
                        len(derivative_ids),
                        book_links,
                    )

                def branch_focus_rank(entity_id: str) -> tuple[Any, ...]:
                    node = source_node_by_id[entity_id]
                    history_links, derivative_links, book_links = focus_structure.get(
                        entity_id, (0, 0, 0)
                    )
                    return (
                        -history_links,
                        -derivative_links,
                        -book_links,
                        0 if entity_id == canonical_subject_entity_id else 1,
                        0 if entity_id in direct_subject_components else 1,
                        0
                        if subject_form
                        and _normalise(str(node.get("label", ""))) == subject_form
                        else 1,
                        0 if entity_id == branch_focus_id else 1,
                        -float(node.get("quality") or 0.0),
                        entity_id,
                    )

                requested_focus_id = (
                    str(branch_focus_id)
                    if branch_focus_id in focus_candidates
                    else ""
                )
                if requested_branch_focus:
                    # A caller-selected component is an identity, not a form
                    # search. Never replace it with a same-spelling morpheme in
                    # another language or branch, even when that alias is richer.
                    eligible_focus_candidates = (
                        [requested_focus_id] if requested_focus_id else []
                    )
                else:
                    eligible_focus_candidates = list(focus_candidates)
                selected_focus_id = (
                    min(eligible_focus_candidates, key=branch_focus_rank)
                    if eligible_focus_candidates
                    else ""
                )
                if not selected_focus_id:
                    branch_focus_id = ""
                if selected_focus_id:
                    branch_focus_id = selected_focus_id
                    display_subject_entity_id = selected_focus_id
                    focus_node = source_node_by_id[selected_focus_id]
                    equivalent_focus_ids = {
                        selected_focus_id,
                        *(
                            str(seed["target_entity_id"])
                            for seed in projection_seed_candidates
                            if str(seed.get("relation", "")) == "has-history"
                            and str(seed.get("source_entity_id", ""))
                            == selected_focus_id
                            and str(
                                seed.get("properties", {}).get(
                                    "recovery_kind", ""
                                )
                            )
                            == "exact-form-cited-history-peer"
                        ),
                    }
                    collapse_subject_wrapper = bool(
                        subject_node
                        and str(subject_node.get("type", "")) == "term"
                        and subject_form
                        == _normalise(str(focus_node.get("label", "")))
                    )

                    history_relations = {
                        "developed-into",
                        "developed-toward",
                        "explains-component",
                        "has-history",
                    }
                    history_edge_pool = [
                        edge
                        for edge in origin_edges
                        if str(edge["relation"]) in history_relations
                        and str(edge["source"]) in source_node_by_id
                        and str(edge["target"]) in source_node_by_id
                        and str(
                            source_node_by_id[str(edge["source"])].get("type", "")
                        )
                        in {"historical-form", "morpheme"}
                        and str(
                            source_node_by_id[str(edge["target"])].get("type", "")
                        )
                        in {"historical-form", "morpheme"}
                    ]

                    def branch_edge_rank(edge: dict[str, Any]) -> tuple[Any, ...]:
                        return (
                            0 if bool(edge.get("verified", False)) else 1,
                            0
                            if str(edge.get("basis", "")).strip().casefold()
                            == "book"
                            else 1,
                            0 if bool(edge.get("context_supported", False)) else 1,
                            -float(edge.get("confidence", 0.0)),
                            str(edge.get("relation", "")),
                            str(edge.get("source", "")),
                            str(edge.get("target", "")),
                            str(edge.get("id", "")),
                        )

                    history_edge_pool.sort(key=branch_edge_rank)
                    history_capacity = min(8, max(2, max_nodes // 3))
                    history_reachable = {selected_focus_id}
                    selected_history_edges: list[dict[str, Any]] = []
                    owned_history_edges = [
                        edge
                        for edge in history_edge_pool
                        if selected_focus_id in component_owner_ids(edge)
                    ]
                    for edge in owned_history_edges:
                        for endpoint in (
                            str(edge["source"]),
                            str(edge["target"]),
                        ):
                            if endpoint in history_reachable:
                                continue
                            if len(history_reachable) - 1 >= history_capacity:
                                traversal_truncated = True
                                break
                            history_reachable.add(endpoint)
                        if edge not in selected_history_edges:
                            selected_history_edges.append(edge)

                    owned_history_is_connected = any(
                        selected_focus_id
                        in {str(edge["source"]), str(edge["target"])}
                        for edge in owned_history_edges
                    )
                    if owned_history_edges and not owned_history_is_connected:
                        historical_anchors = sorted(
                            {
                                endpoint
                                for edge in owned_history_edges
                                for endpoint in (
                                    str(edge["source"]),
                                    str(edge["target"]),
                                )
                                if str(
                                    source_node_by_id.get(endpoint, {}).get(
                                        "type", ""
                                    )
                                )
                                == "historical-form"
                            },
                            key=lambda entity_id: (
                                -float(
                                    source_node_by_id[entity_id].get("quality")
                                    or 0.0
                                ),
                                entity_id,
                            ),
                        )
                        if historical_anchors:
                            support_edge = min(
                                owned_history_edges, key=branch_edge_rank
                            )
                            anchor_id = historical_anchors[0]
                            bridge_key = "|".join(
                                (
                                    selected_focus_id,
                                    anchor_id,
                                    str(support_edge.get("id", "")),
                                )
                            )
                            bridge_properties = dict(
                                support_edge.get("properties", {})
                            )
                            bridge_properties["projection_component_owner"] = {
                                "component_id": selected_focus_id,
                                "source_edge_id": str(
                                    support_edge.get("id", "")
                                ),
                            }
                            selected_history_edges.append(
                                {
                                    **support_edge,
                                    "id": "projection-component-history-"
                                    + hashlib.sha256(
                                        bridge_key.encode("utf-8")
                                    ).hexdigest()[:24],
                                    "assertion_id": "",
                                    "subject_entity_id": subject_entity_id,
                                    "source": selected_focus_id,
                                    "target": anchor_id,
                                    "relation": "has-history",
                                    "basis": "projection:component-owner",
                                    "properties": bounded_projection_object(
                                        bridge_properties
                                    ),
                                }
                            )
                    history_frontier = set(history_reachable)
                    for _ in range(min(max_depth, 4)):
                        next_frontier: set[str] = set()
                        for edge in history_edge_pool:
                            source_id = str(edge["source"])
                            target_id = str(edge["target"])
                            if source_id in history_frontier:
                                other_id = target_id
                            elif target_id in history_frontier:
                                other_id = source_id
                            else:
                                continue
                            if other_id not in history_reachable:
                                if len(history_reachable) - 1 >= history_capacity:
                                    traversal_truncated = True
                                    continue
                                history_reachable.add(other_id)
                                next_frontier.add(other_id)
                            if edge not in selected_history_edges:
                                selected_history_edges.append(edge)
                        if not next_frontier:
                            break
                        history_frontier = next_frontier
                    branch_history_node_ids = history_reachable - {
                        selected_focus_id
                    }

                    derived_candidates: list[tuple[dict[str, Any], str]] = []
                    for edge in lexical_source_edges:
                        if str(edge["relation"]) not in {
                            "has-component",
                            "shares-component",
                        }:
                            continue
                        source_id = str(edge["source"])
                        target_id = str(edge["target"])
                        if target_id in equivalent_focus_ids and str(
                            source_node_by_id.get(source_id, {}).get("type", "")
                        ) == "term":
                            derived_candidates.append((edge, source_id))
                        elif source_id in equivalent_focus_ids and str(
                            source_node_by_id.get(target_id, {}).get("type", "")
                        ) == "term":
                            derived_candidates.append((edge, target_id))
                    derivative_meanings = self._accepted_meaning_records(
                        connection,
                        {derivative_id for _, derivative_id in derived_candidates},
                    )
                    derived_candidates.sort(
                        key=lambda item: (
                            0 if derivative_meanings.get(item[1]) else 1,
                            branch_edge_rank(item[0]),
                            _normalise(
                                str(
                                    source_node_by_id[item[1]].get("label", "")
                                )
                            ),
                            item[1],
                        )
                    )

                    # The branch-focused traversal deliberately excludes
                    # has-component edges that do not touch the selected
                    # morpheme. That keeps Root/Affix projections small, but it
                    # also means a derivative's other accepted components are
                    # absent from lexical_source_edges. Materialize only those
                    # canonical term_morpheme links here, bounded again below
                    # to two secondary components per derivative.
                    persisted_components_by_term: dict[
                        str, list[tuple[dict[str, Any], str]]
                    ] = {}
                    derivative_term_ids = sorted(
                        {derivative_id for _, derivative_id in derived_candidates}
                    )
                    if derivative_term_ids:
                        derivative_placeholders = ",".join(
                            "?" for _ in derivative_term_ids
                        )
                        persisted_component_rows = connection.execute(
                            f"""SELECT link.term_id, link.morpheme_id,
                                       link.ordinal, link.surface,
                                       link.confidence AS link_confidence,
                                       link.basis AS link_basis,
                                       component.entity_type,
                                       component.canonical_key,
                                       component.label,
                                       component.quality_score,
                                       component.payload,
                                       edge.edge_id,
                                       edge.basis AS edge_basis,
                                       edge.confidence AS edge_confidence,
                                       edge.properties AS edge_properties,
                                       edge.created_at AS edge_created_at,
                                       edge.updated_at AS edge_updated_at,
                                       (
                                           SELECT GROUP_CONCAT(
                                               DISTINCT direct.evidence_id
                                           )
                                           FROM relation_assertions AS assertion
                                           JOIN relation_assertion_slots AS authority_slot
                                             ON authority_slot.assertion_id =
                                                assertion.assertion_id
                                           JOIN assertion_evidence AS direct
                                             ON direct.assertion_id =
                                                assertion.assertion_id
                                           WHERE assertion.status = 'accepted'
                                             AND assertion.source_entity_id =
                                                link.term_id
                                             AND assertion.target_entity_id =
                                                link.morpheme_id
                                             AND assertion.relation =
                                                'has-component'
                                       ) AS direct_evidence_ids
                                FROM term_morphemes AS link
                                JOIN entities AS component
                                  ON component.entity_id = link.morpheme_id
                                 AND component.entity_type = 'morpheme'
                                 AND component.status = 'accepted'
                                LEFT JOIN entity_edges AS edge
                                  ON edge.source_entity_id = link.term_id
                                 AND edge.target_entity_id = link.morpheme_id
                                 AND edge.relation = 'has-component'
                                 AND edge.status = 'accepted'
                                WHERE link.term_id IN ({derivative_placeholders})
                                  AND link.morpheme_id != ?
                                  AND (
                                      NOT EXISTS (
                                          SELECT 1
                                          FROM relation_assertions AS history
                                          JOIN relation_assertion_slots AS history_slot
                                            ON history_slot.assertion_id =
                                               history.assertion_id
                                          WHERE history.subject_entity_id = link.term_id
                                            AND history.relation = 'has-component'
                                            AND CAST(json_extract(
                                                history.properties_json,
                                                '$.ordinal'
                                            ) AS INTEGER) = link.ordinal
                                      )
                                      OR EXISTS (
                                          SELECT 1
                                          FROM relation_assertions AS authority
                                          JOIN relation_assertion_slots AS authority_slot
                                            ON authority_slot.assertion_id =
                                               authority.assertion_id
                                          WHERE authority.status = 'accepted'
                                            AND authority.subject_entity_id = link.term_id
                                            AND authority.source_entity_id = link.term_id
                                            AND authority.target_entity_id =
                                                link.morpheme_id
                                            AND authority.relation = 'has-component'
                                            AND CAST(json_extract(
                                                authority.properties_json,
                                                '$.ordinal'
                                            ) AS INTEGER) = link.ordinal
                                      )
                                  )
                                ORDER BY link.term_id,
                                         CASE WHEN lower(COALESCE(
                                             edge.basis, link.basis, ''
                                         )) = 'book' THEN 0 ELSE 1 END,
                                         link.confidence DESC,
                                         link.ordinal,
                                         link.morpheme_id,
                                         COALESCE(edge.edge_id, '')
                                LIMIT ?""",
                            (
                                *derivative_term_ids,
                                selected_focus_id,
                                max(8, max_nodes * 4),
                            ),
                        ).fetchall()
                        for row in persisted_component_rows:
                            derivative_id = str(row["term_id"])
                            component_id = str(row["morpheme_id"])
                            if component_id not in source_node_by_id:
                                source_node_by_id[component_id] = {
                                    "id": component_id,
                                    "type": str(row["entity_type"]),
                                    "key": str(row["canonical_key"]),
                                    "label": str(row["label"]),
                                    "quality": row["quality_score"],
                                    "payload": bounded_projection_object(
                                        json.loads(row["payload"])
                                    ),
                                }
                            basis = str(
                                row["edge_basis"]
                                or row["link_basis"]
                                or "accepted"
                            )
                            confidence = float(
                                row["edge_confidence"]
                                if row["edge_confidence"] is not None
                                else row["link_confidence"]
                            )
                            raw_properties = json.loads(
                                row["edge_properties"] or "{}"
                            )
                            if not isinstance(raw_properties, dict):
                                raw_properties = {}
                            raw_properties = dict(raw_properties)
                            raw_properties.setdefault(
                                "ordinal", int(row["ordinal"])
                            )
                            raw_properties.setdefault(
                                "surface", str(row["surface"])
                            )
                            raw_properties["projection_support"] = {
                                "status": (
                                    "cited-assertion"
                                    if str(row["direct_evidence_ids"] or "")
                                    else (
                                        "accepted-model-unverified"
                                        if basis.strip().casefold() == "model"
                                        else "accepted-uncited"
                                    )
                                ),
                                "assertion_evidence": bool(
                                    str(row["direct_evidence_ids"] or "")
                                ),
                                "subject_context_evidence": False,
                                "accepted_unverified": not bool(
                                    str(row["direct_evidence_ids"] or "")
                                ),
                                "model_generated": (
                                    basis.strip().casefold() == "model"
                                ),
                                "ownership": "accepted-term-morpheme",
                            }
                            edge_id = str(row["edge_id"] or "")
                            if not edge_id:
                                edge_key = "|".join(
                                    (
                                        derivative_id,
                                        component_id,
                                        str(row["ordinal"]),
                                    )
                                )
                                edge_id = "projection-term-component-" + (
                                    hashlib.sha256(
                                        edge_key.encode("utf-8")
                                    ).hexdigest()[:24]
                                )
                            component_edge = {
                                "id": edge_id,
                                "assertion_id": "",
                                "subject_entity_id": "",
                                "source": derivative_id,
                                "target": component_id,
                                "relation": "has-component",
                                "basis": basis,
                                "confidence": confidence,
                                "properties": bounded_projection_object(
                                    raw_properties
                                ),
                                "support_status": raw_properties[
                                    "projection_support"
                                ]["status"],
                                "verified": bool(
                                    str(row["direct_evidence_ids"] or "")
                                ),
                                "context_supported": False,
                                "model_generated": (
                                    basis.strip().casefold() == "model"
                                ),
                                "evidence_ids": list(
                                    dict.fromkeys(
                                        evidence_id
                                        for evidence_id in str(
                                            row["direct_evidence_ids"] or ""
                                        ).split(",")
                                        if evidence_id
                                    )
                                )[:evidence_limit],
                                "evidence_count": len(
                                    {
                                        evidence_id
                                        for evidence_id in str(
                                            row["direct_evidence_ids"] or ""
                                        ).split(",")
                                        if evidence_id
                                    }
                                ),
                                "evidence_truncated": False,
                                "revision": 1,
                                "created_at": str(
                                    row["edge_created_at"] or ""
                                ),
                                "updated_at": str(
                                    row["edge_updated_at"] or ""
                                ),
                            }
                            persisted_components_by_term.setdefault(
                                derivative_id, []
                            ).append((component_edge, component_id))

                    selected_node_ids = set(history_reachable)
                    selected_branch_edges = list(selected_history_edges)
                    seen_derivatives: set[str] = set()
                    for branch_edge, derivative_id in derived_candidates:
                        if derivative_id in seen_derivatives:
                            continue
                        if len(selected_node_ids) >= max_nodes:
                            traversal_truncated = True
                            break
                        if len(selected_branch_edges) >= max_edges:
                            traversal_truncated = True
                            break
                        seen_derivatives.add(derivative_id)
                        selected_node_ids.add(derivative_id)
                        selected_branch_edges.append(branch_edge)

                        other_component_candidates: list[
                            tuple[dict[str, Any], str]
                        ] = []
                        for component_edge in lexical_source_edges:
                            if str(component_edge["relation"]) != "has-component":
                                continue
                            component_source = str(component_edge["source"])
                            component_target = str(component_edge["target"])
                            if component_source == derivative_id:
                                component_id = component_target
                            elif component_target == derivative_id:
                                component_id = component_source
                            else:
                                continue
                            if component_id in equivalent_focus_ids or str(
                                source_node_by_id.get(component_id, {}).get(
                                    "type", ""
                                )
                            ) != "morpheme":
                                continue
                            other_component_candidates.append(
                                (component_edge, component_id)
                            )
                        other_component_candidates.extend(
                            persisted_components_by_term.get(derivative_id, [])
                        )
                        other_component_candidates.sort(
                            key=lambda item: (
                                branch_edge_rank(item[0]),
                                _normalise(
                                    str(
                                        source_node_by_id[item[1]].get(
                                            "label", ""
                                        )
                                    )
                                ),
                                item[1],
                            )
                        )
                        component_ids: list[str] = []
                        for component_edge, component_id in other_component_candidates:
                            if component_id in component_ids:
                                continue
                            if len(component_ids) >= 2:
                                break
                            component_label = _normalise(
                                str(
                                    source_node_by_id.get(component_id, {}).get(
                                        "label", ""
                                    )
                                )
                            )
                            if component_label and any(
                                existing_id != component_id
                                and _normalise(
                                    str(
                                        source_node_by_id.get(
                                            existing_id, {}
                                        ).get("label", "")
                                    )
                                )
                                == component_label
                                for existing_id in selected_node_ids
                            ):
                                continue
                            if (
                                component_id not in selected_node_ids
                                and len(selected_node_ids) >= max_nodes
                            ):
                                traversal_truncated = True
                                break
                            component_pair = frozenset(
                                (derivative_id, component_id)
                            )
                            pair_already_selected = any(
                                frozenset(
                                    (
                                        str(selected_edge["source"]),
                                        str(selected_edge["target"]),
                                    )
                                )
                                == component_pair
                                for selected_edge in selected_branch_edges
                            )
                            if (
                                not pair_already_selected
                                and len(selected_branch_edges) >= max_edges
                            ):
                                traversal_truncated = True
                                break
                            component_ids.append(component_id)
                            selected_node_ids.add(component_id)
                            if not pair_already_selected:
                                selected_branch_edges.append(component_edge)
                        branch_slide_specs.append(
                            {
                                "derivative_id": derivative_id,
                                "component_ids": component_ids,
                            }
                        )

                    nodes = [
                        source_node_by_id[entity_id]
                        for entity_id in sorted(selected_node_ids)
                        if entity_id in source_node_by_id
                    ]
                    edges = selected_branch_edges
            canonical_projection_edge_count = len(edges)
            if mode in {"knowledge", "word", "root", "affix"}:
                # Models occasionally put a language or period property in the
                # historical-form slot. Detect those atoms from their own
                # metadata, collapse their context, and retain real lexical forms.
                node_by_id = {str(node["id"]): node for node in nodes}

                def semantic_property_text(value: Any) -> str:
                    return " ".join(
                        re.findall(r"[^\W_]+", _normalise(str(value)), re.UNICODE)
                    )

                explicit_property_node_types = {
                    "date label",
                    "history label",
                    "historical label",
                    "language",
                    "language label",
                    "period",
                    "period label",
                }
                property_form_contexts: dict[str, dict[str, Any]] = {}
                for entity_id, node in node_by_id.items():
                    if entity_id in {
                        canonical_subject_entity_id,
                        branch_focus_id,
                    }:
                        continue
                    node_type = semantic_property_text(node.get("type", ""))
                    label = semantic_property_text(node.get("label", ""))
                    payload = node.get("payload", {})
                    payload = payload if isinstance(payload, dict) else {}
                    language_values: list[str] = []
                    period_values: list[str] = []
                    history_values: list[str] = []
                    for key, value in payload.items():
                        if not isinstance(value, str) or not value.strip():
                            continue
                        normalized_key = semantic_property_text(key)
                        normalized_value = semantic_property_text(value)
                        if not normalized_value:
                            continue
                        if "language" in normalized_key:
                            language_values.append(normalized_value)
                        if "period" in normalized_key or normalized_key == "date label":
                            period_values.append(normalized_value)
                        if "history" in normalized_key and "label" in normalized_key:
                            history_values.append(normalized_value)
                    property_values = set(
                        language_values + period_values + history_values
                    )
                    label_tokens = set(label.split())
                    contextual_token_sets = [
                        set(value.split())
                        for value in (
                            *language_values,
                            *period_values,
                            *history_values,
                        )
                        if value
                    ]
                    combined_context_tokens = set().union(
                        *contextual_token_sets
                    ) if contextual_token_sets else set()
                    if combined_context_tokens:
                        contextual_token_sets.append(combined_context_tokens)
                    period_tokens = set().union(
                        *(set(value.split()) for value in period_values)
                    ) if period_values else set()
                    composite_period_language_label = bool(
                        str(node.get("type", "")) == "historical-form"
                        and not str(payload.get("meaning", "")).strip()
                        and period_tokens
                        and period_tokens < label_tokens
                        and len(label_tokens - period_tokens) <= 2
                    )
                    is_property_value = label in property_values or any(
                        label_tokens
                        and label_tokens.issubset(context_tokens)
                        for context_tokens in contextual_token_sets
                    ) or composite_period_language_label
                    is_explicit_property_node = (
                        node_type in explicit_property_node_types
                    )
                    is_mislabeled_historical_form = (
                        str(node.get("type", "")) == "historical-form"
                        and is_property_value
                    )
                    if not (
                        is_explicit_property_node
                        or is_mislabeled_historical_form
                    ):
                        continue
                    context = {
                        field: payload[field]
                        for field in (
                            "language",
                            "language_label",
                            "period",
                            "period_label",
                            "history_label",
                            "date_label",
                            "date_min",
                            "date_max",
                            "meaning",
                        )
                        if payload.get(field) not in (None, "")
                    }
                    context["property_label"] = str(node.get("label", ""))
                    property_form_contexts[entity_id] = bounded_projection_object(
                        context
                    )

                property_form_ids = set(property_form_contexts)
                if property_form_ids:
                    valid_endpoint_types = {"term", "morpheme", "historical-form"}
                    for property_id, context in property_form_contexts.items():
                        adjacent_form_ids = {
                            endpoint
                            for edge in edges
                            if property_id
                            in {str(edge["source"]), str(edge["target"])}
                            for endpoint in (
                                str(edge["source"]),
                                str(edge["target"]),
                            )
                            if endpoint not in property_form_ids
                            and str(
                                node_by_id.get(endpoint, {}).get("type", "")
                            )
                            in valid_endpoint_types
                        }
                        if (
                            not adjacent_form_ids
                            and display_subject_entity_id in node_by_id
                        ):
                            adjacent_form_ids.add(display_subject_entity_id)
                        for form_id in sorted(adjacent_form_ids):
                            form = node_by_id[form_id]
                            payload = dict(form.get("payload", {}))
                            for field in (
                                "language",
                                "language_label",
                                "period",
                                "period_label",
                                "history_label",
                                "date_label",
                                "date_min",
                                "date_max",
                            ):
                                if payload.get(field) in (None, "") and context.get(
                                    field
                                ) not in (None, ""):
                                    payload[field] = context[field]
                            contexts = payload.get("history_contexts", [])
                            if not isinstance(contexts, list):
                                contexts = []
                            context_key = json.dumps(
                                context,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            existing_keys = {
                                json.dumps(
                                    item,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                for item in contexts
                                if isinstance(item, dict)
                            }
                            if context_key not in existing_keys:
                                contexts = [*contexts[:7], context]
                            payload["history_contexts"] = contexts
                            form["payload"] = bounded_projection_object(payload)

                    retained_edges = [
                        edge
                        for edge in edges
                        if str(edge["source"]) not in property_form_ids
                        and str(edge["target"]) not in property_form_ids
                    ]
                    retained_triples = {
                        (
                            str(edge["source"]),
                            str(edge["target"]),
                            str(edge["relation"]),
                        )
                        for edge in retained_edges
                    }
                    for property_id in sorted(property_form_ids):
                        incoming = [
                            edge
                            for edge in edges
                            if str(edge["target"]) == property_id
                        ]
                        outgoing = [
                            edge
                            for edge in edges
                            if str(edge["source"]) == property_id
                        ]
                        for incoming_edge in incoming:
                            for outgoing_edge in outgoing:
                                source_id = str(incoming_edge["source"])
                                target_id = str(outgoing_edge["target"])
                                if (
                                    source_id == target_id
                                    or source_id in property_form_ids
                                    or target_id in property_form_ids
                                    or str(
                                        node_by_id.get(source_id, {}).get("type", "")
                                    )
                                    not in valid_endpoint_types
                                    or str(
                                        node_by_id.get(target_id, {}).get("type", "")
                                    )
                                    not in valid_endpoint_types
                                ):
                                    continue
                                relation = (
                                    "has-history"
                                    if source_id == subject_entity_id
                                    else "developed-into"
                                )
                                triple = (source_id, target_id, relation)
                                if triple in retained_triples:
                                    continue
                                support = [incoming_edge, outgoing_edge]
                                evidence_ids: list[str] = []
                                for edge in support:
                                    for evidence_id in edge.get("evidence_ids", []):
                                        clean_id = str(evidence_id)
                                        if clean_id and clean_id not in evidence_ids:
                                            evidence_ids.append(clean_id)
                                evidence_truncated = any(
                                    bool(edge.get("evidence_truncated", False))
                                    for edge in support
                                ) or len(evidence_ids) > evidence_limit
                                evidence_ids = evidence_ids[:evidence_limit]
                                model_generated = any(
                                    bool(edge.get("model_generated", False))
                                    for edge in support
                                )
                                edge_key = "|".join(
                                    (
                                        subject_entity_id,
                                        source_id,
                                        target_id,
                                        relation,
                                        property_id,
                                    )
                                )
                                retained_triples.add(triple)
                                retained_edges.append(
                                    {
                                        "id": "projection-property-form-"
                                        + hashlib.sha256(
                                            edge_key.encode("utf-8")
                                        ).hexdigest()[:24],
                                        "assertion_id": "",
                                        "subject_entity_id": subject_entity_id,
                                        "source": source_id,
                                        "target": target_id,
                                        "relation": relation,
                                        "basis": "projection:collapsed-property-form",
                                        "confidence": min(
                                            float(edge.get("confidence", 0.0))
                                            for edge in support
                                        ),
                                        "properties": bounded_projection_object(
                                            {
                                                "collapsed_property_form_id": property_id,
                                                "history_context": property_form_contexts[
                                                    property_id
                                                ],
                                            }
                                        ),
                                        "support_status": (
                                            "accepted-model-unverified"
                                            if model_generated
                                            else "projected-history-context"
                                        ),
                                        "verified": False,
                                        "context_supported": any(
                                            bool(
                                                edge.get(
                                                    "context_supported", False
                                                )
                                            )
                                            for edge in support
                                        ),
                                        "model_generated": model_generated,
                                        "evidence_ids": evidence_ids,
                                        "evidence_count": len(evidence_ids),
                                        "evidence_truncated": evidence_truncated,
                                        "revision": max(
                                            int(edge.get("revision", 1))
                                            for edge in support
                                        ),
                                        "created_at": min(
                                            str(edge.get("created_at", ""))
                                            for edge in support
                                        ),
                                        "updated_at": max(
                                            str(edge.get("updated_at", ""))
                                            for edge in support
                                        ),
                                    }
                                )
                    nodes = [
                        node
                        for node in nodes
                        if str(node["id"]) not in property_form_ids
                    ]
                    edges = retained_edges

                    reachable = {display_subject_entity_id}
                    changed = True
                    while changed:
                        changed = False
                        for edge in edges:
                            source_id = str(edge["source"])
                            target_id = str(edge["target"])
                            if source_id in reachable or target_id in reachable:
                                before = len(reachable)
                                reachable.update((source_id, target_id))
                                changed = changed or len(reachable) != before
                    nodes = [
                        node for node in nodes if str(node["id"]) in reachable
                    ]
                    edges = [
                        edge
                        for edge in edges
                        if str(edge["source"]) in reachable
                        and str(edge["target"]) in reachable
                    ]
            if mode in {"knowledge", "word", "root", "affix"}:
                # Coalesce projection identities, not lexical concepts. A free
                # whole-word component is presentation scaffolding in Word Origin,
                # while affixes/roots and genuinely distinct historical stages stay.
                node_by_id = {str(node["id"]): node for node in nodes}
                aliases: dict[str, str] = {}

                # A direct component can have both a legacy notation ID and the
                # role-aware canonical ID.  Coalesce only one subject-local
                # occurrence whose exact assertion evidence proves that the two
                # IDs describe the same claim.  Context evidence, equal glosses,
                # and spelling alone are deliberately insufficient.
                projection_subject = node_by_id.get(canonical_subject_entity_id)
                projection_subject_form = str(
                    projection_subject.get("label", "")
                    if projection_subject is not None
                    else ""
                )
                direct_component_claims: list[dict[str, Any]] = []
                for edge in edges:
                    if (
                        str(edge.get("source", ""))
                        != canonical_subject_entity_id
                        or str(edge.get("relation", "")) != "has-component"
                        or not bool(edge.get("verified", False))
                    ):
                        continue
                    component_id = str(edge.get("target", ""))
                    component = node_by_id.get(component_id)
                    if not component or str(component.get("type", "")) != "morpheme":
                        continue
                    component_payload = component.get("payload", {})
                    component_payload = (
                        component_payload
                        if isinstance(component_payload, dict)
                        else {}
                    )
                    language = str(component_payload.get("language", ""))
                    kind = str(component_payload.get("kind", ""))
                    form = str(component.get("label", ""))
                    edge_properties = edge.get("properties", {})
                    edge_properties = (
                        edge_properties if isinstance(edge_properties, dict) else {}
                    )
                    raw_ordinal = edge_properties.get("ordinal", -1)
                    try:
                        ordinal = int(raw_ordinal)
                    except (TypeError, ValueError):
                        ordinal = -1
                    try:
                        occurrence = canonical_morpheme_occurrence_identity(
                            projection_subject_form,
                            language,
                            form,
                            kind,
                            ordinal,
                        )
                        identity = canonical_lexical_node_identity(
                            canonical_subject_entity_id,
                            "morpheme",
                            language,
                            form,
                            role=kind,
                        ) + occurrence
                    except ValueError:
                        continue
                    # Multiple visible spans require a persisted ordinal.  Old
                    # claims without one cannot prove which occurrence they own.
                    if occurrence[:1] == ("ordinal",) and ordinal < 0:
                        continue
                    evidence_ids = {
                        str(item)
                        for item in edge.get("evidence_ids", [])
                        if str(item)
                    }
                    if not evidence_ids:
                        continue
                    direct_component_claims.append(
                        {
                            "entity_id": component_id,
                            "identity": identity,
                            "evidence_ids": evidence_ids,
                            "language": language,
                            "kind": kind,
                            "form": form,
                        }
                    )

                direct_component_groups: list[list[dict[str, Any]]] = []
                for claim in sorted(
                    direct_component_claims,
                    key=lambda item: str(item["entity_id"]),
                ):
                    matched_group = next(
                        (
                            group
                            for group in direct_component_groups
                            if group[0]["identity"] == claim["identity"]
                            and any(
                                bool(
                                    other["evidence_ids"]
                                    & claim["evidence_ids"]
                                )
                                for other in group
                            )
                        ),
                        None,
                    )
                    if matched_group is None:
                        direct_component_groups.append([claim])
                    else:
                        matched_group.append(claim)

                for group in direct_component_groups:
                    entity_ids = sorted(
                        {str(item["entity_id"]) for item in group}
                    )
                    if len(entity_ids) < 2:
                        continue

                    def component_preference(entity_id: str) -> tuple[Any, ...]:
                        node = node_by_id[entity_id]
                        payload = node.get("payload", {})
                        payload = payload if isinstance(payload, dict) else {}
                        try:
                            desired_key = canonical_morpheme_identity(
                                payload.get("language", ""),
                                node.get("label", ""),
                                payload.get("kind", ""),
                            )[2]
                        except ValueError:
                            desired_key = ""
                        return (
                            0 if str(node.get("key", "")) == desired_key else 1,
                            -float(node.get("quality") or 0.0),
                            entity_id,
                        )

                    representative_id = min(entity_ids, key=component_preference)
                    representative = node_by_id[representative_id]
                    merged_payload = representative.get("payload", {})
                    merged_payload = (
                        dict(merged_payload)
                        if isinstance(merged_payload, dict)
                        else {}
                    )
                    display_aliases = merged_payload.get(
                        "projection_display_aliases", []
                    )
                    if not isinstance(display_aliases, list):
                        display_aliases = []
                    alias_keys = {
                        str(item.get("canonical_entity_id", ""))
                        for item in display_aliases
                        if isinstance(item, dict)
                    }
                    for entity_id in entity_ids:
                        duplicate = node_by_id[entity_id]
                        duplicate_payload = duplicate.get("payload", {})
                        duplicate_payload = (
                            duplicate_payload
                            if isinstance(duplicate_payload, dict)
                            else {}
                        )
                        representative["quality"] = max(
                            float(representative.get("quality") or 0.0),
                            float(duplicate.get("quality") or 0.0),
                        )
                        for key, value in duplicate_payload.items():
                            if merged_payload.get(key) in (None, "", []):
                                merged_payload[key] = value
                        if entity_id == representative_id:
                            continue
                        if entity_id not in alias_keys:
                            display_aliases.append(
                                {
                                    "canonical_entity_id": entity_id,
                                    "entity_type": "morpheme",
                                    "label": str(duplicate.get("label", "")),
                                    "metadata": bounded_projection_object(
                                        duplicate_payload
                                    ),
                                }
                            )
                            alias_keys.add(entity_id)
                        aliases[entity_id] = representative_id
                    merged_payload["projection_display_aliases"] = display_aliases[:8]
                    representative["payload"] = bounded_projection_object(
                        merged_payload
                    )

                while branch_focus_id in aliases:
                    branch_focus_id = aliases[branch_focus_id]
                while display_subject_entity_id in aliases:
                    display_subject_entity_id = aliases[display_subject_entity_id]

                if mode in {"root", "affix"} and not collapse_subject_wrapper:
                    subject_node = node_by_id.get(canonical_subject_entity_id)
                    subject_form = _normalise(
                        str(subject_node.get("label", "")) if subject_node else ""
                    )
                    same_form_focus_ids: list[str] = []
                    if subject_node and str(subject_node.get("type", "")) == "term":
                        for edge in edges:
                            if (
                                str(edge["source"]) != canonical_subject_entity_id
                                or str(edge["relation"]) != "has-component"
                            ):
                                continue
                            focus_id = str(edge["target"])
                            focus_node = node_by_id.get(focus_id)
                            if not focus_node or str(focus_node.get("type", "")) != "morpheme":
                                continue
                            focus_payload = focus_node.get("payload", {})
                            focus_payload = (
                                focus_payload
                                if isinstance(focus_payload, dict)
                                else {}
                            )
                            if _normalise(str(focus_payload.get("kind", ""))) not in expected_branch_kinds:
                                continue
                            if _normalise(str(focus_node.get("label", ""))) != subject_form:
                                continue
                            same_form_focus_ids.append(focus_id)
                    if same_form_focus_ids:
                        if not requested_branch_focus:
                            branch_focus_id = min(
                                same_form_focus_ids,
                                key=lambda entity_id: (
                                    -float(
                                        node_by_id[entity_id].get("quality")
                                        or 0.0
                                    ),
                                    entity_id,
                                ),
                            )
                        if branch_focus_id in same_form_focus_ids:
                            display_subject_entity_id = branch_focus_id
                            collapse_subject_wrapper = True

                if collapse_subject_wrapper:
                    focus_node = node_by_id.get(display_subject_entity_id)
                    if focus_node is not None:
                        focus_payload = focus_node.get("payload", {})
                        focus_payload = (
                            dict(focus_payload)
                            if isinstance(focus_payload, dict)
                            else {}
                        )
                        focus_payload["canonical_subject_entity_id"] = (
                            canonical_subject_entity_id
                        )
                        focus_node["payload"] = bounded_projection_object(
                            focus_payload
                        )
                        aliases[canonical_subject_entity_id] = (
                            display_subject_entity_id
                        )

                if mode in {"root", "affix"}:
                    # A term wrapper, historical spelling, or duplicate morpheme
                    # with the exact focus spelling is one display identity. Keep
                    # its typed payload as bounded metadata and let edge bundles
                    # retain every canonical source ID and evidence link.
                    focus_node = node_by_id.get(display_subject_entity_id)
                    focus_form = _normalise(
                        str(focus_node.get("label", "")) if focus_node else ""
                    )
                    if focus_node and focus_form:
                        focus_payload = focus_node.get("payload", {})
                        focus_payload = (
                            dict(focus_payload)
                            if isinstance(focus_payload, dict)
                            else {}
                        )
                        display_aliases = focus_payload.get(
                            "projection_display_aliases", []
                        )
                        if not isinstance(display_aliases, list):
                            display_aliases = []
                        alias_keys = {
                            str(item.get("canonical_entity_id", ""))
                            for item in display_aliases
                            if isinstance(item, dict)
                        }
                        focus_identity_relations = {
                            "has-component",
                            "shares-component",
                            "developed-into",
                            "developed-toward",
                            "explains-component",
                            "has-history",
                        }
                        connected_ids = {display_subject_entity_id}
                        changed = True
                        while changed:
                            changed = False
                            for edge in edges:
                                if (
                                    str(edge.get("relation", ""))
                                    not in focus_identity_relations
                                ):
                                    continue
                                source_id = str(edge["source"])
                                target_id = str(edge["target"])
                                if source_id in connected_ids:
                                    candidate_id = target_id
                                elif target_id in connected_ids:
                                    candidate_id = source_id
                                else:
                                    continue
                                candidate = node_by_id.get(candidate_id)
                                if not candidate or str(
                                    candidate.get("type", "")
                                ) not in {
                                    "term",
                                    "morpheme",
                                    "historical-form",
                                }:
                                    continue
                                if (
                                    _normalise(str(candidate.get("label", "")))
                                    != focus_form
                                ):
                                    continue
                                before = len(connected_ids)
                                connected_ids.add(candidate_id)
                                changed = changed or len(connected_ids) != before
                        # A canonical morpheme may have been admitted once from
                        # an English dictionary record and once from its source
                        # language.  When both records have the same kind and a
                        # compatible gloss, a shared history neighbor proves that
                        # they are parallel records for one display identity.  Do
                        # not merge unrelated homographs merely because their
                        # labels happen to match.
                        focus_payload_kind = _normalise(
                            str(focus_payload.get("kind", ""))
                        )

                        def comparable_gloss(value: Any) -> str:
                            gloss = _normalise(str(value or ""))
                            return gloss[3:] if gloss.startswith("to ") else gloss

                        focus_gloss = comparable_gloss(
                            focus_payload.get("meaning", "")
                        )
                        identity_adjacency: dict[str, set[str]] = {}
                        for edge in edges:
                            if (
                                str(edge.get("relation", ""))
                                not in focus_identity_relations
                            ):
                                continue
                            source_id = str(edge["source"])
                            target_id = str(edge["target"])
                            identity_adjacency.setdefault(source_id, set()).add(
                                target_id
                            )
                            identity_adjacency.setdefault(target_id, set()).add(
                                source_id
                            )
                        if (
                            str(focus_node.get("type", "")) == "morpheme"
                            and focus_payload_kind
                        ):
                            changed = True
                            while changed:
                                changed = False
                                for entity_id, node in sorted(node_by_id.items()):
                                    if entity_id in connected_ids:
                                        continue
                                    if str(node.get("type", "")) != "morpheme":
                                        continue
                                    if (
                                        _normalise(str(node.get("label", "")))
                                        != focus_form
                                    ):
                                        continue
                                    candidate_payload = node.get("payload", {})
                                    candidate_payload = (
                                        candidate_payload
                                        if isinstance(candidate_payload, dict)
                                        else {}
                                    )
                                    if (
                                        _normalise(
                                            str(candidate_payload.get("kind", ""))
                                        )
                                        != focus_payload_kind
                                    ):
                                        continue
                                    candidate_gloss = comparable_gloss(
                                        candidate_payload.get("meaning", "")
                                    )
                                    if (
                                        focus_gloss
                                        and candidate_gloss
                                        and focus_gloss != candidate_gloss
                                    ):
                                        continue
                                    candidate_neighbors = identity_adjacency.get(
                                        entity_id, set()
                                    )
                                    if not any(
                                        entity_id
                                        in identity_adjacency.get(connected_id, set())
                                        or bool(
                                            candidate_neighbors
                                            & identity_adjacency.get(
                                                connected_id, set()
                                            )
                                        )
                                        for connected_id in connected_ids
                                    ):
                                        continue
                                    connected_ids.add(entity_id)
                                    changed = True
                        for entity_id, node in sorted(node_by_id.items()):
                            if entity_id == display_subject_entity_id:
                                continue
                            if entity_id not in connected_ids:
                                continue
                            if str(node.get("type", "")) not in {
                                "term",
                                "morpheme",
                                "historical-form",
                            }:
                                continue
                            if _normalise(str(node.get("label", ""))) != focus_form:
                                continue
                            aliases[entity_id] = display_subject_entity_id
                            if entity_id not in alias_keys:
                                alias_payload = node.get("payload", {})
                                alias_payload = (
                                    alias_payload
                                    if isinstance(alias_payload, dict)
                                    else {}
                                )
                                display_aliases.append(
                                    {
                                        "canonical_entity_id": entity_id,
                                        "entity_type": str(node.get("type", "")),
                                        **{
                                            key: alias_payload[key]
                                            for key in (
                                                "kind",
                                                "language",
                                                "meaning",
                                                "period_label",
                                                "date_min",
                                                "date_max",
                                            )
                                            if alias_payload.get(key) not in (None, "")
                                        },
                                    }
                                )
                                alias_keys.add(entity_id)
                        if display_aliases:
                            focus_payload["projection_display_aliases"] = (
                                display_aliases[:8]
                            )
                            focus_node["payload"] = bounded_projection_object(
                                focus_payload
                            )

                identity_relations = {
                    "has-component",
                    "shares-component",
                    "developed-into",
                    "developed-toward",
                    "explains-component",
                    "has-history",
                }
                term_history_groups: dict[str, dict[str, list[str]]] = {}
                for entity_id, node in node_by_id.items():
                    if entity_id in aliases:
                        continue
                    node_type = str(node.get("type", ""))
                    if node_type not in {"term", "historical-form"}:
                        continue
                    identity = _normalise(str(node.get("label", "")))
                    if identity:
                        term_history_groups.setdefault(
                            identity, {"term": [], "historical-form": []}
                        )[node_type].append(entity_id)

                def identity_morpheme_neighbors(entity_id: str) -> set[str]:
                    neighbors: set[str] = set()
                    for edge in edges:
                        if str(edge.get("relation", "")) not in identity_relations:
                            continue
                        source_id = str(edge["source"])
                        target_id = str(edge["target"])
                        if entity_id == source_id:
                            neighbor_id = target_id
                        elif entity_id == target_id:
                            neighbor_id = source_id
                        else:
                            continue
                        if str(
                            node_by_id.get(neighbor_id, {}).get("type", "")
                        ) == "morpheme":
                            neighbors.add(neighbor_id)
                    return neighbors

                history_relations = {
                    "developed-into",
                    "developed-toward",
                    "explains-component",
                    "has-history",
                }
                for group in term_history_groups.values():
                    if len(group["term"]) != 1 or len(group["historical-form"]) != 1:
                        continue
                    term_id = group["term"][0]
                    history_id = group["historical-form"][0]
                    directly_related = any(
                        {str(edge["source"]), str(edge["target"])}
                        == {term_id, history_id}
                        and str(edge.get("relation", "")) in history_relations
                        for edge in edges
                    )
                    share_morpheme = bool(
                        identity_morpheme_neighbors(term_id)
                        & identity_morpheme_neighbors(history_id)
                    )
                    if not (directly_related or share_morpheme):
                        continue
                    aliases[history_id] = term_id
                    term_node = node_by_id[term_id]
                    term_payload = term_node.get("payload", {})
                    term_payload = (
                        dict(term_payload) if isinstance(term_payload, dict) else {}
                    )
                    history_node = node_by_id[history_id]
                    history_payload = history_node.get("payload", {})
                    history_payload = (
                        history_payload if isinstance(history_payload, dict) else {}
                    )
                    alias_record = {
                        "canonical_entity_id": history_id,
                        "entity_type": "historical-form",
                        **{
                            key: history_payload[key]
                            for key in (
                                "language",
                                "meaning",
                                "period",
                                "period_label",
                                "date_min",
                                "date_max",
                            )
                            if history_payload.get(key) not in (None, "")
                        },
                    }
                    display_aliases = term_payload.get(
                        "projection_display_aliases", []
                    )
                    if not isinstance(display_aliases, list):
                        display_aliases = []
                    if history_id not in {
                        str(item.get("canonical_entity_id", ""))
                        for item in display_aliases
                        if isinstance(item, dict)
                    }:
                        display_aliases.append(alias_record)
                    historical_variants = term_payload.get(
                        "historical_variants", []
                    )
                    if not isinstance(historical_variants, list):
                        historical_variants = []
                    if history_id not in {
                        str(item.get("canonical_entity_id", ""))
                        for item in historical_variants
                        if isinstance(item, dict)
                    }:
                        historical_variants.append(alias_record)
                    term_payload["projection_display_aliases"] = display_aliases[:8]
                    term_payload["historical_variants"] = historical_variants[:8]
                    term_node["payload"] = bounded_projection_object(term_payload)

                def historical_trajectory_identity(
                    entity_id: str,
                ) -> tuple[tuple[str, str, str, str], ...]:
                    roles: set[tuple[str, str, str, str]] = set()
                    for edge in edges:
                        source_id = str(edge.get("source", ""))
                        target_id = str(edge.get("target", ""))
                        if entity_id not in {source_id, target_id}:
                            continue
                        relation = str(edge.get("relation", ""))
                        if relation not in identity_relations:
                            continue
                        properties = edge.get("properties", {})
                        properties = properties if isinstance(properties, dict) else {}
                        component_id = str(properties.get("component_id", "")).strip()
                        source_term_id = str(
                            properties.get("source_term_id", "")
                        ).strip()
                        direction = "source" if source_id == entity_id else "target"
                        if component_id and source_term_id:
                            roles.add(
                                (direction, relation, component_id, source_term_id)
                            )
                        else:
                            neighbor_id = (
                                target_id if source_id == entity_id else source_id
                            )
                            roles.add(
                                (direction, relation, f"neighbor:{neighbor_id}", entity_id)
                            )
                    if not roles:
                        return (("unscoped", "", entity_id, ""),)
                    return tuple(sorted(roles))

                historical_groups: dict[tuple[Any, ...], list[str]] = {}
                for entity_id, node in node_by_id.items():
                    if (
                        entity_id in aliases
                        or str(node.get("type", "")) != "historical-form"
                    ):
                        continue
                    payload = node.get("payload", {})
                    payload = payload if isinstance(payload, dict) else {}
                    identity = (
                        _normalise(str(node.get("label", ""))),
                        _normalise(str(payload.get("language", ""))),
                        historical_trajectory_identity(entity_id),
                    )
                    if identity[0]:
                        historical_groups.setdefault(identity, []).append(entity_id)

                def historical_metadata_identity(entity_id: str) -> tuple[Any, ...]:
                    payload = node_by_id[entity_id].get("payload", {})
                    payload = payload if isinstance(payload, dict) else {}
                    return (
                        _normalise(str(payload.get("language", ""))),
                        _normalise(
                            str(
                                payload.get("period_label", "")
                                or payload.get("period", "")
                            )
                        ),
                        payload.get("date_min"),
                        payload.get("date_max"),
                        _normalise(str(payload.get("meaning", ""))),
                        historical_trajectory_identity(entity_id),
                    )

                def shared_historical_evidence(left: str, right: str) -> bool:
                    def evidence(entity_id: str) -> set[str]:
                        node = node_by_id[entity_id]
                        payload = node.get("payload", {})
                        payload = payload if isinstance(payload, dict) else {}
                        identifiers = {
                            str(item)
                            for item in (
                                *node.get("evidence_ids", []),
                                *payload.get("evidence_ids", []),
                            )
                            if str(item)
                        }
                        for edge in edges:
                            if not bool(edge.get("verified", False)):
                                continue
                            if entity_id not in {
                                str(edge.get("source", "")),
                                str(edge.get("target", "")),
                            }:
                                continue
                            identifiers.update(
                                str(item)
                                for item in edge.get("evidence_ids", [])
                                if str(item)
                            )
                        return identifiers

                    return bool(evidence(left) & evidence(right))

                historical_adjacency: dict[str, set[str]] = {}
                for edge in edges:
                    if str(edge.get("relation", "")) not in identity_relations:
                        continue
                    source_id = str(edge["source"])
                    target_id = str(edge["target"])
                    historical_adjacency.setdefault(source_id, set()).add(target_id)
                    historical_adjacency.setdefault(target_id, set()).add(source_id)

                def parallel_historical_identity(left: str, right: str) -> bool:
                    if not shared_historical_evidence(left, right):
                        return False
                    if historical_metadata_identity(left) == historical_metadata_identity(
                        right
                    ):
                        return True
                    # Shared predecessor/successor means two records occupy the
                    # same branch position.  A direct edge does not: identical
                    # spelling can legitimately persist across sequential stages.
                    return bool(
                        historical_adjacency.get(left, set())
                        & historical_adjacency.get(right, set())
                    )

                historical_alias_groups: list[list[str]] = []
                for candidates in historical_groups.values():
                    remaining = set(candidates)
                    while remaining:
                        component = {min(remaining)}
                        remaining -= component
                        changed = True
                        while changed:
                            changed = False
                            additions = {
                                candidate
                                for candidate in remaining
                                if any(
                                    parallel_historical_identity(member, candidate)
                                    for member in component
                                )
                            }
                            if additions:
                                component.update(additions)
                                remaining.difference_update(additions)
                                changed = True
                        if len(component) > 1:
                            historical_alias_groups.append(sorted(component))

                for entity_ids in historical_alias_groups:
                    if len(entity_ids) < 2:
                        continue
                    representative_id = min(
                        entity_ids,
                        key=lambda entity_id: (
                            -float(node_by_id[entity_id].get("quality") or 0.0),
                            entity_id,
                        ),
                    )
                    representative = node_by_id[representative_id]
                    merged_payload = dict(representative.get("payload", {}))
                    merged_contexts = merged_payload.get("history_contexts", [])
                    if not isinstance(merged_contexts, list):
                        merged_contexts = []
                    merged_variants = merged_payload.get("historical_variants", [])
                    if not isinstance(merged_variants, list):
                        merged_variants = []
                    context_keys = {
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        for item in merged_contexts
                        if isinstance(item, dict)
                    }
                    variant_keys = {
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        for item in merged_variants
                        if isinstance(item, dict)
                    }
                    for entity_id in sorted(entity_ids):
                        duplicate = node_by_id[entity_id]
                        duplicate_payload = duplicate.get("payload", {})
                        if isinstance(duplicate_payload, dict):
                            variant = {
                                "canonical_entity_id": entity_id,
                                **{
                                    key: duplicate_payload[key]
                                    for key in (
                                        "language",
                                        "meaning",
                                        "period",
                                        "period_label",
                                        "date_min",
                                        "date_max",
                                    )
                                    if duplicate_payload.get(key) not in (None, "")
                                },
                            }
                            variant_key = json.dumps(
                                variant,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            if variant_key not in variant_keys:
                                merged_variants.append(variant)
                                variant_keys.add(variant_key)
                            for key, value in duplicate_payload.items():
                                if merged_payload.get(key) in (None, "", []):
                                    merged_payload[key] = value
                            contexts = duplicate_payload.get("history_contexts", [])
                            if isinstance(contexts, list):
                                for context in contexts:
                                    if not isinstance(context, dict):
                                        continue
                                    context_key = json.dumps(
                                        context,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                    if context_key not in context_keys:
                                        merged_contexts.append(context)
                                        context_keys.add(context_key)
                        representative["quality"] = max(
                            float(representative.get("quality") or 0.0),
                            float(duplicate.get("quality") or 0.0),
                        )
                        if entity_id != representative_id:
                            aliases[entity_id] = representative_id
                    if merged_contexts:
                        merged_payload["history_contexts"] = merged_contexts[:8]
                    if merged_variants:
                        merged_payload["historical_variants"] = merged_variants[:8]
                    representative["payload"] = bounded_projection_object(
                        merged_payload
                    )

                if mode == "word":
                    subject_node = node_by_id.get(subject_entity_id)
                    subject_payload = (
                        dict(subject_node.get("payload", {})) if subject_node else {}
                    )
                    subject_form = _normalise(
                        str(subject_node.get("label", "")) if subject_node else ""
                    )
                    direct_component_ids = {
                        str(edge["target"])
                        for edge in edges
                        if str(edge["source"]) == subject_entity_id
                        and str(edge["relation"]) == "has-component"
                    }
                    component_metadata = subject_payload.get(
                        "component_metadata", []
                    )
                    if not isinstance(component_metadata, list):
                        component_metadata = []
                    component_keys = {
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        for item in component_metadata
                        if isinstance(item, dict)
                    }
                    for entity_id in sorted(direct_component_ids):
                        component = node_by_id.get(entity_id)
                        if not component or str(component.get("type", "")) != "morpheme":
                            continue
                        payload = component.get("payload", {})
                        payload = payload if isinstance(payload, dict) else {}
                        if _normalise(str(component.get("label", ""))) != subject_form:
                            continue
                        aliases[entity_id] = subject_entity_id
                        metadata = {
                            key: payload[key]
                            for key in ("kind", "language", "meaning")
                            if payload.get(key) not in (None, "")
                        }
                        metadata_key = json.dumps(
                            metadata,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if metadata and metadata_key not in component_keys:
                            component_metadata.append(metadata)
                            component_keys.add(metadata_key)
                        if (
                            subject_payload.get("meaning") in (None, "")
                            and payload.get("meaning") not in (None, "")
                        ):
                            subject_payload["meaning"] = payload["meaning"]
                    if subject_node and component_metadata:
                        subject_payload["component_metadata"] = component_metadata[:8]
                        subject_node["payload"] = bounded_projection_object(
                            subject_payload
                        )

                # Canonical entities can remain distinct because language, sense,
                # or ingestion provenance differs while still having one visible
                # identity.  Keep those distinctions as bounded node metadata and
                # rewrite their edges through one deterministic representative.
                def display_semantic_identity(entity_id: str) -> tuple[Any, ...]:
                    node = node_by_id[entity_id]
                    node_type = _normalise(str(node.get("type", "")))
                    if node_type == "historical-form":
                        # Language, trajectory, period, dating, and sense are all
                        # identity boundaries for an accepted historical stage.
                        return historical_metadata_identity(entity_id)
                    payload = node.get("payload", {})
                    payload = payload if isinstance(payload, dict) else {}
                    return tuple(
                        _normalise(str(payload.get(key, "")))
                        for key in (
                            "kind",
                            "meaning",
                            "definition",
                            "gloss",
                            "text",
                            "language",
                            "period_label",
                            "period",
                            "date_min",
                            "date_max",
                        )
                    )

                display_identity_groups: dict[
                    tuple[str, str, tuple[Any, ...]], list[str]
                ] = {}
                for entity_id, node in node_by_id.items():
                    if entity_id in aliases:
                        continue
                    identity = (
                        _normalise(str(node.get("type", ""))),
                        _normalise(str(node.get("label", ""))),
                        display_semantic_identity(entity_id),
                    )
                    if identity[0] and identity[1]:
                        display_identity_groups.setdefault(identity, []).append(
                            entity_id
                        )

                ordered_history_relations = {
                    "derived-from",
                    "developed-from",
                    "developed-into",
                    "developed-toward",
                }
                ordered_history_adjacency: dict[str, set[str]] = {}
                for edge in edges:
                    if str(edge.get("relation", "")) not in ordered_history_relations:
                        continue
                    ordered_history_adjacency.setdefault(
                        str(edge["source"]), set()
                    ).add(str(edge["target"]))

                def ordered_history_path(start: str, target: str) -> bool:
                    if start == target:
                        return True
                    pending = list(ordered_history_adjacency.get(start, set()))
                    seen = {start}
                    while pending:
                        candidate = pending.pop()
                        if candidate == target:
                            return True
                        if candidate in seen:
                            continue
                        seen.add(candidate)
                        pending.extend(
                            ordered_history_adjacency.get(candidate, set()) - seen
                        )
                    return False

                merge_groups: list[list[str]] = []
                for identity, entity_ids in display_identity_groups.items():
                    if len(entity_ids) < 2:
                        continue
                    if identity[0] != "historical-form":
                        merge_groups.append(entity_ids)
                        continue
                    # Equal spelling and metadata are insufficient when records
                    # occupy successive stages in an attested history chain.
                    remaining = set(entity_ids)
                    while remaining:
                        seed = min(remaining)
                        remaining.remove(seed)
                        component = [seed]
                        for candidate in sorted(tuple(remaining)):
                            if not shared_historical_evidence(candidate, seed):
                                continue
                            if any(
                                ordered_history_path(candidate, member)
                                or ordered_history_path(member, candidate)
                                for member in component
                            ):
                                continue
                            component.append(candidate)
                            remaining.remove(candidate)
                        if len(component) > 1:
                            merge_groups.append(component)

                for entity_ids in merge_groups:
                    representative_id = min(
                        entity_ids,
                        key=lambda entity_id: (
                            0 if entity_id == display_subject_entity_id else 1,
                            0 if entity_id == branch_focus_id else 1,
                            -float(node_by_id[entity_id].get("quality") or 0.0),
                            entity_id,
                        ),
                    )
                    representative = node_by_id[representative_id]
                    merged_payload = representative.get("payload", {})
                    merged_payload = (
                        dict(merged_payload)
                        if isinstance(merged_payload, dict)
                        else {}
                    )
                    display_aliases = merged_payload.get(
                        "projection_display_aliases", []
                    )
                    if not isinstance(display_aliases, list):
                        display_aliases = []
                    alias_keys = {
                        str(item.get("canonical_entity_id", ""))
                        for item in display_aliases
                        if isinstance(item, dict)
                    }

                    for entity_id in sorted(entity_ids):
                        duplicate = node_by_id[entity_id]
                        representative["quality"] = max(
                            float(representative.get("quality") or 0.0),
                            float(duplicate.get("quality") or 0.0),
                        )
                        if entity_id == representative_id:
                            continue
                        duplicate_payload = duplicate.get("payload", {})
                        duplicate_payload = (
                            duplicate_payload
                            if isinstance(duplicate_payload, dict)
                            else {}
                        )
                        if entity_id not in alias_keys:
                            display_aliases.append(
                                {
                                    "canonical_entity_id": entity_id,
                                    "entity_type": str(duplicate.get("type", "")),
                                    "label": str(duplicate.get("label", "")),
                                    "metadata": bounded_projection_object(
                                        duplicate_payload
                                    ),
                                }
                            )
                            alias_keys.add(entity_id)
                        for key, value in duplicate_payload.items():
                            if merged_payload.get(key) in (None, "", []):
                                merged_payload[key] = value
                                continue
                            if not isinstance(value, list):
                                continue
                            existing = merged_payload.get(key)
                            if not isinstance(existing, list):
                                continue
                            combined = list(existing)
                            combined_keys = {
                                json.dumps(
                                    item,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                for item in combined
                            }
                            for item in value:
                                item_key = json.dumps(
                                    item,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                if item_key not in combined_keys:
                                    combined.append(item)
                                    combined_keys.add(item_key)
                            merged_payload[key] = combined[:8]
                        aliases[entity_id] = representative_id

                    merged_payload["projection_display_aliases"] = display_aliases[:8]
                    representative["payload"] = bounded_projection_object(
                        merged_payload
                    )

                def resolve_projection_alias(entity_id: str) -> str:
                    seen: set[str] = set()
                    while entity_id in aliases and entity_id not in seen:
                        seen.add(entity_id)
                        entity_id = aliases[entity_id]
                    return entity_id

                edge_groups: dict[
                    tuple[str, str], list[dict[str, Any]]
                ] = {}
                for edge in edges:
                    original_source = str(edge["source"])
                    original_target = str(edge["target"])
                    source_id = resolve_projection_alias(original_source)
                    target_id = resolve_projection_alias(original_target)
                    if source_id == target_id:
                        continue
                    rewritten = dict(edge)
                    rewritten["source"] = source_id
                    rewritten["target"] = target_id
                    rewritten["_projection_rewritten"] = (
                        source_id != original_source or target_id != original_target
                    )
                    rewritten["_projection_collapsed_ids"] = [
                        entity_id
                        for entity_id, resolved in (
                            (original_source, source_id),
                            (original_target, target_id),
                        )
                        if entity_id != resolved
                    ]
                    # Endpoint-level bundling is presentation only. Authority
                    # below is reduced over the primary exact relation triple.
                    pair = (source_id, target_id)
                    edge_groups.setdefault(pair, []).append(rewritten)

                def projection_edge_rank(edge: dict[str, Any]) -> tuple[Any, ...]:
                    """Prefer the strongest relation as a bundle's visible direction."""

                    relation_rank = {
                        "has-component": 0,
                        "shares-component": 1,
                        "developed-into": 2,
                        "developed-from": 2,
                        "derived-from": 2,
                        "has-history": 4,
                        "explains-component": 5,
                    }
                    relation = str(edge.get("relation", ""))
                    return (
                        relation_rank.get(relation, 3),
                        0 if bool(edge.get("verified", False)) else 1,
                        0
                        if str(edge.get("basis", "")).strip().casefold() == "book"
                        else 1,
                        0 if bool(edge.get("context_supported", False)) else 1,
                        1 if bool(edge.get("model_generated", False)) else 0,
                        -float(edge.get("confidence", 0.0)),
                        relation,
                        str(edge.get("source", "")),
                        str(edge.get("target", "")),
                        str(edge.get("id", "")),
                    )

                coalesced_edges: list[dict[str, Any]] = []
                for pair in sorted(edge_groups):
                    group = edge_groups[pair]
                    primary = min(group, key=projection_edge_rank)
                    changed = len(group) > 1 or any(
                        bool(edge["_projection_rewritten"]) for edge in group
                    )
                    if not changed:
                        edge = dict(primary)
                        edge.pop("_projection_rewritten", None)
                        edge.pop("_projection_collapsed_ids", None)
                        coalesced_edges.append(edge)
                        continue

                    primary_identity = _projection_edge_identity(
                        primary["source"], primary["relation"], primary["target"]
                    )
                    authority_group = [
                        edge
                        for edge in group
                        if _projection_edge_identity(
                            edge["source"], edge["relation"], edge["target"]
                        ) == primary_identity
                    ]

                    all_evidence_ids: list[str] = []
                    for edge in authority_group:
                        for evidence_id in edge.get("evidence_ids", []):
                            clean_id = str(evidence_id)
                            if clean_id and clean_id not in all_evidence_ids:
                                all_evidence_ids.append(clean_id)
                    evidence_truncated = any(
                        bool(edge.get("evidence_truncated", False))
                        for edge in authority_group
                    ) or len(all_evidence_ids) > evidence_limit
                    evidence_ids = all_evidence_ids[:evidence_limit]
                    verified = any(
                        bool(edge.get("verified", False)) for edge in authority_group
                    )
                    context_supported = any(
                        bool(edge.get("context_supported", False))
                        for edge in authority_group
                    )
                    model_generated = any(
                        bool(edge.get("model_generated", False))
                        for edge in authority_group
                    )
                    bases = sorted(
                        {
                            str(edge.get("basis", "")).strip()
                            for edge in authority_group
                            if str(edge.get("basis", "")).strip()
                        }
                    )
                    if verified:
                        support_status = "cited-assertion"
                    elif context_supported:
                        support_status = (
                            "model-context-supported"
                            if model_generated
                            else "context-supported"
                        )
                    elif model_generated:
                        support_status = "accepted-model-unverified"
                    else:
                        support_status = "accepted-uncited"

                    relation_groups: dict[
                        tuple[str, str, str], list[dict[str, Any]]
                    ] = {}
                    for edge in group:
                        variant = (
                            *_projection_edge_identity(
                                edge["source"], edge["relation"], edge["target"]
                            ),
                        )
                        relation_groups.setdefault(variant, []).append(edge)
                    relations = []
                    for variant in sorted(relation_groups):
                        members = relation_groups[variant]
                        variant_bases = sorted(
                            {
                                str(edge.get("basis", "")).strip()
                                for edge in members
                                if str(edge.get("basis", "")).strip()
                            }
                        )
                        relations.append(
                            {
                                "source": variant[0],
                                "target": variant[2],
                                "relation": variant[1],
                                "basis": (
                                    variant_bases[0]
                                    if len(variant_bases) == 1
                                    else "mixed"
                                ),
                                "confidence": max(
                                    float(edge.get("confidence", 0.0))
                                    for edge in members
                                ),
                                "verified": any(
                                    bool(edge.get("verified", False))
                                    for edge in members
                                ),
                                "model_generated": any(
                                    bool(edge.get("model_generated", False))
                                    for edge in members
                                ),
                                "source_edge_ids": sorted(
                                    str(edge.get("id", "")) for edge in members
                                )[:12],
                                "evidence_ids": sorted(
                                    {
                                        str(evidence_id)
                                        for edge in members
                                        for evidence_id in edge.get("evidence_ids", [])
                                        if str(evidence_id)
                                    }
                                )[:evidence_limit],
                            }
                        )

                    properties = dict(primary.get("properties", {}))
                    properties["projection_edge_bundle"] = {
                        "source_edge_ids": sorted(
                            {str(edge.get("id", "")) for edge in group}
                        )[:12],
                        "support_bases": bases,
                        "relations": relations,
                        "collapsed_entity_ids": sorted(
                            {
                                entity_id
                                for edge in group
                                for entity_id in edge[
                                    "_projection_collapsed_ids"
                                ]
                            }
                        )[:12],
                    }
                    edge_key = "|".join(
                        (*pair, *sorted(str(edge.get("id", "")) for edge in group))
                    )
                    coalesced_edges.append(
                        {
                            "id": "projection-bundle-"
                            + hashlib.sha256(edge_key.encode("utf-8")).hexdigest()[:24],
                            "assertion_id": "",
                            "subject_entity_id": subject_entity_id,
                            "source": str(primary["source"]),
                            "target": str(primary["target"]),
                            "relation": str(primary["relation"]),
                            "relations": relations,
                            "basis": bases[0] if len(bases) == 1 else "mixed",
                            "confidence": max(
                                float(edge.get("confidence", 0.0)) for edge in group
                            ),
                            "properties": bounded_projection_object(properties),
                            "support_status": support_status,
                            "verified": verified,
                            "context_supported": context_supported,
                            "model_generated": model_generated,
                            "evidence_ids": evidence_ids,
                            "evidence_count": len(all_evidence_ids),
                            "evidence_truncated": evidence_truncated,
                            "revision": max(
                                int(edge.get("revision", 1)) for edge in group
                            ),
                            "created_at": min(
                                str(edge.get("created_at", "")) for edge in group
                            ),
                            "updated_at": max(
                                str(edge.get("updated_at", "")) for edge in group
                            ),
                        }
                    )
                edges = coalesced_edges
                canonical_projection_edge_count = sum(
                    len(group) for group in edge_groups.values()
                )
                if aliases:
                    nodes = [
                        node for node in nodes if str(node["id"]) not in aliases
                    ]

                reachable = {display_subject_entity_id}
                changed = True
                while changed:
                    changed = False
                    for edge in edges:
                        source_id = str(edge["source"])
                        target_id = str(edge["target"])
                        if source_id in reachable or target_id in reachable:
                            before = len(reachable)
                            reachable.update((source_id, target_id))
                            changed = changed or len(reachable) != before
                nodes = [node for node in nodes if str(node["id"]) in reachable]
                edges = [
                    edge
                    for edge in edges
                    if str(edge["source"]) in reachable
                    and str(edge["target"]) in reachable
                ]

                if mode in {"root", "affix"} and display_subject_entity_id:
                    available_node_ids = {str(node["id"]) for node in nodes}
                    slide_focus_id = resolve_projection_alias(
                        display_subject_entity_id
                    )
                    common_history_ids = sorted(
                        {
                            resolve_projection_alias(entity_id)
                            for entity_id in branch_history_node_ids
                            if resolve_projection_alias(entity_id)
                            in available_node_ids
                            and resolve_projection_alias(entity_id)
                            != slide_focus_id
                        }
                    )
                    resolved_specs: list[dict[str, Any]] = []
                    seen_slide_derivatives: set[str] = set()
                    for spec in branch_slide_specs:
                        derivative_id = resolve_projection_alias(
                            str(spec["derivative_id"])
                        )
                        if (
                            derivative_id == slide_focus_id
                            or derivative_id not in available_node_ids
                            or derivative_id in seen_slide_derivatives
                        ):
                            continue
                        seen_slide_derivatives.add(derivative_id)
                        component_ids = sorted(
                            {
                                resolve_projection_alias(str(entity_id))
                                for entity_id in spec.get("component_ids", [])
                                if resolve_projection_alias(str(entity_id))
                                in available_node_ids
                                and resolve_projection_alias(str(entity_id))
                                not in {slide_focus_id, derivative_id}
                            }
                        )
                        resolved_specs.append(
                            {
                                "derivative_id": derivative_id,
                                "component_ids": component_ids,
                            }
                        )

                    slide_node_by_id = {
                        str(node["id"]): node for node in nodes
                    }
                    edge_by_id = {str(edge["id"]): edge for edge in edges}

                    def slide_edge_relations(edge: dict[str, Any]) -> set[str]:
                        relations = {str(edge.get("relation", ""))}
                        raw_relations = edge.get("relations", [])
                        if isinstance(raw_relations, list):
                            relations.update(str(item) for item in raw_relations)
                        return {item for item in relations if item}

                    # A relation bundle can expose a derivative that was not in
                    # the pre-coalescing branch specs. Recover it from the final
                    # display graph so every slide is based on visible topology.
                    fallback_specs: list[dict[str, Any]] = []
                    for edge in edges:
                        if not slide_edge_relations(edge).intersection(
                            {"has-component", "shares-component"}
                        ):
                            continue
                        source_id = str(edge["source"])
                        target_id = str(edge["target"])
                        if slide_focus_id == source_id:
                            derivative_id = target_id
                        elif slide_focus_id == target_id:
                            derivative_id = source_id
                        else:
                            continue
                        if str(
                            slide_node_by_id.get(derivative_id, {}).get(
                                "type", ""
                            )
                        ) != "term":
                            continue
                        secondary_ids: list[str] = []
                        for secondary_edge in edges:
                            if "has-component" not in slide_edge_relations(
                                secondary_edge
                            ):
                                continue
                            secondary_source = str(secondary_edge["source"])
                            secondary_target = str(secondary_edge["target"])
                            if secondary_source == derivative_id:
                                secondary_id = secondary_target
                            elif secondary_target == derivative_id:
                                secondary_id = secondary_source
                            else:
                                continue
                            if (
                                secondary_id != slide_focus_id
                                and str(
                                    slide_node_by_id.get(secondary_id, {}).get(
                                        "type", ""
                                    )
                                )
                                == "morpheme"
                            ):
                                secondary_ids.append(secondary_id)
                        fallback_specs.append(
                            {
                                "derivative_id": derivative_id,
                                "component_ids": sorted(set(secondary_ids))[:2],
                            }
                        )

                    resolved_by_derivative = {
                        str(spec["derivative_id"]): spec for spec in resolved_specs
                    }
                    for spec in sorted(
                        fallback_specs,
                        key=lambda item: (
                            _normalise(
                                str(
                                    slide_node_by_id.get(
                                        str(item["derivative_id"]), {}
                                    ).get("label", "")
                                )
                            ),
                            str(item["derivative_id"]),
                        ),
                    ):
                        derivative_id = str(spec["derivative_id"])
                        existing = resolved_by_derivative.get(derivative_id)
                        if existing is None:
                            resolved_specs.append(spec)
                            resolved_by_derivative[derivative_id] = spec
                        else:
                            existing["component_ids"] = sorted(
                                set(existing["component_ids"])
                                | set(spec["component_ids"])
                            )[:2]

                    # Two derivatives leave room for their secondary components
                    # and the nearest history stages inside the nine-node browser
                    # slide budget.
                    branches_per_slide = 2
                    slide_groups = [
                        resolved_specs[index : index + branches_per_slide]
                        for index in range(0, len(resolved_specs), branches_per_slide)
                    ]
                    if not slide_groups:
                        slide_groups = [[]]
                    anchor_spec = resolved_specs[0] if resolved_specs else None

                    slide_adjacency: dict[str, set[str]] = {}
                    for edge in edges:
                        source_id = str(edge["source"])
                        target_id = str(edge["target"])
                        slide_adjacency.setdefault(source_id, set()).add(target_id)
                        slide_adjacency.setdefault(target_id, set()).add(source_id)
                    history_distance = {slide_focus_id: 0}
                    frontier = [slide_focus_id]
                    while frontier:
                        current_id = frontier.pop(0)
                        for adjacent_id in sorted(
                            slide_adjacency.get(current_id, set())
                        ):
                            if adjacent_id in history_distance:
                                continue
                            history_distance[adjacent_id] = (
                                history_distance[current_id] + 1
                            )
                            frontier.append(adjacent_id)
                    ordered_history_ids = sorted(
                        common_history_ids,
                        key=lambda entity_id: (
                            history_distance.get(entity_id, 10_000), entity_id
                        ),
                    )

                    for ordinal, group in enumerate(slide_groups, start=1):
                        effective_group = group or (
                            [anchor_spec] if anchor_spec is not None else []
                        )
                        ordered_node_ids = [
                            slide_focus_id,
                            *(
                                str(spec["derivative_id"])
                                for spec in effective_group
                            ),
                        ]
                        for spec in effective_group:
                            ordered_node_ids.extend(
                                str(entity_id)
                                for entity_id in spec["component_ids"]
                            )
                        ordered_node_ids.extend(ordered_history_ids)
                        ordered_node_ids = list(dict.fromkeys(ordered_node_ids))[:9]
                        slide_node_set = set(ordered_node_ids)
                        induced_edges = {
                            edge_id: edge
                            for edge_id, edge in edge_by_id.items()
                            if str(edge["source"]) in slide_node_set
                            and str(edge["target"]) in slide_node_set
                        }
                        reachable_slide_ids = {slide_focus_id}
                        changed = True
                        while changed:
                            changed = False
                            for edge in induced_edges.values():
                                source_id = str(edge["source"])
                                target_id = str(edge["target"])
                                if (
                                    source_id in reachable_slide_ids
                                    or target_id in reachable_slide_ids
                                ):
                                    before = len(reachable_slide_ids)
                                    reachable_slide_ids.update(
                                        (source_id, target_id)
                                    )
                                    changed = changed or (
                                        len(reachable_slide_ids) != before
                                    )
                        ordered_node_ids = [
                            entity_id
                            for entity_id in ordered_node_ids
                            if entity_id in reachable_slide_ids
                        ]
                        edge_ids = sorted(
                            edge_id
                            for edge_id, edge in induced_edges.items()
                            if str(edge["source"]) in reachable_slide_ids
                            and str(edge["target"]) in reachable_slide_ids
                        )
                        label = (
                            f"{mode.title()} history"
                            if not effective_group
                            else (
                                f"{mode.title()} words"
                                if len(slide_groups) == 1
                                else f"{mode.title()} words {ordinal}"
                            )
                        )
                        slide_key = "|".join(
                            (
                                mode,
                                slide_focus_id,
                                str(ordinal),
                                *ordered_node_ids,
                                *edge_ids,
                            )
                        )
                        branch_slides.append(
                            {
                                "id": "branch-slide-"
                                + hashlib.sha256(
                                    slide_key.encode("utf-8")
                                ).hexdigest()[:24],
                                "label": label,
                                "focus_node_id": slide_focus_id,
                                "node_ids": ordered_node_ids,
                                "edge_ids": edge_ids,
                            }
                        )
            provenance_counts = {
                "canonical_edges": canonical_projection_edge_count,
                "display_edges": len(edges),
                "bundled_edges": max(
                    0, canonical_projection_edge_count - len(edges)
                ),
                "model_edges": sum(
                    bool(edge.get("model_generated", False)) for edge in edges
                ),
                "book_edges": sum(
                    str(edge.get("basis", "")).strip().casefold() == "book"
                    for edge in edges
                ),
                "other_edges": sum(
                    not bool(edge.get("model_generated", False))
                    and str(edge.get("basis", "")).strip().casefold() != "book"
                    for edge in edges
                ),
                "directly_cited_edges": sum(
                    bool(edge.get("verified", False)) for edge in edges
                ),
                "context_supported_edges": sum(
                    bool(edge.get("context_supported", False)) for edge in edges
                ),
                "unverified_edges": sum(
                    not bool(edge.get("verified", False)) for edge in edges
                ),
            }
            revision_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'relation_graph_revision'"
            ).fetchone()
            graph_revision = int(revision_row["value"]) if revision_row else 0

        applied_limits = {
            "nodes": max_nodes,
            "edges": max_edges,
            "depth": max_depth,
            "evidence_per_edge": evidence_limit,
        }
        truncated = traversal_truncated
        enrichment_status: dict[str, Any] | None = None
        if mode == "word":
            if len(nodes) <= 1 or not edges:
                enrichment_status = {
                    "state": "sparse",
                    "reason": (
                        "accepted-components-awaiting-distinct-history"
                        if owned_component_ids
                        else "no-accepted-origin-atoms"
                    ),
                    "automatic_enrichment": True,
                    "policy": "recursive-novelty-cooldown",
                }
            else:
                enrichment_status = {
                    "state": "ready",
                    "reason": "connected-origin-atoms",
                    "automatic_enrichment": False,
                }
        projection = {
            "projection_contract_version": (
                CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION
            ),
            "subject_entity_id": display_subject_entity_id,
            "canonical_subject_entity_id": canonical_subject_entity_id,
            "mode": mode,
            "branch_focus_id": branch_focus_id,
            "limits": applied_limits,
            "truncated": truncated,
            "provenance_counts": provenance_counts,
            "nodes": nodes,
            "edges": edges,
            "branch_slides": branch_slides,
            "include_shared_words": mode in {"root", "affix"},
        }
        if enrichment_status is not None:
            projection["enrichment_status"] = enrichment_status
        projection_hash = hashlib.sha256(
            json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            **projection,
            "graph_revision": graph_revision,
            "projection_hash": projection_hash,
        }

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
            connection.execute("BEGIN IMMEDIATE")
            self._require_unarchived_endpoints(connection, term_id, morpheme_id)
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

    def link_morpheme_once(
        self,
        term_id: str,
        morpheme_id: str,
        surface: str,
        *,
        basis: str = "model",
        confidence: float = 0.5,
    ) -> tuple[str, bool]:
        """Append one component pair without replacing an earlier analysis slot."""

        confidence = max(0.0, min(float(confidence), 1.0))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_unarchived_endpoints(connection, term_id, morpheme_id)
            existing = connection.execute(
                """SELECT ordinal FROM term_morphemes
                   WHERE term_id = ? AND morpheme_id = ?
                   ORDER BY ordinal LIMIT 1""",
                (term_id, morpheme_id),
            ).fetchone()
            if existing is None:
                ordinal = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(ordinal), -1) + 1
                           FROM term_morphemes WHERE term_id = ?""",
                        (term_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id, morpheme_id, ordinal, surface,
                           confidence, basis
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        term_id,
                        morpheme_id,
                        ordinal,
                        surface,
                        confidence,
                        basis,
                    ),
                )
                created = True
            else:
                ordinal = int(existing["ordinal"])
                created = False
            edge = connection.execute(
                """SELECT edge_id FROM entity_edges
                   WHERE source_entity_id = ? AND target_entity_id = ?
                     AND relation = 'has-component' AND status = 'accepted'
                   ORDER BY updated_at DESC, edge_id LIMIT 1""",
                (term_id, morpheme_id),
            ).fetchone()
            connection.commit()
        if edge is not None:
            return str(edge["edge_id"]), created
        edge_id = self.add_edge(
            term_id,
            morpheme_id,
            "has-component",
            basis=basis,
            confidence=confidence,
            properties={"ordinal": ordinal, "surface": surface},
        )
        return edge_id, created

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

    def add_entity_meaning(
        self,
        subject_entity_id: str,
        definition: str,
        *,
        language: str = "en",
        part_of_speech: str = "",
        register_label: str = "",
        domain_label: str = "",
        sense_order: int = 0,
        status: str = "accepted",
        quality_score: float | None = None,
        basis: str = "reviewed",
        confidence: float = 1.0,
        evidence_ids: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        record_provenance_revision: bool = True,
        revision_model: str = "",
        revision_prompt_version: str = "",
        revision_reason: str = "",
    ) -> str:
        subject_entity_id = subject_entity_id.strip()
        if not subject_entity_id:
            raise ValueError("meaning subject entity ID is empty")
        language = _language(language)
        definition = definition.strip()
        if not definition:
            raise ValueError("meaning definition is empty")
        basis = basis.strip().lower() or "model"
        confidence = max(0.0, min(float(confidence), 1.0))
        if basis == "model":
            confidence = min(confidence, 0.7)
        selected_evidence = list(
            dict.fromkeys(str(item) for item in evidence_ids if str(item))
        )
        if basis == "model":
            selected_evidence = []
        provenance_metadata = dict(metadata or {})
        provenance_metadata["evidence_ids"] = selected_evidence
        canonical_key = (
            f"meaning:{subject_entity_id}:{language}:{sense_order}:"
            f"{_normalise(definition)}"
        )
        with closing(self._connect()) as connection:
            subject = connection.execute(
                "SELECT entity_type FROM entities WHERE entity_id = ?",
                (subject_entity_id,),
            ).fetchone()
            if subject is None:
                raise ValueError(f"meaning subject does not exist: {subject_entity_id}")
            term_id = (
                subject_entity_id if str(subject["entity_type"]) == "term" else None
            )
            entity_id = self._upsert_entity(
                connection,
                "meaning",
                canonical_key,
                definition,
                {
                    "subject_entity_id": subject_entity_id,
                    "term_id": term_id,
                    "language": language,
                    "definition": definition,
                    "part_of_speech": part_of_speech.strip(),
                    "register_label": register_label.strip(),
                    "domain_label": domain_label.strip(),
                    "sense_order": sense_order,
                    "provenance": {
                        "basis": basis,
                        "confidence": confidence,
                        **provenance_metadata,
                    },
                },
                status=status,
                quality_score=quality_score,
            )
            connection.execute(
                """INSERT INTO meanings(
                       entity_id, subject_entity_id, term_id, language, definition,
                       part_of_speech, register_label, domain_label, sense_order
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                       subject_entity_id = excluded.subject_entity_id,
                       term_id = excluded.term_id,
                       definition = excluded.definition,
                       part_of_speech = excluded.part_of_speech,
                       register_label = excluded.register_label,
                       domain_label = excluded.domain_label""",
                (
                    entity_id,
                    subject_entity_id,
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
        self.add_edge(
            subject_entity_id,
            entity_id,
            "has-meaning",
            basis=basis,
            confidence=confidence,
            properties=provenance_metadata,
        )
        if basis == "model" and provenance_metadata.get("semantic_enrichment_job_id"):
            # A newer run may lower confidence for the same text. Do not let an
            # older model score freeze its provenance; stronger owners are untouched.
            with closing(self._connect()) as connection:
                connection.execute(
                    """UPDATE entity_edges SET confidence=?,properties=?,updated_at=?
                       WHERE source_entity_id=? AND target_entity_id=?
                         AND relation='has-meaning' AND status='accepted'
                         AND basis='model'""",
                    (confidence, json.dumps(provenance_metadata, ensure_ascii=False),
                     _now(), subject_entity_id, entity_id),
                )
                connection.commit()
        for evidence_id in selected_evidence:
            self.link_evidence(
                entity_id,
                evidence_id,
                claim=f"{language} meaning: {definition[:160]}",
                confidence=confidence,
            )
        if record_provenance_revision:
            provenance = {
                "basis": basis,
                "confidence": confidence,
                **provenance_metadata,
            }
            revision_payload = {
                "subject_entity_id": subject_entity_id,
                "term_id": term_id,
                "language": language,
                "definition": definition,
                "part_of_speech": part_of_speech.strip(),
                "register_label": register_label.strip(),
                "domain_label": domain_label.strip(),
                "sense_order": sense_order,
                "provenance": provenance,
            }
            resolved_model = (
                revision_model.strip()
                or str(provenance_metadata.get("model", "")).strip()
                or ("local model" if basis == "model" else basis)
            )
            resolved_prompt_version = (
                revision_prompt_version.strip()
                or str(provenance_metadata.get("prompt_version", "")).strip()
            )
            resolved_reason = (
                revision_reason.strip()
                or str(provenance_metadata.get("reason", "")).strip()
                or f"{basis}-owned {language} entity meaning"
            )
            revision_exists = False
            with closing(self._connect()) as connection:
                for row in connection.execute(
                    """SELECT model, prompt_version, reason, accepted, payload
                       FROM entity_revisions WHERE entity_id = ?""",
                    (entity_id,),
                ):
                    try:
                        stored_payload = json.loads(row["payload"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if (
                        stored_payload == revision_payload
                        and str(row["model"]) == resolved_model
                        and str(row["prompt_version"]) == resolved_prompt_version
                        and str(row["reason"]) == resolved_reason
                        and bool(row["accepted"]) == (status == "accepted")
                    ):
                        revision_exists = True
                        break
            if not revision_exists:
                self.record_revision(
                    entity_id,
                    revision_payload,
                    model=resolved_model,
                    prompt_version=resolved_prompt_version,
                    reason=resolved_reason,
                    accepted=status == "accepted",
                )
        if status == "accepted":
            with closing(self._connect()) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    subject = connection.execute(
                        """SELECT entity_type FROM entities
                           WHERE entity_id = ?""",
                        (subject_entity_id,),
                    ).fetchone()
                    if (
                        subject is not None
                        and str(subject["entity_type"]) == "morpheme"
                    ):
                        cached = connection.execute(
                            "SELECT meaning FROM morphemes WHERE entity_id = ?",
                            (subject_entity_id,),
                        ).fetchone()
                        if (
                            cached is not None
                            and not str(cached["meaning"] or "").strip()
                        ):
                            connection.execute(
                                "UPDATE morphemes SET meaning = ? WHERE entity_id = ?",
                                (definition, subject_entity_id),
                            )
                            entity = connection.execute(
                                "SELECT payload FROM entities WHERE entity_id = ?",
                                (subject_entity_id,),
                            ).fetchone()
                            payload = (
                                _json_object(str(entity["payload"])) if entity else {}
                            )
                            payload["meaning"] = definition
                            connection.execute(
                                """UPDATE entities SET payload = ?, updated_at = ?
                                   WHERE entity_id = ?""",
                                (
                                    json.dumps(payload, ensure_ascii=False),
                                    _now(),
                                    subject_entity_id,
                                ),
                            )
                    self._mark_projection_repairs_for_entities(
                        connection, (subject_entity_id, entity_id)
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return entity_id

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
        quality_score: float | None = None,
        basis: str = "reviewed",
        confidence: float = 1.0,
        evidence_ids: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return self.add_entity_meaning(
            term_id,
            definition,
            language=language,
            part_of_speech=part_of_speech,
            register_label=register_label,
            domain_label=domain_label,
            sense_order=sense_order,
            status=status,
            quality_score=quality_score,
            basis=basis,
            confidence=confidence,
            evidence_ids=evidence_ids,
            metadata=metadata,
            record_provenance_revision=False,
        )

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
                {"language": language, "system": system, "dialect": dialect.strip()},
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
        definition: str = "",
        definition_model: str = "",
        definition_prompt_version: str = "",
        transliteration: str = "",
        usage_note: str = "",
        source_meaning_id: str | None = None,
        target_term_id: str | None = None,
        status: str = "accepted",
        quality_score: float | None = None,
        basis: str = "model",
        confidence: float | None = None,
        evidence_ids: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> str:
        target_language = _language(target_language)
        text = text.strip()
        normalized = _normalise(text)
        if not normalized:
            raise ValueError("translation text is empty")
        definition = definition.strip()
        if definition and (not target_term_id or not source_meaning_id):
            raise ValueError(
                "a translated definition requires target term and source meaning IDs"
            )
        basis = basis.strip().lower() or "model"
        edge_confidence = (
            float(confidence)
            if confidence is not None
            else (float(quality_score) if quality_score is not None else 0.5)
        )
        edge_confidence = max(0.0, min(edge_confidence, 1.0))
        if basis == "model":
            edge_confidence = min(edge_confidence, 0.7)
        selected_evidence = list(
            dict.fromkeys(str(item) for item in evidence_ids if str(item))
        )
        if basis == "model":
            selected_evidence = []
        provenance_metadata = dict(metadata or {})
        if definition_model.strip():
            provenance_metadata.setdefault("model", definition_model.strip())
        if definition_prompt_version.strip():
            provenance_metadata.setdefault(
                "prompt_version", definition_prompt_version.strip()
            )
        provenance_metadata["evidence_ids"] = selected_evidence
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
                {
                    "target_language": target_language,
                    "transliteration": transliteration,
                    "usage_note": usage_note,
                    "provenance": {
                        "basis": basis,
                        "confidence": edge_confidence,
                        **provenance_metadata,
                    },
                },
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
        self.add_edge(
            source_term_id,
            entity_id,
            "has-translation",
            basis=basis,
            confidence=edge_confidence,
            properties=provenance_metadata,
            status=status,
        )
        if target_term_id:
            self.add_edge(
                entity_id,
                target_term_id,
                "renders-as",
                basis=basis,
                confidence=edge_confidence,
                properties=provenance_metadata,
                status=status,
            )
        if definition and target_term_id and source_meaning_id:
            definition_metadata = {
                **provenance_metadata,
                "source_meaning_id": source_meaning_id,
                "translation_id": entity_id,
            }
            target_meaning_id = self.add_meaning(
                target_term_id,
                target_language,
                definition,
                status=status,
                quality_score=quality_score,
                basis=basis,
                confidence=edge_confidence,
                evidence_ids=selected_evidence,
                metadata=definition_metadata,
            )
            provenance = {
                "basis": basis,
                "source_meaning_id": source_meaning_id,
                "translation_id": entity_id,
                "evidence_ids": selected_evidence,
                "confidence": edge_confidence,
                "metadata": provenance_metadata,
            }
            self.add_edge(
                target_meaning_id,
                source_meaning_id,
                "translated-from",
                basis=basis,
                confidence=edge_confidence,
                properties=definition_metadata,
                status=status,
            )
            self.record_revision(
                target_meaning_id,
                {
                    "language": target_language,
                    "definition": definition,
                    "provenance": provenance,
                },
                model=definition_model.strip() or "local model",
                prompt_version=definition_prompt_version.strip(),
                reason=f"{basis}-owned {target_language} translated definition",
                accepted=status == "accepted",
            )
        for evidence_id in selected_evidence:
            self.link_evidence(
                entity_id,
                evidence_id,
                claim=f"translation target wording: {text[:160]}",
                confidence=edge_confidence,
            )
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
                {
                    "language": language,
                    "period": period_label,
                    "period_label": period_label,
                    "meaning": meaning,
                    "date_min": date_min,
                    "date_max": date_max,
                },
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
        basis: str = "reviewed",
        confidence: float = 0.5,
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
                {
                    **(properties or {}),
                    "language": language,
                    "event_type": event_type,
                    "period_label": period_label.strip(),
                    "date_min": date_min,
                    "date_max": date_max,
                    "description": description,
                },
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
        self.add_edge(
            subject_entity_id,
            entity_id,
            "has-history",
            basis=basis,
            confidence=confidence,
        )
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
        corpus_id = corpus_id.strip()
        source_entry_id = source_entry_id.strip()
        source_hash = source_hash.strip()
        locator = locator.strip()
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
                    corpus_id,
                    source_entry_id,
                    source_hash,
                    locator,
                    excerpt.strip(),
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            stored = connection.execute(
                """SELECT evidence_id FROM evidence_records
                   WHERE corpus_id = ? AND source_entry_id = ?
                     AND source_hash = ? AND locator = ?""",
                (corpus_id, source_entry_id, source_hash, locator),
            ).fetchone()
            if stored is None:
                raise RuntimeError("evidence upsert did not persist a record")
            evidence_id = str(stored["evidence_id"])
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
            before = connection.execute(
                """SELECT confidence FROM entity_evidence
                   WHERE entity_id = ? AND evidence_id = ? AND claim = ?""",
                (entity_id, evidence_id, claim.strip()),
            ).fetchone()
            bounded_confidence = max(0.0, min(float(confidence), 1.0))
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
                    bounded_confidence,
                ),
            )
            if before is None or float(before["confidence"]) != bounded_confidence:
                self._mark_projection_repairs_for_entities(
                    connection, (entity_id,)
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
        """Persist liveness independently of long semantic write transactions."""

        if status not in {"running", "stopped"}:
            raise ValueError(f"invalid worker status: {status!r}")
        worker_key = worker_key.strip()
        if not worker_key:
            raise ValueError("worker key is required")
        heartbeat_database = self.database.with_name(
            self.database.name + ".heartbeat.sqlite3"
        )
        with closing(sqlite3.connect(heartbeat_database, timeout=2.0)) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS worker_heartbeat (
                       worker_key TEXT PRIMARY KEY,
                       status TEXT NOT NULL,
                       blocker TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                   )"""
            )
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
        heartbeat_database = self.database.with_name(
            self.database.name + ".heartbeat.sqlite3"
        )
        # Fall back only before the upgraded worker has written its first beat.
        # A stopped or stale sidecar must never be masked by a legacy row.
        if heartbeat_database.is_file():
            heartbeat_connection = sqlite3.connect(
                heartbeat_database.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=2.0,
            )
            heartbeat_connection.row_factory = sqlite3.Row
        else:
            heartbeat_connection = self._connect()
        with closing(heartbeat_connection) as connection:
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

    @staticmethod
    def _ensure_morphology_enrichment_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """Create only the runtime state layered on an existing v3 ingest."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS morphology_enrichment_clusters_v3 (
                cluster_id TEXT PRIMARY KEY,
                parser_version TEXT NOT NULL,
                cluster_key TEXT NOT NULL UNIQUE,
                cluster_kind TEXT NOT NULL CHECK(
                    cluster_kind IN ('declaration', 'component', 'link')
                ),
                priority_class INTEGER NOT NULL,
                canonical_subject_key TEXT NOT NULL,
                subject_entity_id TEXT REFERENCES entities(entity_id),
                evidence_fingerprint TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(
                    status IN (
                        'pending', 'queued', 'running', 'complete', 'cooldown'
                    )
                ),
                lease_token TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_job_id TEXT REFERENCES preparation_jobs(job_id),
                last_completed_fingerprint TEXT NOT NULL DEFAULT '',
                last_output_fingerprint TEXT NOT NULL DEFAULT '',
                next_eligible_at TEXT NOT NULL DEFAULT '',
                last_outcome_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_morph_enrichment_v3_next
                ON morphology_enrichment_clusters_v3(
                    status, priority_class, next_eligible_at, updated_at
                );
            CREATE INDEX IF NOT EXISTS idx_morph_enrichment_v3_last_job
                ON morphology_enrichment_clusters_v3(last_job_id, parser_version);
            CREATE TABLE IF NOT EXISTS morphology_enrichment_epochs_v3 (
                epoch_id TEXT PRIMARY KEY,
                cluster_id TEXT NOT NULL REFERENCES
                    morphology_enrichment_clusters_v3(cluster_id),
                epoch_number INTEGER NOT NULL,
                input_fingerprint TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES preparation_jobs(job_id),
                status TEXT NOT NULL CHECK(
                    status IN ('queued', 'running', 'complete', 'failed')
                ),
                output_fingerprint TEXT NOT NULL DEFAULT '',
                outcome_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                UNIQUE(cluster_id, epoch_number),
                UNIQUE(job_id)
            );
            CREATE INDEX IF NOT EXISTS idx_morph_enrichment_epoch_v3_status
                ON morphology_enrichment_epochs_v3(status, updated_at);
            CREATE TABLE IF NOT EXISTS morphology_enrichment_scheduler_v3 (
                scheduler_key TEXT PRIMARY KEY,
                cursor_offset INTEGER NOT NULL DEFAULT 0,
                cycle_number INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        KnowledgeStore._install_projection_revision_triggers(
            connection, ("morphology_enrichment_clusters_v3",)
        )

    @staticmethod
    def _retire_stale_morphology_enrichment_jobs_connection(
        connection: sqlite3.Connection,
    ) -> int:
        """Pause queued enrichment work owned by a retired parser version."""

        available = connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type = 'table'
               AND name = 'morphology_enrichment_clusters_v3'"""
        ).fetchone()
        if available is None:
            return 0
        rows = connection.execute(
            """SELECT job.job_id, cluster.cluster_id, cluster.parser_version
               FROM preparation_jobs AS job
               JOIN morphology_enrichment_clusters_v3 AS cluster
                 ON cluster.last_job_id = job.job_id
               WHERE job.job_type = 'enrich-morphology-v3'
                 AND job.status = 'queued'
                 AND cluster.parser_version <> ?""",
            (CURRENT_MORPHOLOGY_PARSER_VERSION,),
        ).fetchall()
        if not rows:
            return 0
        timestamp = _now()
        for row in rows:
            retired_version = str(row["parser_version"])
            reason = (
                f"retired morphology parser version {retired_version}; current "
                f"version is {CURRENT_MORPHOLOGY_PARSER_VERSION}"
            )
            connection.execute(
                """UPDATE preparation_jobs
                   SET status = 'paused', locked_at = '', error = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'queued'""",
                (reason, timestamp, str(row["job_id"])),
            )
            connection.execute(
                """UPDATE morphology_enrichment_clusters_v3
                   SET status = 'cooldown', lease_token = '', lease_expires_at = '',
                       next_eligible_at = '', last_outcome_json = ?, updated_at = ?
                   WHERE cluster_id = ?""",
                (
                    json.dumps(
                        {
                            "outcome": "retired-parser-version",
                            "parser_version": retired_version,
                            "current_parser_version": (
                                CURRENT_MORPHOLOGY_PARSER_VERSION
                            ),
                        },
                        sort_keys=True,
                    ),
                    timestamp,
                    str(row["cluster_id"]),
                ),
            )
            connection.execute(
                """UPDATE morphology_enrichment_epochs_v3
                   SET status = 'failed', outcome_json = ?, completed_at = ?,
                       updated_at = ?
                   WHERE job_id = ? AND status = 'queued'""",
                (
                    json.dumps({"outcome": "retired-parser-version"}),
                    timestamp,
                    timestamp,
                    str(row["job_id"]),
                ),
            )
        return len(rows)

    @staticmethod
    def _morphology_enrichment_candidates(
        connection: sqlite3.Connection,
        *,
        scan_limit: int = 192,
        scan_offset: int = 0,
        observation_kinds: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Return a bounded queue over exact current-parser observations."""

        scan_limit = max(8, min(int(scan_limit), 512))
        allowed_kinds = {
            str(item).strip() for item in observation_kinds
            if str(item).strip() in {"declaration", "component", "link"}
        }
        if not allowed_kinds:
            allowed_kinds = {"declaration", "component", "link"}
        rows: list[dict[str, Any]] = []
        scan_offset = max(0, int(scan_offset))
        per_kind = max(4, min(128, scan_limit // 3))
        if "declaration" in allowed_kinds:
            rows.extend(
                {
                    **dict(row),
                    "cluster_kind": "declaration",
                    "observation_id": str(row["declaration_id"]),
                }
                for row in connection.execute(
                    """SELECT declaration.declaration_id,
                              declaration.declaration_type,
                              declaration.object_value,
                              declaration.object_normalized_form,
                              declaration.language AS declaration_language,
                              declaration.period_label,
                              declaration.confidence,
                              declaration.qwen_ready,
                              declaration.properties_json,
                              source.source_observation_id,
                              source.source_kind, source.evidence_id,
                              source.headword, source.raw_excerpt,
                              source.raw_fingerprint,
                              component.component_observation_id,
                              component.raw_form, component.normalized_form,
                              component.gloss, component.proposed_kind,
                              component.materialized_entity_id,
                              COALESCE((
                                  SELECT term.materialized_entity_id
                                  FROM morphology_term_observations_v3 AS term
                                  WHERE term.source_observation_id =
                                        source.source_observation_id
                                    AND term.materialized_entity_id IS NOT NULL
                                  ORDER BY term.clause_index, term.term_observation_id
                                  LIMIT 1
                              ), '') AS term_entity_id,
                              COALESCE((
                                  SELECT term.raw_term
                                  FROM morphology_term_observations_v3 AS term
                                  WHERE term.source_observation_id =
                                        source.source_observation_id
                                  ORDER BY term.clause_index, term.term_observation_id
                                  LIMIT 1
                              ), source.headword) AS term_text,
                              evidence.source_hash, evidence.locator,
                              evidence.excerpt AS evidence_excerpt,
                              evidence.payload AS evidence_payload
                       FROM morphology_declarations_v3 AS declaration
                       JOIN morphology_source_observations_v3 AS source
                         ON source.source_observation_id =
                            declaration.source_observation_id
                       LEFT JOIN morphology_component_observations_v3 AS component
                         ON component.component_observation_id =
                            declaration.subject_component_observation_id
                       JOIN evidence_records AS evidence
                         ON evidence.evidence_id = declaration.evidence_id
                       WHERE source.parser_version = ?
                         AND declaration.declaration_type IN (
                           'history', 'allomorph', 'source', 'opposition'
                       )
                         AND declaration.qwen_ready = 1
                       ORDER BY CASE declaration.declaration_type
                           WHEN 'history' THEN 0 WHEN 'allomorph' THEN 1
                           WHEN 'source' THEN 2 ELSE 3 END,
                           source.source_entry_id, declaration.declaration_id
                       LIMIT ? OFFSET ?""",
                    (
                        CURRENT_MORPHOLOGY_PARSER_VERSION,
                        per_kind,
                        scan_offset,
                    ),
                )
            )
        if "component" in allowed_kinds:
            rows.extend(
                {
                    **dict(row),
                    "cluster_kind": "component",
                    "observation_id": str(row["component_observation_id"]),
                    "declaration_type": "ambiguous-kind",
                    "object_value": "",
                    "object_normalized_form": "",
                    "declaration_language": "",
                    "period_label": "",
                    "confidence": float(row["kind_confidence"]),
                }
                for row in connection.execute(
                    """SELECT component.component_observation_id,
                              component.source_observation_id,
                              component.raw_form, component.normalized_form,
                              component.gloss, component.proposed_kind,
                              component.kind_confidence,
                              component.properties_json,
                              component.materialized_entity_id,
                              source.source_kind, source.evidence_id,
                              source.headword, source.raw_excerpt,
                              source.raw_fingerprint,
                              COALESCE((
                                  SELECT term.materialized_entity_id
                                  FROM morphology_term_observations_v3 AS term
                                  WHERE term.source_observation_id =
                                        source.source_observation_id
                                    AND term.materialized_entity_id IS NOT NULL
                                  ORDER BY term.clause_index, term.term_observation_id
                                  LIMIT 1
                              ), '') AS term_entity_id,
                              COALESCE((
                                  SELECT term.raw_term
                                  FROM morphology_term_observations_v3 AS term
                                  WHERE term.source_observation_id =
                                        source.source_observation_id
                                  ORDER BY term.clause_index, term.term_observation_id
                                  LIMIT 1
                              ), source.headword) AS term_text,
                              evidence.source_hash, evidence.locator,
                              evidence.excerpt AS evidence_excerpt,
                              evidence.payload AS evidence_payload
                       FROM morphology_component_observations_v3 AS component
                       JOIN morphology_source_observations_v3 AS source
                         ON source.source_observation_id =
                            component.source_observation_id
                       JOIN evidence_records AS evidence
                         ON evidence.evidence_id = component.evidence_id
                       WHERE source.parser_version = ?
                         AND component.state = 'ambiguous'
                         AND component.qwen_ready = 1
                       ORDER BY component.kind_confidence DESC,
                                source.source_entry_id,
                                component.component_observation_id
                       LIMIT ? OFFSET ?""",
                    (
                        CURRENT_MORPHOLOGY_PARSER_VERSION,
                        per_kind,
                        scan_offset,
                    ),
                )
            )
        if "link" in allowed_kinds:
            rows.extend(
                {
                    **dict(row),
                    "cluster_kind": "link",
                    "observation_id": str(row["formula_link_id"]),
                    "declaration_type": "ambiguous-link",
                    "object_value": "",
                    "object_normalized_form": "",
                    "declaration_language": "",
                    "period_label": "",
                    "gloss": str(row["component_gloss"]),
                    "confidence": float(row["confidence"]),
                    "materialized_entity_id": str(
                        row["component_entity_id"] or ""
                    ),
                    "properties_json": str(row["link_properties_json"]),
                }
                for row in connection.execute(
                    """SELECT link.formula_link_id,
                              link.source_observation_id,
                              link.surface_relation, link.confidence,
                              link.properties_json AS link_properties_json,
                              component.component_observation_id,
                              component.raw_form, component.normalized_form,
                              component.gloss AS component_gloss,
                              component.proposed_kind,
                              component.materialized_entity_id AS component_entity_id,
                              term.term_observation_id,
                              term.raw_term AS term_text,
                              term.materialized_entity_id AS term_entity_id,
                              source.source_kind, source.evidence_id,
                              source.headword, source.raw_excerpt,
                              source.raw_fingerprint,
                              evidence.source_hash, evidence.locator,
                              evidence.excerpt AS evidence_excerpt,
                              evidence.payload AS evidence_payload
                       FROM morphology_formula_links_v3 AS link
                       JOIN morphology_source_observations_v3 AS source
                         ON source.source_observation_id = link.source_observation_id
                       JOIN morphology_component_observations_v3 AS component
                         ON component.component_observation_id =
                            link.component_observation_id
                       JOIN morphology_term_observations_v3 AS term
                         ON term.term_observation_id = link.term_observation_id
                       JOIN evidence_records AS evidence
                         ON evidence.evidence_id = link.evidence_id
                       WHERE source.parser_version = ?
                         AND link.state = 'ambiguous' AND link.qwen_ready = 1
                       ORDER BY link.confidence DESC, source.source_entry_id,
                                link.formula_link_id
                       LIMIT ? OFFSET ?""",
                    (
                        CURRENT_MORPHOLOGY_PARSER_VERSION,
                        per_kind,
                        scan_offset,
                    ),
                )
            )

        priority = {
            "history": 0,
            "allomorph": 1,
            "source": 2,
            "opposition": 3,
            "ambiguous-kind": 4,
            "ambiguous-link": 5,
        }
        clusters: dict[str, dict[str, Any]] = {}
        for row in rows:
            kind = str(row["cluster_kind"])
            form = str(row.get("normalized_form") or row.get("raw_form") or "")
            form_key = _normalise(form.strip("- "))
            term_id = str(row.get("term_entity_id") or "")
            term_text = str(row.get("term_text") or row.get("headword") or "")
            component_id = str(row.get("materialized_entity_id") or "")
            if kind == "link":
                anchor = (
                    f"term:{term_id or _normalise(term_text)}:component:"
                    f"{component_id or form_key}"
                )
                subject_entity_id = term_id or None
            else:
                anchor = f"component:{component_id or form_key or row['source_observation_id']}"
                subject_entity_id = component_id or term_id or None
            subtype = str(row.get("declaration_type") or kind)
            cluster_key = ":".join(
                (
                    "morphology",
                    CURRENT_MORPHOLOGY_PARSER_VERSION,
                    kind,
                    subtype,
                    anchor,
                    str(row["evidence_id"]),
                )
            )
            observation = {
                "observation_id": str(row["observation_id"]),
                "observation_kind": kind,
                "subtype": subtype,
                "raw_form": str(row.get("raw_form") or ""),
                "normalized_form": str(row.get("normalized_form") or ""),
                "proposed_kind": str(row.get("proposed_kind") or ""),
                "gloss": str(row.get("gloss") or ""),
                "term": term_text,
                "object_value": str(row.get("object_value") or ""),
                "object_normalized_form": str(
                    row.get("object_normalized_form") or ""
                ),
                "language": str(row.get("declaration_language") or ""),
                "period_label": str(row.get("period_label") or ""),
                "confidence": float(row.get("confidence") or 0.0),
                "properties": _json_object(str(row.get("properties_json") or "{}")),
            }
            cluster = clusters.setdefault(
                cluster_key,
                {
                    "cluster_key": cluster_key,
                    "cluster_kind": kind,
                    "priority_class": priority.get(subtype, 9),
                    "canonical_subject_key": anchor,
                    "subject_entity_id": subject_entity_id,
                    "evidence_ids": [str(row["evidence_id"])],
                    "source": {
                        "parser_version": CURRENT_MORPHOLOGY_PARSER_VERSION,
                        "source_observation_id": str(row["source_observation_id"]),
                        "source_kind": str(row["source_kind"]),
                        "headword": str(row["headword"]),
                        "raw_excerpt": str(row["raw_excerpt"]),
                        "raw_fingerprint": str(row["raw_fingerprint"]),
                        "source_hash": str(row["source_hash"]),
                        "locator": str(row["locator"]),
                    },
                    "focus": {
                        "component_entity_id": component_id,
                        "component_form": form,
                        "component_kind": str(row.get("proposed_kind") or ""),
                        "term_entity_id": term_id,
                        "term": term_text,
                    },
                    "observations": [],
                    "evidence_material": [],
                },
            )
            if observation not in cluster["observations"]:
                cluster["observations"].append(observation)
            cluster["evidence_material"].append(
                {
                    "evidence_id": str(row["evidence_id"]),
                    "source_hash": str(row["source_hash"]),
                    "raw_fingerprint": str(row["raw_fingerprint"]),
                    "excerpt": str(row["evidence_excerpt"]),
                    "payload": str(row["evidence_payload"]),
                }
            )

        result: list[dict[str, Any]] = []
        for cluster in clusters.values():
            evidence_material = cluster.pop("evidence_material")
            evidence_fingerprint = hashlib.sha256(
                json.dumps(
                    evidence_material,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            payload_json = json.dumps(
                cluster,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            input_fingerprint = hashlib.sha256(
                (evidence_fingerprint + "\0" + payload_json).encode("utf-8")
            ).hexdigest()
            result.append(
                {
                    **cluster,
                    "evidence_fingerprint": evidence_fingerprint,
                    "input_fingerprint": input_fingerprint,
                    "payload_json": payload_json,
                }
            )
        result.sort(
            key=lambda item: (
                int(item["priority_class"]),
                str(item["canonical_subject_key"]),
                str(item["cluster_key"]),
            )
        )
        return result[:scan_limit]

    def plan_morphology_v3_enrichment(
        self,
        *,
        model: str,
        prompt_version: str = "morphology-v3-enrichment-v1",
        scan_limit: int = 192,
        observation_kinds: Iterable[str] = (),
    ) -> dict[str, Any] | None:
        """Atomically lease one novel current-parser observation cluster."""

        with closing(self._connect()) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """SELECT name FROM sqlite_master WHERE type = 'table'
                       AND name IN (
                           'morphology_source_observations_v3',
                           'morphology_component_observations_v3',
                           'morphology_formula_links_v3',
                           'morphology_declarations_v3'
                       )"""
                )
            }
            if len(tables) != 4:
                return None
            self._ensure_morphology_enrichment_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            timestamp = _now()
            now = datetime.now(UTC)
            scheduler_key = "retained-for-local-qwen"
            connection.execute(
                """INSERT OR IGNORE INTO morphology_enrichment_scheduler_v3(
                       scheduler_key, cursor_offset, cycle_number, updated_at
                   ) VALUES (?, 0, 0, ?)""",
                (scheduler_key, timestamp),
            )
            scheduler = connection.execute(
                """SELECT cursor_offset, cycle_number
                   FROM morphology_enrichment_scheduler_v3
                   WHERE scheduler_key = ?""",
                (scheduler_key,),
            ).fetchone()
            scan_offset = int(scheduler["cursor_offset"])
            active_rows = connection.execute(
                """SELECT cluster.cluster_id, cluster.failure_count,
                          cluster.input_fingerprint, cluster.status,
                          job.status AS job_status, job.error
                   FROM morphology_enrichment_clusters_v3 AS cluster
                   LEFT JOIN preparation_jobs AS job
                     ON job.job_id = cluster.last_job_id
                   WHERE cluster.parser_version = ?
                     AND cluster.status IN ('queued', 'running')""",
                (CURRENT_MORPHOLOGY_PARSER_VERSION,),
            ).fetchall()
            for active in active_rows:
                job_status = str(active["job_status"] or "")
                if job_status == "running" and str(active["status"]) != "running":
                    connection.execute(
                        """UPDATE morphology_enrichment_clusters_v3
                           SET status = 'running', updated_at = ?
                           WHERE cluster_id = ?""",
                        (timestamp, str(active["cluster_id"])),
                    )
                elif job_status == "failed":
                    failures = int(active["failure_count"]) + 1
                    cooldown_hours = min(24 * 30, 6 * (2 ** min(failures - 1, 7)))
                    next_eligible = (now + timedelta(hours=cooldown_hours)).isoformat()
                    connection.execute(
                        """UPDATE morphology_enrichment_clusters_v3
                           SET status = 'cooldown', failure_count = ?,
                               next_eligible_at = ?, lease_token = '',
                               lease_expires_at = '', last_outcome_json = ?,
                               updated_at = ? WHERE cluster_id = ?""",
                        (
                            failures,
                            next_eligible,
                            json.dumps(
                                {"outcome": "failed", "error": str(active["error"] or "")[:500]},
                                ensure_ascii=False,
                            ),
                            timestamp,
                            str(active["cluster_id"]),
                        ),
                    )
                    connection.execute(
                        """UPDATE morphology_enrichment_epochs_v3
                           SET status = 'failed', outcome_json = ?, updated_at = ?,
                               completed_at = ?
                           WHERE cluster_id = ? AND status IN ('queued', 'running')""",
                        (
                            json.dumps(
                                {"error": str(active["error"] or "")[:500]},
                                ensure_ascii=False,
                            ),
                            timestamp,
                            timestamp,
                            str(active["cluster_id"]),
                        ),
                    )

            candidates = self._morphology_enrichment_candidates(
                connection,
                scan_limit=scan_limit,
                scan_offset=scan_offset,
                observation_kinds=observation_kinds,
            )
            for candidate in candidates:
                cluster_id = _identifier(
                    "morphology-enrichment-cluster", str(candidate["cluster_key"])
                )
                existing = connection.execute(
                    """SELECT * FROM morphology_enrichment_clusters_v3
                       WHERE cluster_id = ?""",
                    (cluster_id,),
                ).fetchone()
                subject_entity_id = candidate["subject_entity_id"]
                if subject_entity_id:
                    accepted = connection.execute(
                        """SELECT 1 FROM entities
                           WHERE entity_id = ? AND status = 'accepted'""",
                        (subject_entity_id,),
                    ).fetchone()
                    if accepted is None:
                        subject_entity_id = None
                if existing is None:
                    connection.execute(
                    """INSERT INTO morphology_enrichment_clusters_v3(
                               cluster_id, parser_version, cluster_key,
                               cluster_kind, priority_class,
                               canonical_subject_key, subject_entity_id,
                               evidence_fingerprint, input_fingerprint,
                               payload_json, status, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?,
                                     ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                        (
                            cluster_id,
                            CURRENT_MORPHOLOGY_PARSER_VERSION,
                            candidate["cluster_key"],
                            candidate["cluster_kind"],
                            candidate["priority_class"],
                            candidate["canonical_subject_key"],
                            subject_entity_id,
                            candidate["evidence_fingerprint"],
                            candidate["input_fingerprint"],
                            candidate["payload_json"],
                            timestamp,
                            timestamp,
                        ),
                    )
                    existing = connection.execute(
                        """SELECT * FROM morphology_enrichment_clusters_v3
                           WHERE cluster_id = ?""",
                        (cluster_id,),
                    ).fetchone()
                elif (
                    str(existing["input_fingerprint"])
                    != str(candidate["input_fingerprint"])
                    or str(existing["payload_json"]) != str(candidate["payload_json"])
                ):
                    connection.execute(
                        """UPDATE morphology_enrichment_clusters_v3
                           SET parser_version = ?, priority_class = ?,
                               subject_entity_id = ?,
                               evidence_fingerprint = ?, input_fingerprint = ?,
                               payload_json = ?, status = CASE
                                   WHEN status IN ('queued', 'running') THEN status
                                   ELSE 'pending' END,
                               next_eligible_at = CASE
                                   WHEN status IN ('queued', 'running')
                                   THEN next_eligible_at ELSE '' END,
                               updated_at = ? WHERE cluster_id = ?""",
                        (
                            CURRENT_MORPHOLOGY_PARSER_VERSION,
                            candidate["priority_class"],
                            subject_entity_id,
                            candidate["evidence_fingerprint"],
                            candidate["input_fingerprint"],
                            candidate["payload_json"],
                            timestamp,
                            cluster_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM morphology_enrichment_clusters_v3 WHERE cluster_id = ?",
                        (cluster_id,),
                    ).fetchone()

                job_status = ""
                if existing["last_job_id"]:
                    job = connection.execute(
                        "SELECT status FROM preparation_jobs WHERE job_id = ?",
                        (str(existing["last_job_id"]),),
                    ).fetchone()
                    job_status = str(job["status"]) if job else ""
                if job_status in {"queued", "running"}:
                    continue
                fingerprint_changed = (
                    str(existing["last_completed_fingerprint"])
                    != str(candidate["input_fingerprint"])
                )
                next_eligible_text = str(existing["next_eligible_at"] or "")
                respect_cooldown = (
                    not fingerprint_changed
                    or str(existing["status"]) == "cooldown"
                )
                if respect_cooldown and next_eligible_text:
                    try:
                        next_eligible = datetime.fromisoformat(next_eligible_text)
                        if next_eligible.tzinfo is None:
                            next_eligible = next_eligible.replace(tzinfo=UTC)
                        if now < next_eligible:
                            continue
                    except ValueError:
                        pass

                epoch_number = int(existing["attempt_count"]) + 1
                lease_token = str(uuid.uuid4())
                lease_expires_at = (now + timedelta(minutes=15)).isoformat()
                job_fingerprint = hashlib.sha256(
                    f"{candidate['input_fingerprint']}:{epoch_number}".encode("utf-8")
                ).hexdigest()
                subject_key = f"morphology-v3:{cluster_id}"
                job_key = ":".join(
                    (
                        "enrich-morphology-v3",
                        subject_key,
                        "",
                        prompt_version,
                        job_fingerprint,
                    )
                )
                job_id = _identifier("job", job_key)
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id, job_key, job_type, subject_entity_id,
                           subject_key, language, priority, max_attempts, model,
                           prompt_version, source_fingerprint, created_at, updated_at
                       ) VALUES (?, ?, 'enrich-morphology-v3', ?, ?, '', ?, 2,
                                 ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        job_key,
                        subject_entity_id,
                        subject_key,
                        40 + int(candidate["priority_class"]),
                        model.strip(),
                        prompt_version.strip(),
                        job_fingerprint,
                        timestamp,
                        timestamp,
                    ),
                )
                epoch_id = _identifier(
                    "morphology-enrichment-epoch",
                    f"{cluster_id}:{epoch_number}:{candidate['input_fingerprint']}",
                )
                connection.execute(
                    """INSERT INTO morphology_enrichment_epochs_v3(
                           epoch_id, cluster_id, epoch_number, input_fingerprint,
                           lease_token, job_id, status, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                    (
                        epoch_id,
                        cluster_id,
                        epoch_number,
                        candidate["input_fingerprint"],
                        lease_token,
                        job_id,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """UPDATE morphology_enrichment_clusters_v3
                       SET status = 'queued', lease_token = ?,
                           lease_expires_at = ?, attempt_count = ?,
                           last_job_id = ?, updated_at = ? WHERE cluster_id = ?""",
                    (
                        lease_token,
                        lease_expires_at,
                        epoch_number,
                        job_id,
                        timestamp,
                        cluster_id,
                    ),
                )
                connection.commit()
                return {
                    "cluster_id": cluster_id,
                    "cluster_kind": candidate["cluster_kind"],
                    "priority_class": int(candidate["priority_class"]),
                    "canonical_subject_key": candidate["canonical_subject_key"],
                    "subject_entity_id": subject_entity_id or "",
                    "input_fingerprint": candidate["input_fingerprint"],
                    "epoch_number": epoch_number,
                    "job_id": job_id,
                    "status": "queued",
                    "parser_version": CURRENT_MORPHOLOGY_PARSER_VERSION,
                }
            page_size = max(4, min(128, max(8, min(int(scan_limit), 512)) // 3))
            if candidates:
                next_offset = scan_offset + page_size
                next_cycle = int(scheduler["cycle_number"])
            else:
                next_offset = 0
                next_cycle = int(scheduler["cycle_number"]) + 1
            connection.execute(
                """UPDATE morphology_enrichment_scheduler_v3
                   SET cursor_offset = ?, cycle_number = ?, updated_at = ?
                   WHERE scheduler_key = ?""",
                (next_offset, next_cycle, timestamp, scheduler_key),
            )
            connection.commit()
        return None

    def morphology_enrichment_context(self, job_id: str) -> dict[str, Any]:
        """Load one leased cluster with only its exact persisted book evidence."""

        with closing(self._connect()) as connection:
            self._ensure_morphology_enrichment_schema(connection)
            row = connection.execute(
                """SELECT cluster.*, epoch.epoch_id, epoch.epoch_number
                   FROM morphology_enrichment_clusters_v3 AS cluster
                   JOIN morphology_enrichment_epochs_v3 AS epoch
                     ON epoch.cluster_id = cluster.cluster_id
                    AND epoch.job_id = cluster.last_job_id
                   WHERE cluster.last_job_id = ?""",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no morphology enrichment lease for {job_id}")
            payload = _json_object(str(row["payload_json"]))
            evidence_ids = [
                str(item) for item in payload.get("evidence_ids", [])
                if str(item)
            ]
            evidence: list[dict[str, Any]] = []
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                evidence = [
                    {
                        **dict(item),
                        "payload": _json_object(str(item["payload"])),
                    }
                    for item in connection.execute(
                        f"""SELECT evidence_id, corpus_id, source_entry_id,
                                   source_hash, locator, excerpt, payload
                            FROM evidence_records
                            WHERE evidence_id IN ({placeholders})
                            ORDER BY corpus_id, source_entry_id""",
                        evidence_ids,
                    )
                ]
            timestamp = _now()
            connection.execute(
                """UPDATE morphology_enrichment_clusters_v3
                   SET status = 'running', updated_at = ? WHERE cluster_id = ?""",
                (timestamp, str(row["cluster_id"])),
            )
            connection.execute(
                """UPDATE morphology_enrichment_epochs_v3
                   SET status = 'running', updated_at = ? WHERE job_id = ?""",
                (timestamp, job_id),
            )
            connection.commit()
        return {**dict(row), "payload": payload, "evidence": evidence}

    def complete_morphology_v3_enrichment(
        self,
        job_id: str,
        *,
        output_fingerprint: str,
        outcome: dict[str, Any],
    ) -> None:
        timestamp = _now()
        next_eligible = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        outcome_json = json.dumps(outcome, ensure_ascii=False, sort_keys=True)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT cluster_id, input_fingerprint
                   FROM morphology_enrichment_clusters_v3
                   WHERE last_job_id = ?""",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            connection.execute(
                """UPDATE morphology_enrichment_clusters_v3
                   SET status = 'complete', lease_token = '',
                       lease_expires_at = '', failure_count = 0,
                       last_completed_fingerprint = input_fingerprint,
                       last_output_fingerprint = ?, next_eligible_at = ?,
                       last_outcome_json = ?, completed_at = ?, updated_at = ?
                   WHERE cluster_id = ?""",
                (
                    output_fingerprint,
                    next_eligible,
                    outcome_json,
                    timestamp,
                    timestamp,
                    str(row["cluster_id"]),
                ),
            )
            connection.execute(
                """UPDATE morphology_enrichment_epochs_v3
                   SET status = 'complete', output_fingerprint = ?,
                       outcome_json = ?, completed_at = ?, updated_at = ?
                   WHERE job_id = ?""",
                (output_fingerprint, outcome_json, timestamp, timestamp, job_id),
            )
            connection.commit()

    def morphology_enrichment_worker_slot_due(
        self, *, after_other_jobs: int = 6
    ) -> bool:
        """Reserve one durable morphology slot after bounded non-morph work."""

        after_other_jobs = max(1, min(int(after_other_jobs), 32))
        with closing(self._connect()) as connection:
            enrichment_table = connection.execute(
                """SELECT 1 FROM sqlite_master WHERE type = 'table'
                   AND name = 'morphology_enrichment_clusters_v3'"""
            ).fetchone()
            if enrichment_table is None:
                queued = connection.execute(
                    """SELECT 1 FROM preparation_jobs
                       WHERE job_type = 'enrich-morphology-v3'
                         AND status = 'queued' LIMIT 1"""
                ).fetchone()
            else:
                queued = connection.execute(
                    """SELECT 1 FROM preparation_jobs AS job
                       LEFT JOIN morphology_enrichment_clusters_v3 AS cluster
                         ON cluster.last_job_id = job.job_id
                       WHERE job.job_type = 'enrich-morphology-v3'
                         AND job.status = 'queued'
                         AND (cluster.cluster_id IS NULL
                              OR cluster.parser_version = ?)
                       LIMIT 1""",
                    (CURRENT_MORPHOLOGY_PARSER_VERSION,),
                ).fetchone()
            if queued is None:
                return False
            if enrichment_table is None:
                last = connection.execute(
                    """SELECT MAX(updated_at) FROM preparation_jobs
                       WHERE job_type = 'enrich-morphology-v3'
                         AND status = 'complete'"""
                ).fetchone()[0]
            else:
                last = connection.execute(
                    """SELECT MAX(job.updated_at) FROM preparation_jobs AS job
                       LEFT JOIN morphology_enrichment_clusters_v3 AS cluster
                         ON cluster.last_job_id = job.job_id
                       WHERE job.job_type = 'enrich-morphology-v3'
                         AND job.status = 'complete'
                         AND (cluster.cluster_id IS NULL
                              OR cluster.parser_version = ?)""",
                    (CURRENT_MORPHOLOGY_PARSER_VERSION,),
                ).fetchone()[0]
            if not last:
                return True
            completed = connection.execute(
                """SELECT COUNT(*) FROM preparation_jobs
                   WHERE job_type <> 'enrich-morphology-v3'
                     AND status = 'complete' AND updated_at > ?""",
                (str(last),),
            ).fetchone()[0]
        return int(completed) >= after_other_jobs

    def morphology_v3_status(self) -> dict[str, Any]:
        """Return bounded aggregate coverage for cached health composition."""

        with closing(self._connect()) as connection:
            available = connection.execute(
                """SELECT 1 FROM sqlite_master WHERE type = 'table'
                   AND name = 'morphology_source_observations_v3'"""
            ).fetchone()
            if available is None:
                return {
                    "ready": False,
                    "parser_version": CURRENT_MORPHOLOGY_PARSER_VERSION,
                    "observed": 0,
                    "materialized": 0,
                    "ambiguous": 0,
                    "qwen_ready": 0,
                    "qwen_ready_records": 0,
                    "qwen_ready_observations": 0,
                    "enrichment": {},
                }
            source_rows = connection.execute(
                """SELECT source_kind, materialization_state, COUNT(*) AS count,
                          SUM(CASE WHEN qwen_ready_count > 0 THEN 1 ELSE 0 END)
                              AS qwen_ready_records,
                          SUM(qwen_ready_count) AS qwen_ready_observations
                   FROM morphology_source_observations_v3
                   WHERE parser_version = ?
                   GROUP BY source_kind, materialization_state""",
                (CURRENT_MORPHOLOGY_PARSER_VERSION,),
            ).fetchall()
            component_rows = connection.execute(
                """SELECT component.state, COUNT(*)
                   FROM morphology_component_observations_v3 AS component
                   JOIN morphology_source_observations_v3 AS source
                     ON source.source_observation_id =
                        component.source_observation_id
                   WHERE source.parser_version = ?
                   GROUP BY component.state""",
                (CURRENT_MORPHOLOGY_PARSER_VERSION,),
            ).fetchall()
            link_rows = connection.execute(
                """SELECT link.state, COUNT(*)
                   FROM morphology_formula_links_v3 AS link
                   JOIN morphology_source_observations_v3 AS source
                     ON source.source_observation_id = link.source_observation_id
                   WHERE source.parser_version = ?
                   GROUP BY link.state""",
                (CURRENT_MORPHOLOGY_PARSER_VERSION,),
            ).fetchall()
            declaration_rows = connection.execute(
                """SELECT declaration.declaration_type, COUNT(*)
                   FROM morphology_declarations_v3 AS declaration
                   JOIN morphology_source_observations_v3 AS source
                     ON source.source_observation_id =
                        declaration.source_observation_id
                   WHERE source.parser_version = ?
                   GROUP BY declaration.declaration_type""",
                (CURRENT_MORPHOLOGY_PARSER_VERSION,),
            ).fetchall()
            enrichment_table = connection.execute(
                """SELECT 1 FROM sqlite_master WHERE type = 'table'
                   AND name = 'morphology_enrichment_clusters_v3'"""
            ).fetchone()
            enrichment = (
                {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        """SELECT status, COUNT(*)
                           FROM morphology_enrichment_clusters_v3
                           WHERE parser_version = ?
                           GROUP BY status"""
                        , (CURRENT_MORPHOLOGY_PARSER_VERSION,)
                    )
                }
                if enrichment_table is not None
                else {}
            )
            job_states = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    """SELECT job.status, COUNT(*)
                       FROM preparation_jobs AS job
                       JOIN morphology_enrichment_clusters_v3 AS cluster
                         ON cluster.last_job_id = job.job_id
                       WHERE cluster.parser_version = ?
                         AND job.job_type = 'enrich-morphology-v3'
                       GROUP BY job.status""",
                    (CURRENT_MORPHOLOGY_PARSER_VERSION,),
                )
            }
            revision_row = connection.execute(
                """SELECT value FROM schema_meta
                   WHERE key = 'relation_graph_revision'"""
            ).fetchone()
            graph_revision = int(revision_row[0]) if revision_row else 0
            scheduler_row = connection.execute(
                """SELECT cursor_offset, cycle_number, updated_at
                   FROM morphology_enrichment_scheduler_v3
                   WHERE scheduler_key = 'retained-for-local-qwen'"""
            ).fetchone() if enrichment_table is not None else None
        by_kind: dict[str, dict[str, int]] = {}
        record_states: dict[str, int] = {}
        qwen_ready_records = 0
        qwen_ready_observations = 0
        for row in source_rows:
            source_kind = str(row["source_kind"])
            state = str(row["materialization_state"])
            count = int(row["count"])
            by_kind.setdefault(source_kind, {})[state] = count
            record_states[state] = record_states.get(state, 0) + count
            qwen_ready_records += int(row["qwen_ready_records"] or 0)
            qwen_ready_observations += int(row["qwen_ready_observations"] or 0)
        observed = sum(record_states.values())
        return {
            "ready": observed > 0,
            "parser_version": CURRENT_MORPHOLOGY_PARSER_VERSION,
            "observed": observed,
            "materialized": int(record_states.get("materialized", 0)),
            "ambiguous": int(record_states.get("ambiguous", 0)),
            "evidence_only": int(record_states.get("observed", 0)),
            "qwen_ready": qwen_ready_records,
            "qwen_ready_records": qwen_ready_records,
            "qwen_ready_observations": qwen_ready_observations,
            "record_states": record_states,
            "by_source_kind": by_kind,
            "components": {str(row[0]): int(row[1]) for row in component_rows},
            "formula_links": {str(row[0]): int(row[1]) for row in link_rows},
            "declarations": {
                str(row[0]): int(row[1]) for row in declaration_rows
            },
            "enrichment": {"clusters": enrichment, "jobs": job_states},
            "enrichment_scheduler": dict(scheduler_row) if scheduler_row else {},
            "graph_revision": graph_revision,
            "coverage_complete": observed > 0,
        }

    def artifacts_for_job(
        self, job_id: str, *, stage: str = ""
    ) -> list[dict[str, Any]]:
        clause = " AND stage = ?" if stage else ""
        parameters: tuple[Any, ...] = (job_id, stage) if stage else (job_id,)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT artifact_id, job_id, stage, language, payload,
                          reusable, validation_state, quality_score, created_at
                   FROM job_artifacts WHERE job_id = ?""" + clause +
                " ORDER BY created_at, artifact_id",
                parameters,
            ).fetchall()
        return [
            {**dict(row), "payload": _json_object(str(row["payload"]))}
            for row in rows
        ]

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

    def claim_word_card_progress(self) -> dict[str, Any] | None:
        """Reserve bounded work for the oldest Word Card's unfinished inputs."""
        progress_types = (
            "prepare-translation", "prepare-pronunciation",
            "prepare-grammar-properties", "compose-word-card",
        )
        with closing(self._connect()) as connection:
            recent = connection.execute(
                """SELECT job_type FROM preparation_jobs
                   WHERE status IN ('complete','failed')
                   ORDER BY updated_at DESC,job_id DESC LIMIT 6"""
            ).fetchall()
            if any(row[0] in progress_types for row in recent):
                return None
            candidate = connection.execute(
                """WITH RECURSIVE pending AS (
                       SELECT job_id,subject_key,created_at FROM preparation_jobs
                       WHERE job_type='compose-word-card' AND status='queued'
                         AND attempts<max_attempts
                       ORDER BY created_at,job_id LIMIT 32
                   ), needed(card_id,job_id) AS (
                       SELECT job_id,job_id FROM pending
                       UNION
                       SELECT p.job_id,j.job_id FROM pending p
                       JOIN preparation_jobs j ON j.subject_key=p.subject_key
                       WHERE j.job_type IN ('prepare-translation','prepare-pronunciation')
                       UNION
                       SELECT n.card_id,d.depends_on_job_id FROM needed n
                       JOIN job_dependencies d ON d.job_id=n.job_id
                   )
                   SELECT j.job_type,j.subject_key
                   FROM needed n JOIN pending p ON p.job_id=n.card_id
                   JOIN preparation_jobs j ON j.job_id=n.job_id
                   WHERE j.status='queued' AND j.attempts<j.max_attempts
                     AND NOT EXISTS(
                       SELECT 1 FROM job_dependencies d
                       JOIN preparation_jobs prerequisite
                         ON prerequisite.job_id=d.depends_on_job_id
                       WHERE d.job_id=j.job_id AND prerequisite.status<>'complete'
                         AND (j.job_type NOT IN ('compose-word-card','compose-origin-card')
                              OR prerequisite.status<>'failed'))
                   ORDER BY p.created_at,p.job_id,j.priority,j.created_at,j.job_id
                   LIMIT 1"""
            ).fetchone()
        if candidate is None:
            return None
        return self.claim_next_job(
            (candidate["job_type"],), preferred_subject_keys=(candidate["subject_key"],)
        )

    def claim_next_job(
        self,
        job_types: Iterable[str] | None = None,
        *,
        preferred_subject_keys: Iterable[str] | None = None,
    ) -> dict[str, Any] | None:
        allowed = tuple(dict.fromkeys(str(item).strip() for item in (job_types or ())))
        preferred_subjects = tuple(
            dict.fromkeys(
                str(item).strip() for item in (preferred_subject_keys or ())
                if str(item).strip()
            )
        )
        type_clause = ""
        parameters: tuple[Any, ...] = ()
        if allowed:
            type_clause = " AND job.job_type IN (" + ",".join("?" for _ in allowed) + ")"
            parameters = allowed
        affinity_order = ""
        if preferred_subjects:
            affinity_order = (
                "CASE WHEN job.subject_key IN ("
                + ",".join("?" for _ in preferred_subjects)
                + ") THEN 0 ELSE 1 END, "
            )
            parameters += preferred_subjects
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._retire_stale_morphology_enrichment_jobs_connection(connection)
            self._fail_blocked_jobs(connection)
            morphology_cte = ""
            morphology_join = ""
            morphology_order = ""
            if allowed == ("enrich-morphology-v3",) and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='morphology_enrichment_epochs_v3'"
            ).fetchone():
                # Reserve turns by the least recently attempted kind, not by
                # a permanent declaration/component/link priority hierarchy.
                # Failed attempts count too, so one bad kind cannot monopolize
                # the local model. The existing dependency checks still apply.
                morphology_cte = """WITH morphology_turns AS (
                    SELECT cluster.cluster_kind,
                           MAX(COALESCE(NULLIF(attempt.locked_at, ''),
                                        attempt.updated_at)) AS served_at
                    FROM morphology_enrichment_epochs_v3 AS epoch
                    JOIN morphology_enrichment_clusters_v3 AS cluster
                      ON cluster.cluster_id = epoch.cluster_id
                    JOIN preparation_jobs AS attempt ON attempt.job_id = epoch.job_id
                    WHERE attempt.attempts > 0
                    GROUP BY cluster.cluster_kind
                ) """
                morphology_join = """
                    LEFT JOIN morphology_enrichment_epochs_v3 AS fair_epoch
                      ON fair_epoch.job_id = job.job_id
                    LEFT JOIN morphology_enrichment_clusters_v3 AS fair_cluster
                      ON fair_cluster.cluster_id = fair_epoch.cluster_id
                    LEFT JOIN morphology_turns AS fair_turn
                      ON fair_turn.cluster_kind = fair_cluster.cluster_kind
                """
                morphology_order = "COALESCE(fair_turn.served_at, ''), "
            row = connection.execute(
                morphology_cte + """SELECT job.* FROM preparation_jobs AS job
                    """ + morphology_join + """
                    WHERE job.status = 'queued' AND job.attempts < job.max_attempts
                     """ + type_clause + """
                      AND NOT EXISTS (
                          SELECT 1 FROM job_dependencies AS dependency
                          JOIN preparation_jobs AS prerequisite
                            ON prerequisite.job_id = dependency.depends_on_job_id
                          WHERE dependency.job_id = job.job_id
                            AND prerequisite.status <> 'complete'
                            AND (
                                job.job_type NOT IN (
                                    'compose-word-card', 'compose-origin-card'
                                )
                                OR prerequisite.status <> 'failed'
                            )
                      )
                    ORDER BY
                        """ + morphology_order + affinity_order + """
                       CASE WHEN job.subject_key LIKE 'term:%' THEN 0 ELSE 1 END,
                       priority, created_at, job.subject_key, job.job_id
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

    def active_lexical_discovery_subject_keys(self) -> set[str]:
        """Return every durably discovered term whose preparation is unfinished."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT 'term:' || term.entity_id AS subject_key
                   FROM lexical_discoveries AS discovery
                   JOIN terms AS term
                     ON term.language = discovery.language
                    AND term.normalized = discovery.normalized
                    AND term.kind = 'word'
                   JOIN preparation_jobs AS job
                     ON job.subject_entity_id = term.entity_id
                    AND job.subject_key = 'term:' || term.entity_id
                   WHERE job.status IN ('queued', 'running')"""
            ).fetchall()
        return {str(row["subject_key"]) for row in rows}

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
            if source_kind not in {
                "qa-investigation",
                "word-origins",
                "derivative",
                "card-derivative",
                "historical-form",
                "shared-morpheme",
                "graph-frontier",
            }:
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

    def has_active_lexical_discovery_claim(self) -> bool:
        """Report whether a durable discovery claim still awaits planning."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT 1 FROM lexical_discovery_rounds AS round
                   JOIN lexical_discoveries AS discovery
                     ON discovery.round_id = round.round_id
                   WHERE round.status = 'claimed' AND discovery.planned_at = ''
                   LIMIT 1"""
            ).fetchone()
        return row is not None

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

    def lexical_frontier_candidates(
        self, *, limit: int = 64
    ) -> list[dict[str, Any]]:
        """Return bounded graph-born subjects that have not had a full plan.

        Derivatives are persisted as relation assertions, while historical
        forms and shared morphology also have normalized relational storage.
        Reading all three layers here prevents recursive growth from depending
        on whichever related-word list happened to survive card composition.
        A terminal draft can return after an exponential cooldown (or as soon
        as a newer accepted connection arrives); accepted planned words are
        owned by ``lexical_revisit_candidate`` instead.
        """

        limit = max(1, min(int(limit), 256))
        # Coverage ranking happens after graph connections are merged. Keep a
        # wider bounded window so dense low-evidence branches cannot hide
        # incomplete accepted terms before that ranking is applied.
        scan_limit = max(512, min(limit * 32, 8192))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """WITH frontier AS (
                       SELECT term.language, term.text, term.normalized,
                              'derivative' AS source_kind,
                              assertion.subject_entity_id AS source_entity_id,
                              assertion.relation, assertion.basis,
                              assertion.confidence,
                              assertion.updated_at AS frontier_updated_at,
                              CASE assertion.basis
                                  WHEN 'book' THEN 0 WHEN 'reviewed' THEN 0
                                  WHEN 'derived' THEN 1 ELSE 2 END AS evidence_rank
                       FROM relation_assertions AS assertion
                       JOIN terms AS term
                         ON term.entity_id = assertion.source_entity_id
                        AND term.kind = 'word'
                       JOIN entities AS entity
                         ON entity.entity_id = term.entity_id
                        AND entity.status = 'accepted'
                       WHERE assertion.status = 'accepted'
                         AND assertion.relation IN (
                             'shares-component', 'derivative-of', 'derived-from',
                             'developed-from', 'developed-into', 'cognate-with',
                             'related-to', 'variant-of'
                         )
                         AND term.entity_id <> assertion.subject_entity_id
                         AND EXISTS (
                             SELECT 1 FROM preparation_jobs AS source_job
                             WHERE source_job.subject_entity_id =
                                   assertion.subject_entity_id
                         )
                       UNION ALL
                       SELECT term.language, term.text, term.normalized,
                              'derivative' AS source_kind,
                              assertion.subject_entity_id AS source_entity_id,
                              assertion.relation, assertion.basis,
                              assertion.confidence,
                              assertion.updated_at AS frontier_updated_at,
                              CASE assertion.basis
                                  WHEN 'book' THEN 0 WHEN 'reviewed' THEN 0
                                  WHEN 'derived' THEN 1 ELSE 2 END AS evidence_rank
                       FROM relation_assertions AS assertion
                       JOIN terms AS term
                         ON term.entity_id = assertion.target_entity_id
                        AND term.kind = 'word'
                       JOIN entities AS entity
                         ON entity.entity_id = term.entity_id
                        AND entity.status = 'accepted'
                       WHERE assertion.status = 'accepted'
                         AND assertion.relation IN (
                             'derivative-of', 'derived-from', 'developed-from',
                             'developed-into', 'cognate-with', 'related-to',
                             'variant-of'
                         )
                         AND term.entity_id <> assertion.subject_entity_id
                         AND EXISTS (
                             SELECT 1 FROM preparation_jobs AS source_job
                             WHERE source_job.subject_entity_id =
                                   assertion.subject_entity_id
                         )
                       UNION ALL
                       SELECT candidate.language, candidate.text,
                              candidate.normalized,
                              'shared-morpheme' AS source_kind,
                              anchor.entity_id AS source_entity_id,
                              'shares-component' AS relation,
                              'derived' AS basis, 0.55 AS confidence,
                              candidate_entity.updated_at AS frontier_updated_at,
                              1 AS evidence_rank
                       FROM terms AS candidate
                       JOIN entities AS candidate_entity
                         ON candidate_entity.entity_id = candidate.entity_id
                        AND candidate_entity.status = 'accepted'
                       JOIN term_morphemes AS candidate_link
                         ON candidate_link.term_id = candidate.entity_id
                       JOIN term_morphemes AS anchor_link
                         ON anchor_link.morpheme_id = candidate_link.morpheme_id
                        AND anchor_link.term_id <> candidate.entity_id
                       JOIN terms AS anchor ON anchor.entity_id = anchor_link.term_id
                       WHERE candidate.kind = 'word'
                         AND EXISTS (
                             SELECT 1 FROM preparation_jobs AS source_job
                             WHERE source_job.subject_entity_id = anchor.entity_id
                         )
                       UNION ALL
                       SELECT historical.language, historical.form,
                              historical.normalized,
                              'historical-form' AS source_kind,
                              assertion.subject_entity_id AS source_entity_id,
                              assertion.relation, assertion.basis,
                              assertion.confidence,
                              assertion.updated_at AS frontier_updated_at,
                              CASE assertion.basis
                                  WHEN 'book' THEN 0 WHEN 'reviewed' THEN 0
                                  WHEN 'derived' THEN 1 ELSE 2 END AS evidence_rank
                       FROM relation_assertions AS assertion
                       JOIN historical_forms AS historical
                         ON historical.entity_id IN (
                             assertion.source_entity_id,
                             assertion.target_entity_id
                         )
                       JOIN entities AS entity
                         ON entity.entity_id = historical.entity_id
                        AND entity.status = 'accepted'
                       WHERE assertion.status = 'accepted'
                         AND assertion.relation IN (
                             'developed-into', 'developed-toward',
                             'derived-from', 'cognate-with'
                         )
                         AND EXISTS (
                             SELECT 1 FROM preparation_jobs AS source_job
                             WHERE source_job.subject_entity_id =
                                   assertion.subject_entity_id
                         )
                       UNION ALL
                       SELECT historical.language, historical.form,
                              historical.normalized,
                              'historical-form' AS source_kind,
                              CASE WHEN edge.source_entity_id = historical.entity_id
                                   THEN edge.target_entity_id
                                   ELSE edge.source_entity_id END AS source_entity_id,
                              edge.relation, edge.basis, edge.confidence,
                              edge.updated_at AS frontier_updated_at,
                              CASE edge.basis
                                  WHEN 'book' THEN 0 WHEN 'reviewed' THEN 0
                                  WHEN 'derived' THEN 1 ELSE 2 END AS evidence_rank
                       FROM historical_forms AS historical
                       JOIN entities AS entity
                         ON entity.entity_id = historical.entity_id
                        AND entity.status = 'accepted'
                       JOIN entity_edges AS edge
                         ON edge.status = 'accepted'
                        AND (edge.source_entity_id = historical.entity_id
                          OR edge.target_entity_id = historical.entity_id)
                       WHERE edge.relation IN (
                           'has-history', 'developed-into',
                           'developed-toward', 'derived-from'
                       )
                         AND EXISTS (
                             SELECT 1 FROM preparation_jobs AS source_job
                             WHERE source_job.subject_entity_id = CASE
                                 WHEN edge.source_entity_id = historical.entity_id
                                 THEN edge.target_entity_id
                                 ELSE edge.source_entity_id END
                         )
                   )
                   SELECT DISTINCT language, text, normalized, source_kind,
                          source_entity_id, relation, basis, confidence,
                          frontier_updated_at, evidence_rank
                   FROM frontier
                   WHERE normalized <> ''
                   ORDER BY evidence_rank, confidence DESC,
                            frontier_updated_at DESC, language, normalized
                   LIMIT ?""",
                (scan_limit,),
            ).fetchall()
            state_rows = connection.execute(
                """WITH meaning_coverage AS (
                       SELECT meaning.subject_entity_id AS term_id,
                              COUNT(DISTINCT meaning.entity_id)
                                  AS source_meaning_count
                       FROM meanings AS meaning
                       JOIN terms AS term
                         ON term.entity_id = meaning.subject_entity_id
                        AND term.kind = 'word'
                        AND meaning.language = term.language
                       JOIN entities AS meaning_entity
                         ON meaning_entity.entity_id = meaning.entity_id
                        AND meaning_entity.status = 'accepted'
                       GROUP BY meaning.subject_entity_id
                   ), translation_coverage AS (
                       SELECT translation.source_term_id AS term_id,
                              SUM(CASE WHEN translation.target_language = 'ja'
                                       THEN 1 ELSE 0 END) AS ja_translation_count,
                              SUM(CASE WHEN translation.target_language = 'zh'
                                       THEN 1 ELSE 0 END) AS zh_translation_count,
                              SUM(CASE WHEN translation.target_language = 'fr'
                                       THEN 1 ELSE 0 END) AS fr_translation_count,
                              SUM(CASE WHEN translation.target_language = 'ar'
                                       THEN 1 ELSE 0 END) AS ar_translation_count
                       FROM translations AS translation
                       JOIN entities AS translation_entity
                         ON translation_entity.entity_id = translation.entity_id
                        AND translation_entity.status = 'accepted'
                       WHERE translation.source_term_id IS NOT NULL
                       GROUP BY translation.source_term_id
                   ), component_coverage AS (
                       SELECT link.term_id,
                              COUNT(DISTINCT link.morpheme_id) AS component_count
                       FROM term_morphemes AS link
                       JOIN entities AS morpheme_entity
                         ON morpheme_entity.entity_id = link.morpheme_id
                        AND morpheme_entity.status = 'accepted'
                       GROUP BY link.term_id
                   ), history_coverage AS (
                       SELECT term.entity_id AS term_id,
                              COUNT(DISTINCT historical.entity_id) AS history_count
                       FROM terms AS term
                       JOIN entity_edges AS edge
                         ON edge.status = 'accepted'
                        AND (edge.source_entity_id = term.entity_id
                          OR edge.target_entity_id = term.entity_id)
                       JOIN historical_forms AS historical
                         ON historical.entity_id = CASE
                             WHEN edge.source_entity_id = term.entity_id
                             THEN edge.target_entity_id
                             ELSE edge.source_entity_id
                         END
                       JOIN entities AS historical_entity
                         ON historical_entity.entity_id = historical.entity_id
                        AND historical_entity.status = 'accepted'
                       WHERE term.kind = 'word'
                         AND edge.relation IN (
                             'has-history', 'developed-into', 'developed-toward',
                             'derived-from'
                         )
                       GROUP BY term.entity_id
                   )
                   SELECT term.language, term.normalized, entity.status,
                          COUNT(DISTINCT job.prompt_version) AS plan_count,
                          COALESCE(MAX(job.updated_at), '') AS last_prepared_at,
                          SUM(CASE WHEN job.status IN ('queued', 'running')
                                   THEN 1 ELSE 0 END) AS active_jobs,
                          COALESCE(meaning_coverage.source_meaning_count, 0)
                              AS source_meaning_count,
                          COALESCE(translation_coverage.ja_translation_count, 0)
                              AS ja_translation_count,
                          COALESCE(translation_coverage.zh_translation_count, 0)
                              AS zh_translation_count,
                          COALESCE(translation_coverage.fr_translation_count, 0)
                              AS fr_translation_count,
                          COALESCE(translation_coverage.ar_translation_count, 0)
                              AS ar_translation_count,
                          COALESCE(component_coverage.component_count, 0)
                              AS component_count,
                          COALESCE(history_coverage.history_count, 0)
                              AS history_count
                   FROM terms AS term
                   JOIN entities AS entity ON entity.entity_id = term.entity_id
                   LEFT JOIN preparation_jobs AS job
                     ON job.subject_entity_id = term.entity_id
                    AND job.subject_key = 'term:' || term.entity_id
                   LEFT JOIN meaning_coverage
                     ON meaning_coverage.term_id = term.entity_id
                   LEFT JOIN translation_coverage
                     ON translation_coverage.term_id = term.entity_id
                   LEFT JOIN component_coverage
                     ON component_coverage.term_id = term.entity_id
                   LEFT JOIN history_coverage
                     ON history_coverage.term_id = term.entity_id
                   WHERE term.kind = 'word'
                   GROUP BY term.entity_id"""
            ).fetchall()

        states = {
            (str(row["language"]), str(row["normalized"])): dict(row)
            for row in state_rows
        }
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            key = (str(item["language"]), str(item["normalized"]))
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    **item,
                    "connection_count": 1,
                    "source_kinds": {str(item["source_kind"])},
                }
                continue
            current["connection_count"] = int(current["connection_count"]) + 1
            current["source_kinds"].add(str(item["source_kind"]))
            if str(item["frontier_updated_at"]) > str(
                current["frontier_updated_at"]
            ):
                current["frontier_updated_at"] = item["frontier_updated_at"]
            if int(item["evidence_rank"]) < int(current["evidence_rank"]):
                current.update(
                    {
                        field: item[field]
                        for field in (
                            "source_kind", "source_entity_id", "relation",
                            "basis", "confidence", "evidence_rank",
                        )
                    }
                )

        now = datetime.now(UTC)
        eligible: list[dict[str, Any]] = []
        for key, item in merged.items():
            state = states.get(key)
            plan_count = int(state["plan_count"]) if state else 0
            if state and int(state["active_jobs"] or 0):
                continue
            # Once accepted, this same term is revisited by the general novelty
            # scheduler rather than being repeatedly rediscovered as frontier.
            if state and plan_count and str(state["status"]) == "accepted":
                continue
            last_text = str(state["last_prepared_at"]) if state else ""
            frontier_text = str(item["frontier_updated_at"])
            novel = not plan_count or bool(frontier_text and frontier_text > last_text)
            cooldown_hours = min(24 * 30, 6 * (2 ** min(plan_count, 7)))
            try:
                last = datetime.fromisoformat(last_text)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                age_hours = (now - last).total_seconds() / 3600
            except (TypeError, ValueError):
                age_hours = float("inf")
            if plan_count and not novel and age_hours < cooldown_hours:
                continue
            coverage = {
                "source_meaning_count": int(state["source_meaning_count"])
                if state else 0,
                "ja_translation_count": int(state["ja_translation_count"])
                if state else 0,
                "zh_translation_count": int(state["zh_translation_count"])
                if state else 0,
                "fr_translation_count": int(state["fr_translation_count"])
                if state else 0,
                "ar_translation_count": int(state["ar_translation_count"])
                if state else 0,
                "component_count": int(state["component_count"]) if state else 0,
                "history_count": int(state["history_count"]) if state else 0,
            }
            missing_displays = sum(
                int(coverage[f"{language}_translation_count"] == 0)
                for language in ("ja", "zh", "fr", "ar")
            )
            missing_structure = int(coverage["component_count"] == 0) + int(
                coverage["history_count"] == 0
            )
            item.update(
                {
                    "source_kinds": sorted(item["source_kinds"]),
                    "retry_count": plan_count,
                    "novel_connections": novel,
                    "cooldown_hours": cooldown_hours,
                    "age_hours": age_hours,
                    "note": (
                        f"accepted {item['source_kind']} via {item['relation']}"
                    ),
                    "coverage": coverage,
                    # Larger scores always prefer missing source meaning, then
                    # display languages, then morphology/history structure.
                    "coverage_priority": (
                        int(coverage["source_meaning_count"] == 0) * 1_000_000
                        + missing_displays * 1_000
                        + missing_structure
                    ),
                }
            )
            eligible.append(item)
        eligible.sort(
            key=lambda item: (
                -int(item["coverage_priority"]),
                not bool(item["novel_connections"]),
                int(item["retry_count"]) > 0,
                int(item["evidence_rank"]),
                -int(item["connection_count"]),
                -float(item["confidence"]),
                str(item["language"]),
                str(item["normalized"]),
            )
        )
        return eligible[:limit]

    def lexical_sparse_candidate(
        self,
        visible_views: Iterable[dict[str, Any]],
        *,
        prompt_prefix: str = "autonomous-lexical",
        limit: int = 64,
    ) -> dict[str, Any] | None:
        """Choose one visible subject whose semantic projection is still sparse.

        The prompt version durably records both graph revision and projection
        fingerprint. A changed local projection can therefore be enriched at
        once, while an unchanged result receives an exponential cooldown. The
        caller supplies visible modes and branch focuses from the card adapter;
        the graph store remains independent of that output adapter.
        """

        limit = max(1, min(int(limit), 128))
        prepared: dict[str, dict[str, Any]] = {}
        for raw in visible_views:
            subject_id = str(raw.get("subject_entity_id", "")).strip()
            mode = str(raw.get("mode", "")).strip()
            if not subject_id or mode not in {"word", "root", "affix"}:
                continue
            item = prepared.setdefault(
                subject_id,
                {
                    "subject_entity_id": subject_id,
                    "modes": set(),
                    "branch_focus_ids": set(),
                },
            )
            item["modes"].add(mode)
            branch_focus_id = str(raw.get("branch_focus_id", "")).strip()
            if branch_focus_id:
                item["branch_focus_ids"].add(branch_focus_id)
        subject_ids = list(prepared)[:limit]
        if not subject_ids:
            return None

        placeholders = ",".join("?" for _ in subject_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT term.entity_id, term.language, term.text,
                          term.normalized, entity.status,
                          job.prompt_version, job.status AS job_status,
                          job.updated_at AS job_updated_at
                   FROM terms AS term
                   JOIN entities AS entity ON entity.entity_id = term.entity_id
                   LEFT JOIN preparation_jobs AS job
                     ON job.subject_entity_id = term.entity_id
                    AND job.subject_key = 'term:' || term.entity_id
                   WHERE term.kind = 'word' AND term.entity_id IN ("""
                + placeholders
                + ") ORDER BY term.entity_id, job.updated_at",
                subject_ids,
            ).fetchall()

        states: dict[str, dict[str, Any]] = {}
        sparse_marker = f"{prompt_prefix}-sparse-g"
        for row in rows:
            subject_id = str(row["entity_id"])
            state = states.setdefault(
                subject_id,
                {
                    "language": str(row["language"]),
                    "term": str(row["text"]),
                    "normalized": str(row["normalized"]),
                    "status": str(row["status"]),
                    "active_jobs": 0,
                    "sparse_versions": set(),
                    "last_sparse_at": "",
                    "seen_projection_hashes": set(),
                    "last_graph_revision": 0,
                },
            )
            job_status = str(row["job_status"] or "")
            if job_status in {"queued", "running"}:
                state["active_jobs"] += 1
            version = str(row["prompt_version"] or "")
            if not version.startswith(sparse_marker):
                continue
            state["sparse_versions"].add(version)
            updated_at = str(row["job_updated_at"] or "")
            if updated_at > state["last_sparse_at"]:
                state["last_sparse_at"] = updated_at
            suffix = version[len(sparse_marker) :]
            revision_text, separator, remainder = suffix.partition("-p")
            fingerprint, _, _attempt = remainder.partition("-a")
            if separator and revision_text.isdigit():
                state["last_graph_revision"] = max(
                    int(state["last_graph_revision"]), int(revision_text)
                )
            if fingerprint:
                state["seen_projection_hashes"].add(fingerprint)

        now = datetime.now(UTC)
        candidates: list[dict[str, Any]] = []
        for subject_id in subject_ids:
            state = states.get(subject_id)
            if (
                state is None
                or state["status"] != "accepted"
                or int(state["active_jobs"])
            ):
                continue
            view = prepared[subject_id]
            sparse_views: list[dict[str, Any]] = []
            projection_keys: list[dict[str, Any]] = []
            for mode in sorted(view["modes"]):
                try:
                    projection = self.lexical_subgraph(subject_id, mode=mode)
                except (KeyError, ValueError):
                    continue
                node_count = len(projection.get("nodes", ()))
                edge_count = len(projection.get("edges", ()))
                projection_keys.append(
                    {
                        "mode": mode,
                        "hash": str(projection.get("projection_hash", "")),
                        "revision": int(projection.get("graph_revision", 0)),
                    }
                )
                if node_count <= 2 or edge_count <= 1:
                    sparse_views.append(
                        {
                            "mode": mode,
                            "nodes": node_count,
                            "edges": edge_count,
                            "branch_focus_id": str(
                                projection.get("branch_focus_id", "")
                            ),
                        }
                    )
            if not sparse_views:
                continue
            projection_fingerprint = hashlib.sha256(
                json.dumps(
                    projection_keys,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:12]
            graph_revision = max(
                (int(item["revision"]) for item in projection_keys), default=0
            )
            repair_count = len(state["sparse_versions"])
            novel = projection_fingerprint not in state["seen_projection_hashes"]
            graph_revision_advanced = (
                graph_revision > int(state["last_graph_revision"])
            )
            cooldown_hours = min(24 * 30, 12 * (2 ** min(repair_count, 6)))
            try:
                last = datetime.fromisoformat(str(state["last_sparse_at"]))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                age_hours = (now - last).total_seconds() / 3600
            except (TypeError, ValueError):
                age_hours = float("inf")
            if repair_count and not novel and age_hours < cooldown_hours:
                continue
            sparse_views.sort(
                key=lambda item: (
                    int(item["edges"]), int(item["nodes"]), str(item["mode"])
                )
            )
            candidates.append(
                {
                    "term_id": subject_id,
                    "term": state["term"],
                    "language": state["language"],
                    "source_kind": "sparse-projection",
                    "sparse_views": sparse_views,
                    "branch_focus_ids": sorted(view["branch_focus_ids"]),
                    "repair_count": repair_count,
                    "novel_projection": novel,
                    "graph_revision": graph_revision,
                    "graph_revision_advanced": graph_revision_advanced,
                    "projection_fingerprint": projection_fingerprint,
                    "cooldown_hours": cooldown_hours,
                    "age_hours": age_hours,
                    "prompt_version": (
                        f"{prompt_prefix}-sparse-g{graph_revision}-"
                        f"p{projection_fingerprint}-a{repair_count + 1}"
                    ),
                }
            )
        candidates.sort(
            key=lambda item: (
                not bool(item["novel_projection"]),
                min(int(view["edges"]) for view in item["sparse_views"]),
                min(int(view["nodes"]) for view in item["sparse_views"]),
                int(item["repair_count"]),
                str(item["language"]),
                str(item["term"]).casefold(),
            )
        )
        return candidates[0] if candidates else None

    def claim_lexical_growth_decision(self) -> int:
        """Atomically claim one durable scheduler decision ordinal.

        A decision is deliberately independent of how many terms its selected
        source batch later plans. This keeps graph/correction/source/source
        cadence stable across restarts and variable-sized five-word batches.
        """

        key = "lexical_growth_decision_count"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, '0')",
                (key,),
            )
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?", (key,)
            ).fetchone()
            try:
                ordinal = max(0, int(row[0])) if row else 0
            except (TypeError, ValueError):
                ordinal = 0
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = ?",
                (str(ordinal + 1), key),
            )
            connection.commit()
        return ordinal

    def claim_background_growth_decision(self) -> int:
        """Claim the next durable outer growth-lane ordinal.

        Wall-clock modulo scheduling can repeatedly choose the same lane when
        one local-model job lasts an exact multiple of the planning interval.
        This independent persisted counter makes lexical and morphology turns
        advance once per actual planning decision and survive worker restarts.
        """

        key = "bounded_background_growth_decision_count_v1"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, '0')",
                (key,),
            )
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?", (key,)
            ).fetchone()
            try:
                ordinal = max(0, int(row[0])) if row else 0
            except (TypeError, ValueError):
                ordinal = 0
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = ?",
                (str(ordinal + 1), key),
            )
            connection.commit()
        return ordinal

    def accepted_morphology_components(
        self, term_entity_id: str, *, authoritative_only: bool = False,
        include_partial: bool = False,
        _connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Return live-support typed components for one term.

        Authoritative reads retain cited book owners and explicit review owners;
        raw corpus labels and unsupported accepted rows never qualify. Partial
        source memberships are opt-in for projection, never authoritative slots.
        The internal connection option keeps repair planning inside its writer
        transaction and never takes ownership of the caller's connection.
        """

        with (closing(self._connect()) if _connection is None
              else nullcontext(_connection)) as connection:
            rows = connection.execute(
                f"""SELECT assertion.*, entity.canonical_key AS entity_canonical_key,
                          morpheme.form AS canonical_form,
                          morpheme.normalized AS base,
                          morpheme.kind AS morpheme_kind,
                          morpheme.language AS morpheme_language,
                          morpheme.meaning AS morpheme_meaning
                   FROM relation_assertions AS assertion
                   JOIN entities AS entity
                     ON entity.entity_id = assertion.target_entity_id
                    AND entity.status = 'accepted'
                   JOIN morphemes AS morpheme
                     ON morpheme.entity_id = assertion.target_entity_id
                   WHERE {LIVE_SOURCE_COMPONENT_SQL}
                     AND (? = 1 OR assertion.relation = 'has-component')
                     AND assertion.subject_entity_id = ?
                     AND assertion.source_entity_id = ?
                     AND EXISTS (
                         SELECT 1 FROM relation_assertion_supports AS support
                         WHERE support.assertion_id = assertion.assertion_id
                           AND support.status = 'accepted'
                           AND (
                               ? = 0 OR support.basis = 'book'
                               OR support.owner_type LIKE '%review%'
                               OR support.owner_type LIKE '%curator%'
                           )
                     )
                   ORDER BY assertion.assertion_id""",
                (int(include_partial and not authoritative_only),
                 term_entity_id, term_entity_id, int(authoritative_only)),
            ).fetchall()
            supports_by_assertion = {
                str(row["assertion_id"]): {
                    "bases": sorted(
                        {
                            str(item["basis"])
                            for item in connection.execute(
                                """SELECT basis FROM relation_assertion_supports
                                   WHERE assertion_id = ? AND status = 'accepted'""",
                                (str(row["assertion_id"]),),
                            )
                        }
                    ),
                    "owners": sorted(
                        {
                            str(item["owner_type"])
                            for item in connection.execute(
                                """SELECT owner_type
                                   FROM relation_assertion_supports
                                   WHERE assertion_id = ? AND status = 'accepted'""",
                                (str(row["assertion_id"]),),
                            )
                        }
                    ),
                }
                for row in rows
            }
            evidence_by_assertion = {
                str(row["assertion_id"]): [
                    str(item[0])
                    for item in connection.execute(
                        """SELECT evidence_id FROM assertion_evidence
                           WHERE assertion_id = ? ORDER BY evidence_id""",
                        (str(row["assertion_id"]),),
                    ).fetchall()
                ]
                for row in rows
            }
            meaning_by_component = self._accepted_meaning_records(
                connection,
                (str(row["target_entity_id"]) for row in rows),
            )
            term_row = connection.execute(
                "SELECT text,normalized FROM terms WHERE entity_id=?",
                (term_entity_id,),
            ).fetchone()
            term_form = str(term_row[0] or term_row[1]) if term_row is not None else ""
        if not rows:
            return []

        accepted: list[dict[str, Any]] = []
        for row in rows:
            assertion = dict(row)
            target_id = str(assertion.get("target_entity_id", "")).strip()
            kind = str(assertion.get("morpheme_kind", "")).strip().casefold()
            if kind not in {"root", "prefix", "suffix"}:
                continue
            properties: dict[str, Any] = {}
            raw_properties = assertion.get("properties_json", "")
            if isinstance(raw_properties, str) and raw_properties:
                try:
                    parsed = json.loads(raw_properties)
                    properties = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    properties = {}
            form = str(assertion.get("canonical_form", "")).strip()
            if not target_id or not form:
                continue
            accepted.append(
                {
                    "morpheme_id": target_id,
                    "base": str(assertion.get("base", "")).strip(),
                    "canonical_form": form,
                    "surface": str(properties.get("surface", "")).strip(),
                    "ordinal": int(properties.get("ordinal", 0) or 0),
                    "kind": kind,
                    "meaning": str(
                        meaning_by_component.get(target_id, [{}])[0].get(
                            "definition",
                            assertion.get("morpheme_meaning", ""),
                        )
                    ).strip(),
                    "language": str(
                        assertion.get("morpheme_language", "")
                    ).strip(),
                    "basis": str(assertion.get("basis", "model")).strip()
                    or "model",
                    "confidence": assertion.get("confidence", 0.7),
                    "evidence_ids": evidence_by_assertion.get(
                        str(assertion.get("assertion_id", "")), []
                    ),
                    "assertion_id": str(assertion.get("assertion_id", "")),
                    "revision": int(assertion.get("revision", 0) or 0),
                    "_entity_canonical_key": str(
                        assertion.get("entity_canonical_key", "")
                    ),
                    "support_bases": supports_by_assertion.get(
                        str(assertion.get("assertion_id", "")), {}
                    ).get("bases", []),
                    "support_owner_types": supports_by_assertion.get(
                        str(assertion.get("assertion_id", "")), {}
                    ).get("owners", []),
                    **({
                        "projection_relation": str(assertion.get("relation", "")),
                        "source_span": properties.get("source_span"),
                        "analysis_scope": properties.get("analysis_scope", ""),
                    } if include_partial else {}),
                }
            )
        semantic_groups: list[list[dict[str, Any]]] = []
        for component in accepted:
            try:
                identity = canonical_lexical_node_identity(
                    term_entity_id,
                    "morpheme",
                    component["language"],
                    component["canonical_form"],
                    role=component["kind"],
                ) + (component_projection_occurrence(term_form, component)
                     if include_partial else canonical_morpheme_occurrence_identity(
                         term_form, component["language"],
                         component["canonical_form"], component["kind"],
                         component["ordinal"],
                     ))
            except ValueError:
                identity = ("entity-id", str(component["morpheme_id"]))
            component["_semantic_identity"] = identity
            evidence = {str(item) for item in component.get("evidence_ids", [])}
            meaning = _normalise(component.get("meaning", ""))
            matched: list[dict[str, Any]] | None = None
            for group in semantic_groups:
                if group[0]["_semantic_identity"] != identity:
                    continue
                if any(
                    str(other["morpheme_id"]) == str(component["morpheme_id"])
                    or bool(
                        evidence
                        & {str(item) for item in other.get("evidence_ids", [])}
                    )
                    for other in group
                ):
                    matched = group
                    break
            if matched is None:
                semantic_groups.append([component])
            else:
                matched.append(component)

        coalesced: list[dict[str, Any]] = []
        basis_rank = {"derived": 0, "model": 1, "book": 2, "reviewed": 3}
        for group in semantic_groups:
            def preference(item: dict[str, Any]) -> tuple[int, int, float, int, str]:
                try:
                    desired_key = canonical_morpheme_identity(
                        item["language"], item["canonical_form"], item["kind"]
                    )[2]
                except ValueError:
                    desired_key = ""
                support_rank = max(
                    (
                        basis_rank.get(str(value), -1)
                        for value in item.get("support_bases", [])
                    ),
                    default=basis_rank.get(str(item.get("basis", "")), -1),
                )
                return (
                    int(item.get("_entity_canonical_key") == desired_key),
                    support_rank,
                    float(item.get("confidence", 0) or 0),
                    int(item.get("revision", 0) or 0),
                    str(item.get("morpheme_id", "")),
                )

            representative = dict(max(group, key=preference))
            representative["ordinal"] = min(
                int(item.get("ordinal", 0) or 0) for item in group
            )
            representative["evidence_ids"] = sorted(
                {
                    str(value)
                    for item in group
                    for value in item.get("evidence_ids", [])
                    if str(value)
                }
            )
            representative["support_bases"] = sorted(
                {
                    str(value)
                    for item in group
                    for value in item.get("support_bases", [])
                    if str(value)
                }
            )
            representative["support_owner_types"] = sorted(
                {
                    str(value)
                    for item in group
                    for value in item.get("support_owner_types", [])
                    if str(value)
                }
            )
            if not str(representative.get("meaning", "")).strip():
                representative["meaning"] = next(
                    (
                        str(item.get("meaning", "")).strip()
                        for item in sorted(group, key=preference, reverse=True)
                        if str(item.get("meaning", "")).strip()
                    ),
                    "",
                )
            representative.pop("_entity_canonical_key", None)
            representative.pop("_semantic_identity", None)
            if include_partial:
                representative["projection_alias_ids"] = sorted({
                    str(item["morpheme_id"]) for item in group
                })
            coalesced.append(representative)
        accepted = coalesced
        if include_partial:
            return merge_partial_component_projection(term_form, accepted)
        accepted.sort(
            key=lambda item: (
                int(item.get("ordinal", 0)),
                str(item.get("assertion_id", "")),
            )
        )
        return accepted

    def lexical_semantic_enrichment_state(
        self, entity_id: str, language: str = "en"
    ) -> dict[str, Any] | None:
        language = _language(language)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM lexical_semantic_enrichments
                   WHERE entity_id = ? AND language = ?""",
                (entity_id.strip(), language),
            ).fetchone()
        return dict(row) if row is not None else None

    def link_retrieved_semantic_evidence(
        self, entity_id: str, records: list[dict[str, Any]]
    ) -> list[str]:
        """Attach corpus context inside an atomic semantic write, not a role claim."""
        identifiers = []
        with closing(self._connect()) as connection:
            for record in records:
                corpus_id = str(record["corpus_id"]).strip()
                entry_id = str(record["entry_id"]).strip()
                excerpt = str(record["excerpt"])
                locator = str(record.get("locator") or "")
                if not corpus_id or not entry_id or not excerpt.strip():
                    raise ValueError("retrieved semantic evidence has no corpus identity or text")
                existing = connection.execute(
                    """SELECT evidence_id FROM evidence_records
                       WHERE corpus_id = ? AND source_entry_id = ?
                         AND excerpt = ? AND locator = ?
                       ORDER BY evidence_id LIMIT 1""",
                    (corpus_id, entry_id, excerpt, locator),
                ).fetchone()
                if existing is not None:
                    evidence_id = str(existing[0])
                else:
                    source_hash = hashlib.sha256(
                        json.dumps(
                            record, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    evidence_id = self.add_evidence(
                        corpus_id, entry_id, source_hash=source_hash,
                        locator=locator, excerpt=excerpt, payload=record,
                    )
                self.link_evidence(
                    entity_id, evidence_id, claim="retrieved local book context",
                    confidence=0.5,
                )
                identifiers.append(evidence_id)
        return list(dict.fromkeys(identifiers))

    def semantic_enrichment_context(
        self, entity_id: str, language: str = "en"
    ) -> dict[str, Any]:
        from .semantic_contract import SemanticContextUnavailable

        language = _language(language)
        with closing(self._connect()) as connection:
            snapshot = self._semantic_enrichment_snapshot(
                connection, entity_id.strip(), language
            )
        if snapshot is None:
            raise SemanticContextUnavailable(
                f"entity is not eligible for semantic enrichment: {entity_id!r}"
            )
        return snapshot

    def refresh_lexical_semantic_enrichment(
        self, entity_id: str, language: str = "en"
    ) -> dict[str, Any] | None:
        language = _language(language)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._refresh_semantic_enrichment_state(
                    connection, entity_id.strip(), language=language
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.lexical_semantic_enrichment_state(entity_id, language)

    def bootstrap_lexical_semantic_enrichments(
        self, limit: int = 64
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = self._bootstrap_lexical_semantic_enrichments_connection(
                    connection, limit=limit
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _matching_semantic_job(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT job_id, job_type, subject_entity_id,
                      source_fingerprint, status, error, updated_at
               FROM preparation_jobs
               WHERE subject_entity_id = ? AND job_type = ?
                 AND source_fingerprint = ?
               ORDER BY created_at DESC, job_id DESC LIMIT 1""",
            (
                str(row["entity_id"]),
                str(row["job_type"]),
                str(row["desired_fingerprint"]),
            ),
        ).fetchone()

    def _requeue_model_context_semantic_failures_connection(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, str]]:
        """Retry legacy evidence-only failures under the model-context policy."""

        rows = connection.execute(
            """SELECT * FROM lexical_semantic_enrichments
               WHERE entity_type = 'morpheme'
                 AND job_type = 'prepare-entity-meaning'
                 AND status <> 'applied'"""
        ).fetchall()
        timestamp = _now()
        outcomes: list[dict[str, str]] = []
        for row in rows:
            job = self._matching_semantic_job(connection, row)
            if (
                job is None
                or str(job["status"]) != "failed"
                or str(job["error"]) != _SEMANTIC_MODEL_CONTEXT_RECOVERY_ERROR
            ):
                continue
            job_id = str(job["job_id"])
            connection.execute(
                """UPDATE preparation_jobs
                   SET status = 'queued', attempts = 0, locked_at = '', error = '',
                       updated_at = ?
                   WHERE job_id = ? AND status = 'failed' AND error = ?""",
                (
                    timestamp,
                    job_id,
                    _SEMANTIC_MODEL_CONTEXT_RECOVERY_ERROR,
                ),
            )
            connection.execute(
                """UPDATE lexical_semantic_enrichments
                   SET status = 'planned', enrichment_job_id = ?,
                       bound_fingerprint = desired_fingerprint,
                       lease_token = '', lease_expires_at = ?,
                       next_eligible_at = '', last_error = '', updated_at = ?
                   WHERE entity_id = ? AND language = ?""",
                (
                    job_id,
                    (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    timestamp,
                    str(row["entity_id"]),
                    str(row["language"]),
                ),
            )
            outcomes.append(
                {
                    "entity_id": str(row["entity_id"]),
                    "outcome": "model-context-requeued",
                }
            )
        return outcomes

    def _reconcile_lexical_semantic_enrichments_connection(
        self, connection: sqlite3.Connection, *, limit: int
    ) -> list[dict[str, str]]:
        limit = max(1, min(int(limit), 256))
        timestamp = _now()
        outcomes = self._requeue_model_context_semantic_failures_connection(
            connection
        )
        rows = connection.execute(
            """SELECT * FROM lexical_semantic_enrichments
               WHERE status IN ('pending', 'claimed', 'planned', 'cooldown')
               ORDER BY CASE WHEN status IN ('claimed', 'planned') THEN 0 ELSE 1 END,
                        updated_at, entity_id, language LIMIT ?""",
            (limit,),
        ).fetchall()
        for initial in rows:
            entity_id = str(initial["entity_id"])
            language = str(initial["language"])
            self._refresh_semantic_enrichment_state(
                connection, entity_id, language=language, timestamp=timestamp
            )
            row = connection.execute(
                """SELECT * FROM lexical_semantic_enrichments
                   WHERE entity_id = ? AND language = ?""",
                (entity_id, language),
            ).fetchone()
            if row is None or str(row["status"]) == "applied":
                if row is not None and str(initial["status"]) != "applied":
                    outcomes.append({"entity_id": entity_id, "outcome": "applied"})
                continue
            job = self._matching_semantic_job(connection, row)
            if job is not None and str(job["status"]) in {"queued", "running"}:
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET status = 'planned', enrichment_job_id = ?,
                           bound_fingerprint = ?, lease_token = '',
                           lease_expires_at = ?, updated_at = ?
                       WHERE entity_id = ? AND language = ?""",
                    (
                        str(job["job_id"]),
                        str(row["desired_fingerprint"]),
                        (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                        timestamp,
                        entity_id,
                        language,
                    ),
                )
                if (
                    str(row["status"]) != "planned"
                    or str(row["enrichment_job_id"] or "") != str(job["job_id"])
                ):
                    outcomes.append(
                        {"entity_id": entity_id, "outcome": "orphan-adopted"}
                    )
                continue
            terminal = str(job["status"]) if job is not None else ""
            already_cooled = bool(
                str(row["status"]) == "cooldown"
                and job is not None
                and str(row["enrichment_job_id"] or "") == str(job["job_id"])
            )
            if terminal in {"complete", "failed"} and not already_cooled:
                failures = int(row["failure_count"]) + 1
                hours = min(24 * 30, 6 * (2 ** min(failures - 1, 7)))
                error = (
                    str(job["error"] or "")
                    if terminal == "failed"
                    else "semantic job completed without an accepted meaning"
                )
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET status = 'cooldown', enrichment_job_id = ?,
                           bound_fingerprint = desired_fingerprint,
                           lease_token = '', lease_expires_at = '',
                           failure_count = ?, next_eligible_at = ?,
                           last_error = ?, updated_at = ?
                       WHERE entity_id = ? AND language = ?""",
                    (
                        str(job["job_id"]),
                        failures,
                        (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
                        error[:2000],
                        timestamp,
                        entity_id,
                        language,
                    ),
                )
                outcomes.append({"entity_id": entity_id, "outcome": "cooldown"})
                continue
            if (
                str(row["status"]) in {"claimed", "planned"}
                and str(row["lease_expires_at"] or "")
                and str(row["lease_expires_at"]) <= timestamp
                and job is None
            ):
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET status = 'pending', lease_token = '',
                           lease_expires_at = '', enrichment_job_id = NULL,
                           bound_fingerprint = '', updated_at = ?
                       WHERE entity_id = ? AND language = ?""",
                    (timestamp, entity_id, language),
                )
                outcomes.append({"entity_id": entity_id, "outcome": "released"})
        return outcomes

    def reconcile_lexical_semantic_enrichments(
        self, limit: int = 32
    ) -> list[dict[str, str]]:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                outcomes = self._reconcile_lexical_semantic_enrichments_connection(
                    connection, limit=limit
                )
                connection.commit()
                return outcomes
            except Exception:
                connection.rollback()
                raise

    def claim_lexical_semantic_enrichment(
        self, *, lease_seconds: int = 900, bootstrap_limit: int = 64
    ) -> dict[str, Any] | None:
        from .semantic_contract import SEMANTIC_JOB_PROMPT_MARKER

        lease_seconds = max(30, min(int(lease_seconds), 86_400))
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._bootstrap_lexical_semantic_enrichments_connection(
                    connection, limit=bootstrap_limit
                )
                self._reconcile_lexical_semantic_enrichments_connection(
                    connection, limit=64
                )
                active = connection.execute(
                    """SELECT 1 FROM lexical_semantic_enrichments
                       WHERE status IN ('claimed', 'planned') LIMIT 1"""
                ).fetchone()
                running = connection.execute(
                    """SELECT 1 FROM preparation_jobs
                       WHERE status = 'running'
                         AND (job_type = 'prepare-entity-meaning'
                              OR (job_type = 'prepare-meaning'
                                  AND substr(prompt_version, 1, ?) = ?))
                       LIMIT 1""",
                    (len(SEMANTIC_JOB_PROMPT_MARKER), SEMANTIC_JOB_PROMPT_MARKER),
                ).fetchone()
                if active is not None or running is not None:
                    # Changed inputs can detach a running job from its old
                    # binding. Preserve that inference and global single flight.
                    connection.commit()
                    return None
                timestamp = _now()
                lane_key = "semantic_meaning_last_lane_v1"
                last_lane = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = ?", (lane_key,)
                ).fetchone()
                prefer_review = int(last_lane is not None and str(last_lane[0]) == "missing")
                candidates = connection.execute(
                    """SELECT semantic.*, entity.label,
                       EXISTS (
                           SELECT 1 FROM meanings AS meaning
                           JOIN entities AS meaning_entity
                             ON meaning_entity.entity_id = meaning.entity_id
                            AND meaning_entity.status = 'accepted'
                           JOIN entity_edges AS meaning_edge
                             ON meaning_edge.source_entity_id = semantic.entity_id
                            AND meaning_edge.target_entity_id = meaning.entity_id
                            AND meaning_edge.relation = 'has-meaning'
                            AND meaning_edge.status = 'accepted'
                           WHERE meaning.subject_entity_id = semantic.entity_id
                             AND meaning.language = semantic.language
                       ) AS is_review
                       FROM lexical_semantic_enrichments AS semantic
                       JOIN entities AS entity
                         ON entity.entity_id = semantic.entity_id
                        AND entity.status = 'accepted'
                       WHERE semantic.status IN ('pending', 'cooldown')
                         AND semantic.desired_fingerprint <> ''
                         AND semantic.desired_fingerprint <>
                             semantic.applied_fingerprint
                         AND (semantic.next_eligible_at = ''
                              OR semantic.next_eligible_at <= ?)
                         AND NOT EXISTS (
                             SELECT 1 FROM preparation_jobs AS active
                             WHERE active.subject_entity_id = semantic.entity_id
                               AND active.job_type = semantic.job_type
                               AND active.source_fingerprint =
                                   semantic.desired_fingerprint
                               AND active.status IN ('queued', 'running')
                         )
                       ORDER BY CASE WHEN EXISTS (
                           SELECT 1 FROM meanings AS meaning
                           JOIN entities AS meaning_entity
                             ON meaning_entity.entity_id = meaning.entity_id
                            AND meaning_entity.status = 'accepted'
                           JOIN entity_edges AS meaning_edge
                             ON meaning_edge.source_entity_id = semantic.entity_id
                            AND meaning_edge.target_entity_id = meaning.entity_id
                            AND meaning_edge.relation = 'has-meaning'
                            AND meaning_edge.status = 'accepted'
                           WHERE meaning.subject_entity_id = semantic.entity_id
                             AND meaning.language = semantic.language
                       ) = ? THEN 0 ELSE 1 END,
                                CASE semantic.entity_type
                                  WHEN 'morpheme' THEN 0 ELSE 1 END,
                                CASE semantic.status
                                  WHEN 'pending' THEN 0 ELSE 1 END,
                                semantic.updated_at, semantic.entity_id
                       LIMIT 8""",
                    (timestamp, prefer_review),
                ).fetchall()
                row = None
                for candidate in candidates:
                    entity_id = str(candidate["entity_id"])
                    language = str(candidate["language"])
                    snapshot = self._semantic_enrichment_snapshot(
                        connection, entity_id, language
                    )
                    if snapshot is None:
                        # Invalidated identities must not monopolize admission.
                        # Keep their history, but back off without a fake receipt.
                        connection.execute(
                            """UPDATE lexical_semantic_enrichments
                               SET status = 'cooldown', next_eligible_at = ?,
                                   last_error = 'semantic subject is no longer eligible',
                                   updated_at = ?
                               WHERE entity_id = ? AND language = ?""",
                            ((datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                             timestamp, entity_id, language),
                        )
                        continue
                    # A bounded catalog scan cannot certify the selected row.
                    # Revalidate this candidate while holding the writer lock.
                    self._refresh_semantic_enrichment_state(
                        connection, entity_id, language=language, timestamp=timestamp
                    )
                    fresh = connection.execute(
                        """SELECT semantic.*, entity.label
                           FROM lexical_semantic_enrichments AS semantic
                           JOIN entities AS entity ON entity.entity_id = semantic.entity_id
                           WHERE semantic.entity_id = ? AND semantic.language = ?
                             AND entity.status = 'accepted'""",
                        (entity_id, language),
                    ).fetchone()
                    if (
                        fresh is None
                        or str(fresh["status"]) not in {"pending", "cooldown"}
                        or not str(fresh["desired_fingerprint"])
                        or str(fresh["desired_fingerprint"]) == str(fresh["applied_fingerprint"])
                        or str(fresh["desired_fingerprint"]) != str(snapshot["desired_fingerprint"])
                        or (str(fresh["next_eligible_at"]) and str(fresh["next_eligible_at"]) > timestamp)
                    ):
                        continue
                    existing_job = connection.execute(
                        """SELECT job_id FROM preparation_jobs
                           WHERE subject_entity_id = ? AND language = ?
                             AND job_type = ? AND source_fingerprint = ?
                             AND status IN ('queued', 'running')
                           ORDER BY created_at DESC, job_id DESC LIMIT 1""",
                        (entity_id, language, str(fresh["job_type"]),
                         str(fresh["desired_fingerprint"])),
                    ).fetchone()
                    if existing_job is not None:
                        connection.execute(
                            """UPDATE lexical_semantic_enrichments
                               SET status = 'planned', enrichment_job_id = ?,
                                   bound_fingerprint = desired_fingerprint,
                                   lease_token = '', lease_expires_at = ?, updated_at = ?
                               WHERE entity_id = ? AND language = ?""",
                            (str(existing_job["job_id"]),
                             (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                             timestamp, entity_id, language),
                        )
                        connection.commit()
                        return None
                    row = dict(fresh)
                    row["is_review"] = int(bool(snapshot["has_accepted_meaning"]))
                    break
                if row is None:
                    connection.commit()
                    return None
                lease_token = uuid.uuid4().hex
                lease_expires_at = (
                    datetime.now(UTC) + timedelta(seconds=lease_seconds)
                ).isoformat()
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET status = 'claimed', lease_token = ?,
                           lease_expires_at = ?, attempt_count = attempt_count + 1,
                           last_claimed_at = ?, updated_at = ?
                       WHERE entity_id = ? AND language = ?
                         AND status IN ('pending', 'cooldown')""",
                    (
                        lease_token,
                        lease_expires_at,
                        timestamp,
                        timestamp,
                        str(row["entity_id"]),
                        str(row["language"]),
                    ),
                )
                selected_lane = "review" if bool(row["is_review"]) else "missing"
                connection.execute(
                    "INSERT INTO schema_meta(key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (lane_key, selected_lane),
                )
                connection.commit()
                result = dict(row)
                result.update(
                    {
                        "status": "claimed",
                        "semantic_lane": selected_lane,
                        "lease_token": lease_token,
                        "lease_expires_at": lease_expires_at,
                        "attempt_count": int(row["attempt_count"]) + 1,
                    }
                )
                return result
            except Exception:
                connection.rollback()
                raise

    def bind_lexical_semantic_enrichment(
        self,
        entity_id: str,
        desired_fingerprint: str,
        lease_token: str,
        enrichment_job_id: str,
        *,
        language: str = "en",
        lease_seconds: int = 86_400,
    ) -> bool:
        language = _language(language)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                state = connection.execute(
                    """SELECT * FROM lexical_semantic_enrichments
                       WHERE entity_id = ? AND language = ?""",
                    (entity_id.strip(), language),
                ).fetchone()
                job = connection.execute(
                    """SELECT job_type, subject_entity_id, source_fingerprint
                       FROM preparation_jobs WHERE job_id = ?""",
                    (enrichment_job_id.strip(),),
                ).fetchone()
                if state is None:
                    connection.commit()
                    return False
                if (
                    job is None
                    or str(job["job_type"]) != str(state["job_type"])
                    or str(job["subject_entity_id"]) != entity_id.strip()
                    or str(job["source_fingerprint"]) != desired_fingerprint.strip()
                ):
                    raise ValueError("semantic enrichment job identity does not match claim")
                if (
                    str(state["status"]) == "planned"
                    and str(state["enrichment_job_id"] or "")
                    == enrichment_job_id.strip()
                    and str(state["bound_fingerprint"])
                    == desired_fingerprint.strip()
                ):
                    connection.commit()
                    return True
                if (
                    str(state["status"]) != "claimed"
                    or str(state["desired_fingerprint"])
                    != desired_fingerprint.strip()
                    or str(state["lease_token"]) != lease_token.strip()
                ):
                    connection.commit()
                    return False
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET status = 'planned', enrichment_job_id = ?,
                           bound_fingerprint = ?, lease_token = '',
                           lease_expires_at = ?, updated_at = ?
                       WHERE entity_id = ? AND language = ?""",
                    (
                        enrichment_job_id.strip(),
                        desired_fingerprint.strip(),
                        (
                            datetime.now(UTC)
                            + timedelta(
                                seconds=max(
                                    30, min(int(lease_seconds), 86_400)
                                )
                            )
                        ).isoformat(),
                        _now(),
                        entity_id.strip(),
                        language,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def release_lexical_semantic_enrichment(
        self,
        entity_id: str,
        desired_fingerprint: str,
        lease_token: str,
        *,
        language: str = "en",
    ) -> bool:
        language = _language(language)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE lexical_semantic_enrichments
                   SET status = 'pending', lease_token = '', lease_expires_at = '',
                       enrichment_job_id = NULL, bound_fingerprint = '',
                       updated_at = ?
                   WHERE entity_id = ? AND language = ? AND status = 'claimed'
                     AND desired_fingerprint = ? AND lease_token = ?""",
                (
                    _now(),
                    entity_id.strip(),
                    language,
                    desired_fingerprint.strip(),
                    lease_token.strip(),
                ),
            )
            connection.commit()
            return bool(cursor.rowcount)

    def fail_lexical_semantic_enrichment(
        self,
        entity_id: str,
        desired_fingerprint: str,
        error: str,
        *,
        language: str = "en",
        lease_token: str = "",
        enrichment_job_id: str = "",
    ) -> bool:
        language = _language(language)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """SELECT * FROM lexical_semantic_enrichments
                       WHERE entity_id = ? AND language = ?""",
                    (entity_id.strip(), language),
                ).fetchone()
                if (
                    row is None
                    or str(row["desired_fingerprint"])
                    != desired_fingerprint.strip()
                ):
                    connection.commit()
                    return False
                authorized = bool(
                    (
                        str(row["status"]) == "claimed"
                        and lease_token.strip()
                        and str(row["lease_token"]) == lease_token.strip()
                    )
                    or (
                        str(row["status"]) == "planned"
                        and enrichment_job_id.strip()
                        and str(row["enrichment_job_id"] or "")
                        == enrichment_job_id.strip()
                    )
                )
                if not authorized:
                    connection.commit()
                    return False
                failures = int(row["failure_count"]) + 1
                hours = min(24 * 30, 6 * (2 ** min(failures - 1, 7)))
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET status = 'cooldown', lease_token = '',
                           lease_expires_at = '', failure_count = ?,
                           next_eligible_at = ?, last_error = ?,
                           enrichment_job_id = COALESCE(
                               NULLIF(?, ''), enrichment_job_id
                           ),
                           bound_fingerprint = desired_fingerprint, updated_at = ?
                       WHERE entity_id = ? AND language = ?""",
                    (
                        failures,
                        (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
                        error[:2000],
                        enrichment_job_id.strip(),
                        _now(),
                        entity_id.strip(),
                        language,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def complete_lexical_semantic_enrichment(
        self,
        entity_id: str,
        desired_fingerprint: str,
        *,
        language: str = "en",
        enrichment_job_id: str = "",
    ) -> bool:
        language = _language(language)
        entity_id = entity_id.strip()
        desired_fingerprint = desired_fingerprint.strip()
        enrichment_job_id = enrichment_job_id.strip()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._refresh_semantic_enrichment_state(
                    connection, entity_id, language=language
                )
                row = connection.execute(
                    """SELECT * FROM lexical_semantic_enrichments
                       WHERE entity_id = ? AND language = ?""",
                    (entity_id, language),
                ).fetchone()
                snapshot = self._semantic_enrichment_snapshot(
                    connection, entity_id, language
                )
                if (
                    row is None or snapshot is None
                    or not bool(snapshot["has_current_meaning"])
                    or str(row["desired_fingerprint"]) != desired_fingerprint
                    or (
                        enrichment_job_id
                        and str(row["enrichment_job_id"] or "")
                        not in {"", enrichment_job_id}
                    )
                ):
                    connection.commit()
                    return False
                current_jobs = dict(snapshot["current_meaning_jobs"])
                if current_jobs and enrichment_job_id not in {"", *current_jobs.values()}:
                    connection.commit()
                    return False
                if enrichment_job_id:
                    job = connection.execute(
                        """SELECT job_type,subject_entity_id,language,source_fingerprint
                           FROM preparation_jobs WHERE job_id=?""",
                        (enrichment_job_id,),
                    ).fetchone()
                    if (
                        job is None or str(job["job_type"]) != str(row["job_type"])
                        or str(job["subject_entity_id"]) != entity_id
                        or str(job["language"]) != language
                        or str(job["source_fingerprint"]) != desired_fingerprint
                    ):
                        connection.commit()
                        return False
                timestamp = _now()
                retired: list[str] = []
                old_definitions: set[str] = set()
                if current_jobs:
                    for meaning in snapshot["accepted_meanings"]:
                        meaning_id = str(meaning["meaning_id"])
                        owner = str(meaning.get("properties", {}).get(
                            "semantic_enrichment_job_id", ""
                        ))
                        if meaning_id in current_jobs or str(meaning.get("basis", "")) != "model" or not owner:
                            continue
                        owner_job = connection.execute(
                            """SELECT 1 FROM preparation_jobs
                               WHERE job_id=? AND subject_entity_id=? AND language=?
                                 AND job_type='prepare-entity-meaning'""",
                            (owner, entity_id, language),
                        ).fetchone()
                        if owner_job is None:
                            continue
                        connection.execute(
                            "UPDATE entities SET status='archived',updated_at=? "
                            "WHERE entity_id=? AND status='accepted'",
                            (timestamp, meaning_id),
                        )
                        retired.append(meaning_id)
                        old_definitions.add(str(meaning["definition"]))
                    if retired:
                        # Only a cache equal to our retired generated text is replaceable.
                        # Independent book/reviewed definitions remain authoritative.
                        protected = any(
                            str(item.get("basis", "")) != "model"
                            for item in snapshot["accepted_meanings"]
                        )
                        cached = connection.execute(
                            "SELECT meaning FROM morphemes WHERE entity_id=?", (entity_id,)
                        ).fetchone()
                        if not protected and cached and str(cached[0]) in old_definitions:
                            replacement = next(
                                str(item["definition"]) for item in snapshot["accepted_meanings"]
                                if str(item["meaning_id"]) in current_jobs
                            )
                            connection.execute(
                                "UPDATE morphemes SET meaning=? WHERE entity_id=?",
                                (replacement, entity_id),
                            )
                            entity = connection.execute(
                                "SELECT payload FROM entities WHERE entity_id=?", (entity_id,)
                            ).fetchone()
                            payload = _json_object(str(entity[0]))
                            payload["meaning"] = replacement
                            connection.execute(
                                "UPDATE entities SET payload=?,updated_at=? WHERE entity_id=?",
                                (json.dumps(payload, ensure_ascii=False), timestamp, entity_id),
                            )
                        self._mark_projection_repairs_for_entities(
                            connection, (entity_id, *retired, *current_jobs)
                        )
                # A first correction is immediate; repeated reviews get a quiet period.
                next_eligible = (
                    (datetime.now(UTC) + timedelta(hours=1)).isoformat()
                    if current_jobs and int(row["attempt_count"]) > 1 else ""
                )
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET status = 'applied', applied_fingerprint = ?,
                           lease_token = '', lease_expires_at = '',
                           enrichment_job_id = COALESCE(NULLIF(?, ''), enrichment_job_id),
                           bound_fingerprint = '', failure_count = 0,
                           next_eligible_at = ?, last_error = '',
                           last_applied_at = ?, updated_at = ?
                       WHERE entity_id = ? AND language = ?""",
                    (
                        desired_fingerprint, enrichment_job_id, next_eligible,
                        timestamp, timestamp, entity_id, language,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def lexical_projection_repair_state(
        self, term_entity_id: str
    ) -> dict[str, Any] | None:
        """Return one durable subject-local projection state."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT repair.*, term.language, term.text AS term
                   FROM lexical_projection_repairs AS repair
                   JOIN terms AS term ON term.entity_id = repair.term_entity_id
                   WHERE repair.term_entity_id = ?""",
                (term_entity_id.strip(),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            modes = json.loads(str(result.pop("desired_modes_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            modes = []
        result["desired_modes"] = [
            str(item) for item in modes if str(item) in {"root", "affix"}
        ] if isinstance(modes, list) else []
        return result

    def refresh_lexical_projection_repair(
        self, term_entity_id: str
    ) -> dict[str, Any] | None:
        """Idempotently refresh one subject-local desired fingerprint."""

        term_entity_id = term_entity_id.strip()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._refresh_projection_repair_state(connection, term_entity_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.lexical_projection_repair_state(term_entity_id)

    def bootstrap_lexical_projection_repairs(
        self, limit: int = 64
    ) -> dict[str, Any]:
        """Advance one restart-safe legacy bootstrap page, never an open scan."""

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = self._bootstrap_lexical_projection_repairs_connection(
                    connection, limit=limit
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _reconcile_lexical_projection_repairs_connection(
        self,
        connection: sqlite3.Connection,
        *,
        limit: int,
        audit_limit: int = 1,
    ) -> list[dict[str, Any]]:
        """Recover expired claims and terminal deterministic compose jobs."""

        limit = max(1, min(int(limit), 256))
        audit_limit = max(0, min(int(audit_limit), 64))
        timestamp = _now()
        outcomes: list[dict[str, Any]] = []
        invalidated = connection.execute(
            """SELECT repair.term_entity_id, repair.desired_fingerprint
               FROM lexical_projection_repairs AS repair
               WHERE repair.desired_fingerprint = ''
                  OR (
                      repair.status NOT IN ('claimed', 'planned')
                      AND EXISTS (
                          SELECT 1 FROM relation_assertions AS assertion
                          WHERE assertion.subject_entity_id =
                                repair.term_entity_id
                            AND assertion.source_entity_id =
                                repair.term_entity_id
                            AND assertion.relation = 'has-component'
                            AND assertion.updated_at > repair.updated_at
                      )
                  )
               ORDER BY CASE WHEN repair.desired_fingerprint = ''
                             THEN 0 ELSE 1 END,
                        repair.updated_at, repair.term_entity_id
               LIMIT ?""",
            (limit,),
        ).fetchall()
        refreshed_ids: set[str] = set()
        for row in invalidated:
            term_entity_id = str(row["term_entity_id"])
            refreshed_ids.add(term_entity_id)
            if self._refresh_projection_repair_state(
                connection, term_entity_id, timestamp=timestamp
            ):
                outcomes.append(
                    {
                        "term_entity_id": term_entity_id,
                        "outcome": (
                            "invalidation-refreshed"
                            if not str(row["desired_fingerprint"])
                            else "graph-change-refreshed"
                        ),
                    }
                )
            else:
                # Advance the bounded audit watermark even when a touched
                # assertion was irrelevant to the accepted projection.
                connection.execute(
                    """UPDATE lexical_projection_repairs SET updated_at = ?
                       WHERE term_entity_id = ?
                         AND status NOT IN ('claimed', 'planned')""",
                    (timestamp, term_entity_id),
                )
        # Historical audits are optional so a known stale published view can
        # take the model-free fast path. Direct invalidations and terminal-job
        # recovery above remain bounded by ``limit`` and are never skipped.
        audit_slots = min(audit_limit, max(0, limit - len(outcomes)))
        if audit_slots:
            parameters: list[Any] = []
            exclusion = ""
            if refreshed_ids:
                exclusion = " AND term_entity_id NOT IN (" + ",".join(
                    "?" for _ in refreshed_ids
                ) + ")"
                parameters.extend(sorted(refreshed_ids))
            parameters.append(audit_slots)
            audit_rows = connection.execute(
                """SELECT term_entity_id FROM lexical_projection_repairs
                   WHERE status IN ('applied', 'cooldown')
                     AND desired_fingerprint <> ''"""
                + exclusion
                + " ORDER BY updated_at, term_entity_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
            for row in audit_rows:
                term_entity_id = str(row["term_entity_id"])
                if self._refresh_projection_repair_state(
                    connection, term_entity_id, timestamp=timestamp
                ):
                    outcomes.append(
                        {
                            "term_entity_id": term_entity_id,
                            "outcome": "periodic-audit-refreshed",
                        }
                    )
                else:
                    connection.execute(
                        """UPDATE lexical_projection_repairs SET updated_at = ?
                           WHERE term_entity_id = ?
                             AND status IN ('applied', 'cooldown')""",
                        (timestamp, term_entity_id),
                    )
        if len(outcomes) >= limit:
            return outcomes
        remaining = max(0, limit - len(outcomes))
        expired = connection.execute(
            """SELECT term_entity_id FROM lexical_projection_repairs
               WHERE status = 'claimed' AND lease_expires_at <> ''
                 AND lease_expires_at <= ?
               ORDER BY lease_expires_at, term_entity_id LIMIT ?""",
            (timestamp, remaining),
        ).fetchall()
        for row in expired:
            connection.execute(
                """UPDATE lexical_projection_repairs
                   SET status = 'pending', lease_token = '',
                       lease_expires_at = '', updated_at = ?
                   WHERE term_entity_id = ? AND status = 'claimed'""",
                (timestamp, row["term_entity_id"]),
            )
            outcomes.append(
                {
                    "term_entity_id": str(row["term_entity_id"]),
                    "outcome": "claim-recovered",
                }
            )
        remaining = max(0, limit - len(outcomes))
        if not remaining:
            return outcomes
        planned = connection.execute(
            """SELECT repair.term_entity_id, repair.desired_fingerprint,
                      repair.bound_fingerprint, repair.compose_job_id,
                      repair.failure_count, repair.lease_expires_at,
                      job.status AS job_status, job.error AS job_error
               FROM lexical_projection_repairs AS repair
               LEFT JOIN preparation_jobs AS job
                 ON job.job_id = repair.compose_job_id
               WHERE repair.status = 'planned'
               ORDER BY repair.updated_at, repair.term_entity_id LIMIT ?""",
            (remaining,),
        ).fetchall()
        for row in planned:
            job_status = str(row["job_status"] or "")
            desired = str(row["desired_fingerprint"])
            bound = str(row["bound_fingerprint"])
            if job_status == "complete":
                current = bool(bound and bound == desired)
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET applied_fingerprint = ?, status = ?,
                           compose_job_id = NULL, bound_fingerprint = '',
                           lease_token = '', lease_expires_at = '',
                           failure_count = 0, next_eligible_at = '',
                           last_error = '', last_applied_at = ?, updated_at = ?
                       WHERE term_entity_id = ? AND status = 'planned'""",
                    (
                        bound,
                        "applied" if current else "pending",
                        timestamp,
                        timestamp,
                        row["term_entity_id"],
                    ),
                )
                outcomes.append(
                    {
                        "term_entity_id": str(row["term_entity_id"]),
                        "outcome": "applied" if current else "stale-composition",
                    }
                )
            elif job_status == "failed":
                if bound != desired:
                    status, failures, eligible = "pending", 0, ""
                else:
                    status = "cooldown"
                    failures = int(row["failure_count"]) + 1
                    eligible = (
                        datetime.now(UTC)
                        + timedelta(
                            hours=min(24 * 30, 2 ** min(failures - 1, 9))
                        )
                    ).isoformat()
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET status = ?, bound_fingerprint = '', lease_token = '',
                           lease_expires_at = '', failure_count = ?,
                           next_eligible_at = ?, last_error = ?, updated_at = ?
                       WHERE term_entity_id = ? AND status = 'planned'""",
                    (
                        status,
                        failures,
                        eligible,
                        str(row["job_error"] or "compose job failed")[:2000],
                        timestamp,
                        row["term_entity_id"],
                    ),
                )
                outcomes.append(
                    {
                        "term_entity_id": str(row["term_entity_id"]),
                        "outcome": status,
                    }
                )
            elif (
                not job_status
                and str(row["lease_expires_at"])
                and str(row["lease_expires_at"]) <= timestamp
            ):
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET status = 'pending', compose_job_id = NULL,
                           bound_fingerprint = '', lease_token = '',
                           lease_expires_at = '', updated_at = ?
                       WHERE term_entity_id = ? AND status = 'planned'""",
                    (timestamp, row["term_entity_id"]),
                )
                outcomes.append(
                    {
                        "term_entity_id": str(row["term_entity_id"]),
                        "outcome": "missing-job-recovered",
                    }
                )
        if len(outcomes) >= limit:
            return outcomes
        active = connection.execute(
            """SELECT 1 FROM lexical_projection_repairs
               WHERE status IN ('claimed', 'planned') LIMIT 1"""
        ).fetchone()
        if active is not None:
            return outcomes
        orphan = connection.execute(
            """SELECT repair.term_entity_id, repair.desired_fingerprint,
                      repair.failure_count, job.job_id, job.status AS job_status,
                      job.error AS job_error
               FROM lexical_projection_repairs AS repair
               JOIN preparation_jobs AS job
                 ON job.subject_entity_id = repair.term_entity_id
                AND job.job_type = 'compose-origin-card'
                AND job.source_fingerprint = repair.desired_fingerprint
               WHERE repair.status = 'pending'
                 AND repair.desired_fingerprint <>
                     repair.applied_fingerprint
                 AND (
                     repair.compose_job_id IS NULL
                     OR job.job_id <> repair.compose_job_id
                 )
                 AND (
                     repair.last_claimed_at = ''
                     OR job.created_at >= repair.last_claimed_at
                     OR job.updated_at >= repair.last_claimed_at
                 )
                 AND job.status IN ('queued', 'running', 'complete', 'failed')
               ORDER BY repair.updated_at, repair.term_entity_id,
                        CASE job.status
                          WHEN 'running' THEN 0
                          WHEN 'queued' THEN 1
                          WHEN 'complete' THEN 2
                          ELSE 3 END,
                        job.updated_at DESC, job.job_id
               LIMIT 1"""
        ).fetchone()
        if orphan is None:
            return outcomes
        desired = str(orphan["desired_fingerprint"])
        job_status = str(orphan["job_status"])
        if job_status == "complete":
            connection.execute(
                """UPDATE lexical_projection_repairs
                   SET applied_fingerprint = desired_fingerprint,
                       status = 'applied', compose_job_id = NULL,
                       bound_fingerprint = '', lease_token = '',
                       lease_expires_at = '', failure_count = 0,
                       next_eligible_at = '', last_error = '',
                       last_applied_at = ?, updated_at = ?
                   WHERE term_entity_id = ? AND status = 'pending'
                     AND desired_fingerprint = ?""",
                (
                    timestamp,
                    timestamp,
                    orphan["term_entity_id"],
                    desired,
                ),
            )
            outcome = "applied"
        elif job_status == "failed":
            failures = int(orphan["failure_count"]) + 1
            eligible = (
                datetime.now(UTC)
                + timedelta(hours=min(24 * 30, 2 ** min(failures - 1, 9)))
            ).isoformat()
            connection.execute(
                """UPDATE lexical_projection_repairs
                   SET status = 'cooldown', compose_job_id = ?,
                       bound_fingerprint = '', lease_token = '',
                       lease_expires_at = '', failure_count = ?,
                       next_eligible_at = ?, last_error = ?, updated_at = ?
                   WHERE term_entity_id = ? AND status = 'pending'
                     AND desired_fingerprint = ?""",
                (
                    orphan["job_id"],
                    failures,
                    eligible,
                    str(orphan["job_error"] or "compose job failed")[:2000],
                    timestamp,
                    orphan["term_entity_id"],
                    desired,
                ),
            )
            outcome = "cooldown"
        else:
            connection.execute(
                """UPDATE lexical_projection_repairs
                   SET status = 'planned', compose_job_id = ?,
                       bound_fingerprint = desired_fingerprint,
                       lease_token = '',
                       lease_expires_at = ?, updated_at = ?
                   WHERE term_entity_id = ? AND status = 'pending'
                     AND desired_fingerprint = ?""",
                (
                    orphan["job_id"],
                    (
                        datetime.now(UTC) + timedelta(days=1)
                    ).isoformat(),
                    timestamp,
                    orphan["term_entity_id"],
                    desired,
                ),
            )
            outcome = "orphan-adopted"
        outcomes.append(
            {
                "term_entity_id": str(orphan["term_entity_id"]),
                "outcome": outcome,
            }
        )
        return outcomes

    def reconcile_lexical_projection_repairs(
        self, limit: int = 32
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                outcomes = self._reconcile_lexical_projection_repairs_connection(
                    connection, limit=limit
                )
                connection.commit()
                return outcomes
            except Exception:
                connection.rollback()
                raise

    def claim_lexical_projection_repair(
        self,
        *,
        lease_seconds: int = 900,
        bootstrap_limit: int = 64,
        burst_limit: int = _PROJECTION_REPAIR_BURST_LIMIT,
    ) -> dict[str, Any] | None:
        """Claim one recomposable dirty projection without model work.

        The queue remains globally single-flight. A bounded run of completed
        repair attempts yields only when another atomic job is genuinely ready;
        deriving the run from persisted job attempts makes that fairness gate
        survive a worker restart without a second scheduler table. First
        publication and existing-view refreshes alternate durably; an empty
        group lends its turn rather than stalling the other group.
        """

        lease_seconds = max(30, min(int(lease_seconds), 86_400))
        burst_limit = max(1, min(int(burst_limit), 64))
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._bootstrap_lexical_projection_repairs_connection(
                    connection, limit=bootstrap_limit
                )
                self._reconcile_lexical_projection_repairs_connection(
                    connection, limit=64, audit_limit=0
                )
                active = connection.execute(
                    """SELECT 1 FROM lexical_projection_repairs
                       WHERE status IN ('claimed', 'planned') LIMIT 1"""
                ).fetchone()
                if active is not None:
                    connection.commit()
                    return None
                recent_attempts = connection.execute(
                    """SELECT job.job_type, job.prompt_version
                       FROM preparation_jobs AS job
                       WHERE job.attempts > 0 AND job.status <> 'running'
                       ORDER BY job.updated_at DESC, job.job_id DESC
                       LIMIT ?""",
                    (burst_limit,),
                ).fetchall()
                consecutive_repairs = 0
                for attempted in recent_attempts:
                    if (
                        str(attempted["job_type"]) == "compose-origin-card"
                        and _PROJECTION_REPAIR_PROMPT_MARKER
                        in str(attempted["prompt_version"])
                    ):
                        consecutive_repairs += 1
                        continue
                    break
                if consecutive_repairs >= burst_limit:
                    ready_other = connection.execute(
                        """SELECT 1 FROM preparation_jobs AS job
                           WHERE job.status = 'queued'
                             AND job.attempts < job.max_attempts
                             AND NOT (
                                 job.job_type = 'compose-origin-card'
                                 AND instr(job.prompt_version, ?) > 0
                             )
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM job_dependencies AS dependency
                                 JOIN preparation_jobs AS prerequisite
                                   ON prerequisite.job_id =
                                      dependency.depends_on_job_id
                                 WHERE dependency.job_id = job.job_id
                                   AND prerequisite.status <> 'complete'
                                   AND (
                                       job.job_type NOT IN (
                                           'compose-word-card',
                                           'compose-origin-card'
                                       )
                                       OR prerequisite.status <> 'failed'
                                   )
                             )
                           LIMIT 1""",
                        (_PROJECTION_REPAIR_PROMPT_MARKER,),
                    ).fetchone()
                    if ready_other is not None:
                        connection.commit()
                        return None
                timestamp = _now()

                def select_due_projection(
                    *, published: bool
                ) -> sqlite3.Row | None:
                    lane_clause = (
                        " AND repair.applied_fingerprint <> ''"
                        if published
                        else " AND repair.applied_fingerprint = ''"
                    )
                    return connection.execute(
                        """WITH recomposable_subjects AS (
                           SELECT job.subject_key
                           FROM job_artifacts AS artifact
                           JOIN preparation_jobs AS job
                             ON job.job_id = artifact.job_id
                           WHERE artifact.validation_state = 'accepted'
                             AND artifact.stage IN (?, ?)
                           GROUP BY job.subject_key
                           HAVING COUNT(DISTINCT artifact.stage) = 2
                       ), active_compositions AS (
                           SELECT DISTINCT subject_entity_id, source_fingerprint
                           FROM preparation_jobs
                           WHERE job_type = 'compose-origin-card'
                             AND status IN ('queued', 'running')
                       )
                       SELECT repair.*, term.language, term.text AS term
                       FROM lexical_projection_repairs AS repair
                       JOIN terms AS term
                         ON term.entity_id = repair.term_entity_id
                       JOIN entities AS entity
                         ON entity.entity_id = term.entity_id
                        AND entity.status = 'accepted'
                       JOIN recomposable_subjects AS ready
                         ON ready.subject_key = 'term:' || repair.term_entity_id
                       LEFT JOIN active_compositions AS active
                         ON active.subject_entity_id = repair.term_entity_id
                        AND active.source_fingerprint = repair.desired_fingerprint
                       WHERE repair.status IN ('pending', 'cooldown')
                         AND repair.desired_fingerprint <> ''
                         AND repair.desired_fingerprint <>
                             repair.applied_fingerprint
                         """
                        + lane_clause
                        + """
                         AND (repair.next_eligible_at = ''
                              OR repair.next_eligible_at <= ?)
                         AND active.subject_entity_id IS NULL
                       ORDER BY CASE repair.status
                                  WHEN 'pending' THEN 0 ELSE 1 END,
                                CASE
                                  WHEN repair.applied_fingerprint <> ''
                                  THEN COALESCE(NULLIF(repair.last_applied_at, ''),
                                                repair.created_at)
                                  ELSE repair.created_at END,
                                repair.term_entity_id
                       LIMIT 1""",
                        ("accepted-meaning", "accepted-origin-branches", timestamp),
                    ).fetchone()

                lane_key = "lexical_projection_last_publication_lane_v1"
                last_lane = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = ?", (lane_key,)
                ).fetchone()
                prefer_published = (
                    last_lane is not None and str(last_lane[0]) == "unpublished"
                )

                def recover_unmarked_changes() -> bool:
                    self._reconcile_lexical_projection_repairs_connection(
                        connection, limit=64, audit_limit=1
                    )
                    return connection.execute(
                        """SELECT 1 FROM lexical_projection_repairs
                           WHERE status IN ('claimed', 'planned') LIMIT 1"""
                    ).fetchone() is not None

                row = select_due_projection(published=prefer_published)
                if row is None and prefer_published:
                    # Keep the bounded historical audit on a refresh turn,
                    # before lending that turn to first publication.
                    if recover_unmarked_changes():
                        connection.commit()
                        return None
                    timestamp = _now()
                    row = select_due_projection(published=True)
                if row is None:
                    row = select_due_projection(published=not prefer_published)
                if row is None and not prefer_published:
                    if recover_unmarked_changes():
                        connection.commit()
                        return None
                    timestamp = _now()
                    row = select_due_projection(published=False)
                    if row is None:
                        row = select_due_projection(published=True)
                if row is None:
                    connection.commit()
                    return None
                lease_token = uuid.uuid4().hex
                lease_expires_at = (
                    datetime.now(UTC) + timedelta(seconds=lease_seconds)
                ).isoformat()
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET status = 'claimed', lease_token = ?,
                           lease_expires_at = ?, attempt_count = attempt_count + 1,
                           last_claimed_at = ?, updated_at = ?
                       WHERE term_entity_id = ?
                         AND status IN ('pending', 'cooldown')""",
                    (
                        lease_token,
                        lease_expires_at,
                        timestamp,
                        timestamp,
                        row["term_entity_id"],
                    ),
                )
                selected_lane = (
                    "published" if str(row["applied_fingerprint"]) else "unpublished"
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (lane_key, selected_lane),
                )
                connection.commit()
                result = dict(row)
                result.update(
                    {
                        "status": "claimed",
                        "lease_token": lease_token,
                        "lease_expires_at": lease_expires_at,
                        "attempt_count": int(row["attempt_count"]) + 1,
                        "desired_modes": json.loads(
                            str(row["desired_modes_json"])
                        ),
                        "recomposable": True,
                        "publication_lane": selected_lane,
                    }
                )
                result.pop("desired_modes_json", None)
                return result
            except Exception:
                connection.rollback()
                raise

    def bind_lexical_projection_repair(
        self,
        term_entity_id: str,
        desired_fingerprint: str,
        lease_token: str,
        compose_job_id: str,
        *,
        lease_seconds: int = 86_400,
    ) -> bool:
        """Bind the claimed fingerprint to its deterministic compose job."""

        term_entity_id = term_entity_id.strip()
        desired_fingerprint = desired_fingerprint.strip()
        lease_token = lease_token.strip()
        compose_job_id = compose_job_id.strip()
        if not all(
            (term_entity_id, desired_fingerprint, lease_token, compose_job_id)
        ):
            raise ValueError("projection binding requires a complete claim and job")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                job = connection.execute(
                    """SELECT job_type, subject_entity_id FROM preparation_jobs
                       WHERE job_id = ?""",
                    (compose_job_id,),
                ).fetchone()
                if (
                    job is None
                    or str(job["job_type"]) != "compose-origin-card"
                    or str(job["subject_entity_id"]) != term_entity_id
                ):
                    raise ValueError(
                        "projection repair must bind its subject compose-origin-card"
                    )
                current = connection.execute(
                    """SELECT status, desired_fingerprint, lease_token,
                              compose_job_id, bound_fingerprint
                       FROM lexical_projection_repairs
                       WHERE term_entity_id = ?""",
                    (term_entity_id,),
                ).fetchone()
                if current is None:
                    connection.commit()
                    return False
                if (
                    str(current["status"]) == "planned"
                    and str(current["compose_job_id"]) == compose_job_id
                    and str(current["bound_fingerprint"])
                    == desired_fingerprint
                ):
                    connection.commit()
                    return True
                if (
                    str(current["status"]) != "claimed"
                    or str(current["desired_fingerprint"])
                    != desired_fingerprint
                    or str(current["lease_token"]) != lease_token
                ):
                    connection.commit()
                    return False
                timestamp = _now()
                bound_until = (
                    datetime.now(UTC)
                    + timedelta(
                        seconds=max(30, min(int(lease_seconds), 7 * 86_400))
                    )
                ).isoformat()
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET status = 'planned', compose_job_id = ?,
                           bound_fingerprint = ?, lease_token = '',
                           lease_expires_at = ?, updated_at = ?
                       WHERE term_entity_id = ?""",
                    (
                        compose_job_id,
                        desired_fingerprint,
                        bound_until,
                        timestamp,
                        term_entity_id,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def release_lexical_projection_repair(
        self,
        term_entity_id: str,
        desired_fingerprint: str,
        lease_token: str,
    ) -> bool:
        """Release an unbound claim without charging a semantic failure."""

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE lexical_projection_repairs
                   SET status = 'pending', lease_token = '',
                       lease_expires_at = '', updated_at = ?
                   WHERE term_entity_id = ? AND status = 'claimed'
                     AND desired_fingerprint = ? AND lease_token = ?""",
                (
                    _now(),
                    term_entity_id.strip(),
                    desired_fingerprint.strip(),
                    lease_token.strip(),
                ),
            )
            connection.commit()
            return bool(cursor.rowcount)

    def fail_lexical_projection_repair(
        self,
        term_entity_id: str,
        desired_fingerprint: str,
        error: str,
        *,
        lease_token: str = "",
        compose_job_id: str = "",
    ) -> bool:
        """Cooldown only the same failed fingerprint; novelty remains immediate."""

        term_entity_id = term_entity_id.strip()
        desired_fingerprint = desired_fingerprint.strip()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """SELECT * FROM lexical_projection_repairs
                       WHERE term_entity_id = ?""",
                    (term_entity_id,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return False
                valid_claim = (
                    str(row["status"]) == "claimed"
                    and bool(lease_token)
                    and str(row["lease_token"]) == lease_token.strip()
                )
                valid_job = (
                    str(row["status"]) == "planned"
                    and bool(compose_job_id)
                    and str(row["compose_job_id"]) == compose_job_id.strip()
                )
                if not (valid_claim or valid_job):
                    connection.commit()
                    return False
                timestamp = _now()
                if str(row["desired_fingerprint"]) != desired_fingerprint:
                    status, failures, eligible = "pending", 0, ""
                else:
                    status = "cooldown"
                    failures = int(row["failure_count"]) + 1
                    eligible = (
                        datetime.now(UTC)
                        + timedelta(
                            hours=min(24 * 30, 2 ** min(failures - 1, 9))
                        )
                    ).isoformat()
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET status = ?, lease_token = '', lease_expires_at = '',
                           bound_fingerprint = '',
                           failure_count = ?, next_eligible_at = ?,
                           last_error = ?, updated_at = ?
                       WHERE term_entity_id = ?""",
                    (
                        status,
                        failures,
                        eligible,
                        str(error).strip()[:2000],
                        timestamp,
                        term_entity_id,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def complete_lexical_projection_repair(
        self,
        term_entity_id: str,
        fingerprint: str,
        *,
        compose_job_id: str = "",
    ) -> bool:
        """Record successful publication without losing newer dirtiness."""

        term_entity_id = term_entity_id.strip()
        fingerprint = fingerprint.strip()
        if not term_entity_id or not fingerprint:
            raise ValueError("completed projection needs a subject and fingerprint")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """SELECT desired_fingerprint, compose_job_id
                       FROM lexical_projection_repairs
                       WHERE term_entity_id = ?""",
                    (term_entity_id,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return False
                if (
                    compose_job_id
                    and str(row["compose_job_id"])
                    and str(row["compose_job_id"]) != compose_job_id.strip()
                ):
                    connection.commit()
                    return False
                timestamp = _now()
                current = str(row["desired_fingerprint"]) == fingerprint
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET applied_fingerprint = ?, status = ?,
                           compose_job_id = NULL, bound_fingerprint = '',
                           lease_token = '', lease_expires_at = '',
                           failure_count = 0, next_eligible_at = '',
                           last_error = '', last_applied_at = ?, updated_at = ?
                       WHERE term_entity_id = ?""",
                    (
                        fingerprint,
                        "applied" if current else "pending",
                        timestamp,
                        timestamp,
                        term_entity_id,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def lexical_missing_projection_candidate(
        self,
        visible_views: Iterable[dict[str, Any]],
        *,
        prompt_prefix: str,
        limit: int = 64,
    ) -> dict[str, Any] | None:
        """Find a visible word whose accepted components lack a card mode."""

        modes_by_subject: dict[str, set[str]] = {}
        for view in visible_views:
            subject_id = str(view.get("subject_entity_id", "")).strip()
            mode = str(view.get("mode", "")).strip()
            if subject_id and mode:
                modes_by_subject.setdefault(subject_id, set()).add(mode)
        candidates: list[dict[str, Any]] = []
        for subject_id in sorted(modes_by_subject)[: max(1, int(limit))]:
            components = self.accepted_morphology_components(subject_id)
            expected_modes = {
                "root" if item["kind"] == "root" else "affix"
                for item in components
            }
            missing_modes = sorted(expected_modes - modes_by_subject[subject_id])
            if not missing_modes:
                continue
            record = self.term_record(subject_id)
            artifacts = self.artifacts_for_subject(
                f"term:{subject_id}", validation_state="accepted"
            )
            stages = {str(item.get("stage", "")) for item in artifacts}
            recomposable = {
                "accepted-meaning",
                "accepted-morpheme-split",
                "accepted-origin-branches",
            }.issubset(stages)
            projection_payload = [
                {
                    "assertion_id": item["assertion_id"],
                    "revision": item["revision"],
                    "morpheme_id": item["morpheme_id"],
                    "kind": item["kind"],
                    "canonical_form": item["canonical_form"],
                }
                for item in components
            ]
            projection_fingerprint = hashlib.sha256(
                json.dumps(
                    projection_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:16]
            with closing(self._connect()) as connection:
                revision_row = connection.execute(
                    """SELECT value FROM schema_meta
                       WHERE key = 'relation_graph_revision'"""
                ).fetchone()
            graph_revision = int(revision_row[0]) if revision_row else 0
            candidates.append(
                {
                    "candidate_kind": "missing-morphology-projection",
                    "subject_entity_id": subject_id,
                    "term": str(record.get("text", "")),
                    "language": str(record.get("language", "en")),
                    "missing_modes": missing_modes,
                    "components": components,
                    "recomposable": recomposable,
                    "graph_revision": graph_revision,
                    "projection_fingerprint": projection_fingerprint,
                    "prompt_version": (
                        f"{prompt_prefix}-projection-g{graph_revision}-"
                        f"p{projection_fingerprint}"
                    ),
                }
            )
        candidates.sort(
            key=lambda item: (
                not bool(item["recomposable"]),
                str(item["language"]),
                str(item["term"]).casefold(),
            )
        )
        return candidates[0] if candidates else None

    def morphology_source_outcomes(
        self, mode: str, corpus_id: str, source_fingerprint: str
    ) -> dict[str, dict[str, Any]]:
        """Load durable terminal/cooldown decisions for one corpus revision."""

        prefix = "morphology_source_outcome:"
        outcomes: dict[str, dict[str, Any]] = {}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT key, value FROM schema_meta WHERE key LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row[1]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if (
                str(payload.get("mode", "")) != mode
                or str(payload.get("corpus_id", "")) != corpus_id
                or str(payload.get("source_fingerprint", "")) != source_fingerprint
            ):
                continue
            entry_id = str(payload.get("entry_id", "")).strip()
            if entry_id:
                outcomes[entry_id] = payload
        return outcomes

    def record_morphology_source_outcome(
        self,
        mode: str,
        corpus_id: str,
        entry_id: str,
        source_fingerprint: str,
        *,
        status: str,
        next_eligible_epoch: int = 0,
    ) -> None:
        """Persist one source result without introducing a dedicated table."""

        identity = "\x1f".join((mode, corpus_id, entry_id))
        key = "morphology_source_outcome:" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        payload = json.dumps(
            {
                "mode": mode,
                "corpus_id": corpus_id,
                "entry_id": entry_id,
                "source_fingerprint": source_fingerprint,
                "status": status,
                "next_eligible_epoch": max(0, int(next_eligible_epoch)),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO schema_meta(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, payload),
            )
            connection.commit()

    def lexical_plan_epoch_count(self) -> int:
        """Count durable full lexical plan epochs, including later revisits."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(*) FROM (
                       SELECT DISTINCT subject_key, prompt_version
                       FROM preparation_jobs
                       WHERE job_type = 'compose-origin-card'
                   )"""
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _canonical_fingerprint_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value

    def _term_local_revisit_fingerprint(
        self,
        connection: sqlite3.Connection,
        term_entity_id: str,
        *,
        max_entities: int = 192,
    ) -> tuple[str, dict[str, int]]:
        """Hash a bounded two-hop projection plus its retrieval provenance."""

        local_ids = {term_entity_id}
        frontier = {term_entity_id}
        edge_rows: dict[str, sqlite3.Row] = {}
        assertion_rows: dict[str, sqlite3.Row] = {}
        for _depth in range(2):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            parameters = tuple(sorted(frontier))
            for row in connection.execute(
                f"""SELECT edge_id, source_entity_id, target_entity_id,
                           relation, basis, confidence, properties
                    FROM entity_edges
                    WHERE status = 'accepted'
                      AND (source_entity_id IN ({placeholders})
                        OR target_entity_id IN ({placeholders}))
                    ORDER BY relation, source_entity_id, target_entity_id, edge_id""",
                (*parameters, *parameters),
            ):
                edge_rows[str(row["edge_id"])] = row
            for row in connection.execute(
                f"""SELECT assertion_id, subject_entity_id, source_entity_id,
                           target_entity_id, relation, basis, confidence,
                           properties_json, revision
                    FROM relation_assertions
                    WHERE status = 'accepted'
                      AND (subject_entity_id IN ({placeholders})
                        OR source_entity_id IN ({placeholders})
                        OR target_entity_id IN ({placeholders}))
                    ORDER BY relation, source_entity_id, target_entity_id,
                             subject_entity_id, assertion_id""",
                (*parameters, *parameters, *parameters),
            ):
                assertion_rows[str(row["assertion_id"])] = row
            neighboring_ids = {
                str(row[column])
                for row in (*edge_rows.values(), *assertion_rows.values())
                for column in (
                    "source_entity_id", "target_entity_id", "subject_entity_id"
                )
                if column in row.keys() and str(row[column]).strip()
            }
            available = max(0, max_entities - len(local_ids))
            additions = set(sorted(neighboring_ids - local_ids)[:available])
            local_ids.update(additions)
            frontier = additions

        identifiers = tuple(sorted(local_ids))
        placeholders = ",".join("?" for _ in identifiers)
        nodes = [
            {
                "id": str(row["entity_id"]),
                "type": str(row["entity_type"]),
                "key": str(row["canonical_key"]),
                "label": str(row["label"]),
                "quality": row["quality_score"],
                "payload": self._canonical_fingerprint_value(row["payload"]),
            }
            for row in connection.execute(
                f"""SELECT entity_id, entity_type, canonical_key, label,
                           quality_score, payload
                    FROM entities WHERE status = 'accepted'
                      AND entity_id IN ({placeholders})
                    ORDER BY entity_id""",
                identifiers,
            )
        ]
        edges = [
            {
                "id": str(row["edge_id"]),
                "source": str(row["source_entity_id"]),
                "target": str(row["target_entity_id"]),
                "relation": str(row["relation"]),
                "basis": str(row["basis"]),
                "confidence": float(row["confidence"]),
                "properties": self._canonical_fingerprint_value(row["properties"]),
            }
            for row in sorted(edge_rows.values(), key=lambda item: str(item["edge_id"]))
            if str(row["source_entity_id"]) in local_ids
            and str(row["target_entity_id"]) in local_ids
        ]
        assertions = [
            {
                "id": str(row["assertion_id"]),
                "subject": str(row["subject_entity_id"]),
                "source": str(row["source_entity_id"]),
                "target": str(row["target_entity_id"]),
                "relation": str(row["relation"]),
                "basis": str(row["basis"]),
                "confidence": float(row["confidence"]),
                "revision": int(row["revision"]),
                "properties": self._canonical_fingerprint_value(
                    row["properties_json"]
                ),
            }
            for row in sorted(
                assertion_rows.values(), key=lambda item: str(item["assertion_id"])
            )
            if str(row["subject_entity_id"]) in local_ids
            and str(row["source_entity_id"]) in local_ids
            and str(row["target_entity_id"]) in local_ids
        ]
        assertion_ids = tuple(item["id"] for item in assertions)
        components = [
            {
                "term": str(row["term_id"]),
                "morpheme": str(row["morpheme_id"]),
                "ordinal": int(row["ordinal"]),
                "surface": str(row["surface"]),
                "confidence": float(row["confidence"]),
                "basis": str(row["basis"]),
            }
            for row in connection.execute(
                f"""SELECT term_id, morpheme_id, ordinal, surface,
                           confidence, basis
                    FROM term_morphemes
                    WHERE term_id IN ({placeholders})
                       OR morpheme_id IN ({placeholders})
                    ORDER BY term_id, ordinal, morpheme_id""",
                (*identifiers, *identifiers),
            )
        ]
        entity_evidence = [
            {
                "entity": str(row["entity_id"]),
                "claim": str(row["claim"]),
                "corpus": str(row["corpus_id"]),
                "entry": str(row["source_entry_id"]),
                "hash": str(row["source_hash"]),
                "locator": str(row["locator"]),
            }
            for row in connection.execute(
                f"""SELECT link.entity_id, link.claim, evidence.corpus_id,
                           evidence.source_entry_id, evidence.source_hash,
                           evidence.locator
                    FROM entity_evidence AS link
                    JOIN evidence_records AS evidence
                      ON evidence.evidence_id = link.evidence_id
                    WHERE link.entity_id IN ({placeholders})
                    ORDER BY link.entity_id, evidence.corpus_id,
                             evidence.source_entry_id, evidence.source_hash,
                             evidence.locator, link.claim""",
                identifiers,
            )
        ]
        assertion_evidence: list[dict[str, str]] = []
        if assertion_ids:
            assertion_placeholders = ",".join("?" for _ in assertion_ids)
            assertion_evidence = [
                {
                    "assertion": str(row["assertion_id"]),
                    "corpus": str(row["corpus_id"]),
                    "entry": str(row["source_entry_id"]),
                    "hash": str(row["source_hash"]),
                    "locator": str(row["locator"]),
                }
                for row in connection.execute(
                    f"""SELECT link.assertion_id, evidence.corpus_id,
                               evidence.source_entry_id, evidence.source_hash,
                               evidence.locator
                        FROM assertion_evidence AS link
                        JOIN evidence_records AS evidence
                          ON evidence.evidence_id = link.evidence_id
                        WHERE link.assertion_id IN ({assertion_placeholders})
                        ORDER BY link.assertion_id, evidence.corpus_id,
                                 evidence.source_entry_id, evidence.source_hash,
                                 evidence.locator""",
                    assertion_ids,
                )
            ]
        snapshot = {
            "subject": term_entity_id,
            "projection": {
                "nodes": nodes,
                "edges": edges,
                "assertions": assertions,
                "components": components,
            },
            "evidence_sources": {
                "entities": entity_evidence,
                "assertions": assertion_evidence,
            },
        }
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), {
            "nodes": len(nodes),
            "edges": len(edges) + len(assertions) + len(components),
            "evidence": len(entity_evidence) + len(assertion_evidence),
        }

    def record_lexical_discovery_observations(
        self,
        observations: Iterable[dict[str, Any]],
        *,
        context_key: str = "",
    ) -> int:
        """Persist repeatable source observations without reopening first discovery."""

        prepared: list[dict[str, Any]] = []
        for raw in observations:
            language = _language(str(raw.get("language", "en")))
            term = unicodedata.normalize("NFKC", str(raw.get("term", ""))).strip()
            normalized = _normalise(term)
            if not normalized:
                continue
            identity = {
                "context": context_key,
                "language": language,
                "normalized": normalized,
                "source_kind": str(raw.get("source_kind", "unknown")).strip(),
                "source_entity_id": str(raw.get("source_entity_id", "")).strip(),
                "source_artifact_id": str(raw.get("source_artifact_id", "")).strip(),
                "source_entry_id": str(raw.get("source_entry_id", "")).strip(),
                "source_fingerprint": str(raw.get("source_fingerprint", "")).strip(),
                "graph_fingerprint": str(raw.get("graph_fingerprint", "")).strip(),
            }
            idempotency_key = str(raw.get("idempotency_key", "")).strip()
            if not idempotency_key:
                idempotency_key = "source:" + hashlib.sha256(
                    json.dumps(identity, sort_keys=True).encode("utf-8")
                ).hexdigest()
            prepared.append({**identity, "term": term, "idempotency_key": idempotency_key})
        if not prepared:
            return 0
        inserted = 0
        timestamp = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in prepared:
                term_row = connection.execute(
                    """SELECT entity_id FROM terms
                       WHERE language = ? AND normalized = ? AND kind = 'word'""",
                    (item["language"], item["normalized"]),
                ).fetchone()
                discovery_row = connection.execute(
                    """SELECT discovery_id FROM lexical_discoveries
                       WHERE language = ? AND normalized = ?""",
                    (item["language"], item["normalized"]),
                ).fetchone()
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO lexical_discovery_observations(
                           observation_id, idempotency_key, discovery_id,
                           term_entity_id, language, term, normalized,
                           source_kind, source_entity_id, source_artifact_id,
                           source_entry_id, source_fingerprint,
                           graph_fingerprint, metadata_json, observed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _identifier("lexical-observation", item["idempotency_key"]),
                        item["idempotency_key"],
                        discovery_row["discovery_id"] if discovery_row else None,
                        term_row["entity_id"] if term_row else None,
                        item["language"],
                        item["term"],
                        item["normalized"],
                        item["source_kind"] or "unknown",
                        item["source_entity_id"],
                        item["source_artifact_id"],
                        item["source_entry_id"],
                        item["source_fingerprint"],
                        item["graph_fingerprint"],
                        "{}",
                        timestamp,
                    ),
                )
                inserted += int(cursor.rowcount)
            connection.commit()
        return inserted

    def quarantine_lexical_discovery(
        self,
        discovery: dict[str, Any],
        *,
        reason: str,
        context_key: str = "",
    ) -> str:
        """Skip an unschedulable frontier while retaining its exact provenance."""

        discovery_id = str(discovery.get("discovery_id", "")).strip()
        language = unicodedata.normalize(
            "NFKC", str(discovery.get("language", ""))
        ).strip().casefold() or "und"
        term = unicodedata.normalize(
            "NFKC", str(discovery.get("term", discovery.get("text", "")))
        ).strip()
        normalized = _normalise(term)
        reason = re.sub(r"\s+", " ", str(reason)).strip()[:500]
        if not normalized:
            raise ValueError("a quarantined lexical discovery needs a term")
        if not reason:
            raise ValueError("a quarantined lexical discovery needs a reason")
        identity = {
            "context": context_key.strip(),
            "discovery_id": discovery_id,
            "language": language,
            "normalized": normalized,
            "source_kind": str(discovery.get("source_kind", "unknown")).strip(),
            "source_entity_id": str(
                discovery.get("source_entity_id", "")
            ).strip(),
            "source_artifact_id": str(
                discovery.get("source_artifact_id", "")
            ).strip(),
            "source_entry_id": str(discovery.get("source_entry_id", "")).strip(),
            "reason": reason,
        }
        idempotency_key = "quarantine:" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
        observation_id = _identifier("lexical-observation", idempotency_key)
        timestamp = _now()
        metadata_json = json.dumps(
            {
                "status": "quarantined",
                "reason": reason,
                "original_language": language,
                "original_source_kind": identity["source_kind"],
                "context": identity["context"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            discovery_row = None
            if discovery_id:
                discovery_row = connection.execute(
                    """SELECT discovery_id, round_id FROM lexical_discoveries
                       WHERE discovery_id = ?""",
                    (discovery_id,),
                ).fetchone()
                if discovery_row is None:
                    connection.rollback()
                    raise ValueError(
                        f"unknown lexical discovery: {discovery_id!r}"
                    )
                connection.execute(
                    """UPDATE lexical_discoveries SET planned_at = ?
                       WHERE discovery_id = ? AND planned_at = ''""",
                    (timestamp, discovery_id),
                )
                connection.execute(
                    """UPDATE lexical_discovery_rounds
                       SET status = 'planned', updated_at = ?
                       WHERE round_id = ? AND NOT EXISTS (
                           SELECT 1 FROM lexical_discoveries
                           WHERE round_id = ? AND planned_at = ''
                       )""",
                    (
                        timestamp,
                        discovery_row["round_id"],
                        discovery_row["round_id"],
                    ),
                )
            term_row = connection.execute(
                """SELECT entity_id FROM terms
                   WHERE language = ? AND normalized = ? AND kind = 'word'""",
                (language, normalized),
            ).fetchone()
            connection.execute(
                """INSERT OR IGNORE INTO lexical_discovery_observations(
                       observation_id, idempotency_key, discovery_id,
                       term_entity_id, language, term, normalized,
                       source_kind, source_entity_id, source_artifact_id,
                       source_entry_id, source_fingerprint,
                       graph_fingerprint, metadata_json, observed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'quarantined-discovery',
                             ?, ?, ?, ?, '', ?, ?)""",
                (
                    observation_id,
                    idempotency_key,
                    discovery_id or None,
                    term_row["entity_id"] if term_row else None,
                    language,
                    term,
                    normalized,
                    identity["source_entity_id"],
                    identity["source_artifact_id"],
                    identity["source_entry_id"],
                    identity["context"],
                    metadata_json,
                    timestamp,
                ),
            )
            connection.commit()
        return observation_id

    @staticmethod
    def _revisit_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    def claim_lexical_revisit_candidate(
        self,
        language: str | None = "en",
        *,
        prompt_prefix: str = "autonomous-lexical",
        scan_limit: int = 256,
        base_cooldown_seconds: int = 6 * 60 * 60,
        max_cooldown_seconds: int = 30 * 24 * 60 * 60,
        lease_seconds: int = 30 * 60,
        history_only: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically lease one changed or cooldown-eligible lexical subject."""

        scan_limit = max(1, min(int(scan_limit), 2048))
        base_cooldown_seconds = max(60, int(base_cooldown_seconds))
        max_cooldown_seconds = max(base_cooldown_seconds, int(max_cooldown_seconds))
        lease_seconds = max(60, int(lease_seconds))
        language_clause = ""
        parameters: list[Any] = [int(history_only), int(history_only)]
        if language is not None and str(language).strip():
            language_clause = " AND term.language = ?"
            parameters.append(_language(str(language)))
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """SELECT epoch_id, term_entity_id FROM lexical_revisit_epochs
                   WHERE status = 'claimed' AND lease_expires_at <= ?""",
                (timestamp,),
            ).fetchall()
            for row in expired:
                connection.execute(
                    """UPDATE lexical_revisit_epochs
                       SET status = 'expired', completed_at = ?,
                           result_summary = 'claim lease expired before planning'
                       WHERE epoch_id = ? AND status = 'claimed'""",
                    (timestamp, row["epoch_id"]),
                )
                connection.execute(
                    """UPDATE lexical_revisit_state
                       SET active_epoch_id = '', lease_expires_at = '',
                           next_eligible_at = ?, updated_at = ?
                       WHERE term_entity_id = ? AND active_epoch_id = ?""",
                    (
                        (now + timedelta(seconds=base_cooldown_seconds)).isoformat(),
                        timestamp,
                        row["term_entity_id"],
                        row["epoch_id"],
                    ),
                )
            rows = connection.execute(
                """SELECT term.entity_id AS term_id, term.language, term.text AS term,
                          term.normalized, state.last_observed_fingerprint,
                          state.last_completed_fingerprint, state.last_observed_at,
                          state.last_completed_at, state.next_eligible_at,
                           state.revisit_count, state.unchanged_count,
                           state.active_epoch_id,
                           EXISTS (
                               SELECT 1 FROM job_artifacts AS history
                               JOIN preparation_jobs AS history_job
                                 ON history_job.job_id = history.job_id
                               WHERE history_job.subject_entity_id = term.entity_id
                                 AND history.stage = 'accepted-origin-branches'
                                 AND history.validation_state = 'accepted'
                           ) AS has_origin_checkpoint,
                          COALESCE((SELECT MAX(job.updated_at)
                           FROM preparation_jobs AS job
                           WHERE job.subject_entity_id = term.entity_id), '')
                              AS last_prepared_at
                   FROM terms AS term
                   JOIN entities AS entity ON entity.entity_id = term.entity_id
                   LEFT JOIN lexical_revisit_state AS state
                     ON state.term_entity_id = term.entity_id
                   WHERE term.kind = 'word' AND entity.status = 'accepted'
                      AND (EXISTS (
                          SELECT 1 FROM preparation_jobs AS planned
                          WHERE planned.subject_entity_id = term.entity_id
                      ) OR (? AND EXISTS (
                          SELECT 1 FROM entity_edges AS component
                          WHERE component.source_entity_id = term.entity_id
                            AND component.relation = 'has-component'
                            AND component.status = 'accepted'
                      )))
                      AND NOT EXISTS (
                          SELECT 1 FROM preparation_jobs AS active
                          WHERE active.subject_entity_id = term.entity_id
                            AND active.status IN ('queued', 'running')
                            AND (? = 0 OR active.job_type IN (
                                'retrieve-evidence', 'prepare-meaning',
                                'split-morphemes', 'expand-origin-branches',
                                'compose-origin-card'
                            ))
                     )
                     AND COALESCE(state.active_epoch_id, '') = ''"""
                + language_clause
                + """ ORDER BY COALESCE(state.last_observed_at, ''),
                                  COALESCE(state.next_eligible_at, ''),
                                  term.language, term.normalized
                       LIMIT ?""",
                (*parameters, scan_limit),
            ).fetchall()
            eligible: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                fingerprint, metrics = self._term_local_revisit_fingerprint(
                    connection, str(item["term_id"])
                )
                completed_fingerprint = str(
                    item.get("last_completed_fingerprint") or ""
                )
                revisit_count = int(item.get("revisit_count") or 0)
                unchanged_count = int(item.get("unchanged_count") or 0)
                if not completed_fingerprint:
                    completed_fingerprint = fingerprint
                    last_completed_at = (
                        str(item.get("last_prepared_at") or "") or timestamp
                    )
                    baseline = self._revisit_datetime(last_completed_at) or now
                    next_eligible_at = (
                        baseline + timedelta(seconds=base_cooldown_seconds)
                    ).isoformat()
                    if history_only and not item["has_origin_checkpoint"]:
                        # A first historical analysis is new work, not a repeat
                        # that must wait behind unrelated translation updates.
                        next_eligible_at = timestamp
                    connection.execute(
                        """INSERT INTO lexical_revisit_state(
                               term_entity_id, last_observed_fingerprint,
                               last_completed_fingerprint, last_observed_at,
                               last_completed_at, next_eligible_at,
                               revisit_count, unchanged_count, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(term_entity_id) DO UPDATE SET
                               last_observed_fingerprint = excluded.last_observed_fingerprint,
                               last_observed_at = excluded.last_observed_at,
                               updated_at = excluded.updated_at""",
                        (
                            item["term_id"], fingerprint, fingerprint, timestamp,
                            last_completed_at, next_eligible_at, revisit_count,
                            unchanged_count, timestamp,
                        ),
                    )
                else:
                    next_eligible_at = str(item.get("next_eligible_at") or "")
                    connection.execute(
                        """UPDATE lexical_revisit_state
                           SET last_observed_fingerprint = ?, last_observed_at = ?,
                               updated_at = ? WHERE term_entity_id = ?""",
                        (fingerprint, timestamp, timestamp, item["term_id"]),
                    )
                changed = fingerprint != completed_fingerprint
                due_at = self._revisit_datetime(next_eligible_at)
                due = due_at is None or due_at <= now
                if not changed and not due:
                    continue
                eligible.append(
                    {
                        **item,
                        **metrics,
                        "fingerprint": fingerprint,
                        "changed": changed,
                        "next_eligible_at": next_eligible_at,
                        "revisit_count": revisit_count,
                        "unchanged_count": unchanged_count,
                    }
                )
            if not eligible:
                connection.commit()
                return None
            eligible.sort(
                key=lambda item: (
                    not bool(item["changed"]),
                    int(item["revisit_count"]),
                    str(item["next_eligible_at"]),
                    -int(item["evidence"]),
                    -int(item["edges"]),
                    str(item["normalized"]),
                )
            )
            selected = eligible[0]
            epoch_number = int(selected["revisit_count"]) + 1
            epoch_key = f"{selected['term_id']}:{epoch_number}:{selected['fingerprint']}"
            epoch_id = _identifier("lexical-revisit", epoch_key)
            prompt_version = (
                f"{prompt_prefix}-revisit-e{epoch_number}-"
                f"{str(selected['fingerprint'])[:12]}"
            )
            lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
            novelty_kind = "graph-changed" if selected["changed"] else "cooldown"
            connection.execute(
                """INSERT INTO lexical_revisit_epochs(
                       epoch_id, term_entity_id, epoch_number, prompt_version,
                       status, novelty_kind, fingerprint_before, claimed_at,
                       lease_expires_at
                   ) VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?, ?)""",
                (
                    epoch_id, selected["term_id"], epoch_number, prompt_version,
                    novelty_kind, selected["fingerprint"], timestamp,
                    lease_expires_at,
                ),
            )
            connection.execute(
                """UPDATE lexical_revisit_state
                   SET last_claimed_at = ?, revisit_count = ?,
                       active_epoch_id = ?, lease_expires_at = ?, updated_at = ?
                   WHERE term_entity_id = ?""",
                (
                    timestamp, epoch_number, epoch_id, lease_expires_at,
                    timestamp, selected["term_id"],
                ),
            )
            observation_key = f"revisit-claim:{epoch_id}"
            connection.execute(
                """INSERT OR IGNORE INTO lexical_discovery_observations(
                       observation_id, idempotency_key, term_entity_id,
                       language, term, normalized, source_kind,
                       source_fingerprint, graph_fingerprint, metadata_json,
                       observed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'revisit-claim', ?, ?, ?, ?)""",
                (
                    _identifier("lexical-observation", observation_key),
                    observation_key, selected["term_id"], selected["language"],
                    selected["term"], selected["normalized"], prompt_version,
                    selected["fingerprint"],
                    json.dumps(metrics, sort_keys=True), timestamp,
                ),
            )
            connection.commit()
            return {
                **selected,
                "epoch_id": epoch_id,
                "epoch_number": epoch_number,
                "prompt_version": prompt_version,
                "novelty_kind": novelty_kind,
                "lease_expires_at": lease_expires_at,
            }

    def bind_lexical_revisit_epoch(
        self, epoch_id: str, *, job_subject_key: str
    ) -> bool:
        """Atomically bind a leased epoch to its deterministic job family."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE lexical_revisit_epochs
                   SET status = 'planned', job_subject_key = ?
                   WHERE epoch_id = ? AND status = 'claimed'""",
                (job_subject_key.strip(), epoch_id),
            )
            connection.commit()
        return bool(cursor.rowcount)

    def complete_lexical_revisit_epoch(
        self,
        epoch_id: str,
        *,
        outcome: str = "complete",
        result_summary: str = "",
        base_cooldown_seconds: int = 6 * 60 * 60,
        max_cooldown_seconds: int = 30 * 24 * 60 * 60,
    ) -> dict[str, Any] | None:
        """Atomically close one epoch and apply its bounded cooldown."""

        if outcome not in {"complete", "failed", "no-work"}:
            raise ValueError(f"invalid lexical revisit outcome: {outcome!r}")
        base_cooldown_seconds = max(60, int(base_cooldown_seconds))
        max_cooldown_seconds = max(base_cooldown_seconds, int(max_cooldown_seconds))
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT epoch.*, state.unchanged_count
                   FROM lexical_revisit_epochs AS epoch
                   JOIN lexical_revisit_state AS state
                     ON state.term_entity_id = epoch.term_entity_id
                   WHERE epoch.epoch_id = ?""",
                (epoch_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if str(row["status"]) in {"complete", "failed", "expired"}:
                connection.commit()
                return dict(row)
            fingerprint_after, metrics = self._term_local_revisit_fingerprint(
                connection, str(row["term_entity_id"])
            )
            changed = fingerprint_after != str(row["fingerprint_before"])
            previous_unchanged = int(row["unchanged_count"])
            unchanged_count = 0 if changed else previous_unchanged + 1
            exponent = max(0, min(unchanged_count - 1, 12))
            cooldown_seconds = min(
                max_cooldown_seconds,
                base_cooldown_seconds * (2 ** exponent),
            )
            next_eligible_at = (now + timedelta(seconds=cooldown_seconds)).isoformat()
            terminal_status = "failed" if outcome == "failed" else "complete"
            summary = result_summary.strip() or (
                "local lexical projection changed"
                if changed
                else "local lexical projection unchanged"
            )
            connection.execute(
                """UPDATE lexical_revisit_epochs
                   SET status = ?, fingerprint_after = ?, result_summary = ?,
                       completed_at = ? WHERE epoch_id = ?
                     AND status IN ('claimed', 'planned')""",
                (terminal_status, fingerprint_after, summary[:500], timestamp, epoch_id),
            )
            connection.execute(
                """UPDATE lexical_revisit_state
                   SET last_observed_fingerprint = ?,
                       last_completed_fingerprint = ?, last_observed_at = ?,
                       last_completed_at = ?, next_eligible_at = ?,
                       unchanged_count = ?, active_epoch_id = '',
                       lease_expires_at = '', updated_at = ?
                   WHERE term_entity_id = ? AND active_epoch_id = ?""",
                (
                    fingerprint_after, fingerprint_after, timestamp, timestamp,
                    next_eligible_at, unchanged_count, timestamp,
                    row["term_entity_id"], epoch_id,
                ),
            )
            term = connection.execute(
                """SELECT language, text, normalized FROM terms
                   WHERE entity_id = ?""",
                (row["term_entity_id"],),
            ).fetchone()
            if term is not None:
                observation_key = f"revisit-complete:{epoch_id}"
                connection.execute(
                    """INSERT OR IGNORE INTO lexical_discovery_observations(
                           observation_id, idempotency_key, term_entity_id,
                           language, term, normalized, source_kind,
                           source_fingerprint, graph_fingerprint, metadata_json,
                           observed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'revisit-complete', ?, ?, ?, ?)""",
                    (
                        _identifier("lexical-observation", observation_key),
                        observation_key, row["term_entity_id"], term["language"],
                        term["text"], term["normalized"], row["prompt_version"],
                        fingerprint_after,
                        json.dumps(
                            {
                                **metrics,
                                "changed": changed,
                                "outcome": outcome,
                                "cooldown_seconds": cooldown_seconds,
                            },
                            sort_keys=True,
                        ),
                        timestamp,
                    ),
                )
            connection.commit()
            return {
                "epoch_id": epoch_id,
                "status": terminal_status,
                "changed": changed,
                "fingerprint_after": fingerprint_after,
                "unchanged_count": unchanged_count,
                "cooldown_seconds": cooldown_seconds,
                "next_eligible_at": next_eligible_at,
            }

    def reconcile_lexical_revisit_epochs(self, limit: int = 32) -> list[dict[str, Any]]:
        """Finish planned epochs once their complete job family is terminal."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT epoch_id, term_entity_id, prompt_version,
                          (SELECT COUNT(*) FROM preparation_jobs AS job
                           WHERE job.subject_entity_id = epoch.term_entity_id
                             AND job.prompt_version = epoch.prompt_version) AS jobs,
                          (SELECT COUNT(*) FROM preparation_jobs AS job
                           WHERE job.subject_entity_id = epoch.term_entity_id
                             AND job.prompt_version = epoch.prompt_version
                             AND job.status IN ('queued', 'running')) AS active_jobs,
                          (SELECT COUNT(*) FROM preparation_jobs AS job
                           WHERE job.subject_entity_id = epoch.term_entity_id
                             AND job.prompt_version = epoch.prompt_version
                             AND job.status = 'complete') AS complete_jobs
                   FROM lexical_revisit_epochs AS epoch
                   WHERE epoch.status = 'planned'
                   ORDER BY epoch.claimed_at LIMIT ?""",
                (max(1, min(int(limit), 256)),),
            ).fetchall()
        completed: list[dict[str, Any]] = []
        for row in rows:
            if int(row["active_jobs"]):
                continue
            jobs = int(row["jobs"])
            complete_jobs = int(row["complete_jobs"])
            outcome = "complete" if jobs and complete_jobs else (
                "failed" if jobs else "no-work"
            )
            result = self.complete_lexical_revisit_epoch(
                str(row["epoch_id"]),
                outcome=outcome,
                result_summary=(
                    f"{complete_jobs}/{jobs} deterministic revisit jobs completed"
                ),
            )
            if result is not None:
                completed.append(result)
        return completed

    def active_lexical_revisit_subject_keys(self) -> set[str]:
        """Return authoritative active, incomplete revisit subjects."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT 'term:' || epoch.term_entity_id AS subject_key
                   FROM lexical_revisit_epochs AS epoch
                   JOIN lexical_revisit_state AS state
                     ON state.term_entity_id = epoch.term_entity_id
                    AND state.active_epoch_id = epoch.epoch_id
                   WHERE epoch.status IN ('claimed', 'planned')"""
            ).fetchall()
        return {str(row["subject_key"]) for row in rows}

    def lexical_revisit_candidate(
        self,
        language: str | None = "en",
        *,
        prompt_prefix: str = "autonomous-lexical",
    ) -> dict[str, Any] | None:
        """Choose one old term using novelty and a durable exponential cooldown."""

        language_filter = ""
        parameters: list[Any] = [f"{prompt_prefix}-revisit-%"]
        if language is not None and str(language).strip():
            language_filter = " AND term.language = ?"
            parameters.append(_language(str(language)))
        revisit_pattern = f"{prompt_prefix}-revisit-%"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT term.entity_id AS term_id, term.language AS language,
                          term.text AS term,
                          term.normalized,
                          (SELECT COUNT(DISTINCT job.prompt_version)
                           FROM preparation_jobs AS job
                           WHERE job.subject_entity_id = term.entity_id
                             AND job.prompt_version LIKE ?) AS revisit_count,
                          COALESCE((SELECT MAX(job.updated_at)
                           FROM preparation_jobs AS job
                           WHERE job.subject_entity_id = term.entity_id), '')
                              AS last_prepared_at,
                          COALESCE((SELECT MAX(updated_at) FROM (
                           SELECT edge.updated_at AS updated_at
                           FROM entity_edges AS edge
                           WHERE edge.status = 'accepted'
                             AND (edge.source_entity_id = term.entity_id
                               OR edge.target_entity_id = term.entity_id)
                           UNION ALL
                           SELECT assertion.updated_at AS updated_at
                           FROM relation_assertions AS assertion
                           WHERE assertion.status = 'accepted'
                             AND (assertion.subject_entity_id = term.entity_id
                               OR assertion.source_entity_id = term.entity_id
                               OR assertion.target_entity_id = term.entity_id)
                          )), '')
                              AS last_connection_at,
                          ((SELECT COUNT(*) FROM entity_edges AS edge
                           WHERE edge.status = 'accepted'
                             AND (edge.source_entity_id = term.entity_id
                               OR edge.target_entity_id = term.entity_id)) +
                           (SELECT COUNT(*) FROM relation_assertions AS assertion
                            WHERE assertion.status = 'accepted'
                              AND (assertion.subject_entity_id = term.entity_id
                                OR assertion.source_entity_id = term.entity_id
                                OR assertion.target_entity_id = term.entity_id)))
                              AS connection_count,
                          (SELECT COUNT(*) FROM preparation_jobs AS active
                           WHERE active.subject_entity_id = term.entity_id
                             AND active.status IN ('queued', 'running'))
                              AS active_jobs
                   FROM terms AS term
                   JOIN entities AS entity ON entity.entity_id = term.entity_id
                   WHERE term.kind = 'word'
                     AND entity.status = 'accepted'
                     AND EXISTS (
                         SELECT 1 FROM preparation_jobs AS planned
                         WHERE planned.subject_entity_id = term.entity_id
                     )""" + language_filter,
                parameters,
            ).fetchall()

        now = datetime.now(UTC)
        eligible: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if int(item["active_jobs"]):
                continue
            revisits = int(item["revisit_count"])
            cooldown_hours = min(24 * 30, 6 * (2 ** min(revisits, 7)))
            last_text = str(item["last_prepared_at"])
            connection_text = str(item["last_connection_at"])
            novel = bool(connection_text and connection_text > last_text)
            try:
                last = datetime.fromisoformat(last_text)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                age_hours = (now - last).total_seconds() / 3600
            except (TypeError, ValueError):
                age_hours = float("inf")
            if not novel and age_hours < cooldown_hours:
                continue
            item.update(
                {
                    "novel_connections": novel,
                    "cooldown_hours": cooldown_hours,
                    "age_hours": age_hours,
                }
            )
            eligible.append(item)
        eligible.sort(
            key=lambda item: (
                not bool(item["novel_connections"]),
                int(item["revisit_count"]),
                str(item["last_prepared_at"]),
                -int(item["connection_count"]),
                str(item["normalized"]),
            )
        )
        return eligible[0] if eligible else None

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
            job = connection.execute(
                """SELECT subject_entity_id, subject_key
                   FROM preparation_jobs WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
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
            if (
                job is not None
                and job["subject_entity_id"]
                and stage.strip() in _PROJECTION_REQUIRED_ARTIFACT_STAGES
            ):
                self._refresh_projection_repair_state(
                    connection, str(job["subject_entity_id"])
                )
            connection.commit()
        return artifact_id

    def finish_job(self, job_id: str, *, error: str = "") -> None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT attempts, max_attempts, job_type
                   FROM preparation_jobs WHERE job_id = ?""",
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
                if str(row["job_type"]) == "enrich-morphology-v3":
                    cluster = connection.execute(
                        """SELECT cluster_id, failure_count
                           FROM morphology_enrichment_clusters_v3
                           WHERE last_job_id = ?""",
                        (job_id,),
                    ).fetchone()
                    if cluster is not None:
                        failures = int(cluster["failure_count"]) + 1
                        hours = min(24 * 30, 6 * (2 ** min(failures - 1, 7)))
                        timestamp = _now()
                        eligible = (
                            datetime.now(UTC) + timedelta(hours=hours)
                        ).isoformat()
                        outcome = json.dumps(
                            {"outcome": "failed", "error": error[:500]},
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        connection.execute(
                            """UPDATE morphology_enrichment_clusters_v3
                               SET status = 'cooldown', failure_count = ?,
                                   next_eligible_at = ?, lease_token = '',
                                   lease_expires_at = '', last_outcome_json = ?,
                                   updated_at = ? WHERE cluster_id = ?""",
                            (
                                failures,
                                eligible,
                                outcome,
                                timestamp,
                                str(cluster["cluster_id"]),
                            ),
                        )
                        connection.execute(
                            """UPDATE morphology_enrichment_epochs_v3
                               SET status = 'failed', outcome_json = ?,
                                   updated_at = ?, completed_at = ?
                               WHERE job_id = ?""",
                            (outcome, timestamp, timestamp, job_id),
                        )
                self._fail_blocked_jobs(connection)
            connection.commit()

    @staticmethod
    def _fail_blocked_jobs(connection: sqlite3.Connection) -> int:
        """Mark queued descendants of terminal failures as terminal too.

        A declared dependency is required. Leaving its non-composition
        descendants queued makes progress counters lie and creates jobs that
        can never be claimed. Work through the DAG a layer at a time so
        transitive descendants receive the same truthful terminal state without
        consuming model attempts. Composition is deliberately retained: once
        all of its prerequisites are terminal, it can publish the useful
        accepted checkpoints that remain.
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
                   WHERE job.status = 'queued'
                     AND job.job_type NOT IN (
                         'compose-word-card', 'compose-origin-card'
                     )
                     AND prerequisite.status = 'failed'
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
            timestamp = _now()
            connection.execute("BEGIN IMMEDIATE")
            enrichment_tables = int(connection.execute(
                """SELECT COUNT(*) FROM sqlite_master
                   WHERE type = 'table' AND name IN (
                       'morphology_enrichment_clusters_v3',
                       'morphology_enrichment_epochs_v3'
                   )"""
            ).fetchone()[0]) == 2
            if enrichment_tables:
                connection.execute(
                    """UPDATE morphology_enrichment_clusters_v3 AS cluster
                       SET status = 'queued', updated_at = ?
                       WHERE cluster.status = 'running'
                         AND EXISTS (
                           SELECT 1
                           FROM morphology_enrichment_epochs_v3 AS epoch
                           JOIN preparation_jobs AS job ON job.job_id = epoch.job_id
                           WHERE epoch.cluster_id = cluster.cluster_id
                             AND epoch.job_id = cluster.last_job_id
                             AND job.job_type = 'enrich-morphology-v3'
                             AND job.status = 'running'
                         )""",
                    (timestamp,),
                )
                connection.execute(
                    """UPDATE morphology_enrichment_epochs_v3 AS epoch
                       SET status = 'queued', updated_at = ?
                       WHERE epoch.status = 'running'
                         AND EXISTS (
                           SELECT 1
                           FROM morphology_enrichment_clusters_v3 AS cluster
                           JOIN preparation_jobs AS job
                             ON job.job_id = cluster.last_job_id
                           WHERE cluster.cluster_id = epoch.cluster_id
                             AND cluster.last_job_id = epoch.job_id
                             AND job.job_type = 'enrich-morphology-v3'
                             AND job.status = 'running'
                         )""",
                    (timestamp,),
                )
            cursor = connection.execute(
                """UPDATE preparation_jobs
                   SET status = 'queued',
                       attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                       error = 'worker interrupted; safely requeued',
                       locked_at = '', updated_at = ?
                   WHERE status = 'running'""",
                (timestamp,),
            )
            connection.commit()
            return int(cursor.rowcount)

    def search_lexical_entities(
        self, query: str, *, limit: int = 20, mode: str = "word"
    ) -> list[dict[str, Any]]:
        """Find accepted graph entities, preferring the type requested by a view."""

        query = unicodedata.normalize("NFKC", str(query)).strip()
        if not query:
            return []
        mode = str(mode).strip().lower() or "word"
        if mode not in {"knowledge", "word", "origin", "root", "affix"}:
            raise ValueError("invalid lexical search mode")
        limit = max(1, min(int(limit), 100))
        pattern = f"%{query}%"
        if mode == "root":
            type_rank = """CASE
                WHEN entity_type = 'morpheme'
                 AND json_extract(payload, '$.kind') = 'root' THEN 0
                WHEN entity_type = 'term' THEN 1
                WHEN entity_type = 'morpheme'
                 AND json_extract(payload, '$.kind') IN
                     ('prefix', 'suffix', 'affix', 'infix', 'circumfix') THEN 2
                WHEN entity_type = 'morpheme' THEN 3
                ELSE 4 END"""
        elif mode == "affix":
            type_rank = """CASE
                WHEN entity_type = 'morpheme'
                 AND json_extract(payload, '$.kind') IN
                     ('prefix', 'suffix', 'affix', 'infix', 'circumfix') THEN 0
                WHEN entity_type = 'term' THEN 1
                WHEN entity_type = 'morpheme'
                 AND json_extract(payload, '$.kind') = 'root' THEN 2
                WHEN entity_type = 'morpheme' THEN 3
                ELSE 4 END"""
        else:
            type_rank = """CASE
                WHEN entity_type = 'term' THEN 0
                WHEN entity_type = 'morpheme'
                 AND json_extract(payload, '$.kind') != 'free' THEN 1
                WHEN entity_type = 'morpheme' THEN 2
                ELSE 3 END"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT entity_id, entity_type, canonical_key, label,
                          quality_score, payload, updated_at,
                          (SELECT COUNT(*)
                             FROM entity_edges AS structural_edge
                            WHERE structural_edge.status = 'accepted'
                              AND structural_edge.relation IN (
                                  'has-component', 'shares-component',
                                  'developed-into', 'developed-toward',
                                  'explains-component', 'has-history'
                              )
                              AND (
                                  structural_edge.source_entity_id = entities.entity_id
                                  OR structural_edge.target_entity_id = entities.entity_id
                              )) AS structural_degree
                   FROM entities
                   WHERE status = 'accepted'
                     AND (label LIKE ? COLLATE NOCASE
                       OR canonical_key LIKE ? COLLATE NOCASE)
                   ORDER BY {type_rank},
                            CASE WHEN label = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                            structural_degree DESC,
                            CASE WHEN quality_score IS NULL THEN 1 ELSE 0 END,
                            quality_score DESC, length(label), label, entity_id
                   LIMIT ?""",
                (pattern, pattern, query, limit),
            ).fetchall()
        return [
            {
                "entity_id": str(row["entity_id"]),
                "entity_type": str(row["entity_type"]),
                "canonical_key": str(row["canonical_key"]),
                "label": str(row["label"]),
                "quality": row["quality_score"],
                "structural_degree": int(row["structural_degree"] or 0),
                "payload": json.loads(row["payload"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

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

    def projection_generation(self) -> str:
        """Return a cheap token for presentation-relevant knowledge changes.

        The database and WAL mtimes also advance for worker heartbeats, queue
        leases, and other operational bookkeeping. A trigger-driven revision
        covers normalized entities, graph assertions, artifacts, and citation
        records without letting that operational churn evict caches.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT value FROM schema_meta
                   WHERE key = 'projection_content_revision'"""
            ).fetchone()
        return str(row["value"]) if row else "0"

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
            "relation_assertions",
            "assertion_evidence",
            "preparation_jobs",
            "job_dependencies",
            "inquiry_events",
        )
        with closing(self._connect()) as connection:
            counts = {
                name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in names
            }
            enrichment_table = connection.execute(
                """SELECT 1 FROM sqlite_master WHERE type = 'table'
                   AND name = 'morphology_enrichment_clusters_v3'"""
            ).fetchone()
            if enrichment_table is None:
                queued = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM preparation_jobs
                           WHERE status = 'queued'"""
                    ).fetchone()[0]
                )
            else:
                queued = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM preparation_jobs AS job
                           WHERE job.status = 'queued'
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM morphology_enrichment_clusters_v3 AS cluster
                                 WHERE cluster.last_job_id = job.job_id
                                   AND job.job_type = 'enrich-morphology-v3'
                                   AND cluster.parser_version <> ?
                             )""",
                        (CURRENT_MORPHOLOGY_PARSER_VERSION,),
                    ).fetchone()[0]
                )
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        return {
            "ready": True,
            "database": str(self.database),
            "schema_version": str(version["value"]) if version else "4",
            "counts": counts,
            "queued_jobs": queued,
        }
