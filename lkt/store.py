from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Card


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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS observations (
                    observation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    model TEXT NOT NULL,
                    grounded INTEGER NOT NULL,
                    metrics TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_created "
                "ON observations(created_at DESC)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=10)

    def save(self, card: Card) -> None:
        payload = json.dumps(card.to_dict(), ensure_ascii=False)
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO cards
                   (card_id, mode, query, title, created_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (card.card_id, card.mode, card.query, card.title, card.created_at, payload),
            )
            connection.commit()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM cards ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_observation(
        self,
        prompt: str,
        response: str,
        model: str,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        observation = {
            "observation_id": str(uuid.uuid4()),
            "kind": "raw-chat",
            "prompt": prompt,
            "response": response,
            "model": model,
            "grounded": False,
            "metrics": metrics,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO observations(
                    observation_id, kind, prompt, response, model,
                    grounded, metrics, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation["observation_id"],
                    observation["kind"],
                    observation["prompt"],
                    observation["response"],
                    observation["model"],
                    0,
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
                          grounded, metrics, created_at
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
                "metrics": json.loads(row[6]),
                "created_at": row[7],
            }
            for row in rows
        ]
