from __future__ import annotations

import tempfile
import unittest
import sqlite3
import json
from contextlib import closing
from pathlib import Path

from lkt.store import CardStore, card_validation_errors


class StoreTests(unittest.TestCase):
    def test_mixed_script_arabic_blocks_card_publication(self) -> None:
        errors = card_validation_errors(
            {
                "card_id": "dirty-arabic",
                "mode": "knowledge",
                "query": "breakthrough",
                "title": "breakthrough",
                "grounded": True,
                "english": {"term": "breakthrough", "meaning": "an insight"},
                "japanese": {
                    "term": "発見",
                    "meaning": "重要な発見",
                    "ruby_tokens": [{"t": "発見", "r": "はっけん"}],
                },
                "chinese": {
                    "simplified": "突破",
                    "meaning": "重要的进展",
                    "ruby_tokens": [
                        {"t": "突", "r": "tū"},
                        {"t": "破", "r": "pò"},
                    ],
                },
                "extra_languages": {
                    "arabic": {
                        "term": "انBREAKTHROUGH",
                        "meaning": "إنجاز مهم",
                    }
                },
                "evidence": [{"entry_id": "sense-1", "corpus_id": "omw-en:2.0"}],
            }
        )
        self.assertIn("Arabic term contains mixed or non-Arabic script", errors)

    def test_word_card_does_not_inherit_the_word_origin_graph_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "cards.sqlite3"
            store = CardStore(database)
            payload = {
                "card_id": "word-inspection",
                "mode": "knowledge",
                "query": "inspection",
                "title": "inspection",
                "created_at": "now",
                "grounded": True,
                "english": {"term": "inspection", "meaning": "an official examination"},
                "japanese": {
                    "term": "\u5be9\u67fb",
                    "reading": "\u3057\u3093\u3055",
                    "meaning": "\u516c\u5f0f\u306a\u8abf\u67fb",
                    "ruby_tokens": [{"t": "\u5be9\u67fb", "r": "\u3057\u3093\u3055"}],
                },
                "chinese": {
                    "simplified": "\u68c0\u67e5",
                    "meaning": "\u6b63\u5f0f\u7684\u68c0\u67e5",
                    "ruby_tokens": [
                        {"t": "\u68c0", "r": "ji\u01cen"},
                        {"t": "\u67e5", "r": "ch\u00e1"},
                    ],
                },
                "evidence": [{"entry_id": "sense-1", "corpus_id": "omw-en:2.0"}],
                "extensions": {},
            }
            connection = sqlite3.connect(database)
            connection.execute(
                """INSERT INTO cards(
                       card_id, mode, query, title, created_at, payload,
                       status, revision_of, updated_at, validation_state,
                       validation_errors
                       ) VALUES (?, 'knowledge', 'inspection', 'inspection', 'now', ?,
                             'candidate', '', 'now', 'candidate', '[]')""",
                (payload["card_id"], json.dumps(payload, ensure_ascii=False)),
            )
            connection.commit()
            connection.close()
            store.publish(payload["card_id"], quality_score=0.9)
            self.assertEqual(store.recent(1)[0]["card_id"], payload["card_id"])

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
                    status, revision_of, updated_at, quality_score, review_note,
                    validation_state, validation_errors
                ) VALUES ('card-1', 'root', 'spect', 'SPECT', 'now', ?,
                          'active', '', 'now', NULL, '', 'accepted', '[]')""",
                ('{"card_id":"card-1","mode":"root"}',),
            )
            connection.commit()
            connection.close()
            self.assertEqual(len(store.recent()), 1)
            self.assertTrue(store.archive("card-1"))
            self.assertEqual(store.recent(), [])

    def test_complete_mode_migration_reads_only_selected_accepted_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            with closing(sqlite3.connect(database)) as connection:
                for card_id, mode, state in (
                    ("answer-1", "answer", "accepted"),
                    ("question-1", "question", "accepted"),
                    ("word-1", "knowledge", "accepted"),
                    ("answer-rejected", "answer", "rejected"),
                ):
                    payload = {"card_id": card_id, "mode": mode}
                    connection.execute(
                        """INSERT INTO cards(
                               card_id, mode, query, title, created_at, payload,
                               status, revision_of, updated_at, validation_state,
                               validation_errors
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, '[]')""",
                        (
                            card_id,
                            mode,
                            card_id,
                            card_id,
                            card_id,
                            json.dumps(payload),
                            "active" if state == "accepted" else "candidate",
                            card_id,
                            state,
                        ),
                    )
                connection.commit()

            migrated = store.accepted_for_modes(("answer", "question"))

            self.assertEqual(
                [card["card_id"] for card in migrated],
                ["answer-1", "question-1"],
            )
            self.assertEqual(
                [card["card_id"] for card in store.recent(1000, "question")],
                ["question-1"],
            )

    def test_established_card_is_reused_by_mode_and_normalized_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            payload = {
                "card_id": "inspection-card",
                "mode": "knowledge",
                "query": "inspection",
                "title": "inspection",
            }
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """INSERT INTO cards(
                        card_id, mode, query, title, created_at, payload,
                        status, revision_of, updated_at, quality_score, review_note,
                        validation_state, validation_errors
                    ) VALUES ('inspection-card', 'knowledge', 'inspection',
                              'inspection', 'now', ?, 'active', '', 'now',
                              0.95, 'reviewed', 'accepted', '[]')""",
                    (json.dumps(payload),),
                )
                connection.commit()
            self.assertEqual(
                store.find_active("knowledge", "  INSPECTION  ")["card_id"],
                "inspection-card",
            )
            self.assertIsNone(store.find_active("word", "inspection"))

    def test_revision_preserves_source_and_supersedes_old_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            connection = sqlite3.connect(database)
            payload = {
                "card_id": "card-1",
                "mode": "answer",
                "query": "What now?",
                "title": "Begin",
                "created_at": "now",
                "grounded": True,
                "evidence": [
                    {
                        "entry_id": "answer-1",
                        "corpus_id": "book-of-answers",
                        "pages": [58],
                    }
                ],
                "extensions": {},
            }
            connection.execute(
                """INSERT INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note,
                    validation_state, validation_errors
                ) VALUES ('card-1', 'answer', 'What now?', 'Begin', 'now', ?,
                          'active', '', 'now', NULL, '', 'accepted', '[]')""",
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

    def test_legacy_and_rejected_cards_never_enter_visible_carousels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            connection = sqlite3.connect(database)
            connection.execute(
                """INSERT INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note
                ) VALUES ('dirty-1', 'knowledge', 'word', 'Dirty', 'now', ?,
                          'active', '', 'now', NULL, '')""",
                ('{"card_id":"dirty-1","mode":"knowledge"}',),
            )
            connection.commit()
            connection.close()
            self.assertEqual(store.recent(), [])
            self.assertEqual(store.quarantine_unvalidated(), {"legacy-unreviewed": 1})
            self.assertEqual(store.recent(), [])

    def test_dirty_cleanup_is_backed_up_and_keeps_raw_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "cards.sqlite3"
            backup = Path(temp) / "backups" / "before-cleanup.sqlite3"
            store = CardStore(database)
            connection = sqlite3.connect(database)
            connection.execute(
                """INSERT INTO cards(
                    card_id, mode, query, title, created_at, payload,
                    status, revision_of, updated_at, quality_score, review_note
                ) VALUES ('dirty-1', 'knowledge', 'word', 'Dirty', 'now', ?,
                          'active', '', 'now', NULL, '')""",
                ('{"card_id":"dirty-1","mode":"knowledge"}',),
            )
            connection.commit()
            connection.close()
            run_id = store.start_preparation("root", "bad", "test")
            store.save_preparation_artifact(run_id, "bad-output", {"bad": True})
            store.finish_preparation(run_id, "failed", error="invalid")
            store.save_observation(
                "keep this", "raw answer", "test", {}, context_card_id="dirty-1"
            )

            result = store.purge_unvalidated(backup)
            self.assertTrue(backup.is_file())
            self.assertEqual(result["cards_removed"], 1)
            self.assertEqual(result["preparation_runs_removed"], 1)
            self.assertEqual(result["preparation_artifacts_removed"], 1)
            self.assertIsNone(store.get("dirty-1"))
            self.assertEqual(store.recent_observations(1)[0]["prompt"], "keep this")
            self.assertEqual(store.recent_observations(1)[0]["context_card_id"], "")
            with closing(sqlite3.connect(backup)) as restored:
                self.assertEqual(restored.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 1)

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
