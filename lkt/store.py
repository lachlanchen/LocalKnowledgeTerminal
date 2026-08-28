from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Card


_VISIBLE_MODES = {"word", "knowledge", "answer", "question", "root", "affix"}
_MOJIBAKE_MARKERS = ("\ufffd", "Ã", "Â", "â€", "åŒ", "æ˜", "çš")
_HAN = re.compile(r"[\u3400-\u9fff]")


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _text_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in _text_values(item)]
    return []


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


def card_validation_errors(card: dict[str, Any]) -> list[str]:
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

    if mode in {"word", "root", "affix"}:
        extensions = card.get("extensions")
        graph = extensions.get("morphology_graph") if isinstance(extensions, dict) else None
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        edges = graph.get("edges") if isinstance(graph, dict) else None
        if not isinstance(nodes, list) or len(nodes) < 2:
            errors.append("origin graph has fewer than two nodes")
        if not isinstance(edges, list) or not edges:
            errors.append("origin graph has no relationships")
    return list(dict.fromkeys(errors))


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
        with closing(self._connect()) as connection:
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
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                )"""
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

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT payload FROM cards
                   WHERE status = 'active' AND validation_state = 'accepted'
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

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
        """Keep one accepted revision current for a mode/query collection key."""
        timestamp = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE cards SET status = 'superseded', updated_at = ?
                   WHERE mode = ? AND query = ? AND card_id <> ?
                     AND status = 'active' AND validation_state = 'accepted'""",
                (timestamp, mode.strip(), query.strip(), keep_card_id),
            )
            connection.commit()
        return int(cursor.rowcount)

    def get(self, card_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM cards WHERE card_id = ?", (card_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def start_preparation(self, mode: str, query: str, model: str) -> str:
        run_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO preparation_runs(
                    run_id, mode, query, model, status, created_at
                ) VALUES (?, ?, ?, ?, 'running', ?)""",
                (run_id, mode, query, model, created_at),
            )
            connection.commit()
        return run_id

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
