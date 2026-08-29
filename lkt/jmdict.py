from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE readings (
    row_id INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL,
    form TEXT NOT NULL,
    form_key TEXT NOT NULL,
    reading TEXT NOT NULL,
    common INTEGER NOT NULL,
    glosses TEXT NOT NULL,
    parts_of_speech TEXT NOT NULL,
    UNIQUE(entry_id, form, reading)
);
CREATE INDEX idx_jmdict_form ON readings(form_key, common DESC, entry_id);
"""


def _key(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sense_values(
    senses: list[dict[str, Any]], form: str, reading: str
) -> tuple[list[str], list[str]]:
    glosses: list[str] = []
    parts: list[str] = []
    for sense in senses:
        kanji_scope = _strings(sense.get("appliesToKanji"))
        kana_scope = _strings(sense.get("appliesToKana"))
        if kanji_scope and "*" not in kanji_scope and form not in kanji_scope:
            continue
        if kana_scope and "*" not in kana_scope and reading not in kana_scope:
            continue
        for gloss in sense.get("gloss", []):
            if not isinstance(gloss, dict) or gloss.get("lang") != "eng":
                continue
            text = str(gloss.get("text", "")).strip()
            if text and text not in glosses:
                glosses.append(text)
        for part in _strings(sense.get("partOfSpeech")):
            if part not in parts:
                parts.append(part)
    return glosses[:12], parts[:8]


def _reading_rows(words: Iterable[Any]) -> Iterable[tuple[Any, ...]]:
    for word in words:
        if not isinstance(word, dict):
            continue
        entry_id = str(word.get("id", "")).strip()
        kana = [item for item in word.get("kana", []) if isinstance(item, dict)]
        kanji = [item for item in word.get("kanji", []) if isinstance(item, dict)]
        senses = [item for item in word.get("sense", []) if isinstance(item, dict)]
        if not entry_id or not kana:
            continue
        forms = kanji or kana
        for form_item in forms:
            form = _key(form_item.get("text"))
            if not form:
                continue
            for kana_item in kana:
                reading = _key(kana_item.get("text"))
                applies = _strings(kana_item.get("appliesToKanji"))
                if not reading:
                    continue
                if kanji and applies and "*" not in applies and form not in applies:
                    continue
                if not kanji and reading != form:
                    continue
                glosses, parts = _sense_values(senses, form, reading)
                yield (
                    entry_id,
                    form,
                    _key(form),
                    reading,
                    int(bool(form_item.get("common") or kana_item.get("common"))),
                    json.dumps(glosses, ensure_ascii=False),
                    json.dumps(parts, ensure_ascii=False),
                )


def build_jmdict_index(
    source: Path,
    destination: Path,
    *,
    release: str = "",
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Build a compact exact Japanese form/reading index from pinned JSON."""

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    digest = _sha256_file(source)
    with source.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("words"), list):
        raise ValueError("JMdict JSON has no words array")

    version = str(payload.get("version", "")).strip()
    dictionary_date = str(payload.get("dictDate", "")).strip()
    release = release.strip() or "+".join(item for item in (version, dictionary_date) if item)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".building")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    count = 0
    try:
        connection.executescript(SCHEMA)
        batch: list[tuple[Any, ...]] = []
        for row in _reading_rows(payload["words"]):
            batch.append(row)
            if len(batch) >= 1000:
                connection.executemany(
                    """INSERT OR IGNORE INTO readings(
                           entry_id, form, form_key, reading, common, glosses,
                           parts_of_speech
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    batch,
                )
                count += len(batch)
                batch.clear()
                if progress:
                    progress(count)
        if batch:
            connection.executemany(
                """INSERT OR IGNORE INTO readings(
                       entry_id, form, form_key, reading, common, glosses,
                       parts_of_speech
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            count += len(batch)
        stored_count = int(connection.execute("SELECT count(*) FROM readings").fetchone()[0])
        metadata = {
            "corpus_id": f"jmdict:{release}",
            "source_title": "JMdict Japanese-English dictionary",
            "release": release,
            "dictionary_date": dictionary_date,
            "format_version": version,
            "common_only": str(bool(payload.get("commonOnly"))).lower(),
            "source_name": source.name,
            "source_sha256": digest,
            "reading_count": str(stored_count),
            "schema_version": "1",
            "license": "EDRDG JMdict / CC BY-SA 4.0 distribution",
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
        "readings": stored_count,
        "processed_rows": count,
        "release": release,
        "sha256": digest,
        "database": str(destination),
    }


class JapaneseReadingIndex:
    def __init__(self, database: Path):
        self.database = database.resolve()
        self._metadata: dict[str, str] | None = None

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(
                f"JMdict index not found: {self.database}; run `lkt ingest-jmdict` first"
            )
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def metadata(self) -> dict[str, str]:
        if self._metadata is None:
            with closing(self._connect()) as connection:
                self._metadata = dict(
                    connection.execute("SELECT key, value FROM metadata")
                )
        return dict(self._metadata)

    def status(self) -> dict[str, Any]:
        metadata = self.metadata()
        return {
            "ready": True,
            "database": str(self.database),
            "release": metadata.get("release", ""),
            "dictionary_date": metadata.get("dictionary_date", ""),
            "common_only": metadata.get("common_only", "false") == "true",
            "readings": int(metadata.get("reading_count", "0")),
            "sha256": metadata.get("source_sha256", ""),
        }

    def lookup(self, form: str, limit: int = 16) -> list[dict[str, Any]]:
        form = _key(form)
        if not form:
            return []
        limit = max(1, min(int(limit), 32))
        metadata = self.metadata()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM readings WHERE form_key = ?
                   ORDER BY common DESC, entry_id, reading LIMIT ?""",
                (_key(form), limit),
            ).fetchall()
        return [
            {
                "entry_id": f"{row['entry_id']}:{row['reading']}",
                "jmdict_entry_id": str(row["entry_id"]),
                "form": str(row["form"]),
                "reading": str(row["reading"]),
                "common": bool(row["common"]),
                "glosses": json.loads(row["glosses"]),
                "parts_of_speech": json.loads(row["parts_of_speech"]),
                "corpus_id": metadata.get("corpus_id", "jmdict"),
                "source_title": metadata.get("source_title", "JMdict"),
                "source_hash": metadata.get("source_sha256", ""),
                "locator": f"JMdict entry {row['entry_id']}",
                "kind": "japanese-reading",
            }
            for row in rows
        ]
