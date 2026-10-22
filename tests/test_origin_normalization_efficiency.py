from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker, _origin_draft_review_reason
from lkt.knowledge import KnowledgeStore


class OriginNormalizationEfficiencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = KnowledgeStore(Path(temporary.name) / "knowledge.sqlite3")

    @staticmethod
    def step(form="priorform", *, language="fro", period="medieval"):
        return {
            "form": form,
            "language": language,
            "period": period,
            "meaning": "synthetic fixture meaning",
            "confidence": 0.7,
            "evidence_ids": [],
        }

    def subject(self, word="testword"):
        term = self.store.upsert_term("en", word, status="accepted")
        component = self.store.upsert_morpheme(
            "en", word, "free", "synthetic fixture meaning",
        )
        record = {
            "corpus_id": "word-origins",
            "entry_id": "synthetic-normalization-" + word,
            "kind": "entry",
            "headword": word,
            "locator": "synthetic test record",
            "excerpt": (
                "Synthetic normalization fixture, not a production claim. "
                "The historical spelling priorform is printed here."
            ),
        }
        evidence = self.store.add_evidence(
            record["corpus_id"], record["entry_id"],
            locator=record["locator"], excerpt=record["excerpt"], payload=record,
        )
        self.store.apply_relation_decision(
            term, term, component, "has-component", basis="model",
            confidence=0.7, properties={"ordinal": 0, "surface": word},
        )
        seed = self.store.enqueue_job(
            "fixture", "term:" + term, subject_entity_id=term,
        )
        self.store.save_job_artifact(
            seed, "retrieved-evidence",
            {"records": [{**record, "knowledge_evidence_id": evidence}]},
            language="en", validation_state="accepted",
        )
        self.store.save_job_artifact(
            seed, "accepted-morpheme-split",
            {"term": word, "parts": [{
                "morpheme_id": component,
                "surface": word,
                "canonical_form": word,
                "kind": "free",
                "language": "en",
                "meaning": "synthetic fixture meaning",
                "basis": "model",
                "confidence": 0.7,
                "evidence_ids": [],
            }]},
            language="en", validation_state="accepted",
        )
        self.store.finish_job(seed)
        return term, component, evidence

    def run_origin(self, term, drafts, *, book_steps=()):
        model = mock.Mock(model_name="local-fixture-model")
        model.complete_json.side_effect = [
            {
                "value": value,
                "model": "local-fixture-model",
                "metrics": {"requests": 1, "elapsed_seconds": index + 2},
            }
            for index, value in enumerate(drafts)
        ]
        retriever = mock.Mock()
        retriever.origin_evidence.return_value = []
        worker = PreparationWorker(
            self.store, retriever, model, pronouncer=mock.Mock(),
        )
        job = self.store.enqueue_job(
            "expand-origin-branches", "term:" + term,
            subject_entity_id=term, model=model.model_name,
        )
        with mock.patch("lkt.atomic._book_origin_steps", return_value=list(book_steps)):
            result = worker.run_once(("expand-origin-branches",))
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "complete", str(result))
        accepted = self.store.artifacts_for_job(job, stage="accepted-origin-branches")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["validation_state"], "accepted")
        return accepted[0]["payload"], model, job

    def test_book_context_endpoint_cleanup_uses_one_call_for_multiple_words(self):
        for word in ("testword", "sampleword"):
            with self.subTest(word=word):
                term, component, evidence = self.subject(word)
                draft = {"component_id": component, "steps": [
                    self.step(),
                    self.step(word, language="en", period="Modern English"),
                ]}
                untouched = copy.deepcopy(draft)
                accepted, model, job = self.run_origin(term, [draft])
                self.assertEqual(draft, untouched)
                model.complete_json.assert_called_once()
                self.assertIn("Synthetic normalization fixture", model.complete_json.call_args.args[1])
                self.assertEqual([step["form"] for step in accepted["branches"][0]["steps"]], ["priorform"])
                self.assertEqual(accepted["branches"][0]["steps"][0]["evidence_ids"], [evidence])
                review = accepted["origin_review"]
                self.assertEqual(review["policy"], "normalize-before-review-v1")
                self.assertEqual(review["reason"], "")
                self.assertEqual([item["stage"] for item in review["inference_trace"]], ["draft"])
                self.assertEqual(review["inference_trace"][0]["metrics"]["elapsed_seconds"], 2)
                self.assertEqual(self.store.artifacts_for_job(job, stage="model-origin-review-draft"), [])
                normalized = self.store.artifacts_for_job(job, stage="normalized-origin-draft")
                self.assertIn("removed-redundant-modern-endpoint", normalized[-1]["payload"]["normalizations"])

    def test_system_identity_repair_does_not_spend_a_second_call(self):
        term, component, _ = self.subject()
        accepted, model, _ = self.run_origin(term, [{
            "component_id": "model-copied-the-wrong-id", "steps": [self.step()],
        }])
        model.complete_json.assert_called_once()
        self.assertEqual(accepted["branches"][0]["component_id"], component)

    def test_uncertain_metadata_keeps_useful_history_without_review(self):
        term, component, _ = self.subject()
        accepted, model, _ = self.run_origin(term, [{
            "component_id": component,
            "steps": [self.step(language="", period="")],
        }])
        model.complete_json.assert_called_once()
        step = accepted["branches"][0]["steps"][0]
        self.assertEqual(step["language"], "und")
        self.assertEqual(step["period"], "unspecified")

    def test_empty_history_after_cleanup_still_gets_one_bounded_review(self):
        term, component, _ = self.subject()
        accepted, model, job = self.run_origin(term, [
            {"component_id": component, "steps": [self.step("testword", language="en", period="Modern English")]},
            {"component_id": component, "steps": [self.step()]},
        ])
        self.assertEqual(model.complete_json.call_count, 2)
        review = accepted["origin_review"]
        self.assertIn("one to five", review["reason"])
        self.assertEqual([item["stage"] for item in review["inference_trace"]], ["draft", "review"])
        self.assertEqual([item["metrics"]["elapsed_seconds"] for item in review["inference_trace"]], [2, 3])
        self.assertEqual(len(self.store.artifacts_for_job(job, stage="model-origin-review-draft")), 1)

    def test_structural_error_still_gets_a_review(self):
        term, component, _ = self.subject()
        accepted, model, _ = self.run_origin(term, [
            {"component_id": component, "steps": [42]},
            {"component_id": component, "steps": [self.step()]},
        ])
        self.assertEqual(model.complete_json.call_count, 2)
        self.assertEqual(accepted["origin_review"]["reason"], "a step is not an object")

    def test_bad_review_does_not_erase_useful_first_draft(self):
        term, component, _ = self.subject()
        accepted, model, job = self.run_origin(term, [
            {"component_id": component, "steps": [self.step(), 42]},
            {"component_id": component, "steps": "malformed reviewer output"},
        ])
        self.assertEqual(model.complete_json.call_count, 2)
        self.assertEqual([step["form"] for step in accepted["branches"][0]["steps"]], ["priorform"])
        reviewed = self.store.artifacts_for_job(job, stage="model-origin-review-draft")
        self.assertEqual(reviewed[0]["validation_state"], "rejected")

    def test_citations_are_repaired_from_records_not_trusted_from_model(self):
        term, component, evidence = self.subject()
        step = {**self.step(), "evidence_ids": ["invented-citation"]}
        accepted, model, _ = self.run_origin(term, [{"component_id": component, "steps": [step]}])
        model.complete_json.assert_called_once()
        self.assertEqual(accepted["branches"][0]["steps"][0]["evidence_ids"], [evidence])
        self.assertEqual(step["evidence_ids"], ["invented-citation"])
        reason = _origin_draft_review_reason(
            {"component_id": component, "steps": [step]},
            component_id=component, modern_word="testword", base_form="testword",
            fixed_provenance_ids={evidence}, evidence=[],
        )
        self.assertIn("not visibly supported", reason)

    def test_book_only_history_records_zero_model_calls(self):
        term, _, _ = self.subject()
        accepted, model, _ = self.run_origin(term, [], book_steps=[self.step()])
        model.complete_json.assert_not_called()
        self.assertEqual(accepted["origin_review"], {
            "policy": "normalize-before-review-v1", "reason": "", "inference_trace": [],
        })


if __name__ == "__main__":
    unittest.main()
