from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from lkt.knowledge import KnowledgeStore


class PayloadRefreshStore(KnowledgeStore):
    """Exercise startup refresh independently of unrelated schema migrations."""

    def _initialise(self) -> None:
        pass

    def _hydrate_accepted_morpheme_meanings(self) -> None:
        pass


class StoreStartupLockingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "knowledge.sqlite3"

    def seed(self, journal: str) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(f"PRAGMA journal_mode={journal}")
            connection.executescript("""
                CREATE TABLE entities(entity_id TEXT PRIMARY KEY, payload TEXT,
                                      updated_at TEXT);
                CREATE TABLE terms(entity_id TEXT, language TEXT, kind TEXT);
                CREATE TABLE morphemes(entity_id TEXT, language TEXT, kind TEXT,
                                       meaning TEXT);
                CREATE TABLE meanings(entity_id TEXT, language TEXT,
                    definition TEXT, part_of_speech TEXT, register_label TEXT,
                    domain_label TEXT, sense_order INTEGER);
                CREATE TABLE pronunciations(entity_id TEXT, language TEXT,
                    system TEXT, reading TEXT, dialect TEXT);
                CREATE TABLE translations(entity_id TEXT, target_language TEXT,
                    transliteration TEXT, usage_note TEXT);
                CREATE TABLE historical_forms(entity_id TEXT, language TEXT,
                    period_label TEXT, meaning TEXT, date_min INTEGER,
                    date_max INTEGER);
                CREATE TABLE history_events(entity_id TEXT, language TEXT,
                    event_type TEXT, period_label TEXT, description TEXT,
                    date_min INTEGER, date_max INTEGER);
            """)
            connection.execute(
                "INSERT INTO entities VALUES(?, ?, ?)",
                ("example", json.dumps({"language": "old", "source": "book"}), "before"),
            )
            connection.execute("INSERT INTO terms VALUES('example', 'en', 'word')")
            connection.commit()

    def test_startup_waits_for_writer_before_reading_payloads(self) -> None:
        self.seed("WAL")
        attempting_write = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []
        database = self.database

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *parameters):
                if sql.strip().upper() == "BEGIN IMMEDIATE":
                    attempting_write.set()
                return super().execute(sql, *parameters)

        class ObservedStore(PayloadRefreshStore):
            def _open_connection(self):
                connection = sqlite3.connect(
                    self.database, timeout=5, factory=ObservedConnection,
                )
                connection.row_factory = sqlite3.Row
                return connection

        def initialise() -> None:
            try:
                ObservedStore(database)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        writer = sqlite3.connect(self.database)
        thread = threading.Thread(target=initialise, daemon=True)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE terms SET language='fr'")
            thread.start()
            self.assertTrue(attempting_write.wait(2), "startup did not acquire its write lock first")
            self.assertFalse(finished.is_set())
            writer.commit()
            self.assertTrue(finished.wait(5), "startup did not resume after the writer committed")
        finally:
            writer.close()
            thread.join(6)
        self.assertEqual(errors, [])
        with closing(sqlite3.connect(self.database)) as connection:
            payload = json.loads(connection.execute("SELECT payload FROM entities").fetchone()[0])
        self.assertEqual(payload, {"language": "fr", "kind": "word", "source": "book"})

    def test_repeated_startup_does_not_rewrite_unchanged_payloads(self) -> None:
        self.seed("DELETE")
        PayloadRefreshStore(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            first = connection.execute("SELECT payload, updated_at FROM entities").fetchone()
        PayloadRefreshStore(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            second = connection.execute("SELECT payload, updated_at FROM entities").fetchone()
        self.assertEqual(second, first)
        self.assertEqual(json.loads(second[0])["source"], "book")


if __name__ == "__main__":
    unittest.main()
