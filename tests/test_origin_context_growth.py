from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lkt.atomic import (
    PreparationWorker,
    _normalize_origin_draft,
    _origin_context_evidence,
    _origin_draft_review_reason,
    _origin_source_record_matches,
)
from lkt.knowledge import KnowledgeStore


class OriginContextGrowthTests(unittest.TestCase):
    @staticmethod
    def record(excerpt, headword="related", entry="fixture-entry"):
        return {
            "kind": "entry", "corpus_id": "test-origin-book",
            "entry_id": entry, "headword": headword, "excerpt": excerpt,
            "locator": "fixture page",
        }

    def test_related_body_mention_is_context_not_citation_authority(self):
        record = self.record("A discussion places fixtureword beside olderform.")
        self.assertFalse(_origin_source_record_matches("fixtureword", record))
        context = _origin_context_evidence("fixtureword", [record])
        self.assertEqual(len(context), 1)
        self.assertTrue(context[0]["context_only"])
        self.assertNotIn("evidence_id", context[0])

    def test_context_does_not_match_word_substrings(self):
        self.assertEqual(_origin_context_evidence(
            "read", [self.record("This book discusses readiness.")],
        ), [])

    def test_context_window_retains_a_late_target_mention(self):
        excerpt = "Background material. " * 100 + "fixtureword is discussed here."
        context = _origin_context_evidence("fixtureword", [self.record(excerpt)])
        self.assertIn("fixtureword", context[0]["excerpt"])
        self.assertLessEqual(len(context[0]["excerpt"]), 1200)

    def test_context_is_deduplicated_bounded_and_exact_first(self):
        related = self.record("A discussion of fixtureword.")
        exact = self.record("An exact entry.", "fixtureword", "exact")
        context = _origin_context_evidence("fixtureword", [related, related, exact], limit=1)
        self.assertEqual([item["headword"] for item in context], ["fixtureword"])

    def test_missing_metadata_is_retained_with_conservative_confidence(self):
        value, changes = _normalize_origin_draft(
            {"component_id": "root", "steps": [{
                "form": "olderform", "meaning": "an earlier form", "confidence": 0.99,
            }]}, component_id="root", modern_word="fixtureword", base_form="fixture",
        )
        step = value["steps"][0]
        self.assertEqual((step["language"], step["period"]), ("und", "unspecified"))
        self.assertLessEqual(step["confidence"], 0.35)
        self.assertEqual(len(step["metadata_warnings"]), 2)
        self.assertIn("retained-unspecified-historical-language", changes)

    def test_review_accepts_the_same_five_steps_requested_by_the_prompt(self):
        value = {"component_id": "root", "steps": [
            {"form": "historical" + str(index), "language": "la", "period": "Latin"}
            for index in range(5)
        ]}
        kwargs = dict(component_id="root", modern_word="fixtureword", base_form="fixture",
                      fixed_provenance_ids=set(), evidence=[])
        self.assertEqual(_origin_draft_review_reason(value, **kwargs), "")
        value["steps"].append({"form": "sixth"})
        self.assertTrue(_origin_draft_review_reason(value, **kwargs))

    def test_worker_retains_partial_history_and_supplies_related_book_context(self):
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term = store.upsert_term("en", "fixtureword")
            root = store.upsert_morpheme("en", "fixture", "root", "an example")
            subject = "term:" + term
            fixture = store.enqueue_job("fixture", subject, subject_entity_id=term)
            store.save_job_artifact(fixture, "accepted-morpheme-split", {"parts": [{
                "morpheme_id": root, "canonical_form": "fixture", "surface": "fixture",
                "kind": "root", "meaning": "an example", "basis": "model", "evidence_ids": [],
            }]}, language="en", validation_state="accepted")
            record = self.record("A discussion places fixtureword beside olderform.")
            store.save_job_artifact(fixture, "retrieved-evidence", {"records": [record]}, language="en")
            store.finish_job(fixture)
            job = store.enqueue_job("expand-origin-branches", subject, subject_entity_id=term,
                                    language="en", prompt_version="context-growth-test")

            class Retriever:
                def origin_evidence(self, form):
                    return []

            class Model:
                model_name = "local-test-model"

                def __init__(self):
                    self.prompts = []

                def complete_json(self, system, prompt, **kwargs):
                    self.prompts.append(prompt)
                    return {"value": {"component_id": root, "steps": [{
                        "form": "olderform", "meaning": "an earlier form", "confidence": 0.99,
                    }]}, "model": self.model_name}

            model = Model()
            result = PreparationWorker(store, Retriever(), model).run_once(("expand-origin-branches",))
            self.assertEqual(result.status, "complete")
            accepted = store.artifacts_for_job(job, stage="accepted-origin-branches")[-1]["payload"]
            self.assertEqual(accepted["history_outcome"], "history-found")
            step = accepted["branches"][0]["steps"][0]
            self.assertEqual(step["language"], "und")
            self.assertEqual(step["basis"], "model")
            self.assertEqual(step["edge_basis"], "model")
            self.assertEqual(step["evidence_ids"], [])
            self.assertIn("historical_form_id", step)
            self.assertIn(record["excerpt"], model.prompts[0])
            self.assertEqual(len(model.prompts), 1)


if __name__ == "__main__":
    unittest.main()
