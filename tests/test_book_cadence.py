from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from lkt.book_cadence import BOOK_CADENCE_KEY, OccasionalBookSeeder


class _Store:
    def __init__(self, path: Path):
        self.path = path
        connection = self._connect()
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT)")
        connection.commit()
        connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class BookCadenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = _Store(Path(self.directory.name) / "state.sqlite3")
        self.now = 1000.0
        self.book = Mock()
        self.book.modes = ("answer", "question")
        self.book.run_mode.side_effect = lambda mode: {
            "status": "prepared", "mode": mode, "card_id": mode + "-card",
        }

    def seeder(self):
        return OccasionalBookSeeder(
            self.book, self.store, interval_seconds=120, clock=lambda: self.now,
        )

    def test_books_alternate_with_persistent_cooldown(self):
        self.assertEqual(self.seeder().run_once()["mode"], "answer")
        self.assertEqual(self.seeder().run_once()["status"], "waiting")
        self.now += 120
        self.assertEqual(self.seeder().run_once()["mode"], "question")
        self.now += 120
        self.assertEqual(self.seeder().run_once()["mode"], "answer")
        self.assertEqual([call.args[0] for call in self.book.run_mode.call_args_list],
                         ["answer", "question", "answer"])

    def test_failed_book_does_not_block_other_book_or_remove_cooldown(self):
        self.book.run_mode.side_effect = RuntimeError("weak local draft")
        result = self.seeder().run_once()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["mode"], "answer")
        self.assertEqual(self.seeder().run_once()["status"], "waiting")
        self.now += 120
        self.book.run_mode.side_effect = lambda mode: {"status": "prepared"}
        self.assertEqual(self.seeder().run_once()["mode"], "question")

    def test_cooldown_starts_after_model_finishes_and_database_is_unlocked(self):
        def generate(mode):
            connection = self.store._connect()
            connection.execute("INSERT INTO schema_meta VALUES ('during-model','ok')")
            connection.commit()
            connection.close()
            self.now += 500
            return {"status": "prepared", "mode": mode}
        self.book.run_mode.side_effect = generate
        result = self.seeder().run_once()
        self.assertEqual(result["next_due_at"], 1620)
        self.assertEqual(self.seeder().run_once()["status"], "waiting")

    def test_completed_book_still_yields_next_opportunity_to_other_book(self):
        self.book.run_mode.side_effect = lambda mode: {"status": "complete"}
        self.assertEqual(self.seeder().run_once()["mode"], "answer")
        self.now += 120
        self.assertEqual(self.seeder().run_once()["mode"], "question")

    def test_interrupted_attempt_preserves_mode_rotation(self):
        connection = self.store._connect()
        connection.execute("INSERT INTO schema_meta VALUES (?,?)", (
            BOOK_CADENCE_KEY, json.dumps({
                "last_mode": "answer", "next_due_at": 1100, "status": "running",
            }),
        ))
        connection.commit()
        connection.close()
        self.assertEqual(self.seeder().run_once()["status"], "waiting")
        self.now = 1100
        self.assertEqual(self.seeder().run_once()["mode"], "question")

    def test_single_configured_book_is_supported(self):
        self.book.modes = ("question",)
        self.assertEqual(self.seeder().run_once()["mode"], "question")
        self.now += 120
        self.assertEqual(self.seeder().run_once()["mode"], "question")


if __name__ == "__main__":
    unittest.main()
