"""Projection selection must not rescan the artifact/job queues per repair."""

from contextlib import ExitStack, closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lkt.knowledge import KnowledgeStore, _PROJECTION_REQUIRED_ARTIFACT_STAGES


class ProjectionSelectionPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = KnowledgeStore(Path(self.directory.name) / "knowledge.sqlite3")
        self.timestamp = "2026-01-01T00:00:00+00:00"

    def seed_term(self, connection, name):
        connection.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_key, label, status, payload,
                created_at, updated_at) VALUES (?, 'term', ?, ?, 'accepted', '{}', ?, ?)""",
            (name, name, name, self.timestamp, self.timestamp),
        )
        connection.execute(
            """INSERT INTO terms (entity_id, language, text, normalized, kind)
               VALUES (?, 'en', ?, ?, 'word')""", (name, name, name),
        )
        connection.execute(
            """INSERT INTO lexical_projection_repairs
               (term_entity_id, desired_fingerprint, created_at, updated_at)
               VALUES (?, 'desired', ?, ?)""", (name, self.timestamp, self.timestamp),
        )

    def seed_artifact(self, connection, name, subject, stage, state="accepted"):
        # Historical artifacts may have only subject_key, with no entity FK.
        connection.execute(
            """INSERT INTO preparation_jobs
               (job_id, job_key, job_type, subject_key, status, created_at, updated_at)
               VALUES (?, ?, 'retrieve-evidence', ?, 'complete', ?, ?)""",
            (name, name, "term:" + subject, self.timestamp, self.timestamp),
        )
        connection.execute(
            """INSERT INTO job_artifacts
               (artifact_id, job_id, stage, payload, created_at, validation_state)
               VALUES (?, ?, ?, '{}', ?, ?)""",
            (name, name, stage, self.timestamp, state),
        )

    def claim_with_budget(self):
        steps = 0
        original_connect = self.store._connect

        def progress():
            nonlocal steps
            steps += 1000
            return steps > 2_000_000

        def connect():
            connection = original_connect()
            connection.set_progress_handler(progress, 1000)
            return connection

        with ExitStack() as stack:
            # Isolate selection from separately tested graph reconciliation.
            for name in (
                "_bootstrap_lexical_projection_repairs_connection",
                "_reconcile_lexical_projection_repairs_connection",
            ):
                stack.enter_context(patch.object(self.store, name, return_value=None))
            stack.enter_context(patch.object(self.store, "_connect", side_effect=connect))
            result = self.store.claim_lexical_projection_repair()
        self.assertLessEqual(steps, 2_000_000)
        return result

    def test_large_queue_uses_bounded_work_and_accepts_cross_job_checkpoints(self):
        with closing(self.store._connect()) as connection:
            for number in range(180):
                self.seed_term(connection, f"pending-{number:03d}")
            for number in range(2000):
                for index, stage in enumerate(_PROJECTION_REQUIRED_ARTIFACT_STAGES):
                    self.seed_artifact(connection, f"noise-{number}-{index}",
                                       f"noise-{number}", stage)
            for index, stage in enumerate(_PROJECTION_REQUIRED_ARTIFACT_STAGES):
                self.seed_artifact(connection, f"ready-{index}", "pending-179", stage)
            connection.commit()
        result = self.claim_with_budget()
        self.assertEqual(result["term_entity_id"], "pending-179")
        self.assertEqual(result["status"], "claimed")
        self.assertTrue(result["lease_token"])

    def test_duplicate_and_rejected_stages_do_not_make_a_subject_ready(self):
        with closing(self.store._connect()) as connection:
            self.seed_term(connection, "incomplete")
            stages = _PROJECTION_REQUIRED_ARTIFACT_STAGES
            self.seed_artifact(connection, "first", "incomplete", stages[0])
            self.seed_artifact(connection, "duplicate", "incomplete", stages[0])
            self.seed_artifact(connection, "second", "incomplete", stages[1])
            self.seed_artifact(connection, "rejected", "incomplete", stages[2], "rejected")
            connection.commit()
        self.assertIsNone(self.claim_with_budget())

    def test_only_matching_active_composition_blocks_a_repair(self):
        with closing(self.store._connect()) as connection:
            for name in ("a-blocked", "b-different-fingerprint"):
                self.seed_term(connection, name)
                for index, stage in enumerate(_PROJECTION_REQUIRED_ARTIFACT_STAGES):
                    self.seed_artifact(connection, f"{name}-{index}", name, stage)
                connection.execute(
                    """INSERT INTO preparation_jobs
                       (job_id, job_key, job_type, subject_key, subject_entity_id,
                        source_fingerprint, created_at, updated_at)
                       VALUES (?, ?, 'compose-origin-card', ?, ?, ?, ?, ?)""",
                    (name, name, "term:" + name, name,
                     "desired" if name == "a-blocked" else "older",
                     self.timestamp, self.timestamp),
                )
            connection.commit()
        result = self.claim_with_budget()
        self.assertEqual(result["term_entity_id"], "b-different-fingerprint")


if __name__ == "__main__":
    unittest.main()
