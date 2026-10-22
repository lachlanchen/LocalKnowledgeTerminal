from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker, SUPPORTED_ATOMIC_JOBS
from lkt.growth import _TURN_KEY, claim_growth_work, growth_status
from lkt.knowledge import KnowledgeStore
from lkt.publication_queue import claim_bounded_composition_refresh


class ReadyPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3")

    def turn(self, number):
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO schema_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_TURN_KEY, str(number)))
            c.commit()

    def compose(self, word, version="autonomous-lexical-v4", dependencies=(), kind="compose-origin-card", priority=100):
        term = self.store.upsert_term("en", word, status="accepted")
        return self.store.enqueue_job(
            kind, "term:" + term, subject_entity_id=term, prompt_version=version,
            source_fingerprint=word, depends_on=dependencies, priority=priority,
        )

    def claim(self):
        return claim_growth_work(self.store, model="local-test-qwen", allowed_job_types=SUPPORTED_ATOMIC_JOBS)

    def test_ready_publication_precedes_every_slow_background_slot(self):
        for slot in (8, 9, 10, 11):
            with self.subTest(slot=slot):
                self.turn(slot)
                job = self.compose("fixture-" + str(slot))
                with mock.patch.object(self.store, "claim_next_job", side_effect=AssertionError("background preempted ready publication")):
                    self.assertEqual(self.claim()["job_id"], job)
                self.assertEqual(growth_status(self.store)["worker_turns"], slot)
                self.store.finish_job(job)

    def test_finished_history_publishes_without_waiting_for_next_history_turn(self):
        self.turn(10)
        job = self.compose("history-fixture", "continuous-origin-v1-revisit-fixture")
        self.assertEqual(self.claim()["job_id"], job)
        self.assertEqual(growth_status(self.store)["worker_turns"], 10)

    def test_old_refresh_and_new_publication_both_precede_the_next_model_turn(self):
        refresh = self.compose("old-fixture", "autonomous-lexical-v4-projection-local-p1")
        new = self.compose("new-fixture")
        self.assertEqual(self.claim()["job_id"], refresh)
        self.store.finish_job(refresh)
        self.assertEqual(self.claim()["job_id"], new)
        self.assertEqual(growth_status(self.store)["worker_turns"], 0)

    def test_refresh_quota_survives_restart_and_resets_on_normal_dispatch(self):
        first = self.compose("refresh-one", "autonomous-lexical-v4-projection-local-p1")
        second = self.compose("refresh-two", "autonomous-lexical-v4-projection-local-p2")
        third = self.compose("refresh-three", "autonomous-lexical-v4-projection-local-p3")
        args = {"allowed_job_types": SUPPORTED_ATOMIC_JOBS, "turn_key": _TURN_KEY}
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], first)
        self.store.finish_job(first)
        restarted = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3", serving=True)
        self.assertEqual(claim_bounded_composition_refresh(restarted, **args)["job_id"], second)
        restarted.finish_job(second)
        self.assertIsNone(claim_bounded_composition_refresh(restarted, **args))
        self.turn(1)
        self.assertEqual(claim_bounded_composition_refresh(restarted, **args)["job_id"], third)

    def test_new_urgent_refreshes_cannot_overtake_an_older_ready_card(self):
        old = self.compose("older-fixture", "mixed-partial-projection-v6-validation", priority=100)
        urgent = self.compose("urgent-fixture", "autonomous-lexical-v4-projection-local-p1", priority=5)
        args = {"allowed_job_types": SUPPORTED_ATOMIC_JOBS, "turn_key": _TURN_KEY}
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], old)
        self.store.finish_job(old)
        self.compose("newer-urgent-fixture", "autonomous-lexical-v4-projection-local-p2", priority=-100)
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], urgent)

    def test_word_backlog_and_origin_updates_alternate_oldest_within_each_kind(self):
        first_word = self.compose("old-word-one", kind="compose-word-card")
        second_word = self.compose("old-word-two", kind="compose-word-card", priority=5)
        origin = self.compose("new-origin", "autonomous-lexical-v4-projection-local-p1")
        args = {"allowed_job_types": SUPPORTED_ATOMIC_JOBS, "turn_key": _TURN_KEY}
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], first_word)
        self.store.finish_job(first_word)
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], origin)
        self.store.finish_job(origin)
        self.assertIsNone(claim_bounded_composition_refresh(self.store, **args))
        self.turn(1)
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], second_word)

    def test_empty_ready_queue_does_not_spend_the_publication_quota(self):
        args = {"allowed_job_types": SUPPORTED_ATOMIC_JOBS, "turn_key": _TURN_KEY}
        self.assertIsNone(claim_bounded_composition_refresh(self.store, **args))
        first = self.compose("first-after-idle")
        second = self.compose("second-after-idle")
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], first)
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], second)
        self.assertIsNone(claim_bounded_composition_refresh(self.store, **args))

    def test_running_prerequisite_blocks_but_failed_prerequisite_can_publish(self):
        dependency = self.store.enqueue_job("prepare-meaning", "dependency-fixture", max_attempts=1)
        self.assertEqual(self.store.claim_next_job(("prepare-meaning",))["job_id"], dependency)
        publication = self.compose("dependency-fixture", dependencies=(dependency,))
        args = {"allowed_job_types": SUPPORTED_ATOMIC_JOBS, "turn_key": _TURN_KEY}
        self.assertIsNone(claim_bounded_composition_refresh(self.store, **args))
        self.store.finish_job(dependency, error="fixture model failure")
        self.assertEqual(claim_bounded_composition_refresh(self.store, **args)["job_id"], publication)

    def test_explicit_types_and_exhausted_attempts_are_respected(self):
        exhausted = self.compose("exhausted-word", kind="compose-word-card")
        self.compose("origin-not-allowed")
        ready = self.compose("ready-word", kind="compose-word-card")
        with closing(self.store._connect()) as c:
            c.execute("UPDATE preparation_jobs SET attempts=max_attempts WHERE job_id=?", (exhausted,))
            c.commit()
        job = claim_bounded_composition_refresh(self.store, ("compose-word-card",), turn_key=_TURN_KEY)
        self.assertEqual(job["job_id"], ready)
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["status"], "running")
        self.assertTrue(job["locked_at"])

    def test_concurrent_publishers_share_the_durable_quota_and_unique_leases(self):
        jobs = {self.compose("concurrent-" + str(index)) for index in range(3)}
        self.turn(42)
        stores = [KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3", serving=True)
                  for _ in range(3)]

        def claim(store):
            return claim_bounded_composition_refresh(store, SUPPORTED_ATOMIC_JOBS, turn_key=_TURN_KEY)

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(claim, stores))
        ids = [result["job_id"] for result in results if result is not None]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(set(ids).issubset(jobs))

    def test_normal_worker_publishes_both_card_kinds_then_advances_inference(self):
        origin = self.compose("cached-origin", "mixed-partial-projection-v6-validation")
        word = self.compose("cached-word", "language-card-refresh-v1", kind="compose-word-card", priority=5)
        term = self.store.upsert_term("en", "next-fixture", status="accepted")
        meaning = self.store.enqueue_job(
            "prepare-meaning", "term:" + term, subject_entity_id=term,
            prompt_version="autonomous-lexical-v4", source_fingerprint="next-fixture", priority=-100,
        )
        self.compose("next-fixture", dependencies=(meaning,))
        model = mock.Mock(model_name="local-test-qwen")
        worker = PreparationWorker(self.store, mock.Mock(), model, card_store=object(), continuous_growth=True)
        worker._compose_origin_card = mock.Mock(return_value="origin-artifact")
        worker._compose_word_card = mock.Mock(return_value="word-artifact")
        worker._prepare_meaning = mock.Mock(return_value="meaning-artifact")
        self.assertEqual([worker.run_once().job_id for _ in range(3)], [origin, word, meaning])
        worker._compose_origin_card.assert_called_once()
        worker._compose_word_card.assert_called_once()
        worker._prepare_meaning.assert_called_once()
        self.assertEqual(growth_status(self.store)["worker_turns"], 1)

    def test_health_describes_the_bounded_fair_policy(self):
        dispatch = growth_status(self.store)["dispatch"]
        self.assertEqual(dispatch["cached_refresh_limit"], "two-per-normal-dispatch")
        self.assertEqual(dispatch["cached_refresh_order"], "alternate-word-origin-oldest-ready")

    def test_refresh_never_claims_a_model_job(self):
        self.store.enqueue_job("prepare-meaning", "fixture", priority=-100)
        self.assertIsNone(claim_bounded_composition_refresh(self.store, SUPPORTED_ATOMIC_JOBS, turn_key=_TURN_KEY))

    def test_ready_word_card_can_use_the_bounded_model_free_refresh(self):
        job = self.compose("word-fixture", kind="compose-word-card")
        self.assertEqual(claim_bounded_composition_refresh(self.store, SUPPORTED_ATOMIC_JOBS, turn_key=_TURN_KEY)["job_id"], job)

    def test_unfinished_dependency_still_blocks_fast_publication(self):
        dependency = self.store.enqueue_job("prepare-meaning", "dependency-fixture")
        self.compose("blocked-fixture", dependencies=(dependency,))
        self.assertIsNone(claim_bounded_composition_refresh(self.store, SUPPORTED_ATOMIC_JOBS, turn_key=_TURN_KEY))

    def test_explicit_model_free_dispatch_does_not_enter_autonomous_policy(self):
        self.compose("fixture")
        self.assertIsNone(claim_growth_work(self.store, model="local-test-qwen", allowed_job_types=("compose-origin-card",)))


if __name__ == "__main__":
    unittest.main()
