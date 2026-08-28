from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lkt.knowledge import KnowledgeStore
from lkt.preparation import PreparationPlanner


class PreparationPlannerTests(unittest.TestCase):
    def test_word_plan_splits_languages_and_blocks_composition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            planner = PreparationPlanner(
                store,
                model="Qwen3-8B-Q4_K_M",
                prompt_version="atomic-v1",
                source_fingerprint="books-v1",
            )
            plan = planner.plan_word(
                "inspection", display_languages=("en", "ja", "zh", "ar")
            )
            self.assertIn("split-morphemes", plan.jobs)
            self.assertIn("expand-origin-branches", plan.jobs)
            self.assertIn("translation:ja", plan.jobs)
            self.assertIn("translation:zh", plan.jobs)
            self.assertIn("translation:ar", plan.jobs)
            self.assertIn("pronunciation:ja", plan.jobs)
            self.assertIn("compose-word-card", plan.jobs)
            self.assertIn("compose-origin-card", plan.jobs)
            first = store.claim_next_job()
            self.assertEqual(first["job_type"], "retrieve-evidence")
            self.assertIsNone(store.claim_next_job())
            store.finish_job(first["job_id"])
            second = store.claim_next_job()
            self.assertEqual(second["job_type"], "prepare-meaning")
            connection = sqlite3.connect(database)
            blocked = connection.execute(
                "SELECT status FROM preparation_jobs WHERE job_id = ?",
                (plan.jobs["compose-origin-card"],),
            ).fetchone()[0]
            connection.close()
            self.assertEqual(blocked, "queued")

    def test_planning_does_not_downgrade_established_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            term_id = store.upsert_term("en", "inspection", status="accepted")
            planner = PreparationPlanner(store, model="Qwen3-8B-Q4_K_M")
            self.assertEqual(planner.plan_word("Inspection").subject_entity_id, term_id)
            connection = sqlite3.connect(database)
            status = connection.execute(
                "SELECT status FROM entities WHERE entity_id = ?", (term_id,)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(status, "accepted")

    def test_one_translation_can_be_replanned_without_the_full_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "inspection", status="accepted")
            subject_key = f"term:{term_id}"
            meaning_job = store.enqueue_job(
                "prepare-meaning", subject_key, subject_entity_id=term_id
            )
            store.save_job_artifact(
                meaning_job,
                "accepted-meaning",
                {"meaning_id": "meaning-1", "definition": "an official examination"},
                language="en",
                validation_state="accepted",
                quality_score=0.9,
            )
            store.finish_job(meaning_job)
            planner = PreparationPlanner(
                store, model="Qwen3-4B-Q4_K_M", prompt_version="atomic-v2"
            )
            plan = planner.plan_translation("inspection", "ar")
            self.assertEqual(set(plan.jobs), {"translation:ar"})
            claimed = store.claim_next_job(("prepare-translation",))
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["language"], "ar")
            self.assertEqual(claimed["prompt_version"], "atomic-v2")

    def test_answer_plan_prepares_languages_and_investigation_terms_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            planner = PreparationPlanner(store, model="Qwen3-8B-Q4_K_M")
            plan = planner.plan_content(
                "answer",
                "Look more closely.",
                source_key="answer-1",
                display_languages=("en", "ja", "zh"),
            )
            self.assertIn("extract-investigation-terms", plan.jobs)
            self.assertIn("grammar-parts", plan.jobs)
            self.assertIn("translation:en", plan.jobs)
            self.assertIn("translation:ja", plan.jobs)
            self.assertIn("translation:zh", plan.jobs)
            self.assertIn("compose-answer-card", plan.jobs)


if __name__ == "__main__":
    unittest.main()
