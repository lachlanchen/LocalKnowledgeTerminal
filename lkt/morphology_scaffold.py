from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .knowledge import _identifier


PARSER_VERSION = "book-morphology-scaffold-v2"
_LEXICAL_WORD = re.compile(r"^[A-Za-z][A-Za-z'’-]{1,48}$")
_READING_HEAD = re.compile(
    r"(?<![A-Za-z])([A-Za-z][A-Za-z'’-]{1,40})(?:[＊*·•]+)?\s*［"
)
_EXPLICIT_COMPONENT = re.compile(
    r"(?P<form>[－-]?[A-Za-z][A-Za-z/／〈<>-]{0,28}[－-]?)\s*"
    r"[（(]\s*[=＝]\s*"
    r"(?P<meaning>[A-Za-z][A-Za-z0-9 ,;/.'’-]{0,72})\s*[）)]"
)
_ROOT_DECLARATION = re.compile(
    r"词\s*根\s+(?P<forms>.{1,180}?)(?=来自|意为|[。；]|$)"
)
_EXPLICIT_MEANING = re.compile(r"意为\s*([A-Za-z][^。，；]{0,100})")
_ASCII_FORM = re.compile(r"[A-Za-z][A-Za-z-]{1,24}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS morphology_source_scaffolds (
    scaffold_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('root', 'affix')),
    corpus_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    headword TEXT NOT NULL,
    headword_normalized TEXT NOT NULL,
    source_page INTEGER NOT NULL DEFAULT 0,
    locator TEXT NOT NULL DEFAULT '',
    raw_excerpt TEXT NOT NULL,
    parse_state TEXT NOT NULL,
    entry_term_count INTEGER NOT NULL DEFAULT 0,
    component_count INTEGER NOT NULL DEFAULT 0,
    relation_count INTEGER NOT NULL DEFAULT 0,
    graph_revision INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(corpus_id, source_entry_id, source_hash)
);
CREATE INDEX IF NOT EXISTS idx_morphology_scaffold_coverage
    ON morphology_source_scaffolds(
        source_kind, corpus_id, source_hash, parse_state, source_entry_id
    );
"""


@dataclass(frozen=True)
class ExplicitComponent:
    form: str
    kind: str
    meaning: str
    raw_form: str


@dataclass(frozen=True)
class ParsedScaffold:
    record_id: str
    headword: str
    source_page: int
    raw_content: str
    parse_state: str
    entry_terms: tuple[str, ...]
    components: tuple[ExplicitComponent, ...]
    links: tuple[tuple[str, int], ...]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _letters(value: str) -> str:
    return re.sub(r"[^a-z]", "", _normalise(value))


def _looks_index_only(content: str) -> bool:
    """Conservatively identify page-reference rows without lexical claims."""

    if re.match(r"^\s*(?:LECTURE|CONTENTS|INDEX)\b", content, re.IGNORECASE):
        return True
    if any(marker in content for marker in ("词根", "＝", "=", "．", "。", "；")):
        return False
    numbers = re.findall(r"(?<![A-Za-z])\d{1,4}(?:[RS])?(?![A-Za-z])", content)
    words = re.findall(r"[A-Za-z][A-Za-z'’-]{1,48}", content)
    return len(numbers) >= 2 and len(words) >= 2 and len(content) < 240


def _component_form(raw_form: str, headword: str, forced_kind: str = "") -> tuple[str, str]:
    raw_form = raw_form.strip()
    leading = raw_form.startswith(("-", "－"))
    trailing = raw_form.endswith(("-", "－"))
    primary = re.split(r"[/／〈<>]", raw_form.strip("-－"), maxsplit=1)[0]
    primary = "".join(re.findall(r"[A-Za-z]+", primary)).casefold()
    if not primary:
        return "", ""
    if forced_kind:
        kind = forced_kind
    elif leading and not trailing:
        kind = "suffix"
    elif trailing and not leading:
        kind = "prefix"
    else:
        kind = "unknown"
    if kind == "prefix":
        return f"{primary}-", kind
    if kind == "suffix":
        return f"-{primary}", kind
    return primary, kind


def parse_record(
    record_id: str,
    headword: str,
    source_page: int,
    content: str,
    source_kind: str,
) -> ParsedScaffold:
    """Extract only explicitly printed entries, definitions, and components."""

    content = " ".join(str(content).split())
    index_only = _looks_index_only(content) or _normalise(headword) in {
        "lecture",
        "contents",
        "index",
    }
    entry_terms: list[str] = []
    if (
        not index_only
        and _LEXICAL_WORD.fullmatch(headword.strip())
        and not headword.strip().isupper()
        and not content.lstrip().startswith(("-", "－"))
        and _normalise(content).startswith(_normalise(headword))
    ):
        entry_terms.append(headword.strip())
    if not index_only:
        for match in _READING_HEAD.finditer(content):
            term = match.group(1).strip()
            if _LEXICAL_WORD.fullmatch(term):
                entry_terms.append(term)
    entry_terms = list(dict.fromkeys(entry_terms))[:24]
    if index_only:
        return ParsedScaffold(
            record_id=str(record_id),
            headword=str(headword),
            source_page=int(source_page or 0),
            raw_content=content,
            parse_state="evidence-only",
            entry_terms=(),
            components=(),
            links=(),
        )

    components: list[ExplicitComponent] = []
    component_positions: dict[tuple[str, str], int] = {}
    for match in _EXPLICIT_COMPONENT.finditer(content):
        raw_form = match.group("form").strip()
        declaration_context = content[max(0, match.start() - 120) : match.start()]
        form, kind = _component_form(
            raw_form,
            headword,
            "root" if re.search(r"词\s*根", declaration_context) else "",
        )
        meaning = " ".join(match.group("meaning").split()).strip(" ,.;")
        key = (form, kind)
        if not form or not meaning:
            continue
        if key in component_positions:
            continue
        component_positions[key] = len(components)
        components.append(ExplicitComponent(form, kind, meaning, raw_form))

    declaration = _ROOT_DECLARATION.search(content)
    if declaration:
        explicit_meaning_match = _EXPLICIT_MEANING.search(content)
        explicit_meaning = (
            " ".join(explicit_meaning_match.group(1).split()).strip(" ,.;")
            if explicit_meaning_match
            else ""
        )
        declared_forms = re.sub(
            r"[（(][^）)]*[）)]", " ", declaration.group("forms")
        )
        for raw_form in _ASCII_FORM.findall(declared_forms)[:16]:
            if raw_form.casefold() in {"from", "root", "lecture"}:
                continue
            form, kind = _component_form(raw_form, headword, "root")
            key = (form, kind)
            if not form:
                continue
            if key in component_positions:
                position = component_positions[key]
                existing = components[position]
                if explicit_meaning and not existing.meaning:
                    components[position] = ExplicitComponent(
                        existing.form,
                        existing.kind,
                        explicit_meaning,
                        existing.raw_form,
                    )
                continue
            component_positions[key] = len(components)
            components.append(
                ExplicitComponent(form, kind, explicit_meaning, raw_form)
            )
    components = components[:24]

    links: list[tuple[str, int]] = []
    for term in entry_terms:
        word = _letters(term)
        is_primary_entry = _normalise(term) == _normalise(headword)
        for ordinal, component in enumerate(components):
            part = _letters(component.form)
            if part and (is_primary_entry or part in word):
                links.append((term, ordinal))
    links = list(dict.fromkeys(links))[:96]

    if components and links:
        parse_state = "explicit-components"
    elif components:
        parse_state = "explicit-morphemes"
    elif entry_terms:
        parse_state = "explicit-entry"
    else:
        parse_state = "evidence-only"
    return ParsedScaffold(
        record_id=str(record_id),
        headword=str(headword),
        source_page=int(source_page or 0),
        raw_content=content,
        parse_state=parse_state,
        entry_terms=tuple(entry_terms),
        components=tuple(components),
        links=tuple(links),
    )


def _ensure_term(
    connection: sqlite3.Connection, term: str, timestamp: str
) -> tuple[str, bool]:
    normalized = _normalise(term)
    canonical_key = f"term:en:word:{normalized}"
    entity_id = _identifier("term", canonical_key)
    payload = json.dumps(
        {
            "language": "en",
            "kind": "word",
            "acquisition": "book-morphology-scaffold",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cursor = connection.execute(
        """INSERT OR IGNORE INTO entities(
               entity_id, entity_type, canonical_key, label, status,
               quality_score, payload, created_at, updated_at
           ) VALUES (?, 'term', ?, ?, 'accepted', 1.0, ?, ?, ?)""",
        (entity_id, canonical_key, term, payload, timestamp, timestamp),
    )
    connection.execute(
        """INSERT OR IGNORE INTO terms(
               entity_id, language, text, normalized, kind
           ) VALUES (?, 'en', ?, ?, 'word')""",
        (entity_id, term, normalized),
    )
    return entity_id, bool(cursor.rowcount)


def _ensure_morpheme(
    connection: sqlite3.Connection,
    component: ExplicitComponent,
    timestamp: str,
) -> tuple[str, bool]:
    normalized = _normalise(component.form)
    canonical_key = f"morpheme:en:{component.kind}:{normalized}"
    entity_id = _identifier("morpheme", canonical_key)
    payload = json.dumps(
        {
            "language": "en",
            "kind": component.kind,
            "meaning": component.meaning,
            "acquisition": "book-morphology-scaffold",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cursor = connection.execute(
        """INSERT OR IGNORE INTO entities(
               entity_id, entity_type, canonical_key, label, status,
               quality_score, payload, created_at, updated_at
           ) VALUES (?, 'morpheme', ?, ?, 'accepted', 1.0, ?, ?, ?)""",
        (
            entity_id,
            canonical_key,
            component.form,
            payload,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """INSERT OR IGNORE INTO morphemes(
               entity_id, language, form, normalized, kind, meaning
           ) VALUES (?, 'en', ?, ?, ?, ?)""",
        (
            entity_id,
            component.form,
            normalized,
            component.kind,
            component.meaning,
        ),
    )
    return entity_id, bool(cursor.rowcount)


def _evidence(
    connection: sqlite3.Connection,
    parsed: ParsedScaffold,
    metadata: dict[str, str],
    source_kind: str,
) -> str:
    corpus_id = metadata["corpus_id"]
    source_hash = metadata["source_sha256"]
    locator = f"source page {parsed.source_page}" if parsed.source_page else ""
    material = (
        f"morphology-scaffold:{corpus_id}\0{parsed.record_id}\0"
        f"{source_hash}\0{locator}"
    )
    proposed_id = _identifier("evidence", material)
    payload = json.dumps(
        {
            "entry_id": parsed.record_id,
            "headword": parsed.headword,
            "section": f"{source_kind.title()} dictionary",
            "pages": [parsed.source_page] if parsed.source_page else [],
            "excerpt": parsed.raw_content,
            "raw_content": parsed.raw_content,
            "corpus_id": corpus_id,
            "source_title": metadata.get("source_title", ""),
            "kind": f"morphology-{source_kind}",
            "locator": locator,
            "source_hash": source_hash,
            "parser_version": metadata.get("parser_version", PARSER_VERSION),
            "parse_state": parsed.parse_state,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    connection.execute(
        """INSERT INTO evidence_records(
               evidence_id, corpus_id, source_entry_id, source_hash,
               locator, excerpt, payload
           ) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(corpus_id, source_entry_id, source_hash, locator)
           DO UPDATE SET excerpt = excluded.excerpt, payload = excluded.payload""",
        (
            proposed_id,
            corpus_id,
            parsed.record_id,
            source_hash,
            locator,
            parsed.raw_content,
            payload,
        ),
    )
    row = connection.execute(
        """SELECT evidence_id FROM evidence_records
           WHERE corpus_id = ? AND source_entry_id = ?
             AND source_hash = ? AND locator = ?""",
        (corpus_id, parsed.record_id, source_hash, locator),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"evidence checkpoint failed for {parsed.record_id}")
    return str(row[0])


def _link_entity_evidence(
    connection: sqlite3.Connection,
    entity_id: str,
    evidence_id: str,
    claim: str,
) -> None:
    connection.execute(
        """INSERT OR IGNORE INTO entity_evidence(
               entity_id, evidence_id, claim, confidence
           ) VALUES (?, ?, ?, 1.0)""",
        (entity_id, evidence_id, claim),
    )


def _append_property_value(
    properties: dict[str, Any], key: str, value: str
) -> None:
    value = str(value).strip()
    if not value:
        return
    existing = properties.get(key, [])
    values = list(existing) if isinstance(existing, list) else [existing]
    properties[key] = list(
        dict.fromkeys(str(item) for item in (*values, value) if str(item))
    )


def _ensure_component_meaning(
    connection: sqlite3.Connection,
    morpheme_id: str,
    component: ExplicitComponent,
    evidence_id: str,
    parsed: ParsedScaffold,
    metadata: dict[str, str],
    source_kind: str,
    timestamp: str,
) -> tuple[bool, bool, bool, bool]:
    """Persist one explicit gloss as a sense owned by its morpheme."""

    definition = " ".join(component.meaning.split()).strip(" ,.;")
    if not definition:
        return False, False, False, False
    subject = connection.execute(
        "SELECT entity_type FROM entities WHERE entity_id = ?", (morpheme_id,)
    ).fetchone()
    if subject is None or str(subject[0]) != "morpheme":
        raise RuntimeError(f"missing scaffold morpheme subject: {morpheme_id}")

    existing_senses = connection.execute(
        """SELECT meaning.entity_id, meaning.definition,
                  meaning.sense_order, entity.status
           FROM meanings AS meaning
           JOIN entities AS entity ON entity.entity_id = meaning.entity_id
           WHERE meaning.subject_entity_id = ? AND meaning.language = 'en'
           ORDER BY meaning.sense_order, meaning.entity_id""",
        (morpheme_id,),
    ).fetchall()
    normalized_definition = _normalise(definition)
    matching = next(
        (
            row
            for row in existing_senses
            if _normalise(str(row[1])) == normalized_definition
        ),
        None,
    )
    created_meaning = False
    status_changed = bool(matching is not None and str(matching[3]) != "accepted")
    if matching is not None:
        meaning_id = str(matching[0])
        sense_order = int(matching[2])
    else:
        sense_order = max((int(row[2]) for row in existing_senses), default=-1) + 1
        canonical_key = (
            f"meaning:{morpheme_id}:en:{sense_order}:{normalized_definition}"
        )
        meaning_id = _identifier("meaning", canonical_key)
        provenance = {
            "basis": "book",
            "confidence": 1.0,
            "evidence_ids": [evidence_id],
            "acquisition": "book-morphology-scaffold",
            "parser_version": PARSER_VERSION,
            "source_kind": source_kind,
            "corpus_id": metadata["corpus_id"],
            "source_title": metadata.get("source_title", ""),
            "source_hash": metadata["source_sha256"],
            "source_entry_id": parsed.record_id,
            "source_page": parsed.source_page,
            "headword": parsed.headword,
            "raw_form": component.raw_form,
            "component_kind": component.kind,
        }
        payload = json.dumps(
            {
                "subject_entity_id": morpheme_id,
                "term_id": None,
                "language": "en",
                "definition": definition,
                "part_of_speech": "",
                "register_label": "",
                "domain_label": "morphology",
                "sense_order": sense_order,
                "provenance": provenance,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cursor = connection.execute(
            """INSERT OR IGNORE INTO entities(
                   entity_id, entity_type, canonical_key, label, status,
                   quality_score, payload, created_at, updated_at
               ) VALUES (?, 'meaning', ?, ?, 'accepted', 1.0, ?, ?, ?)""",
            (
                meaning_id,
                canonical_key,
                definition,
                payload,
                timestamp,
                timestamp,
            ),
        )
        created_meaning = bool(cursor.rowcount)
        connection.execute(
            """INSERT OR IGNORE INTO meanings(
                   entity_id, subject_entity_id, term_id, language, definition,
                   part_of_speech, register_label, domain_label, sense_order
               ) VALUES (?, ?, NULL, 'en', ?, '', '', 'morphology', ?)""",
            (meaning_id, morpheme_id, definition, sense_order),
        )

    connection.execute(
        """UPDATE entities SET status = 'accepted',
                  quality_score = CASE
                      WHEN quality_score IS NULL OR quality_score < 1.0 THEN 1.0
                      ELSE quality_score END,
                  updated_at = CASE WHEN status <> 'accepted' THEN ? ELSE updated_at END
           WHERE entity_id = ? AND entity_type = 'meaning'""",
        (timestamp, meaning_id),
    )
    edge = connection.execute(
        """SELECT edge_id, basis, confidence, status, properties
           FROM entity_edges
           WHERE source_entity_id = ? AND target_entity_id = ?
             AND relation = 'has-meaning'""",
        (morpheme_id, meaning_id),
    ).fetchone()
    try:
        properties = json.loads(str(edge[4])) if edge is not None else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        properties = {}
    if not isinstance(properties, dict):
        properties = {}
    observed_raw_forms = properties.get("raw_forms", [])
    if not isinstance(observed_raw_forms, list):
        observed_raw_forms = [observed_raw_forms]
    observed_raw_forms = sorted(
        dict.fromkeys(
            str(value).strip()
            for value in (
                *observed_raw_forms,
                properties.get("raw_form", ""),
                component.raw_form,
            )
            if str(value).strip()
        ),
        key=lambda value: (_normalise(value), value),
    )
    properties.update(
        {
            "source_scaffold": True,
            "acquisition": "book-morphology-scaffold",
            "component_kind": component.kind,
            "raw_form": observed_raw_forms[0] if observed_raw_forms else "",
            "raw_forms": observed_raw_forms,
            "modes": ["knowledge", "word", "root", "affix"],
        }
    )
    for key, value in (
        ("parser_versions", metadata.get("parser_version", PARSER_VERSION)),
        ("source_kinds", source_kind),
        ("corpus_ids", metadata["corpus_id"]),
        ("source_titles", metadata.get("source_title", "")),
        ("source_hashes", metadata["source_sha256"]),
        ("source_entry_ids", parsed.record_id),
        ("evidence_ids", evidence_id),
    ):
        _append_property_value(properties, key, value)
    properties_json = json.dumps(properties, ensure_ascii=False, sort_keys=True)
    edge_changed = bool(
        edge is None
        or str(edge[1]) != "book"
        or float(edge[2]) != 1.0
        or str(edge[3]) != "accepted"
        or str(edge[4]) != properties_json
    )
    edge_id = _identifier(
        "edge", f"{morpheme_id}\0{meaning_id}\0has-meaning"
    )
    connection.execute(
        """INSERT INTO entity_edges(
               edge_id, source_entity_id, target_entity_id, relation,
               basis, confidence, status, properties, created_at, updated_at
           ) VALUES (?, ?, ?, 'has-meaning', 'book', 1.0, 'accepted', ?, ?, ?)
           ON CONFLICT(source_entity_id, target_entity_id, relation)
           DO UPDATE SET basis = 'book', confidence = 1.0,
                         status = 'accepted', properties = excluded.properties,
                         updated_at = CASE
                             WHEN entity_edges.basis <> 'book'
                               OR entity_edges.confidence <> 1.0
                               OR entity_edges.status <> 'accepted'
                               OR entity_edges.properties <> excluded.properties
                             THEN excluded.updated_at
                             ELSE entity_edges.updated_at END""",
        (
            edge_id,
            morpheme_id,
            meaning_id,
            properties_json,
            timestamp,
            timestamp,
        ),
    )
    evidence = connection.execute(
        """INSERT OR IGNORE INTO entity_evidence(
               entity_id, evidence_id, claim, confidence
           ) VALUES (?, ?, ?, 1.0)""",
        (
            meaning_id,
            evidence_id,
            f"explicit {component.kind} gloss {definition!r}",
        ),
    )
    evidence_added = bool(evidence.rowcount)
    return (
        created_meaning,
        edge is None,
        evidence_added,
        created_meaning or status_changed or edge_changed or evidence_added,
    )


def _stable_term_morpheme_ordinal(
    connection: sqlite3.Connection,
    term_id: str,
    morpheme_id: str,
) -> int:
    existing_link = connection.execute(
        """SELECT ordinal FROM term_morphemes
           WHERE term_id = ? AND morpheme_id = ?
           ORDER BY ordinal LIMIT 1""",
        (term_id, morpheme_id),
    ).fetchone()
    if existing_link is not None:
        return int(existing_link[0])
    next_ordinal = connection.execute(
        """SELECT COALESCE(MAX(ordinal), -1) + 1
           FROM term_morphemes WHERE term_id = ?""",
        (term_id,),
    ).fetchone()
    return int(next_ordinal[0])


def _link_component(
    connection: sqlite3.Connection,
    term_id: str,
    morpheme_id: str,
    component: ExplicitComponent,
    evidence_id: str,
    parsed: ParsedScaffold,
    source_kind: str,
    ordinal: int,
    timestamp: str,
) -> None:
    relation = "has-component"
    properties = json.dumps(
        {
            "source_scaffold": True,
            "source_kind": source_kind,
            "source_entry_id": parsed.record_id,
            "explicit": True,
            "raw_form": component.raw_form,
            "modes": ["knowledge", "word", "root", "affix"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    edge_key = f"{term_id}\0{morpheme_id}\0{relation}"
    edge_id = _identifier("edge", edge_key)
    connection.execute(
        """INSERT INTO entity_edges(
               edge_id, source_entity_id, target_entity_id, relation,
               basis, confidence, status, properties, created_at, updated_at
           ) VALUES (?, ?, ?, ?, 'book', 1.0, 'accepted', ?, ?, ?)
           ON CONFLICT(source_entity_id, target_entity_id, relation)
           DO UPDATE SET basis = 'book', confidence = 1.0,
                         status = 'accepted', properties = excluded.properties,
                         updated_at = excluded.updated_at""",
        (
            edge_id,
            term_id,
            morpheme_id,
            relation,
            properties,
            timestamp,
            timestamp,
        ),
    )
    assertion_key = f"{term_id}\0{term_id}\0{morpheme_id}\0{relation}"
    proposed_assertion_id = _identifier("assertion", assertion_key)
    connection.execute(
        """INSERT INTO relation_assertions(
               assertion_id, subject_entity_id, source_entity_id,
               target_entity_id, relation, basis, confidence, status,
               properties_json, revision, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, 'book', 1.0, 'accepted', ?, 1, ?, ?)
           ON CONFLICT(
               subject_entity_id, source_entity_id, target_entity_id, relation
           ) DO UPDATE SET basis = 'book', confidence = 1.0,
                           status = 'accepted',
                           properties_json = excluded.properties_json,
                           updated_at = excluded.updated_at""",
        (
            proposed_assertion_id,
            term_id,
            term_id,
            morpheme_id,
            relation,
            properties,
            timestamp,
            timestamp,
        ),
    )
    assertion = connection.execute(
        """SELECT assertion_id FROM relation_assertions
           WHERE subject_entity_id = ? AND source_entity_id = ?
             AND target_entity_id = ? AND relation = ?""",
        (term_id, term_id, morpheme_id, relation),
    ).fetchone()
    if assertion is None:
        raise RuntimeError("component assertion checkpoint failed")
    connection.execute(
        """INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id)
           VALUES (?, ?)""",
        (str(assertion[0]), evidence_id),
    )
    surface = _letters(component.form)
    stable_ordinal = _stable_term_morpheme_ordinal(
        connection, term_id, morpheme_id
    )
    connection.execute(
        """INSERT OR IGNORE INTO term_morphemes(
               term_id, morpheme_id, ordinal, surface, confidence, basis
           ) VALUES (?, ?, ?, ?, 1.0, 'book')""",
        (term_id, morpheme_id, stable_ordinal, surface),
    )


def repair_missing_term_morpheme_links(knowledge_db: Path) -> dict[str, int]:
    """Restore normalized links already proved by accepted scaffold claims.

    Both the assertion marker and its checkpoint evidence are required, so the
    repair cannot absorb unrelated book- or model-backed component assertions.
    It deliberately leaves every checkpoint, entity, edge, assertion, and
    evidence row unchanged.
    """

    with closing(sqlite3.connect(knowledge_db, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT DISTINCT assertion.source_entity_id AS term_id,
                              assertion.target_entity_id AS morpheme_id,
                              morpheme.form AS surface_form
               FROM relation_assertions AS assertion
               JOIN assertion_evidence AS assertion_source
                 ON assertion_source.assertion_id = assertion.assertion_id
               JOIN morphology_source_scaffolds AS scaffold
                 ON scaffold.evidence_id = assertion_source.evidence_id
               JOIN terms AS term
                 ON term.entity_id = assertion.source_entity_id
               JOIN morphemes AS morpheme
                 ON morpheme.entity_id = assertion.target_entity_id
               WHERE assertion.subject_entity_id = assertion.source_entity_id
                 AND assertion.relation = 'has-component'
                 AND assertion.basis = 'book'
                 AND assertion.status = 'accepted'
                 AND json_extract(
                       assertion.properties_json, '$.source_scaffold'
                     ) = 1
                 AND NOT EXISTS (
                       SELECT 1 FROM term_morphemes AS existing
                       WHERE existing.term_id = assertion.source_entity_id
                         AND existing.morpheme_id = assertion.target_entity_id
                     )
               ORDER BY term_id, morpheme_id"""
        ).fetchall()
        inserted = 0
        for row in rows:
            term_id = str(row[0])
            morpheme_id = str(row[1])
            stable_ordinal = _stable_term_morpheme_ordinal(
                connection, term_id, morpheme_id
            )
            cursor = connection.execute(
                """INSERT OR IGNORE INTO term_morphemes(
                       term_id, morpheme_id, ordinal, surface,
                       confidence, basis
                   ) VALUES (?, ?, ?, ?, 1.0, 'book')""",
                (
                    term_id,
                    morpheme_id,
                    stable_ordinal,
                    _letters(str(row[2])),
                ),
            )
            inserted += int(cursor.rowcount)

        revision = connection.execute(
            """SELECT value FROM schema_meta
               WHERE key = 'relation_graph_revision'"""
        ).fetchone()
        graph_revision = int(revision[0]) if revision else 0
        if inserted:
            graph_revision += 1
            connection.execute(
                """INSERT INTO schema_meta(key, value)
                   VALUES ('relation_graph_revision', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (str(graph_revision),),
            )
        connection.commit()
    return {
        "selected": len(rows),
        "inserted": inserted,
        "graph_revision": graph_revision,
    }


def repair_scaffold_component_meanings(
    knowledge_db: Path,
    *,
    limit: int = 1000,
    after_scaffold_id: str = "",
) -> dict[str, int]:
    """Replay bounded checkpoints into morpheme-owned, book-backed senses."""

    limit = max(1, min(int(limit), 5000))
    with closing(sqlite3.connect(knowledge_db, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT scaffold.scaffold_id, scaffold.source_kind,
                      scaffold.corpus_id, scaffold.source_entry_id,
                      scaffold.source_hash, scaffold.evidence_id,
                      scaffold.headword, scaffold.source_page,
                      scaffold.raw_excerpt, scaffold.parser_version,
                      COALESCE(
                          json_extract(evidence.payload, '$.source_title'), ''
                      ) AS source_title
               FROM morphology_source_scaffolds AS scaffold
               JOIN evidence_records AS evidence
                 ON evidence.evidence_id = scaffold.evidence_id
               WHERE scaffold.scaffold_id > ?
               ORDER BY scaffold.scaffold_id LIMIT ?""",
            (after_scaffold_id.strip(), limit),
        ).fetchall()
        claims: list[
            tuple[str, ExplicitComponent, str, ParsedScaffold, dict[str, str], str]
        ] = []
        seen_claims: set[tuple[str, str, str]] = set()
        blank_glosses = 0
        for row in rows:
            parsed = parse_record(
                str(row[3]), str(row[6]), int(row[7]), str(row[8]), str(row[1])
            )
            metadata = {
                "corpus_id": str(row[2]),
                "source_title": str(row[10]),
                "source_sha256": str(row[4]),
                "parser_version": str(row[9]),
            }
            for component in parsed.components:
                definition = " ".join(component.meaning.split()).strip(" ,.;")
                if not definition:
                    blank_glosses += 1
                    continue
                morpheme_id = _identifier(
                    "morpheme",
                    (
                        f"morpheme:en:{component.kind}:"
                        f"{_normalise(component.form)}"
                    ),
                )
                subject = connection.execute(
                    """SELECT 1 FROM morphemes WHERE entity_id = ?""",
                    (morpheme_id,),
                ).fetchone()
                if subject is None:
                    raise RuntimeError(
                        "checkpoint references a missing morpheme; "
                        f"repair stopped before commit: {morpheme_id}"
                    )
                claim_key = (morpheme_id, _normalise(definition), str(row[5]))
                if claim_key in seen_claims:
                    continue
                seen_claims.add(claim_key)
                claims.append(
                    (
                        morpheme_id,
                        component,
                        str(row[5]),
                        parsed,
                        metadata,
                        str(row[1]),
                    )
                )

        created_meanings = 0
        created_edges = 0
        linked_evidence = 0
        graph_changed = False
        timestamp = _now()
        for morpheme_id, component, evidence_id, parsed, metadata, source_kind in claims:
            created, edge_created, evidence_added, changed = _ensure_component_meaning(
                connection,
                morpheme_id,
                component,
                evidence_id,
                parsed,
                metadata,
                source_kind,
                timestamp,
            )
            created_meanings += int(created)
            created_edges += int(edge_created)
            linked_evidence += int(evidence_added)
            graph_changed = graph_changed or changed

        revision = connection.execute(
            """SELECT value FROM schema_meta
               WHERE key = 'relation_graph_revision'"""
        ).fetchone()
        graph_revision = int(revision[0]) if revision else 0
        if graph_changed:
            graph_revision += 1
            connection.execute(
                """INSERT INTO schema_meta(key, value)
                   VALUES ('relation_graph_revision', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (str(graph_revision),),
            )
        connection.commit()
    return {
        "checkpoints": len(rows),
        "selected_claims": len(claims),
        "blank_glosses": blank_glosses,
        "created_meanings": created_meanings,
        "created_edges": created_edges,
        "linked_evidence": linked_evidence,
        "graph_revision": graph_revision,
    }


def _store_batch(
    knowledge_db: Path,
    parsed_records: Iterable[ParsedScaffold],
    metadata: dict[str, str],
    source_kind: str,
) -> dict[str, int]:
    counts = {
        "scaffolded": 0,
        "evidence_only": 0,
        "entry_terms": 0,
        "components": 0,
        "relations": 0,
        "new_entities": 0,
        "meaning_claims": 0,
        "new_meanings": 0,
    }
    scaffold_ids: list[str] = []
    graph_changed = False
    timestamp = _now()
    with closing(sqlite3.connect(knowledge_db, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        for parsed in parsed_records:
            evidence_id = _evidence(connection, parsed, metadata, source_kind)
            term_ids: dict[str, str] = {}
            for term in parsed.entry_terms:
                term_id, created = _ensure_term(connection, term, timestamp)
                term_ids[term] = term_id
                counts["entry_terms"] += 1
                counts["new_entities"] += int(created)
                _link_entity_evidence(
                    connection,
                    term_id,
                    evidence_id,
                    f"explicit dictionary entry {term!r}",
                )
            morpheme_ids: list[str] = []
            for component in parsed.components:
                morpheme_id, created = _ensure_morpheme(
                    connection, component, timestamp
                )
                morpheme_ids.append(morpheme_id)
                counts["components"] += 1
                counts["new_entities"] += int(created)
                _link_entity_evidence(
                    connection,
                    morpheme_id,
                    evidence_id,
                    (
                        f"explicit {component.kind} {component.form!r}"
                        + (
                            f" defined as {component.meaning!r}"
                            if component.meaning
                            else ""
                        )
                    ),
                )
                if component.meaning.strip():
                    created_meaning, _, evidence_added, meaning_changed = (
                        _ensure_component_meaning(
                            connection,
                            morpheme_id,
                            component,
                            evidence_id,
                            parsed,
                            metadata,
                            source_kind,
                            timestamp,
                        )
                    )
                    counts["meaning_claims"] += int(evidence_added)
                    counts["new_meanings"] += int(created_meaning)
                    counts["new_entities"] += int(created_meaning)
                    graph_changed = graph_changed or meaning_changed
            term_ordinals: dict[str, int] = {}
            for term, component_ordinal in parsed.links:
                term_id = term_ids.get(term)
                if term_id is None or component_ordinal >= len(morpheme_ids):
                    continue
                ordinal = term_ordinals.get(term, 0)
                term_ordinals[term] = ordinal + 1
                _link_component(
                    connection,
                    term_id,
                    morpheme_ids[component_ordinal],
                    parsed.components[component_ordinal],
                    evidence_id,
                    parsed,
                    source_kind,
                    ordinal,
                    timestamp,
                )
                counts["relations"] += 1
                graph_changed = True
            locator = (
                f"source page {parsed.source_page}" if parsed.source_page else ""
            )
            scaffold_id = _identifier(
                "morphology-scaffold",
                (
                    f"{metadata['corpus_id']}:{parsed.record_id}:"
                    f"{metadata['source_sha256']}"
                ),
            )
            scaffold_ids.append(scaffold_id)
            connection.execute(
                """INSERT INTO morphology_source_scaffolds(
                       scaffold_id, source_kind, corpus_id, source_entry_id,
                       source_hash, evidence_id, headword, headword_normalized,
                       source_page, locator, raw_excerpt, parse_state,
                       entry_term_count, component_count, relation_count,
                       graph_revision, parser_version, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             0, ?, ?, ?)
                   ON CONFLICT(corpus_id, source_entry_id, source_hash)
                   DO UPDATE SET evidence_id = excluded.evidence_id,
                       headword = excluded.headword,
                       headword_normalized = excluded.headword_normalized,
                       source_page = excluded.source_page,
                       locator = excluded.locator,
                       raw_excerpt = excluded.raw_excerpt,
                       parse_state = excluded.parse_state,
                       entry_term_count = excluded.entry_term_count,
                       component_count = excluded.component_count,
                       relation_count = excluded.relation_count,
                       parser_version = excluded.parser_version,
                       updated_at = excluded.updated_at""",
                (
                    scaffold_id,
                    source_kind,
                    metadata["corpus_id"],
                    parsed.record_id,
                    metadata["source_sha256"],
                    evidence_id,
                    parsed.headword,
                    _normalise(parsed.headword),
                    parsed.source_page,
                    locator,
                    parsed.raw_content,
                    parsed.parse_state,
                    len(parsed.entry_terms),
                    len(parsed.components),
                    len(parsed.links),
                    PARSER_VERSION,
                    timestamp,
                    timestamp,
                ),
            )
            counts["scaffolded"] += 1
            counts["evidence_only"] += int(parsed.parse_state == "evidence-only")
        if graph_changed:
            connection.execute(
                """INSERT INTO schema_meta(key, value)
                   VALUES ('relation_graph_revision', '1')
                   ON CONFLICT(key) DO UPDATE
                   SET value = CAST(value AS INTEGER) + 1"""
            )
        revision_row = connection.execute(
            """SELECT value FROM schema_meta
               WHERE key = 'relation_graph_revision'"""
        ).fetchone()
        graph_revision = int(revision_row[0]) if revision_row else 0
        if scaffold_ids:
            placeholders = ",".join("?" for _ in scaffold_ids)
            connection.execute(
                """UPDATE morphology_source_scaffolds SET graph_revision = ?
                   WHERE scaffold_id IN ("""
                + placeholders
                + ")",
                (graph_revision, *scaffold_ids),
            )
        connection.commit()
    return counts


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT key, value FROM metadata")
    }


def progress(
    source_db: Path, knowledge_db: Path, source_kind: str
) -> dict[str, Any]:
    with closing(sqlite3.connect(source_db)) as source:
        metadata = _metadata(source)
        total = int(source.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    with closing(sqlite3.connect(knowledge_db, timeout=30)) as knowledge:
        table = knowledge.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'morphology_source_scaffolds'"""
        ).fetchone()
        if table is None:
            row = None
        else:
            row = knowledge.execute(
                """SELECT COUNT(*) AS scaffolded,
                          SUM(parse_state = 'evidence-only') AS evidence_only,
                          SUM(entry_term_count) AS entry_terms,
                          SUM(component_count) AS components,
                          SUM(relation_count) AS relations,
                          MAX(graph_revision) AS graph_revision
                   FROM morphology_source_scaffolds
                   WHERE source_kind = ? AND corpus_id = ? AND source_hash = ?""",
                (
                    source_kind,
                    metadata["corpus_id"],
                    metadata["source_sha256"],
                ),
            ).fetchone()
    scaffolded = int(row[0] or 0) if row else 0
    return {
        "source_kind": source_kind,
        "corpus_id": metadata["corpus_id"],
        "source_hash": metadata["source_sha256"],
        "source_total": total,
        "scaffolded": scaffolded,
        "remaining": max(0, total - scaffolded),
        "evidence_only": int(row[1] or 0) if row else 0,
        "explicit_entry_terms": int(row[2] or 0) if row else 0,
        "explicit_components": int(row[3] or 0) if row else 0,
        "explicit_relations": int(row[4] or 0) if row else 0,
        "graph_revision": int(row[5] or 0) if row else 0,
        "coverage_complete": scaffolded >= total,
        "model_enrichment": "tracked separately by accepted Root/Affix cards",
    }


def run(
    source_db: Path,
    knowledge_db: Path,
    source_kind: str,
    *,
    batch_size: int = 250,
    limit: int = 0,
    dry_run: bool = False,
) -> dict[str, Any]:
    if source_kind not in {"root", "affix"}:
        raise ValueError("source_kind must be root or affix")
    source_db = source_db.resolve()
    knowledge_db = knowledge_db.resolve()
    batch_size = max(1, min(int(batch_size), 500))
    limit = max(0, int(limit))
    started = time.monotonic()
    with closing(sqlite3.connect(source_db)) as source:
        source.row_factory = sqlite3.Row
        metadata = _metadata(source)
        rows = source.execute(
            """SELECT record_id, headword, source_page, content
               FROM records ORDER BY row_id"""
        ).fetchall()

    processed: set[str] = set()
    if not dry_run:
        with closing(sqlite3.connect(knowledge_db, timeout=30)) as knowledge:
            knowledge.executescript(SCHEMA)
            processed = {
                str(row[0])
                for row in knowledge.execute(
                    """SELECT source_entry_id FROM morphology_source_scaffolds
                       WHERE source_kind = ? AND corpus_id = ?
                         AND source_hash = ?""",
                    (
                        source_kind,
                        metadata["corpus_id"],
                        metadata["source_sha256"],
                    ),
                )
            }
    selected = [row for row in rows if str(row["record_id"]) not in processed]
    if limit:
        selected = selected[:limit]
    parsed = [
        parse_record(
            str(row["record_id"]),
            str(row["headword"]),
            int(row["source_page"] or 0),
            str(row["content"]),
            source_kind,
        )
        for row in selected
    ]
    totals = {
        "scaffolded": 0,
        "evidence_only": 0,
        "entry_terms": 0,
        "components": 0,
        "relations": 0,
        "new_entities": 0,
    }
    if dry_run:
        totals.update(
            {
                "scaffolded": len(parsed),
                "evidence_only": sum(
                    item.parse_state == "evidence-only" for item in parsed
                ),
                "entry_terms": sum(len(item.entry_terms) for item in parsed),
                "components": sum(len(item.components) for item in parsed),
                "relations": sum(len(item.links) for item in parsed),
            }
        )
    else:
        for offset in range(0, len(parsed), batch_size):
            batch_counts = _store_batch(
                knowledge_db,
                parsed[offset : offset + batch_size],
                metadata,
                source_kind,
            )
            for key, value in batch_counts.items():
                totals[key] += value
    elapsed = max(time.monotonic() - started, 0.000001)
    result = {
        "dry_run": dry_run,
        "parser_version": PARSER_VERSION,
        "source_kind": source_kind,
        "source_total": len(rows),
        "previously_scaffolded": len(processed),
        "selected": len(parsed),
        **totals,
        "elapsed_seconds": round(elapsed, 3),
        "records_per_second": round(len(parsed) / elapsed, 1),
        "samples": [
            {
                **asdict(item),
                "raw_content": item.raw_content[:360],
            }
            for item in parsed
            if item.parse_state != "evidence-only"
        ][:8],
    }
    if not dry_run:
        result["progress"] = progress(source_db, knowledge_db, source_kind)
    return result


def run_v3(
    knowledge_db: Path,
    sources: Iterable[tuple[str, Path]],
    *,
    batch_size: int = 100,
) -> dict[str, Any]:
    """Lazy compatibility entry point for versioned evidence-first ingestion."""

    from .morphology_ingest_v3 import run as run_versioned_ingest

    return run_versioned_ingest(
        knowledge_db, tuple(sources), batch_size=batch_size
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist deterministic book-grounded morphology scaffolds"
    )
    parser.add_argument("source_kind", choices=("root", "affix"))
    parser.add_argument("source_db", type=Path)
    parser.add_argument("knowledge_db", type=Path)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.source_db,
                args.knowledge_db,
                args.source_kind,
                batch_size=args.batch_size,
                limit=args.limit,
                dry_run=args.dry_run,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
