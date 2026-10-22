from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lkt.knowledge import KnowledgeStore


class RelationEdgeAuthorityTests(unittest.TestCase):
    @staticmethod
    def revision(store: KnowledgeStore) -> int:
        with closing(store._connect()) as connection:
            return int(connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'relation_graph_revision'"
            ).fetchone()[0])

    @staticmethod
    def component_entities(store: KnowledgeStore, stem: str) -> tuple[str, str, str]:
        return (
            store.upsert_term("en", stem),
            store.upsert_morpheme("la", f"{stem}-old", "root"),
            store.upsert_morpheme("la", f"{stem}-new", "root"),
        )

    def test_retired_assertion_archives_mirror_and_cannot_be_revived_by_add_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, source, target = self.component_entities(store, "retire-authority")
            assertion_id = store.accept_relation_assertion(
                subject, source, target, "developed-into", confidence=0.66
            )
            accepted_revision = self.revision(store)
            with closing(store._connect()) as connection:
                accepted_edge = connection.execute(
                    """SELECT status, basis, confidence FROM entity_edges
                       WHERE source_entity_id = ? AND target_entity_id = ?
                         AND relation = 'developed-into'""",
                    (source, target),
                ).fetchone()
            self.assertEqual(
                (accepted_edge["status"], accepted_edge["basis"], accepted_edge["confidence"]),
                ("accepted", "model", 0.66),
            )

            self.assertTrue(store.retire_relation_assertion(subject, assertion_id))
            self.assertEqual(self.revision(store), accepted_revision + 1)
            store.add_edge(source, target, "developed-into", basis="book", confidence=1.0)
            self.assertEqual(self.revision(store), accepted_revision + 1)
            with closing(store._connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        """SELECT status FROM entity_edges
                           WHERE source_entity_id = ? AND target_entity_id = ?
                             AND relation = 'developed-into'""",
                        (source, target),
                    ).fetchone()[0],
                    "archived",
                )

    def test_supersession_updates_edge_and_primary_slot_once_and_projection_excludes_loser(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, old_root, new_root = self.component_entities(store, "slot-authority")
            incumbent = store.apply_relation_decision(
                subject, subject, old_root, "has-component",
                confidence=0.50, properties={"ordinal": 0, "surface": "old"},
            )
            revision = self.revision(store)
            evidence = store.add_evidence("roots", "slot-authority-entry")
            winner = store.apply_relation_decision(
                subject, subject, new_root, "has-component",
                basis="book", confidence=0.88, evidence_ids=(evidence,),
                properties={"ordinal": 0, "surface": "new"},
            )
            self.assertEqual(winner["slot_decision"], "supersede")
            self.assertEqual(self.revision(store), revision + 1)
            with closing(store._connect()) as connection:
                statuses = {
                    row["assertion_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE subject_entity_id = ? AND relation = 'has-component'""",
                        (subject,),
                    )
                }
                edge_statuses = {
                    row["target_entity_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT target_entity_id, status FROM entity_edges
                           WHERE source_entity_id = ? AND relation = 'has-component'""",
                        (subject,),
                    )
                }
                primary = connection.execute(
                    """SELECT morpheme_id, surface, basis FROM term_morphemes
                       WHERE term_id = ? AND ordinal = 0""",
                    (subject,),
                ).fetchone()
                archive_events = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_events
                       WHERE assertion_id = ? AND action = 'archive'""",
                    (incumbent["assertion_id"],),
                ).fetchone()[0]
            self.assertEqual(statuses[incumbent["assertion_id"]], "archived")
            self.assertEqual(statuses[winner["assertion_id"]], "accepted")
            self.assertEqual(edge_statuses, {old_root: "archived", new_root: "accepted"})
            self.assertEqual(
                (primary["morpheme_id"], primary["surface"], primary["basis"]),
                (new_root, "new", "book"),
            )
            self.assertEqual(archive_events, 1)
            graph = store.lexical_subgraph(
                subject, "word", {"nodes": 16, "edges": 24, "depth": 4}
            )
            self.assertNotIn(old_root, {edge["target"] for edge in graph["edges"]})
            self.assertIn(new_root, {edge["target"] for edge in graph["edges"]})

    def test_startup_reconciliation_repairs_only_assertion_controlled_mirror_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject, source, target = self.component_entities(store, "startup-authority")
            assertion_id = store.accept_relation_assertion(
                subject, source, target, "developed-into", confidence=0.71
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE entity_edges SET status = 'archived', confidence = 0.1
                       WHERE source_entity_id = ? AND target_entity_id = ?
                         AND relation = 'developed-into'""",
                    (source, target),
                )
                connection.commit()
            revision = self.revision(store)
            repaired = KnowledgeStore(database)
            self.assertEqual(self.revision(repaired), revision + 1)
            with closing(repaired._connect()) as connection:
                edge = connection.execute(
                    """SELECT status, confidence FROM entity_edges
                       WHERE source_entity_id = ? AND target_entity_id = ?
                         AND relation = 'developed-into'""",
                    (source, target),
                ).fetchone()
                slot_count = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertion_slots WHERE assertion_id = ?",
                    (assertion_id,),
                ).fetchone()[0]
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertion_events WHERE assertion_id = ?",
                    (assertion_id,),
                ).fetchone()[0]
            self.assertEqual((edge["status"], edge["confidence"]), ("accepted", 0.71))
            self.assertEqual((slot_count, event_count), (1, 1))
            reopened = KnowledgeStore(database)
            self.assertEqual(self.revision(reopened), revision + 1)

    def test_edge_sync_failure_rolls_back_assertion_slot_event_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, _, target = self.component_entities(store, "rollback-authority")
            revision = self.revision(store)
            with patch.object(
                store,
                "_sync_relation_authority",
                side_effect=sqlite3.DatabaseError("forced edge sync failure"),
            ):
                with self.assertRaises(sqlite3.DatabaseError):
                    store.apply_relation_decision(
                        subject, subject, target, "has-component",
                        properties={"ordinal": 0},
                    )
            with closing(store._connect()) as connection:
                counts = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "relation_assertions",
                        "relation_assertion_slots",
                        "relation_assertion_events",
                        "entity_edges",
                        "term_morphemes",
                    )
                )
            self.assertEqual(counts, (0, 0, 0, 0, 0))
            self.assertEqual(self.revision(store), revision)


if __name__ == "__main__":
    unittest.main()
