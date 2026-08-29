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

    def test_linked_word_card_plan_excludes_independent_origin_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="linked-word-v1",
            ).plan_word_card(
                "breakthrough", display_languages=("en", "ja", "zh", "fr", "ar")
            )
            self.assertIn("compose-word-card", plan.jobs)
            self.assertIn("translation:ja", plan.jobs)
            self.assertIn("grammar-properties", plan.jobs)
            self.assertNotIn("split-morphemes", plan.jobs)
            self.assertNotIn("expand-origin-branches", plan.jobs)
            self.assertNotIn("compose-origin-card", plan.jobs)
            job_types = {
                item["job_type"] for item in store.jobs_for_subject(plan.subject_key)
            }
            self.assertNotIn("split-morphemes", job_types)
            self.assertNotIn("expand-origin-branches", job_types)

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

    def test_one_language_rebuild_includes_its_pronunciation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "breakthrough", status="accepted")
            subject_key = f"term:{term_id}"
            meaning_job = store.enqueue_job("prepare-meaning", subject_key)
            store.save_job_artifact(
                meaning_job,
                "accepted-meaning",
                {"meaning_id": "meaning-1", "definition": "a productive insight"},
                language="en",
                validation_state="accepted",
            )
            store.finish_job(meaning_job)
            plan = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="arabic-script-v2",
            ).plan_language("breakthrough", "ar")
            self.assertEqual(
                set(plan.jobs), {"translation:ar", "pronunciation:ar"}
            )
            store.finish_job(plan.jobs["translation:ar"])
            claimed = store.claim_next_job(("prepare-pronunciation",))
            self.assertEqual(claimed["job_id"], plan.jobs["pronunciation:ar"])

    def test_word_card_view_reuses_only_accepted_atoms(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "breakthrough", status="accepted")
            subject_key = f"term:{term_id}"
            for stage, language in (
                ("accepted-meaning", "en"),
                ("accepted-grammar-properties", "en"),
                ("accepted-pronunciation", "en"),
                ("accepted-translation", "ja"),
                ("accepted-pronunciation", "ja"),
            ):
                job_id = store.enqueue_job(
                    stage,
                    subject_key,
                    language=language,
                    prompt_version="accepted-v1",
                )
                store.save_job_artifact(
                    job_id,
                    stage,
                    {"accepted": True},
                    language=language,
                    validation_state="accepted",
                )
                store.finish_job(job_id)
            plan = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="word-card-view-v2",
            ).plan_word_card_view(
                "breakthrough", display_languages=("en", "ja")
            )
            self.assertEqual(set(plan.jobs), {"compose-word-card"})

    def test_evidence_can_be_refreshed_without_replanning_downstream_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            planner = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="retrieval-v2",
                source_fingerprint="polished-books-v2",
            )
            plan = planner.plan_evidence("inspection")
            self.assertEqual(set(plan.jobs), {"retrieve-evidence"})
            claimed = store.claim_next_job(("retrieve-evidence",))
            self.assertEqual(claimed["prompt_version"], "retrieval-v2")
            self.assertEqual(claimed["source_fingerprint"], "polished-books-v2")

    def test_morphemes_can_be_replanned_from_current_evidence_and_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "inspection", status="accepted")
            subject_key = f"term:{term_id}"
            evidence_job = store.enqueue_job(
                "retrieve-evidence", subject_key, subject_entity_id=term_id
            )
            store.save_job_artifact(
                evidence_job,
                "retrieved-evidence",
                {"records": []},
                validation_state="candidate",
            )
            store.finish_job(evidence_job)
            meaning_job = store.enqueue_job(
                "prepare-meaning", subject_key, subject_entity_id=term_id
            )
            store.save_job_artifact(
                meaning_job,
                "accepted-meaning",
                {"meaning_id": "meaning-1"},
                language="en",
                validation_state="accepted",
                quality_score=0.9,
            )
            store.finish_job(meaning_job)
            planner = PreparationPlanner(
                store, model="Qwen3-4B-Q4_K_M", prompt_version="morphology-v2"
            )
            plan = planner.plan_morphemes("inspection")
            self.assertEqual(set(plan.jobs), {"split-morphemes"})
            claimed = store.claim_next_job(("split-morphemes",))
            self.assertEqual(claimed["prompt_version"], "morphology-v2")

    def test_origin_can_be_replanned_from_an_accepted_split(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "inspection", status="accepted")
            subject_key = f"term:{term_id}"
            split_job = store.enqueue_job(
                "split-morphemes", subject_key, subject_entity_id=term_id
            )
            store.save_job_artifact(
                split_job,
                "accepted-morpheme-split",
                {"term": "inspection", "parts": [{"surface": "spect"}]},
                language="en",
                validation_state="accepted",
                quality_score=0.9,
            )
            store.finish_job(split_job)
            plan = PreparationPlanner(
                store, model="Qwen3-4B-Q4_K_M", prompt_version="origin-v2"
            ).plan_origin("inspection")
            self.assertEqual(set(plan.jobs), {"expand-origin-branches"})
            claimed = store.claim_next_job(("expand-origin-branches",))
            self.assertEqual(claimed["prompt_version"], "origin-v2")

    def test_lexical_history_repair_reuses_accepted_language_atoms(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "lecher", status="accepted")
            subject_key = f"term:{term_id}"

            def checkpoint(
                stage: str,
                language: str = "",
                validation_state: str = "accepted",
            ) -> str:
                job_id = store.enqueue_job(
                    f"old-{stage}",
                    subject_key,
                    subject_entity_id=term_id,
                    language=language,
                    prompt_version="old-v1",
                )
                store.save_job_artifact(
                    job_id,
                    stage,
                    {"accepted": True},
                    language=language,
                    validation_state=validation_state,
                    quality_score=0.9,
                )
                store.finish_job(job_id)
                return job_id

            checkpoint("retrieved-evidence", validation_state="candidate")
            checkpoint("accepted-meaning", "en")
            for language in ("ja", "zh", "fr", "ar"):
                checkpoint("accepted-translation", language)
            for language in ("en", "ja", "zh", "fr", "ar"):
                checkpoint("accepted-pronunciation", language)

            plan = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="autonomous-lexical-v2",
                source_fingerprint="books-v2",
            ).plan_lexical_history_repair("lecher")

            self.assertEqual(
                set(plan.jobs),
                {
                    "split-morphemes",
                    "expand-origin-branches",
                    "compose-origin-card",
                },
            )
            claimed = store.claim_next_job(("split-morphemes",))
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["prompt_version"], "autonomous-lexical-v2")
            self.assertEqual(claimed["source_fingerprint"], "books-v2")

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

    def test_reviewed_card_enrichment_uses_one_independent_job_per_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            store.acquire_card_book_card(
                {
                    "card_id": "answer-card-1",
                    "mode": "answer",
                    "english": {"term": "Look more closely."},
                    "japanese": {"term": "もっとよく見て。"},
                    "chinese": {"simplified": "再仔细看看。"},
                    "evidence": [
                        {
                            "corpus_id": "book-of-answers",
                            "entry_id": "answer-1",
                            "locator": "answers.xhtml",
                            "excerpt": "Look more closely.",
                        }
                    ],
                }
            )
            planner = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="content-enrichment-v2",
            )
            plan = planner.plan_card_enrichment("answer-card-1")
            self.assertEqual(
                set(plan.jobs),
                {
                    "extract-investigation-terms",
                    "grammar:en",
                    "grammar:ja",
                    "grammar:zh",
                },
            )
            jobs = {
                (job["job_type"], job["language"])
                for job in store.jobs_for_subject(plan.subject_key)
            }
            self.assertIn(("extract-investigation-terms", "en"), jobs)
            self.assertIn(("prepare-grammar-parts", "en"), jobs)
            self.assertEqual(
                len(
                    PreparationPlanner(
                        store,
                        model="Qwen3-4B-Q4_K_M",
                        prompt_version="grammar-backfill-v2",
                    ).plan_card_enrichment(
                        "answer-card-1", include_investigation=False
                    ).jobs
                ),
                3,
            )

    def test_missing_only_enrichment_replans_failed_language_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            store.acquire_card_book_card(
                {
                    "card_id": "answer-card-repair",
                    "mode": "answer",
                    "english": {"term": "Look more closely."},
                    "japanese": {"term": "もっとよく見て。"},
                    "chinese": {"simplified": "再仔细看看。"},
                    "evidence": [
                        {
                            "corpus_id": "book-of-answers",
                            "entry_id": "answer-repair",
                            "locator": "answers.xhtml",
                            "excerpt": "Look more closely.",
                        }
                    ],
                }
            )
            first = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="autonomous-content-enrichment-v2",
            ).plan_card_enrichment(
                "answer-card-repair", include_investigation=False
            )
            while True:
                claimed = store.claim_next_job(("prepare-grammar-parts",))
                self.assertIsNotNone(claimed)
                if claimed["language"] != "en":
                    self.fail("English should be the first queued language")
                store.finish_job(claimed["job_id"], error="old validator rejected draft")
                if claimed["attempts"] >= claimed["max_attempts"]:
                    break

            repaired = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="autonomous-content-enrichment-v3",
            ).plan_card_enrichment(
                "answer-card-repair",
                include_investigation=False,
                missing_only=True,
            )
            self.assertEqual(set(repaired.jobs), {"grammar:en"})
            self.assertNotEqual(repaired.jobs["grammar:en"], first.jobs["grammar:en"])


if __name__ == "__main__":
    unittest.main()
