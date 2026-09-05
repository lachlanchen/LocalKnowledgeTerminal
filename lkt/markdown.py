from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1"
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".obsidian",
        ".git",
        "__pycache__",
        "build",
        "dist",
        "generated",
        "node_modules",
    }
)
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
BLOCKQUOTE_PREFIX = re.compile(r"^[ \t]{0,3}>[ \t]?")
WIKILINK = re.compile(r"(?<!\\)\[\[([^\]\n]+?)\]\]")
CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL
);
CREATE TABLE sections (
    row_id INTEGER PRIMARY KEY,
    section_id TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL REFERENCES files(path),
    heading TEXT NOT NULL,
    heading_level INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    excerpt TEXT NOT NULL,
    search_text TEXT NOT NULL,
    source_sha256 TEXT NOT NULL
);
CREATE INDEX idx_markdown_sections_path ON sections(path, line_start);
CREATE TABLE wikilinks (
    source_section_id TEXT NOT NULL REFERENCES sections(section_id),
    target TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (source_section_id, target, label)
);
CREATE INDEX idx_markdown_wikilinks_target ON wikilinks(target);
CREATE VIRTUAL TABLE sections_fts USING fts5(
    heading,
    excerpt,
    search_text,
    content='sections',
    content_rowid='row_id',
    tokenize='unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True)
class MarkdownSource:
    relative_path: str
    absolute_path: Path
    source_sha256: str
    byte_count: int
    text: str


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _excluded_directory(name: str) -> bool:
    return name.startswith(".") or name.casefold() in EXCLUDED_DIRECTORIES


def markdown_paths(vault: Path) -> list[Path]:
    """Return stable, non-symlink Markdown paths contained by ``vault``."""

    vault = vault.resolve()
    if not vault.is_dir():
        raise NotADirectoryError(vault)
    paths: list[Path] = []
    for root, directories, filenames in os.walk(vault, topdown=True, followlinks=False):
        root_path = Path(root)
        directories[:] = sorted(
            directory
            for directory in directories
            if not _excluded_directory(directory)
            and not (root_path / directory).is_symlink()
        )
        for filename in sorted(filenames):
            path = root_path / filename
            if (
                filename.startswith(".")
                or path.suffix.casefold() != ".md"
                or path.is_symlink()
            ):
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(vault):
                continue
            paths.append(resolved)
    return sorted(paths, key=lambda path: path.relative_to(vault).as_posix())


def read_sources(vault: Path) -> list[MarkdownSource]:
    vault = vault.resolve()
    sources = []
    for path in markdown_paths(vault):
        payload = path.read_bytes()
        sources.append(
            MarkdownSource(
                relative_path=path.relative_to(vault).as_posix(),
                absolute_path=path,
                source_sha256=_sha256(payload),
                byte_count=len(payload),
                text=payload.decode("utf-8-sig"),
            )
        )
    return sources


def vault_fingerprint(sources: Iterable[MarkdownSource]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.source_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def external_destination(vault: Path, destination: Path, label: str) -> Path:
    """Resolve a generated destination and keep it outside a canonical vault."""

    vault = vault.resolve()
    destination = destination.resolve()
    if destination == vault or destination.is_relative_to(vault):
        raise ValueError(f"{label} must stay outside the canonical vault")
    return destination


@contextmanager
def _destination_lock(destination: Path):
    lock = destination.with_name(destination.name + ".lock")
    if lock.is_symlink():
        raise ValueError("Markdown index lock path must not be a symbolic link")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as error:
        if lock.is_symlink():
            raise ValueError(
                "Markdown index lock path must not be a symbolic link"
            ) from error
        raise
    lock_status = os.fstat(descriptor)
    if not stat.S_ISREG(lock_status.st_mode) or lock_status.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(
            "Markdown index lock path must be a singly linked regular file"
        )
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError(
                    f"Markdown index build already in progress for {destination}"
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    f"Markdown index build already in progress for {destination}"
                ) from error
        locked = True
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _container_line(line: str) -> tuple[int, str]:
    depth = 0
    while match := BLOCKQUOTE_PREFIX.match(line):
        line = line[match.end() :]
        depth += 1
    return depth, line


def _fence_transition(
    line: str, fence: tuple[str, int, int] | None
) -> tuple[tuple[str, int, int] | None, bool]:
    depth, content = _container_line(line)
    match = FENCE.match(content)
    if fence:
        if depth < fence[2]:
            fence = None
        elif match:
            delimiter, remainder = match.groups()
            if (
                depth == fence[2]
                and delimiter[0] == fence[0]
                and len(delimiter) >= fence[1]
                and not remainder.strip()
            ):
                return None, True
            return fence, True
        else:
            return fence, True
    if fence:
        return fence, True
    if not match:
        return None, False
    delimiter, remainder = match.groups()
    if delimiter[0] == "`" and "`" in remainder:
        return None, False
    return (delimiter[0], len(delimiter), depth), True


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    fence = None
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.removesuffix("\n").removesuffix("\r")
        fence, hidden = _fence_transition(line, fence)
        if hidden:
            continue
        match = HEADING.match(line)
        if match:
            headings.append((number, len(match.group(1)), match.group(2).strip()))
    return headings


def _sections(source: MarkdownSource) -> list[tuple[Any, ...]]:
    lines = source.text.splitlines(keepends=True)
    if not lines:
        return []
    headings = _headings(lines)
    starts = headings or [(1, 0, "(document)")]
    if headings and headings[0][0] > 1:
        starts = [(1, 0, "(document)"), *headings]
    rows = []
    for index, (line_start, level, heading) in enumerate(starts):
        line_end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        excerpt = "".join(lines[line_start - 1 : line_end])
        if not excerpt.strip():
            continue
        identity = f"{source.relative_path}\n{line_start}\n{heading}"
        section_id = f"markdown-{_sha256(identity.encode('utf-8'))[:20]}"
        rows.append(
            (
                section_id,
                source.relative_path,
                heading,
                level,
                line_start,
                line_end,
                excerpt,
                _search_text(excerpt),
                source.source_sha256,
            )
        )
    return rows


def _without_html_comments(text: str) -> str:
    visible = []
    cursor = 0
    in_comment = False
    while cursor < len(text):
        if in_comment:
            end = text.find("-->", cursor)
            if end < 0:
                break
            cursor = end + 3
            in_comment = False
            continue
        start = text.find("<!--", cursor)
        if start < 0:
            visible.append(text[cursor:])
            break
        visible.append(text[cursor:start])
        cursor = start + 4
        in_comment = True
    return "".join(visible)


def _without_inline_code(text: str) -> str:
    runs = list(re.finditer(r"`+", text))
    visible = []
    cursor = 0
    index = 0
    while index < len(runs):
        opener = runs[index]
        closer_index = next(
            (
                candidate
                for candidate in range(index + 1, len(runs))
                if len(runs[candidate].group()) == len(opener.group())
            ),
            None,
        )
        if closer_index is None:
            index += 1
            continue
        closer = runs[closer_index]
        visible.append(text[cursor : opener.start()])
        visible.append(" ")
        cursor = closer.end()
        index = closer_index + 1
    visible.append(text[cursor:])
    return "".join(visible)


def _wikilinks(section_id: str, excerpt: str) -> list[tuple[str, str, str]]:
    links = set()
    visible_lines = []
    fence = None
    for line in excerpt.splitlines():
        fence, hidden = _fence_transition(line, fence)
        if hidden:
            continue
        _, visible_line = _container_line(line)
        if not visible_line.startswith(("    ", "\t")):
            visible_lines.append(visible_line)
    visible_text = _without_html_comments("\n".join(visible_lines))
    visible_text = _without_inline_code(visible_text)
    for match in WIKILINK.finditer(visible_text):
        value = match.group(1).strip()
        target, separator, label = value.partition("|")
        target = target.strip()
        label = label.strip() if separator else target
        if target:
            links.add((section_id, target, label or target))
    return sorted(links, key=lambda item: (item[1].casefold(), item[2].casefold()))


def _cjk_tokens(value: str) -> list[str]:
    if len(value) == 1:
        return [f"u{value}"]
    return [f"b{value[index:index + 2]}" for index in range(len(value) - 1)]


def _search_text(value: str) -> str:
    tokens = []
    for run in CJK_RUN.findall(value):
        tokens.extend(f"u{character}" for character in run)
        tokens.extend(_cjk_tokens(run) if len(run) > 1 else [])
    return " ".join(tokens)


def build_markdown_index(vault: Path, destination: Path) -> dict[str, Any]:
    """Atomically build a disposable FTS index without modifying the vault."""

    vault = vault.resolve()
    destination = external_destination(vault, destination, "Markdown index destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _destination_lock(destination):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.building-",
            suffix=".sqlite3",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            sources = read_sources(vault)
            if not sources:
                raise ValueError("vault has no indexable Markdown files")
            fingerprint = vault_fingerprint(sources)
            section_count = 0
            link_count = 0
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(SCHEMA)
                for source in sources:
                    connection.execute(
                        "INSERT INTO files(path, source_sha256, byte_count) VALUES (?, ?, ?)",
                        (source.relative_path, source.source_sha256, source.byte_count),
                    )
                    for row in _sections(source):
                        connection.execute(
                            """
                            INSERT INTO sections(
                                section_id, path, heading, heading_level, line_start,
                                line_end, excerpt, search_text, source_sha256
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            row,
                        )
                        section_count += 1
                        links = _wikilinks(row[0], row[6])
                        connection.executemany(
                            """
                            INSERT INTO wikilinks(source_section_id, target, label)
                            VALUES (?, ?, ?)
                            """,
                            links,
                        )
                        link_count += len(links)
                connection.execute(
                    "INSERT INTO sections_fts(sections_fts) VALUES ('rebuild')"
                )
                metadata = {
                    "file_count": str(len(sources)),
                    "section_count": str(section_count),
                    "wikilink_count": str(link_count),
                    "schema_version": SCHEMA_VERSION,
                    "vault_fingerprint": fingerprint,
                }
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
                )
                connection.commit()
                quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                if quick_check != "ok":
                    raise sqlite3.DatabaseError(
                        f"markdown index quick_check failed: {quick_check}"
                    )
                current_sources = read_sources(vault)
                if vault_fingerprint(current_sources) != fingerprint:
                    raise RuntimeError(
                        "Markdown vault changed while its index was being built"
                    )
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return {
        "database": str(destination),
        "files": len(sources),
        "sections": section_count,
        "wikilinks": link_count,
        "vault_fingerprint": fingerprint,
    }


def _fts_query(query: str) -> str:
    terms = []
    cursor = 0
    for match in CJK_RUN.finditer(query):
        terms.extend(re.findall(r"[^\W_]+", query[cursor:match.start()], re.UNICODE))
        cjk = _cjk_tokens(match.group())
        terms.append(" ".join(cjk))
        cursor = match.end()
    terms.extend(re.findall(r"[^\W_]+", query[cursor:], re.UNICODE))
    if not terms:
        return ""
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


class MarkdownIndex:
    def __init__(self, database: Path):
        self.database = database.resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(
                f"Markdown index not found: {self.database}; run `lkt ingest-markdown` first"
            )
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def metadata(self) -> dict[str, str]:
        with closing(self._connect()) as connection:
            return dict(connection.execute("SELECT key, value FROM metadata"))

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT count(*) FROM sections").fetchone()[0])

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        expression = _fts_query(query.strip())
        if not expression:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT s.*, bm25(sections_fts) AS rank
                FROM sections_fts
                JOIN sections s ON s.row_id=sections_fts.rowid
                WHERE sections_fts MATCH ?
                ORDER BY rank, s.path, s.line_start
                LIMIT ?
                """,
                (expression, max(1, min(int(limit), 100))),
            ).fetchall()
            return [self._result(connection, row) for row in rows]

    def resolve(self, section_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT *, 0.0 AS rank FROM sections WHERE section_id=?",
                (section_id,),
            ).fetchone()
            return self._result(connection, row) if row else None

    @staticmethod
    def _result(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        links = [
            {"target": link[0], "label": link[1]}
            for link in connection.execute(
                """
                SELECT target, label FROM wikilinks
                WHERE source_section_id=? ORDER BY target, label
                """,
                (row["section_id"],),
            )
        ]
        return {
            "section_id": row["section_id"],
            "path": row["path"],
            "heading": row["heading"],
            "heading_level": row["heading_level"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "excerpt": row["excerpt"],
            "source_sha256": row["source_sha256"],
            "wikilinks": links,
        }

    def edges(self) -> list[dict[str, str]]:
        with closing(self._connect()) as connection:
            return [
                {
                    "source_section_id": row[0],
                    "target": row[1],
                    "label": row[2],
                }
                for row in connection.execute(
                    """
                    SELECT source_section_id, target, label FROM wikilinks
                    ORDER BY source_section_id, target, label
                    """
                )
            ]
