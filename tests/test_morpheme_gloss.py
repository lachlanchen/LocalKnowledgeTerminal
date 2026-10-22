from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lkt.atomic import PreparationWorker, _clean_morpheme_meaning
from lkt.knowledge import KnowledgeStore
from lkt.morpheme_gloss import MORPHEME_GLOSS_POLICY, prepare_morpheme_gloss
from lkt.morpheme_prompting import (
    MORPHEME_PROMPT_POLICY, morpheme_structure_error, pack_morpheme_context,
)
from lkt.morpheme_recovery import morpheme_context_fingerprint
from lkt.partial_morphology import finalize_morpheme_analysis
from lkt.preparation import SemanticEnrichmentSeeder
from lkt.relation_support import _assert_morphology_finalization_invariants


def part(surface="punch", meaning="a strike", kind="root"):
    return {
        "surface": surface, "canonical_form": surface, "kind": kind,
        "language": "en", "meaning": meaning, "confidence": 0.8, "evidence_ids": [],
    }


class OptionalMorphemeGlossTests(unittest.TestCase):
    def gloss(self, value):
        return prepare_morpheme_gloss(value, _clean_morpheme_meaning(value))

    def test_apostrophes_parentheses_and_quotes_are_not_validation_failures(self):
        for value in ("one's self", "a blow (with a fist)", 'meaning "strike"', "a cafe",
                      "to hit; strike", "a person\u2019s belongings"):
            with self.subTest(value=value):
                result = self.gloss(value)
                self.assertFalse(result["meaning_pending"])
                self.assertEqual(result["meaning"], _clean_morpheme_meaning(value))

    def test_placeholders_defer_only_the_meaning(self):
        for value in ("unknown", "meaning unknown", "definition is unavailable",
                      "basic form of the word 'punch'", "base form", "N/A"):
            with self.subTest(value=value):
                result = self.gloss(value)
                self.assertEqual(result["meaning"], "")
                self.assertTrue(result["meaning_pending"])
                self.assertEqual(result["meaning_proposal"], value)
                self.assertEqual(result["meaning_issue"], "placeholder-gloss")

    def test_placeholder_lists_are_recognized_before_lossy_cleanup(self):
        for value in (["N/A"], ["N/A", "unknown"], ["", "N/A"]):
            with self.subTest(value=value):
                result = self.gloss(value)
                self.assertEqual(result["meaning"], "")
                self.assertEqual(result["meaning_issue"], "placeholder-gloss")
                self.assertEqual(result["meaning_proposal"], value)

    def test_raw_placeholders_survive_unicode_or_empty_cleaned_text(self):
        for value in (" N/A. ", "\uFF2E\uFF0F\uFF21"):
            for cleaned in ("N or A", ""):
                with self.subTest(value=value, cleaned=cleaned):
                    result = prepare_morpheme_gloss(value, cleaned)
                    self.assertEqual(result["meaning_issue"], "placeholder-gloss")
                    self.assertTrue(result["meaning_pending"])
                    self.assertEqual(result["meaning_proposal"], value)

    def test_mixed_list_does_not_discard_the_useful_meaning(self):
        result = self.gloss(["N/A", "to strike"])
        self.assertFalse(result["meaning_pending"])
        self.assertIn("to strike", result["meaning"])

    def test_useful_cautious_explanations_are_not_discarded(self):
        for value in ("uncertain but probably to strike", "an unknown person",
                      "a root of a plant", "to be free"):
            self.assertFalse(self.gloss(value)["meaning_pending"])

    def test_absent_or_structured_values_do_not_become_fake_word_meanings(self):
        for value in (None, "", [], {}, {"en": "strike"}, 12, True):
            with self.subTest(value=value):
                result = self.gloss(value)
                self.assertEqual(result["meaning"], "")
                self.assertTrue(result["meaning_pending"])
                self.assertEqual(result["meaning_proposal"], value)

    def test_non_english_or_damaged_glosses_remain_repairable_proposals(self):
        for value in ("\u6253\u51fb", "<b>strike</b>", r"\text{strike}", "str\ufffdke"):
            with self.subTest(value=value):
                result = self.gloss(value)
                self.assertTrue(result["meaning_pending"])
                self.assertEqual(result["meaning_proposal"], value)

    def test_string_lists_can_still_supply_a_useful_gloss(self):
        self.assertEqual(self.gloss(["look", "see"]), {
            "meaning": "look or see", "meaning_pending": False,
        })

    def test_pending_proposal_is_copied_without_mutating_the_model_draft(self):
        proposal = {"en": ["strike"]}
        result = self.gloss(proposal)
        result["meaning_proposal"]["en"].clear()
        self.assertEqual(proposal, {"en": ["strike"]})

    def test_missing_gloss_is_not_a_structural_gap(self):
        value = {"parts": [part()]}
        value["parts"][0].pop("meaning")
        self.assertEqual(morpheme_structure_error(value, "punch", []), "")
        result = finalize_morpheme_analysis(value, "punch", [], [])
        self.assertEqual(result["analysis_scope"], "complete")
        self.assertEqual(result["unresolved_spans"], [])
        self.assertNotIn("meaning", result["parts"][0])

    def test_canonical_forms_book_boundaries_and_letter_coverage_still_matter(self):
        for canonical in (None, "", 12):
            value = {"parts": [part()]}
            value["parts"][0]["canonical_form"] = canonical
            self.assertIn("canonical", morpheme_structure_error(value, "punch", []))
        self.assertTrue(morpheme_structure_error({"parts": [part("punc")]}, "punch", []))
        self.assertTrue(morpheme_structure_error(
            {"parts": [part()]}, "punch", [{"surface": "pun"}, {"surface": "ch"}],
        ))


class OptionalGlossWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = KnowledgeStore(Path(temporary.name) / "knowledge.sqlite3")
        self.term = self.store.upsert_term("en", "punch", status="accepted")
        self.subject = "term:" + self.term
        self.record = {
            "corpus_id": "fixture-dictionary", "entry_id": "punch",
            "headword": "punch", "kind": "dictionary-sense",
            "excerpt": "A punch is a blow delivered with a fist.",
        }
        eid = self.store.add_evidence(
            self.record["corpus_id"], self.record["entry_id"],
            excerpt=self.record["excerpt"], payload=self.record,
        )
        self.record["knowledge_evidence_id"] = eid
        seed = self.store.enqueue_job("fixture", self.subject, subject_entity_id=self.term)
        self.store.save_job_artifact(
            seed, "retrieved-evidence", {"records": [self.record]},
            language="en", validation_state="candidate",
        )
        self.store.finish_job(seed)
        self.retriever = Mock()
        self.retriever.component_evidence.return_value = []
        self.retriever.retrieve.return_value = [self.record]
        self.model = Mock(model_name="local-fixture")
        self.worker = PreparationWorker(self.store, self.retriever, self.model, pronouncer=Mock())
        self.worker._lexical_view = Mock(return_value={"nodes": [], "edges": []})

    def job(self, version=""):
        jid = self.store.enqueue_job(
            "split-morphemes", self.subject, subject_entity_id=self.term,
            prompt_version=version,
        )
        return {"job_id": jid, "subject_entity_id": self.term,
                "subject_key": self.subject, "prompt_version": version}

    def respond(self, *values):
        self.model.reset_mock()
        self.model.complete_json.side_effect = [
            {"value": deepcopy(value), "model": "local-fixture", "metrics": {"requests": 1}}
            for value in values
        ]

    def accepted(self, job):
        return self.store.artifacts_for_job(job["job_id"], stage="accepted-morpheme-split")[-1]["payload"]

    def assert_invariants(self):
        with closing(self.store._connect()) as connection:
            _assert_morphology_finalization_invariants(connection)

    def test_placeholder_gloss_keeps_structure_derivatives_and_the_raw_proposal(self):
        raw = {"parts": [part(meaning="basic form of the word 'punch'")],
               "derivatives": [
                   {"term": "punching", "component_forms": ["punch"]},
                   {"term": "punches", "component_forms": ["punch"]},
               ]}
        job = self.job()
        self.respond(raw)
        self.worker._split_morphemes(job)
        self.model.complete_json.assert_called_once()
        accepted = self.accepted(job)
        self.assertEqual(accepted["analysis_scope"], "complete")
        self.assertFalse(accepted["refinement_needed"])
        self.assertEqual(accepted["unresolved_spans"], [])
        self.assertEqual({r["term"] for r in accepted["related_terms"]}, {"punching", "punches"})
        component = accepted["parts"][0]
        self.assertEqual(component["meaning"], "")
        self.assertTrue(component["meaning_pending"])
        self.assertEqual(component["meaning_proposal"], raw["parts"][0]["meaning"])
        self.assertEqual(accepted["morphology_inference"]["glosses"], {
            "policy": MORPHEME_GLOSS_POLICY, "pending_forms": ["punch"],
        })
        draft = self.store.artifacts_for_job(job["job_id"], stage="model-morpheme-draft")[-1]
        self.assertEqual(draft["payload"]["value"], raw)
        state = self.store.lexical_semantic_enrichment_state(component["morpheme_id"])
        self.assertIsNotNone(state)
        self.assertNotEqual(state["status"], "applied")
        self.assert_invariants()

    def test_missing_gloss_does_not_trigger_a_second_structural_model_request(self):
        raw = {"parts": [part()]}
        raw["parts"][0].pop("meaning")
        job = self.job()
        self.respond(raw)
        self.worker._split_morphemes(job)
        self.model.complete_json.assert_called_once()
        self.assertTrue(self.accepted(job)["parts"][0]["meaning_pending"])
        self.assertEqual(self.accepted(job)["morphology_inference"]["review_reason"], "")
        self.assertEqual(self.store.artifacts_for_job(job["job_id"], stage="model-morpheme-review-draft"), [])

    def test_punctuated_gloss_remains_visible_not_pending(self):
        job = self.job()
        self.respond({"parts": [part(meaning="to strike one's target (directly)")]})
        self.worker._split_morphemes(job)
        result = self.accepted(job)["parts"][0]
        self.assertFalse(result["meaning_pending"])
        self.assertIn("one's", result["meaning"])
        self.assertIn("(directly)", result["meaning"])

    def test_matching_pre_fix_draft_replays_without_new_inference(self):
        job = self.job()
        raw = {"parts": [part(meaning="basic form of the word 'punch'")],
               "derivatives": [{"term": "punching", "component_forms": ["punch"]}]}
        context = pack_morpheme_context(
            "punch", [self.record], [], [], {}, job["job_id"],
        )
        artifact = self.store.save_job_artifact(
            job["job_id"], "model-morpheme-draft",
            {"term": "punch", "value": raw, "model": "local-fixture",
             "inference_policy": MORPHEME_PROMPT_POLICY,
             "input_fingerprint": morpheme_context_fingerprint("punch", context)},
            language="en", validation_state="candidate",
        )
        self.model.complete_json.side_effect = AssertionError("must reuse the saved draft")
        self.worker._split_morphemes(job)
        self.model.complete_json.assert_not_called()
        accepted = self.accepted(job)
        self.assertEqual(accepted["morphology_inference"]["draft_checkpoint_artifact_id"], artifact)
        self.assertEqual(accepted["morphology_inference"]["fresh_model_calls"], 0)
        self.assertEqual([r["term"] for r in accepted["related_terms"]], ["punching"])
        self.assertTrue(accepted["parts"][0]["meaning_pending"])

    def test_deferred_gloss_does_not_erase_a_previous_book_meaning(self):
        first = self.job("first")
        self.respond({"parts": [part(meaning="a strike")]})
        self.worker._split_morphemes(first)
        root = self.accepted(first)["parts"][0]["morpheme_id"]
        meaning_id = self.store.add_entity_meaning(
            root, "printed explanation", basis="book",
            evidence_ids=[self.record["knowledge_evidence_id"]],
        )
        second = self.job("second")
        self.respond({"parts": [part(meaning=None)]})
        self.worker._split_morphemes(second)
        with closing(self.store._connect()) as connection:
            meanings = self.store._accepted_meaning_records(connection, (root,))[root]
            cache = connection.execute("SELECT meaning FROM morphemes WHERE entity_id=?", (root,)).fetchone()[0]
        self.assertTrue(any(r["meaning_id"] == meaning_id for r in meanings))
        self.assertEqual(cache, "a strike")
        self.assertEqual(self.accepted(second)["analysis_scope"], "complete")

    def test_bad_canonical_identity_is_not_fabricated_from_a_missing_gloss(self):
        raw = {"parts": [part(meaning=None)]}
        raw["parts"][0]["canonical_form"] = None
        job = self.job()
        self.respond(raw, raw)
        self.worker._split_morphemes(job)
        accepted = self.accepted(job)
        self.assertEqual(accepted["analysis_scope"], "partial")
        self.assertEqual(accepted["parts"], [])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM morphemes").fetchone()[0], 0)
        self.assert_invariants()

    def test_partial_structure_keeps_real_spans_even_when_the_gloss_is_pending(self):
        raw = {"parts": [part("pun", meaning=None), part("xyz", meaning=None, kind="suffix")]}
        job = self.job()
        self.respond(raw, raw)
        self.worker._split_morphemes(job)
        accepted = self.accepted(job)
        self.assertEqual([p["surface"] for p in accepted["parts"]], ["pun"])
        self.assertTrue(accepted["parts"][0]["meaning_pending"])
        self.assertEqual(accepted["analysis_scope"], "partial")
        self.assertEqual(accepted["unresolved_spans"], [{"surface": "ch", "start": 3, "end": 5}])
        self.assert_invariants()

    def test_existing_semantic_lane_can_fill_the_deferred_gloss_without_resplitting(self):
        job = self.job()
        self.respond({"parts": [part(meaning=None)]})
        self.worker._split_morphemes(job)
        root = self.accepted(job)["parts"][0]["morpheme_id"]
        seeder = SemanticEnrichmentSeeder(self.store, model="local-meaning-test")
        model = Mock(model_name="local-meaning-test")
        model.complete_json.return_value = {
            "value": {"meaning": "a strike delivered with the fist", "confidence": 0.7, "evidence_ids": ["E1"]},
            "model": "local-meaning-test", "metrics": {"requests": 1},
        }
        for _ in range(2):
            plan = seeder.run_once()
            self.assertEqual(plan["entity_id"], root)
            result = PreparationWorker(self.store, self.retriever, model).run_once(("prepare-entity-meaning",))
            if result.status == "complete":
                break
            self.assertEqual(result.status, "failed")
            model.complete_json.assert_not_called()
        self.assertEqual(result.status, "complete")
        model.complete_json.assert_called_once()
        self.model.complete_json.assert_called_once()
        state = self.store.lexical_semantic_enrichment_state(root)
        self.assertEqual(state["desired_fingerprint"], state["applied_fingerprint"])
        context = self.store.semantic_enrichment_context(root)
        self.assertTrue(any(m["definition"] == "a strike delivered with the fist" for m in context["accepted_meanings"]))
        self.assert_invariants()

    def test_subject_archiving_still_fences_all_graph_writes(self):
        job = self.job()

        def completion(*args, **kwargs):
            with closing(self.store._connect()) as connection, connection:
                connection.execute("UPDATE entities SET status='archived' WHERE entity_id=?", (self.term,))
            return {"value": {"parts": [part(meaning=None)]}, "model": "local-fixture"}

        self.model.complete_json.side_effect = completion
        with self.assertRaisesRegex(Exception, "archived"):
            self.worker._split_morphemes(job)
        self.assertEqual(self.store.artifacts_for_job(job["job_id"], stage="accepted-morpheme-split"), [])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM morphemes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
