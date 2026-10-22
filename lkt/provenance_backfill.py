from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


TRANSLATION_RELATIONS = frozenset(
    {"has-translation", "renders-as", "translated-from"}
)
BACKFILL_VERSION = "translation-relation-provenance-v2"


@dataclass(frozen=True)
class EdgeEvidenceBackfillReport:
    database: str
    dry_run: bool
    scanned_edges: int
    translation_edges_scanned: int
    evidence_bearing_edges: int
    direct_evidence_edges: int
    inferred_evidence_edges: int
    context_evidence_edges: int
    direct_evidence_references: int
    inferred_evidence_references: int
    context_evidence_references: int
    assertions_to_create: int
    evidence_links_to_create: int
    assertions_created: int = 0
    evidence_links_created: int = 0
    invalid_evidence_references: int = 0
    model_edges_skipped: int = 0
    no_citation_edges_skipped: int = 0
    unresolved_translation_edges_skipped: int = 0
    invalid_subjects_skipped: int = 0
    archived_assertions_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _listed_ids(mapping: dict[str, Any], keys: Iterable[str]) -> set[str]:
    values: set[str] = set()
    for key in keys:
        raw = mapping.get(key)
        if isinstance(raw, list):
            values.update(
                str(value).strip() for value in raw if str(value).strip()
            )
    return values


def _direct_evidence_ids(properties: dict[str, Any]) -> set[str]:
    """Return legacy fields that explicitly declare relation citations."""

    keys = ("evidence_ids", "edge_evidence_ids", "citation_evidence_ids")
    values = _listed_ids(properties, keys)
    provenance = properties.get("provenance")
    if isinstance(provenance, dict):
        values.update(_listed_ids(provenance, keys))
    return values


def _dictionary_evidence_ids(properties: dict[str, Any]) -> set[str]:
    """Return exact dictionary IDs stored as metadata rather than citations."""

    values = _listed_ids(properties, ("dictionary_evidence_ids",))
    provenance = properties.get("provenance")
    if not isinstance(provenance, dict):
        return values
    values.update(_listed_ids(provenance, ("dictionary_evidence_ids",)))
    sources = provenance.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                values.update(
                    _listed_ids(source, ("dictionary_evidence_ids",))
                )
    return values


def _valid_evidence_ids(
    connection: sqlite3.Connection, requested: set[str]
) -> tuple[set[str], int]:
    if not requested:
        return set(), 0
    placeholders = ",".join("?" for _ in requested)
    valid = {
        str(row[0])
        for row in connection.execute(
            f"SELECT evidence_id FROM evidence_records "
            f"WHERE evidence_id IN ({placeholders})",
            tuple(sorted(requested)),
        )
    }
    return valid, len(requested - valid)


def _entity_evidence_ids(
    connection: sqlite3.Connection, entity_id: str
) -> set[str]:
    if not entity_id:
        return set()
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT evidence_id FROM entity_evidence WHERE entity_id = ?",
            (entity_id,),
        )
    }


def _translation_exists(connection: sqlite3.Connection, entity_id: str) -> bool:
    return bool(
        entity_id
        and connection.execute(
            "SELECT 1 FROM translations WHERE entity_id = ?", (entity_id,)
        ).fetchone()
    )


def _payload_translation_ids(
    payload: dict[str, Any], source_meaning_id: str = ""
) -> set[str]:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return set()
    sources: list[dict[str, Any]] = [provenance]
    nested = provenance.get("sources")
    if isinstance(nested, list):
        sources.extend(item for item in nested if isinstance(item, dict))
    result: set[str] = set()
    for source in sources:
        translation_id = str(source.get("translation_id", "")).strip()
        linked_meaning = str(source.get("source_meaning_id", "")).strip()
        if translation_id and (
            not source_meaning_id
            or not linked_meaning
            or linked_meaning == source_meaning_id
        ):
            result.add(translation_id)
    return result


def _translation_id_for_edge(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    properties: dict[str, Any],
) -> str:
    explicit = str(properties.get("translation_id", "")).strip()
    if _translation_exists(connection, explicit):
        return explicit
    relation = str(row["relation"])
    endpoint = (
        str(row["target_entity_id"])
        if relation == "has-translation"
        else str(row["source_entity_id"])
        if relation == "renders-as"
        else ""
    )
    if _translation_exists(connection, endpoint):
        return endpoint
    if relation != "translated-from":
        return ""

    payload_row = connection.execute(
        "SELECT payload FROM entities WHERE entity_id = ?",
        (row["source_entity_id"],),
    ).fetchone()
    if payload_row is not None:
        candidates = {
            candidate
            for candidate in _payload_translation_ids(
                _object(payload_row[0]), str(row["target_entity_id"])
            )
            if _translation_exists(connection, candidate)
        }
        if len(candidates) == 1:
            return next(iter(candidates))

    topology = connection.execute(
        """SELECT DISTINCT t.entity_id
           FROM translations AS t
           JOIN meanings AS target_meaning
             ON target_meaning.subject_entity_id = t.target_term_id
          WHERE target_meaning.entity_id = ?
            AND t.source_meaning_id = ?
          ORDER BY t.entity_id""",
        (row["source_entity_id"], row["target_entity_id"]),
    ).fetchall()
    return str(topology[0][0]) if len(topology) == 1 else ""


def _translation_row(
    connection: sqlite3.Connection, translation_id: str
) -> sqlite3.Row | None:
    if not translation_id:
        return None
    return connection.execute(
        """SELECT entity_id, source_term_id, source_meaning_id,
                  target_language, target_term_id
           FROM translations WHERE entity_id = ?""",
        (translation_id,),
    ).fetchone()


def _translation_owned_meaning_evidence(
    connection: sqlite3.Connection,
    translation: sqlite3.Row,
    edge: sqlite3.Row,
) -> set[str]:
    target_term_id = str(translation["target_term_id"] or "")
    source_meaning_id = str(translation["source_meaning_id"] or "")
    if not target_term_id:
        return set()
    if str(edge["relation"]) == "translated-from":
        meanings = connection.execute(
            """SELECT e.entity_id, e.payload
               FROM entities AS e JOIN meanings AS m USING(entity_id)
              WHERE e.entity_id = ? AND m.subject_entity_id = ?
                AND e.status = 'accepted'""",
            (edge["source_entity_id"], target_term_id),
        ).fetchall()
    else:
        meanings = connection.execute(
            """SELECT e.entity_id, e.payload
               FROM entities AS e JOIN meanings AS m USING(entity_id)
              WHERE m.subject_entity_id = ? AND m.language = ?
                AND e.status = 'accepted'
              ORDER BY m.sense_order, e.entity_id""",
            (target_term_id, translation["target_language"]),
        ).fetchall()

    evidence: set[str] = set()
    translation_id = str(translation["entity_id"])
    for meaning in meanings:
        payload = _object(meaning["payload"])
        if translation_id not in _payload_translation_ids(
            payload, source_meaning_id
        ):
            continue
        evidence.update(
            _entity_evidence_ids(connection, str(meaning["entity_id"]))
        )
        provenance = payload.get("provenance")
        if isinstance(provenance, dict):
            evidence.update(
                _dictionary_evidence_ids({"provenance": provenance})
            )
    return evidence


def _cluster_metadata_evidence(
    connection: sqlite3.Connection, translation: sqlite3.Row
) -> set[str]:
    """Collect exact IDs declared anywhere on one translation chain."""

    translation_id = str(translation["entity_id"])
    source_term_id = str(translation["source_term_id"])
    target_term_id = str(translation["target_term_id"] or "")
    source_meaning_id = str(translation["source_meaning_id"] or "")
    rows = connection.execute(
        """SELECT relation, basis, properties, source_entity_id
           FROM entity_edges
          WHERE status = 'accepted'
            AND relation IN ('has-translation','renders-as','translated-from')
            AND ((relation = 'has-translation'
                  AND source_entity_id = ? AND target_entity_id = ?)
              OR (relation = 'renders-as'
                  AND source_entity_id = ? AND target_entity_id = ?)
              OR (relation = 'translated-from' AND target_entity_id = ?))""",
        (
            source_term_id,
            translation_id,
            translation_id,
            target_term_id,
            source_meaning_id,
        ),
    ).fetchall()
    result: set[str] = set()
    for sibling in rows:
        if str(sibling["basis"]).casefold() == "model":
            continue
        properties = _object(sibling["properties"])
        if str(sibling["relation"]) == "translated-from":
            linked = str(properties.get("translation_id", "")).strip()
            if linked and linked != translation_id:
                continue
            if not linked:
                payload_row = connection.execute(
                    "SELECT payload FROM entities WHERE entity_id = ?",
                    (sibling["source_entity_id"],),
                ).fetchone()
                if (
                    payload_row is None
                    or translation_id
                    not in _payload_translation_ids(
                        _object(payload_row[0]), source_meaning_id
                    )
                ):
                    continue
        result.update(_direct_evidence_ids(properties))
        result.update(_dictionary_evidence_ids(properties))
    return result


def _subject_for_edge(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    properties: dict[str, Any],
    translation: sqlite3.Row | None,
) -> str:
    if translation is not None:
        return str(translation["source_term_id"])
    explicit = str(properties.get("subject_entity_id", "")).strip()
    if explicit:
        return explicit
    for endpoint in (row["source_entity_id"], row["target_entity_id"]):
        found = connection.execute(
            "SELECT source_term_id FROM translations WHERE entity_id = ?",
            (endpoint,),
        ).fetchone()
        if found is not None:
            return str(found[0])
    return str(row["source_entity_id"])


def _assertion_properties(
    edge_properties: dict[str, Any],
    edge_id: str,
    translation_id: str,
    citations: set[str],
    direct: set[str],
    inferred: set[str],
    context: set[str],
) -> dict[str, Any]:
    properties = dict(edge_properties)
    properties["citation_evidence_ids"] = sorted(citations)
    properties["context_evidence_ids"] = sorted(context)
    properties["provenance_backfill"] = {
        "version": BACKFILL_VERSION,
        "source_edge_id": edge_id,
        "translation_id": translation_id,
        "direct_citation_evidence_ids": sorted(direct),
        "inferred_citation_evidence_ids": sorted(inferred),
    }
    return properties


def backfill_edge_evidence(
    database: Path, *, dry_run: bool = True
) -> EdgeEvidenceBackfillReport:
    """Promote exact edge provenance into scoped relation assertions.

    Translation relations may inherit exact dictionary citations from their
    shared translation entity or translation-owned target meaning. Evidence
    attached only to the source sense is context and is never inserted into
    assertion_evidence. Model-owned relations remain uncited.

    Dry-run is read-only. Apply is one immediate transaction and replay
    idempotent. Existing assertions are never replaced or rewritten; missing
    citation links may be added only to accepted, non-model assertions.
    """

    database = Path(database).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    uri = f"file:{database.as_posix()}?mode=ro" if dry_run else str(database)
    connection = sqlite3.connect(uri, uri=dry_run, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if not dry_run:
            connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT edge_id, source_entity_id, target_entity_id, relation,
                      basis, confidence, properties, created_at, updated_at
               FROM entity_edges WHERE status = 'accepted'
               ORDER BY edge_id"""
        ).fetchall()

        counts = {
            "translation_edges": 0,
            "evidence_bearing": 0,
            "direct_edges": 0,
            "inferred_edges": 0,
            "context_edges": 0,
            "direct_refs": 0,
            "inferred_refs": 0,
            "context_refs": 0,
            "assertions_to_create": 0,
            "links_to_create": 0,
            "assertions_created": 0,
            "links_created": 0,
            "invalid": 0,
            "model_skipped": 0,
            "no_citation": 0,
            "unresolved": 0,
            "invalid_subjects": 0,
            "archived": 0,
        }
        changed = False
        for row in rows:
            relation = str(row["relation"])
            is_translation = relation in TRANSLATION_RELATIONS
            counts["translation_edges"] += int(is_translation)
            properties = _object(row["properties"])
            direct, invalid = _valid_evidence_ids(
                connection, _direct_evidence_ids(properties)
            )
            counts["invalid"] += invalid
            translation_id = ""
            translation = None
            inferred_candidates: set[str] = set()
            context_candidates: set[str] = set()
            basis = str(row["basis"]).casefold()

            if is_translation:
                translation_id = _translation_id_for_edge(
                    connection, row, properties
                )
                translation = _translation_row(connection, translation_id)
                if translation is None:
                    if basis != "model" and not direct:
                        counts["unresolved"] += 1
                else:
                    context_candidates.update(
                        _entity_evidence_ids(
                            connection,
                            str(translation["source_meaning_id"] or ""),
                        )
                    )
                    if basis != "model":
                        inferred_candidates.update(
                            _dictionary_evidence_ids(properties)
                        )
                        inferred_candidates.update(
                            _entity_evidence_ids(connection, translation_id)
                        )
                        inferred_candidates.update(
                            _translation_owned_meaning_evidence(
                                connection, translation, row
                            )
                        )
                        inferred_candidates.update(
                            _cluster_metadata_evidence(
                                connection, translation
                            )
                        )

            inferred, invalid = _valid_evidence_ids(
                connection, inferred_candidates
            )
            counts["invalid"] += invalid
            inferred.difference_update(direct)
            citations = direct | inferred
            context = context_candidates - citations

            if basis == "model":
                if is_translation or citations:
                    counts["model_skipped"] += 1
                continue
            if not citations:
                if is_translation or _direct_evidence_ids(properties):
                    counts["no_citation"] += 1
                continue

            counts["evidence_bearing"] += 1
            counts["direct_edges"] += int(bool(direct))
            counts["inferred_edges"] += int(bool(inferred))
            counts["context_edges"] += int(bool(context))
            counts["direct_refs"] += len(direct)
            counts["inferred_refs"] += len(inferred)
            counts["context_refs"] += len(context)

            subject = _subject_for_edge(
                connection, row, properties, translation
            )
            if connection.execute(
                "SELECT 1 FROM entities WHERE entity_id = ?", (subject,)
            ).fetchone() is None:
                counts["invalid_subjects"] += 1
                continue
            existing = connection.execute(
                """SELECT assertion_id, status, basis
                     FROM relation_assertions
                    WHERE subject_entity_id = ? AND source_entity_id = ?
                      AND target_entity_id = ? AND relation = ?""",
                (
                    subject,
                    row["source_entity_id"],
                    row["target_entity_id"],
                    relation,
                ),
            ).fetchone()
            if existing is not None and str(existing["status"]) != "accepted":
                counts["archived"] += 1
                continue
            if (
                existing is not None
                and str(existing["basis"]).casefold() == "model"
            ):
                counts["model_skipped"] += 1
                continue

            assertion_id = (
                str(existing["assertion_id"]) if existing is not None else ""
            )
            if not assertion_id:
                counts["assertions_to_create"] += 1
                assertion_id = "assertion-" + str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"lkt:{subject}:{row['source_entity_id']}:"
                        f"{relation}:{row['target_entity_id']}",
                    )
                )
                if not dry_run:
                    assertion_properties = _assertion_properties(
                        properties,
                        str(row["edge_id"]),
                        translation_id,
                        citations,
                        direct,
                        inferred,
                        context,
                    )
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id, subject_entity_id, source_entity_id,
                               target_entity_id, relation, basis, confidence,
                               status, properties_json, revision,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, 1, ?, ?)""",
                        (
                            assertion_id,
                            subject,
                            row["source_entity_id"],
                            row["target_entity_id"],
                            relation,
                            row["basis"],
                            row["confidence"],
                            json.dumps(
                                assertion_properties,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            row["created_at"],
                            row["updated_at"],
                        ),
                    )
                    counts["assertions_created"] += 1
                    changed = True

            present = {
                str(item[0])
                for item in connection.execute(
                    "SELECT evidence_id FROM assertion_evidence "
                    "WHERE assertion_id = ?",
                    (assertion_id,),
                )
            }
            missing = sorted(citations - present)
            counts["links_to_create"] += len(missing)
            if not dry_run and missing:
                connection.executemany(
                    "INSERT OR IGNORE INTO assertion_evidence"
                    "(assertion_id, evidence_id) VALUES (?, ?)",
                    [(assertion_id, evidence_id) for evidence_id in missing],
                )
                counts["links_created"] += len(missing)
                changed = True

        if not dry_run:
            if changed:
                connection.execute(
                    """UPDATE schema_meta
                          SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
                        WHERE key = 'relation_graph_revision'"""
                )
            connection.commit()
        return EdgeEvidenceBackfillReport(
            database=str(database),
            dry_run=dry_run,
            scanned_edges=len(rows),
            translation_edges_scanned=counts["translation_edges"],
            evidence_bearing_edges=counts["evidence_bearing"],
            direct_evidence_edges=counts["direct_edges"],
            inferred_evidence_edges=counts["inferred_edges"],
            context_evidence_edges=counts["context_edges"],
            direct_evidence_references=counts["direct_refs"],
            inferred_evidence_references=counts["inferred_refs"],
            context_evidence_references=counts["context_refs"],
            assertions_to_create=counts["assertions_to_create"],
            evidence_links_to_create=counts["links_to_create"],
            assertions_created=counts["assertions_created"],
            evidence_links_created=counts["links_created"],
            invalid_evidence_references=counts["invalid"],
            model_edges_skipped=counts["model_skipped"],
            no_citation_edges_skipped=counts["no_citation"],
            unresolved_translation_edges_skipped=counts["unresolved"],
            invalid_subjects_skipped=counts["invalid_subjects"],
            archived_assertions_skipped=counts["archived"],
        )
    except Exception:
        if not dry_run:
            connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply idempotent relation-provenance promotion with "
            "translation-aware citation inference."
        )
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    report = backfill_edge_evidence(
        arguments.database, dry_run=not arguments.apply
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
