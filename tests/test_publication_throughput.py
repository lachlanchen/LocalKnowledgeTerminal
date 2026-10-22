from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker, SUPPORTED_ATOMIC_JOBS
from lkt.growth import claim_growth_work, growth_status
from lkt.knowledge import KnowledgeStore
from lkt.publication_queue import PUBLICATION_JOB_TYPES, claim_publication_job


class PublicationThroughputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "knowledge.sqlite3"
        self.store = KnowledgeStore(self.path)

    def plan(self, word, version="autonomous-morphology-v1"):
        term = self.store.upsert_term("en", word, status="accepted")
        jobs = []
        for kind in PUBLICATION_JOB_TYPES:
            jobs.append(self.store.enqueue_job(
                kind, "term:" + term, subject_entity_id=term,
                prompt_version=version, source_fingerprint=word + "-source",
                priority=10 + len(jobs) * 20, depends_on=jobs[-1:],
            ))
        return jobs

    def turn(self, number):
        with closing(self.store._connect()) as connection:
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES ('continuous_origin_worker_turn_v1',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number),),
            )
            connection.commit()

    def test_one_plan_finishes_before_next_subject_despite_priorities(self):
        first = self.plan("first-fixture")
        second = self.plan("second-fixture")
        for expected in first + second:
            job = claim_publication_job(self.store, PUBLICATION_JOB_TYPES)
            self.assertEqual(job["job_id"], expected)
            self.store.finish_job(expected)

    def test_focus_survives_restart(self):
        first = self.plan("first-fixture")
        self.plan("second-fixture")
        self.assertEqual(claim_publication_job(self.store, PUBLICATION_JOB_TYPES)["job_id"], first[0])
        self.store.finish_job(first[0])
        restarted = KnowledgeStore(self.path, serving=True)
        self.assertEqual(claim_publication_job(restarted, PUBLICATION_JOB_TYPES)["job_id"], first[1])

    def test_running_dependency_yields_without_stealing_lease(self):
        first = self.plan("first-fixture")
        second = self.plan("second-fixture")
        self.assertEqual(claim_publication_job(self.store, PUBLICATION_JOB_TYPES)["job_id"], first[0])
        self.assertEqual(claim_publication_job(self.store, PUBLICATION_JOB_TYPES)["job_id"], second[0])

    def test_repairs_and_history_cannot_take_publication_slots(self):
        self.plan("repair-fixture", "autonomous-lexical-v4-projection-local-p1")
        self.plan("history-fixture", "continuous-origin-v1-revisit-1")
        self.plan("semantic-fixture", "semantic-enrichment:fixture")
        ordinary = self.plan("ordinary-fixture")
        self.assertEqual(claim_publication_job(self.store, PUBLICATION_JOB_TYPES)["job_id"], ordinary[0])

    def test_publication_reservation_precedes_semantic_and_word_repairs(self):
        first = self.plan("first-fixture")
        worker = PreparationWorker(
            self.store, mock.Mock(), mock.Mock(model_name="local-test-qwen"),
            card_store=object(), continuous_growth=True,
        )
        worker._retrieve = mock.Mock(return_value="fixture-artifact")
        with mock.patch("lkt.atomic._semantic_enrichment_worker_slot_due", return_value=True) as semantic:
            with mock.patch.object(self.store, "claim_word_card_progress") as language:
                result = worker.run_once()
        self.assertEqual(result.job_id, first[0])
        semantic.assert_not_called()
        language.assert_not_called()

    def test_all_eight_reserved_turns_select_new_publication(self):
        for number in range(8):
            self.plan("fixture-" + str(number))
        with mock.patch("lkt.growth._plan_history") as history:
            for _ in range(8):
                job = claim_growth_work(self.store, model="local-test-qwen", allowed_job_types=SUPPORTED_ATOMIC_JOBS)
                self.assertIsNotNone(job)
                self.assertEqual(job["prompt_version"], "autonomous-morphology-v1")
                self.store.finish_job(job["job_id"])
        history.assert_not_called()

    def test_maintenance_has_two_explicit_turns(self):
        self.plan("first-fixture")
        for turn in (9, 11):
            self.turn(turn)
            self.assertIsNone(claim_growth_work(self.store, model="local-test-qwen", allowed_job_types=SUPPORTED_ATOMIC_JOBS))

    def test_background_history_and_morphology_keep_reservations(self):
        self.plan("first-fixture")
        self.turn(8)
        with mock.patch("lkt.growth._plan_history"), mock.patch("lkt.growth._claim_history_job", return_value={"job_id": "history"}):
            self.assertEqual(claim_growth_work(self.store, model="local-test-qwen", allowed_job_types=SUPPORTED_ATOMIC_JOBS)["job_id"], "history")
        self.turn(10)
        with mock.patch.object(self.store, "claim_next_job", return_value={"job_id": "morphology"}) as claim:
            self.assertEqual(claim_growth_work(self.store, model="local-test-qwen", allowed_job_types=SUPPORTED_ATOMIC_JOBS)["job_id"], "morphology")
            claim.assert_called_once_with(("enrich-morphology-v3",))

    def test_type_restriction_does_not_claim_inference(self):
        self.plan("first-fixture")
        self.assertIsNone(claim_publication_job(self.store, ("compose-origin-card",)))

    def test_status_exposes_dispatch_policy(self):
        self.plan("first-fixture")
        claim_publication_job(self.store, PUBLICATION_JOB_TYPES)
        status = growth_status(self.store)["dispatch"]
        self.assertEqual(status["publication_turns"], 8)
        self.assertEqual(status["cycle"], 12)
        self.assertTrue(status["focused_publication_job"])


if __name__ == "__main__":
    unittest.main()
