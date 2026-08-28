from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.preparation import PreparationPlanner


class FakeRetriever:
    def retrieve(self, term: str) -> list[dict[str, Any]]:
        return [
            {
                "entry_id": "dictionary-inspection-1",
                "corpus_id": "test-dictionary:1.0",
                "source_title": "Test Dictionary",
                "headword": term,
                "part_of_speech": "noun",
                "definition": "a careful examination of something",
                "source_hash": "abc123",
                "locator": "sense 1",
            }
        ]


class FakeAtomicModel:
    model_name = "test-qwen-8b"

    def complete_json(
        self, _system: str, prompt: str, *, max_tokens: int = 256
    ) -> dict[str, Any]:
        match = re.search(r'"id":\s*"([^"]+)"', prompt)
        assert match is not None
        return {
            "value": {
                "definition": "A careful examination to assess condition or quality.",
                "part_of_speech": "noun",
                "sense_note": "the core examination sense",
                "confidence": 0.92,
                "evidence_ids": [match.group(1)],
            },
            "model": self.model_name,
            "metrics": {"completion_tokens": 40},
        }


class AtomicWorkerTests(unittest.TestCase):
    def test_evidence_and_meaning_run_as_two_reusable_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(
                store,
                model="test-qwen-8b",
                source_fingerprint="sources-v1",
            ).plan_word("inspection", display_languages=("en",))
            worker = PreparationWorker(store, FakeRetriever(), FakeAtomicModel())
            results = worker.run(2)
            self.assertEqual(
                [result.job_type for result in results],
                ["retrieve-evidence", "prepare-meaning"],
            )
            self.assertTrue(all(result.status == "complete" for result in results))
            term = store.term_record(plan.subject_entity_id)
            self.assertEqual(term["status"], "accepted")
            self.assertEqual(term["quality_score"], 0.92)
            meaning = store.artifacts_for_subject(
                plan.subject_key, stage="accepted-meaning"
            )
            self.assertEqual(len(meaning), 1)
            self.assertEqual(meaning[0]["payload"]["part_of_speech"], "noun")
            self.assertEqual(store.status()["counts"]["meanings"], 1)

    def test_worker_does_not_claim_later_unsupported_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test").plan_word(
                "inspection", display_languages=("en",)
            )
            worker = PreparationWorker(store, FakeRetriever(), FakeAtomicModel())
            self.assertEqual(len(worker.run(10)), 2)
            queued_types = {
                job["job_type"]
                for job in store.jobs_for_subject(plan.subject_key)
                if job["status"] == "queued"
            }
            self.assertIn("split-morphemes", queued_types)
            self.assertIn("compose-word-card", queued_types)


if __name__ == "__main__":
    unittest.main()
