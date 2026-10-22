"""Occasional, restart-safe book generation beside continuous graph work."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from typing import Any

from .knowledge import KnowledgeStore


BOOK_CADENCE_KEY = "occasional_book_growth_v1"


class OccasionalBookSeeder:
    """Alternate the separate book decks without requiring an empty graph queue.

    The serial worker calls this only between jobs. Persist the cooldown and
    attempted mode before inference so a restart or weak draft cannot repeatedly
    favor the same book. Corpus selection and publication stay with the seeder.
    """

    def __init__(
        self,
        book: Any,
        store: KnowledgeStore,
        *,
        interval_seconds: float = 1800,
        clock: Callable[[], float] = time.time,
    ):
        self.book = book
        self.store = store
        self.interval_seconds = max(60.0, min(float(interval_seconds), 86_400.0))
        self.clock = clock
        self.modes = tuple(book.modes)
        if not self.modes:
            raise ValueError("occasional book growth requires at least one mode")

    @staticmethod
    def _save(connection: Any, state: dict[str, Any]) -> None:
        connection.execute(
            """INSERT INTO schema_meta(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (BOOK_CADENCE_KEY, json.dumps(state, sort_keys=True)),
        )

    def run_once(self) -> dict[str, Any]:
        now = self.clock()
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key=?", (BOOK_CADENCE_KEY,)
            ).fetchone()
            try:
                state = json.loads(row["value"]) if row else {}
                if not isinstance(state, dict):
                    state = {}
                next_due = float(state.get("next_due_at", 0))
            except (TypeError, ValueError):
                state, next_due = {}, 0.0
            if now < next_due:
                connection.commit()
                return {
                    "event": "occasional-book-growth",
                    "status": "waiting",
                    "next_due_at": next_due,
                    "last_mode": state.get("last_mode", ""),
                }
            last_mode = state.get("last_mode", "")
            index = self.modes.index(last_mode) + 1 if last_mode in self.modes else 0
            mode = self.modes[index % len(self.modes)]
            state = {
                "last_mode": mode,
                "started_at": now,
                "next_due_at": now + self.interval_seconds,
                "status": "running",
            }
            self._save(connection, state)
            connection.commit()

        # Never hold a SQLite write lock across a local-model request.
        try:
            result = self.book.run_mode(mode)
            payload = dict(result) if isinstance(result, Mapping) else result.to_dict()
        except Exception as exc:
            payload = {"status": "deferred", "message": str(exc)[:300]}
        payload["mode"] = str(payload.get("mode") or mode)
        finished = self.clock()
        state.update(
            finished_at=finished,
            next_due_at=finished + self.interval_seconds,
            status=str(payload.get("status", "deferred")),
            card_id=str(payload.get("card_id", "")),
        )
        with closing(self.store._connect()) as connection:
            self._save(connection, state)
            connection.commit()
        return {
            **payload,
            "event": "occasional-book-growth",
            "next_due_at": state["next_due_at"],
        }
