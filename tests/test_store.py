from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from lkt.store import CardStore


class StoreTests(unittest.TestCase):
    def test_raw_model_observations_are_persistent_and_marked_uncited(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            saved = store.save_observation(
                "Explain RAG",
                "Retrieval-augmented generation uses retrieved context.",
                "test-qwen",
                {"elapsed_seconds": 2.0, "completion_tokens": 8},
                context_card_id="card-123",
            )
            restored = CardStore(database).recent_observations(1)[0]
            self.assertEqual(restored["observation_id"], saved["observation_id"])
            self.assertEqual(restored["prompt"], "Explain RAG")
            self.assertFalse(restored["grounded"])
            self.assertEqual(restored["context_card_id"], "card-123")
            self.assertEqual(restored["metrics"]["completion_tokens"], 8)

    def test_existing_observation_table_gains_card_context_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "old.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """CREATE TABLE observations (
                    observation_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    prompt TEXT NOT NULL, response TEXT NOT NULL,
                    model TEXT NOT NULL, grounded INTEGER NOT NULL,
                    metrics TEXT NOT NULL, created_at TEXT NOT NULL
                )"""
            )
            connection.commit()
            connection.close()
            CardStore(database)
            connection = sqlite3.connect(database)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(observations)")
            }
            connection.close()
            self.assertIn("context_card_id", columns)


if __name__ == "__main__":
    unittest.main()
