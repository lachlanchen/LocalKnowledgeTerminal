from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from .models import Evidence


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE items (
    row_id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_language TEXT NOT NULL,
    source_primary TEXT NOT NULL,
    en_primary TEXT NOT NULL,
    ja_primary TEXT NOT NULL,
    zh_primary TEXT NOT NULL,
    follow_ups TEXT NOT NULL,
    ja_tokens TEXT NOT NULL,
    pdf_page INTEGER,
    locator TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    translations TEXT NOT NULL
);
CREATE INDEX idx_items_ordinal ON items(ordinal);
CREATE VIRTUAL TABLE items_fts USING fts5(
    source_primary,
    en_primary,
    ja_primary,
    zh_primary,
    follow_ups,
    content='items',
    content_rowid='row_id',
    tokenize='unicode61 remove_diacritics 2'
);
"""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _language(record: dict[str, Any], code: str) -> dict[str, Any]:
    languages = record.get("languages")
    if not isinstance(languages, dict):
        return {}
    value = languages.get(code)
    return value if isinstance(value, dict) else {}


def _primary(value: dict[str, Any]) -> str:
    return _text(value.get("primary"))


def _follow_ups(value: dict[str, Any]) -> list[str]:
    items = value.get("follow_ups")
    if not isinstance(items, list):
        return []
    return [_text(item) for item in items if _text(item)]


def _record_row(record: dict[str, Any]) -> tuple[Any, ...]:
    item_id = _text(record.get("id"))
    kind = _text(record.get("kind"))
    source = record.get("source")
    source = source if isinstance(source, dict) else {}
    source_primary = _primary(source)
    try:
        ordinal = int(record.get("ordinal"))
    except (TypeError, ValueError) as exc:
        raise ValueError("record must include an integer ordinal") from exc
    if not item_id or kind not in {"answer", "question"} or not source_primary:
        raise ValueError("record must include id, answer/question kind, and source.primary")

    en, ja, zh = (_language(record, code) for code in ("en", "ja", "zh"))
    if not all((_primary(en), _primary(ja), _primary(zh))):
        raise ValueError(f"{item_id} must include reviewed en, ja, and zh primary text")
    translations = {
        "en": {"primary": _primary(en), "follow_ups": _follow_ups(en)},
        "ja": {"primary": _primary(ja), "follow_ups": _follow_ups(ja)},
        "zh": {"primary": _primary(zh), "follow_ups": _follow_ups(zh)},
    }
    follow_ups = list(
        dict.fromkeys(_follow_ups(source) + _follow_ups(en) + _follow_ups(ja) + _follow_ups(zh))
    )
    tokens = ja.get("tokens") if isinstance(ja.get("tokens"), dict) else {}
    ja_tokens = tokens.get("primary") if isinstance(tokens.get("primary"), list) else []
    evidence = record.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    page_value = evidence.get("pdf_page")
    try:
        pdf_page = int(page_value) if page_value is not None else None
    except (TypeError, ValueError):
        pdf_page = None
    locator = _text(evidence.get("epub_member"))
    return (
        item_id,
        kind,
        ordinal,
        _text(source.get("language")),
        source_primary,
        _primary(en),
        _primary(ja),
        _primary(zh),
        json.dumps(follow_ups, ensure_ascii=False),
        json.dumps(ja_tokens, ensure_ascii=False),
        pdf_page,
        locator,
        _text(record.get("source_hash")),
        json.dumps(translations, ensure_ascii=False),
    )


def _rows(source: Path) -> Iterable[tuple[Any, ...]]:
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("line must be a JSON object")
                yield _record_row(record)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid card-book line {line_number}: {exc}") from exc


def build_card_book_index(
    source: Path,
    destination: Path,
    corpus_id: str,
    source_title: str,
    expected_kind: str,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Build an atomic multilingual FTS index from a validated card-book JSONL."""

    source = source.resolve()
    destination = destination.resolve()
    if expected_kind not in {"answer", "question"}:
        raise ValueError("expected_kind must be 'answer' or 'question'")
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".building")
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1024 * 1024), b""):
            digest.update(chunk)

    connection = sqlite3.connect(temporary)
    count = 0
    try:
        connection.executescript(SCHEMA)
        batch: list[tuple[Any, ...]] = []
        for row in _rows(source):
            if row[1] != expected_kind:
                raise ValueError(f"expected {expected_kind} records, found {row[1]}")
            batch.append(row)
            if len(batch) >= 250:
                connection.executemany(
                    """INSERT INTO items(
                        item_id, kind, ordinal, source_language, source_primary,
                        en_primary, ja_primary, zh_primary, follow_ups, ja_tokens,
                        pdf_page, locator, source_hash, translations
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    batch,
                )
                count += len(batch)
                batch.clear()
                if progress:
                    progress(count)
        if batch:
            connection.executemany(
                """INSERT INTO items(
                    item_id, kind, ordinal, source_language, source_primary,
                    en_primary, ja_primary, zh_primary, follow_ups, ja_tokens,
                    pdf_page, locator, source_hash, translations
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            count += len(batch)
        connection.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
        metadata = {
            "corpus_id": corpus_id,
            "source_title": source_title,
            "kind": expected_kind,
            "item_count": str(count),
            "source_name": source.name,
            "source_sha256": digest.hexdigest(),
            "schema_version": "1",
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
        )
        connection.commit()
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        connection.close()
    os.replace(temporary, destination)
    return {
        "items": count,
        "kind": expected_kind,
        "sha256": digest.hexdigest(),
        "database": str(destination),
    }


class CardBookIndex:
    def __init__(self, database: Path):
        self.database = database.resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(
                f"card-book index not found: {self.database}; run `lkt ingest-card-book` first"
            )
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def metadata(self) -> dict[str, str]:
        with closing(self._connect()) as connection:
            return dict(connection.execute("SELECT key, value FROM metadata"))

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT count(*) FROM items").fetchone()[0])

    def _evidence(self, row: sqlite3.Row, metadata: dict[str, str]) -> Evidence:
        page = row["pdf_page"]
        translations = json.loads(row["translations"])
        translations["ja"]["ruby_tokens"] = json.loads(row["ja_tokens"])
        return Evidence(
            entry_id=row["item_id"],
            headword=row["en_primary"] or row["source_primary"],
            section=f"{row['kind'].title()} #{row['ordinal']:03d}",
            date_label="",
            pages=(int(page),) if page is not None else (),
            excerpt=row["source_primary"],
            corpus_id=metadata.get("corpus_id", ""),
            source_title=metadata.get("source_title", ""),
            kind=row["kind"],
            locator=row["locator"],
            translations=translations,
        )

    def draw(self, seed: str) -> Evidence:
        metadata = self.metadata()
        count = self.count()
        if count < 1:
            raise LookupError("card-book index is empty")
        material = f"{metadata.get('corpus_id', '')}\0{seed.strip().casefold()}"
        offset = int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big") % count
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM items ORDER BY ordinal LIMIT 1 OFFSET ?", (offset,)
            ).fetchone()
        if row is None:
            raise LookupError("could not draw a card-book item")
        return self._evidence(row, metadata)

    def search(self, query: str, limit: int = 4) -> list[Evidence]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(limit, 12))
        tokens = [
            token.replace('"', "")
            for token in re.findall(r"[^\W_]+", query, flags=re.UNICODE)
            if token
        ]
        metadata = self.metadata()
        rows: list[sqlite3.Row] = []
        with closing(self._connect()) as connection:
            if tokens:
                expression = " OR ".join(f'"{token}"*' for token in tokens[:12])
                rows = connection.execute(
                    """SELECT items.*
                       FROM items_fts
                       JOIN items ON items.row_id = items_fts.rowid
                       WHERE items_fts MATCH ?
                       ORDER BY bm25(items_fts, 2.0, 3.0, 2.0, 2.0, 1.0)
                       LIMIT ?""",
                    (expression, limit),
                ).fetchall()
            if not rows:
                pattern = f"%{query.casefold()}%"
                rows = connection.execute(
                    """SELECT * FROM items
                       WHERE lower(source_primary) LIKE ? OR lower(en_primary) LIKE ?
                          OR lower(ja_primary) LIKE ? OR lower(zh_primary) LIKE ?
                       ORDER BY ordinal LIMIT ?""",
                    (pattern, pattern, pattern, pattern, limit),
                ).fetchall()
        return [self._evidence(row, metadata) for row in rows]
