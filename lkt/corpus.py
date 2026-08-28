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

from .models import CorpusEntry, Evidence


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE entries (
    row_id INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL UNIQUE,
    headword TEXT NOT NULL,
    headword_key TEXT NOT NULL,
    display_headword TEXT NOT NULL,
    display_key TEXT NOT NULL,
    section TEXT NOT NULL,
    date_label TEXT NOT NULL,
    plain_text TEXT NOT NULL,
    related_targets TEXT NOT NULL,
    source_pages TEXT NOT NULL
);
CREATE INDEX idx_entries_headword_key ON entries(headword_key);
CREATE INDEX idx_entries_display_key ON entries(display_key);
CREATE VIRTUAL TABLE entries_fts USING fts5(
    headword,
    display_headword,
    plain_text,
    related_targets,
    content='entries',
    content_rowid='row_id',
    tokenize='unicode61 remove_diacritics 2'
);
"""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _page_list(value: Any) -> tuple[int, ...]:
    pages: list[int] = []
    if isinstance(value, list):
        for item in value:
            try:
                pages.append(int(item))
            except (TypeError, ValueError):
                continue
    return tuple(dict.fromkeys(pages))


def record_to_entry(record: dict[str, Any]) -> CorpusEntry:
    entry_id = _text(record.get("id")) or _text(record.get("headword"))
    headword = _text(record.get("headword")).strip()
    display = _text(record.get("display_headword")).strip() or headword
    if not entry_id or not headword:
        raise ValueError("corpus record must include id and headword")
    plain_text = _text(record.get("plain_text")).strip()
    if not plain_text and isinstance(record.get("paragraphs"), list):
        plain_text = "\n".join(
            _text(item.get("text")).strip()
            for item in record["paragraphs"]
            if isinstance(item, dict) and _text(item.get("text")).strip()
        )
    return CorpusEntry(
        entry_id=entry_id,
        headword=headword,
        display_headword=display,
        section=_text(record.get("section")).strip(),
        date_label=_text(record.get("date_label")).strip(),
        plain_text=plain_text,
        related_targets=_string_list(record.get("related_targets")),
        source_pages=_page_list(record.get("source_pages")),
    )


def _rows(source: Path) -> Iterable[tuple[str, ...]]:
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                entry = record_to_entry(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid corpus line {line_number}: {exc}") from exc
            yield (
                entry.entry_id,
                entry.headword,
                entry.headword.casefold(),
                entry.display_headword,
                entry.display_headword.casefold(),
                entry.section,
                entry.date_label,
                entry.plain_text,
                json.dumps(entry.related_targets, ensure_ascii=False),
                json.dumps(entry.source_pages),
            )


def build_index(
    source: Path,
    destination: Path,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Build an atomic SQLite FTS index from Word Origins entries JSONL."""

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".building")
    temporary.unlink(missing_ok=True)
    count = 0
    digest = hashlib.sha256()
    with source.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1024 * 1024), b""):
            digest.update(chunk)
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(SCHEMA)
        batch: list[tuple[str, ...]] = []
        for row in _rows(source):
            batch.append(row)
            if len(batch) >= 250:
                connection.executemany(
                    """INSERT INTO entries(
                        entry_id, headword, headword_key, display_headword,
                        display_key, section, date_label, plain_text,
                        related_targets, source_pages
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    batch,
                )
                count += len(batch)
                batch.clear()
                if progress:
                    progress(count)
        if batch:
            connection.executemany(
                """INSERT INTO entries(
                    entry_id, headword, headword_key, display_headword,
                    display_key, section, date_label, plain_text,
                    related_targets, source_pages
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            count += len(batch)
        connection.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
        metadata = {
            "entry_count": str(count),
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
    return {"entries": count, "sha256": digest.hexdigest(), "database": str(destination)}


def _entry_from_row(row: sqlite3.Row) -> CorpusEntry:
    return CorpusEntry(
        entry_id=row["entry_id"],
        headword=row["headword"],
        display_headword=row["display_headword"],
        section=row["section"],
        date_label=row["date_label"],
        plain_text=row["plain_text"],
        related_targets=tuple(json.loads(row["related_targets"])),
        source_pages=tuple(json.loads(row["source_pages"])),
    )


def _excerpt(text: str, query: str, limit: int = 900) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    terms = [term.casefold() for term in re.findall(r"[^\W_]+", query)]
    lowered = compact.casefold()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 4)
    end = min(len(compact), start + limit)
    prefix = "…" if start else ""
    suffix = "…" if end < len(compact) else ""
    return prefix + compact[start:end].strip() + suffix


class CorpusIndex:
    def __init__(self, database: Path):
        self.database = database.resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(
                f"corpus index not found: {self.database}; run `lkt ingest` first"
            )
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def metadata(self) -> dict[str, str]:
        with closing(self._connect()) as connection:
            return dict(connection.execute("SELECT key, value FROM metadata"))

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT count(*) FROM entries").fetchone()[0])

    def search(self, query: str, limit: int = 4) -> list[Evidence]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(limit, 12))
        results: list[CorpusEntry] = []
        seen: set[str] = set()
        with closing(self._connect()) as connection:
            exact_rows = connection.execute(
                """SELECT * FROM entries
                   WHERE headword_key = ? OR display_key = ?
                   ORDER BY CASE WHEN headword_key = ? THEN 0 ELSE 1 END
                   LIMIT ?""",
                (query.casefold(), query.casefold(), query.casefold(), limit),
            ).fetchall()
            for row in exact_rows:
                entry = _entry_from_row(row)
                results.append(entry)
                seen.add(entry.entry_id)

            tokens = [
                token.replace('"', "")
                for token in re.findall(r"[^\W_]+", query, flags=re.UNICODE)
                if token
            ]
            if tokens and len(results) < limit:
                expression = " OR ".join(f'"{token}"*' for token in tokens[:12])
                rows = connection.execute(
                    """SELECT entries.*
                       FROM entries_fts
                       JOIN entries ON entries.row_id = entries_fts.rowid
                       WHERE entries_fts MATCH ?
                       ORDER BY bm25(entries_fts, 8.0, 5.0, 1.0, 2.0)
                       LIMIT ?""",
                    (expression, limit * 3),
                ).fetchall()
                for row in rows:
                    entry = _entry_from_row(row)
                    if entry.entry_id in seen:
                        continue
                    results.append(entry)
                    seen.add(entry.entry_id)
                    if len(results) >= limit:
                        break
        return [entry.evidence(_excerpt(entry.plain_text, query)) for entry in results]
