"""An infinite enrichment backlog must leave turns for ordinary requests."""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from lkt.growth import claim_growth_work, _TURN_KEY
from lkt.knowledge import KnowledgeStore


class GrowthQueueFairnessTests(unittest.TestCase):
    def test_ordinary_turns_claim_the_request_before_background_fallback(self):
        for turn in (1, 3):
            with self.subTest(turn=turn), tempfile.TemporaryDirectory() as directory:
                store = KnowledgeStore(Path(directory) / "knowledge.sqlite3")
                with closing(store._connect()) as connection:
                    connection.execute("INSERT INTO schema_meta(key,value) VALUES (?,?)",
                                       (_TURN_KEY, str(turn)))
                    for name, kind, priority in (
                        ("request", "retrieve-evidence", -20),
                        ("background", "enrich-morphology-v3", 50),
                    ):
                        connection.execute(
                            """INSERT INTO preparation_jobs
                               (job_id,job_key,job_type,subject_key,priority,created_at,updated_at)
                               VALUES (?,?,?,?,?,'2026-01-01','2026-01-01')""",
                            (name, name, kind, name, priority),
                        )
                    connection.commit()
                job = claim_growth_work(store, model="local-test",
                                        allowed_job_types=("retrieve-evidence", "enrich-morphology-v3"))
                self.assertEqual(job["job_id"], "request")

    def test_model_free_dispatch_does_not_enter_the_growth_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "knowledge.sqlite3")
            self.assertIsNone(claim_growth_work(
                store, model="local-test", allowed_job_types=("retrieve-evidence",)))


if __name__ == "__main__":
    unittest.main()
