from __future__ import annotations

import json
import sqlite3
from contextlib import closing
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
