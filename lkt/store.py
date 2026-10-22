from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Card
from .pronunciation import is_arabic_script_text


_VISIBLE_MODES = {"word", "knowledge", "answer", "question", "root", "affix"}
_GRAPH_MODES_SQL = "'word', 'root', 'affix'"
_ROTATION_ELIGIBILITY_SQL = f"""
    CASE
        WHEN NOT json_valid(payload) THEN 0
        WHEN mode NOT IN ({_GRAPH_MODES_SQL}) THEN 1
        ELSE NOT (
            COALESCE(
                json_type(
                    payload,
                    '$.extensions.lexical_view.projection_contract_version'
                ) IN ('integer', 'real', 'text'),
                0
            )
            AND json_type(payload, '$.extensions.lexical_view.nodes') = 'array'
            AND json_array_length(
                json_extract(payload, '$.extensions.lexical_view.nodes')
            ) <= 1
            AND json_type(payload, '$.extensions.lexical_view.edges') = 'array'
            AND json_array_length(
                json_extract(payload, '$.extensions.lexical_view.edges')
            ) = 0
        )
    END
"""
_MOJIBAKE_MARKERS = ("\ufffd", "Ã", "Â", "â€", "åŒ", "æ˜", "çš")
_HAN = re.compile(r"[\u3400-\u9fff]")


def _process_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is a POSIX liveness probe, but on Windows signal
        # zero is ``CTRL_C_EVENT``.  Calling it from preparation recovery can
        # therefore interrupt the worker's entire console process group (and
        # the terminal that launched it).  Query the kernel's immutable process
        # creation time instead; this also protects against PID reuse.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            get_process_times = kernel32.GetProcessTimes
            get_process_times.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            get_process_times.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL

            handle = open_process(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ""
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not get_process_times(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return ""
                creation_ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return f"{pid}:{creation_ticks}"
            finally:
                close_handle(handle)
        except (AttributeError, OSError, OverflowError, ValueError):
            return ""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = stat[stat.rfind(")") + 2 :].split()
        return f"{pid}:{tail[19]}"
    except (IndexError, OSError):
        try:
            os.kill(pid, 0)
        except (OSError, OverflowError):
            return ""
        return str(pid)


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _text_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in _text_values(item)]
    return []


def _normalized_collection_query(value: Any) -> str:
    """Return the stable subject key used to compare card revisions."""

    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _card_branch_key(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        return ""
    return str(extensions.get("branch_focus_id", "")).strip().casefold()


def _ruby_covers(term: str, tokens: Any) -> bool:
    if not _HAN.search(term):
        return True
    if not isinstance(tokens, list) or not tokens:
        return False
    visible = "".join(
        str(item.get("t", "")) for item in tokens if isinstance(item, dict)
    )
    if re.sub(r"\s+", "", visible) != re.sub(r"\s+", "", term):
        return False
    return all(
        not _HAN.search(str(item.get("t", ""))) or bool(str(item.get("r", "")).strip())
        for item in tokens
        if isinstance(item, dict)
    )


def card_validation_errors(
    card: dict[str, Any], *, strict_annotations: bool = False
) -> list[str]:
    """Return publication blockers for a visible product card.

    This gate intentionally checks provenance and presentation integrity. Factual
    claims still move through the atomic knowledge/revision workflow before a
    composed card reaches this function.
    """

    errors: list[str] = []
    mode = str(card.get("mode", "")).strip()
    if mode not in _VISIBLE_MODES:
        errors.append("unknown card mode")
    for field in ("card_id", "query", "title"):
        if not str(card.get(field, "")).strip():
            errors.append(f"missing {field}")
    if card.get("grounded") is not True:
        errors.append("card is not grounded")
    evidence = card.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("missing RAG evidence")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict) or not str(item.get("entry_id", "")).strip():
                errors.append(f"evidence {index + 1} has no stable entry id")
            if not isinstance(item, dict) or not str(item.get("corpus_id", "")).strip():
                errors.append(f"evidence {index + 1} has no corpus id")

    visible_text = "\n".join(_text_values(card))
    if any(marker in visible_text for marker in _MOJIBAKE_MARKERS):
        errors.append("text contains encoding damage")

    english = card.get("english") if isinstance(card.get("english"), dict) else {}
    japanese = card.get("japanese") if isinstance(card.get("japanese"), dict) else {}
    chinese = card.get("chinese") if isinstance(card.get("chinese"), dict) else {}
    if mode in {"word", "knowledge", "root", "affix"}:
        languages = card.get("languages")
        if isinstance(languages, dict) and languages:
            usable = {
                str(code): atom
                for code, atom in languages.items()
                if isinstance(atom, dict)
                and str(atom.get("term", "")).strip()
                and str(atom.get("meaning", "")).strip()
            }
            if not usable:
                errors.append("no usable language atom has both term and meaning")
            japanese_atom = usable.get("ja")
            if japanese_atom is not None and not _ruby_covers(
                str(japanese_atom.get("term", "")),
                japanese_atom.get("ruby_tokens"),
            ):
                errors.append("Japanese ruby does not cover every kanji")
            chinese_atom = usable.get("zh")
            if chinese_atom is not None and not _ruby_covers(
                str(chinese_atom.get("term", "")),
                chinese_atom.get("ruby_tokens"),
            ):
                errors.append("Chinese ruby does not cover every Han character")
        else:
            if not str(english.get("term", "")).strip() or not str(
                english.get("meaning", "")
            ).strip():
                errors.append("English term or meaning is missing")
            japanese_term = str(japanese.get("term", "")).strip()
            chinese_term = str(chinese.get("simplified", "")).strip()
            if not japanese_term or not str(japanese.get("meaning", "")).strip():
                errors.append("Japanese term or meaning is missing")
            elif not _ruby_covers(japanese_term, japanese.get("ruby_tokens")):
                errors.append("Japanese ruby does not cover every kanji")
            if not chinese_term or not str(chinese.get("meaning", "")).strip():
                errors.append("Chinese term or meaning is missing")
            elif not _ruby_covers(chinese_term, chinese.get("ruby_tokens")):
                errors.append("Chinese ruby does not cover every Han character")
        extra_languages = card.get("extra_languages")
        arabic = (
            extra_languages.get("arabic")
            if isinstance(extra_languages, dict)
            else None
        )
        if isinstance(languages, dict) and isinstance(languages.get("ar"), dict):
            arabic = languages["ar"]
        if isinstance(arabic, dict) and str(arabic.get("term", "")).strip():
            if not is_arabic_script_text(str(arabic.get("term", ""))):
                errors.append("Arabic term contains mixed or non-Arabic script")
            if not is_arabic_script_text(str(arabic.get("meaning", ""))):
                errors.append("Arabic meaning contains mixed or non-Arabic script")

    if mode in {"word", "root", "affix"}:
        extensions = card.get("extensions")
        graph = extensions.get("morphology_graph") if isinstance(extensions, dict) else None
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        edges = graph.get("edges") if isinstance(graph, dict) else None
        # Accepted graph corrections can legitimately withdraw the last
        # relationship. Preserve the word/meaning as an explicit pending
        # projection, not a fabricated graph. Rotation excludes this state
        # until supported relationships are available again.
        pending_origin = (
            mode == "word"
            and isinstance(extensions, dict)
            and extensions.get("knowledge_policy") == "accepted-atomic-origin-view"
            and isinstance(graph, dict)
            and graph.get("status") == "awaiting-enrichment"
            and isinstance(nodes, list)
            and len(nodes) == 1
            and isinstance(nodes[0], dict)
            and bool(str(graph.get("center_id", "")).strip())
            and nodes[0].get("id") == graph.get("center_id")
            and nodes[0].get("type") == "word"
            and str(nodes[0].get("form", "")).strip().casefold()
                == str(card.get("query", "")).strip().casefold()
            and bool(str(nodes[0].get("meaning", "")).strip())
            and isinstance(edges, list)
            and not edges
        )
        if not pending_origin:
            if not isinstance(nodes, list) or len(nodes) < 2:
                errors.append("origin graph has fewer than two nodes")
            if not isinstance(edges, list) or not edges:
                errors.append("origin graph has no relationships")
    if not strict_annotations:
        errors = [error for error in errors if error not in _ANNOTATION_WARNINGS]
    return list(dict.fromkeys(errors))


_ANNOTATION_WARNINGS = frozenset({
    "Japanese ruby does not cover every kanji",
    "Chinese ruby does not cover every Han character",
})


def card_annotation_warnings(card: dict[str, Any]) -> list[str]:
    """Retain pronunciation gaps as diagnostics, not publication blockers."""
    return [
        error for error in card_validation_errors(card, strict_annotations=True)
        if error in _ANNOTATION_WARNINGS
    ]


def _merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


class CardStore:
    def __init__(self, database: Path):
        self.database = database.resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        # A first deployment may need to scan a multi-gigabyte legacy cards
        # file while building additive indexes. Web and worker can start at the
        # same time, so let the second constructor wait for that one-time
        # migration instead of failing after the normal ten-second query limit.
        with closing(sqlite3.connect(self.database, timeout=300)) as connection:
            connection.execute("PRAGMA busy_timeout = 300000")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS cards (
                    card_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    query TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cards_created ON cards(created_at DESC)"
            )
            card_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(cards)")
            }
            card_migrations = {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "revision_of": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
                "quality_score": "REAL",
                "review_note": "TEXT NOT NULL DEFAULT ''",
                "validation_state": "TEXT NOT NULL DEFAULT 'legacy-unreviewed'",
                "validation_errors": "TEXT NOT NULL DEFAULT '[]'",
            }
            for column, definition in card_migrations.items():
                if column not in card_columns:
                    connection.execute(
                        f"ALTER TABLE cards ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cards_status_created "
                "ON cards(status, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cards_publication "
                "ON cards(status, validation_state, mode, created_at DESC)"
            )
            connection.execute(
                f"""CREATE INDEX IF NOT EXISTS idx_cards_rotation
                    ON cards(mode, created_at DESC, card_id)
                    WHERE status = 'active' AND validation_state = 'accepted'
                      AND {_ROTATION_ELIGIBILITY_SQL}"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS observations (
                    observation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    model TEXT NOT NULL,
                    grounded INTEGER NOT NULL,
                    context_card_id TEXT NOT NULL DEFAULT '',
                    metrics TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            observation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(observations)")
            }
            if "context_card_id" not in observation_columns:
                connection.execute(
                    "ALTER TABLE observations ADD COLUMN "
                    "context_card_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_created "
                "ON observations(created_at DESC)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS preparation_runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    query TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    card_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    owner_identity TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            preparation_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(preparation_runs)")
            }
            if "owner_pid" not in preparation_columns:
                connection.execute(
                    "ALTER TABLE preparation_runs ADD COLUMN "
                    "owner_pid INTEGER NOT NULL DEFAULT 0"
                )
            if "owner_identity" not in preparation_columns:
                connection.execute(
                    "ALTER TABLE preparation_runs ADD COLUMN "
                    "owner_identity TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS preparation_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    reusable INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES preparation_runs(run_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_preparation_artifacts_run "
                "ON preparation_artifacts(run_id, created_at)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=10)

    def save(self, card: Card) -> None:
        """Persist a candidate without making it visible in a carousel."""

        warnings = card_annotation_warnings(card.to_dict())
        if warnings:
            card.extensions["annotation_warnings"] = warnings
        else:
            card.extensions.pop("annotation_warnings", None)
        payload = json.dumps(card.to_dict(), ensure_ascii=False)
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note,
                    validation_state, validation_errors
                ) VALUES (?, ?, ?, ?, ?, ?, 'candidate', '', ?, NULL, '',
                          'candidate', '[]')""",
                (
                    card.card_id,
                    card.mode,
                    card.query,
                    card.title,
                    card.created_at,
                    payload,
                    card.created_at,
                ),
            )
            connection.commit()

    def publish(
        self,
        card_id: str,
        *,
        quality_score: float | None = None,
        review_note: str = "validated LLM + RAG card",
    ) -> dict[str, Any]:
        if quality_score is not None:
            quality_score = float(quality_score)
            if not 0 <= quality_score <= 1:
                raise ValueError("quality_score must be between 0 and 1")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM cards WHERE card_id = ?", (card_id,)
            ).fetchone()
            if row is None:
                raise KeyError(card_id)
            payload = json.loads(row[0])
            errors = card_validation_errors(payload)
            timestamp = datetime.now(UTC).isoformat()
            connection.execute(
                """UPDATE cards
                   SET status = ?, validation_state = ?, validation_errors = ?,
                       quality_score = ?, review_note = ?, updated_at = ?
                   WHERE card_id = ?""",
                (
                    "candidate" if errors else "active",
                    "rejected" if errors else "accepted",
                    json.dumps(errors, ensure_ascii=False),
                    quality_score,
                    review_note.strip()[:1000],
                    timestamp,
                    card_id,
                ),
            )
            connection.commit()
        if errors:
            raise ValueError("card failed publication: " + "; ".join(errors))
        return payload

    def recent(self, limit: int = 20, mode: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        selected_mode = mode.strip() if mode.strip() in _VISIBLE_MODES else ""
        with closing(self._connect()) as connection:
            if selected_mode:
                rows = connection.execute(
                    """SELECT payload FROM cards
                       WHERE status = 'active' AND validation_state = 'accepted'
                         AND mode = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (selected_mode, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT payload FROM cards
                       WHERE status = 'active' AND validation_state = 'accepted'
                       ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recent_summaries(
        self, limit: int = 64, mode: str = "", offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return a bounded carousel page without decoding full card payloads."""

        limit = max(1, min(int(limit), 128))
        offset = max(0, int(offset))
        selected_mode = mode.strip() if mode.strip() in _VISIBLE_MODES else ""
        where = (
            "status = 'active' AND validation_state = 'accepted'"
            + f" AND {_ROTATION_ELIGIBILITY_SQL}"
            + (" AND mode = ?" if selected_mode else "")
        )
        parameters: tuple[Any, ...] = (
            (selected_mode, limit, offset)
            if selected_mode
            else (limit, offset)
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT card_id, mode, query, title, created_at, updated_at
                    FROM cards WHERE {where}
                    ORDER BY created_at DESC, card_id
                    LIMIT ? OFFSET ?""",
                parameters,
            ).fetchall()
        return [
            {
                "card_id": str(row[0]),
                "mode": str(row[1]),
                "query": str(row[2]),
                "title": str(row[3]),
                "created_at": str(row[4]),
                "updated_at": str(row[5]),
                "summary_only": True,
            }
            for row in rows
        ]

    def accepted_deck_info(self, mode: str = "") -> dict[str, Any]:
        """Return cheap revision metadata for one accepted carousel."""

        selected_mode = mode.strip() if mode.strip() in _VISIBLE_MODES else ""
        where = (
            "status = 'active' AND validation_state = 'accepted'"
            + f" AND {_ROTATION_ELIGIBILITY_SQL}"
            + (" AND mode = ?" if selected_mode else "")
        )
        parameters = (selected_mode,) if selected_mode else ()
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""SELECT COUNT(*),
                           COALESCE(MAX(CASE WHEN updated_at = ''
                                             THEN created_at ELSE updated_at END), '')
                    FROM cards WHERE {where}""",
                parameters,
            ).fetchone()
        total = int(row[0]) if row else 0
        latest = str(row[1]) if row else ""
        return {"total": total, "revision": f"{total}:{latest}"}

    def get_accepted(self, card_id: str) -> dict[str, Any] | None:
        """Read one public card without exposing candidates or archived revisions."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT payload FROM cards
                   WHERE card_id = ? AND status = 'active'
                     AND validation_state = 'accepted'""",
                (card_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_published_revision(self, card_id: str) -> dict[str, Any] | None:
        """Resolve a shared link after a newer accepted revision replaces it.

        Withdrawn/archived cards and unaccepted candidates remain private.
        This does not republish a revision or add it back to rotation.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT payload FROM cards
                   WHERE card_id = ? AND status IN ('active', 'superseded')
                     AND validation_state = 'accepted'""",
                (card_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def random_accepted(
        self, mode: str, exclude_card_ids: tuple[str, ...] = ()
    ) -> dict[str, Any] | None:
        """Sample the complete accepted mode while avoiding a short recent window."""

        selected_mode = mode.strip()
        if selected_mode not in _VISIBLE_MODES:
            return None
        excluded = tuple(
            dict.fromkeys(
                str(card_id).strip()[:100]
                for card_id in exclude_card_ids[:24]
                if str(card_id).strip()
            )
        )

        def select(connection: sqlite3.Connection, omitted: tuple[str, ...]):
            exclusion = (
                f" AND card_id NOT IN ({','.join('?' for _ in omitted)})"
                if omitted
                else ""
            )
            parameters = (selected_mode, *omitted)
            total_row = connection.execute(
                f"""SELECT COUNT(*) FROM cards
                   WHERE status = 'active' AND validation_state = 'accepted'
                     AND mode = ? AND {_ROTATION_ELIGIBILITY_SQL}"""
                + exclusion,
                parameters,
            ).fetchone()
            total = int(total_row[0]) if total_row else 0
            if not total:
                return None
            offset = uuid.uuid4().int % total
            return connection.execute(
                f"""SELECT payload FROM cards
                   WHERE status = 'active' AND validation_state = 'accepted'
                     AND mode = ? AND {_ROTATION_ELIGIBILITY_SQL}"""
                + exclusion
                + " ORDER BY created_at DESC, card_id LIMIT 1 OFFSET ?",
                (*parameters, offset),
            ).fetchone()

        with closing(self._connect()) as connection:
            row = select(connection, excluded)
            if row is None and excluded:
                row = select(connection, ())
        return json.loads(row[0]) if row else None

    def accepted_for_modes(self, modes: tuple[str, ...]) -> list[dict[str, Any]]:
        """Return the complete accepted collection for an explicit migration.

        Unlike ``recent``, this intentionally has no UI-oriented limit. It is
        used by bounded maintenance commands that need to migrate every
        already-published card in the selected modes.
        """

        selected = tuple(
            dict.fromkeys(mode.strip() for mode in modes if mode.strip() in _VISIBLE_MODES)
        )
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT payload FROM cards
                    WHERE status = 'active' AND validation_state = 'accepted'
                      AND mode IN ({placeholders})
                    ORDER BY created_at""",
                selected,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def backfill_morphology_branch_cards(self) -> dict[str, int]:
        """Split accepted legacy morphology cards into persistent branch cards."""

        created = 0
        superseded = 0
        timestamp = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT card_id, mode, query, created_at, payload,
                          quality_score, review_note
                   FROM cards
                   WHERE status = 'active' AND validation_state = 'accepted'
                     AND mode IN ('root', 'affix')
                   ORDER BY created_at, card_id"""
            ).fetchall()
            for row in rows:
                source_id, mode, query, created_at = map(str, row[:4])
                payload = json.loads(row[4])
                if _card_branch_key(payload):
                    continue
                extensions = payload.get("extensions")
                graph = (
                    extensions.get("morphology_graph")
                    if isinstance(extensions, dict)
                    else None
                )
                focuses = graph.get("focus_areas") if isinstance(graph, dict) else None
                nodes = graph.get("nodes") if isinstance(graph, dict) else None
                if not isinstance(focuses, list) or not isinstance(nodes, list):
                    continue
                branch_kinds = {"root"} if mode == "root" else {"prefix", "suffix"}
                branches = [
                    focus
                    for focus in focuses
                    if isinstance(focus, dict)
                    and str(focus.get("kind", "")) in branch_kinds
                ]
                if len(branches) < 2:
                    continue
                overview = next(
                    (
                        focus
                        for focus in focuses
                        if isinstance(focus, dict)
                        and str(focus.get("kind", "")) == "overview"
                    ),
                    None,
                )
                node_by_id = {
                    str(node.get("id", "")): node
                    for node in nodes
                    if isinstance(node, dict) and str(node.get("id", ""))
                }
                branch_payloads: list[tuple[str, dict[str, Any]]] = []
                for focus in branches:
                    focus_id = str(focus.get("id", "")).strip()
                    focus_kind = str(focus.get("kind", "")).strip()
                    focus_node_ids = [
                        str(node_id) for node_id in focus.get("node_ids", [])
                    ]
                    component_id = next(
                        (
                            node_id
                            for node_id in focus_node_ids
                            if str(node_by_id.get(node_id, {}).get("type", ""))
                            == focus_kind
                        ),
                        "",
                    )
                    if not focus_id or not component_id:
                        branch_payloads = []
                        break
                    branch = json.loads(json.dumps(payload, ensure_ascii=False))
                    branch_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"lkt:morphology-branch:{source_id}:{focus_id}",
                        )
                    )
                    branch["card_id"] = branch_id
                    branch["title"] = str(
                        focus.get("headline") or focus.get("label") or branch["title"]
                    )
                    branch_extensions = branch["extensions"]
                    branch_extensions["branch_focus_id"] = focus_id
                    branch_graph = branch_extensions["morphology_graph"]
                    branch_graph["center_id"] = component_id
                    branch_graph["focus_areas"] = [
                        focus,
                        *([overview] if isinstance(overview, dict) else []),
                    ]
                    lexical_view = branch_extensions.get("lexical_view")
                    if isinstance(lexical_view, dict):
                        lexical_view["focus_entity_ids"] = [component_id]
                    if card_validation_errors(branch):
                        branch_payloads = []
                        break
                    branch_payloads.append((branch_id, branch))
                if len(branch_payloads) != len(branches):
                    continue
                for branch_id, branch in branch_payloads:
                    connection.execute(
                        """INSERT OR REPLACE INTO cards(
                            card_id, mode, query, title, created_at, payload,
                            status, revision_of, updated_at, quality_score,
                            review_note, validation_state, validation_errors
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?,
                                  'accepted', '[]')""",
                        (
                            branch_id,
                            mode,
                            query,
                            str(branch["title"]),
                            created_at,
                            json.dumps(branch, ensure_ascii=False),
                            source_id,
                            timestamp,
                            row[5],
                            str(row[6]),
                        ),
                    )
                    created += 1
                connection.execute(
                    "UPDATE cards SET status = 'superseded', updated_at = ? "
                    "WHERE card_id = ?",
                    (timestamp, source_id),
                )
                superseded += 1
            connection.commit()
        return {"created": created, "superseded": superseded}

    def find_active(self, mode: str, query: str) -> dict[str, Any] | None:
        """Return established card knowledge before scheduling new inference."""

        mode, query = mode.strip(), query.strip()
        if not mode or not query:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT payload FROM cards
                   WHERE mode = ? AND query = ? COLLATE NOCASE
                     AND status = 'active' AND validation_state = 'accepted'
                   ORDER BY
                     CASE WHEN updated_at = '' THEN created_at ELSE updated_at END DESC,
                     created_at DESC
                   LIMIT 1""",
                (mode, query),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def supersede_others(self, mode: str, query: str, keep_card_id: str) -> int:
        """Keep one accepted revision current for its persisted collection key.

        ``mode`` and ``query`` remain in the public signature for existing callers,
        but they are only hints.  The kept card is authoritative so a stale caller
        cannot supersede a different card mode that happens to share its subject.
        """

        del mode, query
        timestamp = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            kept = connection.execute(
                "SELECT mode, query, payload FROM cards WHERE card_id = ?",
                (keep_card_id,),
            ).fetchone()
            if kept is None:
                raise KeyError(keep_card_id)
            kept_mode = str(kept[0]).strip()
            kept_query = _normalized_collection_query(kept[1])
            kept_branch = _card_branch_key(json.loads(kept[2]))
            superseded_ids = [
                str(row[0])
                for row in connection.execute(
                    """SELECT card_id, query, payload FROM cards
                       WHERE mode = ? AND card_id <> ?
                         AND status = 'active' AND validation_state = 'accepted'""",
                    (kept_mode, keep_card_id),
                )
                if _normalized_collection_query(row[1]) == kept_query
                and (
                    not kept_branch
                    or not _card_branch_key(json.loads(row[2]))
                    or _card_branch_key(json.loads(row[2])) == kept_branch
                )
            ]
            if superseded_ids:
                placeholders = ",".join("?" for _ in superseded_ids)
                cursor = connection.execute(
                    f"""UPDATE cards SET status = 'superseded', updated_at = ?
                        WHERE card_id IN ({placeholders})""",
                    (timestamp, *superseded_ids),
                )
            else:
                cursor = None
            connection.commit()
        return int(cursor.rowcount) if cursor is not None else 0

    def archive_obsolete_morphology_branches(
        self,
        mode: str,
        query: str,
        kept_branch_keys: Iterable[str],
        *,
        authoritative: bool = False,
        allow_empty_authoritative: bool = False,
    ) -> dict[str, Any]:
        """Archive branches absent from one complete Root/Affix recomposition.

        The caller must explicitly assert that kept_branch_keys is the complete
        authoritative branch set. Branchless or malformed legacy rows are
        deliberately preserved because their identity cannot be compared safely.
        """

        selected_mode = str(mode).strip().casefold()
        subject = _normalized_collection_query(query)
        invalid_key_collection = isinstance(kept_branch_keys, (str, bytes))
        if invalid_key_collection:
            raw_keys: tuple[str, ...] = ()
        else:
            raw_keys = tuple(str(item).strip() for item in kept_branch_keys)
        normalized_keys = tuple(
            dict.fromkeys(key.casefold() for key in raw_keys if key)
        )
        result = {
            "archived_count": 0,
            "archived_ids": [],
            "mode": selected_mode,
            "subject": subject,
            "kept_branch_keys": list(normalized_keys),
        }
        if (
            authoritative is not True
            or selected_mode not in {"root", "affix"}
            or not subject
            or invalid_key_collection
            or any(not key for key in raw_keys)
            or (
                not normalized_keys
                and allow_empty_authoritative is not True
            )
        ):
            return result

        kept = set(normalized_keys)
        timestamp = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidates = connection.execute(
                """SELECT card_id, query, payload
                   FROM cards
                   WHERE mode = ? AND status = 'active'
                     AND validation_state = 'accepted'
                   ORDER BY card_id""",
                (selected_mode,),
            ).fetchall()
            obsolete_ids: list[str] = []
            for card_id, stored_query, raw_payload in candidates:
                if _normalized_collection_query(stored_query) != subject:
                    continue
                try:
                    payload = json.loads(raw_payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                branch_key = _card_branch_key(payload)
                if not branch_key or branch_key in kept:
                    continue
                obsolete_ids.append(str(card_id))
            if obsolete_ids:
                placeholders = ",".join("?" for _ in obsolete_ids)
                cursor = connection.execute(
                    f"""UPDATE cards
                        SET status = 'archived', updated_at = ?
                        WHERE status = 'active'
                          AND validation_state = 'accepted'
                          AND card_id IN ({placeholders})""",
                    (timestamp, *obsolete_ids),
                )
                if int(cursor.rowcount) != len(obsolete_ids):
                    connection.rollback()
                    raise RuntimeError(
                        "obsolete branch archive changed during its transaction"
                    )
            connection.commit()
        return {
            **result,
            "archived_count": len(obsolete_ids),
            "archived_ids": obsolete_ids,
        }

    def get(self, card_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM cards WHERE card_id = ?", (card_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def start_preparation(self, mode: str, query: str, model: str) -> str:
        run_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        owner_pid = os.getpid()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO preparation_runs(
                    run_id, mode, query, model, status, owner_pid,
                    owner_identity, created_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
                (
                    run_id,
                    mode,
                    query,
                    model,
                    owner_pid,
                    _process_identity(owner_pid),
                    created_at,
                ),
            )
            connection.commit()
        return run_id

    def recover_interrupted_preparations(self) -> int:
        """Fail only running preparations whose exact owner process is gone."""

        timestamp = datetime.now(UTC).isoformat()
        recovered: list[str] = []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT run_id, owner_pid, owner_identity
                   FROM preparation_runs WHERE status = 'running'"""
            ).fetchall()
            for run_id, owner_pid, owner_identity in rows:
                if (
                    int(owner_pid) > 0
                    and str(owner_identity)
                    and _process_identity(int(owner_pid)) == str(owner_identity)
                ):
                    continue
                recovered.append(str(run_id))
            if recovered:
                placeholders = ",".join("?" for _ in recovered)
                connection.execute(
                    f"""UPDATE preparation_runs
                        SET status = 'failed',
                            error = 'owner process interrupted before completion',
                            finished_at = ?
                        WHERE run_id IN ({placeholders})""",
                    (timestamp, *recovered),
                )
                connection.commit()
        return len(recovered)

    def save_preparation_artifact(
        self,
        run_id: str,
        stage: str,
        payload: Any,
        reusable: bool = True,
    ) -> str:
        artifact_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO preparation_artifacts(
                    artifact_id, run_id, stage, payload, reusable, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    run_id,
                    stage,
                    json.dumps(payload, ensure_ascii=False),
                    int(reusable),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        return artifact_id

    def finish_preparation(
        self,
        run_id: str,
        status: str,
        card_id: str = "",
        error: str = "",
    ) -> None:
        if status not in {"complete", "failed"}:
            raise ValueError("preparation status must be complete or failed")
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE preparation_runs
                   SET status = ?, card_id = ?, error = ?, finished_at = ?
                   WHERE run_id = ?""",
                (
                    status,
                    card_id,
                    error[:1000],
                    datetime.now(UTC).isoformat(),
                    run_id,
                ),
            )
            connection.commit()

    def preparation_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT artifact_id, stage, payload, reusable, created_at
                   FROM preparation_artifacts WHERE run_id = ?
                   ORDER BY created_at""",
                (run_id,),
            ).fetchall()
        return [
            {
                "artifact_id": row[0],
                "stage": row[1],
                "payload": json.loads(row[2]),
                "reusable": bool(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]

    def reusable_preparation_artifact(
        self,
        mode: str,
        query: str,
        model: str,
        stage: str,
        evidence_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Reuse one accepted inference stage after a later stage failed."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT artifact.artifact_id, artifact.run_id,
                          artifact.payload, artifact.created_at
                   FROM preparation_artifacts AS artifact
                   JOIN preparation_runs AS run ON run.run_id = artifact.run_id
                   WHERE run.mode = ? AND run.query = ? COLLATE NOCASE
                     AND run.model = ? AND artifact.stage = ?
                     AND artifact.reusable = 1
                   ORDER BY artifact.created_at DESC LIMIT 20""",
                (mode.strip(), query.strip(), model.strip(), stage.strip()),
            ).fetchall()
        for row in rows:
            payload = json.loads(row[2])
            if not isinstance(payload, dict):
                continue
            if payload.get("evidence_fingerprint") != evidence_fingerprint:
                continue
            if not isinstance(payload.get("value"), dict):
                continue
            return {
                "artifact_id": row[0],
                "run_id": row[1],
                "payload": payload,
                "created_at": row[3],
            }
        return None

    def archive(self, card_id: str) -> bool:
        with closing(self._connect()) as connection:
            result = connection.execute(
                "UPDATE cards SET status = 'archived', updated_at = ? WHERE card_id = ?",
                (datetime.now(UTC).isoformat(), card_id),
            )
            connection.commit()
        return result.rowcount == 1

    def quarantine_unvalidated(self) -> dict[str, int]:
        """Archive non-visible legacy/candidate rows without deleting their audit trail."""

        timestamp = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            counts = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    """SELECT validation_state, COUNT(*) FROM cards
                       WHERE status IN ('active', 'candidate')
                         AND validation_state <> 'accepted'
                       GROUP BY validation_state"""
                )
            }
            connection.execute(
                """UPDATE cards SET status = 'archived', updated_at = ?
                   WHERE status IN ('active', 'candidate')
                     AND validation_state <> 'accepted'""",
                (timestamp,),
            )
            connection.commit()
        return counts

    def purge_unvalidated(self, backup_path: Path) -> dict[str, int | str]:
        """Back up the ledger, then remove rejected/legacy card material."""

        backup_path = backup_path.resolve()
        if backup_path == self.database:
            raise ValueError("backup path must differ from the card database")
        if backup_path.exists():
            raise FileExistsError(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as source, closing(
            sqlite3.connect(backup_path, timeout=10)
        ) as destination:
            source.backup(destination)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            dirty_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT card_id FROM cards WHERE validation_state <> 'accepted'"
                )
            ]
            placeholders = ",".join("?" for _ in dirty_ids)
            linked_runs: list[str] = []
            if dirty_ids:
                linked_runs = [
                    str(row[0])
                    for row in connection.execute(
                        f"""SELECT run_id FROM preparation_runs
                            WHERE card_id IN ({placeholders}) OR status = 'failed'""",
                        dirty_ids,
                    )
                ]
                connection.execute(
                    f"UPDATE observations SET context_card_id = '' "
                    f"WHERE context_card_id IN ({placeholders})",
                    dirty_ids,
                )
            else:
                linked_runs = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT run_id FROM preparation_runs WHERE status = 'failed'"
                    )
                ]
            artifact_count = 0
            if linked_runs:
                run_placeholders = ",".join("?" for _ in linked_runs)
                artifact_count = int(
                    connection.execute(
                        f"""SELECT COUNT(*) FROM preparation_artifacts
                            WHERE run_id IN ({run_placeholders})""",
                        linked_runs,
                    ).fetchone()[0]
                )
                connection.execute(
                    f"DELETE FROM preparation_artifacts "
                    f"WHERE run_id IN ({run_placeholders})",
                    linked_runs,
                )
                connection.execute(
                    f"DELETE FROM preparation_runs WHERE run_id IN ({run_placeholders})",
                    linked_runs,
                )
            if dirty_ids:
                connection.execute(
                    f"DELETE FROM cards WHERE card_id IN ({placeholders})", dirty_ids
                )
            connection.commit()
        return {
            "backup": str(backup_path),
            "cards_removed": len(dirty_ids),
            "preparation_runs_removed": len(linked_runs),
            "preparation_artifacts_removed": artifact_count,
        }

    def revise(
        self,
        card_id: str,
        patch: dict[str, Any],
        review_note: str = "",
        quality_score: float | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "title",
            "subtitle",
            "summary_en",
            "origin_story",
            "key_points",
            "english",
            "japanese",
            "chinese",
            "memory_hook",
            "related_terms",
            "extensions",
            "origin_graph",
            "extra_languages",
        }
        safe_patch = {key: value for key, value in patch.items() if key in allowed}
        if not safe_patch:
            raise ValueError("revision patch has no editable card fields")
        if quality_score is not None:
            quality_score = float(quality_score)
            if not 0 <= quality_score <= 1:
                raise ValueError("quality_score must be between 0 and 1")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM cards WHERE card_id = ?", (card_id,)
            ).fetchone()
            if not row:
                raise KeyError(card_id)
            original = json.loads(row[0])
            revised = _merge_dict(original, safe_patch)
            revised_id = str(uuid.uuid4())
            revised_at = datetime.now(UTC).isoformat()
            revised["card_id"] = revised_id
            revised["created_at"] = revised_at
            extensions = revised.get("extensions")
            extensions = dict(extensions) if isinstance(extensions, dict) else {}
            extensions["revision_of"] = card_id
            if review_note.strip():
                extensions["review_note"] = review_note.strip()[:1000]
            revised["extensions"] = extensions
            connection.execute(
                """INSERT INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note,
                    validation_state, validation_errors
                ) VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?,
                          'candidate', '[]')""",
                (
                    revised_id,
                    str(revised.get("mode", original.get("mode", ""))),
                    str(revised.get("query", original.get("query", ""))),
                    str(revised.get("title", original.get("title", ""))),
                    revised_at,
                    json.dumps(revised, ensure_ascii=False),
                    card_id,
                    revised_at,
                    quality_score,
                    review_note.strip()[:1000],
                ),
            )
            connection.execute(
                "UPDATE cards SET status = 'superseded', updated_at = ? "
                "WHERE card_id = ?",
                (revised_at, card_id),
            )
            connection.commit()
        self.publish(
            revised_id,
            quality_score=quality_score,
            review_note=review_note or "reviewed revision",
        )
        return revised

    def save_observation(
        self,
        prompt: str,
        response: str,
        model: str,
        metrics: dict[str, Any],
        context_card_id: str = "",
    ) -> dict[str, Any]:
        observation = {
            "observation_id": str(uuid.uuid4()),
            "kind": "raw-chat",
            "prompt": prompt,
            "response": response,
            "model": model,
            "grounded": False,
            "context_card_id": context_card_id,
            "metrics": metrics,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO observations(
                    observation_id, kind, prompt, response, model,
                    grounded, context_card_id, metrics, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation["observation_id"],
                    observation["kind"],
                    observation["prompt"],
                    observation["response"],
                    observation["model"],
                    0,
                    observation["context_card_id"],
                    json.dumps(metrics, ensure_ascii=False),
                    observation["created_at"],
                ),
            )
            connection.commit()
        return observation

    def recent_observations(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT observation_id, kind, prompt, response, model,
                          grounded, context_card_id, metrics, created_at
                   FROM observations ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "observation_id": row[0],
                "kind": row[1],
                "prompt": row[2],
                "response": row[3],
                "model": row[4],
                "grounded": bool(row[5]),
                "context_card_id": row[6],
                "metrics": json.loads(row[7]),
                "created_at": row[8],
            }
            for row in rows
        ]
