from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote


MAX_QUERY_CHARS = 200
MAX_RESULTS = 5
MAX_DEPTH = 4
MAX_GRAPH_NODES = 24
MAX_GRAPH_EDGES = 32
MAX_EVIDENCE = 32
MAX_EVIDENCE_PER_CLAIM = 8
MAX_RELATION_SCAN = 1024
MAX_COLLECTIONS = 64
MAX_SOURCE_HASHES_PER_COLLECTION = 16
MAX_EXCERPT_CHARS = 600
MAX_SQL_VM_STEPS = 5_000_000
MAX_LOCATOR_DECODE_ROUNDS = 8

_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_ABSOLUTE_PATH_FRAGMENT = re.compile(
    r"(?<![A-Za-z0-9._-])(?:/|~/|[A-Za-z]:/)"
)
_ENCODED_PATH_FRAGMENT = re.compile(r"(?i)%(?:25|2f|5c|7e)")

_REQUIRED_TABLES = {
    "entities",
    "terms",
    "meanings",
    "morphemes",
    "pronunciations",
    "translations",
    "historical_forms",
    "content_items",
    "relation_assertions",
    "assertion_evidence",
    "evidence_records",
    "entity_evidence",
}


class PrivateKnowledgeError(ValueError):
    """A safe, user-facing failure from the read-only knowledge boundary."""


def _normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _bounded_text(value: Any, limit: int = MAX_EXCERPT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _opaque_id(kind: str, *values: Any) -> str:
    material = "\x00".join(str(value) for value in values)
    return f"{kind}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _safe_source_hash(value: Any) -> str:
    digest = str(value or "").strip().lower()
    return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else ""


def _safe_locator(value: Any) -> str:
    """Keep source-relative locators while withholding local filesystem paths."""

    locator = _bounded_text(value, 300)
    decoded = locator
    for _ in range(MAX_LOCATOR_DECODE_ROUNDS):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    normalized = decoded.replace("\\", "/")
    path_parts = normalized.split("?", 1)[0].split("#", 1)[0].split("/")
    if (
        normalized.startswith(("/", "~/"))
        or "://" in normalized
        or re.search(r"(?i)(?:^|[=,;\s])(?:file|smb|ftp|ssh):", normalized)
        or _ABSOLUTE_PATH_FRAGMENT.search(normalized)
        or _ENCODED_PATH_FRAGMENT.search(normalized)
        or _WINDOWS_ABSOLUTE_PATH.match(decoded)
        or any(part in {".", "..", "~"} for part in path_parts)
        or any(ord(character) < 32 for character in decoded)
        or not all(
            character.isalnum() or character in " -_./#:=?&%+@[]()"
            for character in decoded
        )
    ):
        return "[local path withheld]"
    return locator


class KnowledgeReader:
    """Read accepted local knowledge without initializing or changing its database."""

    def __init__(self, database: Path):
        self.database = Path(database).expanduser().resolve()
        self._validate_database()

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise PrivateKnowledgeError("the local knowledge database is unavailable")
        uri = "file:" + quote(str(self.database), safe="/") + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2)
        except sqlite3.Error as exc:
            raise PrivateKnowledgeError(
                "the local knowledge database is unavailable"
            ) from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        progress = 0

        def stop_expensive_query() -> int:
            nonlocal progress
            progress += 1000
            return int(progress > MAX_SQL_VM_STEPS)

        connection.set_progress_handler(stop_expensive_query, 1000)
        return connection

    def _validate_database(self) -> None:
        try:
            with closing(self._connect()) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
        except sqlite3.Error as exc:
            raise PrivateKnowledgeError(
                "the local knowledge database could not be read"
            ) from exc
        if not _REQUIRED_TABLES.issubset(tables):
            raise PrivateKnowledgeError("the local knowledge database is incompatible")

    @staticmethod
    def _validated_query(query: str) -> tuple[str, str]:
        if not isinstance(query, str):
            raise PrivateKnowledgeError("query must be text")
        query = query.strip()
        if not query:
            raise PrivateKnowledgeError("query must not be empty")
        if len(query) > MAX_QUERY_CHARS:
            raise PrivateKnowledgeError(
                f"query must be at most {MAX_QUERY_CHARS} characters"
            )
        return query, _normalise(query)

    @staticmethod
    def _validated_language(language: str) -> str:
        if not isinstance(language, str):
            raise PrivateKnowledgeError("response_language must be a language code")
        value = language.strip().lower()
        if not (2 <= len(value) <= 16) or not all(
            character.isascii() and (character.isalnum() or character == "-")
            for character in value
        ):
            raise PrivateKnowledgeError("response_language must be a language code")
        return value

    @staticmethod
    def _validated_range(name: str, value: int, minimum: int, maximum: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise PrivateKnowledgeError(
                f"{name} must be an integer from {minimum} to {maximum}"
            )
        return value

    @staticmethod
    def _entity(connection: sqlite3.Connection, entity_id: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT entity.entity_id, entity.entity_type, entity.label,
                      entity.quality_score,
                      term.language AS term_language, term.kind AS term_kind,
                      meaning.language AS meaning_language,
                      meaning.part_of_speech AS meaning_kind,
                      morpheme.language AS morpheme_language,
                      morpheme.kind AS morpheme_kind,
                      pronunciation.language AS pronunciation_language,
                      pronunciation.system AS pronunciation_kind,
                      translation.target_language AS translation_language,
                      historical.language AS historical_language,
                      historical.period_label AS historical_kind,
                      content.language AS content_language,
                      content.kind AS content_kind
               FROM entities AS entity
               LEFT JOIN terms AS term ON term.entity_id = entity.entity_id
               LEFT JOIN meanings AS meaning ON meaning.entity_id = entity.entity_id
               LEFT JOIN morphemes AS morpheme ON morpheme.entity_id = entity.entity_id
               LEFT JOIN pronunciations AS pronunciation
                 ON pronunciation.entity_id = entity.entity_id
               LEFT JOIN translations AS translation
                 ON translation.entity_id = entity.entity_id
               LEFT JOIN historical_forms AS historical
                 ON historical.entity_id = entity.entity_id
               LEFT JOIN content_items AS content
                 ON content.entity_id = entity.entity_id
               WHERE entity.entity_id = ? AND entity.status = 'accepted'""",
            (entity_id,),
        ).fetchone()
        if row is None:
            raise PrivateKnowledgeError("accepted knowledge was not found")
        language = next(
            (
                str(row[key])
                for key in (
                    "term_language",
                    "meaning_language",
                    "morpheme_language",
                    "pronunciation_language",
                    "translation_language",
                    "historical_language",
                    "content_language",
                )
                if row[key]
            ),
            "",
        )
        subtype = next(
            (
                str(row[key])
                for key in (
                    "term_kind",
                    "meaning_kind",
                    "morpheme_kind",
                    "pronunciation_kind",
                    "historical_kind",
                    "content_kind",
                )
                if row[key]
            ),
            "",
        )
        result: dict[str, Any] = {
            "id": _bounded_text(row["entity_id"], 200),
            "type": _bounded_text(row["entity_type"], 80),
            "label": _bounded_text(row["label"], 400),
        }
        if language:
            result["language"] = _bounded_text(language, 32)
        if subtype:
            result["subtype"] = _bounded_text(subtype, 80)
        if row["quality_score"] is not None:
            result["quality"] = float(row["quality_score"])
        return result

    @staticmethod
    def _canonical_subject(connection: sqlite3.Connection, entity_id: str) -> str:
        translation = connection.execute(
            """SELECT item.source_term_id
               FROM translations AS item
               JOIN entities AS translation_entity
                 ON translation_entity.entity_id = item.entity_id
                AND translation_entity.status = 'accepted'
               JOIN entities AS source_entity
                 ON source_entity.entity_id = item.source_term_id
                AND source_entity.status = 'accepted'
               WHERE item.entity_id = ? OR item.target_term_id = ?
               ORDER BY item.entity_id LIMIT 1""",
            (entity_id, entity_id),
        ).fetchone()
        if translation is not None:
            return str(translation["source_term_id"])
        meaning = connection.execute(
            """SELECT item.term_id FROM meanings AS item
               JOIN entities AS source_entity
                 ON source_entity.entity_id = item.term_id
                AND source_entity.status = 'accepted'
               WHERE item.entity_id = ? LIMIT 1""",
            (entity_id,),
        ).fetchone()
        if meaning is not None:
            return str(meaning["term_id"])
        relation = connection.execute(
            """SELECT assertion.subject_entity_id
               FROM relation_assertions AS assertion
               JOIN entities AS subject
                 ON subject.entity_id = assertion.subject_entity_id
                AND subject.status = 'accepted'
               JOIN entities AS source
                 ON source.entity_id = assertion.source_entity_id
                AND source.status = 'accepted'
               JOIN entities AS target
                 ON target.entity_id = assertion.target_entity_id
                AND target.status = 'accepted'
               WHERE assertion.status = 'accepted'
                 AND assertion.basis <> 'model'
                 AND EXISTS (
                     SELECT 1 FROM assertion_evidence AS grounded
                     JOIN evidence_records AS evidence
                       ON evidence.evidence_id = grounded.evidence_id
                     WHERE grounded.assertion_id = assertion.assertion_id
                 )
                 AND (assertion.subject_entity_id = ?
                      OR assertion.source_entity_id = ?
                      OR assertion.target_entity_id = ?)
               ORDER BY CASE WHEN assertion.subject_entity_id = ? THEN 0 ELSE 1 END,
                        assertion.subject_entity_id
               LIMIT 1""",
            (entity_id, entity_id, entity_id, entity_id),
        ).fetchone()
        return str(relation["subject_entity_id"]) if relation else entity_id

    def _search_candidates(
        self,
        connection: sqlite3.Connection,
        normalized_query: str,
        response_language: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        escaped = _like(normalized_query)
        prefix = escaped + "%"
        contains = "%" + escaped + "%"
        specifications = (
            ("terms", "normalized", "language", "entity_id", "term"),
            ("content_items", "normalized", "language", "entity_id", "content"),
            (
                "translations",
                "normalized",
                "target_language",
                "entity_id",
                "translation",
            ),
            ("morphemes", "normalized", "language", "entity_id", "morpheme"),
            (
                "historical_forms",
                "normalized",
                "language",
                "entity_id",
                "historical-form",
            ),
        )
        candidates: list[dict[str, Any]] = []
        for table, text_column, language_column, id_column, match_kind in specifications:
            rows = connection.execute(
                f"""SELECT entity.entity_id, entity.entity_type, entity.label,
                           item.{language_column} AS language,
                           CASE WHEN item.{text_column} = ? THEN 0
                                WHEN item.{text_column} LIKE ? ESCAPE '\\' THEN 1
                                ELSE 2 END AS match_rank
                    FROM {table} AS item
                    JOIN entities AS entity ON entity.entity_id = item.{id_column}
                    WHERE entity.status = 'accepted'
                      AND item.{text_column} LIKE ? ESCAPE '\\'
                    ORDER BY match_rank, entity.entity_id LIMIT ?""",
                (normalized_query, prefix, contains, limit * 4),
            ).fetchall()
            for row in rows:
                candidates.append(
                    {
                        "entity_id": str(row["entity_id"]),
                        "entity_type": str(row["entity_type"]),
                        "label": str(row["label"]),
                        "language": str(row["language"] or ""),
                        "match_kind": match_kind,
                        "match_rank": int(row["match_rank"]),
                    }
                )

        english_contains = "%" + _like(normalized_query) + "%"
        for table, parent_column, text_column, match_kind in (
            ("meanings", "term_id", "definition", "meaning"),
        ):
            rows = connection.execute(
                f"""SELECT entity.entity_id, entity.entity_type, entity.label,
                           item.language,
                           CASE WHEN lower(item.{text_column}) = ? THEN 0 ELSE 2 END
                               AS match_rank
                    FROM {table} AS item
                    JOIN entities AS entity ON entity.entity_id = item.entity_id
                    JOIN entities AS parent ON parent.entity_id = item.{parent_column}
                    WHERE entity.status = 'accepted' AND parent.status = 'accepted'
                      AND lower(item.{text_column}) LIKE ? ESCAPE '\\'
                    ORDER BY match_rank, entity.entity_id LIMIT ?""",
                (normalized_query, english_contains, limit * 2),
            ).fetchall()
            for row in rows:
                candidates.append(
                    {
                        "entity_id": str(row["entity_id"]),
                        "entity_type": str(row["entity_type"]),
                        "label": str(row["label"]),
                        "language": str(row["language"] or ""),
                        "match_kind": match_kind,
                        "match_rank": int(row["match_rank"]),
                    }
                )

        type_order = {
            "term": 0,
            "content-item": 1,
            "meaning": 2,
            "translation": 3,
            "morpheme": 4,
            "historical-form": 5,
        }
        candidates.sort(
            key=lambda item: (
                item["match_rank"],
                0 if item["language"] == response_language else 1,
                type_order.get(item["entity_type"], 9),
                _normalise(item["label"]),
                item["entity_id"],
            )
        )
        results: list[dict[str, Any]] = []
        seen_subjects: set[str] = set()
        for candidate in candidates:
            subject_id = self._canonical_subject(connection, candidate["entity_id"])
            if subject_id in seen_subjects:
                continue
            seen_subjects.add(subject_id)
            results.append({**candidate, "subject_id": subject_id})
            if len(results) >= limit:
                break
        return results

    def _term_facts(
        self,
        connection: sqlite3.Connection,
        subject_id: str,
        response_language: str,
    ) -> dict[str, list[dict[str, Any]]]:
        facts: dict[str, list[dict[str, Any]]] = {
            "meanings": [],
            "translations": [],
            "pronunciations": [],
        }
        facts["meanings"] = [
            {
                "id": str(row["entity_id"]),
                "language": _bounded_text(row["language"], 32),
                "text": _bounded_text(row["definition"], 500),
                "part_of_speech": _bounded_text(row["part_of_speech"], 80),
            }
            for row in connection.execute(
                """SELECT item.entity_id, item.language, item.definition,
                          item.part_of_speech
                   FROM meanings AS item
                   JOIN entities AS entity ON entity.entity_id = item.entity_id
                   WHERE item.term_id = ? AND entity.status = 'accepted'
                   ORDER BY CASE WHEN item.language = ? THEN 0 ELSE 1 END,
                            item.sense_order, item.entity_id LIMIT 8""",
                (subject_id, response_language),
            )
        ]
        facts["translations"] = [
            {
                "id": str(row["entity_id"]),
                "language": _bounded_text(row["target_language"], 32),
                "text": _bounded_text(row["text"], 240),
                "transliteration": _bounded_text(row["transliteration"], 240),
            }
            for row in connection.execute(
                """SELECT item.entity_id, item.target_language, item.text,
                          item.transliteration
                   FROM translations AS item
                   JOIN entities AS entity ON entity.entity_id = item.entity_id
                   WHERE item.source_term_id = ? AND entity.status = 'accepted'
                   ORDER BY CASE WHEN item.target_language = ? THEN 0 ELSE 1 END,
                            item.target_language, item.entity_id LIMIT 8""",
                (subject_id, response_language),
            )
        ]
        facts["pronunciations"] = [
            {
                "id": str(row["entity_id"]),
                "language": _bounded_text(row["language"], 32),
                "system": _bounded_text(row["system"], 80),
                "reading": _bounded_text(row["reading"], 240),
            }
            for row in connection.execute(
                """SELECT item.entity_id, item.language, item.system, item.reading
                   FROM pronunciations AS item
                   JOIN entities AS entity ON entity.entity_id = item.entity_id
                   WHERE item.term_id = ? AND entity.status = 'accepted'
                   ORDER BY CASE WHEN item.language = ? THEN 0 ELSE 1 END,
                            item.language, item.system, item.entity_id LIMIT 8""",
                (subject_id, response_language),
            )
        ]
        return {name: values for name, values in facts.items() if values}

    @staticmethod
    def _evidence_records(
        connection: sqlite3.Connection, evidence_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        ordered = list(dict.fromkeys(str(value) for value in evidence_ids if value))[
            :MAX_EVIDENCE
        ]
        if not ordered:
            return []
        placeholders = ",".join("?" for _ in ordered)
        rows = connection.execute(
            f"""SELECT evidence_id, corpus_id, source_entry_id, source_hash,
                       locator, excerpt
                FROM evidence_records
                WHERE evidence_id IN ({placeholders})""",
            ordered,
        ).fetchall()
        by_id = {str(row["evidence_id"]): row for row in rows}
        return [
            {
                "id": evidence_id,
                "collection_id": _opaque_id(
                    "collection", by_id[evidence_id]["corpus_id"]
                ),
                "source_id": _opaque_id(
                    "source",
                    by_id[evidence_id]["corpus_id"],
                    by_id[evidence_id]["source_entry_id"],
                ),
                "source_hash": _safe_source_hash(
                    by_id[evidence_id]["source_hash"]
                ),
                "locator": _safe_locator(by_id[evidence_id]["locator"]),
                "excerpt": _bounded_text(by_id[evidence_id]["excerpt"]),
            }
            for evidence_id in ordered
            if evidence_id in by_id
        ]

    def _subgraph(
        self,
        connection: sqlite3.Connection,
        subject_id: str,
        max_depth: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self._entity(connection, subject_id)
        rows = connection.execute(
            """SELECT assertion.assertion_id, assertion.source_entity_id,
                      assertion.target_entity_id, assertion.relation,
                      assertion.basis, assertion.confidence, assertion.revision
               FROM relation_assertions AS assertion
               JOIN entities AS subject
                 ON subject.entity_id = assertion.subject_entity_id
                AND subject.status = 'accepted'
               JOIN entities AS source
                 ON source.entity_id = assertion.source_entity_id
                AND source.status = 'accepted'
               JOIN entities AS target
                 ON target.entity_id = assertion.target_entity_id
                AND target.status = 'accepted'
               WHERE assertion.subject_entity_id = ?
                 AND assertion.status = 'accepted'
                 AND assertion.basis <> 'model'
                 AND EXISTS (
                     SELECT 1 FROM assertion_evidence AS grounded
                     JOIN evidence_records AS evidence
                       ON evidence.evidence_id = grounded.evidence_id
                     WHERE grounded.assertion_id = assertion.assertion_id
               )
               ORDER BY assertion.relation, assertion.source_entity_id,
                        assertion.target_entity_id, assertion.assertion_id
               LIMIT ?""",
            (subject_id, MAX_RELATION_SCAN + 1),
        ).fetchall()
        source_truncated = len(rows) > MAX_RELATION_SCAN
        candidates = [dict(row) for row in rows[:MAX_RELATION_SCAN]]
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            adjacency.setdefault(str(candidate["source_entity_id"]), []).append(candidate)
            adjacency.setdefault(str(candidate["target_entity_id"]), []).append(candidate)
        for values in adjacency.values():
            values.sort(key=lambda value: str(value["assertion_id"]))

        depths = {subject_id: 0}
        queue = [subject_id]
        selected: dict[str, dict[str, Any]] = {}
        cursor = 0
        while cursor < len(queue) and len(selected) < MAX_GRAPH_EDGES:
            current = queue[cursor]
            cursor += 1
            if depths[current] >= max_depth:
                continue
            for candidate in adjacency.get(current, ()):
                assertion_id = str(candidate["assertion_id"])
                if assertion_id in selected:
                    continue
                source_id = str(candidate["source_entity_id"])
                target_id = str(candidate["target_entity_id"])
                other = target_id if source_id == current else source_id
                if other not in depths:
                    if len(depths) >= MAX_GRAPH_NODES:
                        continue
                    depths[other] = depths[current] + 1
                    queue.append(other)
                selected[assertion_id] = candidate
                if len(selected) >= MAX_GRAPH_EDGES:
                    break

        selected_ids = sorted(selected)
        evidence_by_claim: dict[str, list[str]] = {
            assertion_id: [] for assertion_id in selected_ids
        }
        for assertion_id in selected_ids:
            evidence_by_claim[assertion_id] = [
                str(row["evidence_id"])
                for row in connection.execute(
                    """SELECT evidence_id FROM assertion_evidence
                       WHERE assertion_id = ? ORDER BY evidence_id LIMIT ?""",
                    (assertion_id, MAX_EVIDENCE_PER_CLAIM + 1),
                )
            ]

        nodes = [self._entity(connection, entity_id) for entity_id in sorted(depths)]
        claims = [
            {
                "id": assertion_id,
                "source_id": str(selected[assertion_id]["source_entity_id"]),
                "target_id": str(selected[assertion_id]["target_entity_id"]),
                "relation": _bounded_text(selected[assertion_id]["relation"], 120),
                "basis": _bounded_text(selected[assertion_id]["basis"], 32),
                "confidence": float(selected[assertion_id]["confidence"]),
                "revision": int(selected[assertion_id]["revision"]),
                "evidence_truncated": (
                    len(evidence_by_claim[assertion_id]) > MAX_EVIDENCE_PER_CLAIM
                ),
                "evidence_ids": (
                    []
                    if str(selected[assertion_id]["basis"]) == "model"
                    else evidence_by_claim[assertion_id][:MAX_EVIDENCE_PER_CLAIM]
                ),
            }
            for assertion_id in selected_ids
        ]
        evidence_ids = [
            evidence_id for claim in claims for evidence_id in claim["evidence_ids"]
        ]
        entity_evidence_rows = connection.execute(
            """SELECT link.evidence_id FROM entity_evidence AS link
               JOIN entities AS entity ON entity.entity_id = link.entity_id
               WHERE link.entity_id = ? AND entity.status = 'accepted'
               ORDER BY link.evidence_id LIMIT ?""",
            (subject_id, MAX_EVIDENCE),
        ).fetchall()
        evidence_ids.extend(str(row["evidence_id"]) for row in entity_evidence_rows)
        evidence = self._evidence_records(connection, evidence_ids)
        graph = {
            "subject_id": subject_id,
            "max_depth": max_depth,
            "nodes": nodes,
            "claims": claims,
            "truncated": (
                source_truncated
                or len(selected) < len(candidates)
                or any(claim["evidence_truncated"] for claim in claims)
            ),
        }
        graph["projection_hash"] = _fingerprint(graph)
        return graph, evidence

    def query_private_knowledge(
        self,
        query: str,
        response_language: str = "en",
        limit: int = 3,
        max_depth: int = 2,
    ) -> dict[str, Any]:
        query, normalized_query = self._validated_query(query)
        response_language = self._validated_language(response_language)
        limit = self._validated_range("limit", limit, 1, MAX_RESULTS)
        max_depth = self._validated_range("max_depth", max_depth, 0, MAX_DEPTH)
        try:
            with closing(self._connect()) as connection:
                candidates = self._search_candidates(
                    connection, normalized_query, response_language, limit
                )
                matches: list[dict[str, Any]] = []
                for candidate in candidates:
                    subject_id = str(candidate["subject_id"])
                    graph, evidence = self._subgraph(
                        connection, subject_id, max_depth
                    )
                    match = self._entity(connection, candidate["entity_id"])
                    matches.append(
                        {
                            "subject": self._entity(connection, subject_id),
                            "matched": {
                                **match,
                                "kind": candidate["match_kind"],
                            },
                            "facts": self._term_facts(
                                connection, subject_id, response_language
                            ),
                            "graph": graph,
                            "evidence": evidence,
                        }
                    )
        except sqlite3.Error as exc:
            raise PrivateKnowledgeError("local knowledge could not be queried") from exc
        response = {
            "ok": True,
            "query": query,
            "response_language": response_language,
            "match_count": len(matches),
            "matches": matches,
        }
        response["result_hash"] = _fingerprint(response)
        return response

    def trace_private_claim(self, identifier: str) -> dict[str, Any]:
        if not isinstance(identifier, str):
            raise PrivateKnowledgeError("identifier must be text")
        identifier = identifier.strip()
        if not identifier or len(identifier) > 200:
            raise PrivateKnowledgeError("identifier is invalid")
        try:
            with closing(self._connect()) as connection:
                assertion = connection.execute(
                    """SELECT assertion.assertion_id, assertion.source_entity_id,
                              assertion.target_entity_id, assertion.relation,
                              assertion.basis, assertion.confidence,
                              assertion.revision
                       FROM relation_assertions AS assertion
                       JOIN entities AS subject
                         ON subject.entity_id = assertion.subject_entity_id
                        AND subject.status = 'accepted'
                       JOIN entities AS source
                         ON source.entity_id = assertion.source_entity_id
                        AND source.status = 'accepted'
                       JOIN entities AS target
                         ON target.entity_id = assertion.target_entity_id
                        AND target.status = 'accepted'
                       WHERE assertion.assertion_id = ?
                         AND assertion.status = 'accepted'
                         AND assertion.basis <> 'model'
                         AND EXISTS (
                             SELECT 1 FROM assertion_evidence AS grounded
                             JOIN evidence_records AS evidence
                               ON evidence.evidence_id = grounded.evidence_id
                             WHERE grounded.assertion_id = assertion.assertion_id
                         )""",
                    (identifier,),
                ).fetchone()
                if assertion is not None:
                    evidence_ids = [
                        str(row["evidence_id"])
                        for row in connection.execute(
                            """SELECT evidence_id FROM assertion_evidence
                               WHERE assertion_id = ? ORDER BY evidence_id LIMIT ?""",
                            (identifier, MAX_EVIDENCE + 1),
                        )
                    ]
                    evidence_truncated = len(evidence_ids) > MAX_EVIDENCE
                    evidence_ids = evidence_ids[:MAX_EVIDENCE]
                    result = {
                        "ok": True,
                        "kind": "claim",
                        "claim": {
                            "id": str(assertion["assertion_id"]),
                            "source": self._entity(
                                connection, str(assertion["source_entity_id"])
                            ),
                            "target": self._entity(
                                connection, str(assertion["target_entity_id"])
                            ),
                            "relation": _bounded_text(assertion["relation"], 120),
                            "basis": _bounded_text(assertion["basis"], 32),
                            "confidence": float(assertion["confidence"]),
                            "revision": int(assertion["revision"]),
                            "evidence_truncated": evidence_truncated,
                            "evidence_ids": (
                                [] if assertion["basis"] == "model" else evidence_ids
                            ),
                        },
                        "evidence": (
                            []
                            if assertion["basis"] == "model"
                            else self._evidence_records(connection, evidence_ids)
                        ),
                    }
                    result["result_hash"] = _fingerprint(result)
                    return result

                evidence = connection.execute(
                    """SELECT evidence.evidence_id
                       FROM evidence_records AS evidence
                       WHERE evidence.evidence_id = ? AND (
                           EXISTS (
                               SELECT 1 FROM entity_evidence AS link
                               JOIN entities AS entity
                                 ON entity.entity_id = link.entity_id
                                AND entity.status = 'accepted'
                               WHERE link.evidence_id = evidence.evidence_id
                           ) OR EXISTS (
                               SELECT 1 FROM assertion_evidence AS link
                               JOIN relation_assertions AS assertion
                                 ON assertion.assertion_id = link.assertion_id
                                AND assertion.status = 'accepted'
                                AND assertion.basis <> 'model'
                               JOIN entities AS subject
                                 ON subject.entity_id = assertion.subject_entity_id
                                AND subject.status = 'accepted'
                               JOIN entities AS source
                                 ON source.entity_id = assertion.source_entity_id
                                AND source.status = 'accepted'
                               JOIN entities AS target
                                 ON target.entity_id = assertion.target_entity_id
                                AND target.status = 'accepted'
                               WHERE link.evidence_id = evidence.evidence_id
                           )
                       )""",
                    (identifier,),
                ).fetchone()
                if evidence is None:
                    raise PrivateKnowledgeError("accepted claim or evidence was not found")
                linked_claims = [
                    {
                        "id": str(row["assertion_id"]),
                        "relation": _bounded_text(row["relation"], 120),
                        "basis": _bounded_text(row["basis"], 32),
                    }
                    for row in connection.execute(
                        """SELECT assertion.assertion_id, assertion.relation,
                                  assertion.basis
                           FROM assertion_evidence AS link
                           JOIN relation_assertions AS assertion
                             ON assertion.assertion_id = link.assertion_id
                            AND assertion.status = 'accepted'
                            AND assertion.basis <> 'model'
                           JOIN entities AS subject
                             ON subject.entity_id = assertion.subject_entity_id
                            AND subject.status = 'accepted'
                           JOIN entities AS source
                             ON source.entity_id = assertion.source_entity_id
                            AND source.status = 'accepted'
                           JOIN entities AS target
                             ON target.entity_id = assertion.target_entity_id
                            AND target.status = 'accepted'
                           WHERE link.evidence_id = ?
                           ORDER BY assertion.assertion_id LIMIT ?""",
                        (identifier, MAX_GRAPH_EDGES),
                    )
                ]
                result = {
                    "ok": True,
                    "kind": "evidence",
                    "evidence": self._evidence_records(connection, [identifier])[0],
                    "linked_claims": linked_claims,
                }
                result["result_hash"] = _fingerprint(result)
                return result
        except PrivateKnowledgeError:
            raise
        except sqlite3.Error as exc:
            raise PrivateKnowledgeError("local provenance could not be read") from exc

    def collections_status(self) -> dict[str, Any]:
        try:
            with closing(self._connect()) as connection:
                counts = {
                    "accepted_entities": int(
                        connection.execute(
                            "SELECT count(*) FROM entities WHERE status = 'accepted'"
                        ).fetchone()[0]
                    ),
                    "accepted_claims": int(
                        connection.execute(
                            """SELECT count(*) FROM relation_assertions AS assertion
                               JOIN entities AS subject
                                 ON subject.entity_id = assertion.subject_entity_id
                                AND subject.status = 'accepted'
                               JOIN entities AS source
                                 ON source.entity_id = assertion.source_entity_id
                                AND source.status = 'accepted'
                               JOIN entities AS target
                                 ON target.entity_id = assertion.target_entity_id
                                AND target.status = 'accepted'
                               WHERE assertion.status = 'accepted'
                                 AND assertion.basis <> 'model'
                                 AND EXISTS (
                                     SELECT 1 FROM assertion_evidence AS grounded
                                     JOIN evidence_records AS evidence
                                       ON evidence.evidence_id = grounded.evidence_id
                                     WHERE grounded.assertion_id = assertion.assertion_id
                                 )"""
                        ).fetchone()[0]
                    ),
                }
                language_rows = connection.execute(
                    """SELECT language FROM terms
                       JOIN entities USING(entity_id) WHERE entities.status = 'accepted'
                       UNION SELECT language FROM content_items
                       JOIN entities USING(entity_id) WHERE entities.status = 'accepted'
                       UNION SELECT target_language FROM translations
                       JOIN entities USING(entity_id) WHERE entities.status = 'accepted'
                       ORDER BY language LIMIT 129"""
                ).fetchall()
                evidence_rows = connection.execute(
                    """SELECT evidence.corpus_id, evidence.source_hash,
                              count(DISTINCT evidence.evidence_id) AS record_count
                       FROM evidence_records AS evidence
                       WHERE EXISTS (
                           SELECT 1 FROM entity_evidence AS link
                           JOIN entities AS entity
                             ON entity.entity_id = link.entity_id
                            AND entity.status = 'accepted'
                           WHERE link.evidence_id = evidence.evidence_id
                       ) OR EXISTS (
                           SELECT 1 FROM assertion_evidence AS link
                           JOIN relation_assertions AS assertion
                             ON assertion.assertion_id = link.assertion_id
                            AND assertion.status = 'accepted'
                            AND assertion.basis <> 'model'
                           JOIN entities AS subject
                             ON subject.entity_id = assertion.subject_entity_id
                            AND subject.status = 'accepted'
                           JOIN entities AS source
                             ON source.entity_id = assertion.source_entity_id
                            AND source.status = 'accepted'
                           JOIN entities AS target
                             ON target.entity_id = assertion.target_entity_id
                            AND target.status = 'accepted'
                           WHERE link.evidence_id = evidence.evidence_id
                       )
                       GROUP BY evidence.corpus_id, evidence.source_hash
                       ORDER BY evidence.corpus_id, evidence.source_hash LIMIT ?""",
                    (MAX_COLLECTIONS * MAX_SOURCE_HASHES_PER_COLLECTION + 1,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise PrivateKnowledgeError("local collection status could not be read") from exc

        collections_truncated = len(evidence_rows) > (
            MAX_COLLECTIONS * MAX_SOURCE_HASHES_PER_COLLECTION
        )
        collections: dict[str, dict[str, Any]] = {}
        for row in evidence_rows[: MAX_COLLECTIONS * MAX_SOURCE_HASHES_PER_COLLECTION]:
            corpus_id = str(row["corpus_id"])
            if corpus_id not in collections and len(collections) >= MAX_COLLECTIONS:
                collections_truncated = True
                continue
            collection = collections.setdefault(
                corpus_id,
                {
                    "collection_id": _opaque_id("collection", corpus_id),
                    "evidence_records": 0,
                    "source_hashes": [],
                },
            )
            collection["evidence_records"] += int(row["record_count"])
            source_hash = _safe_source_hash(row["source_hash"])
            if (
                source_hash
                and len(collection["source_hashes"]) < MAX_SOURCE_HASHES_PER_COLLECTION
            ):
                collection["source_hashes"].append(source_hash)
            elif source_hash:
                collections_truncated = True
        result = {
            "ready": True,
            "read_only": True,
            "model_invocation": False,
            "languages": [
                _bounded_text(row["language"], 32) for row in language_rows[:128]
            ],
            "languages_truncated": len(language_rows) > 128,
            "counts": counts,
            "collections": list(collections.values()),
            "collections_truncated": collections_truncated,
        }
        result["status_hash"] = _fingerprint(result)
        return result
