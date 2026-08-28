from __future__ import annotations

import tempfile
import unittest
import sqlite3
import json
from pathlib import Path

from lkt.store import CardStore


class StoreTests(unittest.TestCase):
    def test_preparation_artifacts_survive_as_reusable_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = CardStore(Path(temp) / "knowledge.sqlite3")
            run_id = store.start_preparation("root", "inspection", "qwen-test")
            store.save_preparation_artifact(
                run_id,
                "retrieved-evidence",
                [{"entry_id": "root-1", "excerpt": "spect means look"}],
            )
            store.save_preparation_artifact(
                run_id,
                "cleaned-model-draft",
                {"center": "inspection", "nodes": [{"id": "center"}]},
            )
            store.finish_preparation(run_id, "complete", "card-1")
            artifacts = store.preparation_artifacts(run_id)
            self.assertEqual(
                [artifact["stage"] for artifact in artifacts],
                ["retrieved-evidence", "cleaned-model-draft"],
            )
            self.assertTrue(all(artifact["reusable"] for artifact in artifacts))

    def test_archive_removes_card_from_active_carousel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            connection = sqlite3.connect(database)
            connection.execute(
                """INSERT INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note
                ) VALUES ('card-1', 'root', 'spect', 'SPECT', 'now', ?,
                          'active', '', 'now', NULL, '')""",
                ('{"card_id":"card-1","mode":"root"}',),
            )
            connection.commit()
            connection.close()
            self.assertEqual(len(store.recent()), 1)
            self.assertTrue(store.archive("card-1"))
            self.assertEqual(store.recent(), [])

    def test_revision_preserves_source_and_supersedes_old_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            connection = sqlite3.connect(database)
            payload = {
                "card_id": "card-1",
                "mode": "root",
                "query": "spect",
                "title": "SPECT",
                "created_at": "now",
                "evidence": [{"entry_id": "root-1", "pages": [58]}],
                "extensions": {"morphology_graph": {"nodes": []}},
            }
            connection.execute(
                """INSERT INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note
                ) VALUES ('card-1', 'root', 'spect', 'SPECT', 'now', ?,
                          'active', '', 'now', NULL, '')""",
                (json.dumps(payload),),
            )
            connection.commit()
            connection.close()
            revised = store.revise(
                "card-1",
                {"summary_en": "to look or see"},
                review_note="reviewed wording",
                quality_score=0.9,
            )
            self.assertNotEqual(revised["card_id"], "card-1")
            self.assertEqual(revised["evidence"], payload["evidence"])
            self.assertEqual(revised["extensions"]["revision_of"], "card-1")
            self.assertEqual([card["card_id"] for card in store.recent()], [revised["card_id"]])

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
