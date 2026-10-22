from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


PARSER_VERSION = "book-morphology-observation-v3"
TYPED_COMPONENT_KINDS = frozenset(("root", "prefix", "suffix"))
EXAMPLE_LIMIT = 8


REPAIR_SCHEMA = """
CREATE TABLE IF NOT EXISTS morphology_isolate_repair_runs_v3 (
    run_id TEXT PRIMARY KEY,
    parser_version TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    applied_action_count INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(parser_version, plan_digest)
);

CREATE TABLE IF NOT EXISTS morphology_isolate_repairs_v3 (
    repair_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES morphology_isolate_repair_runs_v3(run_id),
    parser_version TEXT NOT NULL,
    repair_kind TEXT NOT NULL CHECK(
        repair_kind IN ('entity-status', 'entity-quality', 'assertion-evidence')
    ),
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(parser_version, repair_kind, target_id, action, after_json)
);
CREATE INDEX IF NOT EXISTS idx_morph_isolate_repairs_v3_target
    ON morphology_isolate_repairs_v3(parser_version, repair_kind, target_id);

CREATE TABLE IF NOT EXISTS morphology_assertion_provenance_repairs_v3 (
    repair_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES morphology_isolate_repair_runs_v3(run_id),
    parser_version TEXT NOT NULL,
    assertion_id TEXT NOT NULL REFERENCES relation_assertions(assertion_id),
    action TEXT NOT NULL CHECK(
        action IN ('downgrade-to-model', 'archive-unsupported')
    ),
    supporting_edge_id TEXT NOT NULL DEFAULT '',
    context_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    warning TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(parser_version, assertion_id, action, after_json)
);
CREATE INDEX IF NOT EXISTS idx_morph_assertion_provenance_repairs_v3
    ON morphology_assertion_provenance_repairs_v3(
        parser_version, assertion_id, action
    );
"""


@dataclass(frozen=True)
class RepairAction:
    repair_kind: str
    target_id: str
    action: str
    reason: str
    before: dict[str, Any]
    after: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()
    detail: dict[str, Any] | None = None

    def canonical(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
        value["detail"] = dict(self.detail or {})
        return value


@dataclass(frozen=True)
class MorphologyIsolateAuditV3:
    parser_version: str
    generated_at: str
    categories: dict[str, int]
    projections: dict[str, int]
    evidence: dict[str, Any]
    provenance: dict[str, Any]
    examples: dict[str, list[dict[str, Any]]]
    plan_digest: str
    actions: tuple[RepairAction, ...]

    @property
    def mutation_action_count(self) -> int:
        return len(self.actions)

    def to_dict(self, *, include_actions: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "parser_version": self.parser_version,
            "generated_at": self.generated_at,
            "categories": dict(self.categories),
            "projections": dict(self.projections),
            "evidence": dict(self.evidence),
            "provenance": dict(self.provenance),
            "examples": dict(self.examples),
            "plan_digest": self.plan_digest,
            "mutation_action_count": self.mutation_action_count,
        }
        if include_actions:
            result["actions"] = [action.canonical() for action in self.actions]
        return result


@dataclass(frozen=True)
class MorphologyIsolateRepairResultV3:
    copy_database: str
    source_database: str
    before: MorphologyIsolateAuditV3
    after: MorphologyIsolateAuditV3
    applied_action_count: int
    replay_noop: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "copy_database": self.copy_database,
            "source_database": self.source_database,
            "applied_action_count": self.applied_action_count,
            "replay_noop": self.replay_noop,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(kind: str, value: str) -> str:
    return f"{kind}-{uuid.uuid5(uuid.NAMESPACE_URL, value)}"


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({str(item) for item in value if str(item).strip()}))


def _split_group(value: Any) -> tuple[str, ...]:
    return tuple(sorted({item for item in str(value or "").split(",") if item}))


def _calibrated_quality(confidence: Any) -> float:
    try:
        parsed = float(confidence)
    except (TypeError, ValueError):
        parsed = 0.5
    return round(min(0.99, max(0.0, parsed)), 6)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _require_schema(connection: sqlite3.Connection) -> None:
    required = (
        "entities",
        "entity_edges",
        "relation_assertions",
        "assertion_evidence",
        "evidence_records",
        "morphology_source_observations_v3",
        "morphology_term_observations_v3",
        "morphology_component_observations_v3",
        "morphology_formula_links_v3",
        "morphology_declarations_v3",
    )
    missing = [table for table in required if not _table_exists(connection, table)]
    if missing:
        raise RuntimeError(
            "database is not a morphology-v3 knowledge copy; missing: "
            + ", ".join(missing)
        )


def _insert_owner_rows(connection: sqlite3.Connection) -> None:
    statements = (
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT source_entity_id, 'accepted-edge', relation || ':' || edge_id
               FROM entity_edges WHERE status = 'accepted'""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT target_entity_id, 'accepted-edge', relation || ':' || edge_id
               FROM entity_edges WHERE status = 'accepted'""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT subject_entity_id, 'accepted-assertion',
                      relation || ':' || assertion_id
               FROM relation_assertions WHERE status = 'accepted'""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT source_entity_id, 'accepted-assertion',
                      relation || ':' || assertion_id
               FROM relation_assertions WHERE status = 'accepted'""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT target_entity_id, 'accepted-assertion',
                      relation || ':' || assertion_id
               FROM relation_assertions WHERE status = 'accepted'""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT term_id, 'morphology-record', morpheme_id
               FROM term_morphemes""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT morpheme_id, 'morphology-record', term_id
               FROM term_morphemes""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT subject_entity_id, 'semantic-record', entity_id
               FROM meanings""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT source_term_id, 'translation-record', entity_id
               FROM translations""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT target_term_id, 'translation-record', entity_id
               FROM translations WHERE target_term_id IS NOT NULL""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT term_id, 'pronunciation-record', entity_id
               FROM pronunciations""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT subject_entity_id, 'history-record', entity_id
               FROM history_events""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT subject_entity_id, 'active-job', job_type || ':' || job_id
               FROM preparation_jobs
               WHERE subject_entity_id IS NOT NULL
                 AND status IN ('queued', 'running', 'paused')""",
        """INSERT OR IGNORE INTO temp.morph_v3_owned
               SELECT job.subject_entity_id, 'accepted-card',
                      artifact.stage || ':' || artifact.artifact_id
               FROM job_artifacts artifact
               JOIN preparation_jobs job ON job.job_id = artifact.job_id
               WHERE job.subject_entity_id IS NOT NULL
                 AND artifact.validation_state = 'accepted'
                 AND artifact.reusable = 1
                 AND artifact.stage IN ('accepted-word-card', 'accepted-origin-card')""",
    )
    for statement in statements:
        connection.execute(statement)


def _prepare_temporary_audit_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS temp.morph_v3_refs;
        DROP TABLE IF EXISTS temp.morph_v3_owned;
        DROP TABLE IF EXISTS temp.morph_v3_external_evidence;

        CREATE TEMP TABLE morph_v3_refs (
            entity_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            source_observation_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            observation_class TEXT NOT NULL,
            proposed_kind TEXT NOT NULL,
            confidence REAL NOT NULL,
            observation_state TEXT NOT NULL,
            observation_created_at TEXT NOT NULL,
            PRIMARY KEY(entity_id, observation_id)
        ) WITHOUT ROWID;

        INSERT OR IGNORE INTO temp.morph_v3_refs
        SELECT materialized_entity_id, term_observation_id,
               source_observation_id, evidence_id, 'term', '', confidence,
               state, created_at
        FROM morphology_term_observations_v3
        WHERE materialized_entity_id IS NOT NULL;

        INSERT OR IGNORE INTO temp.morph_v3_refs
        SELECT materialized_entity_id, component_observation_id,
               source_observation_id, evidence_id, 'component', proposed_kind,
               kind_confidence, state, created_at
        FROM morphology_component_observations_v3
        WHERE materialized_entity_id IS NOT NULL;

        CREATE INDEX temp.idx_morph_v3_refs_evidence
            ON morph_v3_refs(entity_id, evidence_id);
        CREATE INDEX temp.idx_morph_v3_refs_source
            ON morph_v3_refs(source_observation_id, entity_id);

        CREATE TEMP TABLE morph_v3_owned (
            entity_id TEXT NOT NULL,
            ownership_class TEXT NOT NULL,
            detail TEXT NOT NULL,
            PRIMARY KEY(entity_id, ownership_class, detail)
        ) WITHOUT ROWID;

        CREATE TEMP TABLE morph_v3_external_evidence (
            entity_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            claim TEXT NOT NULL,
            PRIMARY KEY(entity_id, evidence_id, claim)
        ) WITHOUT ROWID;
        """
    )
    _insert_owner_rows(connection)
    connection.execute(
        """INSERT OR IGNORE INTO temp.morph_v3_external_evidence
           SELECT evidence.entity_id, evidence.evidence_id, evidence.claim
           FROM entity_evidence evidence
           WHERE EXISTS (
               SELECT 1 FROM temp.morph_v3_refs ref
               WHERE ref.entity_id = evidence.entity_id
           )
             AND (
                 NOT EXISTS (
                     SELECT 1 FROM temp.morph_v3_refs ref
                     WHERE ref.entity_id = evidence.entity_id
                       AND ref.evidence_id = evidence.evidence_id
                 )
                 OR NOT (
                     evidence.claim LIKE 'printed root %'
                     OR evidence.claim LIKE 'printed prefix %'
                     OR evidence.claim LIKE 'printed suffix %'
                     OR evidence.claim LIKE 'printed morphology entry %'
                     OR evidence.claim LIKE 'book-morphology-v3%'
                     OR evidence.claim LIKE 'morphology v3%'
                 )
             )"""
    )


def _entity_audit_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """WITH summary AS (
               SELECT ref.entity_id, entity.entity_type, entity.label,
                      entity.status, entity.quality_score, entity.created_at,
                      COUNT(*) AS observation_count,
                      COUNT(DISTINCT ref.evidence_id) AS evidence_count,
                      GROUP_CONCAT(DISTINCT ref.evidence_id) AS evidence_ids,
                      GROUP_CONCAT(DISTINCT ref.observation_id) AS observation_ids,
                      GROUP_CONCAT(DISTINCT ref.proposed_kind) AS observed_kinds,
                      MAX(CASE WHEN entity.created_at = ref.observation_created_at
                               THEN 1 ELSE 0 END) AS created_by_v3,
                      SUM(CASE WHEN ref.observation_class = 'component'
                                    AND ref.proposed_kind IN ('root','prefix','suffix')
                                    AND ref.observation_state = 'materialized'
                               THEN 1 ELSE 0 END) AS typed_explicit_count,
                      SUM(CASE WHEN ref.observation_class = 'component'
                                    AND ref.proposed_kind IN ('root','prefix','suffix')
                                    AND ref.observation_state = 'materialized'
                                    AND EXISTS (
                                        SELECT 1
                                        FROM morphology_component_observations_v3 component
                                        JOIN morphology_declarations_v3 declaration
                                          ON declaration.subject_component_observation_id =
                                             component.component_observation_id
                                         AND declaration.source_observation_id =
                                             component.source_observation_id
                                         AND declaration.evidence_id = component.evidence_id
                                        WHERE component.component_observation_id =
                                              ref.observation_id
                                          AND (
                                              json_extract(
                                                  component.properties_json,
                                                  '$.declared'
                                              ) = 1
                                              OR json_extract(
                                                  component.properties_json,
                                                  '$.basis'
                                              ) IN (
                                                  'declaration-form-and-gloss',
                                                  'printed-declaration'
                                              )
                                          )
                                          AND declaration.declaration_type IN (
                                              'definition', 'history', 'source'
                                          )
                                          AND declaration.state IN (
                                              'materializable', 'materialized'
                                          )
                                          AND declaration.qwen_ready = 0
                                          AND TRIM(declaration.object_value) <> ''
                                    )
                               THEN 1 ELSE 0 END)
                          AS authoritative_declaration_count,
                      SUM(CASE WHEN ref.observation_class = 'component'
                                    AND ref.proposed_kind = 'ambiguous'
                               THEN 1 ELSE 0 END) AS ambiguous_count,
                      MAX(CASE WHEN ref.observation_class = 'component'
                                    AND ref.proposed_kind IN ('root','prefix','suffix')
                               THEN ref.confidence ELSE NULL END)
                          AS typed_parser_confidence
               FROM temp.morph_v3_refs ref
               JOIN entities entity ON entity.entity_id = ref.entity_id
               GROUP BY ref.entity_id
           )
           SELECT summary.*,
                  (SELECT COUNT(*) FROM temp.morph_v3_owned owner
                   WHERE owner.entity_id = summary.entity_id) AS owner_count,
                  (SELECT GROUP_CONCAT(DISTINCT owner.ownership_class)
                   FROM temp.morph_v3_owned owner
                   WHERE owner.entity_id = summary.entity_id) AS ownership_classes,
                  (SELECT COUNT(*) FROM temp.morph_v3_external_evidence external
                   WHERE external.entity_id = summary.entity_id)
                      AS external_evidence_count,
                  (SELECT GROUP_CONCAT(DISTINCT external.evidence_id)
                   FROM temp.morph_v3_external_evidence external
                   WHERE external.entity_id = summary.entity_id)
                      AS external_evidence_ids
           FROM summary
           ORDER BY summary.entity_type, summary.entity_id"""
    ).fetchall()


def _entity_example(row: sqlite3.Row, category: str) -> dict[str, Any]:
    return {
        "category": category,
        "entity_id": str(row["entity_id"]),
        "entity_type": str(row["entity_type"]),
        "label": str(row["label"]),
        "status": str(row["status"]),
        "quality_score": row["quality_score"],
        "created_by_v3": bool(row["created_by_v3"]),
        "observation_count": int(row["observation_count"]),
        "observed_kinds": list(_split_group(row["observed_kinds"])),
        "evidence_ids": list(_split_group(row["evidence_ids"])),
        "ownership_classes": list(_split_group(row["ownership_classes"])),
        "external_evidence_ids": list(_split_group(row["external_evidence_ids"])),
    }


def _classify_entities(
    rows: Iterable[sqlite3.Row],
) -> tuple[Counter[str], dict[str, list[dict[str, Any]]], list[RepairAction]]:
    categories: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    actions: list[RepairAction] = []
    for row in rows:
        entity_type = str(row["entity_type"])
        status = str(row["status"])
        typed_count = int(row["typed_explicit_count"] or 0)
        authoritative_count = int(row["authoritative_declaration_count"] or 0)
        ambiguous_count = int(row["ambiguous_count"] or 0)
        owner_count = int(row["owner_count"] or 0)
        external_count = int(row["external_evidence_count"] or 0)
        created_by_v3 = bool(row["created_by_v3"])

        if status != "accepted":
            category = "already-nonaccepted"
        elif owner_count:
            category = "owned-accepted-preserved"
        elif entity_type == "morpheme" and authoritative_count:
            calibrated = _calibrated_quality(row["typed_parser_confidence"])
            current = row["quality_score"]
            if current is None or abs(float(current) - calibrated) > 0.0000005:
                category = "standalone-explicit-morpheme-calibrate"
                actions.append(
                    RepairAction(
                        repair_kind="entity-quality",
                        target_id=str(row["entity_id"]),
                        action="calibrate-parser-quality",
                        reason=(
                            "standalone typed declaration remains accepted at its "
                            "strongest calibrated parser confidence, never 1.0"
                        ),
                        before={"quality_score": current},
                        after={"quality_score": calibrated},
                        evidence_ids=_split_group(row["evidence_ids"]),
                        detail={
                            "typed_observation_count": typed_count,
                            "authoritative_declaration_count": authoritative_count,
                            "parser_confidence": row["typed_parser_confidence"],
                            "observed_kinds": list(_split_group(row["observed_kinds"])),
                        },
                    )
                )
            else:
                category = "standalone-explicit-morpheme-preserved"
        elif external_count:
            category = "externally-evidenced-accepted-preserved"
        elif not created_by_v3:
            category = "preexisting-accepted-preserved"
        elif entity_type == "term":
            category = "isolated-v3-term-demote"
            actions.append(
                RepairAction(
                    repair_kind="entity-status",
                    target_id=str(row["entity_id"]),
                    action="demote-to-draft",
                    reason=(
                        "accepted term was created solely by morphology v3 and has "
                        "no accepted incident knowledge, active job, or accepted card"
                    ),
                    before={
                        "status": status,
                        "quality_score": row["quality_score"],
                    },
                    after={
                        "status": "draft",
                        "quality_score": _calibrated_quality(row["quality_score"]),
                    },
                    evidence_ids=_split_group(row["evidence_ids"]),
                    detail={
                        "observation_ids": list(_split_group(row["observation_ids"])),
                        "observation_count": int(row["observation_count"]),
                    },
                )
            )
        elif entity_type == "morpheme" and ambiguous_count:
            category = "isolated-ambiguous-morpheme-demote"
            actions.append(
                RepairAction(
                    repair_kind="entity-status",
                    target_id=str(row["entity_id"]),
                    action="demote-to-draft",
                    reason=(
                        "ambiguous v3 component remains a proposed draft until a "
                        "typed root, prefix, or suffix assertion is available"
                    ),
                    before={
                        "status": status,
                        "quality_score": row["quality_score"],
                    },
                    after={
                        "status": "draft",
                        "quality_score": _calibrated_quality(row["quality_score"]),
                    },
                    evidence_ids=_split_group(row["evidence_ids"]),
                    detail={"ambiguous_observation_count": ambiguous_count},
                )
            )
        elif entity_type == "morpheme" and typed_count:
            category = "isolated-unsubstantiated-morpheme-demote"
            actions.append(
                RepairAction(
                    repair_kind="entity-status",
                    target_id=str(row["entity_id"]),
                    action="demote-to-draft",
                    reason=(
                        "typed parser observation has no exact attached definition, "
                        "history, source declaration, or accepted incident ownership"
                    ),
                    before={
                        "status": status,
                        "quality_score": row["quality_score"],
                    },
                    after={
                        "status": "draft",
                        "quality_score": _calibrated_quality(
                            row["typed_parser_confidence"]
                        ),
                    },
                    evidence_ids=_split_group(row["evidence_ids"]),
                    detail={
                        "typed_observation_count": typed_count,
                        "authoritative_declaration_count": authoritative_count,
                        "observed_kinds": list(_split_group(row["observed_kinds"])),
                    },
                )
            )
        else:
            category = "unclassified-accepted-preserved"

        categories[category] += 1
        if len(examples[category]) < EXAMPLE_LIMIT:
            examples[category].append(_entity_example(row, category))
    return categories, dict(examples), actions


def _evidence_exists_for_v3_entity(
    connection: sqlite3.Connection,
    evidence_id: str,
    entity_ids: tuple[str, ...],
) -> bool:
    placeholders = ",".join("?" for _ in entity_ids)
    if not placeholders:
        return False
    return connection.execute(
        f"""SELECT 1 FROM temp.morph_v3_refs
            WHERE evidence_id = ? AND entity_id IN ({placeholders}) LIMIT 1""",
        (evidence_id, *entity_ids),
    ).fetchone() is not None


def _formula_evidence(
    connection: sqlite3.Connection, term_id: str, component_id: str
) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT DISTINCT link.evidence_id
           FROM morphology_formula_links_v3 link
           JOIN morphology_term_observations_v3 term
             ON term.term_observation_id = link.term_observation_id
           JOIN morphology_component_observations_v3 component
             ON component.component_observation_id = link.component_observation_id
           WHERE term.materialized_entity_id = ?
             AND component.materialized_entity_id = ?
             AND link.state = 'materialized'
             AND term.state = 'materialized'
             AND component.state = 'materialized'
             AND link.evidence_id = term.evidence_id
             AND link.evidence_id = component.evidence_id
           ORDER BY link.evidence_id""",
        (term_id, component_id),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _definition_evidence(
    connection: sqlite3.Connection, component_id: str, meaning_id: str
) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT DISTINCT declaration.evidence_id, declaration.object_value,
                          component.gloss, meaning.definition
           FROM morphology_component_observations_v3 component
           JOIN morphology_declarations_v3 declaration
             ON declaration.subject_component_observation_id =
                component.component_observation_id
           JOIN meanings meaning
             ON meaning.entity_id = ?
            AND meaning.subject_entity_id = component.materialized_entity_id
           WHERE component.materialized_entity_id = ?
             AND component.state = 'materialized'
             AND declaration.declaration_type = 'definition'
             AND declaration.evidence_id = component.evidence_id""",
        (meaning_id, component_id),
    ).fetchall()
    evidence_ids: set[str] = set()
    for row in rows:
        definition = _normalise(row[3])
        if definition and definition in {
            _normalise(row[1]),
            _normalise(row[2]),
        }:
            evidence_ids.add(str(row[0]))
    return tuple(sorted(evidence_ids))


def _history_evidence(
    connection: sqlite3.Connection,
    term_id: str,
    component_id: str,
    source_label: str,
    target_label: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT DISTINCT link.evidence_id, source.raw_excerpt,
                          declaration.object_value
           FROM morphology_formula_links_v3 link
           JOIN morphology_term_observations_v3 term
             ON term.term_observation_id = link.term_observation_id
           JOIN morphology_component_observations_v3 component
             ON component.component_observation_id = link.component_observation_id
           JOIN morphology_source_observations_v3 source
             ON source.source_observation_id = link.source_observation_id
           JOIN morphology_declarations_v3 declaration
             ON declaration.source_observation_id = source.source_observation_id
            AND declaration.subject_component_observation_id =
                component.component_observation_id
            AND declaration.declaration_type IN ('history', 'source')
           WHERE term.materialized_entity_id = ?
             AND component.materialized_entity_id = ?
             AND link.state = 'materialized'
             AND link.evidence_id = term.evidence_id
             AND link.evidence_id = component.evidence_id
             AND link.evidence_id = declaration.evidence_id""",
        (term_id, component_id),
    ).fetchall()
    source_text = _normalise(source_label)
    target_text = _normalise(target_label)
    evidence_ids: set[str] = set()
    for row in rows:
        excerpt = _normalise(f"{row[1]} {row[2]}")
        if source_text and target_text and source_text in excerpt and target_text in excerpt:
            evidence_ids.add(str(row[0]))
    return tuple(sorted(evidence_ids))


def _context_evidence_ids(
    connection: sqlite3.Connection,
    entity_ids: Iterable[str],
) -> tuple[str, ...]:
    identifiers = tuple(sorted({item for item in entity_ids if item}))
    if not identifiers:
        return ()
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        f"""SELECT DISTINCT ref.evidence_id
            FROM temp.morph_v3_refs ref
            JOIN evidence_records evidence
              ON evidence.evidence_id = ref.evidence_id
            WHERE ref.entity_id IN ({placeholders})
            ORDER BY ref.evidence_id""",
        identifiers,
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _verified_existing_context_ids(
    connection: sqlite3.Connection,
    properties: dict[str, Any],
) -> tuple[str, ...]:
    candidates = _json_string_list(properties.get("context_evidence_ids"))
    if not candidates:
        return ()
    placeholders = ",".join("?" for _ in candidates)
    rows = connection.execute(
        f"""SELECT evidence_id FROM evidence_records
            WHERE evidence_id IN ({placeholders}) ORDER BY evidence_id""",
        candidates,
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _unsupported_book_provenance_action(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    properties: dict[str, Any],
) -> RepairAction:
    assertion_id = str(row["assertion_id"])
    entity_ids = (
        str(row["subject_entity_id"]),
        str(row["source_entity_id"]),
        str(row["target_entity_id"]),
        str(properties.get("term_id") or ""),
        str(properties.get("component_id") or ""),
    )
    context_ids = tuple(
        sorted(
            set(_context_evidence_ids(connection, entity_ids))
            | set(_verified_existing_context_ids(connection, properties))
        )
    )
    supporting_edge = connection.execute(
        """SELECT edge_id, basis, confidence, properties
           FROM entity_edges
           WHERE source_entity_id = ? AND target_entity_id = ?
             AND relation = ? AND status = 'accepted'
           ORDER BY confidence DESC, edge_id LIMIT 1""",
        (
            str(row["source_entity_id"]),
            str(row["target_entity_id"]),
            str(row["relation"]),
        ),
    ).fetchone()
    has_context = bool(context_ids)
    has_edge = supporting_edge is not None
    edge_properties = (
        _json_object(supporting_edge["properties"]) if has_edge else {}
    )
    edge_evidence_candidates = _json_string_list(
        edge_properties.get("evidence_ids")
    )
    verified_edge_evidence: tuple[str, ...] = ()
    if edge_evidence_candidates:
        placeholders = ",".join("?" for _ in edge_evidence_candidates)
        verified_edge_evidence = tuple(
            str(item[0])
            for item in connection.execute(
                f"""SELECT evidence_id FROM evidence_records
                    WHERE evidence_id IN ({placeholders}) ORDER BY evidence_id""",
                edge_evidence_candidates,
            ).fetchall()
        )
    downgrade_edge = bool(
        has_edge
        and str(supporting_edge["basis"]) == "book"
        and not verified_edge_evidence
    )
    repaired_edge_properties = dict(edge_properties)
    if downgrade_edge:
        repaired_edge_properties.setdefault("original_basis", "book")
        repaired_edge_properties["provenance_warning"] = (
            "original book edge had no independent evidence IDs; context is not citation"
        )
        repaired_edge_properties["context_evidence_ids"] = list(context_ids)
        repaired_edge_properties["provenance_repair"] = {
            "action": "downgrade-to-model",
            "parser_version": PARSER_VERSION,
        }
    action = "downgrade-to-model" if has_edge or has_context else "archive-unsupported"
    after_status = "accepted" if action == "downgrade-to-model" else "archived"
    after_basis = "model" if action == "downgrade-to-model" else "book"
    warning = (
        "original book basis had no assertion_evidence; retained evidence IDs are "
        "context only and do not cite this relation"
    )
    repaired_properties = dict(properties)
    repaired_properties.setdefault("original_basis", "book")
    repaired_properties["provenance_warning"] = warning
    repaired_properties["context_evidence_ids"] = list(context_ids)
    repaired_properties["provenance_repair"] = {
        "action": action,
        "parser_version": PARSER_VERSION,
        "supporting_edge_id": str(supporting_edge["edge_id"]) if has_edge else "",
        "supporting_edge_basis": str(supporting_edge["basis"]) if has_edge else "",
        "supporting_edge_confidence": (
            float(supporting_edge["confidence"]) if has_edge else None
        ),
    }
    return RepairAction(
        repair_kind="assertion-provenance",
        target_id=assertion_id,
        action=action,
        reason=warning,
        before={
            "basis": "book",
            "status": "accepted",
            "properties_json": properties,
        },
        after={
            "basis": after_basis,
            "status": after_status,
            "properties_json": repaired_properties,
            "supporting_edge": {
                "edge_id": str(supporting_edge["edge_id"]) if has_edge else "",
                "before_basis": str(supporting_edge["basis"]) if has_edge else "",
                "after_basis": (
                    "model" if downgrade_edge else (
                        str(supporting_edge["basis"]) if has_edge else ""
                    )
                ),
                "before_properties": edge_properties,
                "after_properties": repaired_edge_properties,
                "independent_evidence_ids": list(verified_edge_evidence),
                "downgraded": downgrade_edge,
            },
        },
        evidence_ids=context_ids,
        detail={
            "relation": str(row["relation"]),
            "context_only": True,
            "supporting_edge_id": (
                str(supporting_edge["edge_id"]) if has_edge else ""
            ),
            "supporting_edge_downgraded": downgrade_edge,
            "supporting_edge_independent_evidence_ids": list(
                verified_edge_evidence
            ),
        },
    )


def _assertion_evidence_actions(
    connection: sqlite3.Connection,
) -> tuple[list[RepairAction], list[RepairAction], list[dict[str, Any]]]:
    rows = connection.execute(
        """SELECT assertion.assertion_id, assertion.relation,
                  assertion.subject_entity_id, assertion.source_entity_id,
                  assertion.target_entity_id, assertion.properties_json,
                  source.label AS source_label, target.label AS target_label
           FROM relation_assertions assertion
           JOIN entities source ON source.entity_id = assertion.source_entity_id
           JOIN entities target ON target.entity_id = assertion.target_entity_id
           WHERE assertion.status = 'accepted'
             AND assertion.basis = 'book'
             AND NOT EXISTS (
                 SELECT 1 FROM assertion_evidence evidence
                 WHERE evidence.assertion_id = assertion.assertion_id
             )
           ORDER BY assertion.assertion_id"""
    ).fetchall()
    actions: list[RepairAction] = []
    provenance_actions: list[RepairAction] = []
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        assertion_id = str(row["assertion_id"])
        relation = str(row["relation"])
        subject_id = str(row["subject_entity_id"])
        source_id = str(row["source_entity_id"])
        target_id = str(row["target_entity_id"])
        properties = _json_object(row["properties_json"])
        evidence_ids: set[str] = set()
        mapping = ""

        if relation == "has-component" and subject_id == source_id:
            evidence_ids.update(_formula_evidence(connection, source_id, target_id))
            mapping = "exact-materialized-formula"
        elif relation == "has-meaning" and subject_id == source_id:
            evidence_ids.update(_definition_evidence(connection, source_id, target_id))
            mapping = "exact-materialized-definition"
        elif relation == "developed-into":
            term_id = str(properties.get("term_id") or "")
            component_id = str(properties.get("component_id") or "")
            if term_id and component_id and subject_id == term_id:
                evidence_ids.update(
                    _history_evidence(
                        connection,
                        term_id,
                        component_id,
                        str(row["source_label"]),
                        str(row["target_label"]),
                    )
                )
                mapping = "exact-history-text-and-materialized-formula"

        if properties.get("acquisition") == "book-morphology-v3":
            endpoint_ids = tuple(
                dict.fromkeys(
                    (
                        subject_id,
                        source_id,
                        target_id,
                        str(properties.get("term_id") or ""),
                        str(properties.get("component_id") or ""),
                    )
                )
            )
            endpoint_ids = tuple(item for item in endpoint_ids if item)
            for evidence_id in _json_string_list(properties.get("evidence_ids")):
                if _evidence_exists_for_v3_entity(
                    connection, evidence_id, endpoint_ids
                ):
                    evidence_ids.add(evidence_id)
                    mapping = mapping or "explicit-v3-evidence-property"

        if evidence_ids:
            actions.append(
                RepairAction(
                    repair_kind="assertion-evidence",
                    target_id=assertion_id,
                    action="attach-authoritative-observation-evidence",
                    reason=(
                        "accepted book assertion maps exactly to retained morphology "
                        "v3 observation evidence"
                    ),
                    before={"assertion_evidence_ids": []},
                    after={"assertion_evidence_ids": sorted(evidence_ids)},
                    evidence_ids=tuple(sorted(evidence_ids)),
                    detail={"relation": relation, "mapping": mapping},
                )
            )
        else:
            provenance_actions.append(
                _unsupported_book_provenance_action(connection, row, properties)
            )
    return actions, provenance_actions, unresolved


def audit_connection(
    connection: sqlite3.Connection,
    *,
    parser_version: str = PARSER_VERSION,
) -> MorphologyIsolateAuditV3:
    previous_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _require_schema(connection)
        _prepare_temporary_audit_tables(connection)
        entity_rows = _entity_audit_rows(connection)
        categories, examples, entity_actions = _classify_entities(entity_rows)
        evidence_actions, provenance_actions, unresolved = (
            _assertion_evidence_actions(connection)
        )
        provenance_downgrades = [
            action
            for action in provenance_actions
            if action.action == "downgrade-to-model"
        ]
        provenance_archives = [
            action
            for action in provenance_actions
            if action.action == "archive-unsupported"
        ]
        provenance_edge_downgrades = [
            action
            for action in provenance_actions
            if bool((action.detail or {}).get("supporting_edge_downgraded"))
        ]
        provenance_edges_with_independent_evidence = [
            action
            for action in provenance_actions
            if bool(
                (action.detail or {}).get(
                    "supporting_edge_independent_evidence_ids"
                )
            )
        ]
        categories["assertion-evidence-repairable"] = len(evidence_actions)
        categories["assertion-evidence-unresolved"] = len(unresolved)
        categories["unsupported-book-assertion-downgrade-model"] = len(
            provenance_downgrades
        )
        categories["unsupported-book-assertion-archive"] = len(
            provenance_archives
        )
        categories["unsupported-book-edge-downgrade-model"] = len(
            provenance_edge_downgrades
        )
        categories["book-edge-independent-evidence-preserved"] = len(
            provenance_edges_with_independent_evidence
        )
        if evidence_actions:
            examples["assertion-evidence-repairable"] = [
                action.canonical() for action in evidence_actions[:EXAMPLE_LIMIT]
            ]
        if unresolved:
            examples["assertion-evidence-unresolved"] = unresolved[:EXAMPLE_LIMIT]
        if provenance_downgrades:
            examples["unsupported-book-assertion-downgrade-model"] = [
                action.canonical()
                for action in provenance_downgrades[:EXAMPLE_LIMIT]
            ]
        if provenance_archives:
            examples["unsupported-book-assertion-archive"] = [
                action.canonical() for action in provenance_archives[:EXAMPLE_LIMIT]
            ]

        actions = tuple(
            sorted(
                (*entity_actions, *evidence_actions, *provenance_actions),
                key=lambda action: (
                    action.repair_kind,
                    action.target_id,
                    action.action,
                ),
            )
        )
        plan_digest = _digest([action.canonical() for action in actions])
        accepted_terms = sum(
            1
            for row in entity_rows
            if row["entity_type"] == "term" and row["status"] == "accepted"
        )
        accepted_morphemes = sum(
            1
            for row in entity_rows
            if row["entity_type"] == "morpheme" and row["status"] == "accepted"
        )
        status_demotions = sum(
            1
            for action in actions
            if action.repair_kind == "entity-status"
            and action.after.get("status") == "draft"
        )
        term_demotions = categories.get("isolated-v3-term-demote", 0)
        morpheme_demotions = categories.get(
            "isolated-ambiguous-morpheme-demote", 0
        ) + categories.get("isolated-unsubstantiated-morpheme-demote", 0)
        projections = {
            "observed_v3_entity_count": len(entity_rows),
            "accepted_v3_terms_before": accepted_terms,
            "accepted_v3_terms_after": accepted_terms - term_demotions,
            "accepted_v3_morphemes_before": accepted_morphemes,
            "accepted_v3_morphemes_after": accepted_morphemes - morpheme_demotions,
            "entity_status_demotions": status_demotions,
            "typed_standalone_quality_calibrations": categories.get(
                "standalone-explicit-morpheme-calibrate", 0
            ),
            "assertion_evidence_links_to_add": sum(
                len(action.evidence_ids) for action in evidence_actions
            ),
            "unsupported_book_assertions_downgrade_to_model": len(
                provenance_downgrades
            ),
            "unsupported_book_assertions_archive": len(provenance_archives),
            "context_only_evidence_ids_recorded": sum(
                len(action.evidence_ids) for action in provenance_actions
            ),
            "unsupported_book_edges_downgrade_to_model": len(
                provenance_edge_downgrades
            ),
        }
        observation_evidence_ids = connection.execute(
            "SELECT COUNT(DISTINCT evidence_id) FROM temp.morph_v3_refs"
        ).fetchone()[0]
        evidence = {
            "retained_observation_evidence_id_count": int(observation_evidence_ids),
            "repairable_assertion_count": len(evidence_actions),
            "repairable_assertion_evidence_link_count": sum(
                len(action.evidence_ids) for action in evidence_actions
            ),
            "unresolved_book_assertion_count": len(unresolved),
            "context_only_assertion_count": len(provenance_actions),
            "context_only_evidence_id_count": sum(
                len(action.evidence_ids) for action in provenance_actions
            ),
            "mapping_policy": (
                "exact materialized formula, exact definition, exact history text, "
                "or explicit v3 evidence property only; unsupported book assertion "
                "evidence remains context-only and never assertion_evidence"
            ),
        }
        provenance = {
            "unsupported_book_assertion_count": len(provenance_actions),
            "downgrade_to_model_count": len(provenance_downgrades),
            "archive_unsupported_count": len(provenance_archives),
            "context_evidence_id_count": sum(
                len(action.evidence_ids) for action in provenance_actions
            ),
            "supporting_edges_downgraded_to_model": len(
                provenance_edge_downgrades
            ),
            "supporting_book_edges_with_independent_evidence": len(
                provenance_edges_with_independent_evidence
            ),
            "assertion_evidence_links_created": 0,
            "policy": (
                "book basis requires assertion_evidence; supporting graph/evidence "
                "without exact citation is model context only"
            ),
        }
        return MorphologyIsolateAuditV3(
            parser_version=parser_version,
            generated_at=_now(),
            categories=dict(sorted(categories.items())),
            projections=projections,
            evidence=evidence,
            provenance=provenance,
            examples={key: examples[key] for key in sorted(examples)},
            plan_digest=plan_digest,
            actions=actions,
        )
    finally:
        connection.row_factory = previous_row_factory


def _read_only_connection(database: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(database.resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30.0)


def audit_database(
    database: str | Path,
    *,
    parser_version: str = PARSER_VERSION,
) -> MorphologyIsolateAuditV3:
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with _read_only_connection(path) as connection:
        return audit_connection(connection, parser_version=parser_version)


def _assert_distinct_copy(copy_database: Path, source_database: Path) -> None:
    if not copy_database.is_file():
        raise FileNotFoundError(copy_database)
    if not source_database.is_file():
        raise FileNotFoundError(source_database)
    if copy_database.resolve() == source_database.resolve() or os.path.samefile(
        copy_database, source_database
    ):
        raise ValueError("copy database must be a distinct file from the source database")


def _apply_action(
    connection: sqlite3.Connection,
    action: RepairAction,
    timestamp: str,
) -> int:
    if action.repair_kind == "entity-status":
        if "quality_score" in action.after:
            cursor = connection.execute(
                """UPDATE entities
                   SET status = ?, quality_score = ?, updated_at = ?
                   WHERE entity_id = ? AND status = ?""",
                (
                    str(action.after["status"]),
                    float(action.after["quality_score"]),
                    timestamp,
                    action.target_id,
                    str(action.before["status"]),
                ),
            )
        else:
            cursor = connection.execute(
                """UPDATE entities SET status = ?, updated_at = ?
                   WHERE entity_id = ? AND status = ?""",
                (
                    str(action.after["status"]),
                    timestamp,
                    action.target_id,
                    str(action.before["status"]),
                ),
            )
        return int(bool(cursor.rowcount))
    if action.repair_kind == "entity-quality":
        before = action.before.get("quality_score")
        if before is None:
            cursor = connection.execute(
                """UPDATE entities SET quality_score = ?, updated_at = ?
                   WHERE entity_id = ? AND quality_score IS NULL""",
                (float(action.after["quality_score"]), timestamp, action.target_id),
            )
        else:
            cursor = connection.execute(
                """UPDATE entities SET quality_score = ?, updated_at = ?
                   WHERE entity_id = ? AND quality_score = ?""",
                (
                    float(action.after["quality_score"]),
                    timestamp,
                    action.target_id,
                    float(before),
                ),
            )
        return int(bool(cursor.rowcount))
    if action.repair_kind == "assertion-provenance":
        cursor = connection.execute(
            """UPDATE relation_assertions
               SET basis = ?, status = ?, properties_json = ?,
                   revision = revision + 1, updated_at = ?
               WHERE assertion_id = ? AND basis = ? AND status = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM assertion_evidence evidence
                     WHERE evidence.assertion_id = relation_assertions.assertion_id
                 )""",
            (
                str(action.after["basis"]),
                str(action.after["status"]),
                _canonical_json(action.after["properties_json"]),
                timestamp,
                action.target_id,
                str(action.before["basis"]),
                str(action.before["status"]),
            ),
        )
        changed = int(bool(cursor.rowcount))
        edge = action.after.get("supporting_edge")
        if changed and isinstance(edge, dict) and edge.get("downgraded"):
            edge_cursor = connection.execute(
                """UPDATE entity_edges
                   SET basis = ?, properties = ?, updated_at = ?
                   WHERE edge_id = ? AND status = 'accepted' AND basis = ?""",
                (
                    str(edge["after_basis"]),
                    _canonical_json(edge["after_properties"]),
                    timestamp,
                    str(edge["edge_id"]),
                    str(edge["before_basis"]),
                ),
            )
            if not edge_cursor.rowcount:
                raise RuntimeError(
                    f"supporting edge provenance changed during repair: {edge['edge_id']}"
                )
        return changed
    if action.repair_kind == "assertion-evidence":
        added = 0
        for evidence_id in action.evidence_ids:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id)
                   SELECT ?, evidence_id FROM evidence_records WHERE evidence_id = ?""",
                (action.target_id, evidence_id),
            )
            added += int(bool(cursor.rowcount))
        return int(bool(added))
    raise ValueError(f"unsupported repair action: {action.repair_kind}")


def repair_copy_database(
    copy_database: str | Path,
    *,
    source_database: str | Path,
    parser_version: str = PARSER_VERSION,
) -> MorphologyIsolateRepairResultV3:
    copy_path = Path(copy_database).resolve()
    source_path = Path(source_database).resolve()
    _assert_distinct_copy(copy_path, source_path)

    connection = sqlite3.connect(copy_path, timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        before = audit_connection(connection, parser_version=parser_version)
        applied = 0
        if before.actions:
            timestamp = _now()
            run_id = _identifier(
                "morph-isolate-repair-run",
                f"{parser_version}:{before.plan_digest}",
            )
            connection.executescript(REPAIR_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT OR IGNORE INTO morphology_isolate_repair_runs_v3(
                           run_id, parser_version, plan_digest,
                           applied_action_count, report_json, created_at
                       ) VALUES (?, ?, ?, 0, ?, ?)""",
                    (
                        run_id,
                        parser_version,
                        before.plan_digest,
                        _canonical_json(before.to_dict()),
                        timestamp,
                    ),
                )
                graph_basis_changed = False
                for action in before.actions:
                    changed = _apply_action(connection, action, timestamp)
                    if not changed:
                        continue
                    applied += 1
                    if bool(
                        (action.detail or {}).get("supporting_edge_downgraded")
                    ):
                        graph_basis_changed = True
                    repair_id = _identifier(
                        "morph-isolate-repair",
                        f"{parser_version}:{_canonical_json(action.canonical())}",
                    )
                    if action.repair_kind == "assertion-provenance":
                        connection.execute(
                            """INSERT OR IGNORE INTO
                                   morphology_assertion_provenance_repairs_v3(
                                       repair_id, run_id, parser_version,
                                       assertion_id, action, supporting_edge_id,
                                       context_evidence_ids_json, before_json,
                                       after_json, warning, created_at
                                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                repair_id,
                                run_id,
                                parser_version,
                                action.target_id,
                                action.action,
                                str((action.detail or {}).get("supporting_edge_id") or ""),
                                _canonical_json(list(action.evidence_ids)),
                                _canonical_json(action.before),
                                _canonical_json(action.after),
                                action.reason,
                                timestamp,
                            ),
                        )
                    else:
                        connection.execute(
                            """INSERT OR IGNORE INTO morphology_isolate_repairs_v3(
                                   repair_id, run_id, parser_version, repair_kind,
                                   target_id, action, reason, before_json, after_json,
                                   evidence_ids_json, detail_json, created_at
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                repair_id,
                                run_id,
                                parser_version,
                                action.repair_kind,
                                action.target_id,
                                action.action,
                                action.reason,
                                _canonical_json(action.before),
                                _canonical_json(action.after),
                                _canonical_json(list(action.evidence_ids)),
                                _canonical_json(action.detail or {}),
                                timestamp,
                            ),
                        )
                if graph_basis_changed:
                    revision = connection.execute(
                        """UPDATE schema_meta
                           SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
                           WHERE key = 'relation_graph_revision'"""
                    )
                    if not revision.rowcount:
                        raise RuntimeError("relation graph revision key is missing")
                connection.execute(
                    """UPDATE morphology_isolate_repair_runs_v3
                       SET applied_action_count = ? WHERE run_id = ?""",
                    (applied, run_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        after = audit_connection(connection, parser_version=parser_version)
        return MorphologyIsolateRepairResultV3(
            copy_database=str(copy_path),
            source_database=str(source_path),
            before=before,
            after=after,
            applied_action_count=applied,
            replay_noop=after.mutation_action_count == 0,
        )
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit morphology-v3 isolated entities, or idempotently repair an "
            "explicit disposable copy. Live-database mutation is not supported."
        )
    )
    parser.add_argument("--copy-db", required=True, type=Path)
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--apply-copy", action="store_true")
    parser.add_argument("--include-actions", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.apply_copy:
        if args.source_db is None:
            raise SystemExit("--source-db is required with --apply-copy")
        result: dict[str, Any] = repair_copy_database(
            args.copy_db,
            source_database=args.source_db,
        ).to_dict()
    else:
        result = audit_database(args.copy_db).to_dict(
            include_actions=args.include_actions
        )
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
