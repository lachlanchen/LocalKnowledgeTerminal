"""Reconcile legacy accepted translations with their accepted artifacts.

The accepted artifact is the authority for this migration.  In particular,
only evidence recorded for the exact selected dictionary candidate can support
the translated term.  Model output remains accepted, but is explicitly
model-owned, uncited, and confidence-capped rather than being discarded.

The command is a dry run unless ``--apply`` is supplied::

    python -m lkt.reconcile_translations /path/to/knowledge.sqlite3
    python -m lkt.reconcile_translations /path/to/knowledge.sqlite3 --apply

Raw job artifacts and evidence records are never changed or deleted.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata
import uuid
from typing import Any


RECONCILIATION_VERSION = "accepted-translation-provenance-v1"
_ID_NAMESPACE = uuid.UUID("740541e0-e434-4ce1-826f-aed95037a552")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(value: Any, default: Any) -> Any:
    if not isinstance(value, str) or not value:
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return default
    return decoded


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.casefold().split())


def _exact_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split())


def _bounded_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 1:
        return None
    return number


def _confidence(payload: dict[str, Any], quality_score: Any, *, book: bool) -> float:
    values = [
        number
        for number in (
            _bounded_number(payload.get("confidence")),
            _bounded_number(quality_score),
        )
        if number is not None
    ]
    confidence = min(values) if values else (0.85 if book else 0.6)
    return round(confidence if book else min(confidence, 0.7), 4)


def _stable_id(prefix: str, key: str) -> str:
    return f"{prefix}-{uuid.uuid5(_ID_NAMESPACE, key)}"


def _merge_properties(raw: Any, provenance: dict[str, Any]) -> str:
    properties = _json_load(raw, {})
    if not isinstance(properties, dict):
        properties = {}
    properties.update(provenance)
    return _json_dump(properties)


def _artifact_rows(connection: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    rows = connection.execute(
        """
        SELECT a.artifact_id, a.job_id, a.language, a.payload,
               a.quality_score, a.created_at, j.model, j.prompt_version
          FROM job_artifacts AS a
          JOIN preparation_jobs AS j ON j.job_id = a.job_id
         WHERE a.stage = 'accepted-translation'
           AND a.validation_state = 'accepted'
         ORDER BY a.created_at, a.artifact_id
        """
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    diagnostics = Counter()
    for row in rows:
        payload = _json_load(row[3], None)
        if not isinstance(payload, dict):
            diagnostics["invalid_artifact_payloads"] += 1
            continue
        translation_id = str(payload.get("translation_id") or "").strip()
        if not translation_id:
            diagnostics["artifacts_without_translation_id"] += 1
            continue
        latest[translation_id] = {
            "artifact_id": row[0],
            "job_id": row[1],
            "language": row[2],
            "payload": payload,
            "quality_score": row[4],
            "created_at": row[5],
            "model": str(payload.get("model") or row[6] or ""),
            "prompt_version": str(payload.get("prompt_version") or row[7] or ""),
        }
    diagnostics["accepted_artifacts"] = len(rows)
    diagnostics["authoritative_artifacts"] = len(latest)
    return latest, dict(diagnostics)


def _translation_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT t.*, source.text AS source_text, target.text AS target_text,
               entity.quality_score AS entity_quality,
               entity.payload AS entity_payload
          FROM translations AS t
          JOIN entities AS entity ON entity.entity_id = t.entity_id
          JOIN terms AS source ON source.entity_id = t.source_term_id
          LEFT JOIN terms AS target ON target.entity_id = t.target_term_id
         WHERE entity.status = 'accepted'
         ORDER BY t.entity_id
        """
    ).fetchall()


def _edge_provenance(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "provenance_version": RECONCILIATION_VERSION,
        "provenance_kind": "dictionary" if plan["book"] else "local-model",
        "accepted_artifact_id": plan["artifact_id"],
        "dictionary_evidence_ids": plan["evidence_ids"],
        "model": plan["model"],
        "prompt_version": plan["prompt_version"],
        "selection": plan["selection"],
    }


def _build_plans(
    connection: sqlite3.Connection,
    artifacts: dict[str, dict[str, Any]],
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    evidence_ids = {
        row[0] for row in connection.execute("SELECT evidence_id FROM evidence_records")
    }
    plans: list[dict[str, Any]] = []
    meanings: dict[str, dict[str, Any]] = {}
    by_language = Counter()
    by_basis = Counter()
    skipped: dict[str, list[str]] = defaultdict(list)

    for row in _translation_rows(connection):
        translation_id = row["entity_id"]
        artifact = artifacts.get(translation_id)
        if artifact is None:
            skipped["without_authoritative_artifact"].append(translation_id)
            continue
        payload = artifact["payload"]
        selected = _exact_text(payload.get("term"))
        candidates = [
            _exact_text(candidate)
            for candidate in payload.get("dictionary_candidates", [])
            if isinstance(candidate, str) and _exact_text(candidate)
        ]
        if (
            not selected
            or selected != _exact_text(row["text"])
            or str(payload.get("language") or artifact["language"] or "")
            != row["target_language"]
            or (
                payload.get("target_term_id")
                and payload.get("target_term_id") != row["target_term_id"]
            )
        ):
            skipped["artifact_translation_mismatch"].append(translation_id)
            continue

        requested_evidence = [
            evidence_id
            for evidence_id in payload.get("dictionary_evidence_ids", [])
            if isinstance(evidence_id, str) and evidence_id
        ]
        supported_evidence = sorted(
            {evidence_id for evidence_id in requested_evidence if evidence_id in evidence_ids}
        )
        unknown_evidence = sorted(set(requested_evidence) - set(supported_evidence))
        if unknown_evidence:
            skipped["unknown_dictionary_evidence"].extend(unknown_evidence)
        exact_candidate = selected in candidates
        book = bool(exact_candidate and supported_evidence)
        if not book:
            supported_evidence = []
        confidence = _confidence(payload, artifact["quality_score"], book=book)
        selection = {
            "selected_term": selected,
            "exact_dictionary_candidate": book,
            "dictionary_candidate_count": len(candidates),
        }
        plan = {
            "translation_id": translation_id,
            "source_term_id": row["source_term_id"],
            "source_meaning_id": row["source_meaning_id"],
            "source_text": row["source_text"],
            "target_term_id": row["target_term_id"],
            "target_language": row["target_language"],
            "target_text": row["text"],
            "meaning": _exact_text(payload.get("meaning")),
            "artifact_id": artifact["artifact_id"],
            "model": artifact["model"],
            "prompt_version": artifact["prompt_version"],
            "book": book,
            "basis": "book" if book else "model",
            "confidence": confidence,
            "evidence_ids": supported_evidence,
            "selection": selection,
            "entity_quality": row["entity_quality"],
            "entity_payload": row["entity_payload"],
        }
        plans.append(plan)
        by_language[row["target_language"]] += 1
        by_basis["dictionary"] += int(book)
        by_basis["model"] += int(not book)

        if not row["target_term_id"] or not plan["meaning"]:
            skipped["without_target_meaning_material"].append(translation_id)
            continue
        canonical_key = (
            f"meaning:{row['target_term_id']}:{row['target_language']}:0:"
            f"{_normalise(plan['meaning'])}"
        )
        existing = connection.execute(
            """
            SELECT entity.entity_id, entity.status, entity.quality_score,
                   entity.payload, entity.canonical_key
              FROM meanings AS meaning
              JOIN entities AS entity ON entity.entity_id = meaning.entity_id
             WHERE meaning.term_id = ? AND meaning.language = ?
               AND meaning.definition = ?
             ORDER BY entity.created_at
             LIMIT 1
            """,
            (row["target_term_id"], row["target_language"], plan["meaning"]),
        ).fetchone()
        if existing is None:
            existing = connection.execute(
                """
                SELECT entity_id, status, quality_score, payload, canonical_key
                  FROM entities WHERE canonical_key = ?
                """,
                (canonical_key,),
            ).fetchone()
        if existing is not None and existing[1] != "accepted":
            skipped["nonaccepted_meaning_collision"].append(translation_id)
            continue
        meaning_id = (
            existing[0]
            if existing is not None
            else _stable_id("meaning", canonical_key)
        )
        plan["meaning_id"] = meaning_id
        group = meanings.setdefault(
            meaning_id,
            {
                "meaning_id": meaning_id,
                "exists": existing is not None,
                "canonical_key": canonical_key,
                "status": existing[1] if existing is not None else "accepted",
                "quality_score": existing[2] if existing is not None else None,
                "entity_payload": existing[3] if existing is not None else "{}",
                "term_id": row["target_term_id"],
                "language": row["target_language"],
                "definition": plan["meaning"],
                "sources": [],
                "evidence_ids": set(),
            },
        )
        group["sources"].append(plan)
        group["evidence_ids"].update(supported_evidence)

    report["translations_by_language"] = dict(sorted(by_language.items()))
    report["translations_by_ownership"] = dict(sorted(by_basis.items()))
    report["skipped"] = {key: sorted(set(values)) for key, values in skipped.items()}
    report["accepted_translations"] = len(_translation_rows(connection))
    report["reconcilable_translations"] = len(plans)
    report["target_meanings"] = len(meanings)
    return plans, meanings


class _Reconciler:
    def __init__(self, connection: sqlite3.Connection, *, apply: bool) -> None:
        self.connection = connection
        self.apply = apply
        self.timestamp = _now()
        self.counts: Counter[str] = Counter()
        self.changes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.revision_metadata: dict[str, dict[str, str]] = {}

    def _change(self, entity_id: str, change: dict[str, Any]) -> None:
        self.changes[entity_id].append(change)

    def update_entity(
        self,
        entity_id: str,
        *,
        quality_score: float,
        provenance: dict[str, Any],
        metadata: dict[str, str],
    ) -> None:
        row = self.connection.execute(
            "SELECT quality_score, payload FROM entities WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return
        payload = _json_load(row[1], {})
        if not isinstance(payload, dict):
            payload = {}
        old_provenance = payload.get("provenance")
        payload["provenance"] = provenance
        new_payload = _json_dump(payload)
        old_quality = _bounded_number(row[0])
        changed = old_quality != quality_score or row[1] != new_payload
        if not changed:
            return
        self.counts["entities_updated"] += 1
        self._change(
            entity_id,
            {
                "kind": "entity-provenance",
                "quality_before": old_quality,
                "quality_after": quality_score,
                "provenance_before": old_provenance,
                "provenance_after": provenance,
            },
        )
        self.revision_metadata[entity_id] = metadata
        if self.apply:
            self.connection.execute(
                """
                UPDATE entities SET quality_score = ?, payload = ?, updated_at = ?
                 WHERE entity_id = ?
                """,
                (quality_score, new_payload, self.timestamp, entity_id),
            )

    def create_meaning(self, group: dict[str, Any], provenance: dict[str, Any]) -> None:
        if group["exists"]:
            return
        confidence = group["desired_confidence"]
        entity_payload = {
            "language": group["language"],
            "definition": group["definition"],
            "part_of_speech": "",
            "register_label": "",
            "domain_label": "",
            "sense_order": 0,
            "provenance": provenance,
        }
        self.counts["meanings_created"] += 1
        self._change(
            group["meaning_id"],
            {
                "kind": "meaning-created",
                "term_id": group["term_id"],
                "language": group["language"],
                "definition": group["definition"],
                "quality_score": confidence,
            },
        )
        if self.apply:
            self.connection.execute(
                """
                INSERT INTO entities(
                    entity_id, entity_type, canonical_key, label, status,
                    quality_score, payload, created_at, updated_at
                ) VALUES (?, 'meaning', ?, ?, 'accepted', ?, ?, ?, ?)
                """,
                (
                    group["meaning_id"],
                    group["canonical_key"],
                    group["definition"],
                    confidence,
                    _json_dump(entity_payload),
                    self.timestamp,
                    self.timestamp,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO meanings(
                    entity_id, term_id, language, definition, part_of_speech,
                    register_label, domain_label, sense_order
                ) VALUES (?, ?, ?, ?, '', '', '', 0)
                """,
                (
                    group["meaning_id"],
                    group["term_id"],
                    group["language"],
                    group["definition"],
                ),
            )

    def reconcile_evidence(
        self,
        entity_id: str,
        desired_ids: set[str],
        *,
        claim: str,
        confidence: float,
    ) -> None:
        rows = self.connection.execute(
            """
            SELECT evidence_id, claim, confidence FROM entity_evidence
             WHERE entity_id = ?
            """,
            (entity_id,),
        ).fetchall()
        existing_ids = {row[0] for row in rows}
        removed = sorted(existing_ids - desired_ids)
        added = sorted(desired_ids - existing_ids)
        if removed:
            removed_rows = sum(1 for row in rows if row[0] in removed)
            self.counts["unsupported_evidence_links_removed"] += removed_rows
            self._change(
                entity_id,
                {"kind": "evidence-removed", "evidence_ids": removed},
            )
            if self.apply:
                placeholders = ",".join("?" for _ in removed)
                self.connection.execute(
                    f"DELETE FROM entity_evidence WHERE entity_id = ? "
                    f"AND evidence_id IN ({placeholders})",
                    (entity_id, *removed),
                )
        if added:
            self.counts["supported_evidence_links_added"] += len(added)
            self._change(
                entity_id,
                {"kind": "evidence-added", "evidence_ids": added},
            )
            if self.apply:
                self.connection.executemany(
                    """
                    INSERT INTO entity_evidence(
                        entity_id, evidence_id, claim, confidence
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [(entity_id, evidence_id, claim, confidence) for evidence_id in added],
                )

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        *,
        basis: str,
        confidence: float,
        provenance: dict[str, Any],
        owner_entity_id: str,
    ) -> None:
        row = self.connection.execute(
            """
            SELECT edge_id, basis, confidence, status, properties
              FROM entity_edges
             WHERE source_entity_id = ? AND target_entity_id = ?
               AND relation = ?
            """,
            (source_id, target_id, relation),
        ).fetchone()
        if row is None:
            self.counts["edges_created"] += 1
            self._change(
                owner_entity_id,
                {
                    "kind": "edge-created",
                    "relation": relation,
                    "source": source_id,
                    "target": target_id,
                    "basis": basis,
                    "confidence": confidence,
                },
            )
            if self.apply:
                edge_id = _stable_id("edge", f"{source_id}:{relation}:{target_id}")
                self.connection.execute(
                    """
                    INSERT INTO entity_edges(
                        edge_id, source_entity_id, target_entity_id, relation,
                        basis, confidence, status, properties, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)
                    """,
                    (
                        edge_id,
                        source_id,
                        target_id,
                        relation,
                        basis,
                        confidence,
                        _json_dump(provenance),
                        self.timestamp,
                        self.timestamp,
                    ),
                )
            return
        properties = _merge_properties(row[4], provenance)
        if (
            row[1] == basis
            and _bounded_number(row[2]) == confidence
            and row[3] == "accepted"
            and row[4] == properties
        ):
            return
        self.counts["edges_updated"] += 1
        self._change(
            owner_entity_id,
            {
                "kind": "edge-updated",
                "edge_id": row[0],
                "relation": relation,
                "basis_before": row[1],
                "basis_after": basis,
                "confidence_before": row[2],
                "confidence_after": confidence,
            },
        )
        if self.apply:
            self.connection.execute(
                """
                UPDATE entity_edges
                   SET basis = ?, confidence = ?, status = 'accepted',
                       properties = ?, updated_at = ?
                 WHERE edge_id = ?
                """,
                (basis, confidence, properties, self.timestamp, row[0]),
            )

    def create_revisions(self) -> None:
        for entity_id, changes in sorted(self.changes.items()):
            if not changes:
                continue
            metadata = self.revision_metadata.get(entity_id, {})
            previous = self.connection.execute(
                """
                SELECT revision_id FROM entity_revisions
                 WHERE entity_id = ?
                 ORDER BY created_at DESC, revision_id DESC LIMIT 1
                """,
                (entity_id,),
            ).fetchone()
            payload = {
                "reconciliation_version": RECONCILIATION_VERSION,
                "changes": changes,
            }
            fingerprint = hashlib.sha256(_json_dump(payload).encode("utf-8")).hexdigest()
            revision_id = _stable_id(
                "revision", f"{entity_id}:{RECONCILIATION_VERSION}:{fingerprint}"
            )
            exists = self.connection.execute(
                "SELECT 1 FROM entity_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if exists is not None:
                continue
            self.counts["revisions_created"] += 1
            if self.apply:
                self.connection.execute(
                    """
                    INSERT INTO entity_revisions(
                        revision_id, entity_id, previous_revision_id, model,
                        prompt_version, reason, accepted, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        revision_id,
                        entity_id,
                        previous[0] if previous else None,
                        metadata.get("model", ""),
                        metadata.get("prompt_version", ""),
                        "legacy accepted translation provenance reconciliation",
                        _json_dump(payload),
                        self.timestamp,
                    ),
                )


def reconcile(database: str | Path, *, apply: bool = False) -> dict[str, Any]:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    report: dict[str, Any] = {
        "version": RECONCILIATION_VERSION,
        "mode": "apply" if apply else "dry-run",
        "database": str(database),
    }
    try:
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        artifacts, diagnostics = _artifact_rows(connection)
        report["artifact_diagnostics"] = diagnostics
        plans, meanings = _build_plans(connection, artifacts, report)
        reconciler = _Reconciler(connection, apply=apply)

        for plan in plans:
            provenance = {
                "version": RECONCILIATION_VERSION,
                "ownership": "dictionary" if plan["book"] else "model",
                "basis": plan["basis"],
                "accepted_artifact_id": plan["artifact_id"],
                "dictionary_evidence_ids": plan["evidence_ids"],
                "model": plan["model"],
                "prompt_version": plan["prompt_version"],
                "selection": plan["selection"],
            }
            metadata = {
                "model": plan["model"],
                "prompt_version": plan["prompt_version"],
            }
            reconciler.revision_metadata[plan["translation_id"]] = metadata
            reconciler.update_entity(
                plan["translation_id"],
                quality_score=plan["confidence"],
                provenance=provenance,
                metadata=metadata,
            )
            reconciler.reconcile_evidence(
                plan["translation_id"],
                set(plan["evidence_ids"]),
                claim=(
                    f"dictionary support for {plan['source_text']} to "
                    f"{plan['target_text']}"
                ),
                confidence=plan["confidence"],
            )
            edge_provenance = _edge_provenance(plan)
            reconciler.upsert_edge(
                plan["source_term_id"],
                plan["translation_id"],
                "has-translation",
                basis=plan["basis"],
                confidence=plan["confidence"],
                provenance=edge_provenance,
                owner_entity_id=plan["translation_id"],
            )
            if plan["target_term_id"]:
                reconciler.upsert_edge(
                    plan["translation_id"],
                    plan["target_term_id"],
                    "renders-as",
                    basis=plan["basis"],
                    confidence=plan["confidence"],
                    provenance=edge_provenance,
                    owner_entity_id=plan["translation_id"],
                )

        for group in meanings.values():
            sources = group["sources"]
            dictionary_sources = [source for source in sources if source["book"]]
            effective_sources = dictionary_sources or sources
            group["desired_basis"] = "book" if dictionary_sources else "model"
            group["desired_confidence"] = max(
                source["confidence"] for source in effective_sources
            )
            source_provenance = [
                {
                    "translation_id": source["translation_id"],
                    "source_meaning_id": source["source_meaning_id"],
                    "accepted_artifact_id": source["artifact_id"],
                    "basis": source["basis"],
                    "dictionary_evidence_ids": source["evidence_ids"],
                    "model": source["model"],
                    "prompt_version": source["prompt_version"],
                    "selection": source["selection"],
                }
                for source in sorted(sources, key=lambda item: item["translation_id"])
            ]
            meaning_provenance = {
                "version": RECONCILIATION_VERSION,
                "ownership": "dictionary" if dictionary_sources else "model",
                "basis": group["desired_basis"],
                "dictionary_evidence_ids": sorted(group["evidence_ids"]),
                "sources": source_provenance,
            }
            primary = effective_sources[0]
            metadata = {
                "model": primary["model"],
                "prompt_version": primary["prompt_version"],
            }
            reconciler.revision_metadata[group["meaning_id"]] = metadata
            reconciler.create_meaning(group, meaning_provenance)
            if group["exists"]:
                reconciler.update_entity(
                    group["meaning_id"],
                    quality_score=group["desired_confidence"],
                    provenance=meaning_provenance,
                    metadata=metadata,
                )
            reconciler.reconcile_evidence(
                group["meaning_id"],
                set(group["evidence_ids"]),
                claim=f"dictionary support for target term {primary['target_text']}",
                confidence=group["desired_confidence"],
            )
            aggregate_edge_provenance = {
                "provenance_version": RECONCILIATION_VERSION,
                "provenance_kind": (
                    "dictionary" if dictionary_sources else "local-model"
                ),
                "accepted_artifact_ids": sorted(
                    source["artifact_id"] for source in sources
                ),
                "dictionary_evidence_ids": sorted(group["evidence_ids"]),
                "sources": source_provenance,
            }
            reconciler.upsert_edge(
                group["term_id"],
                group["meaning_id"],
                "has-meaning",
                basis=group["desired_basis"],
                confidence=group["desired_confidence"],
                provenance=aggregate_edge_provenance,
                owner_entity_id=group["meaning_id"],
            )
            for source in sources:
                if not source["source_meaning_id"]:
                    continue
                reconciler.upsert_edge(
                    group["meaning_id"],
                    source["source_meaning_id"],
                    "translated-from",
                    basis=source["basis"],
                    confidence=source["confidence"],
                    provenance=_edge_provenance(source),
                    owner_entity_id=group["meaning_id"],
                )

        reconciler.create_revisions()
        report["changes"] = dict(sorted(reconciler.counts.items()))
        report["changed_entities"] = len(reconciler.changes)
        if apply:
            connection.commit()
        else:
            connection.rollback()
        return report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile accepted translation provenance without model inference."
    )
    parser.add_argument("database", type=Path, help="Path to knowledge.sqlite3")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes; omission performs a read-only dry run.",
    )
    args = parser.parse_args(argv)
    print(json.dumps(reconcile(args.database, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
