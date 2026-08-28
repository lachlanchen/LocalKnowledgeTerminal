from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any


FREEDICT_CORPUS_ID = "freedict-eng-ara:0.6.3"
FREEDICT_SOURCE_TITLE = "FreeDict English-Arabic 0.6.3"
FREEDICT_LICENSE = "GPL-2.0-or-later"
FREEDICT_SOURCE_URL = (
    "https://github.com/freedict/fd-dictionaries/tree/"
    "5bdceeac8d0dba3298c1bebe734f60d54dad30f7/eng-ara"
)
_TEI_NAMESPACE = "{http://www.tei-c.org/ns/1.0}"


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
    translation TEXT NOT NULL,
    translation_key TEXT NOT NULL,
    UNIQUE(headword_key, translation_key)
);
CREATE INDEX idx_freedict_headword ON entries(headword_key);
"""


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _element_text(element: ET.Element) -> str:
    return _compact("".join(element.itertext()))


def _rows(source: Path) -> Iterable[tuple[str, str, str, str, str]]:
    """Stream exact headword/translation pairs from FreeDict's TEI source."""

    seen: set[tuple[str, str]] = set()
    for _event, entry in ET.iterparse(source, events=("end",)):
        if entry.tag != f"{_TEI_NAMESPACE}entry":
            continue
        orth = entry.find(f"{_TEI_NAMESPACE}form/{_TEI_NAMESPACE}orth")
        headword = _element_text(orth) if orth is not None else ""
        headword_key = headword.casefold()
        if headword_key:
            for citation in entry.findall(f".//{_TEI_NAMESPACE}cit"):
                if citation.get("type") != "trans":
                    continue
                quote = citation.find(f"{_TEI_NAMESPACE}quote")
                translation = _element_text(quote) if quote is not None else ""
                translation_key = translation.casefold()
                key = (headword_key, translation_key)
                if not translation_key or key in seen:
                    continue
                seen.add(key)
                entry_key = hashlib.sha256(
                    f"{headword_key}\0{translation_key}".encode("utf-8")
                ).hexdigest()[:20]
                yield (
                    f"freedict-eng-ara:{entry_key}",
                    headword,
                    headword_key,
                    translation,
                    translation_key,
                )
        entry.clear()


def build_freedict_index(
    source: Path,
    destination: Path,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Build an atomic exact-match SQLite index from FreeDict TEI."""

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".building")
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1024 * 1024), b""):
            digest.update(chunk)

    count = 0
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(SCHEMA)
        batch: list[tuple[str, str, str, str, str]] = []
        for row in _rows(source):
            batch.append(row)
            if len(batch) < 500:
                continue
            connection.executemany(
                """INSERT INTO entries(
                       entry_id, headword, headword_key, translation, translation_key
                   ) VALUES (?, ?, ?, ?, ?)""",
                batch,
            )
            count += len(batch)
            batch.clear()
            if progress and count % 5000 == 0:
                progress(count)
        if batch:
            connection.executemany(
                """INSERT INTO entries(
                       entry_id, headword, headword_key, translation, translation_key
                   ) VALUES (?, ?, ?, ?, ?)""",
                batch,
            )
            count += len(batch)
        metadata = {
            "corpus_id": FREEDICT_CORPUS_ID,
            "source_title": FREEDICT_SOURCE_TITLE,
            "license": FREEDICT_LICENSE,
            "license_locator": FREEDICT_SOURCE_URL,
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
    return {
        "entries": count,
        "sha256": digest.hexdigest(),
        "database": str(destination),
        "corpus_id": FREEDICT_CORPUS_ID,
    }


class FreeDictRag:
    """Exact English-to-Arabic candidate retrieval over a compact local index."""

    def __init__(self, database: Path):
        self.database = database.resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(
                f"FreeDict index not found: {self.database}; run `lkt ingest-freedict`"
            )
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def metadata(self) -> dict[str, str]:
        with closing(self._connect()) as connection:
            return dict(connection.execute("SELECT key, value FROM metadata"))

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = _compact(query)
        if not query:
            return []
        limit = max(1, min(int(limit), 20))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT entry_id, headword, translation
                     FROM entries
                    WHERE headword_key = ?
                    ORDER BY row_id
                    LIMIT ?""",
                (query.casefold(), limit),
            ).fetchall()
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        return [
            {
                "entry_id": str(row["entry_id"]),
                "headword": str(row["headword"]),
                "definition": "",
                "translations": {"ar": [str(row["translation"])]},
                "corpus_id": metadata.get("corpus_id", FREEDICT_CORPUS_ID),
                "source_title": metadata.get(
                    "source_title", FREEDICT_SOURCE_TITLE
                ),
                "source_hash": metadata.get("source_sha256", ""),
                "kind": "bilingual-dictionary",
                "translation_scope": "exact-headword",
                "locator": f"headword {row['headword']}",
                "license_locator": metadata.get(
                    "license_locator", FREEDICT_SOURCE_URL
                ),
                "license": metadata.get("license", FREEDICT_LICENSE),
            }
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        if not self.database.is_file():
            return {"ready": False, "database": str(self.database)}
        metadata = self.metadata()
        return {
            "ready": True,
            "database": str(self.database),
            "entries": int(metadata.get("entry_count", "0")),
            "corpus_id": metadata.get("corpus_id", FREEDICT_CORPUS_ID),
            "source_sha256": metadata.get("source_sha256", ""),
        }
