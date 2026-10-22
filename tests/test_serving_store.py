from __future__ import annotations

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lkt.knowledge import KnowledgeStore


class ServingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "knowledge.sqlite3"

    def version(self) -> str:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            return connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]

    def test_current_schema_attaches_while_worker_holds_write_lock(self) -> None:
        KnowledgeStore(self.database)
        writer = sqlite3.connect(self.database)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE schema_meta SET value='999' WHERE key='relation_graph_revision'"
            )
            with (
                patch.object(KnowledgeStore, "_initialise", side_effect=AssertionError("migration attempted")),
                patch.object(KnowledgeStore, "_hydrate_accepted_morpheme_meanings", side_effect=AssertionError("hydration attempted")),
                patch.object(KnowledgeStore, "_refresh_lexical_entity_payloads", side_effect=AssertionError("refresh attempted")),
            ):
                store = KnowledgeStore(self.database, serving=True)
            self.assertEqual(store.database, self.database)
            self.assertEqual(self.version(), "13")
        finally:
            writer.close()

    def test_fresh_serving_database_initializes_normally(self) -> None:
        KnowledgeStore(self.database, serving=True)
        self.assertEqual(self.version(), "13")

    def test_empty_serving_database_initializes_normally(self) -> None:
        sqlite3.connect(self.database).close()
        KnowledgeStore(self.database, serving=True)
        self.assertEqual(self.version(), "13")

    def test_old_serving_database_runs_migrations(self) -> None:
        KnowledgeStore(self.database)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE schema_meta SET value='12' WHERE key='schema_version'")
        KnowledgeStore(self.database, serving=True)
        self.assertEqual(self.version(), "13")


if __name__ == "__main__":
    unittest.main()
