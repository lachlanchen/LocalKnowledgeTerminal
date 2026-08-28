from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Card


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
        payload = json.dumps(card.to_dict(), ensure_ascii=False)
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', '', ?, NULL, '')""",
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

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT payload FROM cards
                   WHERE status = 'active'
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

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
                    status, revision_of, updated_at, quality_score, review_note
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
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
