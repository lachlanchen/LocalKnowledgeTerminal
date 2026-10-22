"""Measure SQLite work on growing libraries, not machine-specific wall time."""

from __future__ import annotations

import json
import sqlite3
import unittest

from lkt.language_completion import (
    REFRESH_VERSION, claim_language_completion, queue_language_card_refreshes,
)


class SyntheticLibrary:
    """Minimal relational fixture with the same scheduler-facing columns."""

    def __init__(self, size):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.steps = 0
        self.queries = []
        self.queued = []
        self.connection.executescript("""
            CREATE TABLE entities(entity_id TEXT PRIMARY KEY,status TEXT);
            CREATE TABLE terms(entity_id TEXT PRIMARY KEY,language TEXT);
            CREATE TABLE preparation_jobs(
                job_id TEXT PRIMARY KEY,subject_key TEXT,subject_entity_id TEXT,
                job_type TEXT,language TEXT,status TEXT,attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 2,priority INTEGER DEFAULT 50,
                created_at TEXT,updated_at TEXT,locked_at TEXT DEFAULT '',
                prompt_version TEXT DEFAULT 'fixture'
            );
            CREATE TABLE job_artifacts(
                artifact_id TEXT PRIMARY KEY,job_id TEXT,stage TEXT,language TEXT,
                validation_state TEXT,payload TEXT,created_at TEXT
            );
            CREATE TABLE job_dependencies(
                job_id TEXT,depends_on_job_id TEXT,PRIMARY KEY(job_id,depends_on_job_id)
            );
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT);
        """)
        for index in range(size):
            term = f"term-{index:05d}"
            subject = "term:" + term
            self.connection.execute("INSERT INTO entities VALUES (?, 'accepted')", (term,))
            self.connection.execute("INSERT INTO terms VALUES (?, 'en')", (term,))
            for kind, language, status in (
                ("prepare-meaning", "en", "complete"),
                ("compose-word-card", "en", "complete"),
                ("prepare-translation", "zh", "complete"),
                ("prepare-translation", "fr", "queued"),
            ):
                job_id = f"{term}:{kind}:{language}"
                self.connection.execute(
                    """INSERT INTO preparation_jobs(
                        job_id,subject_key,subject_entity_id,job_type,language,status,
                        created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (job_id, subject, term, kind, language, status,
                     "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                )
                if status != "complete":
                    continue
                stage = {
                    "prepare-meaning": "accepted-meaning",
                    "compose-word-card": "accepted-word-card",
                    "prepare-translation": "accepted-translation",
                }[kind]
                payload = {"knowledge_artifact_ids": [term + ":accepted-meaning"]}
                self.connection.execute(
                    "INSERT INTO job_artifacts VALUES (?,?,?,?,?,?,?)",
                    (term + ":" + stage, job_id, stage, language, "accepted",
                     json.dumps(payload), "2026-01-02T00:00:00+00:00"),
                )
            # Unrelated old artifacts must not multiply per-subject scan work.
            self.connection.executemany(
                "INSERT INTO job_artifacts VALUES (?,?,?,?,?,?,?)",
                ((f"{term}:old:{n}", f"{term}:prepare-meaning:en", "draft-meaning",
                  "en", "superseded", "{}", "2025-01-01T00:00:00+00:00") for n in range(8)),
            )
        self.connection.commit()
        self.connection.set_progress_handler(self._progress, 1000)
        self.connection.set_trace_callback(self.queries.append)

    def _progress(self):
        self.steps += 1000
        return self.steps > 2_000_000

    def _connect(self):
        # Closing a borrowed connection must not destroy this in-memory fixture.
        owner = self

        class Borrowed:
            def __getattr__(self, name):
                return getattr(owner.connection, name)

            def close(self):
                pass

        return Borrowed()

    @staticmethod
    def _fail_blocked_jobs(connection):
        # Real dependency/attempt behavior is covered by the companion suite.
        return 0

    def enqueue_job(self, kind, subject, **kwargs):
        self.queued.append((kind, subject, kwargs))
        return "captured-job"

    def close(self):
        self.connection.close()


class LanguageCompletionScalingTests(unittest.TestCase):
    def library(self, size):
        library = SyntheticLibrary(size)
        self.addCleanup(library.close)
        return library

    def test_refresh_selection_scales_near_linearly_without_correlated_scans(self):
        small, large = self.library(100), self.library(500)
        for library in (small, large):
            count = queue_language_card_refreshes(library, model="local-test")
            self.assertEqual(count, 8)
            self.assertEqual(len(library.queued), 8)
            selects = [q for q in library.queries if q.lstrip().startswith("SELECT")]
            self.assertEqual(len(selects), 2)
            self.assertTrue(all("NOT EXISTS" not in query for query in selects))
        self.assertLess(large.steps, small.steps * 8)

    def test_language_claim_shares_checkpoint_lookup_across_waiting_subjects(self):
        small, large = self.library(100), self.library(500)
        for library in (small, large):
            claimed = claim_language_completion(library, ("prepare-translation",))
            self.assertEqual(claimed["language"], "fr")
            self.assertEqual(claimed["status"], "running")
        self.assertLess(large.steps, small.steps * 8)

    def test_paused_refresh_is_not_bypassed_by_new_admission(self):
        library = self.library(1)
        library.connection.execute(
            """INSERT INTO preparation_jobs(
                job_id,subject_key,subject_entity_id,job_type,language,status,
                created_at,updated_at,prompt_version)
                VALUES ('paused','term:term-00000','term-00000','compose-word-card',
                        'en','paused','2026-01-03','2026-01-03',?)""",
            (REFRESH_VERSION,),
        )
        library.connection.commit()
        self.assertEqual(queue_language_card_refreshes(library, model="local-test"), 0)

    def test_archived_subject_and_superseded_input_do_not_trigger_refresh(self):
        library = self.library(2)
        library.connection.execute("UPDATE entities SET status='archived' WHERE entity_id='term-00000'")
        library.connection.execute(
            "UPDATE job_artifacts SET validation_state='superseded' WHERE artifact_id=?",
            ("term-00001:accepted-translation",),
        )
        library.connection.commit()
        self.assertEqual(queue_language_card_refreshes(library, model="local-test"), 0)


if __name__ == "__main__":
    unittest.main()
