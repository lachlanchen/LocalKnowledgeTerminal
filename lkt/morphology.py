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
CREATE TABLE records (
    row_id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE,
    headword TEXT NOT NULL,
    headword_key TEXT NOT NULL,
    source_page INTEGER NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX idx_morphology_headword ON records(headword_key);
CREATE VIRTUAL TABLE records_fts USING fts5(
    headword,
    content,
    content='records',
    content_rowid='row_id',
    tokenize='unicode61 remove_diacritics 2'
);
"""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


_INDEX_TERM = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*\*?", re.UNICODE)
_INDEX_PAGE = re.compile(r"\d{1,4}")


def _is_index_only(content: str) -> bool:
    """Return whether content is only a run of index terms and page numbers."""

    references = 0
    term_tokens = 0
    for token in content.split():
        if _INDEX_PAGE.fullmatch(token):
            if not term_tokens:
                return False
            references += 1
            term_tokens = 0
        elif _INDEX_TERM.fullmatch(token):
            term_tokens += 1
        else:
            return False
    return references >= 2 and term_tokens == 0


def _rows(source: Path) -> Iterable[tuple[str, str, str, int, str]]:
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid morphology line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"invalid morphology line {line_number}: expected object")
            record_id = _text(record.get("id"))
            headword = _text(record.get("headword"))
            if not record_id or not headword:
                continue
            cells = record.get("cells")
            content = _compact(
                "\n".join(_text(cell) for cell in cells)
                if isinstance(cells, list)
                else ""
            )
            if not content or _is_index_only(content):
                continue
            try:
                source_page = int(record.get("source_page") or 0)
            except (TypeError, ValueError):
                source_page = 0
            yield record_id, headword, headword.casefold(), source_page, content


def build_morphology_index(
    source: Path,
    destination: Path,
    corpus_id: str,
    source_title: str,
    kind: str,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Build an atomic FTS index from a reviewed morphology entries JSONL."""

    if kind not in {"root", "affix"}:
        raise ValueError("morphology kind must be root or affix")
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
        batch: list[tuple[str, str, str, int, str]] = []
        for row in _rows(source):
            batch.append(row)
            if len(batch) >= 250:
                connection.executemany(
                    """INSERT INTO records(
                        record_id, headword, headword_key, source_page, content
                    ) VALUES (?, ?, ?, ?, ?)""",
                    batch,
                )
                count += len(batch)
                batch.clear()
                if progress:
                    progress(count)
        if batch:
            connection.executemany(
                """INSERT INTO records(
                    record_id, headword, headword_key, source_page, content
                ) VALUES (?, ?, ?, ?, ?)""",
                batch,
            )
            count += len(batch)
        connection.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
        metadata = {
            "corpus_id": corpus_id.strip(),
            "source_title": source_title.strip(),
            "kind": kind,
            "record_count": str(count),
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
        "records": count,
        "sha256": digest.hexdigest(),
        "database": str(destination),
        "kind": kind,
    }


def _excerpt(content: str, query: str, limit: int = 1100) -> str:
    if len(content) <= limit:
        return content
    terms = [term.casefold() for term in re.findall(r"[^\W_]+", query)]
    lowered = content.casefold()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 5)
    end = min(len(content), start + limit)
    return ("…" if start else "") + content[start:end].strip() + (
        "…" if end < len(content) else ""
    )


class MorphologyIndex:
    def __init__(self, database: Path):
        self.database = database.resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(
                f"morphology index not found: {self.database}; "
                "run `lkt ingest-morphology` first"
            )
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def metadata(self) -> dict[str, str]:
        with closing(self._connect()) as connection:
            return dict(connection.execute("SELECT key, value FROM metadata"))

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT count(*) FROM records").fetchone()[0])

    def draw_unseen(
        self, seed: str, excluded_record_ids: Iterable[str]
    ) -> Evidence | None:
        """Draw one real, not-yet-published dictionary record.

        The randomized starting point keeps the idle carousel varied, while a
        circular scan makes every polished source record reachable and avoids
        inventing a root or affix merely to balance visible card counts.
        """

        metadata = self.metadata()
        excluded = {
            str(record_id).strip()
            for record_id in excluded_record_ids
            if str(record_id).strip()
        }
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM records ORDER BY source_page, row_id"
            ).fetchall()
        rows = [row for row in rows if not _is_index_only(str(row["content"]))]
        if not rows:
            return None
        material = f"{metadata.get('corpus_id', '')}\0{seed.strip().casefold()}"
        start = (
            int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")
            % len(rows)
        )
        for step in range(len(rows)):
            row = rows[(start + step) % len(rows)]
            if str(row["record_id"]) not in excluded:
                return self._evidence([row], str(row["headword"]), metadata)[0]
        return None

    @staticmethod
    def _evidence(
        rows: list[sqlite3.Row], query: str, metadata: dict[str, str]
    ) -> list[Evidence]:
        kind = metadata.get("kind", "morphology")
        return [
            Evidence(
                entry_id=str(row["record_id"]),
                headword=str(row["headword"]),
                section=f"{kind.title()} dictionary",
                date_label="",
                pages=(int(row["source_page"]),) if row["source_page"] else (),
                excerpt=_excerpt(str(row["content"]), query),
                corpus_id=metadata.get("corpus_id", f"english-{kind}-dictionary"),
                source_title=metadata.get(
                    "source_title", f"English {kind.title()} Dictionary"
                ),
                kind=f"morphology-{kind}",
                locator=(f"source page {row['source_page']}" if row["source_page"] else ""),
            )
            for row in rows
            if not _is_index_only(str(row["content"]))
        ]

    def exact(self, query: str, limit: int = 4) -> list[Evidence]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(limit, 8))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM records WHERE headword_key = ?
                   ORDER BY length(content) DESC, source_page, row_id""",
                (query.casefold(),),
            ).fetchall()
        rows = [row for row in rows if not _is_index_only(str(row["content"]))][
            :limit
        ]
        return self._evidence(rows, query, self.metadata())

    def search(self, query: str, limit: int = 4) -> list[Evidence]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(limit, 8))
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        with closing(self._connect()) as connection:
            exact = connection.execute(
                """SELECT * FROM records WHERE headword_key = ?
                   ORDER BY source_page, row_id""",
                (query.casefold(),),
            ).fetchall()
            exact = [
                row
                for row in exact
                if not _is_index_only(str(row["content"]))
            ][:limit]
            rows.extend(exact)
            seen.update(str(row["record_id"]) for row in exact)

            tokens = [
                token.replace('"', "")
                for token in re.findall(r"[^\W_]+", query, flags=re.UNICODE)
                if token
            ]
            if tokens and len(rows) < limit:
                expression = " OR ".join(f'"{token}"*' for token in tokens[:8])
                matches = connection.execute(
                    """SELECT records.* FROM records_fts
                       JOIN records ON records.row_id = records_fts.rowid
                       WHERE records_fts MATCH ?
                       ORDER BY bm25(records_fts, 8.0, 1.0), records.source_page
                       LIMIT ?""",
                    (expression, limit * 4),
                ).fetchall()
                for row in matches:
                    if _is_index_only(str(row["content"])):
                        continue
                    record_id = str(row["record_id"])
                    if record_id in seen:
                        continue
                    rows.append(row)
                    seen.add(record_id)
                    if len(rows) >= limit:
                        break

        return self._evidence(rows, query, self.metadata())
