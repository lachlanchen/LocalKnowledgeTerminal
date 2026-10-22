"""Normalized ownership and deterministic reduction for relation assertions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RELATION_SUPPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS relation_assertion_supports (
    support_id TEXT PRIMARY KEY,
    assertion_id TEXT NOT NULL REFERENCES relation_assertions(assertion_id),
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_version TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL CHECK (basis IN ('book', 'model', 'reviewed', 'derived')),
    confidence REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'retired')),
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(assertion_id, owner_type, owner_id)
);
CREATE INDEX IF NOT EXISTS idx_relation_support_owner
    ON relation_assertion_supports(owner_type, owner_id, status);
CREATE INDEX IF NOT EXISTS idx_relation_support_assertion
    ON relation_assertion_supports(assertion_id, status);
CREATE TABLE IF NOT EXISTS relation_assertion_support_evidence (
    support_id TEXT NOT NULL REFERENCES relation_assertion_supports(support_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    PRIMARY KEY(support_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS relation_support_owner_scopes (
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_version TEXT NOT NULL DEFAULT '',
    desired_count INTEGER NOT NULL,
    desired_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_type, owner_id)
);
CREATE TABLE IF NOT EXISTS relation_support_migrations (
    migration_key TEXT PRIMARY KEY,
    coverage_fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    destructive_ready INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class RelationSupportSpec:
    assertion_id: str
    basis: str
    confidence: float
    properties: Mapping[str, Any]
    evidence_ids: Sequence[str] = ()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()
    return f"{prefix}:{digest[:32]}"


def ensure_relation_support_schema(connection: sqlite3.Connection) -> None:
    """Install owner tables outside an active write transaction."""
    connection.executescript(RELATION_SUPPORT_SCHEMA)


def morphology_source_owner_id(source_record_id: object) -> str:
    value = str(source_record_id).strip()
    if not value:
        raise ValueError("source_record_id must be non-empty")
    return value


def upsert_relation_support(
    connection: sqlite3.Connection,
    *,
    assertion_id: str,
    owner_type: str,
    owner_id: str,
    owner_version: str = "",
    basis: str,
    confidence: float,
    properties: Mapping[str, Any] | None = None,
    evidence_ids: Iterable[str] = (),
    timestamp: str | None = None,
) -> str:
    timestamp = timestamp or _now()
    assertion_id = str(assertion_id).strip()
    owner_type = str(owner_type).strip()
    owner_id = str(owner_id).strip()
    basis = str(basis).strip().casefold()
    if not assertion_id or not owner_type or not owner_id:
        raise ValueError("assertion_id and owner scope must be non-empty")
    if basis not in {"book", "model", "reviewed", "derived"}:
        raise ValueError(f"unsupported relation basis: {basis}")
    support_id = _stable_id("relation-support", assertion_id, owner_type, owner_id)
    connection.execute(
        """INSERT INTO relation_assertion_supports(
               support_id, assertion_id, owner_type, owner_id, owner_version,
               basis, confidence, status, properties_json, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)
           ON CONFLICT(assertion_id, owner_type, owner_id) DO UPDATE SET
               owner_version=excluded.owner_version, basis=excluded.basis,
               confidence=excluded.confidence, status='accepted',
               properties_json=excluded.properties_json, updated_at=excluded.updated_at""",
        (
            support_id, assertion_id, owner_type, owner_id, str(owner_version).strip(),
            basis, max(0.0, min(1.0, float(confidence))), _json(properties),
            timestamp, timestamp,
        ),
    )
    actual_support_id = str(
        connection.execute(
            """SELECT support_id FROM relation_assertion_supports
               WHERE assertion_id=? AND owner_type=? AND owner_id=?""",
            (assertion_id, owner_type, owner_id),
        ).fetchone()[0]
    )
    connection.execute(
        "DELETE FROM relation_assertion_support_evidence WHERE support_id=?",
        (actual_support_id,),
    )
    for evidence_id in sorted({str(item).strip() for item in evidence_ids if str(item).strip()}):
        connection.execute(
            """INSERT OR IGNORE INTO relation_assertion_support_evidence(support_id, evidence_id)
               VALUES (?, ?)""",
            (actual_support_id, evidence_id),
        )
    return actual_support_id


def retire_relation_support(
    connection: sqlite3.Connection,
    *,
    assertion_id: str,
    owner_type: str,
    owner_id: str,
    timestamp: str | None = None,
) -> bool:
    cursor = connection.execute(
        """UPDATE relation_assertion_supports SET status='retired', updated_at=?
           WHERE assertion_id=? AND owner_type=? AND owner_id=? AND status='accepted'""",
        (timestamp or _now(), assertion_id, owner_type, owner_id),
    )
    return bool(cursor.rowcount)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _sync_endpoint_authority(
    connection: sqlite3.Connection,
    endpoints: set[tuple[str, str, str]],
    *,
    timestamp: str,
) -> tuple[int, int]:
    edge_columns = _table_columns(connection, "entity_edges")
    archived = 0
    changed = 0
    for source_id, target_id, relation in endpoints:
        accepted = connection.execute(
            """SELECT assertion_id, basis, confidence, properties_json,
                      revision, updated_at
               FROM relation_assertions
               WHERE source_entity_id=? AND target_entity_id=? AND relation=?
                 AND status='accepted'
               ORDER BY assertion_id""",
            (source_id, target_id, relation),
        ).fetchall()
        edge_properties_column = (
            "properties" if "properties" in edge_columns
            else "properties_json" if "properties_json" in edge_columns
            else "NULL"
        )
        edges = connection.execute(
            f"""SELECT edge_id, status, basis, confidence, {edge_properties_column}
               FROM entity_edges
               WHERE source_entity_id=? AND target_entity_id=? AND relation=?
               ORDER BY CASE status WHEN 'accepted' THEN 0 ELSE 1 END, edge_id""",
            (source_id, target_id, relation),
        ).fetchall()
        if not accepted:
            for edge in edges:
                if str(edge[1]) == "accepted":
                    connection.execute(
                        "UPDATE entity_edges SET status='archived', updated_at=? WHERE edge_id=?",
                        (timestamp, edge[0]),
                    )
                    archived += 1
            continue
        def authority_rank(row: sqlite3.Row) -> tuple[object, ...]:
            properties = _relation_properties(row[3])
            context_ids = properties.get("context_evidence_ids", ())
            if not isinstance(context_ids, (list, tuple, set)):
                context_ids = ()
            requested = sorted(
                {str(value).strip() for value in context_ids if str(value).strip()}
            )
            existing_context: list[str] = []
            if requested:
                placeholders = ",".join("?" for _ in requested)
                existing_context = [
                    str(item[0])
                    for item in connection.execute(
                        f"""SELECT evidence_id FROM evidence_records
                            WHERE evidence_id IN ({placeholders})
                            ORDER BY evidence_id""",
                        requested,
                    )
                ]
            exact_ids = _assertion_evidence(connection, str(row[0]))
            basis = str(row[1])
            strength = (
                4 if basis == "reviewed" and exact_ids
                else 3 if basis == "book" and exact_ids
                else 2 if basis in {"model", "derived"} and existing_context
                else 1
            )
            return (
                strength,
                float(row[2]),
                {"reviewed": 4, "book": 3, "derived": 2, "model": 1}.get(
                    basis, 0
                ),
                int(row[4]),
                str(row[5]),
                str(row[0]),
            )

        preferred = max(accepted, key=authority_rank)
        basis = str(preferred[1])
        confidence = float(preferred[2])
        properties_json = _json(_relation_properties(preferred[3]))
        if edges:
            edge_id = str(edges[0][0])
            current = edges[0]
            edge_differs = (
                str(current[1]) != "accepted"
                or str(current[2]) != basis
                or abs(float(current[3]) - confidence) > 1e-12
                or str(current[4] or "{}") != properties_json
            )
            if edge_differs:
                assignments = ["basis=?", "confidence=?", "status='accepted'"]
                values: list[Any] = [basis, confidence]
                if edge_properties_column != "NULL":
                    assignments.append(f"{edge_properties_column}=?")
                    values.append(properties_json)
                if "updated_at" in edge_columns:
                    assignments.append("updated_at=?")
                    values.append(timestamp)
                values.append(edge_id)
                connection.execute(
                    f"UPDATE entity_edges SET {', '.join(assignments)} WHERE edge_id=?",
                    values,
                )
                changed += 1
            for duplicate in edges[1:]:
                if str(duplicate[1]) == "accepted":
                    connection.execute(
                        "UPDATE entity_edges SET status='archived', updated_at=? WHERE edge_id=?",
                        (timestamp, duplicate[0]),
                    )
                    archived += 1
            continue
        fields = ["edge_id", "source_entity_id", "target_entity_id", "relation",
                  "basis", "confidence", "status"]
        values: list[Any] = [_stable_id("edge", source_id, target_id, relation),
                             source_id, target_id, relation, basis, confidence, "accepted"]
        if edge_properties_column != "NULL":
            fields.append(edge_properties_column)
            values.append(properties_json)
        for column in ("created_at", "updated_at"):
            if column in edge_columns:
                fields.append(column)
                values.append(timestamp)
        connection.execute(
            f"INSERT INTO entity_edges({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            values,
        )
        changed += 1
    return archived, changed


def reduce_relation_assertions(
    connection: sqlite3.Connection,
    assertion_ids: Iterable[str],
    *,
    timestamp: str | None = None,
) -> dict[str, int]:
    """Reduce supports, refusing to retire unattributed legacy assertions."""
    timestamp = timestamp or _now()
    result = {"assertions_updated": 0, "assertions_archived": 0,
              "unowned_legacy_skipped": 0, "edges_archived": 0,
              "edges_changed": 0, "term_morphemes_removed": 0}
    endpoints: set[tuple[str, str, str]] = set()
    maybe_remove: set[tuple[str, str]] = set()
    for assertion_id in sorted({str(item).strip() for item in assertion_ids if str(item).strip()}):
        assertion = connection.execute(
            """SELECT assertion_id, source_entity_id, target_entity_id, relation,
                      basis, confidence, status, properties_json
               FROM relation_assertions WHERE assertion_id=?""",
            (assertion_id,),
        ).fetchone()
        if assertion is None:
            continue
        source_id, target_id, relation = str(assertion[1]), str(assertion[2]), str(assertion[3])
        supports = connection.execute(
            """SELECT support_id, basis, confidence, properties_json
               FROM relation_assertion_supports
               WHERE assertion_id=? AND status='accepted'
               ORDER BY CASE basis
                            WHEN 'reviewed' THEN 0 WHEN 'book' THEN 1
                            WHEN 'model' THEN 2 ELSE 3 END,
                        confidence DESC, support_id""",
            (assertion_id,),
        ).fetchall()
        history_count = int(connection.execute(
            "SELECT COUNT(*) FROM relation_assertion_supports WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()[0])
        if not supports:
            if history_count == 0:
                result["unowned_legacy_skipped"] += 1
                continue
            if str(assertion[6]) != "archived":
                connection.execute(
                    """UPDATE relation_assertions SET status='archived', revision=revision+1,
                              updated_at=? WHERE assertion_id=?""",
                    (timestamp, assertion_id),
                )
                connection.execute(
                    "DELETE FROM assertion_evidence WHERE assertion_id=?", (assertion_id,)
                )
                result["assertions_archived"] += 1
            endpoints.add((source_id, target_id, relation))
            if relation == "has-component":
                maybe_remove.add((source_id, target_id))
            continue
        preferred = supports[0]
        support_bases = {str(row[1]) for row in supports}
        basis = (
            "reviewed" if "reviewed" in support_bases
            else "book" if "book" in support_bases
            else "model" if "model" in support_bases
            else "derived"
        )
        confidence = max(float(row[2]) for row in supports)
        properties_json = str(preferred[3] or "{}")
        desired_evidence = {
            str(evidence[0])
            for evidence in connection.execute(
            """SELECT DISTINCT se.evidence_id FROM relation_assertion_supports s
               JOIN relation_assertion_support_evidence se ON se.support_id=s.support_id
               WHERE s.assertion_id=? AND s.status='accepted' ORDER BY se.evidence_id""",
            (assertion_id,),
            )
        }
        current_evidence = {
            str(row[0]) for row in connection.execute(
                "SELECT evidence_id FROM assertion_evidence WHERE assertion_id=?",
                (assertion_id,),
            )
        }
        assertion_differs = (
            str(assertion[4]) != basis
            or abs(float(assertion[5]) - confidence) > 1e-12
            or str(assertion[6]) != "accepted"
            or str(assertion[7] or "{}") != properties_json
            or current_evidence != desired_evidence
        )
        if assertion_differs:
            connection.execute(
                """UPDATE relation_assertions SET basis=?, confidence=?, properties_json=?,
                          status='accepted', revision=revision+1, updated_at=?
                   WHERE assertion_id=?""",
                (basis, confidence, properties_json, timestamp, assertion_id),
            )
            connection.execute(
                "DELETE FROM assertion_evidence WHERE assertion_id=?", (assertion_id,)
            )
            for evidence_id in sorted(desired_evidence):
                connection.execute(
                    """INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id)
                       VALUES (?, ?)""",
                    (assertion_id, evidence_id),
                )
            result["assertions_updated"] += 1
        endpoints.add((source_id, target_id, relation))
    result["edges_archived"], result["edges_changed"] = _sync_endpoint_authority(
        connection, endpoints, timestamp=timestamp
    )
    for term_id, morpheme_id in maybe_remove:
        still_active = connection.execute(
            """SELECT 1 FROM relation_assertions WHERE source_entity_id=?
               AND target_entity_id=? AND relation='has-component'
               AND status='accepted' LIMIT 1""",
            (term_id, morpheme_id),
        ).fetchone()
        if still_active is None:
            cursor = connection.execute(
                "DELETE FROM term_morphemes WHERE term_id=? AND morpheme_id=?",
                (term_id, morpheme_id),
            )
            result["term_morphemes_removed"] += max(0, int(cursor.rowcount))
    return result


def replace_relation_support_scope(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: str,
    owner_version: str = "",
    supports: Iterable[RelationSupportSpec],
    timestamp: str | None = None,
) -> dict[str, int]:
    """Atomically replace every support in one logical producer scope."""
    timestamp = timestamp or _now()
    owner_type, owner_id = str(owner_type).strip(), str(owner_id).strip()
    if not owner_type or not owner_id:
        raise ValueError("owner scope must be non-empty")
    merged: dict[str, RelationSupportSpec] = {}
    for spec in supports:
        prior = merged.get(spec.assertion_id)
        if prior is None:
            merged[spec.assertion_id] = spec
            continue
        rank = {"derived": 0, "model": 1, "book": 2, "reviewed": 3}
        preferred = spec if (rank.get(spec.basis, -1), spec.confidence) > (
            rank.get(prior.basis, -1), prior.confidence
        ) else prior
        merged[spec.assertion_id] = RelationSupportSpec(
            spec.assertion_id,
            max((prior.basis, spec.basis), key=lambda value: rank.get(value, -1)),
            max(float(prior.confidence), float(spec.confidence)), preferred.properties,
            tuple(sorted({*prior.evidence_ids, *spec.evidence_ids})),
        )
    desired_ids = set(merged)
    existing_ids = {str(row[0]) for row in connection.execute(
        """SELECT assertion_id FROM relation_assertion_supports
           WHERE owner_type=? AND owner_id=? AND status='accepted'""",
        (owner_type, owner_id),
    )}
    retired = sum(int(retire_relation_support(
        connection, assertion_id=assertion_id, owner_type=owner_type,
        owner_id=owner_id, timestamp=timestamp,
    )) for assertion_id in sorted(existing_ids - desired_ids))
    for spec in merged.values():
        upsert_relation_support(
            connection, assertion_id=spec.assertion_id, owner_type=owner_type,
            owner_id=owner_id, owner_version=owner_version, basis=spec.basis,
            confidence=spec.confidence, properties=spec.properties,
            evidence_ids=spec.evidence_ids, timestamp=timestamp,
        )
    payload = [{"assertion_id": spec.assertion_id, "basis": spec.basis,
                "confidence": round(float(spec.confidence), 8),
                "properties": dict(spec.properties),
                "evidence_ids": sorted(set(spec.evidence_ids))}
               for spec in sorted(merged.values(), key=lambda item: item.assertion_id)]
    fingerprint = hashlib.sha256(_json({"supports": payload}).encode()).hexdigest()
    connection.execute(
        """INSERT INTO relation_support_owner_scopes(
               owner_type, owner_id, owner_version, desired_count,
               desired_fingerprint, updated_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(owner_type, owner_id) DO UPDATE SET
               owner_version=excluded.owner_version, desired_count=excluded.desired_count,
               desired_fingerprint=excluded.desired_fingerprint, updated_at=excluded.updated_at""",
        (owner_type, owner_id, str(owner_version).strip(), len(merged), fingerprint, timestamp),
    )
    result = reduce_relation_assertions(connection, existing_ids | desired_ids, timestamp=timestamp)
    result.update({"supports_upserted": len(merged), "supports_retired": retired})
    return result


def morphology_support_migration_report(
    connection: sqlite3.Connection,
    *,
    parser_version: str,
    expected_source_records: int | None = None,
) -> dict[str, Any]:
    """Return a non-destructive, refusal-oriented legacy coverage manifest."""
    parser_scopes = int(connection.execute(
        """SELECT COUNT(*) FROM relation_support_owner_scopes
           WHERE owner_type='morphology-parser' AND owner_version=?""",
        (parser_version,),
    ).fetchone()[0])
    accepted_unowned = int(connection.execute(
        """SELECT COUNT(*) FROM relation_assertions a WHERE a.status='accepted'
           AND a.relation IN ('has-component', 'shares-component')
           AND EXISTS (SELECT 1 FROM morphemes m WHERE m.entity_id=a.target_entity_id)
           AND NOT EXISTS (SELECT 1 FROM relation_assertion_supports s
               WHERE s.assertion_id=a.assertion_id AND s.status='accepted')"""
    ).fetchone()[0])
    evidence_mismatch = int(connection.execute(
        """SELECT COUNT(*) FROM (
             SELECT a.assertion_id FROM relation_assertions a
             LEFT JOIN assertion_evidence ae ON ae.assertion_id=a.assertion_id
             WHERE a.status='accepted'
               AND a.relation IN ('has-component', 'shares-component')
               AND EXISTS (SELECT 1 FROM morphemes m WHERE m.entity_id=a.target_entity_id)
             GROUP BY a.assertion_id
             HAVING COALESCE(GROUP_CONCAT(DISTINCT ae.evidence_id), '') != COALESCE((
                 SELECT GROUP_CONCAT(DISTINCT se.evidence_id)
                 FROM relation_assertion_supports s
                 LEFT JOIN relation_assertion_support_evidence se ON se.support_id=s.support_id
                 WHERE s.assertion_id=a.assertion_id AND s.status='accepted'), '')
           )"""
    ).fetchone()[0])
    scope_complete = expected_source_records is not None and parser_scopes == expected_source_records
    ready = bool(scope_complete and accepted_unowned == 0 and evidence_mismatch == 0)
    manifest: dict[str, Any] = {
        "parser_version": parser_version, "expected_source_records": expected_source_records,
        "parser_scopes": parser_scopes, "parser_scope_complete": scope_complete,
        "accepted_assertions_without_active_owner": accepted_unowned,
        "assertion_evidence_mismatches": evidence_mismatch,
        "destructive_ready": ready,
    }
    manifest["coverage_fingerprint"] = hashlib.sha256(_json(manifest).encode()).hexdigest()
    return manifest


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def morpheme_alias_migration_manifest(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Plan same-role edge-hyphen alias coalescing without changing data.

    The plan is deliberately ownership-gated. It identifies every endpoint and
    support merge needed for a deterministic survivor, while unknown entity
    references or accepted zero-ledger assertions make the plan non-executable.
    Cross-role homographs are separate groups by construction.
    """
    from .morphology_contract import canonical_morpheme_identity

    rows = connection.execute(
        """SELECT m.entity_id, m.language, m.form, m.normalized, m.kind,
                  e.canonical_key, e.status, COALESCE(e.quality_score, 0.0)
           FROM morphemes m JOIN entities e ON e.entity_id=m.entity_id
           WHERE e.status='accepted' ORDER BY m.entity_id"""
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        try:
            base, display, canonical_key = canonical_morpheme_identity(
                row[1], row[2] or row[3], row[4]
            )
        except ValueError:
            continue
        grouped.setdefault((str(row[1]).casefold(), str(row[4]).casefold(), base), []).append(
            {
                "entity_id": str(row[0]),
                "form": str(row[2]),
                "normalized": str(row[3]),
                "canonical_key": str(row[5]),
                "status": str(row[6]),
                "quality_score": float(row[7]),
                "desired_form": display,
                "desired_canonical_key": canonical_key,
            }
        )

    alias_groups = {
        key: members for key, members in grouped.items() if len(members) > 1
    }
    alias_ids: set[str] = set()
    for members in alias_groups.values():
        ordered = sorted(
            members,
            key=lambda member: (
                member["canonical_key"] != member["desired_canonical_key"],
                -float(member["quality_score"]), member["entity_id"],
            ),
        )
        alias_ids.update(member["entity_id"] for member in ordered[1:])
    supported_entity_references = {
        ("morphemes", "entity_id"),
        ("relation_assertions", "subject_entity_id"),
        ("relation_assertions", "source_entity_id"),
        ("relation_assertions", "target_entity_id"),
        ("entity_edges", "source_entity_id"),
        ("entity_edges", "target_entity_id"),
        ("term_morphemes", "morpheme_id"),
        ("entity_evidence", "entity_id"),
        ("lexical_semantic_enrichments", "entity_id"),
        ("meanings", "subject_entity_id"),
        ("morphology_component_observations_v3", "materialized_entity_id"),
    }
    retained_audit_references = {
        ("entity_revisions", "entity_id"),
        ("morphology_pilot_repairs_v3", "legacy_source_entity_id"),
        ("morphology_pilot_repairs_v3", "legacy_target_entity_id"),
        ("morphology_pilot_repairs_v3", "replacement_morpheme_id"),
    }
    unknown_reference_entities: list[dict[str, Any]] = []
    if alias_ids:
        placeholders = ", ".join("?" for _ in alias_ids)
        for table_row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
        ):
            table = str(table_row[0])
            for foreign_key in connection.execute(
                f"PRAGMA foreign_key_list({_quoted_identifier(table)})"
            ):
                referenced_table = str(foreign_key[2])
                source_column = str(foreign_key[3])
                target_column = str(foreign_key[4])
                if referenced_table != "entities" or target_column != "entity_id":
                    continue
                if (table, source_column) in (
                    supported_entity_references | retained_audit_references
                ):
                    continue
                references = connection.execute(
                    f"""SELECT {_quoted_identifier(source_column)}, COUNT(*)
                        FROM {_quoted_identifier(table)}
                        WHERE {_quoted_identifier(source_column)} IN ({placeholders})
                        GROUP BY {_quoted_identifier(source_column)}
                        ORDER BY {_quoted_identifier(source_column)}""",
                    tuple(sorted(alias_ids)),
                ).fetchall()
                for reference in references:
                    entity_id = str(reference[0])
                    if table == "preparation_jobs" and source_column == "subject_entity_id":
                        active = int(connection.execute(
                            """SELECT COUNT(*) FROM preparation_jobs
                               WHERE subject_entity_id=?
                                 AND status NOT IN ('complete','failed')""",
                            (entity_id,),
                        ).fetchone()[0])
                        if active == 0:
                            continue
                    unknown_reference_entities.append(
                        {"table": table, "column": source_column,
                         "entity_id": entity_id, "row_count": int(reference[1])}
                    )

    unknown_reference_counts: dict[tuple[str, str], int] = {}
    for item in unknown_reference_entities:
        key = (str(item["table"]), str(item["column"]))
        unknown_reference_counts[key] = (
            unknown_reference_counts.get(key, 0) + int(item["row_count"])
        )
    unknown_references = [
        {"table": table, "column": column, "row_count": count}
        for (table, column), count in sorted(unknown_reference_counts.items())
    ]

    plans: list[dict[str, Any]] = []
    zero_ledger_assertions: set[str] = set()
    for (language, kind, base), members in sorted(alias_groups.items()):
        ordered = sorted(
            members,
            key=lambda member: (
                member["canonical_key"] != member["desired_canonical_key"],
                -float(member["quality_score"]),
                member["entity_id"],
            ),
        )
        survivor = ordered[0]
        aliases = ordered[1:]
        group_ids = {member["entity_id"] for member in ordered}
        alias_only_ids = {member["entity_id"] for member in aliases}
        assertion_actions: list[dict[str, Any]] = []
        edge_actions: list[dict[str, Any]] = []
        term_actions: list[dict[str, Any]] = []
        evidence_actions: list[dict[str, Any]] = []
        group_zero_ledger: set[str] = set()

        if alias_only_ids:
            placeholders = ", ".join("?" for _ in alias_only_ids)
            assertions = connection.execute(
                f"""SELECT assertion_id, subject_entity_id, source_entity_id,
                           target_entity_id, relation, status
                    FROM relation_assertions
                    WHERE subject_entity_id IN ({placeholders})
                       OR source_entity_id IN ({placeholders})
                       OR target_entity_id IN ({placeholders})
                    ORDER BY assertion_id""",
                (*sorted(alias_only_ids), *sorted(alias_only_ids), *sorted(alias_only_ids)),
            ).fetchall()
            for assertion in assertions:
                assertion_id = str(assertion[0])
                desired_subject = (
                    survivor["entity_id"] if str(assertion[1]) in group_ids else str(assertion[1])
                )
                desired_source = (
                    survivor["entity_id"] if str(assertion[2]) in group_ids else str(assertion[2])
                )
                desired_target = (
                    survivor["entity_id"] if str(assertion[3]) in group_ids else str(assertion[3])
                )
                collision = connection.execute(
                    """SELECT assertion_id, status FROM relation_assertions
                       WHERE assertion_id<>? AND subject_entity_id=?
                         AND source_entity_id=? AND target_entity_id=? AND relation=?
                       ORDER BY CASE status WHEN 'accepted' THEN 0 ELSE 1 END,
                                assertion_id LIMIT 1""",
                    (
                        assertion_id, desired_subject, desired_source, desired_target,
                        str(assertion[4]),
                    ),
                ).fetchone()
                active_owners = [
                    (str(owner[0]), str(owner[1]))
                    for owner in connection.execute(
                        """SELECT owner_type, owner_id
                           FROM relation_assertion_supports
                           WHERE assertion_id=? AND status='accepted'
                           ORDER BY owner_type, owner_id""",
                        (assertion_id,),
                    )
                ]
                if str(assertion[5]) == "accepted" and not active_owners:
                    zero_ledger_assertions.add(assertion_id)
                    group_zero_ledger.add(assertion_id)
                collision_owners: list[tuple[str, str]] = []
                if collision:
                    collision_id = str(collision[0])
                    collision_owners = [
                        (str(owner[0]), str(owner[1]))
                        for owner in connection.execute(
                            """SELECT owner_type, owner_id
                               FROM relation_assertion_supports
                               WHERE assertion_id=? AND status='accepted'
                               ORDER BY owner_type, owner_id""",
                            (collision_id,),
                        )
                    ]
                    if str(collision[1]) == "accepted" and not collision_owners:
                        zero_ledger_assertions.add(collision_id)
                        group_zero_ledger.add(collision_id)
                assertion_actions.append(
                    {
                        "assertion_id": assertion_id,
                        "desired_subject_entity_id": desired_subject,
                        "desired_source_entity_id": desired_source,
                        "desired_target_entity_id": desired_target,
                        "relation": str(assertion[4]),
                        "merge_into_assertion_id": str(collision[0]) if collision else "",
                        "active_owners": [list(owner) for owner in active_owners],
                        "merge_target_active_owners": [
                            list(owner) for owner in collision_owners
                        ],
                    }
                )

            if _table_exists(connection, "entity_edges"):
                edges = connection.execute(
                    f"""SELECT edge_id, source_entity_id, target_entity_id, relation
                        FROM entity_edges
                        WHERE source_entity_id IN ({placeholders})
                           OR target_entity_id IN ({placeholders})
                        ORDER BY edge_id""",
                    (*sorted(alias_only_ids), *sorted(alias_only_ids)),
                ).fetchall()
                for edge in edges:
                    desired_source = (
                        survivor["entity_id"] if str(edge[1]) in group_ids else str(edge[1])
                    )
                    desired_target = (
                        survivor["entity_id"] if str(edge[2]) in group_ids else str(edge[2])
                    )
                    collision = connection.execute(
                        """SELECT edge_id FROM entity_edges WHERE edge_id<>?
                           AND source_entity_id=? AND target_entity_id=? AND relation=?
                           ORDER BY CASE status WHEN 'accepted' THEN 0 ELSE 1 END,
                                    edge_id LIMIT 1""",
                        (edge[0], desired_source, desired_target, edge[3]),
                    ).fetchone()
                    edge_actions.append(
                        {
                            "edge_id": str(edge[0]),
                            "desired_source_entity_id": desired_source,
                            "desired_target_entity_id": desired_target,
                            "relation": str(edge[3]),
                            "merge_into_edge_id": str(collision[0]) if collision else "",
                        }
                    )

            if _table_exists(connection, "term_morphemes"):
                for component in connection.execute(
                    f"""SELECT term_id, morpheme_id, ordinal, surface
                        FROM term_morphemes WHERE morpheme_id IN ({placeholders})
                        ORDER BY term_id, ordinal, morpheme_id""",
                    tuple(sorted(alias_only_ids)),
                ):
                    term_actions.append(
                        {
                            "term_id": str(component[0]),
                            "from_morpheme_id": str(component[1]),
                            "to_morpheme_id": survivor["entity_id"],
                            "ordinal": int(component[2]),
                            "surface": str(component[3]),
                            "merge_existing": False,
                        }
                    )

            if _table_exists(connection, "entity_evidence"):
                for alias_id in sorted(alias_only_ids):
                    count = int(connection.execute(
                        "SELECT COUNT(*) FROM entity_evidence WHERE entity_id=?",
                        (alias_id,),
                    ).fetchone()[0])
                    if count:
                        evidence_actions.append(
                            {
                                "from_entity_id": alias_id,
                                "to_entity_id": survivor["entity_id"],
                                "row_count": count,
                            }
                        )

        group_unknown_counts: dict[tuple[str, str], int] = {}
        for item in unknown_reference_entities:
            if item["entity_id"] not in alias_only_ids:
                continue
            key = (str(item["table"]), str(item["column"]))
            group_unknown_counts[key] = (
                group_unknown_counts.get(key, 0) + int(item["row_count"])
            )
        group_unknown = [
            {"table": table, "column": column, "row_count": count}
            for (table, column), count in sorted(group_unknown_counts.items())
        ]
        plans.append(
            {
                "language": language,
                "kind": kind,
                "base": base,
                "survivor_entity_id": survivor["entity_id"],
                "desired_form": survivor["desired_form"],
                "desired_canonical_key": survivor["desired_canonical_key"],
                "archive_entity_ids": [member["entity_id"] for member in aliases],
                "assertion_actions": assertion_actions,
                "edge_actions": edge_actions,
                "term_morpheme_actions": term_actions,
                "entity_evidence_actions": evidence_actions,
                "unknown_entity_references": group_unknown,
                "zero_ledger_assertion_ids": sorted(group_zero_ledger),
                "deferred": bool(group_unknown or group_zero_ledger),
            }
        )

    manifest: dict[str, Any] = {
        "dry_run": True,
        "group_count": len(plans),
        "alias_entity_count": sum(len(plan["archive_entity_ids"]) for plan in plans),
        "groups": plans,
        "zero_ledger_assertion_ids": sorted(zero_ledger_assertions),
        "unknown_entity_references": unknown_references,
        "executable_group_count": sum(not plan["deferred"] for plan in plans),
        "deferred_group_count": sum(bool(plan["deferred"]) for plan in plans),
        "destructive_ready": not zero_ledger_assertions and not unknown_references,
    }
    manifest["coverage_fingerprint"] = hashlib.sha256(
        _json(manifest).encode()
    ).hexdigest()
    return manifest


MORPHOLOGY_V7_MIGRATION_KEY = "morphology-support-finalize-v7-r2"
PRODUCTION_MORPHOLOGY_RECORD_COUNT = 11_516


def morphology_expected_sources(
    indexes: Sequence[tuple[str, Path]],
) -> tuple[tuple[str, str, str], ...]:
    """Read immutable logical source keys from reviewed morphology indexes."""

    expected: list[tuple[str, str, str]] = []
    for source_kind, raw_path in indexes:
        if source_kind not in {"root", "affix"}:
            raise ValueError(f"unsupported morphology source kind: {source_kind!r}")
        path = Path(raw_path).resolve()
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as source:
            metadata = {
                str(row[0]): str(row[1])
                for row in source.execute("SELECT key, value FROM metadata")
            }
            corpus_id = metadata.get("corpus_id", "").strip()
            source_hash = metadata.get("source_sha256", "").strip()
            if not corpus_id or not source_hash:
                raise ValueError(f"morphology index metadata is incomplete: {path}")
            expected.extend(
                (corpus_id, str(row[0]), source_hash)
                for row in source.execute(
                    "SELECT record_id FROM records ORDER BY row_id"
                )
            )
    return tuple(expected)


def _arbitrary_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_migration_sources(
    expected_sources: Sequence[tuple[str, str, str]],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (str(corpus).strip(), str(record).strip(), str(source_hash).strip())
        for corpus, record, source_hash in expected_sources
    )


def _morphology_migration_input_fingerprint(
    *,
    migration_key: str,
    parser_version: str,
    expected_sources: Sequence[tuple[str, str, str]],
) -> str:
    immutable_input = {
        "migration_key": migration_key,
        "parser_version": parser_version,
        "expected_sources": sorted(_normalized_migration_sources(expected_sources)),
    }
    return hashlib.sha256(_arbitrary_json(immutable_input).encode()).hexdigest()


def _completed_morphology_migration_result(
    connection: sqlite3.Connection,
    *,
    migration_key: str,
    parser_version: str,
    expected_sources: Sequence[tuple[str, str, str]],
    dry_run: bool,
) -> dict[str, Any] | None:
    """Return a completed migration without rebuilding a mutable-state plan."""

    if not _table_exists(connection, "relation_support_migrations"):
        return None
    row = connection.execute(
        """SELECT coverage_fingerprint, manifest_json, destructive_ready,
                  completed_at
           FROM relation_support_migrations WHERE migration_key=?""",
        (migration_key,),
    ).fetchone()
    if row is None or not str(row[3] or ""):
        return None
    expected_fingerprint = _morphology_migration_input_fingerprint(
        migration_key=migration_key,
        parser_version=parser_version,
        expected_sources=expected_sources,
    )
    if str(row[0]) != expected_fingerprint:
        return None
    try:
        stored = json.loads(str(row[1]))
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = None
    if not isinstance(stored, dict) or not bool(row[2]):
        return {
            "migration_key": migration_key,
            "parser_version": parser_version,
            "coverage_fingerprint": expected_fingerprint,
            "completed_at": str(row[3]),
            "already_complete": True,
            "destructive_ready": False,
            "refusal_reasons": ["completed migration record is not reusable"],
            "dry_run": dry_run,
            "applied": False,
            "changes": {},
        }
    try:
        _assert_morphology_finalization_invariants(connection)
        stored_occurrences = stored.get("complete_atomic_occurrence_fingerprints")
        if not isinstance(stored_occurrences, dict):
            raise RuntimeError("completed migration lacks exact occurrence fingerprints")
        current_occurrences = _complete_atomic_occurrence_fingerprints(
            connection, stored_occurrences
        )
        if current_occurrences != {
            str(key): str(value) for key, value in stored_occurrences.items()
        }:
            raise RuntimeError("complete atomic occurrence projection drifted")
    except RuntimeError as exc:
        return {
            **stored,
            "migration_key": migration_key,
            "parser_version": parser_version,
            "coverage_fingerprint": expected_fingerprint,
            "completed_at": str(row[3]),
            "already_complete": True,
            "destructive_ready": False,
            "refusal_reasons": [
                "completed migration state drifted; apply a new repair epoch: "
                + str(exc)
            ],
            "dry_run": dry_run,
            "applied": False,
            "changes": {},
        }
    return {
        **stored,
        "migration_key": migration_key,
        "parser_version": parser_version,
        "coverage_fingerprint": expected_fingerprint,
        "completed_at": str(row[3]),
        "already_complete": True,
        "destructive_ready": True,
        "refusal_reasons": [],
        "dry_run": dry_run,
        "applied": False,
        "changes": {},
    }


def _scope_fingerprint(specs: Iterable[RelationSupportSpec]) -> str:
    payload = [
        {
            "assertion_id": spec.assertion_id,
            "basis": spec.basis,
            "confidence": round(float(spec.confidence), 8),
            "properties": dict(spec.properties),
            "evidence_ids": sorted({str(item) for item in spec.evidence_ids}),
        }
        for spec in sorted(specs, key=lambda item: item.assertion_id)
    ]
    return hashlib.sha256(_json({"supports": payload}).encode()).hexdigest()


def _relation_properties(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _support_evidence(
    connection: sqlite3.Connection, support_id: str
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT evidence_id FROM relation_assertion_support_evidence
               WHERE support_id=? ORDER BY evidence_id""",
            (support_id,),
        )
    )


def _assertion_evidence(
    connection: sqlite3.Connection, assertion_id: str
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT evidence_id FROM assertion_evidence
               WHERE assertion_id=? ORDER BY evidence_id""",
            (assertion_id,),
        )
    )


def _merge_specs(
    prior: RelationSupportSpec | None, candidate: RelationSupportSpec
) -> RelationSupportSpec:
    if prior is None:
        return candidate
    rank = {"derived": 0, "model": 1, "book": 2, "reviewed": 3}
    preferred = max(
        (prior, candidate),
        key=lambda item: (rank.get(item.basis, -1), float(item.confidence)),
    )
    return RelationSupportSpec(
        assertion_id=candidate.assertion_id,
        basis=max(
            (prior.basis, candidate.basis), key=lambda value: rank.get(value, -1)
        ),
        confidence=max(float(prior.confidence), float(candidate.confidence)),
        properties=preferred.properties,
        evidence_ids=tuple(sorted({*prior.evidence_ids, *candidate.evidence_ids})),
    )


def _atomic_split_projection(payload: object) -> dict[str, Any] | None:
    """Extract only explicit structural claims from an accepted split payload."""

    if not isinstance(payload, dict):
        return None
    term_id = str(payload.get("term_id", "")).strip()
    raw_parts = payload.get("parts")
    raw_related = payload.get("related_terms", [])
    if not term_id or not isinstance(raw_parts, list) or not raw_parts:
        return None
    if not isinstance(raw_related, list):
        return None
    parts: list[dict[str, Any]] = []
    ordinals: set[int] = set()
    for raw in raw_parts:
        if not isinstance(raw, dict):
            return None
        morpheme_id = str(raw.get("morpheme_id", "")).strip()
        canonical_form = str(raw.get("canonical_form", "")).strip()
        surface = str(raw.get("surface", "")).strip()
        kind = str(raw.get("kind", "")).strip().casefold()
        basis = str(raw.get("basis", "")).strip().casefold()
        ordinal = raw.get("ordinal")
        confidence = raw.get("confidence")
        evidence = raw.get("evidence_ids", [])
        context = raw.get("context_evidence_ids", [])
        if (
            not morpheme_id
            or not canonical_form
            or not surface
            or kind not in {"prefix", "root", "suffix", "free"}
            or basis not in {"book", "model", "reviewed", "derived"}
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal in ordinals
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
            or not isinstance(evidence, list)
            or not isinstance(context, list)
            or any(not str(value).strip() for value in evidence + context)
        ):
            return None
        ordinals.add(ordinal)
        parts.append(
            {
                "morpheme_id": morpheme_id,
                "canonical_form": canonical_form,
                "surface": surface,
                "kind": kind,
                "language": str(raw.get("language", "")).strip().casefold(),
                "meaning": str(raw.get("meaning", "")).strip(),
                "basis": basis,
                "confidence": float(confidence),
                "ordinal": ordinal,
                "evidence_ids": sorted({str(value).strip() for value in evidence}),
                "context_evidence_ids": sorted(
                    {str(value).strip() for value in context}
                ),
            }
        )
    related: list[dict[str, Any]] = []
    for raw in raw_related:
        if not isinstance(raw, dict):
            return None
        related_term_id = str(raw.get("term_id", "")).strip()
        component_ids = raw.get("component_ids", [])
        if not isinstance(component_ids, list) or any(
            not str(value).strip() for value in component_ids
        ):
            return None
        normalized_component_ids = sorted(
            {str(value).strip() for value in component_ids}
        )
        # Older accepted payloads retained derivative suggestions for which no
        # component relation was accepted. They carry no structural claim.
        if not normalized_component_ids:
            continue
        if not related_term_id:
            return None
        related.append(
            {
                "term_id": related_term_id,
                "term": str(raw.get("term", "")).strip(),
                "component_ids": normalized_component_ids,
                "note": str(raw.get("note", "")).strip(),
            }
        )
    return {
        "term_id": term_id,
        "term": str(payload.get("term", "")).strip(),
        "parts": sorted(parts, key=lambda item: item["ordinal"]),
        "related_terms": sorted(
            related,
            key=lambda item: (item["term_id"], tuple(item["component_ids"])),
        ),
    }


def _canonical_morpheme_id_map(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    """Map archived same-role aliases to one accepted canonical identity."""

    from .morphology_contract import canonical_morpheme_identity

    identities: dict[str, tuple[str, str, str, str]] = {}
    accepted: dict[tuple[str, str, str], list[str]] = {}
    for row in connection.execute(
        """SELECT m.entity_id,m.language,m.form,m.normalized,m.kind,e.status
           FROM morphemes m JOIN entities e ON e.entity_id=m.entity_id
           ORDER BY m.entity_id"""
    ):
        entity_id = str(row[0])
        language = str(row[1]).casefold()
        kind = str(row[4]).casefold()
        try:
            base = canonical_morpheme_identity(
                language, row[2] or row[3], kind
            )[0]
        except ValueError:
            continue
        identities[entity_id] = (language, kind, base, str(row[5]))
        if str(row[5]) == "accepted":
            accepted.setdefault((language, kind, base), []).append(entity_id)
    resolved: dict[str, str] = {}
    for entity_id, (language, kind, base, status) in identities.items():
        if status == "accepted":
            resolved[entity_id] = entity_id
            continue
        candidates = accepted.get((language, kind, base), [])
        if len(candidates) == 1:
            resolved[entity_id] = candidates[0]
    return resolved


def _resolve_atomic_projection_aliases(
    projection: dict[str, Any] | None,
    canonical_ids: Mapping[str, str],
) -> dict[str, Any] | None:
    """Project immutable payload IDs through proven same-role alias identity."""

    if projection is None:
        return None
    parts: list[dict[str, Any]] = []
    for part in projection["parts"]:
        morpheme_id = canonical_ids.get(str(part["morpheme_id"]))
        if not morpheme_id:
            return None
        parts.append({**part, "morpheme_id": morpheme_id})
    related: list[dict[str, Any]] = []
    for item in projection["related_terms"]:
        component_ids: set[str] = set()
        for component_id in item["component_ids"]:
            resolved = canonical_ids.get(str(component_id))
            if not resolved:
                return None
            component_ids.add(resolved)
        related.append({**item, "component_ids": sorted(component_ids)})
    return {**projection, "parts": parts, "related_terms": related}


def _atomic_part_properties(part: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ordinal": int(part["ordinal"]),
        "surface": str(part["surface"]),
        "component_kind": str(part["kind"]),
        "canonical_form": str(part["canonical_form"]),
        "meaning": str(part["meaning"]),
        "context_evidence_ids": list(part["context_evidence_ids"]),
        "modes": [
            "knowledge", "word",
            "root" if part["kind"] in {"root", "free"} else "affix",
        ],
    }


def _valid_atomic_projection(
    connection: sqlite3.Connection, projection: Mapping[str, Any],
    *, prospective_term_ids: Iterable[str] = (),
    allow_nonaccepted_related: bool = False,
) -> bool:
    from .morphology_contract import canonical_morpheme_identity

    subject_id = str(projection["term_id"])
    prospective = {str(item) for item in prospective_term_ids}
    subject = connection.execute(
        """SELECT e.status FROM terms t JOIN entities e ON e.entity_id=t.entity_id
           WHERE t.entity_id=?""", (subject_id,),
    ).fetchone()
    if subject is None or (
        str(subject[0]) != "accepted" and subject_id not in prospective
    ):
        return False
    part_ids = {str(part["morpheme_id"]) for part in projection["parts"]}
    for part in projection["parts"]:
        morpheme = connection.execute(
            """SELECT m.language, m.form, m.normalized, m.kind, e.status
               FROM morphemes m JOIN entities e ON e.entity_id=m.entity_id
               WHERE m.entity_id=?""", (part["morpheme_id"],),
        ).fetchone()
        if morpheme is None or str(morpheme[4]) != "accepted":
            return False
        try:
            part_base = canonical_morpheme_identity(
                part["language"] or morpheme[0], part["canonical_form"], part["kind"]
            )[0]
            stored_base = canonical_morpheme_identity(
                morpheme[0], morpheme[1] or morpheme[2], morpheme[3]
            )[0]
        except ValueError:
            return False
        if part["kind"] != str(morpheme[3]) or part_base != stored_base:
            return False
        evidence_ids = set(part["evidence_ids"]) | set(part["context_evidence_ids"])
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            found = {
                str(row[0]) for row in connection.execute(
                    f"SELECT evidence_id FROM evidence_records WHERE evidence_id IN ({placeholders})",
                    tuple(sorted(evidence_ids)),
                )
            }
            if found != evidence_ids:
                return False
    for related in projection["related_terms"]:
        related_id = str(related["term_id"])
        related_state = connection.execute(
            """SELECT e.status FROM terms t JOIN entities e ON e.entity_id=t.entity_id
               WHERE t.entity_id=?""", (related_id,),
        ).fetchone()
        if related_state is None or (
            str(related_state[0]) != "accepted"
            and related_id not in prospective
            and not allow_nonaccepted_related
        ):
            return False
        if not set(related["component_ids"]).issubset(part_ids):
            return False
    return True


def _normalise_term_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _complete_atomic_occurrence_projection(
    connection: sqlite3.Connection, projection: Mapping[str, Any]
) -> bool:
    """Return whether an accepted split is a complete ordered primary projection."""

    parts = list(projection.get("parts", ()))
    if [int(part["ordinal"]) for part in parts] != list(range(len(parts))):
        return False
    row = connection.execute(
        "SELECT normalized FROM terms WHERE entity_id=?",
        (str(projection.get("term_id", "")),),
    ).fetchone()
    return bool(
        row
        and _normalise_term_text("".join(str(part["surface"]) for part in parts))
        == str(row[0])
    )


def _projection_language(projection: Mapping[str, Any]) -> str:
    languages = {
        str(part.get("language", "")).strip().casefold()
        for part in projection.get("parts", [])
        if str(part.get("language", "")).strip()
    }
    return next(iter(languages)) if len(languages) == 1 else ""


def _direct_draft_term_identity_matches(
    connection: sqlite3.Connection, projection: Mapping[str, Any]
) -> bool:
    term_id = str(projection["term_id"])
    language = _projection_language(projection)
    raw_term = str(projection.get("term", "")).strip()
    row = connection.execute(
        """SELECT t.language,t.normalized,t.kind,e.entity_type,e.canonical_key,e.status
           FROM terms t JOIN entities e ON e.entity_id=t.entity_id
           WHERE t.entity_id=?""", (term_id,),
    ).fetchone()
    return bool(
        row
        and str(row[5]) == "draft"
        and str(row[3]) == "term"
        and str(row[2]) == "word"
        and language
        and raw_term
        and str(row[0]).casefold() == language
        and str(row[1]) == _normalise_term_text(raw_term)
        and str(row[4]) == f"term:{language}:word:{row[1]}"
    )


def _plan_direct_draft_term_promotions(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, str]]:
    """Accept only a draft term that is the direct subject of paired atomic state."""

    canonical_ids = _canonical_morpheme_id_map(connection)
    revisions: dict[tuple[str, str], str] = {}
    direct_occurrences: dict[str, list[dict[str, Any] | None]] = {}
    for row in connection.execute(
        """SELECT revision_id,entity_id,payload FROM entity_revisions
           WHERE accepted=1 AND reason='atomic morpheme split'
           ORDER BY revision_id"""
    ):
        try:
            raw = json.loads(str(row[2]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        raw_term_id = str(raw.get("term_id", "")).strip() if isinstance(raw, dict) else ""
        projection = _resolve_atomic_projection_aliases(
            _atomic_split_projection(raw), canonical_ids
        )
        if raw_term_id:
            direct_occurrences.setdefault(raw_term_id, []).append(projection)
        if projection is None or projection["term_id"] != str(row[1]):
            continue
        fingerprint = hashlib.sha256(_arbitrary_json(projection).encode()).hexdigest()
        revisions[(str(row[1]), fingerprint)] = str(row[0])

    candidates: dict[str, dict[str, str]] = {}
    for row in connection.execute(
        """SELECT artifact.artifact_id,artifact.payload,job.subject_entity_id,
                  job.subject_key,job.job_type
           FROM job_artifacts artifact JOIN preparation_jobs job USING(job_id)
           WHERE artifact.stage='accepted-morpheme-split'
             AND artifact.validation_state='accepted' AND job.status='complete'
           ORDER BY artifact.artifact_id"""
    ):
        try:
            raw = json.loads(str(row[1]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        raw_term_id = str(raw.get("term_id", "")).strip() if isinstance(raw, dict) else ""
        projection = _resolve_atomic_projection_aliases(
            _atomic_split_projection(raw), canonical_ids
        )
        if raw_term_id:
            direct_occurrences.setdefault(raw_term_id, []).append(projection)
        subject_id = str(row[2] or "")
        if (
            str(row[4]) != "split-morphemes"
            or str(row[3]) != f"term:{subject_id}"
            or projection is None
            or projection["term_id"] != subject_id
            or not _direct_draft_term_identity_matches(connection, projection)
            or not _valid_atomic_projection(
                connection, projection, prospective_term_ids=(subject_id,),
                allow_nonaccepted_related=True,
            )
        ):
            continue
        fingerprint = hashlib.sha256(_arbitrary_json(projection).encode()).hexdigest()
        revision_id = revisions.get((subject_id, fingerprint))
        if revision_id:
            candidates[subject_id] = {
                "artifact_id": str(row[0]),
                "revision_id": revision_id,
                "language": _projection_language(projection),
                "normalized": _normalise_term_text(projection["term"]),
            }
    for term_id in tuple(candidates):
        occurrences = direct_occurrences.get(term_id, [])
        if not occurrences or any(
            projection is None
            or not _direct_draft_term_identity_matches(connection, projection)
            for projection in occurrences
        ):
            candidates.pop(term_id, None)
    return candidates


def _current_parser_roles_for_part(
    connection: sqlite3.Connection,
    *,
    parser_version: str,
    term_id: str,
    part: Mapping[str, Any],
) -> set[str]:
    """Return exact current-parser roles for this term and normalized surface."""

    from .morphology_contract import canonical_morpheme_identity

    try:
        expected_base = canonical_morpheme_identity(
            part["language"] or "en", part["canonical_form"], part["kind"]
        )[0]
    except ValueError:
        return set()
    roles: set[str] = set()
    for row in connection.execute(
        """SELECT m.language,m.form,m.normalized,m.kind
           FROM morphology_source_observations_v3 s
           JOIN morphology_formula_links_v3 f USING(source_observation_id)
           JOIN morphology_term_observations_v3 t
             ON t.term_observation_id=f.term_observation_id
            AND t.source_observation_id=s.source_observation_id
           JOIN morphology_component_observations_v3 c
             ON c.component_observation_id=f.component_observation_id
            AND c.source_observation_id=s.source_observation_id
           JOIN morphemes m ON m.entity_id=c.materialized_entity_id
           WHERE s.parser_version=? AND f.state='materialized'
             AND t.state='materialized' AND c.state='materialized'
             AND t.materialized_entity_id=?""",
        (parser_version, term_id),
    ):
        try:
            base = canonical_morpheme_identity(
                row[0], row[1] or row[2], row[3]
            )[0]
        except ValueError:
            continue
        if base == expected_base:
            roles.add(str(row[3]))
    return roles


def _atomic_direct_spec(
    part: Mapping[str, Any], *, current_roles: set[str]
) -> RelationSupportSpec:
    properties = _atomic_part_properties(part)
    basis = str(part["basis"])
    evidence_ids = tuple(part["evidence_ids"])
    if basis == "book" and current_roles != {str(part["kind"])}:
        properties = {
            **properties,
            "legacy_artifact_evidence_ids": list(part["evidence_ids"]),
            "context_evidence_ids": sorted(
                {*part["context_evidence_ids"], *part["evidence_ids"]}
            ),
            "role_authority": "accepted-atomic-revision",
        }
        basis = "model"
        evidence_ids = ()
    return RelationSupportSpec(
        "", basis, float(part["confidence"]), properties, evidence_ids
    )


def _plan_accepted_atomic_lineage(
    connection: sqlite3.Connection, *, parser_version: str,
    prospective_term_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Plan exact revision owners and artifact-backed restoration without guessing."""

    canonical_morpheme_ids = _canonical_morpheme_id_map(connection)
    revisions: dict[str, dict[str, tuple[str, str, dict[str, Any]]]] = {}
    revision_endpoints: set[tuple[str, str, str, str]] = set()
    errors: list[str] = []
    for row in connection.execute(
        """SELECT revision_id, entity_id, prompt_version, payload
           FROM entity_revisions revision
           WHERE accepted=1 AND reason='atomic morpheme split'
           ORDER BY entity_id, revision_id"""
    ):
        try:
            payload = json.loads(str(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        projection = _resolve_atomic_projection_aliases(
            _atomic_split_projection(payload), canonical_morpheme_ids
        )
        subject_id = str(row[1])
        if (
            projection is None
            or projection["term_id"] != subject_id
            or not _valid_atomic_projection(
                connection, projection,
                prospective_term_ids=prospective_term_ids,
                allow_nonaccepted_related=True,
            )
        ):
            continue
        fingerprint = hashlib.sha256(_arbitrary_json(projection).encode()).hexdigest()
        revisions.setdefault(subject_id, {})[fingerprint] = (
            str(row[0]), str(row[2] or ""), projection
        )
        for part in projection["parts"]:
            revision_endpoints.add(
                (subject_id, subject_id, part["morpheme_id"], "has-component")
            )
        for related in projection["related_terms"]:
            related_status = connection.execute(
                "SELECT status FROM entities WHERE entity_id=?", (related["term_id"],),
            ).fetchone()
            if related_status is None or (
                str(related_status[0]) != "accepted"
                and str(related["term_id"]) not in set(prospective_term_ids)
            ):
                continue
            for component_id in related["component_ids"]:
                revision_endpoints.add(
                    (subject_id, related["term_id"], component_id, "shares-component")
                )

    owner_plans: dict[tuple[str, str, str], RelationSupportSpec] = {}
    owner_versions: dict[tuple[str, str, str], str] = {}
    role_conflicts: set[str] = set()

    def add_existing_revision_claim(
        subject_id: str, source_id: str, target_id: str, relation: str,
        revision_version: str, part: Mapping[str, Any] | None = None,
    ) -> None:
        row = connection.execute(
            """SELECT assertion_id,basis,confidence,properties_json
               FROM relation_assertions WHERE status='accepted'
                 AND subject_entity_id=? AND source_entity_id=?
                 AND target_entity_id=? AND relation=?""",
            (subject_id, source_id, target_id, relation),
        ).fetchone()
        if row is None:
            return
        assertion_id = str(row[0])
        if part is None:
            spec = RelationSupportSpec(
                assertion_id, str(row[1]), float(row[2]),
                _relation_properties(row[3]),
                _assertion_evidence(connection, assertion_id)
                if str(row[1]) in {"book", "reviewed"} else (),
            )
        else:
            roles = _current_parser_roles_for_part(
                connection, parser_version=parser_version,
                term_id=subject_id, part=part,
            )
            if roles and roles != {str(part["kind"])}:
                role_conflicts.add(assertion_id)
                return
            candidate = _atomic_direct_spec(part, current_roles=roles)
            spec = RelationSupportSpec(
                assertion_id, candidate.basis, candidate.confidence,
                candidate.properties, candidate.evidence_ids,
            )
        key = (assertion_id, "atomic-split", subject_id)
        owner_plans[key] = _merge_specs(owner_plans.get(key), spec)
        owner_versions[key] = revision_version

    for subject_id, by_fingerprint in revisions.items():
        for _, revision_version, projection in by_fingerprint.values():
            parts = {part["morpheme_id"]: part for part in projection["parts"]}
            for part in projection["parts"]:
                add_existing_revision_claim(
                    subject_id, subject_id, part["morpheme_id"],
                    "has-component", revision_version, part,
                )
            for related in projection["related_terms"]:
                related_status = connection.execute(
                    "SELECT status FROM entities WHERE entity_id=?", (related["term_id"],),
                ).fetchone()
                if related_status is None or (
                    str(related_status[0]) != "accepted"
                    and str(related["term_id"]) not in set(prospective_term_ids)
                ):
                    continue
                for component_id in related["component_ids"]:
                    add_existing_revision_claim(
                        subject_id, related["term_id"], component_id,
                        "shares-component", revision_version,
                    )

    assertions: dict[str, dict[str, Any]] = {}
    restore_assertion_ids: set[str] = set()
    term_morphemes: dict[tuple[str, int], dict[str, Any]] = {}
    complete_term_morpheme_projections: dict[
        str, dict[int, dict[str, Any]]
    ] = {}
    incomplete_projection_subjects: set[str] = set()
    artifact_sets: dict[str, set[str]] = {}
    for row in connection.execute(
        """SELECT artifact.artifact_id, artifact.payload,
                  job.subject_entity_id, job.job_type
           FROM job_artifacts artifact JOIN preparation_jobs job
             ON job.job_id=artifact.job_id
           WHERE artifact.stage='accepted-morpheme-split'
             AND artifact.validation_state='accepted' AND job.status='complete'
           ORDER BY artifact.created_at, artifact.artifact_id"""
    ):
        artifact_id, subject_id = str(row[0]), str(row[2] or "")
        try:
            payload = json.loads(str(row[1]))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        projection = _resolve_atomic_projection_aliases(
            _atomic_split_projection(payload), canonical_morpheme_ids
        )
        if (
            not subject_id
            or str(row[3]) != "split-morphemes"
            or projection is None
            or projection["term_id"] != subject_id
            or not _valid_atomic_projection(
                connection, projection,
                prospective_term_ids=prospective_term_ids,
                allow_nonaccepted_related=True,
            )
        ):
            errors.append(f"artifact:{artifact_id}")
            continue
        fingerprint = hashlib.sha256(_arbitrary_json(projection).encode()).hexdigest()
        revision = revisions.get(subject_id, {}).get(fingerprint)
        if revision is None:
            errors.append(f"artifact:{artifact_id}:missing-matching-accepted-revision")
            continue
        revision_version = revision[1]
        artifact_projection: dict[int, dict[str, Any]] = {}
        projection_complete = _complete_atomic_occurrence_projection(
            connection, projection
        )
        for part in projection["parts"]:
            roles = _current_parser_roles_for_part(
                connection, parser_version=parser_version,
                term_id=subject_id, part=part,
            )
            identity = (subject_id, subject_id, part["morpheme_id"], "has-component")
            match = connection.execute(
                """SELECT assertion_id,status FROM relation_assertions
                   WHERE subject_entity_id=? AND source_entity_id=?
                     AND target_entity_id=? AND relation=?""", identity,
            ).fetchone()
            assertion_id = str(match[0]) if match else _stable_id(
                "relation-assertion", *identity
            )
            if not projection_complete and (
                match is None or str(match[1]) != "accepted"
            ):
                continue
            if roles and roles != {str(part["kind"])}:
                if match is not None and str(match[1]) == "accepted":
                    role_conflicts.add(assertion_id)
                projection_complete = False
                continue
            candidate = _atomic_direct_spec(part, current_roles=roles)
            properties = dict(candidate.properties)
            assertions[assertion_id] = {
                "assertion_id": assertion_id,
                "subject_entity_id": subject_id,
                "source_entity_id": subject_id,
                "target_entity_id": part["morpheme_id"],
                "relation": "has-component",
                "basis": candidate.basis,
                "confidence": part["confidence"],
                "properties": properties,
            }
            if match is None or str(match[1]) != "accepted":
                restore_assertion_ids.add(assertion_id)
            spec = RelationSupportSpec(
                assertion_id, candidate.basis, candidate.confidence, properties,
                candidate.evidence_ids,
            )
            key = (assertion_id, "atomic-split", subject_id)
            owner_plans[key] = _merge_specs(owner_plans.get(key), spec)
            owner_versions[key] = revision_version
            slot = (subject_id, int(part["ordinal"]))
            current = connection.execute(
                "SELECT morpheme_id FROM term_morphemes WHERE term_id=? AND ordinal=?",
                slot,
            ).fetchone()
            if current is not None and str(current[0]) != part["morpheme_id"]:
                errors.append(f"artifact:{artifact_id}:conflicting-ordinal:{part['ordinal']}")
                projection_complete = False
                continue
            link = {
                "term_id": subject_id,
                "morpheme_id": part["morpheme_id"],
                "ordinal": int(part["ordinal"]),
                "surface": part["surface"],
                "confidence": float(part["confidence"]),
                "basis": candidate.basis,
                "evidence_ids": sorted(
                    {*part["evidence_ids"], *part["context_evidence_ids"]}
                ),
            }
            term_morphemes[slot] = link
            artifact_projection[int(part["ordinal"])] = link
        if projection_complete and len(artifact_projection) == len(projection["parts"]):
            prior = complete_term_morpheme_projections.get(subject_id)
            if prior is not None and prior != artifact_projection:
                errors.append(f"conflicting-occurrence-projections:{subject_id}")
                incomplete_projection_subjects.add(subject_id)
            else:
                complete_term_morpheme_projections[subject_id] = artifact_projection
        else:
            incomplete_projection_subjects.add(subject_id)
        artifact_sets.setdefault(subject_id, set()).add(fingerprint)
    errors.extend(
        f"conflicting-artifacts:{subject_id}"
        for subject_id, values in artifact_sets.items() if len(values) > 1
    )
    for subject_id in incomplete_projection_subjects:
        complete_term_morpheme_projections.pop(subject_id, None)
    for subject_id, values in artifact_sets.items():
        if len(values) > 1:
            complete_term_morpheme_projections.pop(subject_id, None)
    return {
        "owner_plans": owner_plans,
        "owner_versions": owner_versions,
        "assertions": assertions,
        "restore_assertion_ids": sorted(restore_assertion_ids),
        "term_morphemes": term_morphemes,
        "complete_term_morpheme_projections": complete_term_morpheme_projections,
        "accepted_revision_endpoints": revision_endpoints,
        "role_conflict_assertion_ids": sorted(role_conflicts),
        "errors": sorted(set(errors)),
    }


def _automatic_non_authoritative_events(
    connection: sqlite3.Connection, assertion_id: str
) -> bool:
    rows = connection.execute(
        """SELECT action,before_json,after_json,job_id,reason,validator_version
           FROM relation_assertion_events WHERE assertion_id=? AND action<>'baseline'
           ORDER BY created_at,event_id""", (assertion_id,),
    ).fetchall()
    for row in rows:
        try:
            before, after = json.loads(str(row[1])), json.loads(str(row[2]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if str(row[0]) == "keep":
            if not (
                str(row[5]) == "relation-assertion-decision-v1"
                and str(row[4]) == "proposal did not materially strengthen the incumbent"
                and isinstance(before, dict) and before == after
            ):
                return False
            continue
        identity_keys = (
            "assertion_id", "subject_entity_id", "source_entity_id",
            "target_entity_id", "relation",
        )
        if str(row[0]) == "archive" and (
            row[3] is None
            and str(row[5]) == "relation-assertion-archive-v1"
            and str(row[4]) == "accepted assertion archived by store reconciliation"
            and isinstance(before, dict) and isinstance(after, dict)
            and all(before.get(key) == after.get(key) for key in identity_keys)
            and before.get("status") == "accepted"
            and after.get("status") == "archived"
        ):
            continue
        return False
    return True


def _legacy_model_baseline_matches(
    connection: sqlite3.Connection, assertion: Sequence[Any]
) -> bool:
    rows = connection.execute(
        """SELECT after_json,evidence_ids_json,reason,validator_version
           FROM relation_assertion_events WHERE assertion_id=? AND action='baseline'
           ORDER BY created_at,event_id""", (assertion[0],),
    ).fetchall()
    if not rows:
        return False
    for row in rows:
        try:
            after, evidence = json.loads(str(row[0])), json.loads(str(row[1]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not (
            isinstance(after, dict) and isinstance(evidence, list) and not evidence
            and str(after.get("assertion_id", "")) == str(assertion[0])
            and str(after.get("subject_entity_id", "")) == str(assertion[1])
            and str(after.get("source_entity_id", "")) == str(assertion[2])
            and str(after.get("target_entity_id", "")) == str(assertion[3])
            and str(after.get("relation", "")) == str(assertion[4])
            and str(after.get("basis", "")) == "model"
            and str(after.get("status", "")) == "accepted"
            and str(row[2]) == (
                "schema-v8 current-state baseline; historical revisions are not reconstructed"
            )
            and str(row[3]) == "relation-assertion-decision-v1"
        ):
            return False
    return _automatic_non_authoritative_events(connection, str(assertion[0]))


def _property_ordinal(value: object) -> int | None:
    properties = _relation_properties(value)
    ordinal = properties.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        return None
    return ordinal


def _plan_exact_role_arbitration(
    connection: sqlite3.Connection,
    *,
    parser_version: str,
    parser_scopes: Mapping[str, Mapping[str, RelationSupportSpec]],
    owner_plans: Mapping[tuple[str, str, str], RelationSupportSpec],
    prospective_accepted_terms: set[str],
    excluded_assertion_ids: set[str],
) -> dict[str, Any]:
    """Let one exact v7 book occurrence outrank only model-only role claims."""

    from .morphology_contract import canonical_morpheme_identity

    desired_parser_owners = {
        (spec.assertion_id, owner_id): spec
        for owner_id, desired in parser_scopes.items()
        for spec in desired.values()
    }

    def assertion_row(assertion_id: str) -> sqlite3.Row | tuple[Any, ...] | None:
        return connection.execute(
            """SELECT a.subject_entity_id,a.source_entity_id,a.target_entity_id,
                      a.relation,a.properties_json,t.language,m.language,m.form,
                      m.normalized,m.kind,t.normalized
               FROM relation_assertions a
               JOIN terms t ON t.entity_id=a.source_entity_id
               JOIN morphemes m ON m.entity_id=a.target_entity_id
               WHERE a.assertion_id=? AND a.status='accepted'""",
            (assertion_id,),
        ).fetchone()

    def occurrence_ordinal(
        assertion_id: str, term_id: str, morpheme_id: str,
        planned_specs: Iterable[RelationSupportSpec] = (),
    ) -> int | None:
        values: set[int] = set()
        row = connection.execute(
            "SELECT properties_json FROM relation_assertions WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        if row:
            ordinal = _property_ordinal(row[0])
            if ordinal is not None:
                values.add(ordinal)
        for support in connection.execute(
            """SELECT properties_json FROM relation_assertion_supports
               WHERE assertion_id=? AND status='accepted'""", (assertion_id,),
        ):
            ordinal = _property_ordinal(support[0])
            if ordinal is not None:
                values.add(ordinal)
        for spec in planned_specs:
            ordinal = _property_ordinal(spec.properties)
            if ordinal is not None:
                values.add(ordinal)
        values.update(int(slot[0]) for slot in connection.execute(
            """SELECT ordinal FROM term_morphemes
               WHERE term_id=? AND morpheme_id=?""", (term_id, morpheme_id),
        ))
        return next(iter(values)) if len(values) == 1 else None

    def base_spans(term: str, base: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        offset = 0
        while base and offset <= len(term) - len(base):
            start = term.find(base, offset)
            if start < 0:
                break
            spans.append((start, start + len(base)))
            offset = start + 1
        return spans

    def occurrence_key(
        term_id: str, language: str, term: str, base: str, ordinal: int | None,
        *, surface_base: str | None = None, allow_ordinal: bool = True,
    ) -> tuple[str, str, str, int, int, str] | None:
        spans = base_spans(term, surface_base or base)
        if len(spans) == 1:
            return (term_id, language, "span", spans[0][0], spans[0][1], base)
        if allow_ordinal and ordinal is not None:
            return (term_id, language, "ordinal", ordinal, ordinal, base)
        return None

    typed: dict[tuple[str, str, str, int, int, str], list[dict[str, Any]]] = {}
    typed_mapping_failures: list[dict[str, Any]] = []
    for (assertion_id, owner_id), spec in desired_parser_owners.items():
        if spec.basis != "book" or not spec.evidence_ids:
            continue
        row = assertion_row(assertion_id)
        if row is None or str(row[3]) != "has-component" or str(row[0]) != str(row[1]):
            continue
        term_id, morpheme_id = str(row[1]), str(row[2])
        kind = str(row[9])
        language = str(row[6]).casefold()
        if str(row[5]).casefold() != language:
            continue
        try:
            base = canonical_morpheme_identity(language, row[7] or row[8], kind)[0]
        except ValueError:
            continue
        formula_rows = connection.execute(
            """SELECT DISTINCT s.evidence_id,c.proposed_kind,c.raw_form,
                              f.properties_json
               FROM morphology_source_observations_v3 s
               JOIN morphology_formula_links_v3 f USING(source_observation_id)
               JOIN morphology_term_observations_v3 t
                 ON t.term_observation_id=f.term_observation_id
                AND t.source_observation_id=s.source_observation_id
               JOIN morphology_component_observations_v3 c
                 ON c.component_observation_id=f.component_observation_id
                AND c.source_observation_id=s.source_observation_id
               WHERE s.parser_version=? AND s.corpus_id||':'||s.source_entry_id=?
                 AND f.state='materialized' AND t.state='materialized'
                 AND c.state='materialized' AND t.materialized_entity_id=?
                 AND c.materialized_entity_id=? AND f.evidence_id=s.evidence_id
                 AND t.evidence_id=s.evidence_id AND c.evidence_id=s.evidence_id""",
            (parser_version, owner_id, term_id, morpheme_id),
        ).fetchall()
        if ({str(item[0]) for item in formula_rows} != set(spec.evidence_ids)
                or {str(item[1]) for item in formula_rows} != {kind}):
            continue
        planned = [item for key, item in owner_plans.items() if key[0] == assertion_id]
        planned.append(spec)
        ordinal = occurrence_ordinal(assertion_id, term_id, morpheme_id, planned)
        raw_surfaces = {str(spec.properties.get("surface", "")).strip()}
        for formula in formula_rows:
            raw_surfaces.add(str(formula[2]).strip())
            properties = _relation_properties(formula[3])
            matching = properties.get("matching_parts", [])
            if isinstance(matching, list):
                for item in matching:
                    if isinstance(item, str):
                        raw_surfaces.add(item.strip())
                    elif isinstance(item, dict):
                        for field in ("surface", "form", "text", "matched"):
                            if str(item.get(field, "")).strip():
                                raw_surfaces.add(str(item[field]).strip())
        candidate_spans: set[tuple[int, int]] = set()
        term_text = str(row[10])
        for raw_surface in raw_surfaces:
            surface_base = _normalise_term_text(raw_surface).strip("-")
            spans = base_spans(term_text, surface_base)
            for span in spans:
                if (
                    (kind == "prefix" and span[0] == 0)
                    or (kind == "suffix" and span[1] == len(term_text))
                    or kind in {"root", "free"}
                ):
                    candidate_spans.add(span)
        key = None
        if len(candidate_spans) == 1:
            span = next(iter(candidate_spans))
            key = (term_id, language, "span", span[0], span[1], base)
        if key is None:
            typed_mapping_failures.append({
                "term_id": term_id, "language": language, "base": base,
                "assertion_id": assertion_id,
                "reason": "typed-v7-winner-has-no-unique-compatible-surface-span",
            })
            continue
        typed.setdefault(key, []).append({
            "assertion_id": assertion_id, "morpheme_id": morpheme_id,
            "kind": kind, "surface": str(spec.properties.get("surface", "")),
            "confidence": float(spec.confidence), "basis": spec.basis,
            "ordinal": ordinal, "term_length": len(str(row[10])),
        })

    typed_keys_by_base: dict[
        tuple[str, str, str], set[tuple[str, str, str, int, int, str]]
    ] = {}
    for key in typed:
        typed_keys_by_base.setdefault((key[0], key[1], key[5]), set()).add(key)

    claims: dict[tuple[str, str, str, int, int, str], list[dict[str, Any]]] = {}
    base_claims: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in connection.execute(
        """SELECT a.assertion_id,a.subject_entity_id,a.source_entity_id,
                  a.target_entity_id,t.language,m.language,m.form,m.normalized,
                  m.kind,e.status,t.normalized
           FROM relation_assertions a
           JOIN terms t ON t.entity_id=a.source_entity_id
           JOIN morphemes m ON m.entity_id=a.target_entity_id
           JOIN entities e ON e.entity_id=a.source_entity_id
           WHERE a.status='accepted' AND a.relation='has-component'
             AND a.subject_entity_id=a.source_entity_id ORDER BY a.assertion_id"""
    ):
        assertion_id = str(row[0])
        if assertion_id in excluded_assertion_ids:
            continue
        term_id, morpheme_id = str(row[2]), str(row[3])
        if str(row[9]) != "accepted" and term_id not in prospective_accepted_terms:
            continue
        language, kind = str(row[5]).casefold(), str(row[8])
        if str(row[4]).casefold() != language:
            continue
        try:
            base = canonical_morpheme_identity(language, row[6] or row[7], kind)[0]
        except ValueError:
            continue
        planned = [item for key, item in owner_plans.items() if key[0] == assertion_id]
        planned.extend(
            spec for (candidate_id, _), spec in desired_parser_owners.items()
            if candidate_id == assertion_id
        )
        ordinal = occurrence_ordinal(assertion_id, term_id, morpheme_id, planned)
        key = occurrence_key(
            term_id, language, str(row[10]), base, ordinal, allow_ordinal=False
        )
        typed_keys = typed_keys_by_base.get((term_id, language, base), set())
        if key is None and len(typed_keys) == 1:
            key = next(iter(typed_keys))
        if key is None:
            key = occurrence_key(term_id, language, str(row[10]), base, ordinal)
        base_claims.setdefault((term_id, language, base), []).append({
            "assertion_id": assertion_id, "kind": kind, "ordinal": ordinal,
            "span_count": len(base_spans(str(row[10]), base)),
        })
        if key is None:
            continue
        claims.setdefault(key, []).append({
            "assertion_id": assertion_id, "morpheme_id": morpheme_id, "kind": kind,
        })

    def prospective_bases(assertion_id: str) -> set[str]:
        owners: dict[tuple[str, str], str] = {}
        for row in connection.execute(
            """SELECT owner_type,owner_id,basis FROM relation_assertion_supports
               WHERE assertion_id=? AND status='accepted'""", (assertion_id,),
        ):
            owner_type, owner_id = str(row[0]), str(row[1])
            if owner_type == "morphology-parser" and (
                assertion_id, owner_id
            ) not in desired_parser_owners:
                continue
            owners[(owner_type, owner_id)] = str(row[2])
        for key, spec in owner_plans.items():
            if key[0] == assertion_id:
                owners[(key[1], key[2])] = spec.basis
        for (candidate_id, owner_id), spec in desired_parser_owners.items():
            if candidate_id == assertion_id:
                owners[("morphology-parser", owner_id)] = spec.basis
        return set(owners.values())

    retire: set[str] = set()
    deferred: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = list(typed_mapping_failures)
    winners: dict[tuple[str, int], dict[str, Any]] = {}
    for key, grouped_claims in sorted(claims.items()):
        roles = {claim["kind"] for claim in grouped_claims}
        if len(roles) <= 1:
            continue
        typed_claims = typed.get(key, [])
        typed_roles = {claim["kind"] for claim in typed_claims}
        typed_targets = {claim["morpheme_id"] for claim in typed_claims}
        unique_span = key[2] == "span"
        if not typed_claims:
            deferred.append({
                "term_id": key[0], "language": key[1], "occurrence": list(key[2:5]),
                "base": key[5], "roles": sorted(roles),
                "reason": "no-exact-v7-book-role-review-required",
            })
            continue
        if (
            not unique_span
            or len(typed_roles) != 1
            or len(typed_targets) != 1
            or any(claim.get("ordinal") is None for claim in typed_claims)
        ):
            blocking.append({
                "term_id": key[0], "language": key[1], "occurrence": list(key[2:5]),
                "base": key[5], "roles": sorted(roles),
                "reason": "multiple-or-incomplete-exact-v7-book-winners",
            })
            continue
        winner = typed_claims[0]
        winner_position_valid = (
            (winner["kind"] == "prefix" and key[3] == 0)
            or (winner["kind"] == "suffix" and key[4] == winner["term_length"])
            or winner["kind"] in {"root", "free"}
        )
        if not winner_position_valid:
            blocking.append({
                "term_id": key[0], "language": key[1], "occurrence": list(key[2:5]),
                "base": key[5], "roles": sorted(roles),
                "reason": "typed-v7-role-is-position-incompatible",
            })
            continue
        unresolved = False
        for claim in grouped_claims:
            if claim["kind"] == winner["kind"]:
                continue
            if prospective_bases(claim["assertion_id"]) == {"model"}:
                retire.add(claim["assertion_id"])
            else:
                unresolved = True
        if unresolved:
            blocking.append({
                "term_id": key[0], "language": key[1], "occurrence": list(key[2:5]),
                "base": key[5], "roles": sorted(roles),
                "reason": "conflicting-role-is-not-model-only",
            })
            continue
        winners[(key[0], int(winner["ordinal"]))] = winner
    deferred_keys = {
        (str(item["term_id"]), str(item["language"]), str(item["base"]))
        for item in deferred
    }
    for base_key, grouped_claims in sorted(base_claims.items()):
        roles = {claim["kind"] for claim in grouped_claims}
        if (len(roles) <= 1 or base_key in deferred_keys
                or base_key in typed_keys_by_base):
            continue
        span_counts = {int(claim["span_count"]) for claim in grouped_claims}
        ordinals = {claim["ordinal"] for claim in grouped_claims}
        if span_counts == {1}:
            continue
        deferred.append({
            "term_id": base_key[0], "language": base_key[1], "base": base_key[2],
            "roles": sorted(roles),
            "reason": (
                "multiple-or-missing-base-spans"
                if len(ordinals) == 1 and None not in ordinals
                else "ambiguous-or-inconsistent-occurrence-slot"
            ),
        })
    unresolved_by_group: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in deferred + blocking:
        group_key = (
            str(item["term_id"]), str(item["language"]), str(item["base"])
        )
        existing = unresolved_by_group.get(group_key)
        if existing is None:
            existing = {
                key: value for key, value in item.items()
                if key != "assertion_id"
            }
            existing["roles"] = sorted(set(existing.get("roles", ())))
            existing["diagnostic_reasons"] = {str(item["reason"])}
            existing["assertion_ids"] = set()
            unresolved_by_group[group_key] = existing
        else:
            existing["roles"] = sorted(
                set(existing.get("roles", ())) | set(item.get("roles", ()))
            )
            existing["diagnostic_reasons"].add(str(item["reason"]))
        if item.get("assertion_id"):
            existing["assertion_ids"].add(str(item["assertion_id"]))
    deferred = []
    for group_key in sorted(unresolved_by_group):
        item = unresolved_by_group[group_key]
        diagnostic_reasons = sorted(item.pop("diagnostic_reasons"))
        assertion_ids = sorted(item.pop("assertion_ids"))
        if len(diagnostic_reasons) > 1:
            item["diagnostic_reasons"] = diagnostic_reasons
        if assertion_ids:
            item["assertion_ids"] = assertion_ids
        deferred.append(item)
    return {
        "retire_assertion_ids": sorted(retire),
        "deferred": deferred,
        "blocking": [],
        "winning_slots": winners,
    }


def _morphology_v7_plan(
    connection: sqlite3.Connection,
    *,
    parser_version: str,
    expected_sources: Sequence[tuple[str, str, str]],
    expected_source_records: int,
    migration_key: str,
) -> dict[str, Any]:
    """Build a refusal-oriented finalization plan without changing state."""

    required = {
        "schema_meta", "entities", "morphemes", "terms", "term_morphemes",
        "entity_edges", "relation_assertions", "assertion_evidence",
        "relation_assertion_supports", "relation_assertion_support_evidence",
        "relation_support_owner_scopes", "relation_support_migrations",
        "relation_assertion_events", "preparation_jobs", "job_artifacts",
        "lexical_projection_repairs", "morphology_ingest_runs",
        "morphology_ingest_batches", "morphology_source_observations_v3",
        "morphology_term_observations_v3",
        "morphology_component_observations_v3",
        "morphology_formula_links_v3", "morphology_declarations_v3",
    }
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = sorted(required - present)
    expected = _normalized_migration_sources(expected_sources)
    expected_set = set(expected)
    expected_owner_ids = {f"{corpus}:{record}" for corpus, record, _ in expected}
    duplicate_sources = len(expected) - len(expected_set)
    duplicate_owner_ids = len(expected) - len(expected_owner_ids)
    refusal_reasons: list[str] = []
    if missing_tables:
        refusal_reasons.append("missing required tables")
    if len(expected) != int(expected_source_records):
        refusal_reasons.append("expected source record total does not match gate")
    if duplicate_sources or duplicate_owner_ids:
        refusal_reasons.append("source indexes contain duplicate logical owners")
    schema_version = ""
    if "schema_meta" in present:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        schema_version = str(row[0]) if row else ""
    if schema_version != "13":
        refusal_reasons.append("knowledge schema is not version 13")

    empty_plan = {
        "migration_key": migration_key,
        "parser_version": parser_version,
        "expected_source_records": int(expected_source_records),
        "source_record_count": len(expected),
        "duplicate_source_count": duplicate_sources,
        "duplicate_owner_count": duplicate_owner_ids,
        "schema_version": schema_version,
        "missing_tables": missing_tables,
    }
    coverage_fingerprint = _morphology_migration_input_fingerprint(
        migration_key=migration_key,
        parser_version=parser_version,
        expected_sources=expected,
    )
    if missing_tables:
        return {
            **empty_plan,
            "coverage_fingerprint": coverage_fingerprint,
            "refusal_reasons": refusal_reasons,
            "destructive_ready": False,
            "_internal": {},
        }

    observation_rows = connection.execute(
        """SELECT source_observation_id, corpus_id, source_entry_id, source_hash,
                  materialization_state, evidence_id, term_count, component_count,
                  formula_link_count, declaration_count
           FROM morphology_source_observations_v3
           WHERE parser_version=? ORDER BY corpus_id, source_entry_id, source_hash""",
        (parser_version,),
    ).fetchall()
    actual_observations = {
        (str(row[1]), str(row[2]), str(row[3])) for row in observation_rows
    }
    missing_observations = sorted(expected_set - actual_observations)
    extra_observations = sorted(actual_observations - expected_set)
    if missing_observations or extra_observations:
        refusal_reasons.append("v7 source observations do not exactly equal indexes")

    scope_rows = connection.execute(
        """SELECT owner_id, owner_version, desired_count, desired_fingerprint
           FROM relation_support_owner_scopes
           WHERE owner_type='morphology-parser' ORDER BY owner_id"""
    ).fetchall()
    actual_scope_pairs = {(str(row[0]), str(row[1])) for row in scope_rows}
    expected_scope_pairs = {(owner_id, parser_version) for owner_id in expected_owner_ids}
    missing_scopes = sorted(expected_scope_pairs - actual_scope_pairs)
    extra_scopes = sorted(actual_scope_pairs - expected_scope_pairs)
    if missing_scopes or extra_scopes:
        refusal_reasons.append("parser owner scopes do not exactly equal indexed records")

    run = connection.execute(
        """SELECT run_id, source_manifest_json, state, planned_records,
                  processed_records, invariant_report_json
           FROM morphology_ingest_runs WHERE parser_version=?
           ORDER BY created_at DESC, run_id DESC LIMIT 1""",
        (parser_version,),
    ).fetchone()
    run_id = str(run[0]) if run else ""
    run_valid = bool(
        run
        and str(run[2]) == "complete"
        and int(run[3]) == len(expected)
        and int(run[4]) == len(expected)
    )
    source_manifest_valid = False
    if run:
        try:
            source_manifest = json.loads(str(run[1]))
        except (TypeError, ValueError, json.JSONDecodeError):
            source_manifest = []
        actual_manifest = {
            (str(item.get("corpus_id", "")), str(item.get("source_hash", "")))
            for item in source_manifest if isinstance(item, dict)
        }
        expected_manifest = {(corpus, source_hash) for corpus, _, source_hash in expected}
        source_manifest_valid = actual_manifest == expected_manifest
    if not run_valid or not source_manifest_valid:
        refusal_reasons.append("latest v7 ingest run is incomplete or for different sources")

    batches = connection.execute(
        """SELECT batch_index, record_count, state FROM morphology_ingest_batches
           WHERE run_id=? ORDER BY batch_index""",
        (run_id,),
    ).fetchall() if run_id else []
    batch_valid = bool(
        batches
        and all(str(row[2]) == "complete" and 1 <= int(row[1]) <= 100 for row in batches)
        and sum(int(row[1]) for row in batches) == len(expected)
        and [int(row[0]) for row in batches] == list(range(len(batches)))
    )
    if not batch_valid:
        refusal_reasons.append("latest v7 ingest batches are incomplete or discontinuous")

    nonterminal = {
        "sources": int(connection.execute(
            """SELECT COUNT(*) FROM morphology_source_observations_v3
               WHERE parser_version=? AND materialization_state NOT IN
                     ('observed','ambiguous','materialized','blocked')""", (parser_version,),
        ).fetchone()[0]),
        "terms": int(connection.execute(
            """SELECT COUNT(*) FROM morphology_term_observations_v3 t
               JOIN morphology_source_observations_v3 s USING(source_observation_id)
               WHERE s.parser_version=? AND t.state NOT IN ('materialized','blocked')""",
            (parser_version,),
        ).fetchone()[0]),
        "components": int(connection.execute(
            """SELECT COUNT(*) FROM morphology_component_observations_v3 c
               JOIN morphology_source_observations_v3 s USING(source_observation_id)
               WHERE s.parser_version=? AND c.state NOT IN
                     ('ambiguous','materialized','blocked')""", (parser_version,),
        ).fetchone()[0]),
        "links": int(connection.execute(
            """SELECT COUNT(*) FROM morphology_formula_links_v3 f
               JOIN morphology_source_observations_v3 s USING(source_observation_id)
               WHERE s.parser_version=? AND f.state NOT IN
                     ('ambiguous','materialized','blocked')""", (parser_version,),
        ).fetchone()[0]),
        "declarations": int(connection.execute(
            """SELECT COUNT(*) FROM morphology_declarations_v3 d
               JOIN morphology_source_observations_v3 s USING(source_observation_id)
               WHERE s.parser_version=? AND d.state NOT IN
                     ('observed','qwen-ready','materializable','materialized')""",
            (parser_version,),
        ).fetchone()[0]),
    }
    if any(nonterminal.values()):
        refusal_reasons.append("v7 observations include nonterminal child states")

    count_mismatches = int(connection.execute(
        """SELECT COUNT(*) FROM morphology_source_observations_v3 s
           WHERE s.parser_version=? AND (
             s.term_count<>(SELECT COUNT(*) FROM morphology_term_observations_v3 t
                            WHERE t.source_observation_id=s.source_observation_id)
             OR s.component_count<>(SELECT COUNT(*) FROM morphology_component_observations_v3 c
                            WHERE c.source_observation_id=s.source_observation_id)
             OR s.formula_link_count<>(SELECT COUNT(*) FROM morphology_formula_links_v3 f
                            WHERE f.source_observation_id=s.source_observation_id)
             OR s.declaration_count<>(SELECT COUNT(*) FROM morphology_declarations_v3 d
                            WHERE d.source_observation_id=s.source_observation_id))""",
        (parser_version,),
    ).fetchone()[0])
    invalid_materialized_links = int(connection.execute(
        """SELECT COUNT(*) FROM morphology_formula_links_v3 f
           JOIN morphology_source_observations_v3 s
             ON s.source_observation_id=f.source_observation_id
           LEFT JOIN morphology_term_observations_v3 t
             ON t.term_observation_id=f.term_observation_id
            AND t.source_observation_id=s.source_observation_id
           LEFT JOIN morphology_component_observations_v3 c
             ON c.component_observation_id=f.component_observation_id
            AND c.source_observation_id=s.source_observation_id
           LEFT JOIN relation_assertions a
             ON a.subject_entity_id=t.materialized_entity_id
            AND a.source_entity_id=t.materialized_entity_id
            AND a.target_entity_id=c.materialized_entity_id
            AND a.relation='has-component' AND a.status='accepted'
           WHERE s.parser_version=? AND f.state='materialized' AND (
             t.state<>'materialized' OR c.state<>'materialized'
             OR t.clause_index<>f.clause_index OR c.clause_index<>f.clause_index
             OR t.materialized_entity_id IS NULL OR c.materialized_entity_id IS NULL
             OR f.evidence_id<>s.evidence_id OR t.evidence_id<>s.evidence_id
             OR c.evidence_id<>s.evidence_id OR a.assertion_id IS NULL)""",
        (parser_version,),
    ).fetchone()[0])
    if count_mismatches or invalid_materialized_links:
        refusal_reasons.append("v7 child counts or materialized joins are invalid")

    parser_scopes: dict[str, dict[str, RelationSupportSpec]] = {
        owner_id: {} for owner_id in expected_owner_ids
    }
    formula_rows = connection.execute(
        """SELECT s.corpus_id, s.source_entry_id, s.source_hash, s.evidence_id,
                  f.confidence, a.assertion_id, a.basis, a.confidence,
                  a.properties_json
           FROM morphology_source_observations_v3 s
           JOIN morphology_formula_links_v3 f USING(source_observation_id)
           JOIN morphology_term_observations_v3 t
             ON t.term_observation_id=f.term_observation_id
           JOIN morphology_component_observations_v3 c
             ON c.component_observation_id=f.component_observation_id
           JOIN relation_assertions a
             ON a.subject_entity_id=t.materialized_entity_id
            AND a.source_entity_id=t.materialized_entity_id
            AND a.target_entity_id=c.materialized_entity_id
            AND a.relation='has-component' AND a.status='accepted'
           WHERE s.parser_version=? AND f.state='materialized'
             AND t.state='materialized' AND c.state='materialized'
             AND t.source_observation_id=s.source_observation_id
             AND c.source_observation_id=s.source_observation_id
             AND f.evidence_id=s.evidence_id AND t.evidence_id=s.evidence_id
             AND c.evidence_id=s.evidence_id
           ORDER BY s.corpus_id, s.source_entry_id, a.assertion_id""",
        (parser_version,),
    ).fetchall()
    for row in formula_rows:
        source_key = (str(row[0]), str(row[1]), str(row[2]))
        if source_key not in expected_set:
            continue
        owner_id = f"{row[0]}:{row[1]}"
        assertion_id = str(row[5])
        spec = RelationSupportSpec(
            assertion_id=assertion_id,
            basis=str(row[6]),
            confidence=max(float(row[4]), float(row[7])),
            properties=_relation_properties(row[8]),
            evidence_ids=(str(row[3]),),
        )
        parser_scopes[owner_id][assertion_id] = _merge_specs(
            parser_scopes[owner_id].get(assertion_id), spec
        )
    scope_count_mismatches = []
    scope_by_id = {str(row[0]): row for row in scope_rows}
    for owner_id, desired in parser_scopes.items():
        row = scope_by_id.get(owner_id)
        if row is not None and int(row[2]) != len(desired):
            scope_count_mismatches.append(owner_id)
    if scope_count_mismatches:
        refusal_reasons.append("parser scope desired counts disagree with v7 formula links")

    morphology_rows = connection.execute(
        """SELECT a.assertion_id, a.subject_entity_id, a.source_entity_id,
                  a.target_entity_id, a.relation, a.basis, a.confidence,
                  a.properties_json, a.created_at
           FROM relation_assertions a JOIN morphemes m
             ON m.entity_id=a.target_entity_id
           WHERE a.status='accepted' AND a.relation IN
                 ('has-component','shares-component')
           ORDER BY a.assertion_id"""
    ).fetchall()
    morphology = {str(row[0]): row for row in morphology_rows}

    draft_term_promotions = _plan_direct_draft_term_promotions(connection)
    atomic_plan = _plan_accepted_atomic_lineage(
        connection, parser_version=parser_version,
        prospective_term_ids=set(draft_term_promotions),
    )
    owner_plans = dict(atomic_plan["owner_plans"])
    owner_versions = dict(atomic_plan["owner_versions"])
    artifact_errors = list(atomic_plan["errors"])
    if artifact_errors:
        refusal_reasons.append("accepted atomic split artifacts are not attributable")

    for assertion_id, row in morphology.items():
        if any(key[0] == assertion_id for key in owner_plans):
            continue
        event = connection.execute(
            """SELECT event_id, action, after_json, job_id, validator_version
               FROM relation_assertion_events
               WHERE assertion_id=? AND action IN ('accept','update')
               ORDER BY created_at DESC, event_id DESC LIMIT 1""",
            (assertion_id,),
        ).fetchone()
        if event is None:
            continue
        if event[3]:
            job = connection.execute(
                "SELECT status FROM preparation_jobs WHERE job_id=?", (event[3],)
            ).fetchone()
            if job is None or str(job[0]) != "complete":
                continue
        try:
            after = json.loads(str(event[2]))
        except (TypeError, ValueError, json.JSONDecodeError):
            after = {}
        if str(after.get("assertion_id", "")) != assertion_id:
            continue
        owner_type = "relation-review" if str(row[5]) == "reviewed" else "relation-decision"
        owner_id = str(row[1])
        owner_key = (assertion_id, owner_type, owner_id)
        owner_plans[owner_key] = RelationSupportSpec(
            assertion_id, str(row[5]), float(row[6]), _relation_properties(row[7]),
            _assertion_evidence(connection, assertion_id)
            if str(row[5]) in {"book", "reviewed"} else (),
        )
        owner_versions[owner_key] = str(event[4] or "relation-assertion-decision-v1")

    entity_states = {
        str(row[0]): (str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT entity_id,entity_type,status FROM entities"
        )
    }
    invalid_endpoint_assertions: set[str] = set()
    invalid_draft_term_ids: set[str] = set()
    prospective_accepted = set(draft_term_promotions)
    for assertion_id, row in morphology.items():
        invalid = False
        for endpoint_id in (str(row[1]), str(row[2]), str(row[3])):
            entity_type, status = entity_states.get(endpoint_id, ("", ""))
            if status == "accepted" or endpoint_id in prospective_accepted:
                continue
            invalid = True
            if entity_type == "term" and status == "draft":
                invalid_draft_term_ids.add(endpoint_id)
        if invalid:
            invalid_endpoint_assertions.add(assertion_id)
    if invalid_endpoint_assertions:
        for owner_key in tuple(owner_plans):
            if owner_key[0] in invalid_endpoint_assertions:
                owner_plans.pop(owner_key, None)
                owner_versions.pop(owner_key, None)
        for desired in parser_scopes.values():
            for assertion_id in invalid_endpoint_assertions:
                desired.pop(assertion_id, None)

    legacy_formula: dict[
        tuple[str, str, str, str], dict[str, Any]
    ] = {}
    for row in connection.execute(
        """SELECT t.materialized_entity_id, c.materialized_entity_id,
                  s.evidence_id, s.corpus_id, s.source_entry_id,
                  s.parser_version, f.created_at
           FROM morphology_source_observations_v3 s
           JOIN morphology_formula_links_v3 f USING(source_observation_id)
           JOIN morphology_term_observations_v3 t
             ON t.term_observation_id=f.term_observation_id
            AND t.source_observation_id=s.source_observation_id
           JOIN morphology_component_observations_v3 c
             ON c.component_observation_id=f.component_observation_id
            AND c.source_observation_id=s.source_observation_id
           WHERE s.parser_version<>? AND f.state='materialized'
             AND t.state='materialized' AND c.state='materialized'
             AND t.materialized_entity_id IS NOT NULL
             AND c.materialized_entity_id IS NOT NULL
             AND t.clause_index=f.clause_index AND c.clause_index=f.clause_index
             AND f.evidence_id=s.evidence_id AND t.evidence_id=s.evidence_id
             AND c.evidence_id=s.evidence_id""",
        (parser_version,),
    ):
        key = (str(row[0]), str(row[0]), str(row[1]), "has-component")
        item = legacy_formula.setdefault(key, {"evidence": set(), "owners": {}})
        item["evidence"].add(str(row[2]))
        item["owners"].setdefault(f"{row[3]}:{row[4]}", set()).add(str(row[2]))

    existing_active: dict[str, list[tuple[str, str, str]]] = {}
    for row in connection.execute(
        """SELECT assertion_id, owner_type, owner_id FROM relation_assertion_supports
           WHERE status='accepted' ORDER BY assertion_id, owner_type, owner_id"""
    ):
        existing_active.setdefault(str(row[0]), []).append(
            (str(row[1]), str(row[2]), "accepted")
        )
    desired_v7_ids = {
        assertion_id
        for values in parser_scopes.values()
        for assertion_id in values
    }
    planned_live_ids = desired_v7_ids | {key[0] for key in owner_plans}
    stale_parser: dict[str, dict[str, set[str]]] = {}
    orphan_model: list[str] = []
    atomic_role_conflicts = set(atomic_plan["role_conflict_assertion_ids"])
    unknown_accepted: list[str] = []
    for assertion_id, row in morphology.items():
        active = existing_active.get(assertion_id, [])
        active_nonparser = [owner for owner in active if owner[0] != "morphology-parser"]
        if assertion_id in invalid_endpoint_assertions:
            continue
        if assertion_id in planned_live_ids or active_nonparser:
            continue
        if assertion_id in atomic_role_conflicts:
            continue
        triple = (str(row[1]), str(row[2]), str(row[3]), str(row[4]))
        proof = legacy_formula.get(triple)
        exact_evidence = set(_assertion_evidence(connection, assertion_id))
        proof_valid = bool(
            proof
            and proof["evidence"] == exact_evidence
            and exact_evidence
            and set(proof["owners"]).issubset(expected_owner_ids)
            and _automatic_non_authoritative_events(connection, assertion_id)
        )
        if proof_valid:
            stale_parser[assertion_id] = proof["owners"]
        elif (
            str(row[5]) == "model"
            and not exact_evidence
            and triple not in atomic_plan["accepted_revision_endpoints"]
            and proof is None
            and _legacy_model_baseline_matches(connection, row)
        ):
            orphan_model.append(assertion_id)
        else:
            unknown_accepted.append(assertion_id)
    if unknown_accepted:
        refusal_reasons.append("accepted morphology assertions remain unattributed")

    role_arbitration = _plan_exact_role_arbitration(
        connection,
        parser_version=parser_version,
        parser_scopes=parser_scopes,
        owner_plans=owner_plans,
        prospective_accepted_terms=prospective_accepted,
        excluded_assertion_ids=(
            invalid_endpoint_assertions | set(stale_parser) | set(orphan_model)
            | atomic_role_conflicts
        ),
    )
    role_arbitration_retire = set(role_arbitration["retire_assertion_ids"])
    if role_arbitration_retire:
        for owner_key in tuple(owner_plans):
            if owner_key[0] in role_arbitration_retire:
                owner_plans.pop(owner_key, None)
                owner_versions.pop(owner_key, None)
        for desired in parser_scopes.values():
            for assertion_id in role_arbitration_retire:
                desired.pop(assertion_id, None)
    desired_v7_ids = {
        assertion_id for values in parser_scopes.values() for assertion_id in values
    }
    planned_live_ids = desired_v7_ids | {key[0] for key in owner_plans}

    pilot_bypass = []
    if _table_exists(connection, "morphology_pilot_repairs_v3"):
        for row in connection.execute(
            """SELECT legacy_assertion_id FROM morphology_pilot_repairs_v3
               WHERE parser_version=? AND action IN ('archive','repoint')
                 AND legacy_assertion_id IS NOT NULL""", (parser_version,),
        ):
            assertion_id = str(row[0])
            history = int(connection.execute(
                "SELECT COUNT(*) FROM relation_assertion_supports WHERE assertion_id=?",
                (assertion_id,),
            ).fetchone()[0])
            if history == 0:
                pilot_bypass.append(assertion_id)
    if pilot_bypass:
        refusal_reasons.append("legacy pilot repair bypassed normalized ownership")

    alias_manifest = _defer_unproved_alias_term_occurrences(
        connection,
        morpheme_alias_migration_manifest(connection),
        atomic_plan["complete_term_morpheme_projections"],
    )
    prospective_zero = set(alias_manifest["zero_ledger_assertion_ids"])
    covered_zero = (
        planned_live_ids | set(stale_parser) | set(orphan_model)
        | atomic_role_conflicts | invalid_endpoint_assertions | role_arbitration_retire
    )
    unexplained_alias_zero = sorted(prospective_zero - covered_zero)
    projected_deferred_alias_groups = [
        {
            "language": group["language"], "kind": group["kind"],
            "base": group["base"],
            "unknown_entity_references": group["unknown_entity_references"],
            "unproved_term_morpheme_ids": list(
                group.get("unproved_term_morpheme_ids", ())
            ),
            "unexplained_zero_ledger_assertion_ids": sorted(
                set(group.get("zero_ledger_assertion_ids", ())) - covered_zero
            ),
        }
        for group in alias_manifest["groups"]
        if group["unknown_entity_references"]
        or group.get("unproved_term_morpheme_ids")
        or (
            set(group.get("zero_ledger_assertion_ids", ())) - covered_zero
        )
    ]

    completed = connection.execute(
        """SELECT coverage_fingerprint, completed_at
           FROM relation_support_migrations WHERE migration_key=?""",
        (migration_key,),
    ).fetchone()
    already_complete = bool(
        completed and str(completed[0]) == coverage_fingerprint and str(completed[1])
    )
    if completed and str(completed[0]) != coverage_fingerprint:
        refusal_reasons.append("migration key was completed for different immutable input")

    deferred_nonstructural = [str(row[0]) for row in connection.execute(
        """SELECT a.assertion_id FROM relation_assertions a JOIN morphemes m
             ON m.entity_id=a.target_entity_id
           WHERE a.status='accepted'
             AND a.relation NOT IN ('has-component','shares-component')
             AND NOT EXISTS (
               SELECT 1 FROM relation_assertion_supports s
               WHERE s.assertion_id=a.assertion_id AND s.status='accepted')
           ORDER BY a.assertion_id"""
    )]
    deferred_nonstructural_edges = [str(row[0]) for row in connection.execute(
        """SELECT edge.edge_id FROM entity_edges edge JOIN morphemes m
             ON m.entity_id=edge.target_entity_id
           WHERE edge.status='accepted'
             AND edge.relation NOT IN ('has-component','shares-component')
             AND NOT EXISTS (
               SELECT 1 FROM relation_assertions a
               WHERE a.source_entity_id=edge.source_entity_id
                 AND a.target_entity_id=edge.target_entity_id
                 AND a.relation=edge.relation AND a.status='accepted')
           ORDER BY edge.edge_id"""
    )]

    return {
        **empty_plan,
        "coverage_fingerprint": coverage_fingerprint,
        "observation_count": len(actual_observations),
        "missing_observations": [list(item) for item in missing_observations],
        "extra_observations": [list(item) for item in extra_observations],
        "missing_owner_scopes": [list(item) for item in missing_scopes],
        "extra_owner_scopes": [list(item) for item in extra_scopes],
        "latest_run_id": run_id,
        "latest_run_complete": run_valid,
        "source_manifest_valid": source_manifest_valid,
        "batches_complete": batch_valid,
        "nonterminal_observations": nonterminal,
        "source_child_count_mismatches": count_mismatches,
        "invalid_materialized_links": invalid_materialized_links,
        "scope_count_mismatch_owner_ids": scope_count_mismatches,
        "atomic_artifact_errors": sorted(set(artifact_errors)),
        "parser_scope_count": len(parser_scopes),
        "parser_support_count": sum(len(item) for item in parser_scopes.values()),
        "attributable_nonparser_support_count": len(owner_plans),
        "artifact_assertions_to_restore": atomic_plan["restore_assertion_ids"],
        "stale_parser_assertion_ids": sorted(stale_parser),
        "orphan_model_assertion_ids": sorted(orphan_model),
        "atomic_role_conflict_assertion_ids": sorted(atomic_role_conflicts),
        "draft_term_promotions": draft_term_promotions,
        "invalid_endpoint_assertion_ids": sorted(invalid_endpoint_assertions),
        "invalid_draft_term_ids": sorted(invalid_draft_term_ids),
        "deferred_nonstructural_assertion_ids": deferred_nonstructural,
        "deferred_nonstructural_edge_ids": deferred_nonstructural_edges,
        "role_arbitration_retired_assertion_ids": sorted(role_arbitration_retire),
        "role_arbitration_deferred": role_arbitration["deferred"],
        "role_arbitration_blocking": role_arbitration["blocking"],
        "unknown_accepted_morphology_assertion_ids": sorted(unknown_accepted),
        "pilot_repair_bypass_assertion_ids": sorted(set(pilot_bypass)),
        "alias_manifest": alias_manifest,
        "deferred_aliases": bool(projected_deferred_alias_groups),
        "projected_deferred_alias_groups": projected_deferred_alias_groups,
        "unexplained_alias_zero_ledger_assertion_ids": unexplained_alias_zero,
        "already_complete": already_complete,
        "refusal_reasons": sorted(set(refusal_reasons)),
        "destructive_ready": not refusal_reasons,
        "_internal": {
            "parser_scopes": parser_scopes,
            "owner_plans": owner_plans,
            "owner_versions": owner_versions,
            "artifact_assertions": atomic_plan["assertions"],
            "artifact_term_morphemes": atomic_plan["term_morphemes"],
            "complete_term_morpheme_projections": atomic_plan[
                "complete_term_morpheme_projections"
            ],
            "stale_parser": stale_parser,
            "orphan_model": tuple(orphan_model),
            "atomic_role_conflicts": tuple(sorted(atomic_role_conflicts)),
            "draft_term_promotions": draft_term_promotions,
            "invalid_endpoint_assertions": tuple(sorted(invalid_endpoint_assertions)),
            "invalid_draft_term_ids": tuple(sorted(invalid_draft_term_ids)),
            "role_arbitration_retire": tuple(sorted(role_arbitration_retire)),
            "role_arbitration_winning_slots": role_arbitration["winning_slots"],
            "morphology_assertion_ids": tuple(
                sorted(
                    set(morphology) | set(atomic_plan["assertions"])
                    | atomic_role_conflicts | invalid_endpoint_assertions
                    | role_arbitration_retire
                )
            ),
        },
    }


def morphology_v7_finalization_manifest(
    connection: sqlite3.Connection,
    *,
    parser_version: str,
    expected_sources: Sequence[tuple[str, str, str]],
    expected_source_records: int = PRODUCTION_MORPHOLOGY_RECORD_COUNT,
    migration_key: str = MORPHOLOGY_V7_MIGRATION_KEY,
) -> dict[str, Any]:
    """Return the public, non-mutating v7 finalization plan."""

    plan = _morphology_v7_plan(
        connection,
        parser_version=parser_version,
        expected_sources=expected_sources,
        expected_source_records=expected_source_records,
        migration_key=migration_key,
    )
    return {key: value for key, value in plan.items() if key != "_internal"}


def _support_matches(
    connection: sqlite3.Connection,
    *,
    assertion_id: str,
    owner_type: str,
    owner_id: str,
    owner_version: str,
    spec: RelationSupportSpec,
) -> bool:
    row = connection.execute(
        """SELECT support_id, owner_version, basis, confidence, status,
                  properties_json FROM relation_assertion_supports
           WHERE assertion_id=? AND owner_type=? AND owner_id=?""",
        (assertion_id, owner_type, owner_id),
    ).fetchone()
    return bool(
        row
        and str(row[1]) == owner_version
        and str(row[2]) == spec.basis
        and abs(float(row[3]) - float(spec.confidence)) <= 1e-12
        and str(row[4]) == "accepted"
        and str(row[5]) == _json(spec.properties)
        and _support_evidence(connection, str(row[0]))
        == tuple(sorted(set(spec.evidence_ids)))
    )


def _move_assertion_supports(
    connection: sqlite3.Connection,
    source_assertion_id: str,
    target_assertion_id: str,
    timestamp: str,
) -> set[tuple[str, str]]:
    owners: set[tuple[str, str]] = set()
    rows = connection.execute(
        """SELECT support_id, owner_type, owner_id, owner_version, basis,
                  confidence, status, properties_json
           FROM relation_assertion_supports WHERE assertion_id=?
           ORDER BY owner_type, owner_id""",
        (source_assertion_id,),
    ).fetchall()
    for row in rows:
        support_id, owner_type, owner_id = str(row[0]), str(row[1]), str(row[2])
        owners.add((owner_type, owner_id))
        evidence = set(_support_evidence(connection, support_id))
        existing = connection.execute(
            """SELECT support_id, owner_version, basis, confidence, status,
                      properties_json FROM relation_assertion_supports
               WHERE assertion_id=? AND owner_type=? AND owner_id=?""",
            (target_assertion_id, owner_type, owner_id),
        ).fetchone()
        if existing:
            evidence.update(_support_evidence(connection, str(existing[0])))
            rank = {"derived": 0, "model": 1, "book": 2, "reviewed": 3}
            source_rank = (rank.get(str(row[4]), -1), float(row[5]))
            target_rank = (rank.get(str(existing[2]), -1), float(existing[3]))
            preferred = row if source_rank > target_rank else existing
            desired_status = (
                "accepted" if "accepted" in {str(row[6]), str(existing[4])}
                else "retired"
            )
            connection.execute(
                """UPDATE relation_assertion_supports SET owner_version=?, basis=?,
                          confidence=?, status=?, properties_json=?, updated_at=?
                   WHERE support_id=?""",
                (
                    str(preferred[3] if preferred is row else preferred[1]),
                    str(preferred[4] if preferred is row else preferred[2]),
                    max(float(row[5]), float(existing[3])), desired_status,
                    str(preferred[7] if preferred is row else preferred[5]),
                    timestamp, str(existing[0]),
                ),
            )
            target_support_id = str(existing[0])
        else:
            target_support_id = _stable_id(
                "relation-support", target_assertion_id, owner_type, owner_id
            )
            connection.execute(
                """INSERT INTO relation_assertion_supports(
                       support_id, assertion_id, owner_type, owner_id,
                       owner_version, basis, confidence, status, properties_json,
                       created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    target_support_id, target_assertion_id, owner_type, owner_id,
                    str(row[3]), str(row[4]), float(row[5]), str(row[6]),
                    str(row[7]), timestamp, timestamp,
                ),
            )
        for evidence_id in sorted(evidence):
            connection.execute(
                """INSERT OR IGNORE INTO relation_assertion_support_evidence(
                       support_id, evidence_id) VALUES (?, ?)""",
                (target_support_id, evidence_id),
            )
        connection.execute(
            "DELETE FROM relation_assertion_support_evidence WHERE support_id=?",
            (support_id,),
        )
        connection.execute(
            "DELETE FROM relation_assertion_supports WHERE support_id=?", (support_id,)
        )
    return owners


def _refresh_owner_scope_fingerprint(
    connection: sqlite3.Connection, owner_type: str, owner_id: str, timestamp: str
) -> None:
    scope = connection.execute(
        """SELECT owner_version FROM relation_support_owner_scopes
           WHERE owner_type=? AND owner_id=?""", (owner_type, owner_id),
    ).fetchone()
    if scope is None:
        return
    specs = []
    for row in connection.execute(
        """SELECT support_id, assertion_id, basis, confidence, properties_json
           FROM relation_assertion_supports
           WHERE owner_type=? AND owner_id=? AND status='accepted'
           ORDER BY assertion_id""", (owner_type, owner_id),
    ):
        specs.append(RelationSupportSpec(
            str(row[1]), str(row[2]), float(row[3]), _relation_properties(row[4]),
            _support_evidence(connection, str(row[0])),
        ))
    connection.execute(
        """UPDATE relation_support_owner_scopes SET desired_count=?,
                  desired_fingerprint=?, updated_at=?
           WHERE owner_type=? AND owner_id=?""",
        (len(specs), _scope_fingerprint(specs), timestamp, owner_type, owner_id),
    )


def _apply_safe_aliases(
    connection: sqlite3.Connection, manifest: Mapping[str, Any], timestamp: str
) -> tuple[dict[str, int], set[str]]:
    counts = {"groups_merged": 0, "entities_archived": 0,
              "assertions_merged": 0, "assertions_rewired": 0,
              "edges_archived": 0, "edges_rewired": 0,
              "term_morphemes_rewired": 0, "entity_evidence_rewired": 0}
    changed_terms: set[str] = set()
    touched_assertions: set[str] = set()
    touched_owners: set[tuple[str, str]] = set()
    for group in manifest.get("groups", []):
        if group.get("deferred") or group.get("unknown_entity_references"):
            continue
        survivor = str(group["survivor_entity_id"])
        aliases = {str(item) for item in group.get("archive_entity_ids", [])}
        if not aliases:
            continue
        placeholders = ",".join("?" for _ in aliases)
        for action in group.get("assertion_actions", []):
            assertion_id = str(action["assertion_id"])
            current = connection.execute(
                """SELECT subject_entity_id, source_entity_id, target_entity_id,
                          relation, status FROM relation_assertions
                   WHERE assertion_id=?""", (assertion_id,),
            ).fetchone()
            if current is None:
                continue
            changed_terms.add(str(current[0]))
            collision_id = str(action.get("merge_into_assertion_id", ""))
            if collision_id:
                touched_owners.update(_move_assertion_supports(
                    connection, assertion_id, collision_id, timestamp
                ))
                connection.execute(
                    "DELETE FROM assertion_evidence WHERE assertion_id=?",
                    (assertion_id,),
                )
                connection.execute(
                    """UPDATE relation_assertions SET status='archived',
                              revision=revision+CASE WHEN status='accepted' THEN 1 ELSE 0 END,
                              updated_at=? WHERE assertion_id=?""",
                    (timestamp, assertion_id),
                )
                touched_assertions.add(collision_id)
                counts["assertions_merged"] += 1
            else:
                desired = (
                    str(action["desired_subject_entity_id"]),
                    str(action["desired_source_entity_id"]),
                    str(action["desired_target_entity_id"]), assertion_id,
                )
                if tuple(str(current[index]) for index in range(3)) != desired[:3]:
                    connection.execute(
                        """UPDATE relation_assertions SET subject_entity_id=?,
                                  source_entity_id=?, target_entity_id=?, updated_at=?
                           WHERE assertion_id=?""",
                        (*desired[:3], timestamp, assertion_id),
                    )
                    counts["assertions_rewired"] += 1
                touched_assertions.add(assertion_id)
        for action in group.get("edge_actions", []):
            edge_id = str(action["edge_id"])
            collision_id = str(action.get("merge_into_edge_id", ""))
            if collision_id:
                cursor = connection.execute(
                    """UPDATE entity_edges SET status='archived', updated_at=?
                       WHERE edge_id=? AND status='accepted'""", (timestamp, edge_id),
                )
                counts["edges_archived"] += max(0, int(cursor.rowcount))
            else:
                cursor = connection.execute(
                    """UPDATE entity_edges SET source_entity_id=?, target_entity_id=?,
                              updated_at=? WHERE edge_id=? AND
                              (source_entity_id<>? OR target_entity_id<>?)""",
                    (
                        action["desired_source_entity_id"], action["desired_target_entity_id"],
                        timestamp, edge_id, action["desired_source_entity_id"],
                        action["desired_target_entity_id"],
                    ),
                )
                counts["edges_rewired"] += max(0, int(cursor.rowcount))
        for action in group.get("term_morpheme_actions", []):
            term_id = str(action["term_id"])
            changed_terms.add(term_id)
            if action.get("merge_existing"):
                cursor = connection.execute(
                    """DELETE FROM term_morphemes
                       WHERE term_id=? AND morpheme_id=?""",
                    (term_id, action["from_morpheme_id"]),
                )
            else:
                cursor = connection.execute(
                    """UPDATE term_morphemes SET morpheme_id=?
                       WHERE term_id=? AND ordinal=? AND morpheme_id=?""",
                    (
                        action["to_morpheme_id"], term_id, action["ordinal"],
                        action["from_morpheme_id"],
                    ),
                )
            counts["term_morphemes_rewired"] += max(0, int(cursor.rowcount))
        for action in group.get("entity_evidence_actions", []):
            connection.execute(
                """INSERT OR IGNORE INTO entity_evidence(
                       entity_id, evidence_id, claim, confidence)
                   SELECT ?, evidence_id, claim, confidence FROM entity_evidence
                   WHERE entity_id=?""",
                (action["to_entity_id"], action["from_entity_id"]),
            )
            cursor = connection.execute(
                "DELETE FROM entity_evidence WHERE entity_id=?",
                (action["from_entity_id"],),
            )
            counts["entity_evidence_rewired"] += max(0, int(cursor.rowcount))
        if _table_exists(connection, "lexical_semantic_enrichments"):
            for alias_id in sorted(aliases):
                for state in connection.execute(
                    """SELECT language FROM lexical_semantic_enrichments
                       WHERE entity_id=? ORDER BY language""", (alias_id,),
                ).fetchall():
                    language = str(state[0])
                    collision = connection.execute(
                        """SELECT 1 FROM lexical_semantic_enrichments
                           WHERE entity_id=? AND language=?""",
                        (survivor, language),
                    ).fetchone()
                    if collision:
                        cursor = connection.execute(
                            """DELETE FROM lexical_semantic_enrichments
                               WHERE entity_id=? AND language=?""",
                            (alias_id, language),
                        )
                    else:
                        cursor = connection.execute(
                            """UPDATE lexical_semantic_enrichments
                               SET entity_id=?, status='pending',
                                   desired_fingerprint='', applied_fingerprint='',
                                   lease_token='', lease_expires_at='',
                                   enrichment_job_id=NULL, bound_fingerprint='',
                                   next_eligible_at='', updated_at=?
                               WHERE entity_id=? AND language=?""",
                            (survivor, timestamp, alias_id, language),
                        )
                    counts["semantic_enrichments_rewired"] = (
                        counts.get("semantic_enrichments_rewired", 0)
                        + max(0, int(cursor.rowcount))
                    )
        if _table_exists(connection, "meanings"):
            cursor = connection.execute(
                f"""UPDATE meanings SET subject_entity_id=?
                    WHERE subject_entity_id IN ({placeholders})""",
                (survivor, *sorted(aliases)),
            )
            counts["meanings_rewired"] = counts.get("meanings_rewired", 0) + max(
                0, int(cursor.rowcount)
            )
        if _table_exists(connection, "morphology_component_observations_v3"):
            cursor = connection.execute(
                f"""UPDATE morphology_component_observations_v3
                    SET materialized_entity_id=?
                    WHERE materialized_entity_id IN ({placeholders})""",
                (survivor, *sorted(aliases)),
            )
            counts["component_observations_rewired"] = (
                counts.get("component_observations_rewired", 0)
                + max(0, int(cursor.rowcount))
            )
        connection.execute(
            """UPDATE morphemes SET form=?, normalized=? WHERE entity_id=?""",
            (group["desired_form"], group["base"], survivor),
        )
        connection.execute(
            """UPDATE entities SET canonical_key=?, label=?, status='accepted',
                      updated_at=? WHERE entity_id=?""",
            (group["desired_canonical_key"], group["desired_form"], timestamp, survivor),
        )
        live_alias_references = {
            "assertions": int(connection.execute(
                f"""SELECT COUNT(*) FROM relation_assertions
                    WHERE status='accepted' AND (
                      subject_entity_id IN ({placeholders})
                      OR source_entity_id IN ({placeholders})
                      OR target_entity_id IN ({placeholders}))""",
                (*sorted(aliases), *sorted(aliases), *sorted(aliases)),
            ).fetchone()[0]),
            "edges": int(connection.execute(
                f"""SELECT COUNT(*) FROM entity_edges
                    WHERE status='accepted' AND (
                      source_entity_id IN ({placeholders})
                      OR target_entity_id IN ({placeholders}))""",
                (*sorted(aliases), *sorted(aliases)),
            ).fetchone()[0]),
            "term_morphemes": int(connection.execute(
                f"""SELECT COUNT(*) FROM term_morphemes
                    WHERE term_id IN ({placeholders})
                       OR morpheme_id IN ({placeholders})""",
                (*sorted(aliases), *sorted(aliases)),
            ).fetchone()[0]),
        }
        if any(live_alias_references.values()):
            raise RuntimeError(
                "alias group retained live references before archive: "
                f"{group['language']}:{group['kind']}:{group['base']} "
                f"{_arbitrary_json(live_alias_references)}"
            )
        cursor = connection.execute(
            f"""UPDATE entities SET status='archived', updated_at=?
                WHERE entity_id IN ({placeholders}) AND status<>'archived'""",
            (timestamp, *sorted(aliases)),
        )
        counts["entities_archived"] += max(0, int(cursor.rowcount))
        counts["groups_merged"] += 1
    if touched_assertions:
        reduced = reduce_relation_assertions(
            connection, touched_assertions, timestamp=timestamp
        )
        for key, value in reduced.items():
            counts[f"reducer_{key}"] = int(value)
    for owner_type, owner_id in sorted(touched_owners):
        _refresh_owner_scope_fingerprint(
            connection, owner_type, owner_id, timestamp
        )
    return counts, changed_terms


def _reconcile_complete_atomic_term_morphemes(
    connection: sqlite3.Connection,
    projections: Mapping[str, Mapping[int, Mapping[str, Any]]],
    alias_manifest: Mapping[str, Any],
    *,
    timestamp: str,
) -> tuple[dict[str, int], set[str], dict[str, dict[int, tuple[Any, ...]]], list[str]]:
    """Apply only evidence-proven, complete primary occurrence projections."""

    alias_ids = _executable_alias_ids(alias_manifest)

    counts = {"updated": 0, "removed": 0, "deferred": 0}
    changed_terms: set[str] = set()
    reconciled: dict[str, dict[int, tuple[Any, ...]]] = {}
    deferred: list[str] = []
    for term_id, raw_projection in sorted(projections.items()):
        desired, actual, safe = _complete_atomic_term_morpheme_state(
            connection, term_id, raw_projection, alias_ids
        )
        if not safe:
            counts["deferred"] += 1
            deferred.append(term_id)
            continue

        desired_ordinals = set(desired)
        for ordinal, value in sorted(desired.items()):
            prior = next((row for row in actual if int(row[0]) == ordinal), None)
            prior_value = None if prior is None else (
                str(prior[1]), str(prior[2]), float(prior[3]), str(prior[4])
            )
            if prior_value != value:
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(term_id,ordinal) DO UPDATE SET
                         morpheme_id=excluded.morpheme_id,
                         surface=excluded.surface,
                         confidence=excluded.confidence,
                         basis=excluded.basis""",
                    (term_id, value[0], ordinal, value[1], value[2], value[3]),
                )
                counts["updated"] += 1
        stale_ordinals = sorted({int(row[0]) for row in actual} - desired_ordinals)
        if stale_ordinals:
            placeholders = ",".join("?" for _ in stale_ordinals)
            cursor = connection.execute(
                f"""DELETE FROM term_morphemes WHERE term_id=?
                    AND ordinal IN ({placeholders})""",
                (term_id, *stale_ordinals),
            )
            counts["removed"] += max(0, int(cursor.rowcount))
        if any(
            int(row[0]) not in desired_ordinals
            or (str(row[1]), str(row[2]), float(row[3]), str(row[4]))
            != desired.get(int(row[0]))
            for row in actual
        ):
            changed_terms.add(term_id)
        reconciled[term_id] = desired
    return counts, changed_terms, reconciled, deferred


def _executable_alias_ids(manifest: Mapping[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for group in manifest.get("groups", ()):
        if group.get("deferred") or group.get("unknown_entity_references"):
            continue
        survivor = str(group["survivor_entity_id"])
        for entity_id in group.get("archive_entity_ids", ()):
            aliases[str(entity_id)] = survivor
    return aliases


def _executable_assertion_alias_ids(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Return the final assertion owner for every executable collision merge."""

    aliases: dict[str, str] = {}
    for group in manifest.get("groups", ()):
        if group.get("deferred") or group.get("unknown_entity_references"):
            continue
        for action in group.get("assertion_actions", ()):
            assertion_id = str(action.get("assertion_id", ""))
            survivor_id = str(action.get("merge_into_assertion_id", ""))
            if assertion_id and survivor_id:
                aliases[assertion_id] = survivor_id
    for assertion_id in tuple(aliases):
        survivor_id = aliases[assertion_id]
        visited = {assertion_id}
        while survivor_id in aliases and survivor_id not in visited:
            visited.add(survivor_id)
            survivor_id = aliases[survivor_id]
        aliases[assertion_id] = survivor_id
    return aliases


def _complete_atomic_term_morpheme_state(
    connection: sqlite3.Connection,
    term_id: str,
    raw_projection: Mapping[int, Mapping[str, Any]],
    alias_ids: Mapping[str, str],
    assertion_alias_ids: Mapping[str, str] | None = None,
) -> tuple[dict[int, tuple[Any, ...]], list[sqlite3.Row], bool]:
    """Evaluate the exact occurrence/evidence predicate without writing."""

    desired: dict[int, tuple[Any, ...]] = {}
    desired_evidence: dict[tuple[str, str], set[str]] = {}
    for raw_ordinal, link in sorted(raw_projection.items()):
        ordinal = int(raw_ordinal)
        morpheme_id = alias_ids.get(
            str(link["morpheme_id"]), str(link["morpheme_id"])
        )
        surface = str(link["surface"])
        desired[ordinal] = (
            morpheme_id,
            surface,
            float(link["confidence"]),
            str(link["basis"]),
        )
        desired_evidence.setdefault(
            (morpheme_id, _normalise_term_text(surface)), set()
        ).update(str(value) for value in link.get("evidence_ids", ()))

    actual = connection.execute(
        """SELECT ordinal,morpheme_id,surface,confidence,basis
           FROM term_morphemes WHERE term_id=? ORDER BY ordinal""",
        (term_id,),
    ).fetchall()
    accepted_rows = connection.execute(
        """SELECT a.assertion_id,a.target_entity_id
           FROM relation_assertions a
           WHERE a.subject_entity_id=? AND a.source_entity_id=?
             AND a.relation='has-component' AND a.status='accepted'
             AND EXISTS (
               SELECT 1 FROM relation_assertion_supports s
               WHERE s.assertion_id=a.assertion_id AND s.status='accepted')
           ORDER BY a.assertion_id""",
        (term_id, term_id),
    ).fetchall()
    accepted_targets = {
        alias_ids.get(str(row[1]), str(row[1])) for row in accepted_rows
    }
    if accepted_targets != {str(value[0]) for value in desired.values()}:
        return desired, actual, False
    final_assertion_ids: dict[str, set[str]] = {}
    assertion_alias_ids = assertion_alias_ids or {}
    for assertion_id, target_id in accepted_rows:
        canonical_target = alias_ids.get(str(target_id), str(target_id))
        final_assertion_ids.setdefault(canonical_target, set()).add(
            assertion_alias_ids.get(str(assertion_id), str(assertion_id))
        )
    desired_target_counts: dict[str, int] = {}
    for value in desired.values():
        target_id = str(value[0])
        desired_target_counts[target_id] = desired_target_counts.get(target_id, 0) + 1
    if any(
        count == 1 and len(final_assertion_ids.get(target_id, set())) != 1
        for target_id, count in desired_target_counts.items()
    ):
        return desired, actual, False
    assertion_evidence: dict[str, set[str]] = {}
    for assertion_id, target_id in accepted_rows:
        canonical_target = alias_ids.get(str(target_id), str(target_id))
        evidence = set(_assertion_evidence(connection, str(assertion_id)))
        evidence.update(
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT support_evidence.evidence_id
                   FROM relation_assertion_supports support
                   JOIN relation_assertion_support_evidence support_evidence
                     ON support_evidence.support_id=support.support_id
                   WHERE support.assertion_id=? AND support.status='accepted'
                   ORDER BY support_evidence.evidence_id""",
                (assertion_id,),
            )
        )
        assertion_evidence.setdefault(canonical_target, set()).update(evidence)

    desired_ordinals = set(desired)
    for row in actual:
        ordinal = int(row[0])
        current = (
            alias_ids.get(str(row[1]), str(row[1])),
            str(row[2]),
            float(row[3]),
            str(row[4]),
        )
        if ordinal in desired_ordinals and current == desired[ordinal]:
            continue
        semantic_key = (current[0], _normalise_term_text(current[1]))
        evidence = desired_evidence.get(semantic_key, set())
        if not evidence or not evidence.intersection(
            assertion_evidence.get(current[0], set())
        ):
            return desired, actual, False
    return desired, actual, True


def _defer_unproved_alias_term_occurrences(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    projections: Mapping[str, Mapping[int, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Require every alias-backed term row to pass the complete proof gate."""

    while True:
        alias_ids = _executable_alias_ids(manifest)
        assertion_alias_ids = _executable_assertion_alias_ids(manifest)
        safe_terms = {
            str(term_id)
            for term_id, projection in projections.items()
            if _complete_atomic_term_morpheme_state(
                connection,
                str(term_id),
                projection,
                alias_ids,
                assertion_alias_ids,
            )[2]
        }
        changed = False
        for group in manifest.get("groups", ()):
            if group.get("deferred"):
                continue
            terms = {
                str(action["term_id"])
                for action in group.get("term_morpheme_actions", ())
            }
            unproved = set(terms - safe_terms)
            for action in group.get("term_morpheme_actions", ()):
                term_id = str(action["term_id"])
                projection = projections.get(term_id)
                if projection is None:
                    unproved.add(term_id)
                    continue
                canonical_target = alias_ids.get(
                    str(action["to_morpheme_id"]),
                    str(action["to_morpheme_id"]),
                )
                target_ordinals = [
                    int(ordinal)
                    for ordinal, link in projection.items()
                    if alias_ids.get(
                        str(link["morpheme_id"]), str(link["morpheme_id"])
                    ) == canonical_target
                ]
                if target_ordinals != [int(action["ordinal"])]:
                    unproved.add(term_id)
            unproved_ids = sorted(unproved)
            if unproved_ids:
                group["deferred"] = True
                group["deferred_reason"] = "unproved-complete-term-occurrence"
                group["unproved_term_morpheme_ids"] = unproved_ids
                changed = True
        if not changed:
            break
    groups = list(manifest.get("groups", ()))
    manifest["executable_group_count"] = sum(
        not group.get("deferred") for group in groups
    )
    manifest["deferred_group_count"] = sum(
        bool(group.get("deferred")) for group in groups
    )
    manifest["coverage_fingerprint"] = hashlib.sha256(
        _arbitrary_json(
            {key: value for key, value in manifest.items()
             if key != "coverage_fingerprint"}
        ).encode()
    ).hexdigest()
    return manifest


def _component_slot_key(term_id: str, ordinal: int) -> str:
    digest = hashlib.sha256(
        _arbitrary_json(
            {
                "family": "component",
                "values": {
                    "subject": str(term_id),
                    "branch": "primary",
                    "ordinal": int(ordinal),
                },
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"relation-slot:component:{digest}"


def _bind_complete_atomic_component_slots(
    connection: sqlite3.Connection,
    expected: Mapping[str, Mapping[int, tuple[Any, ...]]],
    *,
    timestamp: str,
) -> tuple[dict[str, int], set[str], dict[str, dict[int, tuple[Any, ...]]]]:
    """Bind each uniquely represented occurrence to its accepted assertion."""

    counts = {"slots_bound": 0, "assertions_updated": 0, "occurrences_updated": 0}
    changed_terms: set[str] = set()
    authoritative: dict[str, dict[int, tuple[Any, ...]]] = {}
    for term_id, projection in sorted(expected.items()):
        target_counts: dict[str, int] = {}
        for value in projection.values():
            target_id = str(value[0])
            target_counts[target_id] = target_counts.get(target_id, 0) + 1
        authoritative[term_id] = dict(projection)
        for ordinal, value in sorted(projection.items()):
            target_id, surface = str(value[0]), str(value[1])
            if target_counts[target_id] != 1:
                continue
            rows = connection.execute(
                """SELECT a.assertion_id,a.basis,a.confidence,a.properties_json,
                          m.form,m.kind
                   FROM relation_assertions a JOIN morphemes m
                     ON m.entity_id=a.target_entity_id
                   WHERE a.subject_entity_id=? AND a.source_entity_id=?
                     AND a.target_entity_id=? AND a.relation='has-component'
                     AND a.status='accepted'
                     AND EXISTS (
                       SELECT 1 FROM relation_assertion_supports support
                       WHERE support.assertion_id=a.assertion_id
                         AND support.status='accepted')
                   ORDER BY a.assertion_id""",
                (term_id, term_id, target_id),
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    "complete atomic occurrence lacks one accepted slot owner: "
                    f"{term_id}:{ordinal}:{target_id}"
                )
            row = rows[0]
            assertion_id = str(row[0])
            properties = _relation_properties(row[3])
            desired_properties = {
                **properties,
                "ordinal": int(ordinal),
                "surface": surface,
                "canonical_form": str(row[4]),
                "component_kind": str(row[5]),
                "branch_id": "primary",
            }
            encoded_properties = _json(desired_properties)
            if encoded_properties != str(row[3] or "{}"):
                connection.execute(
                    """UPDATE relation_assertions
                       SET properties_json=?,revision=revision+1,updated_at=?
                       WHERE assertion_id=?""",
                    (encoded_properties, timestamp, assertion_id),
                )
                counts["assertions_updated"] += 1
                changed_terms.add(term_id)
            slot_key = _component_slot_key(term_id, ordinal)
            current_slot = connection.execute(
                """SELECT slot_key,slot_mode,policy_version
                   FROM relation_assertion_slots WHERE assertion_id=?""",
                (assertion_id,),
            ).fetchone()
            desired_slot = (slot_key, "exclusive", "relation-slot-v2")
            if current_slot is None or tuple(str(item) for item in current_slot) != desired_slot:
                connection.execute(
                    """INSERT INTO relation_assertion_slots(
                           assertion_id,slot_key,slot_mode,policy_version,created_at)
                       VALUES (?,?,'exclusive','relation-slot-v2',?)
                       ON CONFLICT(assertion_id) DO UPDATE SET
                         slot_key=excluded.slot_key,
                         slot_mode=excluded.slot_mode,
                         policy_version=excluded.policy_version""",
                    (assertion_id, slot_key, timestamp),
                )
                counts["slots_bound"] += 1
                changed_terms.add(term_id)
            authority_value = (
                target_id,
                surface,
                float(row[2]),
                str(row[1]),
            )
            authoritative[term_id][int(ordinal)] = authority_value
            current_occurrence = connection.execute(
                """SELECT morpheme_id,surface,confidence,basis
                   FROM term_morphemes WHERE term_id=? AND ordinal=?""",
                (term_id, ordinal),
            ).fetchone()
            if current_occurrence is None or (
                str(current_occurrence[0]),
                str(current_occurrence[1]),
                float(current_occurrence[2]),
                str(current_occurrence[3]),
            ) != authority_value:
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(term_id,ordinal) DO UPDATE SET
                         morpheme_id=excluded.morpheme_id,
                         surface=excluded.surface,
                         confidence=excluded.confidence,
                         basis=excluded.basis""",
                    (term_id, target_id, ordinal, surface, row[2], row[1]),
                )
                counts["occurrences_updated"] += 1
                changed_terms.add(term_id)
    return counts, changed_terms, authoritative


def _neutralize_shadowed_complete_component_slots(
    connection: sqlite3.Connection,
    expected: Mapping[str, Mapping[int, tuple[Any, ...]]],
) -> int:
    """Stop archived legacy slots from deleting exact artifact occurrences."""

    changed = 0
    for term_id, projection in sorted(expected.items()):
        desired_ordinals = set(projection)
        rows = connection.execute(
            """SELECT slot.assertion_id,slot.slot_key,a.properties_json
               FROM relation_assertion_slots slot
               JOIN relation_assertions a ON a.assertion_id=slot.assertion_id
               WHERE a.subject_entity_id=? AND a.source_entity_id=?
                 AND a.relation='has-component' AND a.status<>'accepted'
                 AND slot.slot_mode='exclusive'
                 AND slot.slot_key LIKE 'relation-slot:component:%'
               ORDER BY slot.assertion_id""",
            (term_id, term_id),
        ).fetchall()
        for assertion_id, slot_key, raw_properties in rows:
            properties = _relation_properties(raw_properties)
            ordinal = properties.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int):
                continue
            if ordinal not in desired_ordinals:
                continue
            accepted = connection.execute(
                """SELECT 1 FROM relation_assertion_slots slot
                   JOIN relation_assertions a
                     ON a.assertion_id=slot.assertion_id
                   WHERE slot.slot_key=? AND slot.slot_mode='exclusive'
                     AND a.status='accepted' LIMIT 1""",
                (slot_key,),
            ).fetchone()
            if accepted is not None:
                continue
            cursor = connection.execute(
                """UPDATE relation_assertion_slots SET slot_mode='none'
                   WHERE assertion_id=? AND slot_mode='exclusive'""",
                (assertion_id,),
            )
            changed += max(0, int(cursor.rowcount))
    return changed


def _assert_complete_atomic_term_morphemes(
    connection: sqlite3.Connection,
    expected: Mapping[str, Mapping[int, tuple[Any, ...]]],
) -> None:
    failures: dict[str, Any] = {}
    for term_id, projection in sorted(expected.items()):
        actual = {
            int(row[0]): (str(row[1]), str(row[2]), float(row[3]), str(row[4]))
            for row in connection.execute(
                """SELECT ordinal,morpheme_id,surface,confidence,basis
                   FROM term_morphemes WHERE term_id=? ORDER BY ordinal""",
                (term_id,),
            )
        }
        if actual != dict(projection):
            failures[term_id] = {"expected": dict(projection), "actual": actual}
        target_counts: dict[str, int] = {}
        for value in projection.values():
            target_id = str(value[0])
            target_counts[target_id] = target_counts.get(target_id, 0) + 1
        for ordinal, value in sorted(projection.items()):
            target_id, surface = str(value[0]), str(value[1])
            if target_counts[target_id] != 1:
                continue
            slot_key = _component_slot_key(term_id, ordinal)
            owners = connection.execute(
                """SELECT a.assertion_id,a.basis,a.confidence,a.properties_json
                   FROM relation_assertion_slots slot
                   JOIN relation_assertions a
                     ON a.assertion_id=slot.assertion_id
                   WHERE slot.slot_key=? AND slot.slot_mode='exclusive'
                     AND slot.policy_version='relation-slot-v2'
                     AND a.subject_entity_id=? AND a.source_entity_id=?
                     AND a.target_entity_id=? AND a.relation='has-component'
                     AND a.status='accepted'
                     AND EXISTS (
                       SELECT 1 FROM relation_assertion_supports support
                       WHERE support.assertion_id=a.assertion_id
                         AND support.status='accepted')
                   ORDER BY a.assertion_id""",
                (slot_key, term_id, term_id, target_id),
            ).fetchall()
            valid_owners = []
            for owner in owners:
                properties = _relation_properties(owner[3])
                branch = next(
                    (
                        str(properties[name]).strip().casefold()
                        for name in (
                            "branch_id",
                            "decomposition_branch",
                            "analysis_branch",
                            "branch",
                        )
                        if str(properties.get(name, "")).strip()
                    ),
                    "primary",
                )
                if (
                    properties.get("ordinal") == ordinal
                    and str(properties.get("surface", "")) == surface
                    and branch == "primary"
                    and str(owner[1]) == str(value[3])
                    and abs(float(owner[2]) - float(value[2])) <= 1e-12
                ):
                    valid_owners.append(str(owner[0]))
            if len(valid_owners) != 1:
                failures.setdefault(term_id, {})[
                    f"accepted_component_slot_owner:{ordinal}"
                ] = valid_owners
        for row in connection.execute(
            """SELECT slot.assertion_id,slot.slot_key,a.properties_json
               FROM relation_assertion_slots slot
               JOIN relation_assertions a ON a.assertion_id=slot.assertion_id
               WHERE a.subject_entity_id=? AND a.source_entity_id=?
                 AND a.relation='has-component' AND a.status<>'accepted'
                 AND slot.slot_mode='exclusive'
                 AND slot.slot_key LIKE 'relation-slot:component:%'
               ORDER BY slot.assertion_id""",
            (term_id, term_id),
        ):
            properties = _relation_properties(row[2])
            ordinal = properties.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int):
                continue
            if ordinal not in projection:
                continue
            accepted = connection.execute(
                """SELECT 1 FROM relation_assertion_slots slot
                   JOIN relation_assertions a
                     ON a.assertion_id=slot.assertion_id
                   WHERE slot.slot_key=? AND slot.slot_mode='exclusive'
                     AND a.status='accepted' LIMIT 1""",
                (row[1],),
            ).fetchone()
            if accepted is None:
                failures.setdefault(term_id, {})[
                    "shadowed_component_slot"
                ] = str(row[0])
    if failures:
        raise RuntimeError(
            "complete atomic term-morpheme projection mismatch: "
            + _arbitrary_json(failures)
        )


def _complete_atomic_occurrence_fingerprints(
    connection: sqlite3.Connection, term_ids: Iterable[str]
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for term_id in sorted({str(value) for value in term_ids}):
        occurrences = [
            {
                "ordinal": int(row[0]),
                "morpheme_id": str(row[1]),
                "surface": str(row[2]),
                "confidence": float(row[3]),
                "basis": str(row[4]),
                "language": str(row[5]),
                "kind": str(row[6]),
                "normalized": str(row[7]),
            }
            for row in connection.execute(
                """SELECT tm.ordinal,tm.morpheme_id,tm.surface,tm.confidence,
                          tm.basis,m.language,m.kind,m.normalized
                   FROM term_morphemes tm JOIN morphemes m
                     ON m.entity_id=tm.morpheme_id
                   WHERE tm.term_id=? ORDER BY tm.ordinal""",
                (term_id,),
            )
        ]
        slots = [
            {
                "assertion_id": str(row[0]),
                "assertion_status": str(row[1]),
                "target_entity_id": str(row[2]),
                "properties": _relation_properties(row[3]),
                "slot_key": str(row[4]),
                "slot_mode": str(row[5]),
                "policy_version": str(row[6]),
            }
            for row in connection.execute(
                """SELECT a.assertion_id,a.status,a.target_entity_id,
                          a.properties_json,slot.slot_key,slot.slot_mode,
                          slot.policy_version
                   FROM relation_assertions a
                   JOIN relation_assertion_slots slot
                     ON slot.assertion_id=a.assertion_id
                   WHERE a.subject_entity_id=? AND a.source_entity_id=?
                     AND a.relation='has-component'
                     AND slot.slot_key LIKE 'relation-slot:component:%'
                   ORDER BY a.assertion_id""",
                (term_id, term_id),
            )
        ]
        fingerprints[term_id] = hashlib.sha256(
            _arbitrary_json(
                {"occurrences": occurrences, "component_slots": slots}
            ).encode()
        ).hexdigest()
    return fingerprints


def _alias_group_key(group: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(group["language"]), str(group["kind"]), str(group["base"]))


def _restrict_aliases_to_audited_groups(
    current: Mapping[str, Any], audited: Mapping[str, Any]
) -> dict[str, Any]:
    """Never promote a group that the immutable preflight marked deferred."""

    executable = {
        _alias_group_key(group)
        for group in audited.get("groups", [])
        if not group.get("deferred")
    }
    groups: list[dict[str, Any]] = []
    for raw in current.get("groups", []):
        group = dict(raw)
        if _alias_group_key(group) not in executable:
            group["deferred"] = True
            group["deferred_reason"] = "deferred-by-preflight"
        groups.append(group)
    result = {**current, "groups": groups}
    result["executable_group_count"] = sum(not group.get("deferred") for group in groups)
    result["deferred_group_count"] = sum(bool(group.get("deferred")) for group in groups)
    result["destructive_ready"] = not result["deferred_group_count"]
    result["coverage_fingerprint"] = hashlib.sha256(
        _arbitrary_json({key: value for key, value in result.items()
                         if key != "coverage_fingerprint"}).encode()
    ).hexdigest()
    return result


def _assert_morphology_finalization_invariants(
    connection: sqlite3.Connection,
) -> None:
    """Refuse a commit unless normalized morphology projections are exact."""

    failures: dict[str, Any] = {}
    invalid_assertion_endpoints = [str(row[0]) for row in connection.execute(
        """SELECT DISTINCT a.assertion_id
           FROM relation_assertions a JOIN morphemes m
             ON m.entity_id=a.target_entity_id
           WHERE a.status='accepted'
             AND a.relation IN ('has-component','shares-component')
             AND EXISTS (
               SELECT 1 FROM entities e
               WHERE e.status<>'accepted' AND e.entity_id IN
                     (a.subject_entity_id,a.source_entity_id,a.target_entity_id))
           ORDER BY a.assertion_id"""
    )]
    if invalid_assertion_endpoints:
        failures["accepted_assertions_with_nonaccepted_endpoint"] = (
            invalid_assertion_endpoints[:20]
        )
    invalid_edge_endpoints = [str(row[0]) for row in connection.execute(
        """SELECT DISTINCT edge.edge_id
           FROM entity_edges edge JOIN morphemes m
             ON m.entity_id=edge.target_entity_id
           WHERE edge.status='accepted'
             AND edge.relation IN ('has-component','shares-component')
             AND EXISTS (
               SELECT 1 FROM entities e WHERE e.status<>'accepted'
                 AND e.entity_id IN (edge.source_entity_id,edge.target_entity_id))
           ORDER BY edge.edge_id"""
    )]
    if invalid_edge_endpoints:
        failures["accepted_edges_with_nonaccepted_endpoint"] = invalid_edge_endpoints[:20]
    invalid_slots = [f"{row[0]}:{row[1]}" for row in connection.execute(
        """SELECT tm.term_id,tm.ordinal FROM term_morphemes tm
           WHERE EXISTS (
             SELECT 1 FROM entities e WHERE e.status<>'accepted'
               AND e.entity_id IN (tm.term_id,tm.morpheme_id))
           ORDER BY tm.term_id,tm.ordinal"""
    )]
    if invalid_slots:
        failures["term_morphemes_with_nonaccepted_endpoint"] = invalid_slots[:20]

    evidence_mismatches: list[str] = []
    support_state_mismatches: list[str] = []
    for row in connection.execute(
        """SELECT a.assertion_id,a.status
           FROM relation_assertions a JOIN morphemes m
             ON m.entity_id=a.target_entity_id
           WHERE a.relation IN ('has-component','shares-component')
             AND (a.status='accepted' OR EXISTS (
               SELECT 1 FROM relation_assertion_supports s
               WHERE s.assertion_id=a.assertion_id AND s.status='accepted'))
           ORDER BY a.assertion_id"""
    ):
        assertion_id = str(row[0])
        active_support = connection.execute(
            """SELECT 1 FROM relation_assertion_supports
               WHERE assertion_id=? AND status='accepted' LIMIT 1""",
            (assertion_id,),
        ).fetchone() is not None
        if active_support and str(row[1]) != "accepted":
            support_state_mismatches.append(assertion_id)
        if set(_assertion_evidence(connection, assertion_id)) != {
            str(item[0]) for item in connection.execute(
                """SELECT DISTINCT se.evidence_id
                   FROM relation_assertion_supports s
                   JOIN relation_assertion_support_evidence se
                     ON se.support_id=s.support_id
                   WHERE s.assertion_id=? AND s.status='accepted'""",
                (assertion_id,),
            )
        }:
            evidence_mismatches.append(assertion_id)
    if support_state_mismatches:
        failures["active_support_on_nonaccepted_assertion"] = support_state_mismatches[:20]
    if evidence_mismatches:
        failures["support_assertion_evidence_mismatch"] = evidence_mismatches[:20]

    assertion_endpoints = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """SELECT DISTINCT a.source_entity_id,a.target_entity_id,a.relation
               FROM relation_assertions a JOIN morphemes m
                 ON m.entity_id=a.target_entity_id
               WHERE a.status='accepted'
                 AND a.relation IN ('has-component','shares-component')"""
        )
    }
    edge_endpoints = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """SELECT DISTINCT edge.source_entity_id,edge.target_entity_id,edge.relation
               FROM entity_edges edge JOIN morphemes m
                 ON m.entity_id=edge.target_entity_id
               WHERE edge.status='accepted'
                 AND edge.relation IN ('has-component','shares-component')"""
        )
    }
    if assertion_endpoints != edge_endpoints:
        failures["assertion_edge_missing"] = sorted(assertion_endpoints - edge_endpoints)[:20]
        failures["assertion_edge_extra"] = sorted(edge_endpoints - assertion_endpoints)[:20]

    supported_components = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """SELECT DISTINCT a.source_entity_id,a.target_entity_id
               FROM relation_assertions a JOIN morphemes m
                 ON m.entity_id=a.target_entity_id
               WHERE a.status='accepted' AND a.relation='has-component'
                 AND EXISTS (
                   SELECT 1 FROM relation_assertion_supports s
                   WHERE s.assertion_id=a.assertion_id AND s.status='accepted')"""
        )
    }
    term_components = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT DISTINCT term_id,morpheme_id FROM term_morphemes"
        )
    }
    if supported_components != term_components:
        failures["supported_component_slot_missing"] = sorted(
            supported_components - term_components
        )[:20]
        failures["component_slot_without_support"] = sorted(
            term_components - supported_components
        )[:20]
    failures = {key: value for key, value in failures.items() if value}
    if failures:
        raise RuntimeError(
            "morphology finalization invariant violation: " + _arbitrary_json(failures)
        )


def _invalidate_morphology_projections(
    connection: sqlite3.Connection, term_ids: Iterable[str], timestamp: str
) -> int:
    identifiers = sorted({str(item) for item in term_ids if str(item)})
    for term_id in identifiers:
        if connection.execute(
            "SELECT 1 FROM terms WHERE entity_id=?", (term_id,)
        ).fetchone() is None:
            continue
        connection.execute(
            """INSERT INTO lexical_projection_repairs(
                   term_entity_id, desired_fingerprint, desired_revision,
                   desired_modes_json, status, created_at, updated_at)
               VALUES (?, '', 0, '[]', 'pending', ?, ?)
               ON CONFLICT(term_entity_id) DO UPDATE SET
                   desired_fingerprint='', desired_revision=0,
                   desired_modes_json='[]', status='pending', lease_token='',
                   lease_expires_at='', compose_job_id=NULL,
                   bound_fingerprint='', failure_count=0,
                   next_eligible_at='', last_error='', updated_at=excluded.updated_at""",
            (term_id, timestamp, timestamp),
        )
    return len(identifiers)


def finalize_morphology_support_migration(
    connection: sqlite3.Connection,
    *,
    parser_version: str,
    expected_sources: Sequence[tuple[str, str, str]],
    expected_source_records: int = PRODUCTION_MORPHOLOGY_RECORD_COUNT,
    migration_key: str = MORPHOLOGY_V7_MIGRATION_KEY,
    allow_write: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Finalize v7 ownership, refusing every unattributed destructive change."""

    if connection.in_transaction:
        raise ValueError("morphology finalization requires no active transaction")
    expected_sources = _normalized_migration_sources(expected_sources)
    completed = _completed_morphology_migration_result(
        connection,
        migration_key=migration_key,
        parser_version=parser_version,
        expected_sources=expected_sources,
        dry_run=not allow_write,
    )
    if completed is not None:
        return completed
    if not allow_write:
        report = morphology_v7_finalization_manifest(
            connection, parser_version=parser_version,
            expected_sources=expected_sources,
            expected_source_records=expected_source_records,
            migration_key=migration_key,
        )
        return {**report, "dry_run": True, "applied": False}

    timestamp = timestamp or _now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _morphology_v7_plan(
            connection, parser_version=parser_version,
            expected_sources=expected_sources,
            expected_source_records=expected_source_records,
            migration_key=migration_key,
        )
        public = {key: value for key, value in plan.items() if key != "_internal"}
        if plan.get("already_complete"):
            connection.rollback()
            return {**public, "dry_run": False, "applied": False, "already_complete": True,
                    "changes": {}}
        if not plan.get("destructive_ready"):
            connection.rollback()
            return {**public, "dry_run": False, "applied": False, "changes": {}}

        internal = plan["_internal"]
        changes: dict[str, int] = {
            "parser_scopes_replaced": 0, "nonparser_supports_backfilled": 0,
            "legacy_parser_supports_recorded": 0,
            "legacy_model_orphans_recorded": 0,
            "atomic_role_conflicts_recorded": 0,
            "artifact_assertions_inserted": 0,
            "artifact_term_morphemes_restored": 0,
            "draft_terms_promoted": 0,
            "invalid_endpoint_assertions_quarantined": 0,
            "draft_terms_archived": 0,
            "model_role_conflicts_retired": 0,
            "typed_role_slots_projected": 0,
            "final_edges_archived": 0,
            "final_edges_changed": 0,
            "post_role_arbitration_remaining": 0,
            "role_review_subjects_deferred": 0,
        }
        touched_assertions: set[str] = set()
        changed_terms: set[str] = set()
        deferred_role_subjects = {
            str(item["term_id"])
            for item in public.get("role_arbitration_deferred", [])
            if str(item.get("term_id", ""))
        }
        changed_terms.update(deferred_role_subjects)
        changes["role_review_subjects_deferred"] = len(deferred_role_subjects)

        for term_id, proof in sorted(internal["draft_term_promotions"].items()):
            current = connection.execute(
                "SELECT status FROM entities WHERE entity_id=?", (term_id,),
            ).fetchone()
            if current is None or str(current[0]) == "accepted":
                continue
            if str(current[0]) != "draft":
                raise RuntimeError(f"draft promotion status changed for {term_id}")
            previous = connection.execute(
                """SELECT revision_id FROM entity_revisions WHERE entity_id=?
                   ORDER BY created_at DESC,revision_id DESC LIMIT 1""", (term_id,),
            ).fetchone()
            connection.execute(
                "UPDATE entities SET status='accepted',updated_at=? WHERE entity_id=?",
                (timestamp, term_id),
            )
            connection.execute(
                """INSERT OR IGNORE INTO entity_revisions(
                       revision_id,entity_id,previous_revision_id,model,prompt_version,
                       reason,accepted,payload,created_at)
                   VALUES (?,?,?,?,?,'morphology v7 direct split promotion',1,?,?)""",
                (
                    _stable_id("entity-revision", migration_key, term_id, "promotion"),
                    term_id, str(previous[0]) if previous else None,
                    "deterministic-migration", parser_version,
                    _arbitrary_json({"term_id": term_id, **proof}), timestamp,
                ),
            )
            changes["draft_terms_promoted"] += 1
            changed_terms.add(term_id)

        for assertion_id, claim in internal["artifact_assertions"].items():
            cursor = connection.execute(
                """INSERT OR IGNORE INTO relation_assertions(
                       assertion_id,subject_entity_id,source_entity_id,
                       target_entity_id,relation,basis,confidence,status,
                       properties_json,revision,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,'accepted',?,1,?,?)""",
                (
                    assertion_id, claim["subject_entity_id"], claim["source_entity_id"],
                    claim["target_entity_id"], claim["relation"], claim["basis"],
                    claim["confidence"], _json(claim["properties"]), timestamp, timestamp,
                ),
            )
            changes["artifact_assertions_inserted"] += max(0, int(cursor.rowcount))
            changed_terms.add(str(claim["subject_entity_id"]))

        for owner_key, spec in internal["owner_plans"].items():
            assertion_id, owner_type, owner_id = owner_key
            owner_version = internal["owner_versions"].get(owner_key, owner_id)
            if not _support_matches(
                connection, assertion_id=assertion_id, owner_type=owner_type,
                owner_id=owner_id, owner_version=owner_version, spec=spec,
            ):
                upsert_relation_support(
                    connection, assertion_id=assertion_id,
                    owner_type=owner_type, owner_id=owner_id,
                    owner_version=owner_version, basis=spec.basis,
                    confidence=spec.confidence, properties=spec.properties,
                    evidence_ids=spec.evidence_ids, timestamp=timestamp,
                )
                changes["nonparser_supports_backfilled"] += 1
                touched_assertions.add(assertion_id)

        for owner_id, desired in sorted(internal["parser_scopes"].items()):
            specs = tuple(desired.values())
            active_ids = {
                str(row[0]) for row in connection.execute(
                    """SELECT assertion_id FROM relation_assertion_supports
                       WHERE owner_type='morphology-parser' AND owner_id=?
                         AND status='accepted'""", (owner_id,),
                )
            }
            exact = active_ids == set(desired)
            if exact:
                exact = all(_support_matches(
                    connection, assertion_id=spec.assertion_id,
                    owner_type="morphology-parser", owner_id=owner_id,
                    owner_version=parser_version, spec=spec,
                ) for spec in specs)
            scope = connection.execute(
                """SELECT owner_version, desired_count, desired_fingerprint
                   FROM relation_support_owner_scopes
                   WHERE owner_type='morphology-parser' AND owner_id=?""",
                (owner_id,),
            ).fetchone()
            exact = bool(exact and scope and str(scope[0]) == parser_version
                         and int(scope[1]) == len(specs)
                         and str(scope[2]) == _scope_fingerprint(specs))
            if not exact:
                result = replace_relation_support_scope(
                    connection, owner_type="morphology-parser", owner_id=owner_id,
                    owner_version=parser_version, supports=specs, timestamp=timestamp,
                )
                changes["parser_scopes_replaced"] += 1
                touched_assertions.update(active_ids | set(desired))
                if any(int(result.get(key, 0)) for key in (
                    "assertions_updated", "assertions_archived", "edges_changed",
                    "edges_archived", "term_morphemes_removed",
                )):
                    for assertion_id in active_ids | set(desired):
                        row = connection.execute(
                            "SELECT subject_entity_id FROM relation_assertions WHERE assertion_id=?",
                            (assertion_id,),
                        ).fetchone()
                        if row:
                            changed_terms.add(str(row[0]))

        for assertion_id in internal["invalid_endpoint_assertions"]:
            row = connection.execute(
                """SELECT basis,confidence,properties_json,subject_entity_id,
                          source_entity_id FROM relation_assertions
                   WHERE assertion_id=?""", (assertion_id,),
            ).fetchone()
            if row is None:
                continue
            evidence_ids = _assertion_evidence(connection, assertion_id)
            quarantine_properties = {
                **_relation_properties(row[2]),
                "migration_quarantine": {
                    "migration_key": migration_key,
                    "reason": "nonaccepted-entity-endpoint-without-direct-split-lineage",
                },
            }
            upsert_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="migration-quarantine", owner_id=assertion_id,
                owner_version=parser_version, basis=str(row[0]),
                confidence=float(row[1]), properties=quarantine_properties,
                evidence_ids=evidence_ids, timestamp=timestamp,
            )
            retire_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="migration-quarantine", owner_id=assertion_id,
                timestamp=timestamp,
            )
            retired_owners: set[tuple[str, str]] = set()
            for support in connection.execute(
                """SELECT owner_type,owner_id FROM relation_assertion_supports
                   WHERE assertion_id=? AND status='accepted'""", (assertion_id,),
            ).fetchall():
                owner_type, owner_id = str(support[0]), str(support[1])
                retire_relation_support(
                    connection, assertion_id=assertion_id,
                    owner_type=owner_type, owner_id=owner_id, timestamp=timestamp,
                )
                retired_owners.add((owner_type, owner_id))
            for owner_type, owner_id in sorted(retired_owners):
                _refresh_owner_scope_fingerprint(
                    connection, owner_type, owner_id, timestamp
                )
            changes["invalid_endpoint_assertions_quarantined"] += 1
            changed_terms.update((str(row[3]), str(row[4])))

        for assertion_id in internal["role_arbitration_retire"]:
            row = connection.execute(
                """SELECT basis,confidence,properties_json,subject_entity_id,
                          source_entity_id FROM relation_assertions
                   WHERE assertion_id=?""", (assertion_id,),
            ).fetchone()
            if row is None:
                continue
            evidence_ids = _assertion_evidence(connection, assertion_id)
            properties = {
                **_relation_properties(row[2]),
                "migration_quarantine": {
                    "migration_key": migration_key,
                    "reason": "exact-v7-book-role-outranks-model-only-role",
                },
            }
            upsert_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="migration-role-arbitration", owner_id=assertion_id,
                owner_version=parser_version, basis="model",
                confidence=float(row[1]), properties=properties,
                evidence_ids=evidence_ids, timestamp=timestamp,
            )
            retire_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="migration-role-arbitration", owner_id=assertion_id,
                timestamp=timestamp,
            )
            retired_owners: set[tuple[str, str]] = set()
            for support in connection.execute(
                """SELECT owner_type,owner_id FROM relation_assertion_supports
                   WHERE assertion_id=? AND status='accepted'""", (assertion_id,),
            ).fetchall():
                owner_type, owner_id = str(support[0]), str(support[1])
                retire_relation_support(
                    connection, assertion_id=assertion_id,
                    owner_type=owner_type, owner_id=owner_id, timestamp=timestamp,
                )
                retired_owners.add((owner_type, owner_id))
            for owner_type, owner_id in sorted(retired_owners):
                _refresh_owner_scope_fingerprint(
                    connection, owner_type, owner_id, timestamp
                )
            changes["model_role_conflicts_retired"] += 1
            changed_terms.update((str(row[3]), str(row[4])))

        for assertion_id, owners in internal["stale_parser"].items():
            row = connection.execute(
                """SELECT basis, confidence, properties_json, subject_entity_id
                   FROM relation_assertions WHERE assertion_id=?""",
                (assertion_id,),
            ).fetchone()
            for owner_id, evidence_ids in sorted(owners.items()):
                upsert_relation_support(
                    connection, assertion_id=assertion_id,
                    owner_type="morphology-parser", owner_id=owner_id,
                    owner_version="legacy-observation",
                    basis=str(row[0]), confidence=float(row[1]),
                    properties=_relation_properties(row[2]),
                    evidence_ids=tuple(sorted(evidence_ids)), timestamp=timestamp,
                )
                retire_relation_support(
                    connection, assertion_id=assertion_id,
                    owner_type="morphology-parser", owner_id=owner_id,
                    timestamp=timestamp,
                )
                changes["legacy_parser_supports_recorded"] += 1
            for support in connection.execute(
                """SELECT owner_type, owner_id FROM relation_assertion_supports
                   WHERE assertion_id=? AND owner_type='morphology-parser'
                     AND status='accepted'""", (assertion_id,),
            ).fetchall():
                retire_relation_support(
                    connection, assertion_id=assertion_id,
                    owner_type=str(support[0]), owner_id=str(support[1]),
                    timestamp=timestamp,
                )
            touched_assertions.add(assertion_id)
            changed_terms.add(str(row[3]))

        for assertion_id in internal["orphan_model"]:
            row = connection.execute(
                """SELECT basis,confidence,properties_json,subject_entity_id
                   FROM relation_assertions WHERE assertion_id=?""", (assertion_id,),
            ).fetchone()
            upsert_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="legacy-model-orphan", owner_id=assertion_id,
                owner_version="schema-v8-baseline", basis=str(row[0]),
                confidence=float(row[1]), properties=_relation_properties(row[2]),
                evidence_ids=(), timestamp=timestamp,
            )
            retire_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="legacy-model-orphan", owner_id=assertion_id,
                timestamp=timestamp,
            )
            changes["legacy_model_orphans_recorded"] += 1
            touched_assertions.add(assertion_id)
            changed_terms.add(str(row[3]))

        for assertion_id in internal["atomic_role_conflicts"]:
            row = connection.execute(
                """SELECT confidence,properties_json,subject_entity_id
                   FROM relation_assertions WHERE assertion_id=?""", (assertion_id,),
            ).fetchone()
            properties = _relation_properties(row[1])
            evidence_ids = _assertion_evidence(connection, assertion_id)
            properties = {
                **properties,
                "legacy_artifact_evidence_ids": list(evidence_ids),
                "role_authority": "current-parser-role-conflict",
            }
            upsert_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="legacy-atomic-role-conflict", owner_id=assertion_id,
                owner_version=parser_version, basis="model",
                confidence=float(row[0]), properties=properties,
                evidence_ids=(), timestamp=timestamp,
            )
            retire_relation_support(
                connection, assertion_id=assertion_id,
                owner_type="legacy-atomic-role-conflict", owner_id=assertion_id,
                timestamp=timestamp,
            )
            changes["atomic_role_conflicts_recorded"] += 1
            touched_assertions.add(assertion_id)
            changed_terms.add(str(row[2]))

        reduction = reduce_relation_assertions(
            connection, internal["morphology_assertion_ids"], timestamp=timestamp
        )
        changes.update({f"reducer_{key}": int(value) for key, value in reduction.items()})

        for slot, winner in internal["role_arbitration_winning_slots"].items():
            term_id, ordinal = str(slot[0]), int(slot[1])
            surface = str(winner.get("surface", ""))
            if not surface:
                morpheme = connection.execute(
                    "SELECT form FROM morphemes WHERE entity_id=?",
                    (winner["morpheme_id"],),
                ).fetchone()
                surface = str(morpheme[0]) if morpheme else ""
            current = connection.execute(
                """SELECT morpheme_id,surface,confidence,basis FROM term_morphemes
                   WHERE term_id=? AND ordinal=?""", (term_id, ordinal),
            ).fetchone()
            desired = (
                str(winner["morpheme_id"]), surface,
                float(winner["confidence"]), str(winner["basis"]),
            )
            if current is None:
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,?,?,?,?)""",
                    (term_id, desired[0], ordinal, desired[1], desired[2], desired[3]),
                )
                changes["typed_role_slots_projected"] += 1
            elif (
                str(current[0]), str(current[1]), float(current[2]), str(current[3])
            ) != desired:
                connection.execute(
                    """UPDATE term_morphemes SET morpheme_id=?,surface=?,confidence=?,basis=?
                       WHERE term_id=? AND ordinal=?""",
                    (*desired, term_id, ordinal),
                )
                changes["typed_role_slots_projected"] += 1
            changed_terms.add(term_id)

        for term_id in internal["invalid_draft_term_ids"]:
            if term_id in internal["draft_term_promotions"]:
                continue
            current = connection.execute(
                "SELECT status FROM entities WHERE entity_id=?", (term_id,),
            ).fetchone()
            if current is None or str(current[0]) != "draft":
                continue
            active_relation = connection.execute(
                """SELECT 1 FROM relation_assertions WHERE status='accepted'
                   AND ? IN (subject_entity_id,source_entity_id,target_entity_id)
                   LIMIT 1""", (term_id,),
            ).fetchone()
            active_edge = connection.execute(
                """SELECT 1 FROM entity_edges WHERE status='accepted'
                   AND ? IN (source_entity_id,target_entity_id) LIMIT 1""", (term_id,),
            ).fetchone()
            active_slot = connection.execute(
                """SELECT 1 FROM term_morphemes
                   WHERE term_id=? OR morpheme_id=? LIMIT 1""", (term_id, term_id),
            ).fetchone()
            active_job = connection.execute(
                """SELECT 1 FROM preparation_jobs WHERE subject_entity_id=?
                   AND status NOT IN ('complete','failed') LIMIT 1""", (term_id,),
            ).fetchone()
            if not any((active_relation, active_edge, active_slot, active_job)):
                connection.execute(
                    "UPDATE entities SET status='archived',updated_at=? WHERE entity_id=?",
                    (timestamp, term_id),
                )
                changes["draft_terms_archived"] += 1
                changed_terms.add(term_id)

        alias_manifest = _restrict_aliases_to_audited_groups(
            morpheme_alias_migration_manifest(connection), plan["alias_manifest"]
        )
        changes["alias_groups_deferred"] = int(
            alias_manifest.get("deferred_group_count", 0)
        )
        alias_changes, alias_terms = _apply_safe_aliases(
            connection, alias_manifest, timestamp
        )
        changes.update({f"alias_{key}": value for key, value in alias_changes.items()})
        changed_terms.update(alias_terms)

        (
            occurrence_changes,
            occurrence_terms,
            reconciled_occurrences,
            deferred_occurrence_terms,
        ) = _reconcile_complete_atomic_term_morphemes(
            connection,
            internal["complete_term_morpheme_projections"],
            alias_manifest,
            timestamp=timestamp,
        )
        changes.update(
            {
                f"artifact_term_projection_{key}": value
                for key, value in occurrence_changes.items()
            }
        )
        (
            component_authority_changes,
            component_authority_terms,
            reconciled_occurrences,
        ) = _bind_complete_atomic_component_slots(
            connection, reconciled_occurrences, timestamp=timestamp
        )
        changes.update(
            {
                f"artifact_component_{key}": value
                for key, value in component_authority_changes.items()
            }
        )
        changed_terms.update(component_authority_terms)
        changes["artifact_component_slots_neutralized"] = (
            _neutralize_shadowed_complete_component_slots(
                connection, reconciled_occurrences
            )
        )
        changed_terms.update(occurrence_terms)

        structural_endpoints = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                """SELECT a.source_entity_id,a.target_entity_id,a.relation
                   FROM relation_assertions a JOIN morphemes m
                     ON m.entity_id=a.target_entity_id
                   WHERE a.relation IN ('has-component','shares-component')
                   UNION
                   SELECT edge.source_entity_id,edge.target_entity_id,edge.relation
                   FROM entity_edges edge JOIN morphemes m
                     ON m.entity_id=edge.target_entity_id
                   WHERE edge.relation IN ('has-component','shares-component')"""
            )
        }
        (
            changes["final_edges_archived"],
            changes["final_edges_changed"],
        ) = _sync_endpoint_authority(
            connection, structural_endpoints, timestamp=timestamp
        )
        post_role_arbitration = _plan_exact_role_arbitration(
            connection,
            parser_version=parser_version,
            parser_scopes=internal["parser_scopes"],
            owner_plans={},
            prospective_accepted_terms=set(),
            excluded_assertion_ids=set(),
        )
        post_role_remaining = post_role_arbitration["retire_assertion_ids"]
        changes["post_role_arbitration_remaining"] = len(post_role_remaining)
        if post_role_remaining:
            raise RuntimeError(
                "post-finalization role arbitration invariant violation: "
                + _arbitrary_json(post_role_arbitration)
            )

        semantic_keys = (
            "reducer_assertions_updated", "reducer_assertions_archived",
            "reducer_edges_archived", "reducer_edges_changed",
            "reducer_term_morphemes_removed", "alias_groups_merged",
            "alias_entities_archived", "alias_assertions_merged",
            "alias_assertions_rewired", "alias_edges_archived",
            "alias_edges_rewired", "alias_term_morphemes_rewired",
            "artifact_assertions_inserted", "artifact_term_morphemes_restored",
            "draft_terms_promoted", "invalid_endpoint_assertions_quarantined",
            "draft_terms_archived",
            "model_role_conflicts_retired", "typed_role_slots_projected",
            "artifact_term_projection_updated", "artifact_term_projection_removed",
            "artifact_component_slots_bound",
            "artifact_component_assertions_updated",
            "artifact_component_occurrences_updated",
            "artifact_component_slots_neutralized",
            "final_edges_archived", "final_edges_changed",
            "role_review_subjects_deferred",
        )
        semantic_changed = any(int(changes.get(key, 0)) for key in semantic_keys)
        if semantic_changed:
            changes["projection_repairs_invalidated"] = _invalidate_morphology_projections(
                connection, changed_terms, timestamp
            )
            connection.execute(
                """INSERT INTO schema_meta(key, value) VALUES
                       ('relation_graph_revision', '1')
                   ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"""
            )
            changes["graph_revision_bumps"] = 1
        else:
            changes["projection_repairs_invalidated"] = 0
            changes["graph_revision_bumps"] = 0

        _assert_complete_atomic_term_morphemes(connection, reconciled_occurrences)
        _assert_morphology_finalization_invariants(connection)
        occurrence_fingerprints = _complete_atomic_occurrence_fingerprints(
            connection, reconciled_occurrences
        )

        stored_manifest = {
            **public,
            "destructive_ready": True,
            "deferred_card_cleanup": bool(semantic_changed),
            "artifact_term_projection_deferred_term_ids": deferred_occurrence_terms,
            "complete_atomic_occurrence_fingerprints": occurrence_fingerprints,
            "changes": changes,
        }
        connection.execute(
            """INSERT INTO relation_support_migrations(
                   migration_key, coverage_fingerprint, manifest_json,
                   destructive_ready, completed_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (
                migration_key, plan["coverage_fingerprint"],
                _arbitrary_json(stored_manifest), timestamp, timestamp,
            ),
        )
        connection.commit()
        return {
            **stored_manifest, "dry_run": False, "applied": True,
            "already_complete": False,
        }
    except Exception:
        connection.rollback()
        raise
