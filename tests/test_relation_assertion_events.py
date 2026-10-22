from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lkt.knowledge import KnowledgeStore


class RelationAssertionEventTests(unittest.TestCase):
    @staticmethod
    def entities(store: KnowledgeStore, name: str = "inspect") -> tuple[str, str, str]:
        return (
            store.upsert_term("en", f"{name}-subject"),
            store.upsert_morpheme("la", f"{name}-source", "root"),
            store.add_historical_form("la", f"{name}-target"),
        )

    @staticmethod
    def revision(store: KnowledgeStore) -> int:
        with closing(store._connect()) as connection:
            return int(connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'relation_graph_revision'"
            ).fetchone()[0])

    def test_baseline_is_exactly_once_and_disclaims_reconstructed_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject, source, target = self.entities(store)
            assertion_id = store.accept_relation_assertion(
                subject, source, target, "developed-into", confidence=0.61
            )
            with closing(store._connect()) as connection:
                connection.execute("DELETE FROM relation_assertion_events")
                connection.execute("UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'")
                connection.commit()
            KnowledgeStore(database)
            KnowledgeStore(database)
            with closing(store._connect()) as connection:
                rows = connection.execute(
                    "SELECT action, after_json, reason FROM relation_assertion_events WHERE assertion_id = ?",
                    (assertion_id,),
                ).fetchall()
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, "13")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action"], "baseline")
            self.assertEqual(json.loads(rows[0]["after_json"])["revision"], 1)
            self.assertIn("not reconstructed", rows[0]["reason"])

    def test_v7_migration_baseline_and_reopen_preserve_graph_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject, source, target = self.entities(store, "migration-noop")
            assertion_id = store.accept_relation_assertion(
                subject, source, target, "developed-into", confidence=0.57
            )
            revision = self.revision(store)
            with closing(store._connect()) as connection:
                # Simulate a schema-v7 live copy with an old derived display
                # mirror. The subtype row remains the canonical morphology fact.
                connection.execute(
                    "UPDATE entities SET payload = '{}' WHERE entity_id = ?", (source,)
                )
                connection.execute("DELETE FROM relation_assertion_events")
                connection.execute(
                    "UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'"
                )
                connection.commit()

            migrated = KnowledgeStore(database)
            self.assertEqual(self.revision(migrated), revision)
            with closing(migrated._connect()) as connection:
                baseline_count = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_events
                       WHERE assertion_id = ? AND action = 'baseline'""",
                    (assertion_id,),
                ).fetchone()[0]
                repaired_payload = json.loads(
                    connection.execute(
                        "SELECT payload FROM entities WHERE entity_id = ?", (source,)
                    ).fetchone()[0]
                )
            self.assertEqual(baseline_count, 1)
            self.assertEqual(
                (repaired_payload["language"], repaired_payload["kind"]),
                ("la", "root"),
            )

            reopened = KnowledgeStore(database)
            self.assertEqual(self.revision(reopened), revision)
            with closing(reopened._connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        """SELECT COUNT(*) FROM relation_assertion_events
                           WHERE assertion_id = ? AND action = 'baseline'""",
                        (assertion_id,),
                    ).fetchone()[0],
                    1,
                )

    def test_legacy_edge_remains_fallback_without_invented_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject, source, target = self.entities(store, "migration-change")
            store.add_edge(
                source,
                target,
                "developed-into",
                basis="model",
                confidence=0.63,
                properties={"term_id": subject},
            )
            revision = self.revision(store)
            with closing(store._connect()) as connection:
                connection.execute(
                    "UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'"
                )
                connection.commit()

            migrated = KnowledgeStore(database)
            self.assertEqual(self.revision(migrated), revision)
            with closing(migrated._connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        """SELECT COUNT(*) FROM relation_assertions
                           WHERE subject_entity_id = ? AND source_entity_id = ?
                             AND target_entity_id = ? AND relation = 'developed-into'""",
                        (subject, source, target),
                    ).fetchone()[0],
                    0,
                )

            reopened = KnowledgeStore(database)
            self.assertEqual(self.revision(reopened), revision)

    def test_identical_replay_is_true_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            ids = self.entities(store)
            store.apply_relation_decision(*ids, "developed-into", confidence=0.64)
            revision = self.revision(store)
            with closing(store._connect()) as connection:
                count = connection.execute("SELECT COUNT(*) FROM relation_assertion_events").fetchone()[0]
            replay = store.apply_relation_decision(*ids, "developed-into", confidence=0.64)
            with closing(store._connect()) as connection:
                replay_count = connection.execute("SELECT COUNT(*) FROM relation_assertion_events").fetchone()[0]
            self.assertEqual(replay["action"], "noop")
            self.assertFalse(replay["event_recorded"])
            self.assertEqual((replay_count, self.revision(store)), (count, revision))

    def test_model_to_book_upgrade_and_book_to_model_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            ids = self.entities(store)
            assertion_id = store.accept_relation_assertion(*ids, "developed-into", confidence=0.4)
            evidence_id = store.add_evidence("roots", "entry-inspect")
            upgrade = store.apply_relation_decision(
                *ids, "developed-into", basis="book", confidence=0.86,
                evidence_ids=(evidence_id,),
            )
            revision = self.revision(store)
            first_keep = store.apply_relation_decision(
                *ids, "developed-into", basis="model", confidence=0.99,
                properties={"warnings": ["model disagreement"]},
            )
            second_keep = store.apply_relation_decision(
                *ids, "developed-into", basis="model", confidence=0.99,
                properties={"warnings": ["model disagreement"]},
            )
            with closing(store._connect()) as connection:
                row = connection.execute(
                    "SELECT basis, confidence, revision FROM relation_assertions WHERE assertion_id = ?",
                    (assertion_id,),
                ).fetchone()
                exact = [row[0] for row in connection.execute(
                    "SELECT evidence_id FROM assertion_evidence WHERE assertion_id = ?", (assertion_id,)
                )]
                keeps = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertion_events WHERE assertion_id = ? AND action = 'keep'",
                    (assertion_id,),
                ).fetchone()[0]
            self.assertEqual(upgrade["action"], "update")
            self.assertEqual((first_keep["action"], second_keep["action"]), ("keep", "keep"))
            self.assertEqual((row["basis"], row["confidence"], row["revision"]), ("book", 0.86, 2))
            self.assertEqual(exact, [evidence_id])
            self.assertEqual(keeps, 1)
            self.assertEqual(self.revision(store), revision)

    def test_model_evidence_stays_context_and_same_basis_improves(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            ids = self.entities(store)
            accepted = store.apply_relation_decision(*ids, "associated-with", confidence=0.3)
            confidence_update = store.apply_relation_decision(*ids, "associated-with", confidence=0.6)
            context_id = store.add_evidence("origins", "context-entry")
            context_update = store.apply_relation_decision(
                *ids, "associated-with", confidence=0.6, evidence_ids=(context_id,),
                properties={"warnings": ["ambiguous direction retained"]},
            )
            with closing(store._connect()) as connection:
                exact_count = connection.execute(
                    "SELECT COUNT(*) FROM assertion_evidence WHERE assertion_id = ?",
                    (accepted["assertion_id"],),
                ).fetchone()[0]
                row = connection.execute(
                    "SELECT confidence, revision, properties_json FROM relation_assertions WHERE assertion_id = ?",
                    (accepted["assertion_id"],),
                ).fetchone()
                event = connection.execute(
                    """SELECT evidence_ids_json, context_evidence_ids_json
                       FROM relation_assertion_events WHERE event_id = ?""",
                    (context_update["event_id"],),
                ).fetchone()
            self.assertEqual((confidence_update["action"], context_update["action"]), ("update", "update"))
            self.assertEqual((row["confidence"], row["revision"], exact_count), (0.6, 3, 0))
            self.assertEqual(json.loads(row["properties_json"])["context_evidence_ids"], [context_id])
            self.assertEqual(json.loads(event["evidence_ids_json"]), [])
            self.assertEqual(json.loads(event["context_evidence_ids_json"]), [context_id])

    def test_archive_has_before_after_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            ids = self.entities(store)
            assertion_id = store.accept_relation_assertion(*ids, "developed-into")
            self.assertTrue(store.retire_relation_assertion(ids[0], assertion_id))
            with closing(store._connect()) as connection:
                event = connection.execute(
                    """SELECT before_json, after_json, input_fingerprint, output_fingerprint
                       FROM relation_assertion_events WHERE assertion_id = ? AND action = 'archive'""",
                    (assertion_id,),
                ).fetchone()
            self.assertEqual(json.loads(event["before_json"])["status"], "accepted")
            self.assertEqual(json.loads(event["after_json"])["status"], "archived")
            self.assertTrue(event["input_fingerprint"] and event["output_fingerprint"])

    def test_event_failure_rolls_back_assertion_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            ids = self.entities(store)
            evidence_id = store.add_evidence("origins", "rollback-context")
            revision = self.revision(store)
            with self.assertRaises(sqlite3.DatabaseError):
                store.apply_relation_decision(
                    *ids, "rollback-relation", context_evidence_ids=(evidence_id,),
                    job_id="missing-preparation-job",
                )
            with closing(store._connect()) as connection:
                assertion_count = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertions WHERE subject_entity_id = ? AND relation = 'rollback-relation'",
                    (ids[0],),
                ).fetchone()[0]
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertion_events WHERE assertion_id NOT IN (SELECT assertion_id FROM relation_assertions)"
                ).fetchone()[0]
            self.assertEqual((assertion_count, event_count), (0, 0))
            self.assertEqual(self.revision(store), revision)


if __name__ == "__main__":
    unittest.main()
