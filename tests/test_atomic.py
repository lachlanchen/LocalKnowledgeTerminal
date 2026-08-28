from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.atomic import PreparationWorker, _lexically_related
from lkt.knowledge import KnowledgeStore
from lkt.models import Evidence
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
                "translations": {"ja": ["\u691c\u67fb"]},
                "source_hash": "abc123",
                "locator": "sense 1",
            }
        ]


class FakeAtomicModel:
    model_name = "test-qwen-8b"

    def complete_json(
        self, _system: str, prompt: str, *, max_tokens: int = 256
    ) -> dict[str, Any]:
        match = re.search(r'"(evidence-[^"]+)"', prompt)
        assert match is not None
        if "TARGET LANGUAGE: Japanese" in prompt:
            return {
                "value": {
                    "term": "\u691c\u67fb",
                    "meaning": "\u72b6\u614b\u3084\u54c1\u8cea\u3092\u78ba\u304b\u3081\u308b\u305f\u3081\u306e\u516c\u5f0f\u306a\u8abf\u67fb\u3002",
                    "reading": "\u3051\u3093\u3055",
                    "usage_note": "standard formal examination sense",
                    "confidence": 0.9,
                    "evidence_ids": [match.group(1)],
                },
                "model": self.model_name,
                "metrics": {"completion_tokens": 48},
            }
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
    def test_morphology_context_rejects_incidental_fts_hits(self) -> None:
        def item(headword: str, kind: str) -> Evidence:
            return Evidence("id", headword, "", "", (), "excerpt", kind=kind)

        self.assertTrue(
            _lexically_related("inspection", item("inspect", "morphology-root"))
        )
        self.assertTrue(
            _lexically_related("inspection", item("-ion", "morphology-affix"))
        )
        self.assertFalse(
            _lexically_related("inspection", item("injurious", "morphology-root"))
        )
        self.assertFalse(
            _lexically_related("inspection", item("autopsy", "morphology-affix"))
        )

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

    def test_translation_is_a_separate_sense_aligned_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "ja")
            )
            worker = PreparationWorker(store, FakeRetriever(), FakeAtomicModel())
            results = worker.run(3)
            self.assertEqual(results[-1].job_type, "prepare-translation")
            artifacts = store.artifacts_for_subject(
                plan.subject_key, stage="accepted-translation"
            )
            self.assertEqual(artifacts[0]["language"], "ja")
            self.assertEqual(artifacts[0]["payload"]["term"], "\u691c\u67fb")
            self.assertEqual(artifacts[0]["payload"]["reading"], "\u3051\u3093\u3055")
            self.assertEqual(store.status()["counts"]["translations"], 1)

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
