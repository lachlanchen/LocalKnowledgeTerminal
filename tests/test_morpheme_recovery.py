from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.morpheme_prompting import MORPHEME_PROMPT_POLICY
from lkt.morpheme_recovery import (
    MORPHEME_REPAIR_MAX_TOKENS,
    build_morpheme_repair_prompt,
    morpheme_context_fingerprint,
    morpheme_coverage,
    select_morpheme_checkpoint,
)


def part(surface, kind="root", meaning="fixture meaning"):
    return {"surface": surface, "canonical_form": surface, "kind": kind,
            "language": "en", "meaning": meaning, "confidence": 0.8, "evidence_ids": []}


def incomplete():
    return {"parts": [part("repainted", "free"), part("re", "prefix"), part("ed", "suffix")],
            "derivatives": [{"term": "painter", "note": "shares lexical base", "component_forms": ["paint"]}]}


def complete():
    return {"parts": [part("re", "prefix"), part("paint"), part("ed", "suffix")]}


def context():
    return {"evidence": [{"headword": "repainted", "excerpt": "paint again"}],
            "required_parts": [], "previous_parts": [], "known_neighbors": [],
            "diagnostics": {"selected_evidence_ids": ["book-1"]}}


class MorphemeRepairPromptTests(unittest.TestCase):
    def test_gap_is_a_text_span_not_an_invented_root(self):
        info = morpheme_coverage(incomplete(), "repainted")
        self.assertEqual(info["whole_word_part_indices"], [0])
        self.assertEqual(info["uncovered_spans"], [{"surface": "paint", "start": 2, "end": 7}])
        self.assertNotIn("kind", info["uncovered_spans"][0])

    def test_suggestions_keep_model_order_and_repeated_real_spans(self):
        info = morpheme_coverage({"parts": [part("go"), part("go")]}, "gogo")
        self.assertEqual([(p["start"], p["end"]) for p in info["aligned_suggestions"]], [(0, 2), (2, 4)])
        self.assertEqual(info["uncovered_spans"], [])

    def test_unmatched_suggestion_is_not_fabricated_into_a_span(self):
        info = morpheme_coverage({"parts": [part("xyz")]}, "repainted")
        self.assertEqual(info["aligned_suggestions"], [])
        self.assertEqual(info["unmatched_suggestions"][0]["surface"], "xyz")
        self.assertEqual(info["uncovered_spans"][0]["surface"], "repainted")

    def test_nonobjects_empty_and_punctuation_are_safe(self):
        self.assertEqual(morpheme_coverage(None, "go-go")["uncovered_spans"][0]["surface"], "gogo")
        self.assertEqual(morpheme_coverage({"parts": [None]}, "")["uncovered_spans"], [])

    def test_short_prompt_preserves_book_and_missing_letters_without_derivative_payload(self):
        prompt, trace = build_morpheme_repair_prompt("repainted", context(), incomplete(), "incomplete")
        self.assertIn("paint again", prompt)
        self.assertIn('"surface": "paint"', prompt)
        self.assertNotIn("painter", prompt)
        self.assertIn("No derivatives", prompt)
        self.assertIn("not asserted roots", prompt)
        self.assertEqual(trace["max_output_tokens"], 512)

    def test_book_boundaries_and_meaningful_exact_entry_survive(self):
        ctx = context()
        ctx["evidence"] = [{"headword": "other", "excerpt": "irrelevant " * 1000}] + ctx["evidence"]
        ctx["required_parts"] = [{"surface": "re", "kind": "prefix"}, {"surface": "painted"}]
        prompt, _ = build_morpheme_repair_prompt("repainted", ctx, incomplete(), "invalid")
        self.assertIn("BOOK-FIXED ORDERED PARTS", prompt)
        self.assertIn("paint again", prompt)
        self.assertIn('"surface": "painted"', prompt)

    def test_large_context_is_bounded_without_mutating_saved_input(self):
        ctx = context()
        ctx["evidence"] *= 40
        ctx["evidence"][0]["excerpt"] = "repainted " + "evidence " * 10000
        before = deepcopy(ctx)
        prompt, trace = build_morpheme_repair_prompt("repainted", ctx, incomplete(), "reason " * 1000)
        self.assertEqual(ctx, before)
        self.assertLessEqual(trace["excerpt_characters"], 1050)
        self.assertLessEqual(trace["selected_records"], 3)
        self.assertLess(len(prompt), 6000)


class MorphemeCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.context = context()
        self.fingerprint = morpheme_context_fingerprint("repainted", self.context)
        self.draft = {
            "artifact_id": "draft-1", "created_at": "2026-09-13T01:00:01Z",
            "payload": {"term": "repainted", "model": "local-fixture",
                        "inference_policy": MORPHEME_PROMPT_POLICY,
                        "input_fingerprint": self.fingerprint, "value": incomplete()},
        }

    def select(self, drafts=None, contexts=None):
        return select_morpheme_checkpoint(
            drafts if drafts is not None else [self.draft], contexts or [],
            term="repainted", fingerprint=self.fingerprint,
            prompt_policy=MORPHEME_PROMPT_POLICY, model_name="local-fixture",
        )

    def test_matching_checkpoint_is_copied_not_mutated(self):
        selected = self.select()
        self.assertEqual(selected, self.draft)
        selected["payload"]["value"]["parts"].clear()
        self.assertTrue(self.draft["payload"]["value"]["parts"])

    def test_changed_evidence_model_policy_or_term_is_not_reused(self):
        for field, value in (("input_fingerprint", "changed"), ("model", "different"),
                             ("inference_policy", "old"), ("term", "different")):
            draft = deepcopy(self.draft)
            draft["payload"][field] = value
            self.assertIsNone(self.select([draft]))

    def test_legacy_draft_requires_matching_earlier_prompt_snapshot(self):
        self.draft["payload"].pop("input_fingerprint")
        snapshot = {"created_at": "2026-09-13T01:00:00Z",
                    "payload": {"term": "repainted", "policy": MORPHEME_PROMPT_POLICY, "context": self.context}}
        self.assertIsNotNone(self.select(contexts=[snapshot]))
        snapshot["created_at"] = "2026-09-13T01:00:02Z"
        self.assertIsNone(self.select(contexts=[snapshot]))

    def test_legacy_uses_closest_snapshot_not_an_older_convenient_match(self):
        self.draft["payload"].pop("input_fingerprint")
        earlier = {"created_at": "2026-09-13T00:00:00Z",
                   "payload": {"term": "repainted", "policy": MORPHEME_PROMPT_POLICY, "context": self.context}}
        later = deepcopy(earlier)
        later["created_at"] = "2026-09-13T01:00:00Z"
        later["payload"]["context"]["evidence"] = []
        self.assertIsNone(self.select(contexts=[earlier, later]))

    def test_no_trace_or_invalid_draft_is_not_reused(self):
        self.draft["payload"].pop("input_fingerprint")
        self.assertIsNone(self.select())
        self.draft["payload"]["input_fingerprint"] = self.fingerprint
        self.draft["payload"]["value"] = {"parts": []}
        self.assertIsNone(self.select())

    def test_fingerprint_changes_with_book_input_not_neighbor_rotation(self):
        ctx = deepcopy(self.context)
        ctx["known_neighbors"] = ["painter"]
        ctx["previous_parts"] = [part("paint")]
        self.assertEqual(morpheme_context_fingerprint("repainted", ctx), self.fingerprint)
        ctx["diagnostics"]["selected_evidence_ids"] = ["new-edition"]
        self.assertNotEqual(morpheme_context_fingerprint("repainted", ctx), self.fingerprint)


class MorphemeRecoveryWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = KnowledgeStore(Path(temp.name) / "knowledge.sqlite3")
        term = self.store.upsert_term("en", "repainted", status="accepted")
        subject = "term:" + term
        record = {"corpus_id": "fixture", "entry_id": "repainted", "headword": "repainted",
                  "kind": "dictionary-sense", "excerpt": "painted again"}
        eid = self.store.add_evidence("fixture", "repainted", excerpt=record["excerpt"], payload=record)
        record["knowledge_evidence_id"] = eid
        seed = self.store.enqueue_job("fixture", subject, subject_entity_id=term)
        self.store.save_job_artifact(seed, "retrieved-evidence", {"records": [record]}, language="en", validation_state="candidate")
        self.store.finish_job(seed)
        job_id = self.store.enqueue_job("split-morphemes", subject, subject_entity_id=term)
        self.job = {"job_id": job_id, "subject_entity_id": term, "subject_key": subject}
        self.model = Mock(model_name="local-fixture")
        retriever = Mock()
        retriever.component_evidence.return_value = []
        self.worker = PreparationWorker(self.store, retriever, self.model, pronouncer=Mock())
        self.worker._lexical_view = Mock(return_value={"nodes": [], "edges": []})

    def responses(self, *values):
        self.model.reset_mock()
        self.model.complete_json.side_effect = [
            {"value": deepcopy(value), "model": "local-fixture", "metrics": {"requests": 1}}
            for value in values
        ]

    def accepted(self):
        return self.store.artifacts_for_job(self.job["job_id"], stage="accepted-morpheme-split")[-1]["payload"]

    def test_small_repair_keeps_first_derivatives_and_partial_checkpoint(self):
        self.responses(incomplete(), complete())
        self.worker._split_morphemes(self.job)
        self.assertEqual(self.model.complete_json.call_count, 2)
        review = self.model.complete_json.call_args
        self.assertEqual(review.kwargs["max_tokens"], MORPHEME_REPAIR_MAX_TOKENS)
        self.assertIn("SMALL MORPHEME REPAIR", review.args[1])
        accepted = self.accepted()
        self.assertEqual([p["surface"] for p in accepted["parts"]], ["re", "paint", "ed"])
        self.assertEqual([p["term"] for p in accepted["related_terms"]], ["painter"])
        partial = self.store.artifacts_for_job(self.job["job_id"], stage="partial-morpheme-analysis")[-1]
        self.assertEqual(partial["validation_state"], "candidate")
        self.assertEqual(partial["payload"]["proposed_parts"], incomplete()["parts"])
        self.assertEqual(partial["payload"]["derivatives"], incomplete()["derivatives"])

    def test_partial_attempt_retry_reuses_draft_and_makes_only_one_repair_call(self):
        self.responses(incomplete(), incomplete())
        self.worker._split_morphemes(self.job)
        self.assertEqual(self.accepted()["analysis_scope"], "partial")
        self.assertTrue(self.accepted()["refinement_needed"])
        original = self.store.artifacts_for_job(self.job["job_id"], stage="model-morpheme-draft")
        self.responses(complete())
        self.worker._split_morphemes(self.job)
        self.model.complete_json.assert_called_once()
        self.assertIn("SMALL MORPHEME REPAIR", self.model.complete_json.call_args.args[1])
        self.assertEqual(self.store.artifacts_for_job(self.job["job_id"], stage="model-morpheme-draft"), original)
        info = self.accepted()["morphology_inference"]
        self.assertEqual(info["draft_checkpoint_artifact_id"], original[-1]["artifact_id"])
        self.assertEqual(info["fresh_model_calls"], 1)
        self.assertEqual(self.accepted()["analysis_scope"], "complete")
        self.assertEqual([p["surface"] for p in self.accepted()["parts"]], ["re", "paint", "ed"])
        self.assertEqual([p["term"] for p in self.accepted()["related_terms"]], ["painter"])

    def test_matching_valid_checkpoint_needs_no_new_inference(self):
        self.responses(complete())
        self.worker._split_morphemes(self.job)
        self.responses()
        self.worker._split_morphemes(self.job)
        self.model.complete_json.assert_not_called()
        self.assertEqual(self.accepted()["morphology_inference"]["fresh_model_calls"], 0)

    def test_new_book_input_requires_a_fresh_draft(self):
        self.responses(complete())
        self.worker._split_morphemes(self.job)
        seed = self.store.enqueue_job("fixture-new", self.job["subject_key"])
        self.store.save_job_artifact(seed, "retrieved-evidence", {"records": [{
            "corpus_id": "new-fixture", "entry_id": "repainted", "headword": "repainted",
            "excerpt": "a new book source", "knowledge_evidence_id": "new-evidence",
        }]}, language="en", validation_state="candidate")
        self.responses(complete())
        self.worker._split_morphemes(self.job)
        self.model.complete_json.assert_called_once()
        self.assertNotIn("SMALL MORPHEME REPAIR", self.model.complete_json.call_args.args[1])
